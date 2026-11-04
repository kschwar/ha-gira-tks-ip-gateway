from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from .protocol import (
    WIRE_BYTES,
    decode_wire_block,
    decrypt_buffer,
    encode_wire_block,
    encrypt_buffer,
    make_auth_key,
    new_challenge,
    parse_record,
    ProtocolError,
)

_LOGGER = logging.getLogger(__name__)

DECODE_WARNING_INTERVAL = 600.0
_PLAUSIBLE_MARKERS = (0x10, 0x11)


class GatewayListener:

    def __init__(
        self,
        username: str,
        password: str,
        gateway_host: str,
        port: int,
        on_record: Callable[[dict[str, Any]], None],
    ) -> None:
        self._auth_key = make_auth_key(username, password)
        self.gateway_host = gateway_host
        self.port = port
        self._on_record = on_record
        self._server: asyncio.Server | None = None
        self._handlers: set[asyncio.Task[Any]] = set()
        self._stopped = False
        self._last_decode_warning: float | None = None
        self.last_record: dict[str, Any] | None = None
        self.last_error: str | None = None
        self.unrecognized_frames = 0

    @property
    def is_running(self) -> bool:
        return self._server is not None and self._server.is_serving()

    async def start(self) -> None:
        self._stopped = False
        self._server = await asyncio.start_server(
            self._handle_client,
            host="0.0.0.0",
            port=self.port,
            reuse_address=True,
        )
        _LOGGER.info("Listening for Gira TKS gateway connections on TCP/%s", self.port)

    async def stop(self) -> None:
        self._stopped = True
        server, self._server = self._server, None
        if server is not None:
            server.close()
        handlers = [task for task in self._handlers if not task.done()]
        for task in handlers:
            task.cancel()
        if handlers:
            await asyncio.gather(*handlers, return_exceptions=True)
        if server is not None:
            await server.wait_closed()

    async def _read_block(self, reader: asyncio.StreamReader) -> bytes:
        wire = await asyncio.wait_for(reader.readexactly(WIRE_BYTES), timeout=5)
        return decode_wire_block(wire)

    def _warn_undecodable(self, peer_ip: str) -> None:
        now = time.monotonic()
        if (
            self._last_decode_warning is not None
            and now - self._last_decode_warning < DECODE_WARNING_INTERVAL
        ):
            return
        self._last_decode_warning = now
        _LOGGER.warning(
            "Could not decrypt a message from the Gira TKS gateway %s. Check that "
            "the user name and password configured for this integration match the "
            "HomeServer connection data in the Gira TKS assistant",
            peer_ip,
        )

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._handlers.add(task)
        peer = writer.get_extra_info("peername")
        peer_ip = str(peer[0]) if peer else "unknown"
        try:
            if self._stopped:
                return
            if peer_ip != self.gateway_host:
                _LOGGER.warning(
                    "Rejected TKS TCP connection from unexpected peer %s", peer_ip
                )
                return

            writer.write(b"\x01")
            await writer.drain()

            encrypted_gateway_challenge = await self._read_block(reader)
            gateway_challenge = decrypt_buffer(
                encrypted_gateway_challenge, self._auth_key
            )

            server_challenge = new_challenge()
            encrypted_server_challenge = encrypt_buffer(
                server_challenge, gateway_challenge[:16]
            )
            writer.write(encode_wire_block(encrypted_server_challenge))
            await writer.drain()

            encrypted_frame = await self._read_block(reader)
            frame = decrypt_buffer(encrypted_frame, server_challenge[:16])
            if self._stopped:
                return
            record = parse_record(frame)
            if record is None:
                self.unrecognized_frames += 1
                self.last_error = "unrecognized_or_checksum_invalid"
                if frame[0] in _PLAUSIBLE_MARKERS:
                    _LOGGER.debug(
                        "Ignored unrecognized or checksum-invalid TKS frame from %s",
                        peer_ip,
                    )
                else:
                    self._warn_undecodable(peer_ip)
                return

            record["source"] = peer_ip
            record["received_at"] = datetime.now(UTC).isoformat()
            self.last_record = record
            self.last_error = None
            self._on_record(record)
        except (
            asyncio.IncompleteReadError,
            asyncio.TimeoutError,
            ProtocolError,
            ValueError,
        ) as err:
            self.last_error = type(err).__name__
            _LOGGER.debug("Incomplete TKS gateway exchange from %s: %s", peer_ip, err)
        except OSError as err:
            self.last_error = type(err).__name__
            _LOGGER.debug("TKS gateway connection from %s failed: %s", peer_ip, err)
        finally:
            if task is not None:
                self._handlers.discard(task)
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
