"""Chart and energy queries over raw / 1-minute / 15-minute data.

The finest source that still covers the start of the requested range is used,
and the range is split at the rollup watermarks so the newest part always
comes from finer data. Results are bucketed in SQL, so a month of history is
a few hundred points no matter how many raw rows exist.
"""

from __future__ import annotations

import math
from typing import Iterable

from .config import Settings
from .db import Database
from .tariff import Tariff

# bucket row: [n, p_sum, p_max, v_sum, i_sum, t_sum, t_n, e_wh, on_n]
_RAW_SQL = """
SELECT (ts/:step)*:step AS b, COUNT(*),
       SUM(COALESCE(power_w,0)), MAX(COALESCE(power_w,0)),
       SUM(COALESCE(voltage,0)), SUM(COALESCE(current,0)),
       COALESCE(SUM(temp_c),0), COUNT(temp_c),
       COALESCE(SUM(e_delta_wh),0), SUM(COALESCE(output,0))
FROM readings WHERE device_id=:dev AND ts>=:a AND ts<:b2 GROUP BY b
"""
_ROLLUP_SQL = """
SELECT (ts/:step)*:step AS b, SUM(n), SUM(p_sum), MAX(p_max), SUM(v_sum),
       SUM(i_sum), SUM(t_sum), SUM(t_n), SUM(e_wh), SUM(on_n)
FROM {table} WHERE device_id=:dev AND ts>=:a AND ts<:b2 GROUP BY b
"""


def segments(
    r1_done: int, r15_done: int, now: int, t0: int, t1: int, cfg: Settings,
) -> list[tuple[str, int, int]]:
    """Split [t0, t1) into (source, a, b) pieces, finest data for the newest part."""
    raw_start = now - cfg.raw_retention_days * 86400
    r1_start = now - cfg.r1m_retention_days * 86400
    if t0 >= raw_start:
        segs = [("readings", t0, t1)]
    elif t0 >= r1_start:
        segs = [("rollup_1m", t0, min(t1, r1_done)), ("readings", max(t0, r1_done), t1)]
    else:
        segs = [
            ("rollup_15m", t0, min(t1, r15_done)),
            ("rollup_1m", max(t0, r15_done), min(t1, r1_done)),
            ("readings", max(t0, r1_done), t1),
        ]
    return [(s, a, b) for s, a, b in segs if a < b]


def _merge(into: dict[int, list[float]], key: int, row: Iterable[float]) -> None:
    row = list(row)
    cur = into.get(key)
    if cur is None:
        into[key] = row
        return
    cur[0] += row[0]
    cur[1] += row[1]
    cur[2] = max(cur[2], row[2])
    for i in (3, 4, 5, 6, 7, 8):
        cur[i] += row[i]


def _collect(
    db: Database, cfg: Settings, now: int, device_id: int, t0: int, t1: int, step: int,
    extra: list[tuple],
) -> dict[int, list[float]]:
    r1_done = db.get_meta_int("r1m_done")
    r15_done = db.get_meta_int("r15m_done")
    buckets: dict[int, list[float]] = {}
    for source, a, b in segments(r1_done, r15_done, now, t0, t1 + 1, cfg):
        sql = _RAW_SQL if source == "readings" else _ROLLUP_SQL.format(table=source)
        for row in db.query(sql, {"step": step, "dev": device_id, "a": a, "b2": b}):
            _merge(buckets, row[0], row[1:])
    # unflushed samples held in RAM: (ts, power, voltage, current, temp, output, e_delta)
    for ts, p, v, i, t, out, e in extra:
        if t0 <= ts <= t1:
            _merge(
                buckets, ts // step * step,
                [1, p or 0.0, p or 0.0, v or 0.0, i or 0.0, t or 0.0,
                 0 if t is None else 1, e or 0.0, 1 if out else 0],
            )
    return buckets


def pick_step(span: int, points: int, coarse: bool) -> int:
    step = max(10, math.ceil(span / max(points, 1)))
    if step >= 60:
        step = math.ceil(step / 60) * 60
    if coarse or step >= 900:
        step = math.ceil(step / 900) * 900  # whole 15-minute blocks, so a price slot never splits a bucket
    return step


def history(
    db: Database, cfg: Settings, now: int, device_id: int, t0: int, t1: int,
    points: int = 300, extra: list[tuple] | None = None, tariff: Tariff | None = None,
) -> dict:
    """Columnar series on a regular grid; missing buckets are None (gaps)."""
    points = max(10, min(points, 1500))
    if t1 <= t0:
        raise ValueError("empty range")
    coarse = t0 < now - cfg.r1m_retention_days * 86400
    step = pick_step(t1 - t0, points, coarse)
    buckets = _collect(db, cfg, now, device_id, t0, t1, step, extra or [])

    costs = None
    if tariff is not None and tariff.enabled:
        costs = _costs(db, cfg, now, device_id, t0, t1, step, extra or [], tariff, buckets)

    cols: dict[str, list] = {k: [] for k in ("t", "p_avg", "p_max", "v", "i", "temp", "e_wh", "on")}
    if costs is not None:
        cols["cost"] = []
    b = t0 // step * step
    while b <= t1:
        row = buckets.get(b)
        cols["t"].append(b)
        if row and row[0] > 0:
            n = row[0]
            cols["p_avg"].append(round(row[1] / n, 2))
            cols["p_max"].append(round(row[2], 2))
            cols["v"].append(round(row[3] / n, 1))
            cols["i"].append(round(row[4] / n, 3))
            cols["temp"].append(round(row[5] / row[6], 1) if row[6] else None)
            cols["e_wh"].append(round(row[7], 3))
            cols["on"].append(round(row[8] / n, 3))
            if costs is not None:
                cols["cost"].append(round(costs.get(b, 0.0), 6))
        else:
            for k in ("p_avg", "p_max", "v", "i", "temp", "e_wh", "on"):
                cols[k].append(None)
            if costs is not None:
                cols["cost"].append(None)
        b += step
    return {"step": step, **cols}


def _costs(
    db: Database, cfg: Settings, now: int, device_id: int, t0: int, t1: int, step: int,
    extra: list[tuple], tariff: Tariff, buckets: dict[int, list[float]],
) -> dict[int, float]:
    """Cost per display bucket. Energy is priced in 15-minute blocks (or in the
    display bucket itself when that is finer) and added up into the bucket."""
    fine = min(step, 900)
    src = buckets if fine == step else _collect(db, cfg, now, device_id, t0, t1, fine, extra)
    out: dict[int, float] = {}
    for b, row in src.items():
        key = b // step * step
        out[key] = out.get(key, 0.0) + row[7] / 1000.0 * tariff.price_at(b)
    return out


def energy_cost(
    db: Database, cfg: Settings, now: int, device_id: int, t0: int, t1: int,
    tariff: Tariff, extra: list[tuple] | None = None,
) -> tuple[float, float]:
    """(Wh, cost) used in [t0, t1]; the cost is in the tariff's currency."""
    if t1 <= t0:
        return 0.0, 0.0
    buckets = _collect(db, cfg, now, device_id, t0, t1, 900, extra or [])
    wh = sum(r[7] for r in buckets.values())
    cost = sum(r[7] / 1000.0 * tariff.price_at(b) for b, r in buckets.items())
    return round(wh, 3), round(cost, 6)


def energy_wh(
    db: Database, cfg: Settings, now: int, device_id: int, t0: int, t1: int,
    extra: list[tuple] | None = None,
) -> float:
    """Energy used in [t0, t1], in Wh, summing the per-sample deltas."""
    if t1 <= t0:
        return 0.0
    buckets = _collect(db, cfg, now, device_id, t0, t1, 10**9, extra or [])
    return round(sum(r[7] for r in buckets.values()), 3)
