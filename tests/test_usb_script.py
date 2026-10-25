"""deploy/libexec/usb.sh, run for real with a pretend lsblk and a plain folder as the "drive"."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "libexec" / "usb.sh"

LSBLK = """\
KNAME="mmcblk0" TYPE="disk" SIZE="31914983424" FSTYPE="" LABEL="" UUID="" MODEL="" VENDOR="" TRAN="" HOTPLUG="0" PKNAME="" MOUNTPOINT=""
KNAME="mmcblk0p2" TYPE="part" SIZE="1000000000" FSTYPE="ext4" LABEL="root" UUID="aaaa-root" MODEL="" VENDOR="" TRAN="" HOTPLUG="0" PKNAME="mmcblk0" MOUNTPOINT="/sysroot"
KNAME="sda" TYPE="disk" SIZE="64023257088" FSTYPE="" LABEL="" UUID="" MODEL="Ultra Fit       " VENDOR="SanDisk " TRAN="usb" HOTPLUG="1" PKNAME="" MOUNTPOINT=""
KNAME="sda1" TYPE="part" SIZE="64000000000" FSTYPE="exfat" LABEL="My \\x22Stick\\x22 $x" UUID="1234-ABCD" MODEL="" VENDOR="" TRAN="" HOTPLUG="1" PKNAME="sda" MOUNTPOINT=""
KNAME="sdb" TYPE="disk" SIZE="500000000000" FSTYPE="" LABEL="" UUID="" MODEL="Elements" VENDOR="WD" TRAN="usb" HOTPLUG="1" PKNAME="" MOUNTPOINT=""
KNAME="sdb1" TYPE="part" SIZE="500000000000" FSTYPE="LVM2_member" LABEL="" UUID="zzzz" MODEL="" VENDOR="" TRAN="" HOTPLUG="1" PKNAME="sdb" MOUNTPOINT=""
KNAME="nvme0n1p1" TYPE="part" SIZE="1" FSTYPE="ext4" LABEL="internal" UUID="nvme-1" MODEL="" VENDOR="" TRAN="nvme" HOTPLUG="0" PKNAME="nvme0n1" MOUNTPOINT=""
"""

NAME = "shelly-pi_2026-10-17_00-00-00.zip"


@pytest.fixture
def helper(tmp_path):
    bridge, drive = tmp_path / "bridge", tmp_path / "drive"
    (bridge / "outgoing").mkdir(parents=True)
    drive.mkdir()
    (tmp_path / "lsblk.txt").write_text(LSBLK)
    fake = tmp_path / "lsblk"
    fake.write_text(f"#!/bin/sh\ncat {tmp_path / 'lsblk.txt'}\n")
    fake.chmod(0o755)
    env = dict(os.environ, SHELLY_BRIDGE=str(bridge), SHELLY_LSBLK=str(fake), SHELLY_USB_FAKE=str(drive))

    def ask(line: str) -> dict:
        (bridge / "usb.req").write_text(line + "\n")
        subprocess.run(["sh", str(SCRIPT)], env=env, check=True, capture_output=True, timeout=30)
        assert not (bridge / "usb.req").exists()      # always consumed
        return json.loads((bridge / "usb.res.json").read_text())

    ask.bridge, ask.drive = bridge, drive
    return ask


def test_lists_only_usb_partitions(helper):
    r = helper("q1 list")
    assert r["id"] == "q1" and r["ok"] is True
    by_id = {d["id"]: d for d in r["drives"]}
    assert set(by_id) == {"1234-ABCD", "zzzz"}          # not the SD card, not the NVMe, not the root fs
    stick = by_id["1234-ABCD"]
    assert stick["label"] == 'My "Stick" $x' and stick["name"] == "SanDisk Ultra Fit"
    assert stick["fstype"] == "exfat" and stick["size"] == 64000000000 and stick["supported"] is True
    assert by_id["zzzz"]["supported"] is False


def test_copy_prunes_only_backups(helper):
    folder = helper.drive / "shelly-pi-backups"
    folder.mkdir()
    for d in ("01", "02", "03"):
        (folder / f"shelly-pi_2026-01-{d}_00-00-00.zip").write_text("old")
    (folder / "notes.txt").write_text("mine")
    (folder / "shelly-pi_holiday.zip").write_text("mine too")
    (helper.drive / "photo.jpg").write_text("not touched")
    (helper.bridge / "outgoing" / NAME).write_bytes(b"z" * 5000)

    r = helper(f"c1 copy 1234-ABCD {NAME} 2")
    assert r["ok"] is True and r["file"] == NAME and r["bytes"] == 5000 and r["removed"] == 2
    left = sorted(p.name for p in folder.iterdir())
    assert left == ["notes.txt", "shelly-pi_2026-01-03_00-00-00.zip", NAME, "shelly-pi_holiday.zip"]
    assert (folder / NAME).read_bytes() == b"z" * 5000
    assert (helper.drive / "photo.jpg").read_text() == "not touched"
    assert not any(p.name.startswith(".") for p in folder.iterdir())   # no half-written leftovers


@pytest.mark.parametrize("line, why", [
    ("c2 copy zzzz {n} 5", "can't write to safely"),                 # LVM member
    ("c3 copy aaaa-root {n} 5", "isn't connected"),             # the Pi's own disk is never offered
    ("c4 copy nvme-1 {n} 5", "isn't connected"),
    ("c5 copy 1234-ABCD ../../etc/passwd 5", "backup file name"),
    ("c6 copy 1234-ABCD shelly-pi_x.zip 5", "backup file name"),
    ("c7 copy 1234-ABCD {n} 0", "Keep between"),
    ("c8 copy 1234-ABCD shelly-pi_2026-10-18_00-00-00.zip 5", "isn't there"),
    ("c9 format 1234-ABCD", "Unknown"),
])
def test_refuses(helper, line, why):
    (helper.bridge / "outgoing" / NAME).write_bytes(b"z")
    r = helper(line.format(n=NAME))
    assert r["ok"] is False and why in r["message"]
    assert not list(helper.drive.iterdir())               # nothing was written


def test_request_without_an_id_is_ignored(helper):
    (helper.bridge / "usb.req").write_text("   \n")
    out = subprocess.run(["sh", str(SCRIPT)], env=dict(os.environ, SHELLY_BRIDGE=str(helper.bridge)), capture_output=True)
    assert out.returncode == 1 and not (helper.bridge / "usb.res.json").exists()


def test_names_with_the_app_version_are_backups_too(helper):
    folder = helper.drive / "shelly-pi-backups"
    folder.mkdir()
    (folder / "shelly-pi_2026-01-01_00-00-00.zip").write_text("old name")
    (folder / "shelly-pi_2026-01-02_00-00-00_v0.1.4.zip").write_text("versioned")
    (folder / "shelly-pi_2026-01-03_00-00-00_vX.zip").write_text("not ours")
    new = "shelly-pi_2026-10-17_00-00-00_v0.1.5.zip"
    (helper.bridge / "outgoing" / new).write_bytes(b"z" * 100)
    r = helper(f"c1 copy 1234-ABCD {new} 2")
    assert r["ok"] is True and r["removed"] == 1
    assert sorted(p.name for p in folder.iterdir()) == [
        "shelly-pi_2026-01-02_00-00-00_v0.1.4.zip", "shelly-pi_2026-01-03_00-00-00_vX.zip", new]
    bad = helper("c2 copy 1234-ABCD shelly-pi_2026-10-17_00-00-00_v1/../x.zip 2")
    assert bad["ok"] is False


def _name(dt):
    return f"shelly-pi_{dt:%Y-%m-%d_%H-%M-%S}.zip"


def test_thinned_keeps_the_newest_and_spreads_the_rest(helper):
    from datetime import datetime, timedelta
    folder = helper.drive / "shelly-pi-backups"
    folder.mkdir()
    end = datetime(2026, 10, 17, 12, 0, 0)
    for h in range(1, 24 * 40):                      # hourly for 40 days
        (folder / _name(end - timedelta(hours=h))).write_text("old")
    (folder / "notes.txt").write_text("mine")
    (helper.bridge / "outgoing" / _name(end)).write_bytes(b"z" * 100)
    r = helper(f"t1 copy 1234-ABCD {_name(end)} thin")
    assert r["ok"] is True and r["removed"] > 900
    kept = sorted(p.name for p in folder.iterdir() if p.name.startswith("shelly-pi_"))
    assert _name(end) in kept and (folder / "notes.txt").exists()
    assert _name(end - timedelta(hours=1)) in kept   # the last few hours stay hourly
    assert 15 <= len(kept) <= 45
    ages = sorted((end - datetime.strptime(n[10:29], "%Y-%m-%d_%H-%M-%S")).total_seconds() / 3600 for n in kept)
    for target in (1, 2, 4, 8, 24, 48, 96, 192, 384):     # something within about a window of each
        assert any(target * 0.5 <= a <= target * 2 for a in ages), (target, ages)


def test_thinned_drops_what_is_older_than_two_years(helper):
    folder = helper.drive / "shelly-pi-backups"
    folder.mkdir()
    (folder / "shelly-pi_2024-01-01_00-00-00.zip").write_text("ancient")
    (folder / "shelly-pi_2026-09-01_00-00-00.zip").write_text("recent")
    (helper.bridge / "outgoing" / NAME).write_bytes(b"z" * 100)
    r = helper(f"t2 copy 1234-ABCD {NAME} thin")
    assert r["ok"] is True
    left = sorted(p.name for p in folder.iterdir())
    assert "shelly-pi_2024-01-01_00-00-00.zip" not in left and "shelly-pi_2026-09-01_00-00-00.zip" in left


def test_thinned_steady_state_when_run_every_hour(helper):
    """The real use: one backup an hour for weeks. Nothing may fall out of the series."""
    from datetime import datetime, timedelta
    folder = helper.drive / "shelly-pi-backups"
    folder.mkdir()
    t = datetime(2026, 1, 1, 0, 0, 0)
    for _ in range(24 * 9):
        t += timedelta(hours=1)
        (helper.bridge / "outgoing" / _name(t)).write_bytes(b"z")
        assert helper(f"s copy 1234-ABCD {_name(t)} thin")["ok"] is True
    kept = sorted(p.name for p in folder.iterdir())
    ages = sorted((t - datetime.strptime(n[10:29], "%Y-%m-%d_%H-%M-%S")).total_seconds() / 3600 for n in kept)
    assert len(kept) <= 30
    for target in (1, 2, 4, 8, 24, 48, 96):
        assert any(target * 0.5 <= a <= target * 2 for a in ages), (target, ages)
