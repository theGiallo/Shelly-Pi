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
