"""HTTP API and page serving."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import re
import secrets
import shutil
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable, Literal

import httpx
import segno
from fastapi import FastAPI, HTTPException, Query, Request
from starlette.background import BackgroundTask
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, backup, history, leases, tariff as tariff_mod
from .auth import BasicAuthMiddleware, HttpsRedirect, RequireCustomHeader
from .https_manager import HttpsManager
from .bridge import BridgeError, HostBridge
from .plugwifi import PlugWifi, is_shelly_ssid, wifi_qr_text
from .usbbackup import AutoBackup, PlanError
from .clock import ClockGuard
from .config import Settings, load_settings
from .db import Database
from .monitor import Monitor
from .provisioning import INITIAL_STATES, AdoptRequest, Wizard
from .shelly import ShellyClient, ShellyError

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
MAX_EPOCH = 4_102_444_800  # 2100-01-01

# Plug addresses must be IPv4 literals (optionally with a port, used by the simulated plugs),
# so the wizard can't be pointed at arbitrary host names.
HOST_PATTERN = r"^\d{1,3}(\.\d{1,3}){3}(:\d{1,5})?$"
HOST_RE = re.compile(HOST_PATTERN)

# Everything is served from this origin: no CDNs, no external scripts.
PAGE_HEADERS = {
    "Cache-Control": "no-cache",
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "connect-src 'self' https://api.github.com; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}


class SwitchBody(BaseModel):
    on: bool | None = None  # None = toggle


class DevicePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=40)
    room: str | None = Field(default=None, max_length=40)
    enabled: bool | None = None


class AdoptBody(BaseModel):
    host: str = Field(pattern=HOST_PATTERN)
    name: str = Field(min_length=1, max_length=40)
    room: str = Field(default="", max_length=40)
    initial_state: Literal["off", "on", "restore_last"] = "off"
    disable_ap: bool = True


class TimeBody(BaseModel):
    epoch: int


class SlotBody(BaseModel):
    name: str = Field(default="", max_length=40)
    price: float
    days: list[int]
    start: str
    end: str


class HttpsBody(BaseModel):
    enabled: bool = True
    https_only: bool = False


class AutoBackupBody(BaseModel):
    enabled: bool = False
    drive_id: str | None = Field(default=None, max_length=64)
    every_hours: int = 24
    hour: int = 0
    timezone: str = Field(default="UTC", max_length=64)
    keep: int = 14


class ResetCaBody(BaseModel):
    confirm: bool = False


class TariffBody(BaseModel):
    enabled: bool = False
    currency: str = Field(default="€", max_length=16)
    currency_after: bool = True
    decimals: int = 2
    timezone: str = Field(default="UTC", max_length=64)
    base_price: float = 0.0
    slots: list[SlotBody] = Field(default_factory=list, max_length=tariff_mod.MAX_SLOTS)


def create_app(
    settings: Settings | None = None,
    time_fn: Callable[[], float] = time.time,
) -> FastAPI:
    cfg = settings or load_settings()

    async def start_services(app: FastAPI) -> None:
        """Open the database and start polling. Also used again after a restore."""
        db = Database(cfg.db_path)
        clock = ClockGuard(cfg, db, time_fn)
        monitor = Monitor(cfg, db, app.state.shelly, clock)
        wizard = Wizard(cfg, db, app.state.shelly, monitor, clock)
        app.state.db, app.state.clock = db, clock
        app.state.monitor, app.state.wizard = monitor, wizard
        app.state.tariff = tariff_mod.load(db)
        st = clock.status()
        if not st["trusted"]:
            log.warning("System clock not trusted (%s): holding samples until the time is set.", st["reason"])
        await monitor.start()

    async def stop_services(app: FastAPI) -> None:
        await app.state.monitor.stop()
        app.state.db.close()

    async def make_archive(dest: Path, now: int, zone) -> None:
        """Build a backup file at `dest` (used by the automatic backups)."""
        async with app.state.admin_lock:
            try:
                await app.state.monitor.flush()
            except Exception:  # noqa: BLE001
                log.exception("flush before backup failed")
            await asyncio.to_thread(
                backup.build_archive, app.state.db, cfg, dest, now=now, tz=zone, app_version=__version__,
                held_samples=len(app.state.monitor.buffer), clock_trusted=app.state.clock.trusted,
            )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per poll would flood the journal
        for d in (cfg.data_dir, cfg.hosts_dir, cfg.bridge_dir, cfg.leases_file.parent):
            d.mkdir(parents=True, exist_ok=True)
        http = httpx.AsyncClient(limits=httpx.Limits(max_connections=40, max_keepalive_connections=0))
        app.state.cfg, app.state.http = cfg, http
        app.state.shelly = ShellyClient(http, timeout=cfg.poll_timeout)
        app.state.admin_lock = asyncio.Lock()  # one backup or restore at a time
        if not cfg.password:
            log.warning("No admin password set: anyone on the network can control the plugs.")
        await start_services(app)
        app.state.https = HttpsManager(cfg, lambda: app.state.db, lambda: app.state.clock)
        app.state.https.start()
        app.state.plugwifi = PlugWifi(
            HostBridge(cfg.bridge_dir, "wifiscan", "shelly-wifiscan.path"), lambda: mon().devices.values())
        app.state.autobackup = AutoBackup(cfg, lambda: app.state.db, lambda: app.state.clock, make_archive)
        app.state.autobackup.start()
        try:
            yield
        finally:
            await app.state.autobackup.stop()
            await app.state.https.stop()
            await stop_services(app)
            await http.aclose()

    app = FastAPI(title="Shelly-Pi", version=__version__, lifespan=lifespan)
    app.add_middleware(RequireCustomHeader)
    app.add_middleware(HttpsRedirect, manager_getter=lambda: getattr(app.state, "https", None))
    app.add_middleware(BasicAuthMiddleware, password=cfg.password)

    def mon() -> Monitor:
        return app.state.monitor

    def eff_now() -> int:
        c: ClockGuard = app.state.clock
        return int(c.now()) if c.trusted else max(c.last_good, cfg.min_valid_epoch)

    # ---- pages -----------------------------------------------------------------

    def page(name: str):
        def handler() -> FileResponse:
            return FileResponse(STATIC / name, headers=PAGE_HEADERS)
        return handler

    app.get("/", include_in_schema=False)(page("index.html"))
    app.get("/add", include_in_schema=False)(page("add.html"))
    app.get("/settings", include_in_schema=False)(page("settings.html"))
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict:
        return {"ok": True, "version": __version__}

    # ---- state -------------------------------------------------------------------

    def tariff_view(now: int | None = None) -> dict:
        t: tariff_mod.Tariff = app.state.tariff
        view = t.to_dict()
        if t.enabled and now is not None:
            i = t.index_at(now)
            view["now"] = {"slot": t.slots[i].name if i >= 0 else None, "price": t.price_for(i)}
        return view

    def device_view(
        dev: dict[str, Any], live: dict | None, energy: float | None, cost: float | None = None,
    ) -> dict:
        return {
            "id": dev["id"], "name": dev["name"], "room": dev["room"], "model": dev["model"],
            "ip": dev["ip"], "mac": dev["mac"], "enabled": dev["enabled"],
            "live": live, "energy_since_wh": energy, "cost_since": cost,
        }

    @app.get("/api/state")
    async def state(since: int | None = None) -> dict:
        now = eff_now()
        since = since if since is not None else now - 86400
        live = mon().live_snapshot()
        devices = []
        for dev in sorted(mon().devices.values(), key=lambda d: d["id"]):
            t: tariff_mod.Tariff = app.state.tariff
            e, c = await asyncio.to_thread(
                history.energy_cost, app.state.db, cfg, now, dev["id"], since, now, t,
                mon().pending_for(dev["id"]),
            )
            devices.append(device_view(dev, live.get(dev["id"]), e, c if t.enabled else None))
        return {
            "now": now, "clock": app.state.clock.status(), "version": __version__,
            "tariff": tariff_view(now),
            "ap": {"ssid": cfg.ap_ssid, "ip": cfg.ap_ip}, "devices": devices,
        }

    @app.get("/api/stream")
    async def stream(request: Request) -> StreamingResponse:
        q = mon().subscribe()

        async def gen():
            try:
                yield "retry: 3000\n\n"
                while True:
                    try:
                        ev = await asyncio.wait_for(q.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
                        if await request.is_disconnected():
                            return
                        continue
                    yield f"event: {ev['type']}\ndata: {json.dumps(ev)}\n\n"
            finally:
                mon().unsubscribe(q)

        return StreamingResponse(
            gen(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/history")
    async def get_history(
        device: int, from_: int = Query(alias="from"), to: int = Query(), points: int = 300,
    ) -> dict:
        if device not in mon().devices:
            raise HTTPException(404, "Unknown plug")
        try:
            return await asyncio.to_thread(
                history.history, app.state.db, cfg, eff_now(), device, from_, to, points,
                mon().pending_for(device), app.state.tariff,
            )
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    # ---- devices -------------------------------------------------------------------

    @app.post("/api/devices/{device_id}/switch")
    async def switch(device_id: int, body: SwitchBody) -> dict:
        if device_id not in mon().devices:
            raise HTTPException(404, "Unknown plug")
        try:
            live = await mon().switch(device_id, body.on)
        except ShellyError as e:
            raise HTTPException(502, f"The plug did not respond: {e}") from e
        return live.to_dict(device_id, time.monotonic())

    @app.patch("/api/devices/{device_id}")
    async def patch_device(device_id: int, body: DevicePatch) -> dict:
        if device_id not in mon().devices:
            raise HTTPException(404, "Unknown plug")
        fields = {k: v for k, v in body.model_dump().items() if v is not None}
        dev = await asyncio.to_thread(app.state.db.update_device, device_id, **fields)
        mon().update_device(dev)
        return dev

    @app.delete("/api/devices/{device_id}")
    async def delete_device(device_id: int, purge: bool = False) -> dict:
        dev = mon().devices.get(device_id)
        if dev is None:
            raise HTTPException(404, "Unknown plug")
        await mon().remove_device(device_id)
        await asyncio.to_thread(app.state.db.delete_device, device_id, purge)
        await asyncio.to_thread(leases.remove_reservation, cfg.hosts_dir, dev["mac"])
        return {"removed": device_id, "purged": purge}

    # ---- add-plug wizard ---------------------------------------------------------------

    @app.get("/api/wizard/provision")
    def wizard_provision() -> dict:
        w: Wizard = app.state.wizard
        return {
            "ssid": cfg.ap_ssid, "password": cfg.ap_password, "plug_ap_ip": cfg.plug_ap_ip,
            "provision_url": w.provision_url(), "reboot_url": w.reboot_url(),
        }

    @app.get("/api/wizard/qr.svg")
    def wizard_qr(
        kind: Literal["provision", "reboot", "page", "plugwifi", "apwifi", "dashboard"] = "provision", ssid: str = "",
    ) -> Response:
        w: Wizard = app.state.wizard
        if kind == "plugwifi":
            if not is_shelly_ssid(ssid):
                raise HTTPException(400, "That isn't the name of a Shelly's Wi-Fi network.")
            content = wifi_qr_text(ssid)
        elif kind == "apwifi":
            content = wifi_qr_text(cfg.ap_ssid, cfg.ap_password)
        elif kind == "dashboard":
            content = f"http://{cfg.ap_ip}/"
        else:
            content = {
                "provision": w.provision_url(), "reboot": w.reboot_url(),
                "page": f"http://{cfg.plug_ap_ip}/",
            }[kind]
        buf = io.BytesIO()
        segno.make(content, error="m").save(
            buf, kind="svg", scale=6, border=2, dark="#000000", light="#ffffff",
        )
        return Response(buf.getvalue(), media_type="image/svg+xml",
                        headers={"Cache-Control": "no-store"})

    @app.get("/api/wizard/plug-wifi")
    async def wizard_plug_wifi() -> dict:
        """Which Shelly setup networks can the Pi hear, strongest first."""
        try:
            return await app.state.plugwifi.scan()
        except BridgeError as e:
            raise HTTPException(504, str(e)) from e

    @app.get("/api/wizard/scan")
    async def wizard_scan() -> dict:
        return {"candidates": await app.state.wizard.scan()}

    @app.get("/api/wizard/probe")
    async def wizard_probe(host: str) -> dict:
        if not HOST_RE.match(host):
            raise HTTPException(400, "Enter the address as numbers, like 192.168.50.101.")
        try:
            return await app.state.wizard.probe(host)
        except ShellyError as e:
            raise HTTPException(404, f"No Shelly answered at {host}: {e}") from e

    @app.post("/api/wizard/adopt")
    async def wizard_adopt(body: AdoptBody) -> dict:
        job = app.state.wizard.start_adopt(AdoptRequest(**body.model_dump()))
        return job.to_dict()

    @app.get("/api/wizard/jobs/{job_id}")
    def wizard_job(job_id: str) -> dict:
        job = app.state.wizard.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "Unknown job")
        return job.to_dict()

    # ---- electricity price ---------------------------------------------------------------------

    def tariff_full() -> dict:
        t: tariff_mod.Tariff = app.state.tariff
        view = tariff_view(eff_now())
        view["week"] = t.week()
        return view

    @app.get("/api/tariff")
    def get_tariff() -> dict:
        return tariff_full()

    @app.put("/api/tariff")
    async def put_tariff(body: TariffBody) -> dict:
        try:
            t = tariff_mod.Tariff.from_dict(body.model_dump())
        except tariff_mod.TariffError as e:
            raise HTTPException(422, str(e)) from e
        await asyncio.to_thread(tariff_mod.save, app.state.db, t)
        app.state.tariff = t
        mon().publish({"type": "devices"})  # open dashboards reload their numbers
        return tariff_full()

    # ---- HTTPS ----------------------------------------------------------------------------------------

    def https() -> HttpsManager:
        m: HttpsManager = app.state.https
        if not m.available:
            raise HTTPException(404, "HTTPS is switched off on this Pi (https_port is 0).")
        return m

    @app.get("/api/https")
    def get_https() -> dict:
        return app.state.https.status()

    @app.put("/api/https")
    async def put_https(body: HttpsBody) -> dict:
        m = https()
        if body.https_only and not (body.enabled and m.front.running):
            raise HTTPException(
                409, "HTTPS isn't running yet, so plain HTTP can't be switched off. "
                     "Wait until it shows as running, then try again.")
        await asyncio.to_thread(m.save_settings, body.enabled, body.https_only)
        await m.reconcile()
        return m.status()

    @app.post("/api/https/reset-ca")
    async def reset_ca(body: ResetCaBody) -> dict:
        """Start over with a new certificate authority. Every device must install the new one."""
        m = https()
        if not body.confirm:
            raise HTTPException(422, "Confirm that you want a new certificate authority.")
        if not app.state.clock.trusted:
            raise HTTPException(409, "The clock isn't set, so a certificate can't be made yet. Set it first.")
        await asyncio.to_thread(m.save_settings, m.settings()["enabled"], False)  # don't lock anyone out meanwhile
        await m.reconcile(force_new_ca=True)
        if m.last_error:
            raise HTTPException(500, m.last_error)
        return m.status()

    @app.get("/api/https/ca.crt")
    def ca_certificate() -> Response:
        m = https()
        if not m.store.has_ca():
            raise HTTPException(404, "There is no certificate authority yet.")
        return Response(
            m.store.ca_pem(), media_type="application/x-x509-ca-cert",
            headers={"Content-Disposition": f'attachment; filename="shelly-pi-ca-{m.hostname}.crt"'},
        )

    # ---- backup and restore ----------------------------------------------------------------------

    @app.get("/api/backup")
    async def export_backup(tz: str | None = None) -> FileResponse:
        """Everything the Pi stores, as one zip named shelly-pi_yyyy-mm-dd_HH-MM-ss.zip.
        The time in the name is the Pi's clock in the caller's time zone (?tz=Europe/Rome)."""
        zone = backup.zone_or(tz, app.state.tariff.timezone)
        async with app.state.admin_lock:
            try:
                await mon().flush()  # so the newest readings are in the file
            except Exception:  # noqa: BLE001 - a failed save shouldn't stop a backup of what is stored
                log.exception("flush before backup failed")
            now = eff_now()
            tmp = Path(tempfile.mkdtemp(prefix=".backup-", dir=cfg.data_dir))
            dest = tmp / "backup.zip"
            try:
                await asyncio.to_thread(
                    backup.build_archive, app.state.db, cfg, dest, now=now, tz=zone,
                    app_version=__version__, held_samples=len(mon().buffer),
                    clock_trusted=app.state.clock.trusted,
                )
            except Exception:
                shutil.rmtree(tmp, ignore_errors=True)
                log.exception("backup failed")
                raise HTTPException(500, "The backup couldn't be made. See the log on the Pi.") from None
        return FileResponse(
            dest, media_type="application/zip", filename=backup.archive_name(now, zone),
            headers={"Cache-Control": "no-store"},
            background=BackgroundTask(shutil.rmtree, tmp, ignore_errors=True),
        )

    # ---- automatic backups to a USB drive ---------------------------------------------------------------

    @app.get("/api/autobackup")
    def get_autobackup() -> dict:
        return app.state.autobackup.status()

    @app.get("/api/autobackup/drives")
    async def autobackup_drives() -> dict:
        """Ask the Pi which USB drives are plugged in right now."""
        try:
            return {"drives": await app.state.autobackup.list_drives()}
        except BridgeError as e:
            raise HTTPException(504, str(e)) from e

    @app.put("/api/autobackup")
    async def put_autobackup(body: AutoBackupBody) -> dict:
        ab: AutoBackup = app.state.autobackup
        data = body.model_dump()
        drive_id = data.pop("drive_id")
        try:
            await asyncio.to_thread(ab.update, data, drive_id)
        except PlanError as e:
            raise HTTPException(422, str(e)) from e
        return ab.status()

    @app.post("/api/autobackup/run")
    async def autobackup_run() -> dict:
        """Back up to the chosen drive right now."""
        ab: AutoBackup = app.state.autobackup
        try:
            res = await ab.run_once()
        except BridgeError as e:
            raise HTTPException(409, str(e)) from e
        return {"result": res, **ab.status()}

    @app.post("/api/restore")
    async def restore_backup(request: Request) -> dict:
        """Replace the stored data with a backup. The archive is the request body."""
        try:
            if int(request.headers.get("content-length", "0")) > backup.MAX_ARCHIVE_BYTES:
                raise HTTPException(413, "That file is too large to be a backup.")
        except ValueError:
            raise HTTPException(400, "Bad Content-Length.") from None
        if app.state.admin_lock.locked():
            raise HTTPException(409, "A backup or restore is already running. Try again in a moment.")
        async with app.state.admin_lock:
            work = cfg.data_dir / ".restore"
            shutil.rmtree(work, ignore_errors=True)
            work.mkdir(parents=True)
            try:
                upload = work / "upload.zip"
                size = 0
                with open(upload, "wb") as f:
                    async for chunk in request.stream():
                        size += len(chunk)
                        if size > backup.MAX_ARCHIVE_BYTES:
                            raise HTTPException(413, "That file is too large to be a backup.")
                        await asyncio.to_thread(f.write, chunk)
                try:
                    manifest = await asyncio.to_thread(backup.read_archive, upload, work / "files")
                except backup.BackupError as e:
                    raise HTTPException(422, str(e)) from e
                upload.unlink()

                # From here the old data is replaced; apply_files puts it back if that fails.
                await stop_services(app)
                try:
                    taken = await asyncio.to_thread(backup.apply_files, cfg, work / "files")
                    backup.apply_to_settings(cfg, taken)
                except Exception:
                    log.exception("restore failed; the previous data was put back")
                    await start_services(app)
                    raise HTTPException(
                        500, "The restore failed and nothing was changed. See the log on the Pi.") from None
                await start_services(app)
                app.state.https.forget_cache()
                log.info("restored backup from %s (%s)", manifest.get("created_local"), manifest.get("app_version"))
                return {"ok": True, "manifest": manifest, "settings_restored": taken,
                        "previous_data_kept_in": backup.ASIDE}
            finally:
                shutil.rmtree(work, ignore_errors=True)

    # ---- updating the app ----------------------------------------------------------------------

    MAX_UPDATE_BYTES = 600 * 1024 * 1024

    def update_status() -> dict | None:
        try:
            data = json.loads((cfg.bridge_dir / "update.res.json").read_text())
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    @app.get("/api/update")
    async def update_info() -> dict:
        return {"version": __version__, "status": await asyncio.to_thread(update_status),
                "max_bytes": MAX_UPDATE_BYTES}

    @app.post("/api/update")
    async def update_upload(request: Request, sha256: str = "") -> dict:
        """Take a new app image (the request body, a `podman save` tar) and hand it to the Pi's update helper."""
        want = sha256.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", want):
            raise HTTPException(400, "Give the image's SHA-256 checksum (64 hex digits).")
        try:
            declared = int(request.headers.get("content-length", "0"))
        except ValueError:
            raise HTTPException(400, "Bad Content-Length.") from None
        if declared > MAX_UPDATE_BYTES:
            raise HTTPException(413, "That file is too large to be an app image.")
        if app.state.admin_lock.locked():
            raise HTTPException(409, "A backup, restore or update is already running. Try again in a moment.")
        async with app.state.admin_lock:
            st = await asyncio.to_thread(update_status)
            req = cfg.bridge_dir / "update.req"
            if req.exists() or (st and st.get("state") in ("loading", "restarting")
                                and time.time() - (cfg.bridge_dir / "update.res.json").stat().st_mtime < 900):
                raise HTTPException(409, "An update is already being installed.")
            cfg.bridge_dir.mkdir(parents=True, exist_ok=True)
            free = shutil.disk_usage(cfg.bridge_dir).free
            if free < max(declared, 1) * 2 + 200 * 1024 * 1024:
                raise HTTPException(507, "There isn't enough free space on the Pi for this update.")
            part, final = cfg.bridge_dir / "update.tar.part", cfg.bridge_dir / "update.tar"
            h, size = hashlib.sha256(), 0
            try:
                with open(part, "wb") as f:
                    async for chunk in request.stream():
                        size += len(chunk)
                        if size > MAX_UPDATE_BYTES:
                            raise HTTPException(413, "That file is too large to be an app image.")
                        h.update(chunk)
                        await asyncio.to_thread(f.write, chunk)
                if size == 0:
                    raise HTTPException(400, "The upload was empty.")
                if h.hexdigest() != want:
                    raise HTTPException(
                        422, "The file doesn't match the checksum, so it is damaged or not the release you picked. "
                             "Nothing was changed.")
                os.replace(part, final)
            finally:
                part.unlink(missing_ok=True)
            rid = secrets.token_hex(6)
            tmp = cfg.bridge_dir / ".update.tmp"
            tmp.write_text(f"{rid} {want}\n")
            (cfg.bridge_dir / "update.res.json").unlink(missing_ok=True)
            os.replace(tmp, req)
            log.info("app update of %d bytes handed to the Pi's helper", size)
            return {"ok": True, "id": rid, "bytes": size}

    # ---- system ------------------------------------------------------------------------------

    @app.get("/api/system")
    async def system() -> dict:
        db: Database = app.state.db
        counts = await asyncio.to_thread(db.counts)
        return {
            "version": __version__, "clock": app.state.clock.status(),
            "ap": {"ssid": cfg.ap_ssid, "ip": cfg.ap_ip, "password": cfg.ap_password},
            "db": {"bytes": db.file_size(), **counts},
            "held_samples": len(mon().buffer),
            "last_maintenance": mon().last_maintenance,
            "last_flush_error": mon().last_flush_error,
            "retention": {"raw_days": cfg.raw_retention_days, "r1m_days": cfg.r1m_retention_days},
            "flush_interval": cfg.flush_interval,
        }

    @app.post("/api/system/time")
    async def set_time(body: TimeBody) -> dict:
        """Ask the host to set its clock (and the RTC). The container cannot do it
        itself, so it drops a request file that a host systemd path unit acts on."""
        if not (cfg.min_valid_epoch <= body.epoch <= MAX_EPOCH):
            raise HTTPException(400, "That time is outside the supported range.")
        clock: ClockGuard = app.state.clock
        cfg.bridge_dir.mkdir(parents=True, exist_ok=True)
        tmp, req = cfg.bridge_dir / ".settime.tmp", cfg.bridge_dir / "settime.req"
        await asyncio.to_thread(tmp.write_text, str(body.epoch))
        await asyncio.to_thread(os.replace, tmp, req)
        t_start = time.monotonic()
        deadline = t_start + 15
        while time.monotonic() < deadline:
            await asyncio.sleep(0.5)
            expected = body.epoch + (time.monotonic() - t_start)
            if abs(clock.now() - expected) < 5:
                await asyncio.to_thread(clock.confirm_now)
                await mon().flush()
                mon().publish({"type": "clock"})
                return {"ok": True, "clock": clock.status()}
        try:
            req.unlink()
        except FileNotFoundError:
            pass
        raise HTTPException(
            504,
            "The Pi did not apply the new time. Check that shelly-settime.path is "
            "active on the Pi (see the README).",
        )

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):  # noqa: ARG001
        log.exception("unhandled error on %s", request.url.path)
        return JSONResponse({"detail": "Internal error"}, status_code=500)

    return app
