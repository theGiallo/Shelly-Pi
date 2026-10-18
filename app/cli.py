"""Helper commands run by the host setup script inside the container image.

  python -m app.cli init-config --data-dir /data --ap-ssid NAME --ap-ip 192.168.50.1

Secrets come from the environment (SM_PASSWORD, SM_AP_PASSWORD), not arguments,
so they never show up in a process list.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def init_config(args: argparse.Namespace) -> int:
    data = Path(args.data_dir)
    data.mkdir(parents=True, exist_ok=True)
    cfg_file = data / "config.json"
    cfg = json.loads(cfg_file.read_text()) if cfg_file.is_file() else {}
    cfg.update(
        {
            "ap_ssid": args.ap_ssid,
            "ap_ip": args.ap_ip,
            "password": os.environ.get("SM_PASSWORD", cfg.get("password", "")),
            "ap_password": os.environ.get("SM_AP_PASSWORD", cfg.get("ap_password", "")),
        }
    )
    tmp = cfg_file.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, cfg_file)
    print(f"wrote {cfg_file}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="app.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    ic = sub.add_parser("init-config")
    ic.add_argument("--data-dir", default="/data")
    ic.add_argument("--ap-ssid", required=True)
    ic.add_argument("--ap-ip", default="192.168.50.1")
    ic.set_defaults(fn=init_config)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
