"""Asking the Pi itself (not the container) to do something, through files in the shared folder.

The app runs in a container and can't scan Wi-Fi or mount drives. A root script on the Pi,
started by a systemd path unit when <name>.req appears, does the job and writes <name>.res.json.
The first word of the request is an id, so an answer can be matched to the question.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from pathlib import Path


class BridgeError(RuntimeError):
    """The Pi's helper didn't do what was asked; the message is meant for the person."""


class HostBridge:
    def __init__(self, folder: Path, name: str, unit: str | None = None):
        self.folder, self.name = folder, name
        self.unit = unit or f"shelly-{name}.path"
        self.req, self.res = folder / f"{name}.req", folder / f"{name}.res.json"

    async def ask(self, command: str, timeout: float) -> dict:
        rid = secrets.token_hex(6)
        self.folder.mkdir(parents=True, exist_ok=True)
        tmp = self.folder / f".{self.name}.tmp"
        await asyncio.to_thread(tmp.write_text, f"{rid} {command}\n")
        for attempt in range(10):   # Windows refuses the swap while the other side has the file open
            try:
                await asyncio.to_thread(os.replace, tmp, self.req)
                break
            except PermissionError:
                if attempt == 9:
                    raise
                await asyncio.sleep(0.1)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(0.3)
            try:
                data = json.loads(await asyncio.to_thread(self.res.read_text))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and data.get("id") == rid:
                return data
        try:
            self.req.unlink()
        except FileNotFoundError:
            pass
        raise BridgeError(
            "The Pi didn't answer. Its helper may not be installed: run setup-host.sh on the Pi "
            f"again (see the README), or check that {self.unit} is active.")
