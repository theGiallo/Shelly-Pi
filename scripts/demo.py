#!/usr/bin/env python3
"""Preview the dashboard on your own computer: simulated plugs, fake history.

    pip install -r requirements.txt
    python scripts/demo.py            # then open http://127.0.0.1:8080

Nothing here touches the network beyond localhost. Data goes in ./demo-data
(delete the folder for a fresh start). The simulated plugs live on ports
8101-8103; open http://127.0.0.1:8101/__test?load=300 to change one's load.
"""

from __future__ import annotations

import argparse
import math
import random
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import uvicorn  # noqa: E402

from app import rollup  # noqa: E402
from app.api import create_app  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import Database  # noqa: E402
from fake_shelly import FakePlug, ServerThread  # noqa: E402

DEMO = [
    # name, room, mac, port, typical load, starts switched on
    ("Desk lamp", "Office", "AABBCCDD0001", 8101, 38.0, True),
    ("Fridge", "Kitchen", "AABBCCDD0002", 8102, 95.0, True),
    ("Gaming PC", "Living room", "AABBCCDD0003", 8103, 310.0, False),
]


def power_at(kind: str, t: float, rng: random.Random) -> float:
    lt = time.localtime(t)
    hour = lt.tm_hour + lt.tm_min / 60
    if kind == "Desk lamp":
        return 38.0 + rng.uniform(-0.5, 0.5) if 8 <= hour < 23 else 0.0
    if kind == "Fridge":
        return 95.0 + rng.uniform(-3, 3) if (t // 60) % 55 < 18 else 0.0
    evening = 18 <= hour < 23.5 or (lt.tm_wday >= 5 and 11 <= hour < 18)
    return 180 + 160 * abs(math.sin(t / 900)) + rng.uniform(-15, 15) if evening else 0.0


def make_history(db: Database, now: int, days: int = 3) -> None:
    rng = random.Random(7)
    start = (now - days * 86400) // 60 * 60
    for dev in db.list_devices():
        rows = []
        for t in range(start, now - 300, 60):
            p = power_at(dev["name"], t, rng)
            rows.append((dev["id"], t, p, 230.0 + rng.uniform(-2, 2), p / 230.0, 30.0 + p / 40,
                         None, p * 60 / 3600, 1 if p > 0 else 0))
        db.insert_readings(rows)
    rollup.run(db, now=now, lag=420)
    db.set_meta("last_good_epoch", now)


def fake_wifi_scanner(bridge: Path, how_many: int) -> None:
    """Stands in for the Pi's scan helper, so the Add plug page has something to find.
    Answers each scan request with `how_many` pretend plug networks plus a couple of ordinary ones."""
    import json
    import threading

    plugs = [("ShellyPlugMG3-A1B2C3", -58), ("ShellyPlugMG3-5F6E7D", -64), ("ShellyPlugMG3-DD0002", -71)]

    def answer(rid: str, rng: random.Random) -> None:
        nets = [
            {"ssid": "ShellyPi", "bssid": "aa:bb:cc:00:00:01", "signal": -40, "channel": 6, "secured": True},
            {"ssid": "Neighbour", "bssid": "11:22:33:44:55:66", "signal": -66, "channel": 11, "secured": True},
        ] + [
            {"ssid": n, "bssid": f"34:94:54:00:00:{i:02x}", "signal": dbm + rng.randint(-3, 3), "channel": 1 + i, "secured": False}
            for i, (n, dbm) in enumerate(plugs[:how_many])
        ]
        time.sleep(1.5)   # a real scan takes a few seconds
        tmp, res = bridge / ".wifiscan.tmp", bridge / "wifiscan.res.json"
        tmp.write_text(json.dumps({"id": rid, "ok": True, "iface": "wlan0", "networks": nets}))
        for _ in range(20):   # Windows refuses the swap while the app is reading the old answer
            try:
                tmp.replace(res)
                return
            except PermissionError:
                time.sleep(0.1)

    def run() -> None:
        rng = random.Random(3)
        req = bridge / "wifiscan.req"
        while True:
            time.sleep(0.2)
            try:
                rid = req.read_text().split()[0]
                req.unlink()
                answer(rid, rng)
                print(f"(demo) pretended to scan Wi-Fi: {how_many} plug network(s)")
            except (OSError, IndexError):
                continue
            except Exception as e:   # never let the pretend scanner die quietly
                print(f"(demo) pretend Wi-Fi scan failed: {e!r}")

    bridge.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=run, daemon=True).start()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--fresh", action="store_true", help="delete ./demo-data first")
    ap.add_argument("--aps", type=int, default=2, choices=[0, 1, 2, 3],
                    help="how many pretend plug Wi-Fi networks the Add plug page finds (default 2)")
    args = ap.parse_args()

    data = Path("demo-data")
    if args.fresh and data.exists():
        shutil.rmtree(data)
    cfg = Settings(
        data_dir=data.resolve(), host="127.0.0.1", port=args.port, password="",
        ap_ssid="ShellyPi", ap_password="demo-password-123", https_port=args.port + 363, hostname="shellypi", poll_interval=2.0, flush_interval=20.0,
    )

    servers = []
    for name, _room, mac, port, load, on in DEMO:
        plug = FakePlug(mac=mac, load_w=load)
        plug.output = on
        servers.append(ServerThread(plug.asgi(), port=port).start())

    first_run = not cfg.db_path.exists()
    db = Database(cfg.db_path)
    if first_run:
        now = int(time.time())
        for name, room, mac, port, _l, _o in DEMO:
            db.add_device(mac=":".join(mac[i:i + 2] for i in range(0, 12, 2)).lower(), name=name,
                          room=room, ip=f"127.0.0.1:{port}", model="PlugMG3", added_ts=now - 3 * 86400)
        make_history(db, now)
        print("Created 3 days of fake history.")
    db.close()

    fake_wifi_scanner(cfg.bridge_dir, args.aps)
    print(f"\nShelly Monitor demo: http://127.0.0.1:{args.port}\n")
    # Open browser tabs hold a live-update stream; without a time limit Ctrl+C would wait for them forever.
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, log_level="warning", timeout_graceful_shutdown=2)


if __name__ == "__main__":
    main()
