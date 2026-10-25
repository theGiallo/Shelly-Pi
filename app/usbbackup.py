"""Automatic backups to a USB drive that is plugged into the Pi.

The drive is never formatted. The app runs in a container that cannot see the host's
drives, so it asks a small root script on the Pi (deploy/libexec/usb.sh, started by
a systemd path unit) to list the drives and to copy a finished backup onto one. The
script mounts the drive only for the copy, so it can be unplugged between backups.

The plan (which drive, how often, at what hour) lives in the database, so it is part of
a backup. What happened last lives in a file next to it and is not.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import backup
from .bridge import BridgeError, HostBridge
from .clock import ClockGuard
from .config import Settings
from .db import Database

log = logging.getLogger(__name__)

META_KEY = "autobackup"
STATE_FILE = "autobackup-state.json"
EVERY_CHOICES = (1, 2, 3, 4, 6, 8, 12, 24, 48, 72, 168)  # hours; up to a day they divide it evenly
RETRY_AFTER = 600        # seconds before trying again after a failure
LOOP_EVERY = 30
LIST_TIMEOUT = 10
COPY_TIMEOUT = 900
KEEP_MAX = 3650


class PlanError(ValueError):
    """The submitted plan isn't usable; the message is meant for the person."""


# ---- the plan --------------------------------------------------------------------------


@dataclass
class Plan:
    enabled: bool = False
    drive: dict | None = None      # {"id", "label", "name", "fstype", "size"} as last seen
    every_hours: int = 24
    hour: int = 0                  # time of day, 0-23, in `timezone`
    timezone: str = "UTC"
    keep: int = 14
    thin: bool = False             # keep backups spread over time (1 h, 2 h, 4 h ... 2 years) instead of the newest `keep`
    anchor: str = ""               # first day of a plan longer than a day, yyyy-mm-dd

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled, "drive": self.drive, "every_hours": self.every_hours,
            "hour": self.hour, "timezone": self.timezone, "keep": self.keep, "thin": self.thin,
            "anchor": self.anchor,
        }

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @classmethod
    def from_dict(cls, d: dict) -> "Plan":
        try:
            every = int(d.get("every_hours", 24))
            hour = int(d.get("hour", 0))
            keep = int(d.get("keep", 14))
        except (TypeError, ValueError):
            raise PlanError("Hours and the number of backups to keep must be numbers.") from None
        if every not in EVERY_CHOICES:
            raise PlanError("Choose how often from the list.")
        if not 0 <= hour <= 23:
            raise PlanError("The time of day is an hour from 0 to 23.")
        if not 1 <= keep <= KEEP_MAX:
            raise PlanError(f"Keep between 1 and {KEEP_MAX} backups.")
        tz = str(d.get("timezone") or "UTC").strip()
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            raise PlanError(f"'{tz}' isn't a time zone name. Use a name like Europe/Rome.") from None
        drive = d.get("drive")
        if drive is not None:
            drive = clean_drive(drive)
        anchor = str(d.get("anchor") or "")
        if anchor:
            try:
                date.fromisoformat(anchor)
            except ValueError:
                anchor = ""
        return cls(bool(d.get("enabled", False)), drive, every, hour, tz, keep, bool(d.get("thin", False)), anchor)

    # ---- schedule ---------------------------------------------------------------------

    def _slots_on(self, day: date) -> list[datetime]:
        step_days = max(1, self.every_hours // 24)
        if step_days > 1:
            try:
                base = date.fromisoformat(self.anchor)
            except ValueError:
                base = day
            if (day - base).days % step_days:
                return []
            hours = [self.hour]
        else:
            per = min(self.every_hours, 24)
            hours = [h for h in range(24) if (h - self.hour) % per == 0]
        return [datetime(day.year, day.month, day.day, h, tzinfo=self.tz) for h in hours]

    def slots_between(self, lo: float, hi: float) -> list[float]:
        """Scheduled instants in (lo, hi], oldest first."""
        out: list[float] = []
        first = datetime.fromtimestamp(lo, self.tz).date() - timedelta(days=1)
        last = datetime.fromtimestamp(hi, self.tz).date() + timedelta(days=1)
        day = first
        while day <= last:
            out += [t.timestamp() for t in self._slots_on(day) if lo < t.timestamp() <= hi]
            day += timedelta(days=1)
        return sorted(out)

    def last_slot(self, now: float) -> float | None:
        span = max(self.every_hours, 24) * 3600 * 2 + 86400
        s = self.slots_between(now - span, now)
        return s[-1] if s else None

    def next_slot(self, now: float) -> float | None:
        span = max(self.every_hours, 24) * 3600 * 2 + 86400
        s = self.slots_between(now, now + span)
        return s[0] if s else None


def clean_drive(d: Any) -> dict:
    """Keep only the fields we know, as plain short strings and numbers."""
    if not isinstance(d, dict):
        raise PlanError("That drive isn't valid.")
    ident = str(d.get("id") or "")
    if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", ident):
        raise PlanError("That drive isn't valid.")
    def s(k: str) -> str:
        return re.sub(r"[\x00-\x1f]", " ", str(d.get(k) or ""))[:64]
    try:
        size = max(0, int(d.get("size") or 0))
    except (TypeError, ValueError):
        size = 0
    return {"id": ident, "label": s("label"), "name": s("name"), "fstype": s("fstype"), "size": size}


# ---- talking to the Pi's helper ---------------------------------------------------------------


class UsbBridge(HostBridge):
    """The USB helper: also has a folder where finished backups wait to be copied."""

    def __init__(self, folder: Path):
        super().__init__(folder, "usb", "shelly-usb.path")
        self.outgoing = folder / "outgoing"


def clean_drives(raw: Any) -> list[dict]:
    out = []
    for d in raw if isinstance(raw, list) else []:
        try:
            item = clean_drive(d)
        except PlanError:
            continue
        item["device"] = re.sub(r"[^A-Za-z0-9/_.-]", "", str(d.get("device") or ""))[:40]
        item["supported"] = bool(d.get("supported"))
        out.append(item)
    return out


# ---- the manager ---------------------------------------------------------------------------------


class AutoBackup:
    def __init__(
        self, cfg: Settings, get_db: Callable[[], Database], get_clock: Callable[[], ClockGuard],
        make_archive: Callable[[Path, int, ZoneInfo], Awaitable[None]],
        bridge: UsbBridge | None = None,
    ):
        self.cfg = cfg
        self._db, self._clock, self._make = get_db, get_clock, make_archive
        self.bridge = bridge or UsbBridge(cfg.bridge_dir)
        self.state_path = cfg.data_dir / STATE_FILE
        self.running = False
        self.drives: list[dict] = []       # last list the Pi gave us
        self._task: asyncio.Task | None = None
        self._busy = asyncio.Lock()

    # ---- stored things ------------------------------------------------------------

    def plan(self) -> Plan:
        try:
            raw = self._db().get_meta(META_KEY)
            if raw:
                return Plan.from_dict(json.loads(raw))
        except Exception:  # noqa: BLE001 - damaged or mid-restore: act as if there is no plan
            pass
        return Plan()

    def save_plan(self, p: Plan) -> None:
        self._db().set_meta(META_KEY, json.dumps(p.to_dict()))

    def state(self) -> dict:
        try:
            d = json.loads(self.state_path.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_state(self, **changes: Any) -> None:
        st = self.state()
        st.update(changes)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(st), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def _now(self) -> float:
        c = self._clock()
        return c.now()

    # ---- what the page needs ---------------------------------------------------------------

    def status(self) -> dict:
        p, st = self.plan(), self.state()
        trusted = self._clock().trusted
        now = self._now()
        nxt = None
        if p.enabled and p.drive and trusted:
            retry = st.get("retry_at") or 0
            last, done = p.last_slot(now), st.get("done_slot") or 0
            nxt = max(retry, now) if (last and last > done) else p.next_slot(now)
        return {
            "plan": p.to_dict(), "last": st.get("last"), "next": nxt, "now": int(now), "running": self.running,
            "clock_trusted": trusted, "every_choices": list(EVERY_CHOICES),
        }

    async def list_drives(self) -> list[dict]:
        ans = await self.bridge.ask("list", LIST_TIMEOUT)
        if not ans.get("ok"):
            raise BridgeError(str(ans.get("message") or "The Pi couldn't list its drives."))
        self.drives = clean_drives(ans.get("drives"))
        return self.drives

    def update(self, body: dict, drive_id: str | None) -> Plan:
        """Validate and store a new plan. `drive_id` picks one of the drives last listed."""
        old = self.plan()
        d = dict(body)
        if drive_id:
            if old.drive and old.drive["id"] == drive_id:
                d["drive"] = old.drive
            else:
                found = next((x for x in self.drives if x["id"] == drive_id), None)
                if not found:
                    raise PlanError("That drive isn't in the list. Refresh the list of drives and pick it again.")
                if not found["supported"]:
                    raise PlanError(f"That drive uses {found['fstype'] or 'a format'} which can't be written to. "
                                    "Use one formatted as exFAT, FAT32, NTFS or ext4.")
                d["drive"] = found
        else:
            d["drive"] = None
        new = Plan.from_dict(d)
        if new.enabled and not new.drive:
            raise PlanError("Pick a drive first.")
        schedule_changed = (new.every_hours, new.hour, new.timezone) != (old.every_hours, old.hour, old.timezone)
        if new.every_hours >= 48 and (schedule_changed or not old.anchor or not old.enabled):
            new.anchor = datetime.fromtimestamp(self._now(), new.tz).date().isoformat()
        elif new.every_hours >= 48:
            new.anchor = old.anchor
        else:
            new.anchor = ""
        if new.enabled and (not old.enabled or schedule_changed or old.drive != new.drive):
            # Start counting from now: switching it on shouldn't fire a backup for a slot in the past.
            last = new.last_slot(self._now())
            self._save_state(done_slot=last or 0, retry_at=0)
        self.save_plan(new)
        return new

    # ---- doing it -----------------------------------------------------------------------------

    async def run_once(self) -> dict:
        """Make a backup and put it on the chosen drive. Returns the result and records it."""
        p = self.plan()
        if not p.drive:
            raise BridgeError("Pick a drive first.")
        if self._busy.locked():
            raise BridgeError("A backup to the drive is already running.")
        async with self._busy:
            self.running = True
            now = int(self._now())
            res: dict[str, Any] = {"time": now, "ok": False, "drive": p.drive.get("label") or p.drive.get("name") or "USB drive"}
            self.bridge.outgoing.mkdir(parents=True, exist_ok=True)
            name = backup.archive_name(now, p.tz)
            part = self.bridge.outgoing / name
            try:
                if not self._clock().trusted:
                    raise BridgeError("The Pi's clock isn't set, so the backup would carry the wrong date. Set it under Clock.")
                try:
                    await self._make(part, now, p.tz)
                except Exception:  # noqa: BLE001
                    log.exception("automatic backup couldn't be built")
                    raise BridgeError("The backup couldn't be made. See the log on the Pi.") from None
                ans = await self.bridge.ask(f"copy {p.drive['id']} {name} {'thin' if p.thin else p.keep}", COPY_TIMEOUT)
                if not ans.get("ok"):
                    raise BridgeError(str(ans.get("message") or "The copy failed."))
                res.update(ok=True, file=name, bytes=int(ans.get("bytes") or 0),
                           free=int(ans.get("free") or 0), removed=int(ans.get("removed") or 0))
            except BridgeError as e:
                res["message"] = str(e)
                log.warning("automatic backup: %s", e)
            finally:
                try:
                    part.unlink()
                except FileNotFoundError:
                    pass
                self.running = False
            self._save_state(last=res)
            return res

    async def tick(self) -> None:
        """One look at the schedule: run a backup if one is due."""
        p = self.plan()
        if not (p.enabled and p.drive) or not self._clock().trusted or self.running:
            return
        st = self.state()
        now = self._now()
        slot = p.last_slot(now)
        if not slot or slot <= (st.get("done_slot") or 0) or now < (st.get("retry_at") or 0):
            return
        res = await self.run_once()
        if res["ok"]:
            self._save_state(done_slot=slot, retry_at=0)
        else:
            self._save_state(retry_at=now + RETRY_AFTER)

    async def loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("automatic backup loop")
            await asyncio.sleep(LOOP_EVERY)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.loop(), name="autobackup")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
