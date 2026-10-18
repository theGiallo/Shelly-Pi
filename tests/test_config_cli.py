from __future__ import annotations

import json
import stat

from app import cli
from app.config import load_settings


def test_init_config_writes_private_file_and_loads_back(tmp_path, monkeypatch):
    monkeypatch.setenv("SM_PASSWORD", 'dash"board\\pw')
    monkeypatch.setenv("SM_AP_PASSWORD", "wifi pass with spaces 123")
    rc = cli.main(["init-config", "--data-dir", str(tmp_path), "--ap-ssid", "My Plugs", "--ap-ip", "192.168.60.1"])
    assert rc == 0
    f = tmp_path / "config.json"
    assert stat.S_IMODE(f.stat().st_mode) == 0o600          # secrets: owner only
    assert json.loads(f.read_text())["ap_ssid"] == "My Plugs"

    s = load_settings({"SM_DATA_DIR": str(tmp_path)})
    assert s.password == 'dash"board\\pw'                    # odd characters survive
    assert s.ap_password == "wifi pass with spaces 123"
    assert s.ap_ssid == "My Plugs" and s.ap_ip == "192.168.60.1"
    assert s.leases_file == tmp_path / "dnsmasq" / "dnsmasq.leases"
    assert s.hosts_dir == tmp_path / "dhcp-hosts.d" and s.bridge_dir == tmp_path / "bridge"


def test_rerun_without_secrets_keeps_existing_ones(tmp_path, monkeypatch):
    monkeypatch.setenv("SM_PASSWORD", "one")
    monkeypatch.setenv("SM_AP_PASSWORD", "two-two-two")
    cli.main(["init-config", "--data-dir", str(tmp_path), "--ap-ssid", "A"])
    monkeypatch.delenv("SM_PASSWORD")
    monkeypatch.delenv("SM_AP_PASSWORD")
    cli.main(["init-config", "--data-dir", str(tmp_path), "--ap-ssid", "B"])
    s = load_settings({"SM_DATA_DIR": str(tmp_path)})
    assert (s.password, s.ap_password, s.ap_ssid) == ("one", "two-two-two", "B")


def test_environment_overrides_file_and_types_are_coerced(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"port": 8081, "poll_interval": 5}))
    s = load_settings({"SM_DATA_DIR": str(tmp_path), "SM_PORT": "9090", "SM_FLUSH_INTERVAL": "60"})
    assert s.port == 9090 and s.poll_interval == 5.0 and s.flush_interval == 60.0
    assert load_settings({"SM_DATA_DIR": str(tmp_path / "none")}).port == 80   # defaults


def test_static_pages_and_vendor_files_are_served(run_app):
    h = run_app()
    for path, ctype in [("/", "text/html"), ("/add", "text/html"), ("/settings", "text/html"),
                        ("/static/app.css", "text/css"), ("/static/vendor/uPlot.iife.min.js", "javascript"),
                        ("/static/vendor/uPlot.min.css", "text/css")]:
        r = h.client.get(path)
        assert r.status_code == 200 and ctype in r.headers["content-type"], path
    assert 'href="/static/vendor/uPlot.min.css"' in h.client.get("/").text
    # nothing may load from the internet: this runs on a network without it
    for page in ("/", "/add", "/settings"):
        html = h.client.get(page).text
        assert "http://" not in html.replace("http://192", "") and "https://" not in html


def test_wizard_only_accepts_ip_addresses_and_pages_send_security_headers(run_app):
    h = run_app()
    assert h.client.get("/api/wizard/probe", params={"host": "evil.example.com"}).status_code == 400
    assert h.client.get("/api/wizard/probe", params={"host": "10.0.0.1/../x"}).status_code == 400
    bad = h.client.post("/api/wizard/adopt", json={"host": "evil.example.com", "name": "x"})
    assert bad.status_code == 422
    r = h.client.get("/")
    csp = r.headers["content-security-policy"]
    assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY"
