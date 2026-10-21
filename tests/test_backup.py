"""Export and import of the whole data set."""

from __future__ import annotations

import io
import json
import re
import sqlite3
import zipfile
from zoneinfo import ZoneInfo

import httpx
import pytest

from app import backup

NAME_RE = re.compile(r"^shelly-pi_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_v\d+\.\d+\.\d+\.zip$")
TARIFF = {"enabled": True, "currency": "CHF", "currency_after": True, "decimals": 2,
          "timezone": "Europe/Zurich", "base_price": 0.31, "slots": []}


def test_archive_name_is_sortable_and_uses_the_zone():
    rome = ZoneInfo("Europe/Rome")
    a = backup.archive_name(1_792_200_000, rome, "0.1.5")     # 2026-10-17 ~01:20 UTC
    assert NAME_RE.match(backup.archive_name(1_792_200_000, rome))
    assert a == "shelly-pi_2026-10-17_03-20-00_v0.1.5.zip"          # +02:00
    assert backup.archive_name(1_792_200_000, ZoneInfo("UTC"), "0.1.5") == "shelly-pi_2026-10-17_01-20-00_v0.1.5.zip"
    names = [backup.archive_name(t, rome) for t in (1_792_200_000 + 86400 * k for k in (3, 0, 40, 1))]
    assert sorted(names) == [backup.archive_name(t, rome) for t in sorted(1_792_200_000 + 86400 * k for k in (3, 0, 40, 1))]
    assert backup.zone_or("Not/AZone", "UTC").key == "UTC"


def populate(h, settings):
    db = h.app.state.db
    dev = db.add_device(mac="aa:bb:cc:dd:ee:01", name="Kettle", ip="10.0.0.9", added_ts=1_790_000_000)["id"]
    now = 1_791_000_000
    db.insert_readings([(dev, now + 10 * i, 1500.0, 230.0, 6.5, 31.0, None, 4.1667, 1) for i in range(300)])
    assert h.client.put("/api/tariff", json=TARIFF).status_code == 200
    settings.data_dir.joinpath("config.json").write_text(json.dumps(
        {"poll_interval": 7, "flush_interval": 120, "password": "dash-secret", "ap_password": "wifi-secret", "ap_ssid": "Mine"}))
    settings.hosts_dir.joinpath("plug-kettle-ee01.conf").write_text("aa:bb:cc:dd:ee:01,10.0.0.9,plug-kettle\n")
    return dev


def test_export_contents(run_app, settings):
    h = run_app()
    populate(h, settings)
    r = h.client.get("/api/backup", params={"tz": "Europe/Rome"})
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    fname = re.search(r'filename="?([^";]+)"?', r.headers["content-disposition"]).group(1)
    assert NAME_RE.match(fname), fname
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert sorted(z.namelist()) == ["config.json", "dhcp-hosts.d/plug-kettle-ee01.conf", "manifest.json", "shelly-pi.db"]
    cfg = json.loads(z.read("config.json"))
    assert "password" not in cfg and "ap_password" not in cfg and cfg["ap_ssid"] == "Mine"
    assert b"dash-secret" not in r.content and b"wifi-secret" not in r.content
    m = json.loads(z.read("manifest.json"))
    assert m["kind"] == backup.KIND and m["format"] == 1 and m["counts"]["devices"] == 1 and m["counts"]["readings"] >= 300
    assert m["created_local"] in fname
    # the database inside is a complete, ordinary SQLite file
    path = settings.data_dir / "check.db"
    path.write_bytes(z.read("shelly-pi.db"))
    con = sqlite3.connect(path)
    assert con.execute("SELECT COUNT(*) FROM readings").fetchone()[0] >= 300
    assert "CHF" in con.execute("SELECT v FROM meta WHERE k='tariff'").fetchone()[0]
    con.close()
    # temporary files are cleaned up
    h.wait_for(lambda: not list(settings.data_dir.glob(".backup-*")), timeout=5)   # removed once the download is sent


def test_restore_round_trip_replaces_data_and_keeps_machine_settings(run_app, settings):
    h = run_app()
    dev = populate(h, settings)
    archive = h.client.get("/api/backup").content

    # change everything afterwards
    db = h.app.state.db
    h.client.delete(f"/api/devices/{dev}", params={"purge": "true"})
    assert h.client.put("/api/tariff", json={**TARIFF, "currency": "$", "base_price": 9}).status_code == 200
    settings.data_dir.joinpath("config.json").write_text(json.dumps(
        {"poll_interval": 3, "password": "dash-secret", "ap_password": "wifi-secret", "ap_ssid": "Mine"}))
    (settings.hosts_dir / "plug-kettle-ee01.conf").unlink()
    assert h.client.get("/api/state").json()["devices"] == []

    r = h.client.post("/api/restore", content=archive, headers={"Content-Type": "application/zip"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and "poll_interval" in body["settings_restored"]

    st = h.client.get("/api/state").json()
    assert [d["name"] for d in st["devices"]] == ["Kettle"]
    assert st["tariff"]["currency"] == "CHF" and st["tariff"]["base_price"] == 0.31
    n = h.app.state.db.query("SELECT COUNT(*) FROM readings")[0][0]
    assert n >= 300
    assert (settings.hosts_dir / "plug-kettle-ee01.conf").is_file()
    on_disk = json.loads(settings.data_dir.joinpath("config.json").read_text())
    assert on_disk["poll_interval"] == 7 and on_disk["flush_interval"] == 120     # taken from the backup
    assert on_disk["password"] == "dash-secret" and on_disk["ap_password"] == "wifi-secret"  # kept
    assert settings.poll_interval == 7.0                                         # live app uses it too
    # the previous data was set aside, and the app still works afterwards
    aside = settings.data_dir / backup.ASIDE
    assert (aside / backup.DB_NAME).is_file()
    assert json.loads((aside / "config.json").read_text())["poll_interval"] == 3
    assert h.client.get("/healthz").status_code == 200
    assert h.client.get("/api/system").json()["db"]["devices"] == 1
    assert not (settings.data_dir / ".restore").exists()


def zip_bytes(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for k, v in members.items():
            z.writestr(k, v)
    return buf.getvalue()


def test_bad_archives_are_refused_and_change_nothing(run_app, settings):
    h = run_app()
    populate(h, settings)
    good = h.client.get("/api/backup").content
    z = zipfile.ZipFile(io.BytesIO(good))
    manifest, dbfile = z.read("manifest.json"), z.read("shelly-pi.db")
    cases = {
        "not a zip": b"hello",
        "empty zip": zip_bytes({}),
        "no database": zip_bytes({"manifest.json": manifest}),
        "path escape": zip_bytes({"manifest.json": manifest, "shelly-pi.db": dbfile, "../evil.txt": b"x"}),
        "stray file": zip_bytes({"manifest.json": manifest, "shelly-pi.db": dbfile, "notes.txt": b"x"}),
        "bad host file name": zip_bytes({"manifest.json": manifest, "shelly-pi.db": dbfile, "dhcp-hosts.d/..": b"x"}),
        "wrong kind": zip_bytes({"manifest.json": b'{"kind":"other","format":1}', "shelly-pi.db": dbfile}),
        "newer format": zip_bytes({"manifest.json": json.dumps({**json.loads(manifest), "format": 99}).encode(), "shelly-pi.db": dbfile}),
        "damaged db": zip_bytes({"manifest.json": manifest, "shelly-pi.db": dbfile[: len(dbfile) // 2] + b"\0" * 4096}),
        "not a db": zip_bytes({"manifest.json": manifest, "shelly-pi.db": b"not sqlite" * 100}),
    }
    for label, data in cases.items():
        r = h.client.post("/api/restore", content=data, headers={"Content-Type": "application/zip"})
        assert r.status_code == 422, (label, r.status_code, r.text)
        assert isinstance(r.json()["detail"], str) and r.json()["detail"], label
    assert not (settings.data_dir.parent / "evil.txt").exists() and not (settings.data_dir / "evil.txt").exists()
    # still the same data, and still running
    assert h.client.get("/api/system").json()["db"]["devices"] == 1
    assert h.app.state.db.query("SELECT COUNT(*) FROM readings")[0][0] >= 300
    assert not (settings.data_dir / ".restore").exists()


def test_changes_need_the_custom_header_but_reading_does_not(run_app):
    h = run_app()
    bare = httpx.Client(base_url=h.base, timeout=10)  # like a form on another website: no custom header
    try:
        assert bare.get("/api/state").status_code == 200
        assert bare.get("/api/backup").status_code == 200
        for method, path, kw in [("PUT", "/api/tariff", {"json": TARIFF}), ("POST", "/api/restore", {"content": b"x"}),
                                 ("POST", "/api/devices/1/switch", {"json": {}}), ("DELETE", "/api/devices/1", {})]:
            r = bare.request(method, path, **kw)
            assert r.status_code == 403, (method, path)
    finally:
        bare.close()
