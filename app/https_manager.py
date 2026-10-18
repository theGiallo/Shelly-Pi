"""Keeps HTTPS running: makes the CA and certificate when needed, renews them, starts the listener.

Settings (two switches) live in the database so they are part of a backup; the CA and the
certificates live in <data>/tls and are deliberately not.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
from typing import Any, Callable

from .clock import ClockGuard
from .config import Settings
from .db import Database
from .tls import CertStore, TlsError, local_ipv4s, short_name
from .tlsfront import TlsFront

log = logging.getLogger(__name__)
META_KEY = "https"
DEFAULTS = {"enabled": True, "https_only": False}
CHECK_UP = 300       # seconds between looks at the address list and expiry while running
CHECK_DOWN = 30      # while waiting for a clock or an error to clear


class HttpsManager:
    def __init__(
        self, cfg: Settings, get_db: Callable[[], Database], get_clock: Callable[[], ClockGuard],
        addresses: Callable[[], list[str]] = local_ipv4s,
    ):
        self.cfg = cfg
        self._db, self._clock, self._addresses = get_db, get_clock, addresses
        self.store = CertStore(cfg.data_dir / "tls")
        self.front = TlsFront(cfg.https_port, cfg.port, bind=cfg.host)
        self.last_error: str | None = None
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._cache: dict[str, Any] | None = None  # settings, so the redirect check doesn't hit the database

    # ---- settings -----------------------------------------------------------------

    @property
    def available(self) -> bool:
        return self.cfg.https_port != 0

    @property
    def hostname(self) -> str:
        return short_name(self.cfg.hostname or socket.gethostname())

    def settings(self) -> dict[str, bool]:
        if self._cache is None:
            data = dict(DEFAULTS)
            try:
                raw = self._db().get_meta(META_KEY)
                if raw:
                    stored = json.loads(raw)
                    data.update({k: bool(stored[k]) for k in DEFAULTS if k in stored})
            except Exception:  # noqa: BLE001 - damaged or mid-restore: use defaults, don't stop the dashboard
                pass
            self._cache = data
        return dict(self._cache)

    def save_settings(self, enabled: bool, https_only: bool) -> None:
        data = {"enabled": enabled, "https_only": bool(https_only and enabled)}
        self._db().set_meta(META_KEY, json.dumps(data))
        self._cache = data

    def forget_cache(self) -> None:
        self._cache = None  # after the database was replaced

    def redirecting(self) -> bool:
        """True when plain-HTTP requests should be sent to HTTPS."""
        return self.available and self.front.running and self.settings()["https_only"]

    def redirect_host(self, host_header: str) -> str:
        """The host to send people to: the one they used if the certificate covers it."""
        info = self.store.server_info()
        names = set(info.dns + info.ips) if info else set()
        host = host_header.rsplit(":", 1)[0] if host_header.count(":") == 1 else host_header
        host = host.strip("[]").lower()
        return host if host in names else f"{self.hostname}.local"

    # ---- doing the work -------------------------------------------------------------------

    def _clock_ok(self) -> bool:
        return self._clock().trusted

    def _now(self) -> float:
        return self._clock().now()

    def _make(self, force_new_ca: bool = False) -> bool:
        """Blocking part: create the CA / certificate as needed. Returns True if a new certificate was issued."""
        ips = self._addresses()
        if force_new_ca or not self.store.has_ca():
            self.store.create_ca(self._now(), self.hostname)
        return self.store.ensure_server(self._now(), self.hostname, ips)

    async def reconcile(self, force_new_ca: bool = False) -> None:
        """Bring the listener and certificates in line with the settings. Never raises."""
        if not self.available:
            return
        async with self._lock:
            try:
                if not self.settings()["enabled"]:
                    await self.front.stop()
                    self.last_error = None
                    return
                have_cert = self.store.has_ca() and self.store.server_info() is not None
                if self._clock_ok():
                    issued = await asyncio.to_thread(self._make, force_new_ca)
                    if issued:
                        log.info("issued a new server certificate")
                elif force_new_ca or not have_cert:
                    raise TlsError("The clock isn't set, so a certificate can't be made yet. Set it under Clock.")
                if self.front.running:
                    self.front.reload(self.store.server_crt, self.store.server_key)
                else:
                    await self.front.start(self.store.server_crt, self.store.server_key)
                self.last_error = None
            except TlsError as e:
                self.last_error = str(e)
                log.warning("HTTPS: %s", e)
            except OSError as e:
                self.last_error = f"Couldn't listen on port {self.cfg.https_port}: {e.strerror or e}"
                log.warning("HTTPS: %s", self.last_error)
            except Exception as e:  # noqa: BLE001
                self.last_error = "HTTPS couldn't be started. See the log on the Pi."
                log.exception("HTTPS failed: %s", e)

    async def loop(self) -> None:
        while True:
            await self.reconcile()
            ok = self.front.running and self.last_error is None
            await asyncio.sleep(CHECK_UP if ok or not self.settings()["enabled"] else CHECK_DOWN)

    def start(self) -> None:
        if self.available and self._task is None:
            self._task = asyncio.create_task(self.loop(), name="https")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self.front.stop()

    # ---- what the page shows -------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        st: dict[str, Any] = {"available": self.available}
        if not self.available:
            return st
        s = self.settings()
        clock = self._clock()
        ca = srv = None
        if self.store.has_ca():
            try:
                ca = self.store.ca_info()
            except TlsError:
                pass
        info = self.store.server_info()
        if info:
            srv = {"dns": info.dns, "ips": info.ips, "not_after": info.not_after,
                   "days_left": round(info.days_left(clock.now()), 1)}
        st.update({
            **s, "listening": self.front.running, "port": self.cfg.https_port,
            "hostname": self.hostname, "ca": ca, "server": srv,
            "clock_trusted": clock.trusted, "error": self.last_error,
        })
        return st
