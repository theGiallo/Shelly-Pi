"""Downsampling and retention.

raw (7d)  ->  rollup_1m (90d)  ->  rollup_15m (forever)

Each level is built from the one below using watermarks in `meta`
(r1m_done / r15m_done = start of the first bucket not yet aggregated).
Aggregation is a pure function of the lower level, so re-running a bucket
(after a watermark rewind for late rows) just overwrites it.
Nothing is deleted until it has been rolled up.
"""

from __future__ import annotations

import logging

from .db import Database

log = logging.getLogger(__name__)

_INSERT_1M = """
INSERT OR REPLACE INTO rollup_1m(device_id, ts, n, p_sum, p_max, v_sum, i_sum,
                                 t_sum, t_n, e_wh, on_n)
SELECT device_id, (ts/60)*60, COUNT(*),
       SUM(COALESCE(power_w,0)), MAX(COALESCE(power_w,0)),
       SUM(COALESCE(voltage,0)), SUM(COALESCE(current,0)),
       COALESCE(SUM(temp_c),0), COUNT(temp_c),
       COALESCE(SUM(e_delta_wh),0), SUM(COALESCE(output,0))
FROM readings
WHERE ts >= ? AND ts < ?
GROUP BY device_id, ts/60
"""

_INSERT_15M = """
INSERT OR REPLACE INTO rollup_15m(device_id, ts, n, p_sum, p_max, v_sum, i_sum,
                                  t_sum, t_n, e_wh, on_n)
SELECT device_id, (ts/900)*900, SUM(n), SUM(p_sum), MAX(p_max), SUM(v_sum),
       SUM(i_sum), SUM(t_sum), SUM(t_n), SUM(e_wh), SUM(on_n)
FROM rollup_1m
WHERE ts >= ? AND ts < ?
GROUP BY device_id, ts/900
"""

_SET_META = "INSERT INTO meta(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v"
_CHUNK = 6 * 3600


def run(
    db: Database, now: int, lag: int, raw_days: int = 7, r1m_days: int = 90,
) -> dict[str, int]:
    """One maintenance pass. `lag` keeps the newest samples out of the rollup
    until they have surely been flushed (flush interval + margin)."""
    stats = {"r1m_rows": 0, "r15m_rows": 0, "raw_deleted": 0, "r1m_deleted": 0}

    # ---- raw -> 1 minute ---------------------------------------------------
    done = db.get_meta("r1m_done")
    if done is None:
        first = db.query("SELECT MIN(ts) FROM readings")[0][0]
        if first is None:
            return stats
        done_i = first // 60 * 60
        db.set_meta("r1m_done", done_i)
    else:
        done_i = int(done)

    end = (now - lag) // 60 * 60
    start = done_i
    while start < end:
        chunk_end = min(end, start + _CHUNK)
        with db.transaction() as c:
            stats["r1m_rows"] += max(c.execute(_INSERT_1M, (start, chunk_end)).rowcount, 0)
            c.execute(_SET_META, ("r1m_done", str(chunk_end)))
        start = chunk_end
    done_i = max(done_i, end)

    # ---- 1 minute -> 15 minutes -------------------------------------------
    done15 = db.get_meta("r15m_done")
    if done15 is None:
        first = db.query("SELECT MIN(ts) FROM rollup_1m")[0][0]
        done15_i = None if first is None else first // 900 * 900
        if done15_i is not None:
            db.set_meta("r15m_done", done15_i)
    else:
        done15_i = int(done15)

    if done15_i is not None:
        end15 = done_i // 900 * 900
        start = done15_i
        while start < end15:
            chunk_end = min(end15, start + _CHUNK * 4)
            with db.transaction() as c:
                stats["r15m_rows"] += max(c.execute(_INSERT_15M, (start, chunk_end)).rowcount, 0)
                c.execute(_SET_META, ("r15m_done", str(chunk_end)))
            start = chunk_end
        done15_i = max(done15_i, end15)

    # ---- retention (never delete what is not yet rolled up) ----------------
    raw_cut = min(now - raw_days * 86400, done_i)
    stats["raw_deleted"] = db.execute("DELETE FROM readings WHERE ts < ?", (raw_cut,))
    if done15_i is not None:
        r1_cut = min(now - r1m_days * 86400, done15_i)
        stats["r1m_deleted"] = db.execute("DELETE FROM rollup_1m WHERE ts < ?", (r1_cut,))

    if any(stats.values()):
        log.info("rollup: %s", stats)
    return stats
