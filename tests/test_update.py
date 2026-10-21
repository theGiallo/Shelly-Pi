"""Updating the app from the Settings page: the upload endpoint and the root helper script."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "libexec" / "update.sh"


# ---- the web endpoint ------------------------------------------------------------------------

def test_upload_is_checked_and_handed_to_the_helper(run_app, settings):
    h = run_app()
    data = os.urandom(300_000)
    sha = hashlib.sha256(data).hexdigest()
    r = h.client.post(f"/api/update?sha256={sha}", content=data, headers={"Content-Type": "application/x-tar"})
    assert r.status_code == 200, r.text
    rid = r.json()["id"]
    bridge = settings.data_dir / "bridge"
    assert (bridge / "update.tar").read_bytes() == data
    assert (bridge / "update.req").read_text().split() == [rid, sha]
    assert not (bridge / "update.tar.part").exists()
    # a second one while the first waits is refused
    r2 = h.client.post(f"/api/update?sha256={sha}", content=data)
    assert r2.status_code == 409


def test_wrong_checksum_changes_nothing(run_app, settings):
    h = run_app()
    r = h.client.post(f"/api/update?sha256={'0' * 64}", content=b"not the right bytes")
    assert r.status_code == 422
    bridge = settings.data_dir / "bridge"
    assert not (bridge / "update.tar").exists() and not (bridge / "update.req").exists()
    assert not (bridge / "update.tar.part").exists()


def test_checksum_must_be_given(run_app):
    h = run_app()
    assert h.client.post("/api/update", content=b"x").status_code == 400
    assert h.client.post("/api/update?sha256=abc", content=b"x").status_code == 400
    assert h.client.get("/api/update").json()["status"] is None


def test_status_is_read_from_the_helper_file(run_app, settings):
    h = run_app()
    bridge = settings.data_dir / "bridge"
    (bridge / "update.res.json").write_text(json.dumps({"id": "a1", "state": "done", "message": "ok"}))
    st = h.client.get("/api/update").json()["status"]
    assert st["state"] == "done" and st["id"] == "a1"


# ---- the root script, with pretend podman / systemctl / curl ---------------------------------

def make_env(tmp_path: Path, load_output: str = "Loaded image: localhost/shelly-pi:latest",
             healthy_after_restart: int = 1, load_ok: bool = True, dhcp_unit: bool = True) -> dict:
    bridge, bin_ = tmp_path / "bridge", tmp_path / "bin"
    bridge.mkdir()
    bin_.mkdir()
    log = tmp_path / "calls.log"
    healthy = tmp_path / "healthy"
    restarts = tmp_path / "restarts"
    (bin_ / "podman").write_text(
        "#!/bin/sh\n"
        f'echo "podman $*" >> {log}\n'
        'case "$1" in\n'
        '  image) exit 0 ;;\n'
        '  tag) exit 0 ;;\n'
        f'  load) echo "{load_output}"; exit {0 if load_ok else 1} ;;\n'
        'esac\n')
    # restarts count up; the app is "healthy" only from restart number `healthy_after_restart` on
    (bin_ / "systemctl").write_text(
        "#!/bin/sh\n"
        f'echo "systemctl $*" >> {log}\n'
        f'[ "$1" = cat ] && exit {0 if dhcp_unit else 1}\n'
        '[ "$1" = restart ] || exit 0\n'
        f'n=$(cat {restarts} 2>/dev/null || echo 0); echo $((n+1)) > {restarts}\n'
        f'if [ $((n+1)) -ge {healthy_after_restart} ]; then touch {healthy}; else rm -f {healthy}; fi\n')
    (bin_ / "curl").write_text(f"#!/bin/sh\n[ -f {healthy} ]\n")
    for f in bin_.iterdir():
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    return {"PATH": f"{bin_}:{os.environ['PATH']}", "SHELLY_BRIDGE": str(bridge),
            "SHELLY_ENVFILE": str(tmp_path / "env"), "SHELLY_UPDATE_WAIT": "2", "LOG": str(log)}


def run_update(env: dict, data: bytes = b"image bytes", sha: str | None = None) -> dict:
    bridge = Path(env["SHELLY_BRIDGE"])
    (bridge / "update.tar").write_bytes(data)
    (bridge / "update.req").write_text(f"abc123 {sha or hashlib.sha256(data).hexdigest()}\n")
    subprocess.run(["sh", str(SCRIPT)], env={**os.environ, **env}, check=True, capture_output=True)
    return json.loads((bridge / "update.res.json").read_text())


def calls(env: dict) -> list[str]:
    return Path(env["LOG"]).read_text().splitlines()


def test_good_update_restarts_the_services(tmp_path):
    env = make_env(tmp_path)
    res = run_update(env)
    assert res["id"] == "abc123" and res["state"] == "done"
    assert not (Path(env["SHELLY_BRIDGE"]) / "update.tar").exists()
    c = calls(env)
    assert any(x.startswith("podman load") for x in c)
    assert "systemctl restart shelly-dhcp.service shelly-app.service" in c


def test_checksum_mismatch_is_refused_before_loading(tmp_path):
    env = make_env(tmp_path)
    res = run_update(env, sha="1" * 64)
    assert res["state"] == "failed" and "checksum" in res["message"]
    assert not Path(env["LOG"]).exists()      # nothing was called


def test_a_file_that_is_not_our_image_is_refused(tmp_path):
    env = make_env(tmp_path, load_output="Loaded image: docker.io/library/alpine:latest")
    res = run_update(env)
    assert res["state"] == "failed" and "isn't a Shelly-Pi image" in res["message"]
    assert not any(x.startswith("systemctl restart") for x in calls(env))
    assert "podman tag localhost/shelly-pi:previous localhost/shelly-pi:latest" in calls(env)


def test_new_version_that_does_not_start_is_rolled_back(tmp_path):
    env = make_env(tmp_path, healthy_after_restart=2)   # the second restart (the rollback) is the healthy one
    res = run_update(env)
    assert res["state"] == "rolled_back"
    c = calls(env)
    assert c.count("systemctl restart shelly-dhcp.service shelly-app.service") == 2
    assert "podman tag localhost/shelly-pi:previous localhost/shelly-pi:latest" in c


def test_load_failure_leaves_the_old_image(tmp_path):
    env = make_env(tmp_path, load_ok=False)
    res = run_update(env)
    assert res["state"] == "failed"
    assert not any(x.startswith("systemctl restart") for x in calls(env))


def test_about_has_license_and_changelog(run_app):
    h = run_app()
    a = h.client.get("/api/about").json()
    assert "MIT License" in a["license"]
    assert a["changelog"].startswith("# Changelog")
    assert a["third_party"][0]["license"]


def test_a_pi_without_wifi_has_no_dhcp_unit_to_restart(tmp_path):
    env = make_env(tmp_path, dhcp_unit=False)
    assert run_update(env)["state"] == "done"
    assert "systemctl restart shelly-app.service" in calls(env)


# ---- shutting down ---------------------------------------------------------------------------

def pretend_helper(bridge, delay=0.3):
    """Stands in for the Pi's helper: takes the request file away shortly after it appears."""
    import threading
    import time

    def run():
        end = time.time() + 8
        while time.time() < end:
            if (bridge / "power.req").exists():
                time.sleep(delay)
                taken.append((bridge / "power.req").read_text())
                (bridge / "power.req").unlink()
                return
            time.sleep(0.05)
    taken: list[str] = []
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return taken


def test_power_needs_confirmation_and_waits_for_the_helper(run_app, settings):
    h = run_app()
    assert h.client.post("/api/power", json={"action": "poweroff"}).status_code == 422
    assert h.client.post("/api/power", json={"action": "format", "confirm": True}).status_code == 422
    bridge = settings.data_dir / "bridge"
    bridge.mkdir(parents=True, exist_ok=True)
    taken = pretend_helper(bridge)
    r = h.client.post("/api/power", json={"action": "poweroff", "confirm": True})
    assert r.status_code == 200
    assert taken and taken[0].split()[1] == "poweroff"


def test_power_without_a_helper_says_so_and_leaves_nothing_behind(run_app, settings):
    h = run_app()
    r = h.client.post("/api/power", json={"action": "poweroff", "confirm": True})
    assert r.status_code == 504 and "helper" in r.json()["detail"]
    assert not (settings.data_dir / "bridge" / "power.req").exists()


def test_power_is_refused_during_an_update(run_app, settings):
    h = run_app()
    bridge = settings.data_dir / "bridge"
    bridge.mkdir(parents=True, exist_ok=True)
    (bridge / "update.res.json").write_text(json.dumps({"id": "x", "state": "loading", "message": ""}))
    assert h.client.post("/api/power", json={"action": "reboot", "confirm": True}).status_code == 409


POWER = Path(__file__).resolve().parent.parent / "deploy" / "libexec" / "power.sh"


def run_power(tmp_path: Path, line: str) -> tuple[subprocess.CompletedProcess, list[str]]:
    bridge, bin_ = tmp_path / "bridge", tmp_path / "bin"
    bridge.mkdir()
    bin_.mkdir()
    log = tmp_path / "calls.log"
    (bin_ / "systemctl").write_text(f'#!/bin/sh\necho "systemctl $*" >> {log}\n')
    (bin_ / "systemctl").chmod(0o755)
    (bridge / "power.req").write_text(line + "\n")
    p = subprocess.run(["sh", str(POWER)], capture_output=True, text=True, env={
        **os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", "SHELLY_BRIDGE": str(bridge), "SHELLY_POWER_DELAY": "0"})
    assert not (bridge / "power.req").exists()
    return p, (log.read_text().splitlines() if log.exists() else [])


def test_power_script_runs_only_the_two_known_actions(tmp_path):
    p, calls_ = run_power(tmp_path, "abc poweroff")
    assert p.returncode == 0 and calls_ == ["systemctl poweroff"]
    (tmp_path / "b").mkdir()
    p, calls_ = run_power(tmp_path / "b", "abc reboot")
    assert calls_ == ["systemctl reboot"]
    (tmp_path / "c").mkdir()
    p, calls_ = run_power(tmp_path / "c", "abc halt; rm -rf /")
    assert p.returncode == 1 and calls_ == []


def test_power_script_ignores_an_old_request(tmp_path):
    import time
    bridge = tmp_path / "bridge"
    bridge.mkdir()
    (bridge / "power.req").write_text("abc poweroff\n")
    old = time.time() - 600
    os.utime(bridge / "power.req", (old, old))
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "systemctl").write_text(f'#!/bin/sh\necho "$*" >> {tmp_path}/calls\n')
    (tmp_path / "bin" / "systemctl").chmod(0o755)
    subprocess.run(["sh", str(POWER)], check=True, capture_output=True, env={
        **os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}", "SHELLY_BRIDGE": str(bridge), "SHELLY_POWER_DELAY": "0"})
    assert not (bridge / "power.req").exists() and not (tmp_path / "calls").exists()
