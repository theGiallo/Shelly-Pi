from __future__ import annotations

import threading
import time
from pathlib import Path

import httpx
import pytest

from app.api import create_app
from app.config import Settings
from fake_shelly import FakePlug, ServerThread

NOW_2026 = 1_792_000_000  # a plausible "now" in late 2026 for synthetic data


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data", poll_interval=0.25, poll_timeout=1.5,
        flush_interval=1.0, maintenance_interval=3600, password="",
        https_port=0, ap_ssid="TestNet", ap_password="p@ss w/rd&1", ap_ip="192.168.50.1",
    )


@pytest.fixture
def plugs():
    """Factory for simulated plugs; stopped at teardown."""
    started: list[ServerThread] = []

    def make(**kw) -> tuple[FakePlug, ServerThread]:
        plug = FakePlug(**kw)
        srv = ServerThread(plug.asgi()).start()
        started.append(srv)
        return plug, srv

    yield make
    for s in started:
        s.stop()


class AppHandle:
    def __init__(self, app, srv: ServerThread, clock_offset: dict):
        self.app, self.srv, self.clock_offset = app, srv, clock_offset
        self.base = f"http://127.0.0.1:{srv.port}"
        self.client = httpx.Client(base_url=self.base, timeout=10, headers={"X-Requested-With": "tests"})

    def wait_for(self, fn, timeout: float = 10.0, interval: float = 0.1):
        end = time.time() + timeout
        last = None
        while time.time() < end:
            last = fn()
            if last:
                return last
            time.sleep(interval)
        raise AssertionError(f"condition not met within {timeout}s (last={last!r})")


@pytest.fixture
def run_app(settings):
    """Start the real app under uvicorn with an adjustable clock."""
    handles: list[AppHandle] = []

    def start(**overrides) -> AppHandle:
        for k, v in overrides.items():
            setattr(settings, k, v)
        offset = {"v": 0.0}
        app = create_app(settings, time_fn=lambda: time.time() + offset["v"])
        srv = ServerThread(app).start()
        h = AppHandle(app, srv, offset)
        handles.append(h)
        return h

    yield start
    for h in handles:
        h.client.close()
        h.srv.stop()
