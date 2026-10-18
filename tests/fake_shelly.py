"""A simulated Shelly Gen3 plug (Plug M style) for tests and local demos.

Implements the small part of the Gen2+ RPC API the app uses. The response
shapes follow Shelly's public docs; real firmware may differ slightly, which
is why the app parses defensively.

Run a demo plug:  python tests/fake_shelly.py --port 8101 --load 120
"""

from __future__ import annotations

import argparse
import random
import threading
import time
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route


class FakePlug:
    def __init__(
        self, mac: str = "AABBCCDDEE01", load_w: float = 60.0, gen: int = 3,
        auth_en: bool = False, speed: float = 1.0, with_power: bool = True,
    ):
        self.mac = mac
        self.load_w = load_w
        self.gen = gen
        self.auth_en = auth_en
        self.speed = speed          # >1 makes the energy counter move faster
        self.with_power = with_power
        self.output = False
        self.counter_wh = 0.0
        self.t_last = time.monotonic()
        self.fail = False           # simulate an unresponsive plug
        self.down_until = 0.0       # simulate a reboot
        self.reboots = 0
        self.config: dict[str, Any] = {
            "cloud": True, "name": None, "initial_state": "match_input",
            "device_name": None, "ap_enable": True,
        }
        self.calls: list[str] = []
        self.lock = threading.Lock()

    # ---- model -----------------------------------------------------------------

    def _tick(self) -> None:
        now = time.monotonic()
        if self.output:
            self.counter_wh += self.load_w * (now - self.t_last) * self.speed / 3600.0
        self.t_last = now

    def power(self) -> float:
        return round(self.load_w + random.uniform(-0.4, 0.4), 1) if self.output else 0.0

    def info(self) -> dict:
        return {
            "name": self.config["device_name"], "id": f"shellyplugmg3-{self.mac}",
            "mac": self.mac, "model": "S3PL-00112EU", "gen": self.gen,
            "fw_id": "20250101-000000/1.4.4-fake", "ver": "1.4.4", "app": "PlugMG3",
            "auth_en": self.auth_en, "auth_domain": None,
        }

    def status(self) -> dict:
        self._tick()
        sw: dict[str, Any] = {"id": 0, "source": "HTTP", "output": self.output}
        if self.with_power:
            p = self.power()
            sw.update({
                "apower": p, "voltage": 230.4, "freq": 50.0,
                "current": round(p / 230.4, 3),
                "aenergy": {"total": round(self.counter_wh, 3)},
                "temperature": {"tC": 31.5, "tF": 88.7},
            })
        return {
            "switch:0": sw,
            "wifi": {"sta_ip": "192.168.50.101", "status": "got ip", "ssid": "ShellyPi", "rssi": -57},
            "cloud": {"connected": False}, "sys": {"uptime": 1234},
        }

    # ---- RPC ---------------------------------------------------------------------

    def call(self, method: str, params: dict) -> Any:
        with self.lock:
            self.calls.append(method)
            if method in ("Shelly.GetStatus",):
                return self.status()
            if method == "Shelly.GetDeviceInfo":
                return self.info()
            if method == "Switch.GetStatus":
                return self.status()["switch:0"]
            if method == "Switch.Set":
                self._tick()
                self.output = bool(params.get("on"))
                return {"was_on": not self.output}
            if method == "Switch.Toggle":
                self._tick()
                self.output = not self.output
                return {"was_on": not self.output}
            if method == "Switch.SetConfig":
                cfg = params.get("config", {})
                self.config["name"] = cfg.get("name", self.config["name"])
                self.config["initial_state"] = cfg.get("initial_state", self.config["initial_state"])
                return {"restart_required": True}
            if method == "Cloud.SetConfig":
                self.config["cloud"] = bool(params.get("config", {}).get("enable"))
                return {"restart_required": True}
            if method == "Sys.SetConfig":
                dev = params.get("config", {}).get("device", {})
                self.config["device_name"] = dev.get("name", self.config["device_name"])
                return {"restart_required": False}
            if method == "WiFi.SetConfig":
                ap = params.get("config", {}).get("ap", {})
                if "enable" in ap:
                    self.config["ap_enable"] = bool(ap["enable"])
                return {"restart_required": True}
            if method == "Shelly.Reboot":
                self.reboots += 1
                self.down_until = time.monotonic() + 1.5
                self._tick()
                init = self.config["initial_state"]
                self.output = {"on": True, "off": False}.get(init, self.output)
                return None
            raise KeyError(method)

    def asgi(self) -> Starlette:
        plug = self

        def down() -> bool:
            return plug.fail or time.monotonic() < plug.down_until

        async def shelly_info(request: Request):
            if down():
                return PlainTextResponse("unavailable", status_code=503)
            return JSONResponse(plug.info())

        async def rpc_post(request: Request):
            if down():
                return PlainTextResponse("unavailable", status_code=503)
            body = await request.json()
            return _rpc(body.get("method", ""), body.get("params") or {}, body.get("id", 1))

        async def rpc_get(request: Request):
            if down():
                return PlainTextResponse("unavailable", status_code=503)
            params = {k: v for k, v in request.query_params.items()}
            return _rpc(request.path_params["method"], params, 1)

        def _rpc(method: str, params: dict, rid: Any):
            try:
                result = plug.call(method, params)
            except KeyError:
                return JSONResponse({"id": rid, "error": {"code": 404, "message": f"No handler for {method}"}})
            return JSONResponse({"id": rid, "src": f"shellyplugmg3-{plug.mac}", "result": result})

        async def control(request: Request):
            """Test hook: /__test?load=120&fail=1&reset=1"""
            q = request.query_params
            with plug.lock:
                if "load" in q:
                    plug._tick()
                    plug.load_w = float(q["load"])
                if "fail" in q:
                    plug.fail = q["fail"] == "1"
                if q.get("reset") == "1":
                    plug._tick()
                    plug.counter_wh = 0.0
            return JSONResponse({"output": plug.output, "counter_wh": plug.counter_wh})

        return Starlette(routes=[
            Route("/shelly", shelly_info),
            Route("/rpc", rpc_post, methods=["POST"]),
            Route("/rpc/{method}", rpc_get, methods=["GET"]),
            Route("/__test", control),
        ])


class ServerThread:
    """Run any ASGI app on 127.0.0.1:<free port> in a background thread."""

    def __init__(self, asgi_app: Any, port: int = 0):
        self.config = uvicorn.Config(asgi_app, host="127.0.0.1", port=port, log_level="warning",
                                     lifespan="auto")
        self.server = uvicorn.Server(self.config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self) -> "ServerThread":
        self.thread.start()
        deadline = time.time() + 15
        while not self.server.started:
            if time.time() > deadline:
                raise RuntimeError("server did not start")
            time.sleep(0.02)
        return self

    @property
    def port(self) -> int:
        return self.server.servers[0].sockets[0].getsockname()[1]

    @property
    def host(self) -> str:
        return f"127.0.0.1:{self.port}"

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8101)
    ap.add_argument("--mac", default="AABBCCDDEE01")
    ap.add_argument("--load", type=float, default=120.0)
    ap.add_argument("--speed", type=float, default=1.0)
    args = ap.parse_args()
    plug = FakePlug(mac=args.mac, load_w=args.load, speed=args.speed)
    uvicorn.run(plug.asgi(), host="127.0.0.1", port=args.port, log_level="info")
