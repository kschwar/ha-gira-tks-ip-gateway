"""Gateway search on UDP 31337 (format from the Gira TKS-IP-Gateway-Finder).

Synthetic addresses only; the local UDP test runs on 127.0.0.1.
"""
import asyncio
import importlib.util
import pathlib
import socket
import sys

_PATH = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "gira_tks" / "discovery.py"
_SPEC = importlib.util.spec_from_file_location("gira_tks_discovery", _PATH)
discovery = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = discovery
_SPEC.loader.exec_module(discovery)


def _reply(name=b"TKS-IP-GATEWAY", ip=(192, 0, 2, 31), dhcp=1):
    data = bytearray(60)
    data[0] = 0xF0
    data[1:7] = b"\xff" * 6
    data[7:13] = bytes.fromhex("000ab3123456")
    data[13] = dhcp
    data[14:18] = bytes(ip)
    data[18:22] = bytes((255, 255, 255, 0))
    data[22:26] = bytes((192, 0, 2, 1))
    data[26:30] = bytes((192, 0, 2, 10))
    data[30:30 + len(name)] = name
    data[30 + len(name)] = 0
    data[31 + len(name):] = b"\x7e" * (29 - len(name))  # leftover bytes after the name
    return bytes(data)


def test_search_request_is_60_bytes_starting_with_01():
    assert discovery.SEARCH_REQUEST == b"\x01" + bytes(59)


def test_parse_reply_fields():
    gateway = discovery.parse_reply(_reply())
    assert gateway == discovery.DiscoveredGateway(
        host="192.0.2.31", mac="00:0a:b3:12:34:56", name="TKS-IP-GATEWAY", dhcp=True,
        netmask="255.255.255.0", router="192.0.2.1", nameserver="192.0.2.10",
    )


def test_parse_reply_rejects_other_packets():
    assert discovery.parse_reply(discovery.SEARCH_REQUEST) is None
    assert discovery.parse_reply(_reply()[:59]) is None
    assert discovery.parse_reply(b"\x00" + _reply()[1:]) is None


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class _FakeGateway(asyncio.DatagramProtocol):
    def __init__(self, ip=(127, 0, 0, 1)):
        self.requests = []
        self.ip = ip

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.requests.append(data)
        if data == discovery.SEARCH_REQUEST:
            self.transport.sendto(_reply(ip=self.ip), addr)
            self.transport.sendto(_reply(ip=self.ip), addr)  # duplicate answer is reported once
            self.transport.sendto(b"\xf0 short", addr)


async def _search(fake_ip=(127, 0, 0, 1), timeout=0.3, local_port=None, **kwargs):
    gateway_port, local_port = _free_port(), local_port or _free_port()
    loop = asyncio.get_running_loop()
    transport, fake = await loop.create_datagram_endpoint(
        lambda: _FakeGateway(fake_ip), local_addr=("127.0.0.1", gateway_port))
    try:
        found = await discovery.async_discover_gateways(
            timeout=timeout, target="127.0.0.1", local_port=local_port,
            target_port=gateway_port, local_host="127.0.0.1", **kwargs)
    finally:
        transport.close()
    return found, fake.requests


def test_discover_sends_request_and_collects_replies():
    found, requests = asyncio.run(_search())
    assert requests == [discovery.SEARCH_REQUEST]
    assert [(g.name, g.host) for g in found] == [("TKS-IP-GATEWAY", "127.0.0.1")]


def test_reply_claiming_another_address_is_ignored():
    found, _requests = asyncio.run(_search(fake_ip=(192, 0, 2, 66)))
    assert found == []


def test_search_stops_early_when_the_wanted_gateway_answers():
    async def run():
        loop = asyncio.get_running_loop()
        started = loop.time()
        found, _ = await _search(timeout=5.0, stop_when=lambda g: g.mac == "00:0a:b3:12:34:56")
        return found, loop.time() - started

    found, elapsed = asyncio.run(run())
    assert len(found) == 1 and elapsed < 2.0


def test_concurrent_searches_on_the_same_port_both_succeed():
    async def run():
        port = _free_port()
        return await asyncio.gather(_search(timeout=0.2, local_port=port), _search(timeout=0.2, local_port=port))

    results = asyncio.run(run())
    assert [len(found) for found, _ in results] == [1, 1]


def test_discover_returns_empty_when_port_is_busy():
    async def run():
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as busy:
            busy.bind(("127.0.0.1", 0))
            port = busy.getsockname()[1]
            return await discovery.async_discover_gateways(
                timeout=0.1, target="127.0.0.1", local_port=port, local_host="127.0.0.1")

    assert asyncio.run(run()) == []
