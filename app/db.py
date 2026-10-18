"""SQLite storage.

Tables
  readings     raw samples (kept ~7 days), one row per poll, WITHOUT ROWID
  rollup_1m    1-minute aggregates (kept ~90 days)
  rollup_15m   15-minute aggregates (kept forever)
  devices      the plugs
  meta         key/value (watermarks, last known good clock)

Every sample also stores `e_delta_wh`, the energy used since the previous
sample, computed from the plug's cumulative counter with reset handling.
Rollups just add those deltas up, so energy totals stay right even when
samples are lost.

Writes are batched by the caller (one transaction per flush) so the SD card
sees a handful of writes every few minutes instead of one per poll.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable, Sequence

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS devices(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  mac TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  room TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL DEFAULT '',
  shelly_id TEXT NOT NULL DEFAULT '',
  ip TEXT NOT NULL,
  switch_id INTEGER NOT NULL DEFAULT 0,
  enabled INTEGER NOT NULL DEFAULT 1,
  added_ts INTEGER NOT NULL,
  last_energy_wh REAL
);

CREATE TABLE IF NOT EXISTS readings(
  device_id INTEGER NOT NULL,
  ts INTEGER NOT NULL,
  power_w REAL,
  voltage REAL,
  current REAL,
  temp_c REAL,
  energy_wh REAL,
  e_delta_wh REAL NOT NULL DEFAULT 0,
  output INTEGER,
  PRIMARY KEY(device_id, ts)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS rollup_1m(
  device_id INTEGER NOT NULL,
  ts INTEGER NOT NULL,
  n INTEGER NOT NULL,
  p_sum REAL NOT NULL,
  p_max REAL NOT NULL,
  v_sum REAL NOT NULL,
  i_sum REAL NOT NULL,
  t_sum REAL NOT NULL,
  t_n INTEGER NOT NULL,
  e_wh REAL NOT NULL,
  on_n INTEGER NOT NULL,
  PRIMARY KEY(device_id, ts)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS rollup_15m(
  device_id INTEGER NOT NULL,
  ts INTEGER NOT NULL,
  n INTEGER NOT NULL,
  p_sum REAL NOT NULL,
  p_max REAL NOT NULL,
  v_sum REAL NOT NULL,
  i_sum REAL NOT NULL,
  t_sum REAL NOT NULL,
  t_n INTEGER NOT NULL,
  e_wh REAL NOT NULL,
  on_n INTEGER NOT NULL,
  PRIMARY KEY(device_id, ts)
) WITHOUT ROWID;
"""

READING_COLS = (
    "device_id, ts, power_w, voltage, current, temp_c, energy_wh, e_delta_wh, output"
)


class Database:
    """One shared connection guarded by a lock; call from threads, not the loop."""

    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=15)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA temp_store=MEMORY")
        self._conn.execute("PRAGMA busy_timeout=15000")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def snapshot(self, dest: Path | str) -> None:
        """A consistent copy of the whole database in one file, made while it is in use."""
        with self._lock:
            out = sqlite3.connect(str(dest))
            try:
                self._conn.backup(out)
                out.execute("PRAGMA journal_mode=DELETE")  # one self-contained file
            finally:
                out.close()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            self._conn.close()

    # ---- generic helpers -------------------------------------------------

    def query(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[tuple]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def execute(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur.rowcount

    def transaction(self):
        return _Tx(self)

    # ---- meta ------------------------------------------------------------

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        rows = self.query("SELECT v FROM meta WHERE k=?", (key,))
        return rows[0][0] if rows else default

    def get_meta_int(self, key: str, default: int = 0) -> int:
        v = self.get_meta(key)
        try:
            return int(v) if v is not None else default
        except ValueError:
            return default

    def set_meta(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO meta(k, v) VALUES(?, ?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (key, str(value)),
        )

    # ---- devices ---------------------------------------------------------

    _DEV_COLS = (
        "id, mac, name, room, model, shelly_id, ip, switch_id, enabled, "
        "added_ts, last_energy_wh"
    )

    @staticmethod
    def _dev(row: tuple) -> dict[str, Any]:
        keys = (
            "id", "mac", "name", "room", "model", "shelly_id", "ip",
            "switch_id", "enabled", "added_ts", "last_energy_wh",
        )
        d = dict(zip(keys, row))
        d["enabled"] = bool(d["enabled"])
        return d

    def list_devices(self) -> list[dict[str, Any]]:
        rows = self.query(f"SELECT {self._DEV_COLS} FROM devices ORDER BY id")
        return [self._dev(r) for r in rows]

    def get_device(self, device_id: int) -> dict[str, Any] | None:
        rows = self.query(f"SELECT {self._DEV_COLS} FROM devices WHERE id=?", (device_id,))
        return self._dev(rows[0]) if rows else None

    def get_device_by_mac(self, mac: str) -> dict[str, Any] | None:
        rows = self.query(f"SELECT {self._DEV_COLS} FROM devices WHERE mac=?", (mac,))
        return self._dev(rows[0]) if rows else None

    def add_device(
        self, *, mac: str, name: str, ip: str, room: str = "", model: str = "",
        shelly_id: str = "", switch_id: int = 0, added_ts: int = 0,
    ) -> dict[str, Any]:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO devices(mac, name, room, model, shelly_id, ip, "
                "switch_id, added_ts) VALUES(?,?,?,?,?,?,?,?)",
                (mac, name, room, model, shelly_id, ip, switch_id, added_ts),
            )
            self._conn.commit()
            device_id = cur.lastrowid
        dev = self.get_device(device_id)
        assert dev is not None
        return dev

    def update_device(self, device_id: int, **fields: Any) -> dict[str, Any] | None:
        allowed = {"name", "room", "model", "shelly_id", "ip", "switch_id", "enabled"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if "enabled" in sets:
            sets["enabled"] = 1 if sets["enabled"] else 0
        if sets:
            cols = ", ".join(f"{k}=?" for k in sets)
            self.execute(f"UPDATE devices SET {cols} WHERE id=?", (*sets.values(), device_id))
        return self.get_device(device_id)

    def delete_device(self, device_id: int, purge: bool) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM devices WHERE id=?", (device_id,))
            if purge:
                for table in ("readings", "rollup_1m", "rollup_15m"):
                    self._conn.execute(f"DELETE FROM {table} WHERE device_id=?", (device_id,))
            self._conn.commit()

    # ---- readings --------------------------------------------------------

    def insert_readings(
        self, rows: Iterable[tuple], last_energy: dict[int, float | None] | None = None,
    ) -> int:
        """Insert a batch in one transaction.

        rows: (device_id, ts, power_w, voltage, current, temp_c, energy_wh,
               e_delta_wh, output)
        Late rows older than the rollup watermark rewind it, so they are
        picked up (rollups are recomputed idempotently from raw rows).
        """
        rows = list(rows)
        if not rows:
            return 0
        min_ts = min(r[1] for r in rows)
        with self._lock:
            c = self._conn
            c.executemany(
                f"INSERT OR REPLACE INTO readings({READING_COLS}) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                rows,
            )
            for key, bucket in (("r1m_done", 60), ("r15m_done", 900)):
                cur = c.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
                if cur is not None and min_ts < int(cur[0]):
                    c.execute(
                        "UPDATE meta SET v=? WHERE k=?",
                        (str(min_ts // bucket * bucket), key),
                    )
            for dev_id, wh in (last_energy or {}).items():
                if wh is not None:
                    c.execute("UPDATE devices SET last_energy_wh=? WHERE id=?", (wh, dev_id))
            c.commit()
        return len(rows)

    def counts(self) -> dict[str, int]:
        out = {}
        for t in ("readings", "rollup_1m", "rollup_15m", "devices"):
            out[t] = self.query(f"SELECT COUNT(*) FROM {t}")[0][0]
        return out

    def file_size(self) -> int:
        try:
            return Path(self.path).stat().st_size
        except OSError:
            return 0


class _Tx:
    def __init__(self, db: Database):
        self.db = db

    def __enter__(self):
        self.db._lock.acquire()
        self.db._conn.execute("BEGIN")
        return self.db._conn

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.db._conn.commit()
            else:
                self.db._conn.rollback()
        finally:
            self.db._lock.release()
        return False
