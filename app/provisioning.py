"""Add-a-plug wizard: provisioning URL, scanning for new plugs, adopting them.

Flow
 1. The wizard shows a QR code. Scanned on a phone that is joined to the plug's
    own setup hotspot (ShellyPlugMG3-XXXX), it opens a URL that tells the plug
    to join the Pi's Wi-Fi:
        http://192.168.33.1/rpc/WiFi.SetConfig?config={"sta":{...}}
    (the documented Gen2+ "HTTP GET" form of WiFi.SetConfig)
 2. The plug joins the Pi's network and shows up in dnsmasq's lease file.
    `scan()` probes every lease for a Shelly (GET /shelly).
 3. `start_adopt()` pins its address, switches Cloud off, sets the power-on
    default and name, adds it to the dashboard, and checks it answers.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from typing import Any

from . import lan, leases
from .clock import ClockGuard
from .config import Settings
from .db import Database
from .monitor import Monitor
from .shelly import ShellyClient, ShellyError

log = logging.getLogger(__name__)

INITIAL_STATES = ("off", "on", "restore_last")

STEP_LABELS = [
    ("identify", "Find the plug"),
    ("reserve", "Reserve its address"),
    ("configure", "Set up the plug"),
    ("save", "Add to the dashboard"),
    ("apply", "Apply settings"),
    ("verify", "Check readings"),
]


@dataclass
class AdoptRequest:
    host: str
    name: str
    room: str = ""
    initial_state: str = "off"
    disable_ap: bool = True


@dataclass
class Job:
    id: str
    steps: list[dict[str, str]]
    status: str = "running"  # running | done | failed
    error: str | None = None
    device_id: int | None = None
    created: float = field(default_factory=time.monotonic)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "status": self.status, "error": self.error,
            "device_id": self.device_id, "steps": self.steps,
        }


class _StepFailed(Exception):
    pass


class Wizard:
    def __init__(
        self, settings: Settings, db: Database, shelly: ShellyClient,
        monitor: Monitor, clock: ClockGuard,
    ):
        self.cfg = settings
        self.db = db
        self.shelly = shelly
        self.monitor = monitor
        self.clock = clock
        self.jobs: dict[str, Job] = {}
        self._tasks: set[asyncio.Task] = set()
        self._not_shelly: dict[tuple[str, str], float] = {}

    # ---- URLs shown as QR codes -------------------------------------------

    def provision_url(self) -> str:
        cfg = {"sta": {"ssid": self.cfg.plug_ssid, "pass": self.cfg.plug_password, "enable": True}}
        if not self.cfg.plug_password:
            cfg["sta"].pop("pass")
        q = urllib.parse.quote(json.dumps(cfg, separators=(",", ":")), safe="")
        return f"http://{self.cfg.plug_ap_ip}/rpc/WiFi.SetConfig?config={q}"

    def reboot_url(self) -> str:
        return f"http://{self.cfg.plug_ap_ip}/rpc/Shelly.Reboot"

    def in_ap_subnet(self, host: str) -> bool:
        try:
            net = ipaddress.ip_network(f"{self.cfg.ap_ip}/24", strict=False)
            return ipaddress.ip_address(host) in net
        except ValueError:
            return False

    # ---- finding plugs -------------------------------------------------------

    def _candidate(self, info: dict, host: str, hostname: str, known: dict[str, Any]) -> dict:
        mac = leases.normalize_mac(info.get("mac")) or ""
        gen = int(info.get("gen") or 1)
        problem = None
        if gen < 2:
            problem = "First-generation Shelly: not supported (Gen2 and newer only)."
        elif info.get("auth_en"):
            problem = "This plug has a password set. Remove it or factory-reset the plug."
        return {
            "host": host, "mac": mac, "hostname": hostname,
            "id": info.get("id", ""), "model": info.get("app") or info.get("model") or "",
            "gen": gen, "fw": info.get("ver", ""),
            "supported": problem is None, "problem": problem,
            "known": mac in known, "known_name": known[mac]["name"] if mac in known else None,
        }

    async def _probe_lease(self, lease: leases.Lease, known: dict[str, Any]) -> dict | None:
        try:
            info = await self.shelly.device_info(lease.ip, timeout=1.5)
        except ShellyError:
            self._not_shelly[(lease.mac, lease.ip)] = time.monotonic() + 60
            return None
        return self._candidate(info, lease.ip, lease.hostname, known)

    def _own_nets(self) -> list[str]:
        """The Pi's own Wi-Fi network, which is left out when looking on the router's network."""
        return [f"{self.cfg.ap_ip}/24"] if self.cfg.ap_ip else []

    async def _probe_ips(self, ips: list[str], known: dict[str, Any], timeout: float) -> list[dict]:
        sem = asyncio.Semaphore(64)

        async def one(ip: str) -> dict | None:
            if self._not_shelly.get(("", ip), 0) > time.monotonic():
                return None
            async with sem:
                try:
                    info = await self.shelly.device_info(ip, timeout=timeout)
                except ShellyError:
                    self._not_shelly[("", ip)] = time.monotonic() + 60
                    return None
            return self._candidate(info, ip, "", known)
        return [r for r in await asyncio.gather(*(one(ip) for ip in ips)) if r]

    async def scan_network(self) -> list[dict]:
        """Try every address on the router's network. Slower than mDNS, but finds plugs that don't announce themselves."""
        known = {d["mac"]: d for d in await asyncio.to_thread(self.db.list_devices)}
        addrs = lan.scan_addresses(await asyncio.to_thread(lan.local_networks), self._own_nets())
        if not addrs:
            return []
        # A new scan forgets which addresses answered "not a Shelly" a moment ago.
        for k in [k for k in self._not_shelly if k[0] == ""]:
            self._not_shelly.pop(k, None)
        return await self._probe_ips(addrs, known, timeout=0.8)

    async def scan(self) -> list[dict]:
        if self.cfg.router_mode:
            known = {d["mac"]: d for d in await asyncio.to_thread(self.db.list_devices)}
            ips = await lan.browse(timeout=2.5, exclude_nets=self._own_nets())
            return await self._probe_ips(ips, known, timeout=1.5)
        found = await asyncio.to_thread(leases.read_leases, self.cfg.leases_file)
        known = {d["mac"]: d for d in await asyncio.to_thread(self.db.list_devices)}
        now = time.monotonic()
        todo = [l for l in found if self._not_shelly.get((l.mac, l.ip), 0) <= now]
        results = await asyncio.gather(*(self._probe_lease(l, known) for l in todo))
        return [r for r in results if r]

    async def probe(self, host: str) -> dict:
        info = await self.shelly.device_info(host, timeout=3)
        known = {d["mac"]: d for d in await asyncio.to_thread(self.db.list_devices)}
        return self._candidate(info, host, "", known)

    # ---- adopting --------------------------------------------------------------

    def start_adopt(self, req: AdoptRequest) -> Job:
        job = Job(id=uuid.uuid4().hex[:12],
                  steps=[{"key": k, "label": l, "status": "pending", "detail": ""}
                         for k, l in STEP_LABELS])
        for old in sorted(self.jobs.values(), key=lambda j: j.created)[:-19]:
            self.jobs.pop(old.id, None)
        self.jobs[job.id] = job
        task = asyncio.create_task(self._run(job, req), name=f"adopt-{job.id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job

    @staticmethod
    def _set(job: Job, key: str, status: str, detail: str = "") -> None:
        for s in job.steps:
            if s["key"] == key:
                s["status"], s["detail"] = status, detail

    def _fail(self, job: Job, key: str, msg: str) -> None:
        self._set(job, key, "failed", msg)
        raise _StepFailed(msg)

    async def _run(self, job: Job, req: AdoptRequest) -> None:
        try:
            await asyncio.wait_for(self._adopt(job, req), timeout=150)
            job.status = "done"
        except _StepFailed as e:
            job.status, job.error = "failed", str(e)
        except asyncio.TimeoutError:
            job.status, job.error = "failed", "Timed out. The plug may still be restarting; try again."
        except Exception as e:  # noqa: BLE001
            log.exception("adopt crashed")
            job.status, job.error = "failed", f"Unexpected error: {e}"
        self.monitor.publish({"type": "devices"})

    async def _adopt(self, job: Job, req: AdoptRequest) -> None:
        host, name = req.host.strip(), req.name.strip()
        restart = False

        # 1. identify -------------------------------------------------------
        self._set(job, "identify", "running")
        try:
            info = await self.shelly.device_info(host, timeout=4)
        except ShellyError as e:
            self._fail(job, "identify", f"Could not reach a Shelly at {host}: {e}")
        cand = self._candidate(info, host, "", {})
        if not cand["supported"]:
            self._fail(job, "identify", cand["problem"])
        mac = cand["mac"]
        if not mac:
            self._fail(job, "identify", "The plug did not report a MAC address.")
        model, shelly_id = cand["model"], cand["id"]
        self._set(job, "identify", "ok", f"{model or 'Shelly'} {mac}")

        # 2. reserve --------------------------------------------------------
        self._set(job, "reserve", "running")
        if self.cfg.router_mode:
            self._set(job, "reserve", "warn",
                      "Your router chooses this plug's address. Reserve it in the router so it never changes; "
                      "if it does change, the Pi looks for the plug by name.")
        elif leases.is_plain_ip(host) and self.in_ap_subnet(host):
            await asyncio.to_thread(leases.write_reservation, self.cfg.hosts_dir, mac, host, name)
            self._set(job, "reserve", "ok", f"Pinned to {host}")
        else:
            self._set(job, "reserve", "warn",
                      "Not on the Pi's Wi-Fi, so the address is not pinned.")

        # 3. configure ------------------------------------------------------
        self._set(job, "configure", "running")
        notes: list[str] = []
        try:
            restart |= await self.shelly.switch_configure(host, 0, name, req.initial_state)
        except ShellyError as e:
            self._fail(job, "configure", f"Could not set the power-on state: {e}")
        try:
            restart |= await self.shelly.cloud_disable(host)
        except ShellyError as e:
            notes.append(f"Cloud not switched off ({e})")
        try:
            restart |= await self.shelly.sys_set_name(host, name)
        except ShellyError as e:
            notes.append(f"Device name not set ({e})")
        if req.disable_ap:
            try:
                restart |= await self.shelly.ap_disable(host)
            except ShellyError as e:
                notes.append(f"Setup hotspot left on ({e})")
        self._set(job, "configure", "warn" if notes else "ok", "; ".join(notes))

        # 4. save -----------------------------------------------------------
        self._set(job, "save", "running")
        existing = await asyncio.to_thread(self.db.get_device_by_mac, mac)
        if existing:
            dev = await asyncio.to_thread(
                self.db.update_device, existing["id"], name=name, room=req.room.strip(),
                ip=host, model=model, shelly_id=shelly_id, enabled=True,
            )
            self.monitor.update_device(dev)
        else:
            added = int(self.clock.now()) if self.clock.trusted else 0
            dev = await asyncio.to_thread(
                self.db.add_device, mac=mac, name=name, ip=host, room=req.room.strip(),
                model=model, shelly_id=shelly_id, added_ts=added,
            )
            self.monitor.add_device(dev)
        job.device_id = dev["id"]
        self._set(job, "save", "ok", "Updated existing plug" if existing else "")

        # 5. apply ----------------------------------------------------------
        self._set(job, "apply", "running")
        if restart:
            try:
                await self.shelly.reboot(host)
            except ShellyError:
                pass  # the plug drops the connection while restarting
            await asyncio.sleep(4)
            self._set(job, "apply", "ok", "The plug restarted to apply the changes.")
        else:
            self._set(job, "apply", "ok", "No restart needed.")

        # 6. verify ---------------------------------------------------------
        self._set(job, "verify", "running")
        deadline = time.monotonic() + 45
        live = None
        while time.monotonic() < deadline:
            live = await self.monitor.poll_once(dev["id"])
            if live.fails == 0 and live.error is None:
                break
            await asyncio.sleep(2)
        else:
            self._fail(job, "verify", "The plug was added but did not answer after restarting. "
                                      "It will appear on the dashboard once it is back.")
        if live.sample.power_w is None:
            self._set(job, "verify", "warn", "The plug answers but reports no power readings.")
        else:
            self._set(job, "verify", "ok", f"{live.sample.power_w:.1f} W right now")
