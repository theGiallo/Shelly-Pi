"""Finding plugs on a router's network: mDNS, the subnet list, and the router-mode settings."""

from __future__ import annotations

import socket
import struct
import threading

from app import lan


def _enc(name: str) -> bytes:
    return b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\x00"


def rr(name: str, rtype: int, rdata: bytes) -> bytes:
    return _enc(name) + struct.pack("!HHIH", rtype, 1, 120, len(rdata)) + rdata


def reply(answers: list[bytes], extra: list[bytes] = ()) -> bytes:
    return struct.pack("!HHHHHH", 0, 0x8400, 0, len(answers), 0, len(extra)) + b"".join(answers) + b"".join(extra)


SERVICE = "_shelly._tcp.local"
INSTANCE = "shellyplugmg3-aabbccddeeff." + SERVICE


def shelly_announcement(ip: str = "192.168.1.77") -> bytes:
    ptr = rr(SERVICE, lan.T_PTR, _enc(INSTANCE))
    srv = rr(INSTANCE, lan.T_SRV, struct.pack("!HHH", 0, 0, 80) + _enc("shellyplugmg3-aabbccddeeff.local"))
    a = rr("shellyplugmg3-aabbccddeeff.local", lan.T_A, socket.inet_aton(ip))
    return reply([ptr], [srv, a])


def test_query_round_trips_and_names_are_checked():
    q = lan.build_query([("_shelly._tcp.local", lan.T_PTR)])
    assert q[:12] == struct.pack("!HHHHHH", 0, 0, 1, 0, 0, 0)
    assert q.endswith(struct.pack("!HH", lan.T_PTR, 0x8001))
    try:
        lan.build_query([("a" * 70 + ".local", lan.T_A)])
    except ValueError:
        pass
    else:
        raise AssertionError("a 70 character label must be refused")


def test_parse_reads_ptr_srv_and_a_records():
    recs = lan.parse_message(shelly_announcement())
    kinds = {r.rtype: r for r in recs}
    assert kinds[lan.T_PTR].data == INSTANCE
    assert kinds[lan.T_SRV].data == (80, "shellyplugmg3-aabbccddeeff.local")
    assert kinds[lan.T_A].data == "192.168.1.77"


def test_parse_follows_compression_and_survives_garbage():
    # name pointing back at offset 12, where the question name sits
    q = b"\x00\x00\x84\x00\x00\x01\x00\x01\x00\x00\x00\x00" + _enc("x.local") + struct.pack("!HH", 1, 1)
    ans = b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 60, 4) + socket.inet_aton("10.0.0.5")
    recs = lan.parse_message(q + ans)
    assert [(r.name, r.data) for r in recs] == [("x.local", "10.0.0.5")]
    loop = b"\x00" * 6 + b"\x00\x01\x00\x00\x00\x00" + b"\xc0\x0c" * 3
    assert lan.parse_message(loop) == []
    assert lan.parse_message(b"short") == []
    assert lan.parse_message(shelly_announcement()[:-5]) is not None


def test_scan_addresses_skips_own_address_pi_wifi_and_wide_networks():
    nets = [("eth0", "192.168.1.10", "255.255.255.0"), ("wlan0", "192.168.50.1", "255.255.255.0")]
    addrs = lan.scan_addresses(nets, skip_nets=["192.168.50.1/24"])
    assert "192.168.1.10" not in addrs and "192.168.1.1" in addrs and len(addrs) == 253
    assert not any(a.startswith("192.168.50.") for a in addrs)
    wide = lan.scan_addresses([("eth0", "10.1.2.3", "255.0.0.0")])
    assert len(wide) <= lan.MAX_SCAN_HOSTS * 2 and "10.1.2.3" not in wide


class FakeResponder:
    """Stands in for the multicast group: answers every question it gets, straight back to the sender."""

    def __init__(self, handler):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.handler = handler
        self.stop = False
        self.sock.settimeout(0.2)
        self.t = threading.Thread(target=self.run, daemon=True)
        self.t.start()

    def run(self):
        while not self.stop:
            try:
                data, src = self.sock.recvfrom(2000)
            except OSError:
                continue
            out = self.handler(data)
            if out:
                self.sock.sendto(out, src)

    def close(self):
        self.stop = True
        self.t.join()
        self.sock.close()


def point_at(monkeypatch, responder):
    monkeypatch.setattr(lan, "MDNS_GROUP", "127.0.0.1")
    monkeypatch.setattr(lan, "MDNS_PORT", responder.port)
    monkeypatch.setattr(lan, "local_networks", lambda: [("lo", "127.0.0.1", "255.0.0.0")])


def test_browse_finds_announcing_plugs(monkeypatch):
    r = FakeResponder(lambda q: shelly_announcement("192.168.1.77"))
    point_at(monkeypatch, r)
    try:
        assert lan._browse_blocking(1.2, []) == ["192.168.1.77"]
    finally:
        r.close()


def test_browse_asks_for_a_missing_address_by_name(monkeypatch):
    ptr = rr(SERVICE, lan.T_PTR, _enc(INSTANCE))
    srv = rr(INSTANCE, lan.T_SRV, struct.pack("!HHH", 0, 0, 80) + _enc("shellyplugmg3-aabbccddeeff.local"))

    def handler(q):
        if b"_shelly" in q:
            return reply([ptr], [srv])                     # no address in the announcement
        return reply([rr("shellyplugmg3-aabbccddeeff.local", lan.T_A, socket.inet_aton("192.168.1.88"))])
    r = FakeResponder(handler)
    point_at(monkeypatch, r)
    try:
        assert lan._browse_blocking(1.5, []) == ["192.168.1.88"]
    finally:
        r.close()


def test_resolve_by_name_and_nothing_when_nobody_answers(monkeypatch):
    name = "shellyplugmg3-aabbccddeeff.local"
    r = FakeResponder(lambda q: reply([rr(name, lan.T_A, socket.inet_aton("192.168.1.99"))]))
    point_at(monkeypatch, r)
    try:
        assert lan._resolve_blocking("ShellyPlugMG3-AABBCCDDEEFF", 1.0, []) == "192.168.1.99"
    finally:
        r.close()
    quiet = FakeResponder(lambda q: None)
    point_at(monkeypatch, quiet)
    try:
        assert lan._resolve_blocking("nobody", 0.6, []) is None
    finally:
        quiet.close()


# ---- the router-mode settings and wizard ---------------------------------------------------------

def test_plugnet_settings_and_provision_url(run_app, settings):
    h = run_app()
    assert h.client.get("/api/plugnet").json()["mode"] == "pi"
    assert h.client.put("/api/plugnet", json={"mode": "router", "ssid": ""}).status_code == 422
    assert h.client.put("/api/plugnet", json={"mode": "router", "ssid": "Home"}).status_code == 422   # needs the password
    assert h.client.put("/api/plugnet", json={"mode": "router", "ssid": "Home", "password": "short"}).status_code == 422
    r = h.client.put("/api/plugnet", json={"mode": "router", "ssid": "Home Net", "password": "sup3r secret"})
    assert r.status_code == 200 and r.json()["mode"] == "router" and r.json()["router_has_password"] is True
    assert "sup3r" not in r.text
    prov = h.client.get("/api/wizard/provision").json()
    assert prov["mode"] == "router" and prov["ssid"] == "Home Net" and prov["password"] == "sup3r secret"
    assert "Home%20Net" in prov["provision_url"] and "ShellyPi" not in prov["provision_url"]
    # saved to config.json, and kept when only switching back and forth
    import json
    saved = json.loads((settings.data_dir / "config.json").read_text())
    assert saved["plug_network"] == "router" and saved["router_ssid"] == "Home Net"
    assert h.client.put("/api/plugnet", json={"mode": "pi"}).json()["mode"] == "pi"
    assert h.client.put("/api/plugnet", json={"mode": "router", "ssid": "Home Net"}).status_code == 200  # password kept
    assert h.client.get("/api/wizard/provision").json()["password"] == "sup3r secret"


def test_a_pi_without_wifi_cannot_choose_its_own_network(run_app, monkeypatch):
    monkeypatch.setenv("SM_NO_AP", "1")
    h = run_app(plug_network="router")
    assert h.client.get("/api/plugnet").json()["pi_wifi"] is False
    assert h.client.put("/api/plugnet", json={"mode": "pi"}).status_code == 409


def test_router_mode_scan_uses_mdns_and_network_scan_only_in_router_mode(run_app, plugs, monkeypatch):
    plug, srv = plugs()
    h = run_app()
    assert h.client.post("/api/wizard/scan-network").status_code == 409
    h.client.put("/api/plugnet", json={"mode": "router", "ssid": "Home", "password": ""})

    async def fake_browse(timeout=3.5, exclude_nets=()):
        return [f"127.0.0.1:{srv.port}", "127.0.0.1:1"]
    monkeypatch.setattr(lan, "browse", fake_browse)
    found = h.client.get("/api/wizard/scan").json()["candidates"]
    assert len(found) == 1 and found[0]["supported"]
    # the subnet scan walks the list it is given
    monkeypatch.setattr(lan, "local_networks", lambda: [("eth0", "127.0.0.1", "255.255.255.255")])
    monkeypatch.setattr(lan, "scan_addresses", lambda nets, skip: [f"127.0.0.1:{srv.port}"])
    found = h.client.post("/api/wizard/scan-network").json()["candidates"]
    assert len(found) == 1
