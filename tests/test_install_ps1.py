"""PowerShell can't run here, so check what can be checked: the installer agrees with the Pi-side script."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PS1 = (ROOT / "scripts" / "install.ps1").read_text()
SETUP = (ROOT / "deploy" / "setup-host.sh").read_text()


def test_ascii_only_so_windows_powershell_5_reads_it_without_a_bom():
    assert all(ord(c) < 128 for c in PS1)


def test_here_strings_and_brackets_balance():
    assert PS1.count("@'") == PS1.count("\n'@")
    assert PS1.count('@"') == PS1.count('\n"@')
    for a, b in ("{}", "()", "[]"):
        assert PS1.count(a) == PS1.count(b), a


def test_every_flag_it_passes_exists_in_setup_host():
    run_step = PS1.split("# ---- 7.")[1].split("# ---- 8.")[0]
    for flag in set(re.findall(r"--[a-z][a-z-]+", run_step)) - {"--preserve-env"}:   # --preserve-env is sudo's
        assert re.search(re.escape(flag) + r"[|)]", SETUP), flag


def test_secrets_use_the_variables_setup_host_reads():
    for name in ("SHELLY_AP_PASSWORD", "SHELLY_ADMIN_PASSWORD"):
        assert name in PS1 and name in SETUP


def test_the_image_name_matches_what_setup_host_loads_and_ci_publishes():
    assert "shelly-pi-image.tar" in PS1 and "shelly-pi-image.tar" in SETUP
    assert "shelly-pi-image.tar" in (ROOT / ".github" / "workflows" / "image.yml").read_text()


def test_the_double_click_wrapper_points_at_the_script():
    cmd = (ROOT / "install.cmd").read_bytes()
    assert b"scripts\\install.ps1" in cmd and b"\r\n" in cmd
