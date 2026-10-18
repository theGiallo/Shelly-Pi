"""HTTPS in the app: status, certificate download, the TLS listener, redirect, clock rules."""

from __future__ import annotations

import asyncio
import socket
import ssl
import time
from types import SimpleNamespace

import httpx
import pytest

from app.auth import HttpsRedirect
from app.config import Settings
from app.https_manager import HttpsManager


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_feature_can_be_switched_off(run_app):
    h = run_app()                       # https_port = 0 in the test settings
    assert h.client.get("/api/https").json() == {"available": False}
    assert h.client.get("/api/https/ca.crt").status_code == 404
    assert h.client.put("/api/https", json={"enabled": True, "https_only": False}).status_code == 404


def test_https_end_to_end(run_app, settings):
    port = free_port()
    h = run_app(https_port=port, host="127.0.0.1", hostname="ShellyPi")
    mgr = h.app.state.https
    mgr.front.backend_port = h.srv.port     # the test server isn't on port 80
    st = h.wait_for(lambda: (lambda s: s if s.get("listening") else None)(h.client.get("/api/https").json()))
    assert st["enabled"] and not st["https_only"] and st["error"] is None and st["port"] == port
    assert st["hostname"] == "shellypi" and st["ca"]["name"].endswith("(shellypi)")
    assert {"shellypi", "shellypi.local", "localhost"} <= set(st["server"]["dns"])
    assert "127.0.0.1" in st["server"]["ips"] and st["server"]["days_left"] > 390

    ca = h.client.get("/api/https/ca.crt")
    assert ca.status_code == 200 and ca.content.startswith(b"-----BEGIN CERTIFICATE-----")
    assert "attachment" in ca.headers["content-disposition"] and b"PRIVATE" not in ca.content
    cafile = settings.data_dir / "ca-for-test.crt"
    cafile.write_bytes(ca.content)

    # the app really answers over TLS, validated against the downloaded authority
    with httpx.Client(verify=ssl.create_default_context(cafile=str(cafile)), timeout=10) as c:
        r = c.get(f"https://127.0.0.1:{port}/healthz")
        assert r.status_code == 200 and r.json()["ok"] is True
        assert c.get(f"https://127.0.0.1:{port}/api/https").json()["listening"] is True
    with pytest.raises(httpx.ConnectError):   # the system's own trust store knows nothing of it
        httpx.get(f"https://127.0.0.1:{port}/healthz", timeout=5)

    # plain HTTP can only be switched off once HTTPS is up, and it is remembered
    assert h.client.put("/api/https", json={"enabled": False, "https_only": True}).status_code == 409
    r = h.client.put("/api/https", json={"enabled": True, "https_only": True})
    assert r.status_code == 200 and r.json()["https_only"] is True
    assert h.client.get("/api/https").json()["https_only"] is True
    # turning HTTPS off closes the port and clears the redirect
    r = h.client.put("/api/https", json={"enabled": False, "https_only": False})
    assert r.json()["listening"] is False and r.json()["https_only"] is False
    with pytest.raises(httpx.ConnectError):
        httpx.get(f"https://127.0.0.1:{port}/healthz", timeout=3, verify=False)
    h.client.put("/api/https", json={"enabled": True, "https_only": False})
    h.wait_for(lambda: h.client.get("/api/https").json()["listening"])

    # a new authority needs confirming, replaces the old one and drops the redirect
    old = h.client.get("/api/https").json()["ca"]["fingerprint"]
    assert h.client.post("/api/https/reset-ca", json={}).status_code == 422
    r = h.client.post("/api/https/reset-ca", json={"confirm": True})
    assert r.status_code == 200, r.text
    assert r.json()["ca"]["fingerprint"] != old and r.json()["listening"] and not r.json()["https_only"]
    new_ca = h.client.get("/api/https/ca.crt").content
    assert new_ca != ca.content
    cafile.write_bytes(new_ca)
    with httpx.Client(verify=ssl.create_default_context(cafile=str(cafile)), timeout=10) as c:
        assert c.get(f"https://127.0.0.1:{port}/healthz").status_code == 200


def run_asgi(mw, scope):
    sent = []

    async def send(m):
        sent.append(m)

    async def receive():
        return {"type": "http.request"}

    asyncio.run(mw(scope, receive, send))
    return sent


def scope(client, path="/", query=b"", host="192.168.1.20"):
    return {"type": "http", "path": path, "query_string": query, "client": (client, 5000),
            "headers": [(b"host", host.encode())]}


def test_redirect_rules():
    reached = []

    async def app(scope, receive, send):
        reached.append(scope["path"])

    mgr = SimpleNamespace(
        cfg=SimpleNamespace(https_port=443), redirecting=lambda: True,
        redirect_host=lambda h: h.split(":")[0] if h.startswith("192.168.1.20") else "shellypi.local")
    mw = HttpsRedirect(app, lambda: mgr)

    sent = run_asgi(mw, scope("192.168.1.5", "/settings", b"a=1"))
    assert sent[0]["status"] == 308 and (b"location", b"https://192.168.1.20/settings?a=1") in sent[0]["headers"]
    sent = run_asgi(mw, scope("192.168.1.5", "/", host="evil.example.com"))
    assert (b"location", b"https://shellypi.local/") in sent[0]["headers"]        # never to a host we don't cover
    assert not reached
    for path in ("/healthz", "/api/https/ca.crt"):                                  # needed before trust exists
        run_asgi(mw, scope("192.168.1.5", path))
    run_asgi(mw, scope("127.0.0.1", "/settings"))                                   # came through the TLS listener
    assert reached == ["/healthz", "/api/https/ca.crt", "/settings"]
    mgr.cfg.https_port = 8443
    assert (b"location", b"https://192.168.1.20:8443/x") in run_asgi(mw, scope("192.168.1.5", "/x"))[0]["headers"]
    mgr.redirecting = lambda: False                                                 # switch off: everything passes
    reached.clear()
    run_asgi(mw, scope("192.168.1.5", "/x"))
    assert reached == ["/x"]


class FakeDb:
    def __init__(self):
        self.meta = {}

    def get_meta(self, k, default=None):
        return self.meta.get(k, default)

    def set_meta(self, k, v):
        self.meta[k] = v


def test_no_certificate_is_made_while_the_clock_is_untrusted(tmp_path):
    cfg = Settings(data_dir=tmp_path, https_port=free_port(), host="127.0.0.1", hostname="shellypi")
    clock = SimpleNamespace(trusted=False, now=lambda: 5.0)
    db = FakeDb()
    mgr = HttpsManager(cfg, lambda: db, lambda: clock, addresses=lambda: ["192.168.1.20"])

    async def main():
        await mgr.reconcile()
        assert not mgr.front.running and "clock" in mgr.last_error.lower()
        assert not mgr.store.has_ca()
        clock.trusted, clock.now = True, time.time           # the clock gets set: it now works by itself
        await mgr.reconcile()
        assert mgr.front.running and mgr.last_error is None and mgr.store.has_ca()
        assert "192.168.1.20" in mgr.store.server_info().ips
        # the clock goes bad later: the existing certificate keeps being served
        clock.trusted = False
        await mgr.reconcile()
        assert mgr.front.running and mgr.last_error is None
        # an address change is picked up on the next check
        clock.trusted = True
        mgr._addresses = lambda: ["192.168.1.99"]
        await mgr.reconcile()
        assert "192.168.1.99" in mgr.store.server_info().ips and "192.168.1.20" not in mgr.store.server_info().ips
        await mgr.stop()

    asyncio.run(main())
