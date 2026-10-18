"""deploy/libexec/wifiscan.sh, run for real on pretend `iw` and `nmcli` output."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "libexec" / "wifiscan.sh"

IW = """\
BSS aa:bb:cc:00:00:01(on wlan0) -- associated
\tTSF: 123 usec (0d, 00:00:00)
\tfreq: 2437
\tsignal: -48.00 dBm
\tcapability: ESS Privacy ShortSlotTime (0x0411)
\tSSID: ShellyPi
\tDS Parameter set: channel 6
\tRSN:\t * Version: 1
BSS 34:94:54:11:22:33(on wlan0)
\tfreq: 2412
\tsignal: -61.00 dBm
\tcapability: ESS ShortSlotTime (0x0401)
\tSSID: ShellyPlugMG3-A1B2C3
BSS 34:94:54:11:22:34(on wlan0)
\tfreq: 2412
\tsignal: -70.00 dBm
\tcapability: ESS ShortSlotTime (0x0401)
\tSSID: ShellyPlugMG3-A1B2C3
BSS 34:94:54:44:55:66(on wlan0)
\tfreq: 2462
\tsignal: -82.50 dBm
\tcapability: ESS (0x0401)
\tSSID: ShellyPlugMG3-D4E5F6
BSS 11:22:33:44:55:66(on wlan0)
\tfreq: 5180
\tsignal: -55.00 dBm
\tcapability: ESS Privacy (0x0011)
\tSSID: Neighbour \\x22Net\\x22
\tWPA:\t * Version: 1
BSS 11:22:33:44:55:77(on wlan0)
\tfreq: 2437
\tsignal: -75.00 dBm
\tcapability: ESS (0x0401)
\tSSID: 
"""

NMCLI = """\
AA\\:BB\\:CC\\:00\\:00\\:01:6:98:WPA2:ShellyPi
""".replace("\\:", ":") + """\
34:94:54:11:22:33:1:78:--:ShellyPlugMG3-A1B2C3
11:22:33:44:55:66:36:60:WPA2:Odd:Name
"""


def scan(tmp_path, raw: str, fmt: str = "iw", env_extra: dict | None = None) -> dict:
    bridge = tmp_path / "bridge"
    bridge.mkdir(exist_ok=True)
    fixture = tmp_path / "scan.txt"
    fixture.write_text(raw)
    (bridge / "wifiscan.req").write_text("w1 scan\n")
    env = dict(os.environ, SHELLY_BRIDGE=str(bridge), SHELLY_WIFISCAN_CMD=f"cat {fixture}",
               SHELLY_WIFISCAN_FORMAT=fmt, SHELLY_ENVFILE=str(tmp_path / "env"), **(env_extra or {}))
    subprocess.run(["sh", str(SCRIPT)], env=env, check=True, capture_output=True, timeout=30)
    assert not (bridge / "wifiscan.req").exists()
    return json.loads((bridge / "wifiscan.res.json").read_text())


def test_parses_iw(tmp_path):
    r = scan(tmp_path, IW)
    assert r["id"] == "w1" and r["ok"] is True and r["iface"] == "wlan0"
    nets = {(n["ssid"], n["bssid"]): n for n in r["networks"]}
    assert len(r["networks"]) == 5                         # the hidden network with no name is left out
    home = nets[("ShellyPi", "aa:bb:cc:00:00:01")]
    assert home["signal"] == -48 and home["channel"] == 6 and home["secured"] is True
    plug = nets[("ShellyPlugMG3-A1B2C3", "34:94:54:11:22:33")]
    assert plug["signal"] == -61 and plug["channel"] == 1 and plug["secured"] is False
    assert nets[("ShellyPlugMG3-D4E5F6", "34:94:54:44:55:66")]["channel"] == 11
    assert nets[('Neighbour "Net"', "11:22:33:44:55:66")]["channel"] == 36    # \x22 decoded, 5 GHz channel


def test_parses_nmcli_and_colons_in_names(tmp_path):
    r = scan(tmp_path, NMCLI, fmt="nmcli")
    by = {n["ssid"]: n for n in r["networks"]}
    assert set(by) == {"ShellyPi", "ShellyPlugMG3-A1B2C3", "Odd:Name"}
    assert by["ShellyPlugMG3-A1B2C3"]["secured"] is False and by["ShellyPi"]["secured"] is True
    assert by["ShellyPlugMG3-A1B2C3"]["signal"] == -61 and by["ShellyPlugMG3-A1B2C3"]["channel"] == 1


def test_interface_comes_from_the_env_file(tmp_path):
    (tmp_path / "env").write_text("SM_AP_IFACE=wlan1\nSM_SCAN_IFACE=wlan7\nOTHER=x\n")
    assert scan(tmp_path, IW)["iface"] == "wlan7"
    (tmp_path / "env").write_text("SM_AP_IFACE=wlan1\n")
    assert scan(tmp_path, IW)["iface"] == "wlan1"


def test_no_networks_is_an_empty_list(tmp_path):
    r = scan(tmp_path, "")
    assert r["ok"] is True and r["networks"] == []
