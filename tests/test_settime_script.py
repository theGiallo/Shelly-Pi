"""deploy/libexec/settime.sh: it adds the time that passed since the request was written."""
import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "libexec" / "settime.sh"


def run(tmp_path, request, uptime="1000.50 900.00"):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "date.log"
    (bin_dir / "date").write_text(f'#!/bin/sh\necho "$@" >> {log}\n')
    (bin_dir / "hwclock").write_text("#!/bin/sh\nexit 0\n")
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    req = tmp_path / "settime.req"
    req.write_text(request)
    up = tmp_path / "uptime"
    up.write_text(uptime)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "SHELLY_SETTIME_REQ": str(req),
           "SHELLY_UPTIME_FILE": str(up)}
    r = subprocess.run(["sh", str(SCRIPT)], env=env, capture_output=True, text=True)
    return r, req, log.read_text() if log.exists() else ""


def test_adds_the_time_since_the_request_was_written(tmp_path):
    r, req, log = run(tmp_path, "1800000000.250 1000.00", uptime="1001.75 0")
    assert r.returncode == 0, r.stderr
    assert "-u -s @1800000001.999" in log or "-u -s @1800000002.000" in log
    assert not req.exists()


def test_old_style_request_still_works(tmp_path):
    r, _req, log = run(tmp_path, "1800000000")
    assert r.returncode == 0, r.stderr
    assert "@1800000000" in log


def test_rejects_times_outside_the_range(tmp_path):
    r, req, log = run(tmp_path, "5 10.0")
    assert r.returncode != 0 and log == "" and not req.exists()


def test_ignores_a_silly_elapsed_time(tmp_path):
    r, _req, log = run(tmp_path, "1800000000.000 10.0", uptime="5000.0 0")
    assert r.returncode == 0, r.stderr
    assert "@1800000000.000" in log
