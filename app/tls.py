"""A small private certificate authority, so the dashboard can use HTTPS with no internet.

The Pi makes its own CA once (valid 10 years) and signs the server certificate with it
(valid about 13 months, renewed by the Pi itself well before it runs out). You install the
CA certificate once on each device that opens the dashboard, and from then on those
devices trust the server certificate without any warning.

The CA is restricted with a name constraint to ".local" names, this Pi's host name, "localhost"
and private IP ranges, so even if its key leaked it couldn't be used to impersonate a real
website on a device that trusts it. The CA key never leaves the Pi and isn't put in backups.

Nothing here needs the network. It does need a believable clock, since certificate dates
come from it: the caller passes `now` and refuses to issue while the clock isn't trusted.
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

CA_DAYS = 3650
LEAF_DAYS = 397
RENEW_BEFORE_DAYS = 30
BACKDATE = timedelta(days=1)  # tolerate a client whose clock is a bit behind
PRIVATE_NETS = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "169.254.0.0/16")
LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


class TlsError(RuntimeError):
    """Something the person can act on; the message is fit to show."""


def short_name(hostname: str) -> str:
    """'ShellyPi.local' -> 'shellypi'. Falls back to 'shellypi' if it isn't a usable label."""
    label = (hostname or "").split(".")[0].strip().lower()
    return label if LABEL_RE.match(label) else "shellypi"


def local_ipv4s() -> list[str]:
    """The IPv4 addresses on this machine's interfaces (Linux; empty elsewhere)."""
    out: set[str] = set()
    try:
        import fcntl
        import struct

        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            for _, name in socket.if_nameindex():
                try:
                    res = fcntl.ioctl(s.fileno(), 0x8915, struct.pack("256s", name.encode()[:15]))  # SIOCGIFADDR
                    out.add(socket.inet_ntoa(res[20:24]))
                except OSError:
                    continue
        finally:
            s.close()
    except (ImportError, OSError):
        pass
    return sorted(out)


def _utc(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, timezone.utc)


def _epoch(dt: datetime) -> int:
    return int(dt.replace(tzinfo=timezone.utc).timestamp()) if dt.tzinfo is None else int(dt.timestamp())


def _key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def _write(path: Path, data: bytes, mode: int) -> None:
    tmp = path.with_name("." + path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def _key_pem(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def _fingerprint(cert: x509.Certificate) -> str:
    h = cert.fingerprint(hashes.SHA256()).hex().upper()
    return ":".join(h[i:i + 2] for i in range(0, len(h), 2))


@dataclass
class ServerInfo:
    dns: list[str]
    ips: list[str]
    not_before: int
    not_after: int

    def days_left(self, now: float) -> float:
        return (self.not_after - now) / 86400


class CertStore:
    """Files in one directory: ca.crt, ca.key, server.crt, server.key."""

    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self._lock = threading.RLock()

    # ---- paths ---------------------------------------------------------------------

    @property
    def ca_crt(self) -> Path:
        return self.dir / "ca.crt"

    @property
    def ca_key(self) -> Path:
        return self.dir / "ca.key"

    @property
    def server_crt(self) -> Path:
        return self.dir / "server.crt"

    @property
    def server_key(self) -> Path:
        return self.dir / "server.key"

    # ---- the authority --------------------------------------------------------------

    def has_ca(self) -> bool:
        return self.ca_crt.is_file() and self.ca_key.is_file()

    def _load_ca(self) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
        try:
            cert = x509.load_pem_x509_certificate(self.ca_crt.read_bytes())
            key = serialization.load_pem_private_key(self.ca_key.read_bytes(), None)
        except (OSError, ValueError) as e:
            raise TlsError("The certificate authority files can't be read.") from e
        if not isinstance(key, ec.EllipticCurvePrivateKey):
            raise TlsError("The certificate authority key isn't the expected kind.")
        return cert, key

    def create_ca(self, now: float, hostname: str) -> None:
        """Make a new CA, replacing any old CA and server certificate."""
        host = short_name(hostname)
        with self._lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            os.chmod(self.dir, 0o700)
            for p in (self.ca_crt, self.ca_key, self.server_crt, self.server_key):
                p.unlink(missing_ok=True)
            key = _key()
            name = x509.Name([
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Shelly-Pi"),
                x509.NameAttribute(NameOID.COMMON_NAME, f"Shelly-Pi local CA ({host})"),
            ])
            constraints = x509.NameConstraints(
                permitted_subtrees=[x509.DNSName("local"), x509.DNSName("localhost"), x509.DNSName(host)]
                + [x509.IPAddress(ipaddress.ip_network(n)) for n in PRIVATE_NETS],
                excluded_subtrees=None,
            )
            cert = (
                x509.CertificateBuilder()
                .subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(_utc(now) - BACKDATE).not_valid_after(_utc(now) + timedelta(days=CA_DAYS))
                .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                .add_extension(x509.KeyUsage(
                    digital_signature=False, content_commitment=False, key_encipherment=False,
                    data_encipherment=False, key_agreement=False, key_cert_sign=True, crl_sign=True,
                    encipher_only=False, decipher_only=False), critical=True)
                .add_extension(constraints, critical=True)
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
                .sign(key, hashes.SHA256())
            )
            _write(self.ca_key, _key_pem(key), 0o600)
            _write(self.ca_crt, cert.public_bytes(serialization.Encoding.PEM), 0o644)

    def ca_pem(self) -> bytes:
        return self.ca_crt.read_bytes()

    def ca_info(self) -> dict[str, Any]:
        cert, _ = self._load_ca()
        cn = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
        nc = cert.extensions.get_extension_for_class(x509.NameConstraints).value
        return {
            "name": cn, "fingerprint": _fingerprint(cert),
            "not_before": _epoch(cert.not_valid_before_utc), "not_after": _epoch(cert.not_valid_after_utc),
            "permitted_dns": [g.value for g in nc.permitted_subtrees or [] if isinstance(g, x509.DNSName)],
        }

    def _permitted(self) -> tuple[set[str], list[ipaddress.IPv4Network | ipaddress.IPv6Network]]:
        cert, _ = self._load_ca()
        nc = cert.extensions.get_extension_for_class(x509.NameConstraints).value
        dns = {g.value for g in nc.permitted_subtrees or [] if isinstance(g, x509.DNSName)}
        nets = [g.value for g in nc.permitted_subtrees or [] if isinstance(g, x509.IPAddress)]
        return dns, nets

    # ---- the server certificate -------------------------------------------------------

    def wanted_names(self, hostname: str, ips: list[str]) -> tuple[list[str], list[str]]:
        """Names and addresses the server certificate should cover, limited to what the CA allows."""
        dns_ok, nets = self._permitted()
        host = short_name(hostname)
        dns = [n for n in (host, f"{host}.local", "localhost")
               if any(n == d or n.endswith("." + d) for d in dns_ok)]
        addrs = []
        for ip in ["127.0.0.1", *ips]:
            try:
                a = ipaddress.ip_address(ip)
            except ValueError:
                continue
            if any(a in n for n in nets) and str(a) not in addrs:
                addrs.append(str(a))
        return dns, sorted(addrs, key=lambda x: tuple(int(p) for p in x.split(".")))

    def server_info(self) -> ServerInfo | None:
        try:
            cert = x509.load_pem_x509_certificate(self.server_crt.read_bytes())
            san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        except (OSError, ValueError, x509.ExtensionNotFound):
            return None
        return ServerInfo(
            dns=sorted(san.get_values_for_type(x509.DNSName)),
            ips=sorted(str(i) for i in san.get_values_for_type(x509.IPAddress)),
            not_before=_epoch(cert.not_valid_before_utc), not_after=_epoch(cert.not_valid_after_utc),
        )

    def _server_ok(self, now: float, dns: list[str], ips: list[str]) -> bool:
        info = self.server_info()
        if info is None or not self.server_key.is_file():
            return False
        if sorted(dns) != info.dns or sorted(ips) != info.ips:
            return False
        if info.days_left(now) < RENEW_BEFORE_DAYS or info.not_before > now + 2 * 86400:
            return False
        try:  # must be signed by the current CA
            ca, _ = self._load_ca()
            leaf = x509.load_pem_x509_certificate(self.server_crt.read_bytes())
            leaf.verify_directly_issued_by(ca)
        except Exception:  # noqa: BLE001 - any failure here just means "issue a new one"
            return False
        return True

    def ensure_server(self, now: float, hostname: str, ips: list[str], force: bool = False) -> bool:
        """Make sure there is a good server certificate; returns True if a new one was issued."""
        with self._lock:
            if not self.has_ca():
                raise TlsError("There is no certificate authority yet.")
            dns, addrs = self.wanted_names(hostname, ips)
            if not dns:
                raise TlsError("The host name isn't one the certificate authority may sign for. "
                               "Create a new authority in Settings.")
            if not force and self._server_ok(now, dns, addrs):
                return False
            ca, ca_key = self._load_ca()
            key = _key()
            san = [x509.DNSName(n) for n in dns] + [x509.IPAddress(ipaddress.ip_address(a)) for a in addrs]
            cert = (
                x509.CertificateBuilder()
                .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, dns[1] if len(dns) > 1 else dns[0])]))
                .issuer_name(ca.subject).public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(_utc(now) - BACKDATE).not_valid_after(_utc(now) + timedelta(days=LEAF_DAYS))
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.KeyUsage(
                    digital_signature=True, content_commitment=False, key_encipherment=False,
                    data_encipherment=False, key_agreement=False, key_cert_sign=False, crl_sign=False,
                    encipher_only=False, decipher_only=False), critical=True)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                .add_extension(x509.SubjectAlternativeName(san), critical=False)
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
                .sign(ca_key, hashes.SHA256())
            )
            _write(self.server_key, _key_pem(key), 0o600)
            _write(self.server_crt, cert.public_bytes(serialization.Encoding.PEM), 0o644)
            return True
