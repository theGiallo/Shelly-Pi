"""Minimal async client for the Shelly Gen2+ local RPC API (Plug M Gen3 etc).

Docs: https://shelly-api-docs.shelly.cloud/gen2/
  POST http://<ip>/rpc  {"id":1,"method":"Switch.Set","params":{"id":0,"on":true}}
  GET  http://<ip>/shelly                (device info, never needs auth)

`ip` may also be "host:port" (used by the tests' simulated plugs).
Devices with HTTP auth enabled are not supported yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


class ShellyError(Exception):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


@dataclass
class Sample:
    output: bool | None = None
    power_w: float | None = None
    voltage: float | None = None
    current: float | None = None
    freq: float | None = None
    temp_c: float | None = None
    energy_wh: float | None = None
    rssi: int | None = None


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_status(status: dict, switch_id: int = 0) -> Sample:
    """Pull our fields out of a Shelly.GetStatus result.

    The metering fields normally live inside `switch:<id>`; some models report
    them in a separate `pm1:<id>` component, so fall back to that.
    """
    sw = status.get(f"switch:{switch_id}")
    pm = status.get(f"pm1:{switch_id}") or {}
    if not isinstance(sw, dict):
        if not pm:
            raise ShellyError(f"no switch:{switch_id} component in status")
        sw = {}

    def pick(key: str) -> Any:
        return sw[key] if key in sw else pm.get(key)

    aenergy = pick("aenergy") or {}
    temperature = sw.get("temperature") or pm.get("temperature") or {}
    wifi = status.get("wifi") or {}
    out = sw.get("output")
    return Sample(
        output=bool(out) if out is not None else None,
        power_w=_num(pick("apower")),
        voltage=_num(pick("voltage")),
        current=_num(pick("current")),
        freq=_num(pick("freq")),
        temp_c=_num(temperature.get("tC") if isinstance(temperature, dict) else None),
        energy_wh=_num(aenergy.get("total") if isinstance(aenergy, dict) else None),
        rssi=int(wifi["rssi"]) if isinstance(wifi.get("rssi"), (int, float)) else None,
    )


class ShellyClient:
    def __init__(self, http: httpx.AsyncClient, timeout: float = 4.0):
        self.http = http
        self.timeout = timeout

    @staticmethod
    def _base(host: str) -> str:
        return f"http://{host}"

    async def rpc(
        self, host: str, method: str, params: dict | None = None, timeout: float | None = None,
    ) -> Any:
        try:
            r = await self.http.post(
                f"{self._base(host)}/rpc",
                json={"id": 1, "method": method, "params": params or {}},
                timeout=timeout or self.timeout,
            )
        except httpx.TimeoutException as e:
            raise ShellyError("timeout") from e
        except httpx.HTTPError as e:
            raise ShellyError(str(e) or e.__class__.__name__) from e
        try:
            body = r.json()
        except ValueError as e:
            raise ShellyError(f"HTTP {r.status_code}: not JSON") from e
        if isinstance(body, dict) and body.get("error"):
            err = body["error"]
            if isinstance(err, dict):
                raise ShellyError(str(err.get("message", err)), err.get("code"))
            raise ShellyError(str(err))
        if r.status_code >= 400:
            raise ShellyError(f"HTTP {r.status_code}", r.status_code)
        if isinstance(body, dict) and "result" in body:
            return body["result"]
        return body

    async def device_info(self, host: str, timeout: float | None = None) -> dict:
        try:
            r = await self.http.get(f"{self._base(host)}/shelly", timeout=timeout or self.timeout)
            data = r.json()
        except httpx.TimeoutException as e:
            raise ShellyError("timeout") from e
        except (httpx.HTTPError, ValueError) as e:
            raise ShellyError(str(e) or e.__class__.__name__) from e
        if not isinstance(data, dict) or "mac" not in data:
            raise ShellyError("not a Shelly device")
        return data

    # ---- readings and control -------------------------------------------

    async def status(self, host: str, switch_id: int = 0) -> Sample:
        result = await self.rpc(host, "Shelly.GetStatus")
        if not isinstance(result, dict):
            raise ShellyError("unexpected status payload")
        return parse_status(result, switch_id)

    async def switch_set(self, host: str, switch_id: int, on: bool) -> None:
        await self.rpc(host, "Switch.Set", {"id": switch_id, "on": on})

    async def switch_toggle(self, host: str, switch_id: int) -> None:
        await self.rpc(host, "Switch.Toggle", {"id": switch_id})

    # ---- one-time setup ---------------------------------------------------

    async def cloud_disable(self, host: str) -> bool:
        r = await self.rpc(host, "Cloud.SetConfig", {"config": {"enable": False}})
        return bool(isinstance(r, dict) and r.get("restart_required"))

    async def switch_configure(
        self, host: str, switch_id: int, name: str, initial_state: str,
    ) -> bool:
        r = await self.rpc(
            host, "Switch.SetConfig",
            {"id": switch_id, "config": {"name": name, "initial_state": initial_state}},
        )
        return bool(isinstance(r, dict) and r.get("restart_required"))

    async def sys_set_name(self, host: str, name: str) -> bool:
        r = await self.rpc(host, "Sys.SetConfig", {"config": {"device": {"name": name}}})
        return bool(isinstance(r, dict) and r.get("restart_required"))

    async def ap_disable(self, host: str) -> bool:
        r = await self.rpc(host, "WiFi.SetConfig", {"config": {"ap": {"enable": False}}})
        return bool(isinstance(r, dict) and r.get("restart_required"))

    async def reboot(self, host: str) -> None:
        await self.rpc(host, "Shelly.Reboot", timeout=3.0)
