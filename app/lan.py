"""Finding plugs on a router's network, where the Pi doesn't hand out the addresses itself.

Shelly plugs announce themselves with mDNS (the "_shelly._tcp" service) and answer to
"<id>.local". There is no mDNS library in the image, so this speaks just enough of the
protocol: one question out on every network the Pi is on, answers read from the replies.

The questions go out from an ordinary port with the "unicast reply" bit set, so a plug answers
the Pi directly and nothing has to listen on port 5353 (the Pi's own mDNS service uses it).
"""

from __future__ import annotations

import asyncio
import fcntl
import ipaddress
import logging
import select
import socket
import struct
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

MDNS_GROUP, MDNS_PORT = "224.0.0.251", 5353
T_A, T_PTR, T_TXT, T_SRV = 1, 12, 16, 33
SHELLY_SERVICE = "_shelly._tcp.local"
MAX_SCAN_HOSTS = 1022          # a /22 at most
SIOCGIFADDR, SIOCGIFNETMASK = 0x8915, 0x891B


# ---- the DNS message format --------------------------------------------------------------

@dataclass
class Record:
    name: str
    rtype: int
    data: object   # A: "1.2.3.4"; PTR: name; SRV: (port, target); TXT: bytes


def build_query(questions: list[tuple[str, int]], unicast_reply: bool = True) -> bytes:
    out = struct.pack("!HHHHHH", 0, 0, len(questions), 0, 0, 0)
    for name, qtype in questions:
        for label in name.strip(".").split("."):
            raw = label.encode("utf-8")
            if not 0 < len(raw) < 64:
                raise ValueError(f"bad name {name!r}")
            out += bytes([len(raw)]) + raw
        out += b"\x00" + struct.pack("!HH", qtype, 0x8001 if unicast_reply else 1)
    return out


def _name(data: bytes, pos: int, depth: int = 0) -> tuple[str, int]:
    """Read a (possibly compressed) name at pos. Returns the name and the position after it."""
    labels: list[str] = []
    end = None
    hops = 0
    while True:
        if pos >= len(data):
            raise ValueError("name runs off the end")
        n = data[pos]
        if n == 0:
            pos += 1
            break
        if n & 0xC0 == 0xC0:
            if pos + 1 >= len(data):
                raise ValueError("bad pointer")
            if end is None:
                end = pos + 2
            pos = ((n & 0x3F) << 8) | data[pos + 1]
            hops += 1
            if hops > 20:
                raise ValueError("pointer loop")
            continue
        if n & 0xC0:
            raise ValueError("bad label")
        if pos + 1 + n > len(data):
            raise ValueError("label runs off the end")
        labels.append(data[pos + 1:pos + 1 + n].decode("utf-8", "replace"))
        pos += 1 + n
    return ".".join(labels), (end if end is not None else pos)


def parse_message(data: bytes) -> list[Record]:
    """All the answer, authority and additional records in a DNS message (bad ones are skipped)."""
    if len(data) < 12:
        return []
    _id, flags, qd, an, ns, ar = struct.unpack("!HHHHHH", data[:12])
    pos = 12
    records: list[Record] = []
    try:
        for _ in range(qd):
            _n, pos = _name(data, pos)
            pos += 4
        for _ in range(an + ns + ar):
            name, pos = _name(data, pos)
            if pos + 10 > len(data):
                break
            rtype, _cls, _ttl, rdlen = struct.unpack("!HHIH", data[pos:pos + 10])
            pos += 10
            rd_start, pos = pos, pos + rdlen
            if pos > len(data):
                break
            if rtype == T_A and rdlen == 4:
                records.append(Record(name.lower(), T_A, socket.inet_ntoa(data[rd_start:pos])))
            elif rtype == T_PTR:
                records.append(Record(name.lower(), T_PTR, _name(data, rd_start)[0].lower()))
            elif rtype == T_SRV and rdlen >= 7:
                port = struct.unpack("!H", data[rd_start + 4:rd_start + 6])[0]
                records.append(Record(name.lower(), T_SRV, (port, _name(data, rd_start + 6)[0].lower())))
            elif rtype == T_TXT:
                records.append(Record(name.lower(), T_TXT, data[rd_start:pos]))
    except (ValueError, struct.error):
        pass
    return records


# ---- the networks this machine is on --------------------------------------------------------

def local_networks() -> list[tuple[str, str, str]]:
    """(interface, address, netmask) of every IPv4 network this machine is on, except loopback."""
    found = []
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for _idx, name in socket.if_nameindex():
            if name == "lo":
                continue
            req = struct.pack("256s", name[:15].encode())
            try:
                addr = socket.inet_ntoa(fcntl.ioctl(s.fileno(), SIOCGIFADDR, req)[20:24])
                mask = socket.inet_ntoa(fcntl.ioctl(s.fileno(), SIOCGIFNETMASK, req)[20:24])
            except OSError:
                continue            # no IPv4 address on this one
            if not addr.startswith("169.254."):
                found.append((name, addr, mask))
    finally:
        s.close()
    return found


def scan_addresses(networks: list[tuple[str, str, str]], skip_nets: list[str] = ()) -> list[str]:
    """Every address worth trying on these networks (never more than a /22 each, never the Pi itself).

    skip_nets: networks to leave out, such as the Pi's own Wi-Fi.
    """
    skip = [ipaddress.ip_network(n, strict=False) for n in skip_nets]
    out: list[str] = []
    seen: set[str] = set()
    for _name, addr, mask in networks:
        try:
            net = ipaddress.ip_network(f"{addr}/{mask}", strict=False)
        except ValueError:
            continue
        if net.prefixlen < 22:
            net = ipaddress.ip_network(f"{addr}/22", strict=False)
        if net.prefixlen > 30 or any(net.overlaps(k) for k in skip):
            continue
        for h in net.hosts():
            ip = str(h)
            if ip != addr and ip not in seen and len(out) < MAX_SCAN_HOSTS * 2:
                seen.add(ip)
                out.append(ip)
    return out


# ---- asking ----------------------------------------------------------------------------------

def _ask(questions: list[tuple[str, int]], addrs: list[str], timeout: float) -> list[Record]:
    """Send the questions out of each address (twice, a second apart) and collect every answer."""
    packet = build_query(questions)
    socks = []
    for a in addrs:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(a))
            s.bind((a, 0))
            s.setblocking(False)
            socks.append(s)
        except OSError as e:
            log.debug("mDNS: can't use %s: %s", a, e)
    records: list[Record] = []
    try:
        deadline = time.monotonic() + timeout
        next_send = 0.0
        while socks:
            now = time.monotonic()
            if now >= deadline:
                break
            if now >= next_send:
                for s in socks:
                    try:
                        s.sendto(packet, (MDNS_GROUP, MDNS_PORT))
                    except OSError:
                        pass
                next_send = now + 1.0
            ready, _, _ = select.select(socks, [], [], min(0.25, deadline - now))
            for s in ready:
                try:
                    data, _src = s.recvfrom(9000)
                except OSError:
                    continue
                records.extend(parse_message(data))
    finally:
        for s in socks:
            s.close()
    return records


def _addresses(exclude_nets: list[str]) -> list[str]:
    skip = [ipaddress.ip_network(n, strict=False) for n in exclude_nets]
    return [a for _n, a, _m in local_networks() if not any(ipaddress.ip_address(a) in k for k in skip)]


def _browse_blocking(timeout: float, exclude_nets: list[str]) -> list[str]:
    addrs = _addresses(exclude_nets)
    if not addrs:
        return []
    recs = _ask([(SHELLY_SERVICE, T_PTR)], addrs, timeout)
    instances = {r.data for r in recs if r.rtype == T_PTR and r.name == SHELLY_SERVICE}
    targets = {r.name: r.data[1] for r in recs if r.rtype == T_SRV and r.name in instances}
    ips = {r.name: r.data for r in recs if r.rtype == T_A}
    # Plugs whose announcement came without an address: ask for it by name.
    missing = sorted({t for t in targets.values() if t not in ips})[:40]
    if missing:
        for r in _ask([(t, T_A) for t in missing], addrs, min(2.0, timeout)):
            if r.rtype == T_A:
                ips[r.name] = r.data
    found = [ips[t] for t in targets.values() if t in ips]
    # Some replies carry only an address record for a name that looks like a Shelly.
    found += [ip for name, ip in ips.items() if name.startswith("shelly") and ip not in found]
    return sorted(set(found), key=lambda x: tuple(int(p) for p in x.split(".")))


async def browse(timeout: float = 3.5, exclude_nets: list[str] = ()) -> list[str]:
    """Addresses of the Shelly devices that answer mDNS on the networks this machine is on."""
    return await asyncio.to_thread(_browse_blocking, timeout, list(exclude_nets))


def _resolve_blocking(hostname: str, timeout: float, exclude_nets: list[str]) -> str | None:
    name = hostname.lower().strip(".")
    if not name.endswith(".local"):
        name += ".local"
    addrs = _addresses(exclude_nets)
    for r in _ask([(name, T_A)], addrs, timeout) if addrs else []:
        if r.rtype == T_A and r.name == name:
            return r.data
    return None


async def resolve(hostname: str, timeout: float = 2.5, exclude_nets: list[str] = ()) -> str | None:
    """The address of '<hostname>.local', or None."""
    return await asyncio.to_thread(_resolve_blocking, hostname, timeout, list(exclude_nets))
