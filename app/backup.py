"""Export and import of everything the Pi stores.

An archive is a zip with these members and nothing else:

  manifest.json          what it is, when it was made, row counts
  shelly-monitor.db      the whole database: plugs, readings, rollups, prices
  config.json            the tunable settings, without the two passwords
  dhcp-hosts.d/*.conf    the address each plug is pinned to

It is named shelly-monitor_yyyy-mm-dd_HH-MM-ss.zip, so a folder of backups sorts by date.

Restoring replaces the database, the plugs' address reservations and the tunable settings
(poll interval, retention and so on). It deliberately keeps this Pi's own Wi-Fi name and
password, dashboard password, address and port: those belong to the machine and are set by
setup-host.sh, so taking them from another Pi's backup could leave the app out of step with the
Wi-Fi it actually hosts. The data it replaces is first copied to <data>/pre-restore.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import Settings
from .db import Database

FORMAT = 1
KIND = "shelly-monitor-backup"
DB_NAME = "shelly-monitor.db"
HOSTS_DIR = "dhcp-hosts.d"
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_UNPACKED_BYTES = 2 * 1024 * 1024 * 1024
MAX_MEMBERS = 1000
REQUIRED_TABLES = ("meta", "devices", "readings", "rollup_1m", "rollup_15m")
SECRET_KEYS = ("password", "ap_password")
# Settings a restore may change. Anything not listed here stays as it is on this Pi.
TUNABLE = (
    "poll_interval", "poll_timeout", "flush_interval", "maintenance_interval",
    "raw_retention_days", "r1m_retention_days", "clock_tolerance_s", "held_samples_max",
)
HOST_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}\.conf$")
ASIDE = "pre-restore"


class BackupError(ValueError):
    """The archive can't be used; the message is meant for the person."""


def zone_or(name: str | None, fallback: str = "UTC") -> ZoneInfo:
    for n in (name, fallback):
        try:
            if n:
                return ZoneInfo(n)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            continue
    return ZoneInfo("UTC")


def stamp(epoch: float, tz: ZoneInfo) -> str:
    """yyyy-mm-dd_HH-MM-ss in the given zone: sorts alphabetically in date order."""
    return datetime.fromtimestamp(epoch, tz).strftime("%Y-%m-%d_%H-%M-%S")


def archive_name(epoch: float, tz: ZoneInfo) -> str:
    return f"shelly-monitor_{stamp(epoch, tz)}.zip"


# ---- export ----------------------------------------------------------------------------


def build_archive(
    db: Database, cfg: Settings, dest: Path, *, now: int, tz: ZoneInfo, app_version: str,
    held_samples: int, clock_trusted: bool,
) -> dict[str, Any]:
    """Write the archive to `dest` and return its manifest."""
    work = dest.parent / (dest.name + ".parts")
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    try:
        snap = work / DB_NAME
        db.snapshot(snap)
        manifest = {
            "kind": KIND, "format": FORMAT, "app_version": app_version,
            "created_epoch": now,
            "created_utc": datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "created_local": stamp(now, tz), "timezone": tz.key,
            "clock_trusted": clock_trusted,
            "held_samples_not_included": held_samples,
            "counts": db.counts(),
            "note": "config.json has the dashboard and Wi-Fi passwords removed.",
        }
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            z.writestr("manifest.json", json.dumps(manifest, indent=2) + "\n")
            z.write(snap, DB_NAME)
            cfg_file = cfg.data_dir / "config.json"
            if cfg_file.is_file():
                try:
                    data = json.loads(cfg_file.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    data = {}
                data = {k: v for k, v in data.items() if k not in SECRET_KEYS}
                z.writestr("config.json", json.dumps(data, indent=2) + "\n")
            if cfg.hosts_dir.is_dir():
                for f in sorted(cfg.hosts_dir.glob("*.conf")):
                    if HOST_FILE_RE.match(f.name) and f.is_file():
                        z.write(f, f"{HOSTS_DIR}/{f.name}")
        return manifest
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---- import ------------------------------------------------------------------------------


def read_archive(archive: Path, out: Path) -> dict[str, Any]:
    """Check an uploaded archive and unpack it into `out` (a fresh directory).

    Members are never extracted by their own names: each allowed one is copied to a path
    built here, so nothing in the archive can write outside `out`.
    """
    try:
        z = zipfile.ZipFile(archive)
    except (zipfile.BadZipFile, OSError):
        raise BackupError("This isn't a backup made by Shelly Monitor. Choose a .zip it exported.") from None
    with z:
        infos = z.infolist()
        if len(infos) > MAX_MEMBERS or sum(i.file_size for i in infos) > MAX_UNPACKED_BYTES:
            raise BackupError("This archive is too large to be one of ours.")
        names = {i.filename for i in infos if not i.is_dir()}
        for n in names:
            ok = n in ("manifest.json", DB_NAME, "config.json") or (
                n.startswith(HOSTS_DIR + "/") and HOST_FILE_RE.match(n[len(HOSTS_DIR) + 1:] or "/")
            )
            if not ok:
                raise BackupError(f"The archive contains a file this program doesn't use ({n[:60]}). Nothing was changed.")
        if "manifest.json" not in names or DB_NAME not in names:
            raise BackupError("This isn't a backup made by Shelly Monitor: the manifest or the database is missing.")
        try:
            manifest = json.loads(z.read("manifest.json").decode("utf-8"))
        except (ValueError, UnicodeDecodeError, KeyError):
            raise BackupError("The archive's manifest can't be read.") from None
        if not isinstance(manifest, dict) or manifest.get("kind") != KIND:
            raise BackupError("This isn't a backup made by Shelly Monitor.")
        fmt = manifest.get("format")
        if not isinstance(fmt, int) or fmt > FORMAT:
            raise BackupError("This backup was made by a newer version of Shelly Monitor. Update this Pi first.")
        if fmt < 1:
            raise BackupError("This backup's format isn't supported.")

        out.mkdir(parents=True)
        (out / HOSTS_DIR).mkdir()
        for n in names:
            if n == "manifest.json":
                continue
            target = out / n
            with z.open(n) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
    check_database(out / DB_NAME)
    return manifest


def check_database(path: Path) -> None:
    try:
        conn = sqlite3.connect(path)
        try:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise BackupError("The database inside the archive is damaged. Nothing was changed.")
            have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
    except sqlite3.Error:
        raise BackupError("The database inside the archive can't be read. Nothing was changed.") from None
    missing = [t for t in REQUIRED_TABLES if t not in have]
    if missing:
        raise BackupError(f"The database inside the archive lacks: {', '.join(missing)}. Nothing was changed.")


def _copy_if(src: Path, dst: Path) -> None:
    if src.is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def _remove_db(cfg: Settings) -> None:
    for suffix in ("", "-wal", "-shm"):
        Path(str(cfg.db_path) + suffix).unlink(missing_ok=True)


def _write_config(cfg: Settings, merged: dict[str, Any]) -> None:
    f = cfg.data_dir / "config.json"
    tmp = cfg.data_dir / ".config.json.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as h:
        json.dump(merged, h, indent=2)
        h.write("\n")
    os.replace(tmp, f)


def apply_files(cfg: Settings, unpacked: Path) -> list[str]:
    """Put the checked, unpacked backup in place. The database must be closed.

    The data being replaced is first copied to <data>/pre-restore, and put back if anything
    here fails. Returns the names of the settings that were taken from the backup.
    """
    aside = cfg.data_dir / ASIDE
    shutil.rmtree(aside, ignore_errors=True)
    aside.mkdir(parents=True)
    _copy_if(cfg.db_path, aside / DB_NAME)
    _copy_if(cfg.data_dir / "config.json", aside / "config.json")
    if cfg.hosts_dir.is_dir():
        for f in cfg.hosts_dir.glob("*.conf"):
            _copy_if(f, aside / HOSTS_DIR / f.name)
    try:
        _remove_db(cfg)
        shutil.move(str(unpacked / DB_NAME), cfg.db_path)

        cfg.hosts_dir.mkdir(parents=True, exist_ok=True)
        for f in cfg.hosts_dir.glob("*.conf"):
            f.unlink()
        for f in (unpacked / HOSTS_DIR).glob("*.conf"):
            shutil.copy2(f, cfg.hosts_dir / f.name)

        taken: list[str] = []
        incoming_file = unpacked / "config.json"
        if incoming_file.is_file():
            try:
                incoming = json.loads(incoming_file.read_text(encoding="utf-8"))
            except ValueError:
                incoming = {}
            current_file = cfg.data_dir / "config.json"
            try:
                merged = json.loads(current_file.read_text(encoding="utf-8")) if current_file.is_file() else {}
            except ValueError:
                merged = {}
            for k in TUNABLE:
                if k in incoming and isinstance(incoming[k], (int, float)) and not isinstance(incoming[k], bool):
                    merged[k] = incoming[k]
                    taken.append(k)
            if taken:
                _write_config(cfg, merged)
        return taken
    except Exception:
        undo(cfg)
        raise


def undo(cfg: Settings) -> None:
    """Put back what apply_files copied aside."""
    aside = cfg.data_dir / ASIDE
    _remove_db(cfg)
    _copy_if(aside / DB_NAME, cfg.db_path)
    _copy_if(aside / "config.json", cfg.data_dir / "config.json")
    if cfg.hosts_dir.is_dir():
        for f in cfg.hosts_dir.glob("*.conf"):
            f.unlink()
    for f in (aside / HOSTS_DIR).glob("*.conf"):
        _copy_if(f, cfg.hosts_dir / f.name)


def apply_to_settings(cfg: Settings, taken: list[str], env: dict[str, str] | None = None) -> None:
    """Make the running app use the restored settings (an SM_* environment variable still wins)."""
    env = os.environ if env is None else env
    f = cfg.data_dir / "config.json"
    if not taken or not f.is_file():
        return
    data = json.loads(f.read_text(encoding="utf-8"))
    for k in taken:
        if "SM_" + k.upper() in env:
            continue
        setattr(cfg, k, type(getattr(cfg, k))(data[k]))
