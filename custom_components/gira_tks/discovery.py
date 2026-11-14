from __future__ import annotations

import asyncio
import logging
import socket
from collections.abc import Callable
from dataclasses import dataclass

_LOGGER = logging.getLogger(__name__)

DISCOVERY_PORT = 31337
DISCOVERY_TIMEOUT = 1.5
PACKET_BYTES = 60
REQUEST_MARKER = 0x01
REPLY_MARKER = 0xF0
SEARCH_REQUEST = bytes([REQUEST_MARKER]) + bytes(PACKET_BYTES - 1)

_SEARCH_LOCK = asyncio.Lock()


@dataclass(frozen=True)
class DiscoveredGateway:
    host: str
    mac: str
    name: str
    dhcp: bool
    netmask: str
    router: str
    nameserver: str


def parse_reply(data: bytes) -> DiscoveredGateway | None:
    if len(data) != PACKET_BYTES or data[0] != REPLY_MARKER:
        return None
    name = data[30:].split(b"\0", 1)[0].decode("latin-1").strip()
    return DiscoveredGateway(
        host=socket.inet_ntoa(data[14:18]),
        mac=data[7:13].hex(":"),
        name=name,
        dhcp=bool(data[13]),
        netmask=socket.inet_ntoa(data[18:22]),
        router=socket.inet_ntoa(data[22:26]),
        nameserver=socket.inet_ntoa(data[26:30]),
    )


class _DiscoveryProtocol(asyncio.DatagramProtocol):

    def __init__(self, stop_when: Callable[[DiscoveredGateway], bool] | None) -> None:
        self.found: dict[str, DiscoveredGateway] = {}
        self.done = asyncio.Event()
        self._stop_when = stop_when

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        gateway = parse_reply(data)
        if gateway is None:
            return
        if gateway.host != addr[0]:
            _LOGGER.debug("Ignoring Gira TKS search reply from %s claiming address %s", addr[0], gateway.host)
            return
        self.found.setdefault(gateway.mac, gateway)
        if self._stop_when is not None and self._stop_when(gateway):
            self.done.set()


async def async_discover_gateways(
    timeout: float = DISCOVERY_TIMEOUT,
    target: str = "255.255.255.255",
    local_port: int = DISCOVERY_PORT,
    target_port: int = DISCOVERY_PORT,
    local_host: str = "0.0.0.0",
    stop_when: Callable[[DiscoveredGateway], bool] | None = None,
) -> list[DiscoveredGateway]:
    async with _SEARCH_LOCK:
        loop = asyncio.get_running_loop()
        try:
            transport, protocol = await loop.create_datagram_endpoint(
                lambda: _DiscoveryProtocol(stop_when), local_addr=(local_host, local_port),
                allow_broadcast=True,
            )
        except OSError as err:
            _LOGGER.debug("Gira TKS gateway search unavailable: UDP port %s (%s)", local_port, err)
            return []
        try:
            transport.sendto(SEARCH_REQUEST, (target, target_port))
            try:
                await asyncio.wait_for(protocol.done.wait(), timeout)
            except TimeoutError:
                pass
        except OSError as err:
            _LOGGER.debug("Gira TKS gateway search failed: %s", err)
        finally:
            transport.close()
            await asyncio.sleep(0)
        return list(protocol.found.values())
