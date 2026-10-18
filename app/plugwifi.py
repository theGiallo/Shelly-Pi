"""Finding the setup Wi-Fi of a new plug, so the phone can join it by scanning a QR code.

A Shelly that hasn't been set up (or was reset) hosts an open network named like
ShellyPlugMG3-A1B2C3. The Pi scans (through its host helper, see bridge.py), keeps the Shelly
networks, and ranks them by signal: the one the Pi hears best is the likeliest to be the plug that
was just plugged in next to it.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Callable, Iterable

from .bridge import BridgeError, HostBridge

# ShellyPlugMG3-A1B2C3, ShellyPlusPlugS-AABBCCDDEEFF, shellyplug-s-123ABC ...
SHELLY_SSID = re.compile(r"^shelly[\w]*(?:-[\w]+)*-[0-9a-f]{4,12}$", re.IGNORECASE)
SCAN_TIMEOUT = 30
CACHE_SECONDS = 4          # several open pages asking at once share one scan
MAX_AGE_SHOWN = 300


def is_shelly_ssid(ssid: str) -> bool:
    return bool(SHELLY_SSID.match(ssid or ""))


def quality(dbm: float) -> str:
    if dbm >= -55:
        return "excellent"
    if dbm >= -67:
        return "good"
    if dbm >= -78:
        return "fair"
    return "weak"


def wifi_qr_text(ssid: str) -> str:
    """The standard Wi-Fi sign-in code that phone cameras understand, for an open network."""
    esc = re.sub(r'([\\;,:"])', r"\\\1", ssid)
    return f"WIFI:T:nopass;S:{esc};;"


def shelly_networks(raw: Any, known_macs: Iterable[tuple[str, str]] = ()) -> list[dict]:
    """Keep the Shelly networks from a scan, one per name (strongest signal), strongest first.
    `known_macs` is (mac, name) of plugs already on the dashboard, to flag them."""
    best: dict[str, dict] = {}
    for n in raw if isinstance(raw, list) else []:
        if not isinstance(n, dict):
            continue
        ssid = str(n.get("ssid") or "")
        if not is_shelly_ssid(ssid):
            continue
        try:
            dbm = float(n.get("signal"))
            channel = int(n.get("channel") or 0)
        except (TypeError, ValueError):
            continue
        cur = best.get(ssid)
        if cur is None or dbm > cur["signal"]:
            best[ssid] = {"ssid": ssid, "signal": round(dbm), "channel": channel, "secured": bool(n.get("secured"))}
    macs = [(re.sub(r"[^0-9a-f]", "", m.lower()), name) for m, name in known_macs]
    out = []
    for net in sorted(best.values(), key=lambda x: (-x["signal"], x["ssid"])):
        net["quality"] = quality(net["signal"])
        suffix = net["ssid"].rsplit("-", 1)[-1].lower()
        match = next((name for m, name in macs if m.endswith(suffix)), None)
        net["known"], net["known_name"] = match is not None, match
        out.append(net)
    return out


class PlugWifi:
    def __init__(self, bridge: HostBridge, devices: Callable[[], Iterable[dict]],
                 now: Callable[[], float] = time.monotonic):
        self.bridge, self._devices, self._now = bridge, devices, now
        self._lock = asyncio.Lock()
        self._last: dict | None = None
        self._at = 0.0

    async def scan(self) -> dict:
        async with self._lock:
            if self._last is not None and self._now() - self._at < CACHE_SECONDS:
                return self._last
            ans = await self.bridge.ask("scan", SCAN_TIMEOUT)
            if not ans.get("ok"):
                raise BridgeError(str(ans.get("message") or "The Pi couldn't scan for Wi-Fi networks."))
            known = [(d.get("mac") or "", d.get("name") or "") for d in self._devices()]
            self._last = {"networks": shelly_networks(ans.get("networks"), known), "iface": str(ans.get("iface") or "")}
            self._at = self._now()
            return self._last
