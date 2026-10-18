"""Runtime settings.

Order of precedence: environment (SM_*) > <data_dir>/config.json > defaults.
Secrets (admin password, AP password) live in config.json, written by
`python -m app.cli init-config` during host setup.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

# Anything earlier than this cannot be a valid "now" for this project
# (2026-01-01T00:00:00Z). A Pi that booted with a dead RTC lands before it.
MIN_VALID_EPOCH = 1_767_225_600


@dataclass
class Settings:
    data_dir: Path = Path("/data")
    host: str = "0.0.0.0"
    port: int = 80

    # HTTPS on this port, in addition to plain HTTP on `port`. 0 turns the feature off.
    https_port: int = 443
    # Name used in the certificate (a single label such as "shellypi"). Empty = the machine's host name.
    hostname: str = ""

    # Auth: single shared password (HTTP Basic, user "admin"). Empty = no auth.
    password: str = ""

    # The Wi-Fi network the Pi hosts for the plugs.
    ap_ssid: str = "ShellyPi"
    ap_password: str = ""
    ap_ip: str = "192.168.50.1"
    # Address every Shelly plug uses for its own setup hotspot.
    plug_ap_ip: str = "192.168.33.1"

    poll_interval: float = 10.0
    poll_timeout: float = 4.0
    flush_interval: float = 300.0
    maintenance_interval: float = 600.0

    raw_retention_days: int = 7
    r1m_retention_days: int = 90

    min_valid_epoch: int = MIN_VALID_EPOCH
    clock_tolerance_s: int = 120
    held_samples_max: int = 20000

    @property
    def db_path(self) -> Path:
        return self.data_dir / "monitor.sqlite3"

    @property
    def leases_file(self) -> Path:
        return self.data_dir / "dnsmasq" / "dnsmasq.leases"

    @property
    def hosts_dir(self) -> Path:
        return self.data_dir / "dhcp-hosts.d"

    @property
    def bridge_dir(self) -> Path:
        return self.data_dir / "bridge"


def _coerce(value: str, template: Any) -> Any:
    if isinstance(template, bool):
        return value.lower() in ("1", "true", "yes", "on")
    if isinstance(template, int):
        return int(value)
    if isinstance(template, float):
        return float(value)
    if isinstance(template, Path):
        return Path(value)
    return value


def load_settings(env: dict[str, str] | None = None) -> Settings:
    env = dict(os.environ if env is None else env)
    s = Settings()
    s.data_dir = Path(env.get("SM_DATA_DIR", str(s.data_dir)))

    cfg_file = s.data_dir / "config.json"
    if cfg_file.is_file():
        data = json.loads(cfg_file.read_text(encoding="utf-8"))
        for f in fields(Settings):
            if f.name in data and f.name != "data_dir":
                setattr(s, f.name, _coerce(str(data[f.name]), getattr(s, f.name)))

    for f in fields(Settings):
        key = "SM_" + f.name.upper()
        if key in env and f.name != "data_dir":
            setattr(s, f.name, _coerce(env[key], getattr(s, f.name)))
    return s
