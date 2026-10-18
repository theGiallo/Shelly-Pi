"""dnsmasq lease reading and DHCP reservations.

The DHCP server (dnsmasq, in its own container) writes its leases to
<data>/dnsmasq/dnsmasq.leases and watches <data>/dhcp-hosts.d/ for
reservation files (--dhcp-hostsdir, picked up automatically, no restart).
"""

from __future__ import annotations

import ipaddress
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

_MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")


def normalize_mac(raw: str | None) -> str | None:
    """'AABBCCDDEEFF', 'aa-bb-..' or 'AA:BB:..' -> 'aa:bb:cc:dd:ee:ff' (or None)."""
    if not raw:
        return None
    s = raw.strip().lower().replace("-", ":")
    if ":" not in s and len(s) == 12:
        s = ":".join(s[i : i + 2] for i in range(0, 12, 2))
    return s if _MAC_RE.match(s) else None


@dataclass
class Lease:
    mac: str
    ip: str
    hostname: str
    expiry: int


def read_leases(path: Path) -> list[Lease]:
    """Parse dnsmasq's lease file ('expiry mac ip hostname clientid'), newest per MAC."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    best: dict[str, Lease] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        mac = normalize_mac(parts[1])
        if not mac:
            continue
        try:
            expiry = int(parts[0])
            ipaddress.ip_address(parts[2])
        except ValueError:
            continue
        lease = Lease(mac=mac, ip=parts[2], hostname="" if parts[3] == "*" else parts[3], expiry=expiry)
        old = best.get(mac)
        if old is None or lease.expiry >= old.expiry:
            best[mac] = lease
    return list(best.values())


def find_ip(path: Path, mac: str) -> str | None:
    mac = normalize_mac(mac) or mac
    for lease in read_leases(path):
        if lease.mac == mac:
            return lease.ip
    return None


def is_plain_ip(host: str) -> bool:
    """True for a bare, non-loopback IP (not 'ip:port')."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return not ip.is_loopback


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s[:24] or "plug"


def reservation_path(hosts_dir: Path, mac: str) -> Path:
    return Path(hosts_dir) / (mac.replace(":", "-") + ".conf")


def write_reservation(hosts_dir: Path, mac: str, ip: str, name: str) -> Path:
    """Pin `mac` to `ip` (dnsmasq hostsfile format: mac,ip,hostname)."""
    mac_n = normalize_mac(mac)
    if not mac_n:
        raise ValueError(f"bad MAC: {mac!r}")
    ipaddress.ip_address(ip)
    hosts_dir = Path(hosts_dir)
    hosts_dir.mkdir(parents=True, exist_ok=True)
    final = reservation_path(hosts_dir, mac_n)
    hostname = f"plug-{_slug(name)}-{mac_n.replace(':', '')[-4:]}"
    # Write next to the directory, then rename in, so dnsmasq never reads a half file.
    fd, tmp = tempfile.mkstemp(prefix=".res-", dir=hosts_dir.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(f"{mac_n},{ip},{hostname}\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, final)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return final


def remove_reservation(hosts_dir: Path, mac: str) -> None:
    mac_n = normalize_mac(mac)
    if not mac_n:
        return
    try:
        reservation_path(hosts_dir, mac_n).unlink()
    except FileNotFoundError:
        pass
