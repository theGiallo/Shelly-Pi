"""The private CA and the TLS front: certificates validate the way a browser would check them."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import ssl
import stat
import time
from datetime import timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from app.tls import LEAF_DAYS, CertStore, TlsError, short_name
from app.tlsfront import TlsFront

IPS = ["192.168.50.1", "192.168.1.20", "8.8.8.8", "100.64.1.2", "not-an-ip"]


@pytest.fixture
def store(tmp_path):
    s = CertStore(tmp_path / "tls")
    s.create_ca(time.time(), "ShellyPi")
    return s


def test_ca_is_private_and_constrained(store):
    assert stat.S_IMODE(store.ca_key.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.dir.stat().st_mode) == 0o700
    info = store.ca_info()
    assert info["name"] == "Shelly Monitor local CA (shellypi)"
    assert info["permitted_dns"] == ["local", "localhost", "shellypi"]
    assert len(info["fingerprint"].split(":")) == 32
    assert info["not_after"] - info["not_before"] > 3600 * 24 * 3600
    assert b"BEGIN CERTIFICATE" in store.ca_pem() and b"PRIVATE" not in store.ca_pem()


def test_names_are_limited_to_what_the_ca_allows(store):
    dns, ips = store.wanted_names("ShellyPi.local", IPS)
    assert dns == ["shellypi", "shellypi.local", "localhost"]
    assert ips == ["127.0.0.1", "192.168.1.20", "192.168.50.1"]      # no public, CGNAT or junk addresses
    assert short_name("Not a host name!") == "shellypi"


def test_issue_reissue_and_expiry(store):
    now = time.time()
    assert store.ensure_server(now, "shellypi", IPS) is True
    assert store.ensure_server(now + 3600, "shellypi", IPS) is False          # nothing changed
    info = store.server_info()
    assert info.ips == ["127.0.0.1", "192.168.1.20", "192.168.50.1"]
    assert LEAF_DAYS - 2 <= info.days_left(now) <= LEAF_DAYS + 2
    assert stat.S_IMODE(store.server_key.stat().st_mode) == 0o600
    # the Ethernet address changed: a new certificate covers it
    assert store.ensure_server(now, "shellypi", ["192.168.50.1", "192.168.1.77"]) is True
    assert "192.168.1.77" in store.server_info().ips
    # close to expiry it renews itself, far from it it doesn't
    assert store.ensure_server(now + 300 * 86400, "shellypi", ["192.168.50.1", "192.168.1.77"]) is False
    assert store.ensure_server(now + (LEAF_DAYS - 20) * 86400, "shellypi", ["192.168.50.1", "192.168.1.77"]) is True
    # a certificate from a different CA is replaced
    old = store.server_crt.read_bytes()
    store.create_ca(now, "shellypi")
    assert not store.server_crt.exists()
    store.ensure_server(now, "shellypi", [])
    assert store.server_crt.read_bytes() != old


def test_no_ca_no_certificate(tmp_path):
    with pytest.raises(TlsError):
        CertStore(tmp_path / "x").ensure_server(time.time(), "shellypi", [])


async def backend(port_holder):
    async def handle(r, w):
        await r.readuntil(b"\r\n\r\n")
        body = b"hello from the app"
        w.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(body) + body)
        await w.drain()
        w.close()
    srv = await asyncio.start_server(handle, "127.0.0.1", 0)
    port_holder.append(srv.sockets[0].getsockname()[1])
    return srv


async def fetch(port, store, server_hostname, connect_host="127.0.0.1"):
    ctx = ssl.create_default_context(cafile=str(store.ca_crt))
    r, w = await asyncio.open_connection(connect_host, port, ssl=ctx, server_hostname=server_hostname)
    try:
        w.write(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        await w.drain()
        data = await r.read()
        der = w.get_extra_info("ssl_object").getpeercert(binary_form=True)
        return data, der
    finally:
        w.close()


def test_front_serves_valid_https_and_picks_up_a_new_certificate(store):
    store.ensure_server(time.time(), "shellypi", ["192.168.50.1"])

    async def main():
        ph: list[int] = []
        be = await backend(ph)
        front = TlsFront(0, ph[0], bind="127.0.0.1")
        await front.start(store.server_crt, store.server_key)
        port = front.bound_port
        try:
            # the three ways a person opens it: the .local name, the host name, an address
            for name in ("shellypi.local", "shellypi", "127.0.0.1"):
                data, der1 = await fetch(port, store, name)
                assert data.endswith(b"hello from the app"), name
            with pytest.raises(ssl.SSLCertVerificationError):          # not a name we cover
                await fetch(port, store, "evil.example.com")
            other = CertStore(store.dir.parent / "other")               # a stranger's CA isn't trusted
            other.create_ca(time.time(), "shellypi")
            with pytest.raises(ssl.SSLCertVerificationError):
                await fetch(port, other, "shellypi.local")
            # renew while running: new connections see the new certificate, no restart
            assert store.ensure_server(time.time(), "shellypi", ["192.168.50.1"], force=True)
            front.reload(store.server_crt, store.server_key)
            _, der2 = await fetch(port, store, "shellypi.local")
            assert der2 != der1
        finally:
            await front.stop()
            be.close()
        assert not front.running

    asyncio.run(main())


def test_a_stolen_ca_key_cannot_sign_for_real_websites(store, tmp_path):
    """The name constraint is enforced by the client's TLS library, not just written in the certificate."""
    ca = x509.load_pem_x509_certificate(store.ca_pem())
    ca_key = serialization.load_pem_private_key(store.ca_key.read_bytes(), None)
    key = ec.generate_private_key(ec.SECP256R1())
    now = time.time()
    from datetime import datetime, timezone
    t = datetime.fromtimestamp(now, timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "google.com")]))
            .issuer_name(ca.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(t - timedelta(days=1)).not_valid_after(t + timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("google.com"), x509.DNSName("shellypi.local")]), critical=False)
            .sign(ca_key, hashes.SHA256()))
    (tmp_path / "evil.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "evil.key").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))

    async def main():
        ph: list[int] = []
        be = await backend(ph)
        front = TlsFront(0, ph[0], bind="127.0.0.1")
        await front.start(tmp_path / "evil.crt", tmp_path / "evil.key")
        try:
            with pytest.raises(ssl.SSLCertVerificationError):
                await fetch(front.bound_port, store, "google.com")
            with pytest.raises(ssl.SSLCertVerificationError):      # one bad name in the certificate spoils it
                await fetch(front.bound_port, store, "shellypi.local")
        finally:
            await front.stop()
            be.close()

    asyncio.run(main())
