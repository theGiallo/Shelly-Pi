"""Finding a new plug's own Wi-Fi: ranking, the join QR code, and the whole path through the helper."""

from __future__ import annotations

import io
import os
import subprocess
import threading
import time

import pytest
import segno

from app import plugwifi
from app.bridge import BridgeError
from app.plugwifi import is_shelly_ssid, quality, shelly_networks, wifi_qr_text

from test_wifiscan_script import IW, SCRIPT


def net(ssid, dbm, **kw):
    return {"ssid": ssid, "signal": dbm, "channel": 6, "secured": False, **kw}


def test_which_names_are_shelly_setup_networks():
    for ok in ("ShellyPlugMG3-A1B2C3", "ShellyPlusPlugS-AABBCCDDEEFF", "shellyplug-s-123ABC", "ShellyPlugMG3-a1b2"):
        assert is_shelly_ssid(ok), ok
    for bad in ("ShellyPi", "Shelly", "MyShelly-ABC", "ShellyPlugMG3-XYZ123", "ShellyPlugMG3-A1B2C3 ", "", "Neighbour"):
        assert not is_shelly_ssid(bad), bad


def test_strongest_first_one_per_name_and_flags_known_plugs():
    raw = [net("ShellyPi", -40), net("ShellyPlugMG3-A1B2C3", -70), net("ShellyPlugMG3-A1B2C3", -58),
           net("ShellyPlugMG3-D4E5F6", -83, secured=True), net("ShellyPlugMG3-111111", -65), {"junk": 1}, "x"]
    nets = shelly_networks(raw, [("34:94:54:d4:e5:f6", "Fridge")])
    assert [n["ssid"] for n in nets] == ["ShellyPlugMG3-A1B2C3", "ShellyPlugMG3-111111", "ShellyPlugMG3-D4E5F6"]
    assert nets[0]["signal"] == -58 and nets[0]["quality"] == "good" and not nets[0]["known"]
    assert nets[2]["known"] and nets[2]["known_name"] == "Fridge" and nets[2]["secured"] and nets[2]["quality"] == "weak"


@pytest.mark.parametrize("dbm, word", [(-40, "excellent"), (-55, "excellent"), (-60, "good"), (-75, "fair"), (-90, "weak")])
def test_quality_words(dbm, word):
    assert quality(dbm) == word


def test_qr_text_escapes_what_the_format_reserves():
    assert wifi_qr_text("ShellyPlugMG3-A1B2C3") == "WIFI:T:nopass;S:ShellyPlugMG3-A1B2C3;;"
    assert wifi_qr_text('a;b,c:d"e\\f') == r'WIFI:T:nopass;S:a\;b\,c\:d\"e\\f;;'


# ---- through the app ---------------------------------------------------------------------------

class FakePi:
    """What shelly-wifiscan.path does: run wifiscan.sh when wifiscan.req shows up."""

    def __init__(self, bridge, scan_text):
        self.bridge, self.scan_text, self.runs = bridge, scan_text, 0
        self.stop = threading.Event()
        threading.Thread(target=self.watch, daemon=True).start()

    def watch(self):
        fixture = self.bridge.parent / "scan.txt"
        while not self.stop.is_set():
            if (self.bridge / "wifiscan.req").exists():
                fixture.write_text(self.scan_text)
                env = dict(os.environ, SHELLY_BRIDGE=str(self.bridge), SHELLY_WIFISCAN_CMD=f"cat {fixture}",
                           SHELLY_ENVFILE=str(self.bridge.parent / "env"))
                subprocess.run(["sh", str(SCRIPT)], env=env, capture_output=True, timeout=60)
                self.runs += 1
            time.sleep(0.05)


@pytest.fixture
def pi(settings):
    bridge = settings.data_dir / "bridge"
    bridge.mkdir(parents=True, exist_ok=True)
    p = FakePi(bridge, IW)
    yield p
    p.stop.set()


def test_scan_through_the_app(run_app, pi, monkeypatch):
    monkeypatch.setattr(plugwifi, "CACHE_SECONDS", 0)
    h = run_app()
    r = h.client.get("/api/wizard/plug-wifi")
    assert r.status_code == 200, r.text
    nets = r.json()["networks"]
    assert [n["ssid"] for n in nets] == ["ShellyPlugMG3-A1B2C3", "ShellyPlugMG3-D4E5F6"]     # not ShellyPi, not the neighbour
    assert nets[0]["signal"] == -61 and nets[0]["quality"] == "good" and nets[0]["secured"] is False
    assert r.json()["iface"] == "wlan0"


def test_nothing_found_is_not_an_error(run_app, pi, monkeypatch):
    monkeypatch.setattr(plugwifi, "CACHE_SECONDS", 0)
    pi.scan_text = ""
    h = run_app()
    r = h.client.get("/api/wizard/plug-wifi")
    assert r.status_code == 200 and r.json()["networks"] == []


def test_scans_are_shared_for_a_moment(run_app, pi):
    h = run_app()
    h.client.get("/api/wizard/plug-wifi")
    h.client.get("/api/wizard/plug-wifi")
    assert pi.runs == 1


def test_helper_missing_is_explained(run_app, monkeypatch):
    monkeypatch.setattr(plugwifi, "SCAN_TIMEOUT", 1.0)
    h = run_app()
    r = h.client.get("/api/wizard/plug-wifi")
    assert r.status_code == 504 and "shelly-wifiscan.path" in r.json()["detail"]


def test_qr_for_joining_the_plug(run_app):
    h = run_app()
    r = h.client.get("/api/wizard/qr.svg", params={"kind": "plugwifi", "ssid": "ShellyPlugMG3-A1B2C3"})
    assert r.status_code == 200 and r.headers["content-type"] == "image/svg+xml"
    buf = io.BytesIO()
    segno.make("WIFI:T:nopass;S:ShellyPlugMG3-A1B2C3;;", error="m").save(buf, kind="svg", scale=6, border=2, dark="#000000", light="#ffffff")
    assert r.content == buf.getvalue()
    # it won't make a sign-in code for any other network name
    for bad in ("ShellyPi", "", "HomeWifi;P:secret"):
        assert h.client.get("/api/wizard/qr.svg", params={"kind": "plugwifi", "ssid": bad}).status_code == 400
