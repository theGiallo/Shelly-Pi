"""scripts/prepare-sd.sh: the parts that can be checked without an SD card."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "prepare-sd.sh"

LISTING = """<html><body><h1>Index of /pub/alt/iot/44/IoT/aarch64/images</h1>
<a href="Fedora-IoT-44-20260401.0-aarch64-CHECKSUM">Fedora-IoT-44-20260401.0-aarch64-CHECKSUM</a>
<a href="Fedora-IoT-44-20260415.1-aarch64-CHECKSUM">Fedora-IoT-44-20260415.1-aarch64-CHECKSUM</a>
<a href="Fedora-IoT-44-20260401.0.aarch64.raw.xz">Fedora-IoT-44-20260401.0.aarch64.raw.xz</a>
<a href="Fedora-IoT-44-20260415.1.aarch64.raw.xz">Fedora-IoT-44-20260415.1.aarch64.raw.xz</a>
<a href="Fedora-IoT-44-20260415.1.aarch64.iso">Fedora-IoT-44-20260415.1.aarch64.iso</a>
<a href="Fedora-IoT-44-20260415.10.aarch64.raw.xz">Fedora-IoT-44-20260415.10.aarch64.raw.xz</a>
<a href="Fedora-IoT-43-20251101.0.aarch64.raw.xz">Fedora-IoT-43-20251101.0.aarch64.raw.xz</a>
<a href="Fedora-IoT-raw-45-20261001.0.aarch64.raw.xz">Fedora-IoT-raw-45-20261001.0.aarch64.raw.xz</a>
<a href="Fedora-IoT-raw-45-20261020.0.aarch64.raw.xz">Fedora-IoT-raw-45-20261020.0.aarch64.raw.xz</a>
<a href="Fedora-IoT-45-aarch64-20261020.0-CHECKSUM">Fedora-IoT-45-aarch64-20261020.0-CHECKSUM</a>
</body></html>
"""

CHECKSUM = """-----BEGIN PGP SIGNED MESSAGE-----
Hash: SHA256

# Fedora-IoT-44-20260415.1.aarch64.raw.xz: 123456 bytes
SHA256 (Fedora-IoT-44-20260415.1.aarch64.raw.xz) = aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
SHA256 (Fedora-IoT-44-20260415.1.aarch64.iso) = bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
-----BEGIN PGP SIGNATURE-----
"""


def sh(code: str, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", f'source "{SCRIPT}"; {code}'], capture_output=True, text=True, **kw)


def test_image_discovery_picks_the_newest_build_of_the_release(tmp_path):
    f = tmp_path / "l.html"
    f.write_text(LISTING)
    r = sh(f'latest_from_listing "{f}" 44')
    assert r.stdout.strip() == "Fedora-IoT-44-20260415.10.aarch64.raw.xz"          # version sort: .10 after .1
    assert sh(f'latest_from_listing "{f}" 43').stdout.strip() == "Fedora-IoT-43-20251101.0.aarch64.raw.xz"
    assert sh(f'latest_from_listing "{f}" 99').stdout.strip() == ""
    # the spelling Fedora uses now: Fedora-IoT-raw-<release>-<build>.aarch64.raw.xz, checksum Fedora-IoT-<release>-aarch64-<build>-CHECKSUM
    assert sh(f'latest_from_listing "{f}" 45').stdout.strip() == "Fedora-IoT-raw-45-20261020.0.aarch64.raw.xz"
    r = sh(f'checksum_file_from_listing "{f}" 45 Fedora-IoT-raw-45-20261020.0.aarch64.raw.xz')
    assert r.stdout.strip() == "Fedora-IoT-45-aarch64-20261020.0-CHECKSUM"
    r = sh(f'checksum_file_from_listing "{f}" 44 Fedora-IoT-44-20260415.1.aarch64.raw.xz')
    assert r.stdout.strip() == "Fedora-IoT-44-20260415.1-aarch64-CHECKSUM"


def test_checksum_is_read_for_the_right_file(tmp_path):
    f = tmp_path / "c"
    f.write_text(CHECKSUM)
    r = sh(f'expected_sha256 "{f}" Fedora-IoT-44-20260415.1.aarch64.raw.xz')
    assert r.stdout.strip() == "a" * 64
    assert sh(f'expected_sha256 "{f}" nothing.raw.xz').stdout.strip() == ""


def make_root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    (root / "ostree/deploy/fedora-iot/var").mkdir(parents=True)
    (root / "ostree/deploy/fedora-iot/deploy/abc123.0/etc/systemd/system").mkdir(parents=True)
    return root


def run_inject(tmp_path, root, *extra):
    image = tmp_path / "app.tar"
    image.write_bytes(b"fake image")
    return subprocess.run(
        ["bash", str(SCRIPT), "--inject-only", str(root), "--app-image", str(image), *extra],
        capture_output=True, text=True, cwd=tmp_path)


def test_inject_puts_everything_where_the_pi_will_find_it(tmp_path):
    root = make_root(tmp_path)
    pw = "pa ss'wo\"rd$1!"
    r = run_inject(tmp_path, root, "--ssid", "My Plugs", "--ap-password", pw, "--admin-password", "adm in#1",
                   "--hostname", "kitchenpi", "--channel", "11", "--country", "DE", "--no-rtc")
    assert r.returncode == 0, r.stderr + r.stdout
    var = root / "ostree/deploy/fedora-iot/var/lib/shelly-install"
    assert (var / "project/deploy/setup-host.sh").is_file() and (var / "project/app/api.py").is_file()
    assert (var / "project/shelly-pi-image.tar").read_bytes() == b"fake image"
    assert not list((var / "project").rglob("__pycache__")) and not (var / "project/demo-data").exists()
    assert oct(var.stat().st_mode & 0o777) == "0o700"
    env = var / "firstboot.env"
    assert oct(env.stat().st_mode & 0o777) == "0o600"
    # the file is shell-quoted: reading it back gives exactly what was typed, special characters included
    out = subprocess.run(["bash", "-c", f'source "{env}"; printf "%s|%s|%s|%s|%s|%s|%s" "$SSID" "$AP_PASSWORD" "$ADMIN_PASSWORD" "$HOSTNAME_WANTED" "$CHANNEL" "$COUNTRY" "$RTC"'],
                         capture_output=True, text=True).stdout
    assert out == f"My Plugs|{pw}|adm in#1|kitchenpi|11|DE|no"
    unit = root / "ostree/deploy/fedora-iot/deploy/abc123.0/etc/systemd/system"
    assert (unit / "shelly-firstboot.service").is_file()
    link = unit / "multi-user.target.wants/shelly-firstboot.service"
    assert link.is_symlink() and os.readlink(link) == "/etc/systemd/system/shelly-firstboot.service"
    assert "ExecStart=/usr/bin/bash /var/lib/shelly-install/project/deploy/firstboot/firstboot.sh" in (unit / "shelly-firstboot.service").read_text()


def test_generated_passwords_are_saved_for_the_person(tmp_path):
    root = make_root(tmp_path)
    r = run_inject(tmp_path, root)
    assert r.returncode == 0, r.stderr
    files = list(tmp_path.glob("sd-credentials_*.txt"))
    assert len(files) == 1 and oct(files[0].stat().st_mode & 0o777) == "0o600"
    text = files[0].read_text()
    assert "Plugs' Wi-Fi password: " in text and "Dashboard password (user admin): " in text
    import re
    assert re.match(r"^sd-credentials_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.txt$", files[0].name)
    ap = re.search(r"Wi-Fi password: (\w+)", text).group(1)
    env = (root / "ostree/deploy/fedora-iot/var/lib/shelly-install/firstboot.env").read_text()
    assert ap in env                                                              # same password goes to the Pi


@pytest.mark.parametrize("args,msg", [
    (["--ssid", ""], "Wi-Fi name"),
    (["--channel", "14"], "channel"),
    (["--hostname", "Bad Name"], "host name"),
    (["--country", "it"], "country"),
    (["--ap-password", "short"], "8 to 63"),
])
def test_bad_answers_are_refused_before_anything_is_touched(tmp_path, args, msg):
    root = make_root(tmp_path)
    r = run_inject(tmp_path, root, *args)
    assert r.returncode != 0 and msg.lower() in r.stderr.lower()
    assert not (root / "ostree/deploy/fedora-iot/var/lib/shelly-install").exists()


def test_not_a_fedora_iot_root_is_refused(tmp_path):
    (tmp_path / "empty").mkdir()
    r = run_inject(tmp_path, tmp_path / "empty")
    assert r.returncode != 0 and "ostree" in r.stderr


def test_flashing_requires_root_and_a_device(tmp_path):
    r = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, cwd=tmp_path)
    assert r.returncode != 0 and ("root" in r.stderr or "--media" in r.stderr)
    if os.geteuid() == 0:
        r = subprocess.run(["bash", str(SCRIPT), "--media", "/dev/null", "--yes"], capture_output=True, text=True, cwd=tmp_path)
        assert r.returncode != 0 and ("missing" in r.stderr or "block device" in r.stderr)
