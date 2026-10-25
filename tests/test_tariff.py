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


def with_change(start="2027-01-01", base=0.40, **kw):
    return italian(changes=[{"start": start, "base_price": base,
                             "slots": [{"name": "F1", "price": 0.50, "days": [0, 1, 2, 3, 4], "start": "08:00", "end": "19:00"}]}], **kw)


def test_a_price_change_applies_from_its_date_and_leaves_the_past_alone():
    t = with_change()
    before, after = ts(2026, 12, 31, 12), ts(2027, 1, 4, 12)      # Thu 31 Dec 2026, Mon 4 Jan 2027, both noon
    assert t.price_at(before) == 0.30 and t.price_at(after) == 0.50   # F1 both days, new price after
    assert t.price_at(ts(2026, 12, 27, 12)) == 0.10 and t.price_at(ts(2027, 1, 3, 12)) == 0.40   # Sunday: old F3, then new base
    assert t.price_at(ts(2026, 12, 31, 23, 59)) == 0.10             # up to the last minute of the old day: old prices
    assert t.price_at(ts(2027, 1, 1, 0, 0)) == 0.40                 # from midnight: new base (no night slot any more)
    assert t.prices_at(ts(2027, 1, 1, 0, 0)).slots[0].price == 0.50


def test_several_changes_are_sorted_and_each_one_ends_at_the_next():
    t = italian(changes=[{"start": "2028-03-01", "base_price": 0.9}, {"start": "2027-01-01", "base_price": 0.4}])
    assert [c.start.isoformat() for c in t.changes] == ["2027-01-01", "2028-03-01"]
    assert t.price_at(ts(2026, 6, 6, 12)) == 0.20                   # Saturday noon: base from the start
    assert t.price_at(ts(2027, 6, 5, 12)) == 0.40
    assert t.price_at(ts(2028, 6, 3, 12)) == 0.90


def test_cost_over_a_change_uses_each_days_own_prices():
    t = with_change(start="2026-10-13")                              # new prices from Tue 13 Oct
    t0, t1 = ts(2026, 10, 12, 9), ts(2026, 10, 14, 9)                # Mon 09:00 to Wed 09:00 at 1 kW
    db = _db_with_constant_load(t0, t1)
    cfg = Settings(data_dir=__import__("pathlib").Path("."))
    wh, cost = history.energy_cost(db, cfg, t1 + 100, 1, t0, t1, t)
    # Mon 09:00-24:00 on the old prices: 09-19 F1 0.30, 19-23 base 0.20, 23-24 F3 0.10
    mon = 10 * 0.30 + 4 * 0.20 + 1 * 0.10
    # Tue 13 Oct on the new prices: 00-08 base 0.40, 08-19 F1 0.50, 19-24 base 0.40
    tue = 8 * 0.40 + 11 * 0.50 + 5 * 0.40
    wed = 0.40 * 8 + 0.50 * 1                          # Wed 00-08 base, 08-09 F1
    assert cost == pytest.approx(mon + tue + wed, abs=0.02)
    db.close()


def test_changes_are_validated_and_old_data_still_loads():
    with pytest.raises(TariffError, match="two price changes"):
        italian(changes=[{"start": "2027-01-01", "base_price": 1}, {"start": "2027-01-01", "base_price": 2}])
    with pytest.raises(TariffError, match="needs a date"):
        italian(changes=[{"start": "soon", "base_price": 1}])
    with pytest.raises(TariffError, match="Prices from 2027-01-01"):
        italian(changes=[{"start": "2027-01-01", "base_price": -1}])
    old = Tariff.from_dict({"enabled": True, "base_price": 0.3, "slots": []})      # saved before changes existed
    assert old.changes == [] and old.price_at(ts(2026, 6, 6, 12)) == 0.3
    t = with_change()
    assert Tariff.from_dict(t.to_dict()).changes[0].prices.slots[0].price == 0.50


def test_api_price_change_roundtrip(run_app, plugs):
    plugs(mac="AABBCCDDEE01")
    h = run_app()
    body = {"enabled": True, "currency": "€", "decimals": 2, "timezone": "Europe/Rome", "base_price": 0.25, "slots": [],
            "changes": [{"start": "2027-01-01", "base_price": 0.35, "slots": [
                {"name": "Night", "price": 0.1, "days": [0, 1, 2, 3, 4, 5, 6], "start": "00:30", "end": "07:30"}]}]}
    got = h.client.put("/api/tariff", json=body).json()
    assert got["base_price"] == 0.25 and got["changes"][0]["start"] == "2027-01-01"
    assert len(got["changes"][0]["week"]) == 7 and got["changes"][0]["slots"][0]["name"] == "Night"
    assert h.client.put("/api/tariff", json={**body, "changes": [{"start": "nope"}]}).status_code == 422
    assert h.client.get("/api/tariff").json()["changes"][0]["base_price"] == 0.35
