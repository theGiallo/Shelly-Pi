"""Rollups, retention and history queries on synthetic data."""

from __future__ import annotations

import pytest

from app import history, rollup
from app.config import Settings
from app.db import Database

DAY = 86400


@pytest.fixture
def db():
    d = Database(":memory:")
    for dev in (1, 2):
        d.add_device(mac=f"aa:bb:cc:dd:ee:0{dev}", name=f"plug{dev}", ip=f"10.0.0.{dev}", added_ts=0)
    yield d
    d.close()


def make_rows(dev, t0, t1, step=10, power=100.0, e_per_sample=None):
    """Constant-power samples; e_delta = power*step/3600 Wh unless overridden."""
    e = power * step / 3600 if e_per_sample is None else e_per_sample
    return [(dev, t, power, 230.0, power / 230, 30.0, None, e, 1) for t in range(t0, t1, step)]


def sum_energy(db, table, col="e_delta_wh"):
    return db.query(f"SELECT COALESCE(SUM({col}),0) FROM {table}")[0][0]


def test_rollup_1m_and_15m_preserve_energy_and_counts(db):
    t0 = 1_800_000_000 // 900 * 900          # aligned start
    t1 = t0 + 3 * 3600
    db.insert_readings(make_rows(1, t0, t1, power=100.0) + make_rows(2, t0, t1, power=50.0))
    now = t1 + 3600
    stats = rollup.run(db, now=now, lag=420)

    assert stats["r1m_rows"] > 0 and stats["r15m_rows"] > 0
    # every sample before the watermark is counted exactly once in the 1-minute table
    done = db.get_meta_int("r1m_done")
    assert done == (now - 420) // 60 * 60
    raw_n = db.query("SELECT COUNT(*) FROM readings WHERE ts < ?", (done,))[0][0]
    assert sum_energy(db, "rollup_1m", "n") == raw_n
    raw_e = db.query("SELECT SUM(e_delta_wh) FROM readings WHERE ts < ?", (done,))[0][0]
    assert sum_energy(db, "rollup_1m", "e_wh") == pytest.approx(raw_e)
    # 15 minute table adds up the same
    done15 = db.get_meta_int("r15m_done")
    e15 = db.query("SELECT SUM(e_wh) FROM rollup_15m")[0][0]
    e1 = db.query("SELECT SUM(e_wh) FROM rollup_1m WHERE ts < ?", (done15,))[0][0]
    assert e15 == pytest.approx(e1)
    # averages survive: dev 1 averages 100 W
    r = db.query("SELECT SUM(p_sum)/SUM(n), MAX(p_max) FROM rollup_15m WHERE device_id=1")[0]
    assert r[0] == pytest.approx(100.0) and r[1] == 100.0


def test_rollup_is_idempotent(db):
    t0 = 1_800_000_000 // 900 * 900
    db.insert_readings(make_rows(1, t0, t0 + 7200))
    rollup.run(db, now=t0 + 7200 + 3600, lag=420)
    before = db.query("SELECT * FROM rollup_1m ORDER BY ts")
    rollup.run(db, now=t0 + 7200 + 3600, lag=420)
    assert db.query("SELECT * FROM rollup_1m ORDER BY ts") == before


def test_late_rows_rewind_watermark_and_get_rolled_up(db):
    t0 = 1_800_000_000 // 900 * 900
    db.insert_readings(make_rows(1, t0 + 3600, t0 + 7200))
    now = t0 + 7200 + 3600
    rollup.run(db, now=now, lag=420)
    wm = db.get_meta_int("r1m_done")
    # a held batch arrives with timestamps older than the watermark
    db.insert_readings(make_rows(1, t0 + 3000, t0 + 3600))
    assert db.get_meta_int("r1m_done") < wm
    rollup.run(db, now=now, lag=420)
    assert sum_energy(db, "rollup_1m", "e_wh") == pytest.approx(sum_energy(db, "readings"), rel=1e-9)


def test_retention_never_deletes_unrolled_data(db):
    cfg = Settings()
    now = 1_800_000_000
    old = now - 10 * DAY
    db.insert_readings(make_rows(1, old, old + 3600, step=30))
    rollup.run(db, now=now, lag=420, raw_days=7, r1m_days=90)
    assert db.query("SELECT COUNT(*) FROM readings WHERE ts < ?", (now - 7 * DAY,))[0][0] == 0
    assert db.query("SELECT COUNT(*) FROM rollup_1m")[0][0] > 0
    assert db.query("SELECT COUNT(*) FROM rollup_15m")[0][0] > 0
    # energy still fully accounted for after raw rows are gone
    assert sum_energy(db, "rollup_15m", "e_wh") == pytest.approx(100.0 * 30 / 3600 * 120, rel=1e-6)

    # a clock that is far ahead must not delete rows that were never rolled up
    db2 = Database(":memory:")
    db2.insert_readings(make_rows(1, now, now + 600))
    rollup.run(db2, now=now + 400 * DAY, lag=420)  # rolls up first, then prunes
    assert db2.query("SELECT COUNT(*) FROM readings")[0][0] == 0
    # the 1-minute rows are pruned too (older than 90 days), but only after reaching 15m
    assert db2.query("SELECT COUNT(*) FROM rollup_1m")[0][0] == 0
    assert db2.query("SELECT SUM(e_wh) FROM rollup_15m")[0][0] == pytest.approx(100.0 * 10 / 3600 * 60)
    db2.close()
    assert cfg  # silence unused


def test_history_uses_right_source_and_energy_matches(db):
    cfg = Settings()
    now = 1_800_000_000
    start = now - 120 * DAY
    # 120 days at one sample per 60 s, 100 W (keeps the test quick)
    rows = make_rows(1, start, now - 3600, step=60, power=100.0)
    db.insert_readings(rows)
    rollup.run(db, now=now, lag=420, raw_days=cfg.raw_retention_days, r1m_days=cfg.r1m_retention_days)
    total = 100.0 * 60 / 3600 * len(rows)

    # the whole range crosses all three tiers; energy must equal what we inserted
    e = history.energy_wh(db, cfg, now, 1, start, now)
    assert e == pytest.approx(total, rel=1e-6)

    # a recent 6 h chart comes from raw rows and has the expected shape
    h = history.history(db, cfg, now, 1, now - 6 * 3600, now, points=120)
    assert len(h["t"]) == len(h["p_avg"]) and h["step"] >= 10
    vals = [v for v in h["p_avg"] if v is not None]
    assert vals and all(v == pytest.approx(100.0) for v in vals)
    # the last hour has no data (we stopped inserting 1 h before now): gaps are None
    assert h["p_avg"][-1] is None

    # a 100-day chart is built from the coarser tables and stays small
    h2 = history.history(db, cfg, now, 1, now - 100 * DAY, now, points=300)
    assert len(h2["t"]) <= 320
    assert [v for v in h2["p_avg"] if v is not None]


def test_history_merges_unflushed_samples(db):
    cfg = Settings()
    now = 1_800_000_000
    db.insert_readings(make_rows(1, now - 600, now - 300, step=10, power=100.0))
    extra = [(now - 100 + i * 10, 200.0, 230.0, 0.9, 30.0, True, 0.5) for i in range(5)]
    h = history.history(db, cfg, now, 1, now - 600, now, points=60, extra=extra)
    assert any(v == pytest.approx(200.0) for v in h["p_avg"] if v is not None)
    e = history.energy_wh(db, cfg, now, 1, now - 600, now, extra)
    flushed = 100.0 * 10 / 3600 * 30
    assert e == pytest.approx(flushed + 2.5, abs=1e-3)
