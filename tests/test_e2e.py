"""End-to-end: the real app under uvicorn, talking to simulated Shelly plugs."""

from __future__ import annotations

import json
import threading
import time
import urllib.parse

import pytest

from app.shelly import ShellyClient


def adopt(h, host, name="Desk lamp", room="Office", **kw):
    body = {"host": host, "name": name, "room": room, "initial_state": "off", "disable_ap": True, **kw}
    r = h.client.post("/api/wizard/adopt", json=body)
    assert r.status_code == 200, r.text
    job_id = r.json()["id"]

    def finished():
        j = h.client.get(f"/api/wizard/jobs/{job_id}").json()
        return j if j["status"] != "running" else None

    return h.wait_for(finished, timeout=40)


def device(h, dev_id=None):
    devs = h.client.get("/api/state").json()["devices"]
    return devs if dev_id is None else next(d for d in devs if d["id"] == dev_id)


def test_wizard_provisioning_url_and_qr(run_app):
    h = run_app()
    p = h.client.get("/api/wizard/provision").json()
    url = urllib.parse.urlparse(p["provision_url"])
    assert url.netloc == "192.168.33.1" and url.path == "/rpc/WiFi.SetConfig"
    cfg = json.loads(urllib.parse.parse_qs(url.query)["config"][0])
    # special characters in the password survive the URL round trip
    assert cfg == {"sta": {"ssid": "TestNet", "pass": "p@ss w/rd&1", "enable": True}}
    assert p["reboot_url"].endswith("/rpc/Shelly.Reboot")
    for kind in ("provision", "reboot", "page"):
        r = h.client.get(f"/api/wizard/qr.svg?kind={kind}")
        assert r.status_code == 200 and r.headers["content-type"] == "image/svg+xml"
        assert b"<svg" in r.content


def test_full_flow_adopt_poll_switch_history(run_app, plugs):
    plug, srv = plugs(mac="AABBCCDDEE01", load_w=80.0, speed=360.0)
    h = run_app()
    assert h.client.get("/api/state").json()["devices"] == []

    probe = h.client.get("/api/wizard/probe", params={"host": srv.host}).json()
    assert probe["supported"] and probe["mac"] == "aa:bb:cc:dd:ee:01" and not probe["known"]

    job = adopt(h, srv.host)
    assert job["status"] == "done", job
    assert [s["status"] for s in job["steps"]] == ["ok", "warn", "ok", "ok", "ok", "ok"]  # reserve: not on AP subnet
    # the plug was configured as promised
    assert plug.config["cloud"] is False
    assert plug.config["name"] == "Desk lamp" and plug.config["device_name"] == "Desk lamp"
    assert plug.config["initial_state"] == "off" and plug.config["ap_enable"] is False
    assert plug.reboots == 1

    dev_id = job["device_id"]
    live = h.wait_for(lambda: (device(h, dev_id)["live"] or {}).get("online") and device(h, dev_id)["live"])
    assert live["output"] is False and live["power_w"] == 0.0 and live["rssi"] == -57

    # switch on through the API; the plug really switches and live data follows
    r = h.client.post(f"/api/devices/{dev_id}/switch", json={"on": True})
    assert r.status_code == 200 and r.json()["output"] is True and plug.output is True
    live = h.wait_for(lambda: (lambda l: l if l["power_w"] and l["power_w"] > 70 else None)(device(h, dev_id)["live"]))
    assert 79 < live["power_w"] < 81 and live["voltage"] == 230.4

    # samples get flushed to SQLite in batches and show up in history and energy
    h.wait_for(lambda: h.app.state.db.query("SELECT COUNT(*) FROM readings")[0][0] >= 4, timeout=15)
    now = int(time.time())
    hist = h.client.get("/api/history", params={"device": dev_id, "from": now - 120, "to": now + 2, "points": 60}).json()
    # polls are 0.25 s apart here, so the whole test falls into one or two 10 s buckets
    # whose average mixes off and on samples; the bucket maximum shows the real load
    pmax = [v for v in hist["p_max"] if v]
    assert pmax and max(pmax) > 70 and any(v for v in hist["p_avg"])
    assert sum(v for v in hist["e_wh"] if v) > 0
    since = h.client.get("/api/state", params={"since": now - 3600}).json()["devices"][0]
    assert since["energy_since_wh"] > 0

    # toggle, then edit and remove
    assert h.client.post(f"/api/devices/{dev_id}/switch", json={}).json()["output"] is False
    assert plug.output is False
    assert h.client.patch(f"/api/devices/{dev_id}", json={"name": "Lamp", "room": "Hall"}).json()["name"] == "Lamp"
    assert device(h, dev_id)["name"] == "Lamp"
    assert h.client.delete(f"/api/devices/{dev_id}", params={"purge": "true"}).status_code == 200
    assert h.client.get("/api/state").json()["devices"] == []
    assert h.app.state.db.query("SELECT COUNT(*) FROM readings WHERE device_id=?", (dev_id,))[0][0] == 0
    assert h.client.post(f"/api/devices/{dev_id}/switch", json={"on": True}).status_code == 404


def test_readopting_a_known_plug_updates_it(run_app, plugs):
    plug, srv = plugs(mac="AABBCCDDEE05")
    h = run_app()
    first = adopt(h, srv.host, name="One")
    second = adopt(h, srv.host, name="Two")
    assert first["device_id"] == second["device_id"]
    assert [d["name"] for d in device(h)] == ["Two"]
    assert h.client.get("/api/wizard/probe", params={"host": srv.host}).json()["known"] is True


def test_adopt_rejects_unsupported_and_unreachable(run_app, plugs):
    _, gen1 = plugs(mac="AABBCCDDEE06", gen=1)
    _, locked = plugs(mac="AABBCCDDEE07", auth_en=True)
    h = run_app()
    j = adopt(h, gen1.host)
    assert j["status"] == "failed" and "First-generation" in j["error"] and j["steps"][0]["status"] == "failed"
    j = adopt(h, locked.host)
    assert j["status"] == "failed" and "password" in j["error"]
    j = adopt(h, "127.0.0.1:9")
    assert j["status"] == "failed" and "Could not reach" in j["error"]
    assert h.client.get("/api/wizard/probe", params={"host": "127.0.0.1:9"}).status_code == 404
    assert device(h) == []


def test_offline_detection_recovery_and_counter_reset(run_app, plugs):
    import httpx

    plug, srv = plugs(mac="AABBCCDDEE02", load_w=100.0, speed=3600.0)
    h = run_app()
    dev_id = adopt(h, srv.host)["device_id"]
    h.client.post(f"/api/devices/{dev_id}/switch", json={"on": True})
    h.wait_for(lambda: (device(h, dev_id)["live"] or {}).get("power_w", 0) > 90)

    plug.fail = True
    h.wait_for(lambda: device(h, dev_id)["live"]["online"] is False, timeout=15)
    assert device(h, dev_id)["live"]["error"]
    assert h.client.post(f"/api/devices/{dev_id}/switch", json={"on": False}).status_code == 502
    plug.fail = False
    h.wait_for(lambda: device(h, dev_id)["live"]["online"] is True, timeout=15)

    # the plug's cumulative counter is reset: energy must never go negative
    httpx.get(f"http://{srv.host}/__test", params={"reset": "1"})
    time.sleep(1.2)
    h.wait_for(lambda: h.app.state.db.query("SELECT COUNT(*) FROM readings")[0][0] > 3, timeout=10)
    h.wait_for(lambda: not h.app.state.monitor.buffer, timeout=5)
    assert h.app.state.db.query("SELECT MIN(e_delta_wh) FROM readings")[0][0] >= 0


def test_scan_finds_plugs_from_dhcp_leases_and_reserves_address(run_app, plugs, monkeypatch, settings):
    plug, srv = plugs(mac="AABBCCDDEE03")
    _, dead = plugs(mac="AABBCCDDEE04")
    dead_host = dead.host
    dead.stop()  # a phone on the Wi-Fi: leases but nothing that answers like a Shelly
    mapping = {"192.168.50.101": srv.host, "192.168.50.150": dead_host}
    monkeypatch.setattr(
        ShellyClient, "_base", staticmethod(lambda host: "http://" + mapping.get(host, host))
    )
    h = run_app()
    settings.leases_file.write_text(
        "1900000000 aa:bb:cc:dd:ee:03 192.168.50.101 shellyplugmg3-aabbccddee03 *\n"
        "1900000000 11:22:33:44:55:66 192.168.50.150 iPhone *\n"
    )
    cands = h.client.get("/api/wizard/scan").json()["candidates"]
    assert [c["host"] for c in cands] == ["192.168.50.101"]
    assert cands[0]["mac"] == "aa:bb:cc:dd:ee:03" and cands[0]["supported"] and not cands[0]["known"]

    job = adopt(h, "192.168.50.101", name="Kettle")
    assert job["status"] == "done", job
    assert job["steps"][1]["status"] == "ok" and "192.168.50.101" in job["steps"][1]["detail"]
    res = settings.hosts_dir / "aa-bb-cc-dd-ee-03.conf"
    assert res.read_text().startswith("aa:bb:cc:dd:ee:03,192.168.50.101,plug-kettle-")
    assert h.client.get("/api/wizard/scan").json()["candidates"][0]["known"] is True

    h.client.delete(f"/api/devices/{job['device_id']}")
    assert not res.exists()                          # the reservation goes with the plug


def test_auth(run_app):
    h = run_app(password="s3cret")
    assert h.client.get("/api/state").status_code == 401
    assert h.client.get("/api/state").headers["www-authenticate"].startswith("Basic")
    assert h.client.get("/healthz").status_code == 200
    assert h.client.get("/api/state", auth=("admin", "wrong")).status_code == 401
    assert h.client.get("/api/state", auth=("root", "s3cret")).status_code == 401
    assert h.client.get("/api/state", auth=("admin", "s3cret")).status_code == 200
    assert h.client.get("/api/wizard/qr.svg").status_code == 401


def test_untrusted_clock_holds_samples_until_time_is_set(run_app, plugs):
    plug, srv = plugs(mac="AABBCCDDEE08", load_w=50.0)
    h = run_app()
    h.clock_offset["v"] = 1000.0 - time.time()        # clock reads 1970-01-01 00:16
    st = h.client.get("/api/state").json()
    assert st["clock"]["trusted"] is False and st["clock"]["reason"] == "before_minimum"

    dev_id = adopt(h, srv.host)["device_id"]
    h.client.post(f"/api/devices/{dev_id}/switch", json={"on": True})
    h.wait_for(lambda: len(h.app.state.monitor.buffer) >= 6)
    time.sleep(1.5)                                    # several flush intervals
    assert h.app.state.db.query("SELECT COUNT(*) FROM readings")[0][0] == 0   # nothing written with a bad clock
    assert h.client.get("/api/system").json()["held_samples"] >= 6

    # stand-in for the host's shelly-settime.path unit
    stop = threading.Event()

    def host_helper():
        req = h.app.state.cfg.bridge_dir / "settime.req"
        while not stop.is_set():
            if req.exists():
                epoch = int(req.read_text().strip())
                req.unlink()
                h.clock_offset["v"] = epoch - time.time()
            time.sleep(0.1)

    t = threading.Thread(target=host_helper, daemon=True)
    t.start()
    try:
        real_now = int(time.time())
        r = h.client.post("/api/system/time", json={"epoch": real_now})
        assert r.status_code == 200 and r.json()["clock"]["trusted"] is True
        h.wait_for(lambda: h.app.state.db.query("SELECT COUNT(*) FROM readings")[0][0] >= 6)
        lo, hi = h.app.state.db.query("SELECT MIN(ts), MAX(ts) FROM readings")[0]
        assert real_now - 120 < lo <= hi <= real_now + 30      # restamped, not 1970
        assert h.client.post("/api/system/time", json={"epoch": 5}).status_code == 400
    finally:
        stop.set()
        t.join(timeout=2)


def test_server_sent_events_stream_live_updates(run_app, plugs):
    import httpx

    _, srv = plugs(mac="AABBCCDDEE09", load_w=40.0)
    h = run_app()
    dev_id = adopt(h, srv.host)["device_id"]
    got = []
    with httpx.stream("GET", h.base + "/api/stream", timeout=10) as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        event = None
        for line in r.iter_lines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:") and event == "live":
                got.append(json.loads(line[5:]))
                if len(got) >= 2:
                    break
    assert got[0]["id"] == dev_id and got[0]["online"] is True and "power_w" in got[0]
