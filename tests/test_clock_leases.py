from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from app import leases
from app.clock import ClockGuard
from app.config import Settings
from app.db import Database
from app.monitor import Monitor
from app.shelly import ShellyClient

GOOD = 1_800_000_000


def test_clock_guard_states():
    db = Database(":memory:")
    cfg = Settings()
    t = {"now": 1000.0}
    g = ClockGuard(cfg, db, lambda: t["now"])
    assert g.status()["reason"] == "before_minimum" and not g.trusted

    t["now"] = GOOD
    assert g.trusted
    g.note_good(GOOD)
    assert db.get_meta_int("last_good_epoch") == GOOD

    t["now"] = GOOD - 30            # small step back is tolerated
    assert g.trusted
    t["now"] = GOOD - 5000          # a big jump back is not
    assert g.status()["reason"] == "went_backwards"

    g.confirm_now()                 # the user says this is right
    assert g.trusted and g.last_good == GOOD - 5000

    # persists across restarts
    assert ClockGuard(cfg, db, lambda: t["now"]).last_good == GOOD - 5000


async def test_samples_are_held_then_restamped_when_time_is_set(tmp_path):
    db = Database(tmp_path / "t.db")
    db.add_device(mac="aa:bb:cc:dd:ee:01", name="x", ip="127.0.0.1:1", added_ts=0)
    cfg = Settings(data_dir=tmp_path)
    wall = {"v": 1000.0}  # a dead-RTC clock
    mono = {"v": 50.0}
    guard = ClockGuard(cfg, db, lambda: wall["v"])
    async with httpx.AsyncClient() as http:
        m = Monitor(cfg, db, ShellyClient(http), guard, mono=lambda: mono["v"])
        from app.monitor import Pending
        from app.shelly import Sample
        for i in range(3):
            m.buffer.append(Pending(1, 50.0 + i * 10, Sample(output=True, power_w=10.0 + i, energy_wh=1.0), 0.1))
        mono["v"] = 90.0

        assert await m.flush() == 0                      # untrusted: nothing written
        assert db.query("SELECT COUNT(*) FROM readings")[0][0] == 0
        assert m.pending_for(1) == []                    # and not shown with fake times

        wall["v"] = GOOD                                  # the user sets the time at mono=90
        guard.confirm_now()
        assert await m.flush() == 3
        ts = [r[0] for r in db.query("SELECT ts FROM readings ORDER BY ts")]
        assert ts == [GOOD - 40, GOOD - 30, GOOD - 20]    # age preserved, wall time corrected
        # confirm_now() set the floor to the time the user entered; flush never lowers it
        assert db.get_meta_int("last_good_epoch") == GOOD
    db.close()


def test_leases_and_reservations(tmp_path):
    f = tmp_path / "dnsmasq.leases"
    f.write_text(
        "1900000000 AA:BB:CC:DD:EE:01 192.168.50.101 shellyplugmg3-aabbccddee01 01:aa:bb\n"
        "1900000500 aa:bb:cc:dd:ee:02 192.168.50.102 * *\n"
        "garbage line\n"
        "1800000100 aa:bb:cc:dd:ee:01 192.168.50.150 old *\n"
    )
    found = {l.mac: l for l in leases.read_leases(f)}
    assert set(found) == {"aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"}
    assert found["aa:bb:cc:dd:ee:01"].ip == "192.168.50.101"      # the newest lease wins
    assert found["aa:bb:cc:dd:ee:02"].hostname == ""
    assert leases.find_ip(f, "AA-BB-CC-DD-EE-02") == "192.168.50.102"
    assert leases.read_leases(tmp_path / "missing") == []

    hosts = tmp_path / "dhcp-hosts.d"
    path = leases.write_reservation(hosts, "AABBCCDDEE01", "192.168.50.101", "Desk Lamp!")
    assert path.read_text() == "aa:bb:cc:dd:ee:01,192.168.50.101,plug-desk-lamp-ee01\n"
    assert [p.name for p in hosts.iterdir()] == ["aa-bb-cc-dd-ee-01.conf"]
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".res-")]  # temp file gone
    leases.remove_reservation(hosts, "aa:bb:cc:dd:ee:01")
    assert list(hosts.iterdir()) == []
    leases.remove_reservation(hosts, "aa:bb:cc:dd:ee:01")  # idempotent
    with pytest.raises(ValueError):
        leases.write_reservation(hosts, "nope", "192.168.50.5", "x")


@pytest.mark.parametrize(
    "raw,expected",
    [("AABBCCDDEEFF", "aa:bb:cc:dd:ee:ff"), ("AA-BB-CC-DD-EE-FF", "aa:bb:cc:dd:ee:ff"),
     ("aa:bb:cc:dd:ee:ff", "aa:bb:cc:dd:ee:ff"), ("aabbcc", None), (None, None), ("zz:bb:cc:dd:ee:ff", None)],
)
def test_normalize_mac(raw, expected):
    assert leases.normalize_mac(raw) == expected


def test_plain_ip():
    assert leases.is_plain_ip("192.168.50.101")
    assert not leases.is_plain_ip("127.0.0.1")
    assert not leases.is_plain_ip("192.168.50.101:8080")
    assert not leases.is_plain_ip("shelly.local")
