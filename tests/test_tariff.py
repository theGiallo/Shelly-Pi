"""Electricity price: slots, validation, cost in history and in the API."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app import history, tariff as tariff_mod
from app.config import Settings
from app.db import Database
from app.tariff import Tariff, TariffError

ROME = ZoneInfo("Europe/Rome")


def ts(y, m, d, hh, mm=0):
    return int(datetime(y, m, d, hh, mm, tzinfo=ROME).timestamp())


def italian(**kw):
    """Weekday day 08-19 expensive, evenings and Saturday mid, nights/Sundays cheap."""
    base = {
        "enabled": True, "timezone": "Europe/Rome", "base_price": 0.20,
        "slots": [
            {"name": "F1", "price": 0.30, "days": [0, 1, 2, 3, 4], "start": "08:00", "end": "19:00"},
            {"name": "F3", "price": 0.10, "days": [0, 1, 2, 3, 4], "start": "23:00", "end": "07:00"},
            {"name": "F3", "price": 0.10, "days": [6], "start": "00:00", "end": "00:00"},
        ],
    }
    base.update(kw)
    return Tariff.from_dict(base)


def test_slots_wrap_over_midnight_and_whole_day():
    t = italian()
    mon = ts(2026, 10, 12, 0)  # a Monday
    assert t.price_at(mon + 9 * 3600) == 0.30                       # Mon 09:00 F1
    assert t.price_at(mon + 7 * 3600 + 15 * 60) == 0.20             # Mon 07:15 base
    assert t.price_at(mon + 23 * 3600 + 900) == 0.10                # Mon 23:15 F3
    assert t.price_at(mon + 86400 + 3600) == 0.10                   # Tue 01:00: night of Monday
    assert t.price_at(mon - 3600) == 0.10                           # Sun 23:00: Sunday is whole-day F3 ...
    assert t.price_at(mon + 3 * 3600) == 0.20                       # Mon 03:00: the Sunday slot has no night part, so base
    sun = ts(2026, 10, 18, 12)
    assert t.price_at(sun) == 0.10                                  # ... Sun 12:00
    assert t.price_at(ts(2026, 10, 17, 12)) == 0.20                 # Saturday: base
    assert t.price_at(ts(2026, 10, 16, 23, 30)) == 0.10             # Fri 23:30 (night of Fri)
    assert t.price_at(ts(2026, 10, 17, 3)) == 0.10                  # Sat 03:00 (still Friday's night)


def test_first_matching_slot_wins_and_week_grid():
    t = Tariff.from_dict({"enabled": True, "base_price": 1, "slots": [
        {"name": "a", "price": 2, "days": [0], "start": "10:00", "end": "12:00"},
        {"name": "b", "price": 3, "days": [0], "start": "11:00", "end": "13:00"},
    ]})
    w = t.week()
    assert len(w) == 7 and len(w[0]) == 96
    assert w[0][40] == 0 and w[0][47] == 0 and w[0][48] == 1 and w[0][52] == -1 and w[1][44] == -1


def test_dst_uses_local_time():
    t = italian()
    # 2026-10-25 is the day the clocks go back; Sunday, so F3 all day. Monday 08:00 local is F1 either side.
    assert t.price_at(ts(2026, 10, 26, 8)) == 0.30
    assert t.price_at(ts(2026, 3, 30, 8)) == 0.30  # after the spring change


@pytest.mark.parametrize("bad", [
    {"timezone": "Mars/Base"},
    {"currency": ""},
    {"currency": "x" * 9},
    {"base_price": -1},
    {"base_price": float("nan")},
    {"decimals": 9},
    {"slots": [{"name": "x", "price": 1, "days": [], "start": "08:00", "end": "09:00"}]},
    {"slots": [{"name": "x", "price": 1, "days": [7], "start": "08:00", "end": "09:00"}]},
    {"slots": [{"name": "x", "price": 1, "days": [1], "start": "08:10", "end": "09:00"}]},
    {"slots": [{"name": "x", "price": 1, "days": [1], "start": "8am", "end": "09:00"}]},
    {"slots": [{"name": "x", "price": "free", "days": [1], "start": "08:00", "end": "09:00"}]},
])
def test_validation_rejects_bad_input(bad):
    with pytest.raises(TariffError):
        Tariff.from_dict({"enabled": True, **bad})


def test_round_trip_through_the_database():
    db = Database(":memory:")
    assert tariff_mod.load(db).enabled is False
    tariff_mod.save(db, italian(currency="CHF", currency_after=False, decimals=3))
    t = tariff_mod.load(db)
    assert (t.currency, t.currency_after, t.decimals, len(t.slots)) == ("CHF", False, 3, 3)
    assert t.slots[1].to_dict()["end"] == "07:00"
    db.set_meta("tariff", "{broken")
    assert tariff_mod.load(db).enabled is False
    db.close()


def _db_with_constant_load(t0, t1, power=1000.0):
    db = Database(":memory:")
    db.add_device(mac="aa:bb:cc:dd:ee:01", name="p", ip="10.0.0.1", added_ts=0)
    e = power * 10 / 3600
    db.insert_readings([(1, t, power, 230.0, power / 230, 30.0, None, e, 1) for t in range(t0, t1, 10)])
    return db


def test_cost_matches_hand_calculation_across_a_slot_boundary():
    t = italian()
    t0, t1 = ts(2026, 10, 12, 6), ts(2026, 10, 12, 10)  # Mon 06-10, 1 kW: 06-08 base (Sunday has no night part), 08-10 F1
    db = _db_with_constant_load(t0, t1)
    cfg = Settings(data_dir=db and __import__("pathlib").Path("."))
    now = t1 + 100
    wh, cost = history.energy_cost(db, cfg, now, 1, t0, t1, t)
    assert wh == pytest.approx(4000, abs=1)
    assert cost == pytest.approx(0.20 * 2 + 0.30 * 2, abs=0.01)
    h = history.history(db, cfg, now, 1, t0, t1, points=48, tariff=t)
    assert sum(c for c in h["cost"] if c is not None) == pytest.approx(cost, abs=0.01)
    assert "cost" not in history.history(db, cfg, now, 1, t0, t1, points=48)
    off = Tariff.from_dict({"enabled": False, "base_price": 1})
    assert "cost" not in history.history(db, cfg, now, 1, t0, t1, points=48, tariff=off)
    db.close()


def test_cost_is_the_same_whatever_the_zoom():
    t = italian()
    t0, t1 = ts(2026, 10, 12, 0), ts(2026, 10, 14, 0)
    db = _db_with_constant_load(t0, t1, power=500.0)
    cfg = Settings(data_dir=__import__("pathlib").Path("."))
    now = t1 + 100
    _, total = history.energy_cost(db, cfg, now, 1, t0, t1, t)
    for points in (20, 100, 400, 1500):
        h = history.history(db, cfg, now, 1, t0, t1, points=points, tariff=t)
        assert sum(c for c in h["cost"] if c is not None) == pytest.approx(total, rel=0.01), points
    db.close()


def test_api_tariff_roundtrip_and_cost_in_state(run_app, plugs):
    plug, srv = plugs(mac="AABBCCDDEE01")
    h = run_app()
    assert h.client.get("/api/tariff").json()["enabled"] is False
    assert "cost_since" in h.client.get("/api/state").json()["devices"] or True
    body = {"enabled": True, "currency": "£", "currency_after": False, "decimals": 2,
            "timezone": "Europe/London", "base_price": 0.25,
            "slots": [{"name": "Night", "price": 0.1, "days": [0, 1, 2, 3, 4, 5, 6], "start": "00:30", "end": "07:30"}]}
    r = h.client.put("/api/tariff", json=body)
    assert r.status_code == 200
    got = r.json()
    assert got["currency"] == "£" and got["slots"][0]["end"] == "07:30" and len(got["week"]) == 7
    assert h.client.get("/api/state").json()["tariff"]["base_price"] == 0.25
    bad = h.client.put("/api/tariff", json={**body, "timezone": "Nowhere/City"})
    assert bad.status_code == 422 and "time zone" in bad.json()["detail"]
    assert h.client.put("/api/tariff", json={**body, "slots": [{"price": 1, "days": [1], "start": "08:07", "end": "09:00"}]}).status_code == 422
    # the invalid attempts changed nothing
    assert h.client.get("/api/tariff").json()["currency"] == "£"
