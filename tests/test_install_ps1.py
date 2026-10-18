"""PowerShell can't run here, so check what can be checked: the installer agrees with the Pi-side script."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PS1 = (ROOT / "scripts" / "install.ps1").read_text()
ALL = {n: (ROOT / "scripts" / n).read_text() for n in ("install.ps1", "install-windows.ps1", "flash-sd.ps1", "ext4-inject.ps1")}
SETUP = (ROOT / "deploy" / "setup-host.sh").read_text()


def test_ascii_only_so_windows_powershell_5_reads_it_without_a_bom():
    for name, text in ALL.items():
        assert all(ord(c) < 128 for c in text), name


def test_here_strings_and_brackets_balance():
    for name, text in ALL.items():
        assert text.count("@'") == text.count("\n'@"), name
        assert text.count('@"') == text.count('\n"@'), name
        # code inside single-quoted here-strings (bash, C#) is not PowerShell; CI's real PowerShell parser checks the rest too
        code = re.sub(r"@'\n.*?\n'@", "", text, flags=re.S)
        for a, b in ("{}", "()", "[]"):
            assert code.count(a) == code.count(b), (name, a)


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


def test_windows_files_have_windows_line_endings():
    for name in ALL:
        raw = (ROOT / "scripts" / name).read_bytes()
        assert raw.count(b"\r\n") == raw.count(b"\n"), name


def test_the_card_writer_is_only_started_with_things_it_declares():
    wizard, flash = ALL["install-windows.ps1"], ALL["flash-sd.ps1"]
    for param in ("-Image", "-Extractor", "-Disk", "-Yes"):
        assert param in wizard and param.lstrip("-") in flash, param
    assert "flash-sd.ps1" in wizard and "install.ps1" in wizard


def test_the_card_writer_never_offers_the_system_disk():
    flash = ALL["flash-sd.ps1"]
    assert "-not $_.IsSystem" in flash and "-not $_.IsBoot" in flash and "USB" in flash


def test_the_one_click_file_runs_the_wizard():
    assert b"install-windows.ps1" in (ROOT / "install.cmd").read_bytes()
