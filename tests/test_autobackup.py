"""Automatic backups: the schedule, the plan and the whole path through a stand-in for the Pi's helper."""

from __future__ import annotations

import os
import subprocess
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app import usbbackup
from app.usbbackup import Plan, PlanError

from test_usb_script import LSBLK, SCRIPT

ROME = ZoneInfo("Europe/Rome")


def at(y, mo, d, h, mi=0, tz=ROME) -> float:
    return datetime(y, mo, d, h, mi, tzinfo=tz).timestamp()


def plan(**kw) -> Plan:
    base = dict(enabled=True, drive={"id": "1234-ABCD"}, every_hours=24, hour=0, timezone="Europe/Rome", keep=14)
    base.update(kw)
    return Plan.from_dict(base)


# ---- schedule ----------------------------------------------------------------------------

def test_defaults_are_daily_at_midnight():
    p = Plan.from_dict({})
    assert (p.every_hours, p.hour, p.keep, p.enabled) == (24, 0, 14, False)


def test_daily_at_midnight_local_time():
    p = plan()
    assert p.last_slot(at(2026, 10, 17, 3, 53)) == at(2026, 10, 17, 0)
    assert p.next_slot(at(2026, 10, 17, 3, 53)) == at(2026, 10, 18, 0)
    assert p.last_slot(at(2026, 10, 17, 0, 0)) == at(2026, 10, 17, 0)   # exactly on the slot counts


def test_several_times_a_day_start_at_the_chosen_hour():
    p = plan(every_hours=6, hour=2)                        # 02, 08, 14, 20
    assert p.next_slot(at(2026, 10, 17, 9)) == at(2026, 10, 17, 14)
    assert p.last_slot(at(2026, 10, 17, 1)) == at(2026, 10, 16, 20)
    assert p.next_slot(at(2026, 10, 17, 21)) == at(2026, 10, 18, 2)


def test_every_few_days_counts_from_the_anchor_day():
    p = plan(every_hours=72, hour=3, anchor="2026-10-10")  # 10th, 13th, 16th, 19th
    assert p.last_slot(at(2026, 10, 17, 12)) == at(2026, 10, 16, 3)
    assert p.next_slot(at(2026, 10, 17, 12)) == at(2026, 10, 19, 3)
    week = plan(every_hours=168, hour=0, anchor="2026-10-12")
    assert week.next_slot(at(2026, 10, 17, 12)) == at(2026, 10, 19, 0)


def test_follows_daylight_saving():
    p = plan(hour=0)
    # Rome moves to winter time on 2026-10-25: the slot stays at local midnight
    assert p.next_slot(at(2026, 10, 24, 12)) == at(2026, 10, 25, 0)
    assert p.next_slot(at(2026, 10, 25, 12)) == at(2026, 10, 26, 0)
    assert at(2026, 10, 26, 0) - at(2026, 10, 25, 0) == 25 * 3600


@pytest.mark.parametrize("bad", [
    {"every_hours": 5}, {"every_hours": 0}, {"hour": 24}, {"hour": -1}, {"keep": 0}, {"keep": 99999},
    {"timezone": "Mars/Base"}, {"every_hours": "x"},
])
def test_plan_validation(bad):
    with pytest.raises(PlanError):
        Plan.from_dict(bad)


# ---- through the app, with a stand-in for the Pi's helper -------------------------------------

class FakePi:
    """Does what shelly-usb.path does: when usb.req appears, run usb.sh on it."""

    def __init__(self, tmp: Path, bridge: Path):
        self.drive = tmp / "stick"
        self.drive.mkdir()
        (tmp / "lsblk.txt").write_text(LSBLK)
        fake = tmp / "lsblk"
        fake.write_text(f"#!/bin/sh\ncat {tmp / 'lsblk.txt'}\n")
        fake.chmod(0o755)
        self.env = dict(os.environ, SHELLY_BRIDGE=str(bridge), SHELLY_LSBLK=str(fake), SHELLY_USB_FAKE=str(self.drive))
        self.bridge, self.stop, self.runs = bridge, threading.Event(), 0
        self.thread = threading.Thread(target=self.watch, daemon=True)
        self.thread.start()

    def watch(self):
        while not self.stop.is_set():
            if (self.bridge / "usb.req").exists():
                subprocess.run(["sh", str(SCRIPT)], env=self.env, capture_output=True, timeout=60)
                self.runs += 1
            time.sleep(0.05)

    def backups(self):
        folder = self.drive / "shelly-pi-backups"
        return sorted(p.name for p in folder.glob("*.zip")) if folder.is_dir() else []


@pytest.fixture
def pi(settings):
    p = FakePi(settings.data_dir.parent, settings.data_dir / "bridge")
    (settings.data_dir / "bridge").mkdir(parents=True, exist_ok=True)
    yield p
    p.stop.set()


def body(**kw):
    d = dict(enabled=True, drive_id="1234-ABCD", every_hours=24, hour=0, timezone="Europe/Rome", keep=14)
    d.update(kw)
    return d


def test_pick_drive_run_now_and_status(run_app, pi):
    h = run_app()
    st = h.client.get("/api/autobackup").json()
    assert st["plan"]["enabled"] is False and st["plan"]["every_hours"] == 24 and st["plan"]["hour"] == 0
    assert st["plan"]["keep"] == 14 and st["last"] is None and st["next"] is None

    r = h.client.get("/api/autobackup/drives")
    assert r.status_code == 200
    drives = {d["id"]: d for d in r.json()["drives"]}
    assert drives["1234-ABCD"]["supported"] and not drives["zzzz"]["supported"]

    # a drive that wasn't listed, or can't be written, is refused; so is switching on without one
    assert h.client.put("/api/autobackup", json=body(drive_id="nope")).status_code == 422
    assert "formatted" in h.client.put("/api/autobackup", json=body(drive_id="zzzz")).json()["detail"]
    assert h.client.put("/api/autobackup", json=body(drive_id=None)).status_code == 422
    assert h.client.put("/api/autobackup", json=body(every_hours=5)).status_code == 422

    r = h.client.put("/api/autobackup", json=body(enabled=False))      # choose the drive without switching on
    assert r.status_code == 200 and r.json()["plan"]["drive"]["label"] == 'My "Stick" $x'
    assert r.json()["next"] is None

    # "Back up now" works whether or not the schedule is on
    r = h.client.post("/api/autobackup/run")
    assert r.status_code == 200, r.text
    res = r.json()["result"]
    assert res["ok"] is True and res["file"].startswith("shelly-pi_") and res["file"].endswith(".zip")
    assert pi.backups() == [res["file"]]
    # it is a real, complete backup, and nothing was left behind in the bridge folder
    z = zipfile.ZipFile(pi.drive / "shelly-pi-backups" / res["file"])
    assert {"manifest.json", "shelly-pi.db"} <= set(z.namelist())
    assert not list((h.app.state.cfg.bridge_dir / "outgoing").iterdir())
    assert h.client.get("/api/autobackup").json()["last"]["file"] == res["file"]

    # switching on shows when the next one is due
    r = h.client.put("/api/autobackup", json=body())
    nxt = r.json()["next"]
    assert nxt and 0 < nxt - time.time() <= 25 * 3600
    local = datetime.fromtimestamp(nxt, ROME)
    assert (local.hour, local.minute) == (0, 0)


def test_helper_missing_is_explained(run_app, monkeypatch):
    monkeypatch.setattr(usbbackup, "LIST_TIMEOUT", 1.0)
    h = run_app()
    r = h.client.get("/api/autobackup/drives")
    assert r.status_code == 504 and "setup-host.sh" in r.json()["detail"]
    assert not (h.app.state.cfg.bridge_dir / "usb.req").exists()


def test_schedule_runs_once_per_slot_and_keeps_the_newest(run_app, pi, monkeypatch):
    monkeypatch.setattr(usbbackup, "LOOP_EVERY", 0.2)
    h = run_app()
    h.client.get("/api/autobackup/drives")
    h.client.put("/api/autobackup", json=body(keep=2, timezone="UTC"))
    time.sleep(1.2)
    assert pi.backups() == []                       # switching on doesn't back up for a slot in the past

    def last_time():
        last = h.client.get("/api/autobackup").json()["last"]
        return last["time"] if last else 0

    seen = 0
    for day in (1, 2, 3):                           # three midnights go by
        h.clock_offset["v"] = day * 86400 + 600
        seen = h.wait_for(lambda s=seen: last_time() > s and last_time())
        time.sleep(0.8)                             # and it doesn't run again for the same slot
        assert last_time() == seen
    names = pi.backups()
    assert len(names) == 2 and names == sorted(names)
    assert h.client.get("/api/autobackup").json()["last"]["ok"] is True


def test_failure_is_reported_and_retried(run_app, pi, monkeypatch):
    monkeypatch.setattr(usbbackup, "LOOP_EVERY", 0.2)
    monkeypatch.setattr(usbbackup, "RETRY_AFTER", 1)
    h = run_app()
    h.client.get("/api/autobackup/drives")
    h.client.put("/api/autobackup", json=body(timezone="UTC"))
    lsblk = pi.drive.parent / "lsblk.txt"
    lsblk.write_text("")                                                 # the stick is unplugged
    h.clock_offset["v"] = 86400 + 600
    last = h.wait_for(lambda: h.client.get("/api/autobackup").json()["last"])
    assert last["ok"] is False and "isn't connected" in last["message"]
    assert pi.backups() == []
    lsblk.write_text(LSBLK)                                       # plugged back in
    h.wait_for(lambda: pi.backups() and h.client.get("/api/autobackup").json()["last"]["ok"], timeout=15)


def test_nothing_runs_while_the_clock_is_untrusted(run_app, pi, monkeypatch):
    monkeypatch.setattr(usbbackup, "LOOP_EVERY", 0.2)
    h = run_app()
    h.client.get("/api/autobackup/drives")
    h.client.put("/api/autobackup", json=body(timezone="UTC"))
    h.clock_offset["v"] = 1000.0 - time.time()
    time.sleep(1.0)
    st = h.client.get("/api/autobackup").json()
    assert pi.backups() == [] and st["clock_trusted"] is False and st["next"] is None
    r = h.client.post("/api/autobackup/run")
    assert r.status_code == 200 and r.json()["result"]["ok"] is False and "clock" in r.json()["result"]["message"]


def test_plan_is_part_of_a_backup_but_the_log_is_not(run_app, pi):
    h = run_app()
    h.client.get("/api/autobackup/drives")
    h.client.put("/api/autobackup", json=body(every_hours=12, hour=5, keep=7))
    h.client.post("/api/autobackup/run")
    z = h.client.get("/api/backup")
    raw = h.app.state.cfg.data_dir / "dl.zip"
    raw.write_bytes(z.content)
    assert "autobackup-state.json" not in zipfile.ZipFile(raw).namelist()
    h.client.put("/api/autobackup", json=body(enabled=False, every_hours=24, hour=0, keep=14))
    assert h.client.post("/api/restore", content=z.content, headers={"content-type": "application/zip"}).status_code == 200
    p = h.client.get("/api/autobackup").json()["plan"]
    assert (p["enabled"], p["every_hours"], p["hour"], p["keep"]) == (True, 12, 5, 7)
