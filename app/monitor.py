"""Polling, buffering and live events.

Every plug is polled by its own task. Samples are kept in RAM and written to
SQLite in one transaction every `flush_interval` seconds (so the SD card sees
a few writes per flush, not one per poll). A power cut loses at most that
window, which is fine for this project.

Samples are stamped on the monotonic clock when taken and converted to wall
time at flush. So if the wall clock is wrong at boot (no RTC / dead battery),
nothing is written until the time has been set, and then the held samples get
the right timestamps.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import leases, rollup
from .clock import ClockGuard
from .config import Settings
from .db import Database
from .shelly import Sample, ShellyClient, ShellyError

log = logging.getLogger(__name__)


@dataclass
class Live:
    online: bool = False
    fails: int = 0
    error: str | None = None
    last_ok_mono: float | None = None
    sample: Sample = field(default_factory=Sample)

    def to_dict(self, device_id: int, now_mono: float) -> dict[str, Any]:
        s = self.sample
        return {
            "id": device_id,
            "online": self.online,
            "error": self.error,
            "age": None if self.last_ok_mono is None else round(now_mono - self.last_ok_mono, 1),
            "output": s.output,
            "power_w": s.power_w,
            "voltage": s.voltage,
            "current": s.current,
            "freq": s.freq,
            "temp_c": s.temp_c,
            "energy_wh": s.energy_wh,
            "rssi": s.rssi,
        }


@dataclass
class Pending:
    device_id: int
    mono: float
    sample: Sample
    e_delta: float


def energy_delta(prev: float | None, cur: float | None) -> float:
    """Wh used since the previous counter reading. A decrease means the plug's
    counter was reset, so the new value is what was used since then."""
    if cur is None or prev is None:
        return 0.0
    d = cur - prev
    return d if d >= 0 else cur


class Monitor:
    def __init__(
        self, settings: Settings, db: Database, shelly: ShellyClient, clock: ClockGuard,
        mono: Callable[[], float] = time.monotonic,
    ):
        self.settings = settings
        self.db = db
        self.shelly = shelly
        self.clock = clock
        self._mono = mono

        self.devices: dict[int, dict[str, Any]] = {}
        self.live: dict[int, Live] = {}
        self.energy_last: dict[int, float | None] = {}
        self.buffer: list[Pending] = []
        self._inflight: list[Pending] = []
        self._flush_lock = asyncio.Lock()
        self._tasks: dict[int, asyncio.Task] = {}
        self._bg: list[asyncio.Task] = []
        self.subscribers: set[asyncio.Queue] = set()
        self.last_maintenance: dict[str, Any] = {}
        self.last_flush_error: str | None = None

    # ---- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        for dev in await asyncio.to_thread(self.db.list_devices):
            self.add_device(dev)
        self._bg = [
            asyncio.create_task(self._flush_loop(), name="flush"),
            asyncio.create_task(self._maintenance_loop(), name="maintenance"),
        ]

    async def stop(self) -> None:
        tasks = [*self._bg, *self._tasks.values()]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._bg, self._tasks = [], {}
        try:
            await self.flush()
        except Exception:  # noqa: BLE001
            log.exception("final flush failed")

    # ---- devices -----------------------------------------------------------

    def add_device(self, dev: dict[str, Any]) -> None:
        dev_id = dev["id"]
        self.devices[dev_id] = dict(dev)
        self.live.setdefault(dev_id, Live())
        self.energy_last.setdefault(dev_id, dev.get("last_energy_wh"))
        if dev.get("enabled", True) and dev_id not in self._tasks:
            self._tasks[dev_id] = asyncio.create_task(
                self._device_loop(dev_id), name=f"poll-{dev_id}"
            )

    def update_device(self, dev: dict[str, Any]) -> None:
        dev_id = dev["id"]
        self.devices[dev_id] = dict(dev)
        task = self._tasks.get(dev_id)
        if dev.get("enabled", True):
            if task is None or task.done():
                self._tasks[dev_id] = asyncio.create_task(
                    self._device_loop(dev_id), name=f"poll-{dev_id}"
                )
        elif task is not None:
            task.cancel()
            self._tasks.pop(dev_id, None)
            self.live[dev_id] = Live()
        self.publish({"type": "devices"})

    async def remove_device(self, dev_id: int) -> None:
        task = self._tasks.pop(dev_id, None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.devices.pop(dev_id, None)
        self.live.pop(dev_id, None)
        self.energy_last.pop(dev_id, None)
        self.buffer = [p for p in self.buffer if p.device_id != dev_id]
        self.publish({"type": "devices"})

    # ---- polling -----------------------------------------------------------

    async def _device_loop(self, dev_id: int) -> None:
        # Spread the first polls out so ten plugs don't all fire in the same instant.
        await asyncio.sleep((dev_id % 10) * 0.25)
        while dev_id in self.devices:
            try:
                await self.poll_once(dev_id)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("poll of device %s crashed", dev_id)
            live = self.live.get(dev_id)
            base = self.settings.poll_interval
            delay = base if not live or live.fails == 0 else min(base * (1 + live.fails), 30.0)
            await asyncio.sleep(delay)

    async def poll_once(self, dev_id: int) -> Live:
        dev = self.devices[dev_id]
        live = self.live[dev_id]
        try:
            sample = await self.shelly.status(dev["ip"], dev["switch_id"])
        except ShellyError as e:
            live.fails += 1
            live.error = str(e)
            if live.fails >= 2:
                live.online = False
            if live.fails in (3, 12):
                await self._reresolve(dev_id)
            self.publish({"type": "live", **live.to_dict(dev_id, self._mono())})
            return live

        now_m = self._mono()
        live.fails, live.error, live.online = 0, None, True
        live.last_ok_mono = now_m
        live.sample = sample
        delta = energy_delta(self.energy_last.get(dev_id), sample.energy_wh)
        if sample.energy_wh is not None:
            self.energy_last[dev_id] = sample.energy_wh
        self.buffer.append(Pending(dev_id, now_m, sample, delta))
        self._cap_buffer()
        self.publish({"type": "live", **live.to_dict(dev_id, now_m)})
        return live

    async def poll_now(self, dev_id: int) -> Live:
        return await self.poll_once(dev_id)

    async def _reresolve(self, dev_id: int) -> None:
        """The plug stopped answering: maybe DHCP gave it another address."""
        dev = self.devices.get(dev_id)
        if not dev or not leases.is_plain_ip(dev["ip"]):
            return
        ip = await asyncio.to_thread(leases.find_ip, self.settings.leases_file, dev["mac"])
        if ip and ip != dev["ip"]:
            log.info("device %s moved %s -> %s", dev_id, dev["ip"], ip)
            await asyncio.to_thread(self.db.update_device, dev_id, ip=ip)
            dev["ip"] = ip

    async def switch(self, dev_id: int, on: bool | None) -> Live:
        dev = self.devices[dev_id]
        if on is None:
            await self.shelly.switch_toggle(dev["ip"], dev["switch_id"])
        else:
            await self.shelly.switch_set(dev["ip"], dev["switch_id"], on)
        return await self.poll_once(dev_id)

    # ---- buffering and writing ----------------------------------------------

    def _cap_buffer(self) -> None:
        cap = self.settings.held_samples_max
        if len(self.buffer) > cap:
            drop = len(self.buffer) - cap * 9 // 10
            del self.buffer[:drop]
            log.warning("clock untrusted and buffer full: dropped %d oldest samples", drop)

    def _wall(self, mono: float, wall_now: float, mono_now: float) -> int:
        return int(round(wall_now - (mono_now - mono)))

    def pending_for(self, dev_id: int) -> list[tuple]:
        """Unflushed samples for charts: (ts, power, voltage, current, temp, output, e_delta).
        Empty while the clock is untrusted (their wall time is unknown)."""
        if not self.clock.trusted:
            return []
        wall, mono = self.clock.now(), self._mono()
        out = []
        for p in (*self._inflight, *self.buffer):
            if p.device_id == dev_id:
                s = p.sample
                out.append((self._wall(p.mono, wall, mono), s.power_w, s.voltage,
                            s.current, s.temp_c, s.output, p.e_delta))
        return out

    async def flush(self) -> int:
        async with self._flush_lock:
            if not self.buffer or not self.clock.trusted:
                return 0
            batch, self.buffer = self.buffer, []
            self._inflight = batch
            wall, mono = self.clock.now(), self._mono()
            rows = []
            for p in batch:
                s = p.sample
                rows.append((
                    p.device_id, self._wall(p.mono, wall, mono), s.power_w, s.voltage,
                    s.current, s.temp_c, s.energy_wh, p.e_delta,
                    None if s.output is None else int(s.output),
                ))
            try:
                n = await asyncio.to_thread(
                    self.db.insert_readings, rows, dict(self.energy_last)
                )
                await asyncio.to_thread(self.clock.note_good, max(r[1] for r in rows))
            except Exception as e:  # keep the samples, try again at the next flush
                self.buffer = batch + self.buffer
                self.last_flush_error = str(e)
                raise
            finally:
                self._inflight = []
            self.last_flush_error = None
            return n

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.flush_interval)
            try:
                await self.flush()
            except Exception:  # noqa: BLE001
                log.exception("flush failed")

    async def maintenance(self) -> dict[str, int]:
        """Flush, then roll up and prune. Skipped while the clock is untrusted."""
        await self.flush()
        if not self.clock.trusted:
            return {}
        now = int(self.clock.now())
        lag = int(self.settings.flush_interval) + 120
        stats = await asyncio.to_thread(
            rollup.run, self.db, now, lag,
            self.settings.raw_retention_days, self.settings.r1m_retention_days,
        )
        self.last_maintenance = {"at": now, **stats}
        return stats

    async def _maintenance_loop(self) -> None:
        await asyncio.sleep(30)
        while True:
            try:
                await self.maintenance()
            except Exception:  # noqa: BLE001
                log.exception("maintenance failed")
            await asyncio.sleep(self.settings.maintenance_interval)

    # ---- live events ---------------------------------------------------------

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)

    def publish(self, event: dict[str, Any]) -> None:
        for q in list(self.subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass  # a stuck browser tab must not hold anything up

    def live_snapshot(self) -> dict[int, dict[str, Any]]:
        now = self._mono()
        return {i: l.to_dict(i, now) for i, l in self.live.items()}
