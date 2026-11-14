from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import logging
import re
import secrets
import socket
import time
from dataclasses import dataclass, field

from aiohttp import web
from homeassistant.components.http import KEY_HASS, HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from . import tks_crypto, tks_video
from .const import (
    DOMAIN,
    EVENT_SIP_MESSAGE,
    EVENT_TKS_RINGING,
    GATEWAY_PREVIEW_DURATION,
    GATEWAY_RING_TIMEOUT,
    VERSION,
    sip_updated_signal,
)

_LOGGER = logging.getLogger(__name__)

USER_AGENT = f"Gira-TKS-Home-Assistant/{VERSION}"
MAX_FU_A_NAL_BYTES = 512 * 1024
PREVIEW_AUTO_RESTART_BLOCK = 60
TKS_STEP_TIMEOUT = 8.0
TKS_RESEND_AFTER = 2.0
TKS_REJECTED_BODY = "0402"
TKS_QUERY_CLOSED_BODY = "0401"
SIP_BIND_RETRIES = 10
SIP_BIND_RETRY_DELAY = 0.3
MEDIA_BIND_RETRIES = 10
MEDIA_BIND_RETRY_DELAY = 0.05
REGISTER_RETRY_DELAY = 30.0
ANSWERED_MEDIA_TIMEOUT = 60.0
ANSWERED_MEDIA_CHECK_INTERVAL = 5.0
STREAM_QUEUE_SIZE = 32


@dataclass
class SipMessage:
    start_line: str
    headers: dict[str, list[str]]
    body: str

    def header(self, name: str, default: str = "") -> str:
        values = self.headers.get(name.lower(), [])
        return values[-1] if values else default


def parse_sip(raw: bytes) -> SipMessage | None:
    text = raw.decode("latin-1", errors="replace")
    head, sep, body = text.partition("\r\n\r\n")
    if not sep:
        head, sep, body = text.partition("\n\n")
    lines = head.replace("\r\n", "\n").split("\n")
    if not lines or not lines[0]:
        return None
    headers: dict[str, list[str]] = {}
    last = None
    for line in lines[1:]:
        if line[:1] in (" ", "\t") and last:
            headers[last][-1] += " " + line.strip()
        elif ":" in line:
            key, value = line.split(":", 1)
            last = key.strip().lower()
            headers.setdefault(last, []).append(value.strip())
    try:
        length = max(0, int(headers.get("content-length", [str(len(body))])[-1]))
        body = body.encode("latin-1")[:length].decode("latin-1")
    except (ValueError, OverflowError):
        pass
    return SipMessage(lines[0], headers, body)


def _digest(challenge: str, username: str, password: str, method: str, uri: str) -> str:
    values = {m.group(1).lower(): (m.group(2) or m.group(3)) for m in re.finditer(r'(\w+)\s*=\s*(?:"([^"]*)"|([^,\s]+))', challenge)}
    realm, nonce = values.get("realm"), values.get("nonce")
    if not realm or not nonce:
        raise ValueError("Unsupported SIP Digest challenge")
    algorithm = values.get("algorithm", "MD5").upper()
    if algorithm not in ("MD5", "MD5-SESS"):
        raise ValueError("Unsupported SIP Digest algorithm")
    qop_values = [v.strip().lower() for v in values.get("qop", "").split(",")]
    qop = "auth" if "auth" in qop_values else ""
    cnonce = secrets.token_hex(12)
    ha1 = hashlib.md5(f"{username}:{realm}:{password}".encode()).hexdigest()
    if algorithm == "MD5-SESS":
        ha1 = hashlib.md5(f"{ha1}:{nonce}:{cnonce}".encode()).hexdigest()
    ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
    nc = "00000001"
    response = hashlib.md5((f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}" if qop else f"{ha1}:{nonce}:{ha2}").encode()).hexdigest()
    fields = [f'username="{username}"', f'realm="{realm}"', f'nonce="{nonce}"', f'uri="{uri}"', f'response="{response}"']
    if "opaque" in values:
        fields.append(f'opaque="{values["opaque"]}"')
    fields.append(f"algorithm={algorithm}")
    if qop:
        fields.extend((f"qop={qop}", f"nc={nc}", f'cnonce="{cnonce}"'))
    return "Digest " + ", ".join(fields)


class H264Rtp(asyncio.DatagramProtocol):

    def __init__(self, payload_type: int | set[int], gateway: str, publish) -> None:
        self.payload_types = {payload_type} if isinstance(payload_type, int) else set(payload_type)
        self.gateway = gateway
        self.publish = publish
        self.packets = 0
        self.fragment_sequence: int | None = None
        self.fragments: bytearray | None = None
        self.last_sequence: int | None = None
        self.sequence_gaps = 0
        self.last_timestamp: int | None = None
        self.timestamp_changes = 0

    def datagram_received(self, data: bytes, addr) -> None:
        if addr[0] != self.gateway:
            return
        if len(data) < 12 or data[0] >> 6 != 2:
            return
        offset = 12 + 4 * (data[0] & 0x0F)
        if data[0] & 0x10:
            if len(data) < offset + 4:
                return
            offset += 4 + int.from_bytes(data[offset + 2:offset + 4], "big") * 4
        if offset >= len(data) or data[1] & 0x7F not in self.payload_types:
            return
        end = len(data) - (data[-1] if data[0] & 0x20 else 0)
        if end <= offset:
            return
        payload = data[offset:end]
        sequence = int.from_bytes(data[2:4], "big")
        timestamp = int.from_bytes(data[4:8], "big")
        if self.last_sequence is not None:
            delta = (sequence - self.last_sequence) & 0xFFFF
            if 1 < delta < 0x8000:
                self.sequence_gaps += delta - 1
        self.last_sequence = sequence
        if self.last_timestamp is None or timestamp != self.last_timestamp:
            self.timestamp_changes += 1
        self.last_timestamp = timestamp
        self.packets += 1
        kind = payload[0] & 0x1F
        start = b"\x00\x00\x00\x01"
        marker = bool(data[1] & 0x80)  # last packet of the access unit (RFC 6184)
        if kind != 28:
            self.fragments = None
        if 1 <= kind <= 23:
            self.publish(start + payload, timestamp, marker)
        elif kind == 24:
            pos = 1
            nals = []
            while pos + 2 <= len(payload):
                size = int.from_bytes(payload[pos:pos + 2], "big")
                pos += 2
                if size == 0 or pos + size > len(payload):
                    break
                nals.append(start + payload[pos:pos + size])
                pos += size
            for index, nal in enumerate(nals):
                self.publish(nal, timestamp, marker and index == len(nals) - 1)
        elif kind == 28 and len(payload) >= 2:
            header = payload[1]
            nal_header = bytes(((payload[0] & 0xE0) | (header & 0x1F),))
            if header & 0x80:
                self.fragments = bytearray(nal_header + payload[2:])
                self.fragment_sequence = sequence
                if header & 0x40:  # start and end in one packet
                    self.publish(start + bytes(self.fragments), timestamp, marker)
                    self.fragments = None
            elif self.fragments is not None:
                if self.fragment_sequence is None or sequence != (self.fragment_sequence + 1) & 0xFFFF:
                    self.fragments = None
                    return
                self.fragment_sequence = sequence
                self.fragments.extend(payload[2:])
                if len(self.fragments) > MAX_FU_A_NAL_BYTES:
                    self.fragments = None
                    return
                if header & 0x40:
                    self.publish(start + bytes(self.fragments), timestamp, marker)
                    self.fragments = None


@dataclass
class SipRuntime:
    status: str = "starting"
    call_state: str = "idle"
    caller: str = ""
    video_packets: int = 0
    video_sequence_gaps: int = 0
    video_timestamp_changes: int = 0
    video_sps_count: int = 0
    video_pps_count: int = 0
    video_idr_count: int = 0
    video_last_nal_type: int | None = None
    stream_clients: int = 0
    stream_inactive_requests: int = 0
    stream_queue_drops: int = 0
    stream_bytes_sent: int = 0
    last_message: dict = field(default_factory=dict)


@dataclass
class SipPreviewDialog:

    request_uri: str
    call_id: str
    local_tag: str
    branch: str
    from_header: str
    to_header: str
    cseq: int = 1
    remote_to: str = ""
    remote_target: str = ""
    accepted: bool = False
    invite_pending: bool = True


def _stream_manager(request: web.Request, entry_id: str, token: str) -> SipClient:
    manager: SipClient | None = request.app[KEY_HASS].data.get(DOMAIN, {}).get(entry_id, {}).get("sip")
    if manager is None or not secrets.compare_digest(
        manager.stream_token.encode("utf-8"), token.encode("utf-8", errors="replace")
    ):
        raise web.HTTPNotFound()
    return manager


def _reject_inactive(manager: SipClient) -> None:
    manager.runtime.stream_inactive_requests += 1
    manager._changed()
    _LOGGER.debug("TKS H.264 stream request rejected: no active media (call state: %s)", manager.runtime.call_state)
    raise web.HTTPServiceUnavailable(text="TKS call has no active H.264 media")


class LiveTsView(HomeAssistantView):

    requires_auth = False
    name = "api:gira_tks:live_ts"
    url = "/api/gira_tks/{entry_id}/{token}/live.ts"

    async def get(self, request: web.Request, entry_id: str, token: str) -> web.StreamResponse:
        manager = _stream_manager(request, entry_id, token)
        if not manager.media_active:
            _reject_inactive(manager)
        return await _serve_ts(manager, request)


class PreviewTsView(HomeAssistantView):

    requires_auth = False
    name = "api:gira_tks:preview_ts"
    url = "/api/gira_tks/{entry_id}/{token}/preview.ts"

    async def get(self, request: web.Request, entry_id: str, token: str) -> web.StreamResponse:
        manager = _stream_manager(request, entry_id, token)
        if not await manager.ensure_preview_for_viewer():
            _reject_inactive(manager)
        return await _serve_ts(manager, request)


async def _serve_ts(manager: SipClient, request: web.Request) -> web.StreamResponse:
    """MPEG-TS with presentation timestamps, so consumers can play evenly."""
    response = web.StreamResponse(headers={"Content-Type": "video/mp2t", "Cache-Control": "no-store"})
    await response.prepare(request)
    channel = manager.ts_channel
    queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=STREAM_QUEUE_SIZE)
    channel.clients.add(queue)
    manager.runtime.stream_clients = manager.viewer_count
    manager._changed()
    _LOGGER.info("TKS MPEG-TS stream opened (media active: %s, viewers: %d)", manager.media_active, manager.viewer_count)
    try:
        for chunk in tuple(channel.buffer):
            await response.write(chunk)
            manager.runtime.stream_bytes_sent += len(chunk)
        while manager.media_active:
            chunk = await queue.get()
            if not chunk:
                break
            await response.write(chunk)
            manager.runtime.stream_bytes_sent += len(chunk)
    except ConnectionResetError:
        pass
    except asyncio.CancelledError:
        raise
    finally:
        channel.clients.discard(queue)
        channel.resync.discard(queue)
        manager.runtime.stream_clients = manager.viewer_count
        manager._changed()
        _LOGGER.info("TKS MPEG-TS stream closed (viewers: %d, bytes sent: %d)", manager.viewer_count, manager.runtime.stream_bytes_sent)
    return response


def _consume_task_exception(task: asyncio.Task) -> None:
    if not task.cancelled():
        task.exception()


class SipClient(asyncio.DatagramProtocol):

    def __init__(self, hass: HomeAssistant, entry_id: str, gateway: str, username: str, password: str,
                 sip_port: int, media_port: int, auto_answer: bool, local_ip: str | None = None) -> None:
        self.hass, self.entry_id = hass, entry_id
        self.gateway, self.username, self.password = gateway, username, password
        self.sip_port, self.media_port, self.auto_answer = sip_port, media_port, auto_answer
        if local_ip:
            self.local_ip = str(ipaddress.IPv4Address(local_ip))
        else:
            route = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                route.connect((gateway, 5060))
                self.local_ip = route.getsockname()[0]
            finally:
                route.close()
        self.transport: asyncio.DatagramTransport | None = None
        self.media_transport: asyncio.DatagramTransport | None = None
        self.media_protocol: H264Rtp | None = None
        self.queue: asyncio.Queue[tuple[bytes, tuple[str, int]]] = asyncio.Queue()
        self.runtime = SipRuntime()
        self.call: tuple[SipMessage, tuple[str, int], int | None, str] | None = None
        self.preview_dialog: SipPreviewDialog | None = None
        self._response_waiters: dict[tuple[int, str], asyncio.Future[SipMessage]] = {}
        self._preview_task: asyncio.Task | None = None
        self._preview_lock = asyncio.Lock()
        self.stream_token = secrets.token_urlsafe(24)
        self.ts_channel = tks_video.StreamChannel(STREAM_QUEUE_SIZE)
        self._ts_muxer = tks_video.MpegTsMuxer()
        self._frame_clock = tks_video.FrameClock()
        self._access_unit: list[bytes] = []
        self._access_unit_prefix: list[bytes] = []
        self._access_unit_timestamp: int | None = None
        self.video_fmtp = ""
        self.media_lines: tuple[list[str], ...] = (["video", "0", "RTP/AVP", "96"],)
        self.sps_pps = b""
        self._sps = b""
        self._pps = b""
        self._last_packet_report = 0
        self._last_packet_event = 0.0
        self.media_active = False
        self._task: asyncio.Task | None = None
        self._from_tag = secrets.token_hex(6)
        self._call_id = f"{secrets.token_hex(12)}@{self.local_ip}"
        self._cseq = 0
        self._registered = False
        self._last_response: SipMessage | None = None
        self._ring_task: asyncio.Task | None = None
        self._media_watchdog_task: asyncio.Task | None = None
        self._restart_lock = asyncio.Lock()
        self._call_last_reply: tuple[int, str, str, str] | None = None
        self._preview_auto_block_until = 0.0
        self._preview_start_task: asyncio.Task | None = None
        self._tks_handshake: dict | None = None
        self._tks_lock = asyncio.Lock()
        self._tks_call_id = ""
        self._tks_tag = ""
        self._tks_cseq = 0
        self._transport_closed: asyncio.Future | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport
        self._transport_closed = asyncio.get_running_loop().create_future()

    def connection_lost(self, exc: Exception | None) -> None:
        if self._transport_closed and not self._transport_closed.done():
            self._transport_closed.set_result(None)

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if addr[0] != self.gateway:
            _LOGGER.debug("Ignoring SIP datagram from unexpected peer %s", addr[0])
            return
        self.queue.put_nowait((data, addr))

    def send(self, text: str, addr: tuple[str, int] | None = None) -> None:
        if self.transport:
            self.transport.sendto(text.encode("latin-1"), addr or (self.gateway, 5060))

    def _changed(self) -> None:
        async_dispatcher_send(self.hass, sip_updated_signal(self.entry_id))

    @property
    def viewer_count(self) -> int:
        return len(self.ts_channel.clients)

    def _reset_ts_stream(self) -> None:
        self._ts_muxer.reset()
        self._frame_clock.reset()
        self._access_unit = []
        self._access_unit_prefix = []
        self._access_unit_timestamp = None
        self.ts_channel.clear()

    def _collect_access_unit(self, chunk: bytes, rtp_timestamp: int | None, end_of_frame: bool) -> None:
        """Group NAL units by RTP timestamp and emit one MPEG-TS PES per frame."""
        if rtp_timestamp is None:
            return
        if self._access_unit and rtp_timestamp != self._access_unit_timestamp:
            self._flush_access_unit()
        self._access_unit.append(chunk)
        self._access_unit_timestamp = rtp_timestamp
        if end_of_frame:
            self._flush_access_unit()

    def _flush_access_unit(self) -> None:
        nals, timestamp = self._access_unit, self._access_unit_timestamp
        self._access_unit, self._access_unit_timestamp = [], None
        if not nals or timestamp is None:
            return
        kinds = {nal[4] & 0x1F for nal in nals if len(nal) > 4}
        if not kinds & {1, 2, 3, 4, 5}:
            # Parameter sets / SEI without a picture: carry them into the next frame.
            self._access_unit_prefix.extend(nals)
            return
        nals = self._access_unit_prefix + nals
        self._access_unit_prefix = []
        kinds = {nal[4] & 0x1F for nal in nals if len(nal) > 4}
        keyframe = 5 in kinds
        if keyframe and 7 not in kinds and self.sps_pps:
            nals.insert(0, self.sps_pps)
        pts = self._frame_clock.stamp(timestamp, time.monotonic())
        self.ts_channel.publish(self._ts_muxer.mux(b"".join(nals), pts, keyframe), keyframe)
        self.runtime.stream_queue_drops += self.ts_channel.drops
        self.ts_channel.drops = 0

    def _register(self, cseq: int, auth: str = "", expires: int = 300) -> str:
        uri = f"sip:{self.gateway}:5060"
        account = f"sip:{self.username}@{self.gateway}"
        lines = [f"REGISTER {uri} SIP/2.0", f"Via: SIP/2.0/UDP {self.local_ip}:{self.sip_port};branch=z9hG4bK{secrets.token_hex(8)};rport",
                 "Max-Forwards: 70", f"From: <{account}>;tag={self._from_tag}", f"To: <{account}>", f"Call-ID: {self._call_id}",
                 f"CSeq: {cseq} REGISTER", f"Contact: <sip:{self.username}@{self.local_ip}:{self.sip_port}>;expires={expires}",
                 f"Expires: {expires}", f"User-Agent: {USER_AGENT}", "Content-Length: 0"]
        if auth:
            lines.append(f"Authorization: {auth}")
        return "\r\n".join(lines) + "\r\n\r\n"

    async def _register_once(self) -> int:
        uri = f"sip:{self.gateway}:5060"
        self._cseq += 1
        cseq = self._cseq
        self.send(self._register(cseq))
        response = await self._response(cseq, 8)
        if response.startswith("SIP/2.0 401") or response.startswith("SIP/2.0 407"):
            challenge_msg = self._last_response
            challenge = challenge_msg.header("www-authenticate") or challenge_msg.header("proxy-authenticate")
            auth = _digest(challenge, self.username, self.password, "REGISTER", uri)
            self._cseq += 1
            cseq = self._cseq
            request = self._register(cseq, auth)
            if response.startswith("SIP/2.0 407"):
                request = request.replace("Authorization:", "Proxy-Authorization:")
            self.send(request)
            response = await self._response(cseq, 8)
        if not response.startswith("SIP/2.0 200"):
            raise RuntimeError(f"Gateway SIP registration failed ({response[8:11]})")
        self._registered = True
        self.runtime.status = "registered"
        self._changed()
        try:
            return int(self._last_response.header("expires", "300"))
        except ValueError:
            return 300

    async def _response(self, cseq: int, timeout: float) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            raw, addr = await asyncio.wait_for(self.queue.get(), deadline - time.monotonic())
            msg = parse_sip(raw)
            if msg and msg.start_line.startswith("SIP/2.0"):
                if (re.search(rf"\b{cseq}\s+REGISTER\b", msg.header("cseq"), re.I)
                        and msg.header("call-id") == self._call_id):
                    self._last_response = msg
                    return msg.start_line
                await self._dispatch(msg, addr)
            elif msg:
                await self._dispatch(msg, addr)
        raise TimeoutError("No SIP REGISTER response")

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        for attempt in range(SIP_BIND_RETRIES):
            try:
                self.transport, _ = await loop.create_datagram_endpoint(
                    lambda: self, local_addr=(self.local_ip, self.sip_port))
                break
            except OSError:
                # The previous socket may not be released yet right after stop().
                if attempt == SIP_BIND_RETRIES - 1:
                    raise
                await asyncio.sleep(SIP_BIND_RETRY_DELAY)
        self._task = self.hass.async_create_background_task(self._run(), "gira_tks_sip")

    async def restart(self) -> None:
        async with self._restart_lock:
            _LOGGER.info("Restarting the Gira TKS SIP participant")
            try:
                await self.stop()
                while not self.queue.empty():
                    self.queue.get_nowait()
                self.runtime.status = "starting"
                self._changed()
                await self.start()
            except (OSError, RuntimeError, ValueError) as err:
                self.runtime.status = "error"
                self._changed()
                _LOGGER.warning("Could not restart the Gira TKS SIP participant (%s)", type(err).__name__)

    async def _run(self) -> None:
        while True:
            try:
                expiry = await self._register_once()
                await self._process_queue_until(time.monotonic() + max(60, expiry * 0.7))
                continue
            except asyncio.CancelledError:
                raise
            except Exception as err:
                _LOGGER.warning("Gira TKS SIP connection unavailable: %s", err)
                self._registered = False
                self.runtime.status = "error"
                self._changed()
            await self._process_queue_until(time.monotonic() + REGISTER_RETRY_DELAY)

    async def _process_queue_until(self, deadline: float) -> None:
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                raw, addr = await asyncio.wait_for(self.queue.get(), remaining)
            except TimeoutError:
                return
            msg = parse_sip(raw)
            if msg:
                await self._dispatch(msg, addr)

    async def _dispatch(self, msg: SipMessage, addr: tuple[str, int]) -> None:
        try:
            await self._handle(msg, addr)
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("Error while handling a SIP message from the Gira TKS gateway")

    async def _handle(self, msg: SipMessage, addr: tuple[str, int]) -> None:
        if msg.start_line.startswith("SIP/2.0"):
            if self._tks_handshake is not None and msg.header("call-id") == self._tks_call_id:
                self._tks_response(msg)
                return
            cseq_match = re.match(r"\s*(\d+)\s+(\w+)", msg.header("cseq"))
            if cseq_match:
                key = (int(cseq_match.group(1)), cseq_match.group(2).upper())
                future = self._response_waiters.get(key)
                dialog = self.preview_dialog
                if not dialog or msg.header("call-id") != dialog.call_id:
                    return
                try:
                    code = int(msg.start_line.split()[1])
                except (IndexError, ValueError):
                    code = 0
                if key[1] == "INVITE" and code >= 300:
                    self._send_non2xx_ack(dialog, msg)
                elif (key[1] == "INVITE" and 200 <= code < 300 and dialog.accepted
                        and key[0] == dialog.cseq):
                    self._send_preview_ack(dialog)
                if future and not future.done() and code >= 200:
                    future.set_result(msg)
            return
        method = msg.start_line.split()[0].upper()
        if self._tks_handshake is not None:
            _LOGGER.debug("Gira TKS handshake: incoming %s, Content-Type %r, body length %d, head %r",
                          method, msg.header("content-type") or msg.header("c"),
                          len(msg.body), msg.body[:4])
        if method == "INVITE":
            await self._invite(msg, addr)
        elif method == "BYE":
            call_id = msg.header("call-id")
            incoming_matches = self.call and self.call[0].header("call-id") == call_id
            preview_matches = self.preview_dialog and self.preview_dialog.call_id == call_id
            if incoming_matches or preview_matches:
                self._reply(msg, addr, 200, "OK")
                self._end_call("ended")
            else:
                self._reply(msg, addr, 481, "Call/Transaction Does Not Exist")
        elif method == "CANCEL":
            call_id = msg.header("call-id")
            incoming_matches = self.call and self.call[0].header("call-id") == call_id
            preview_matches = self.preview_dialog and self.preview_dialog.call_id == call_id
            if incoming_matches and self.runtime.call_state != "ringing":
                self._reply(msg, addr, 200, "OK")
            elif incoming_matches or (preview_matches and not self.preview_dialog.accepted):
                self._reply(msg, addr, 200, "OK")
                if incoming_matches and self.call:
                    invite, invite_addr, _payload, _tag = self.call
                    self._reply(invite, invite_addr, 487, "Request Terminated")
                self._end_call("cancelled")
            else:
                self._reply(msg, addr, 481, "Call/Transaction Does Not Exist")
        elif method == "MESSAGE":
            body = msg.body.strip()
            self.runtime.last_message = {"length": len(body), "prefix": body[:8], "received_at": time.time()}
            self.hass.bus.async_fire(EVENT_SIP_MESSAGE, {"entry_id": self.entry_id, **self.runtime.last_message})
            self._changed()
            self._reply(msg, addr, 200, "OK")
            if self._tks_handshake is not None:
                self._tks_incoming(body)
        elif method in ("OPTIONS", "INFO", "UPDATE"):
            self._reply(msg, addr, 200, "OK")
        elif method == "ACK":
            return
        else:
            self._reply(msg, addr, 501, "Not Implemented")

    async def _invite(self, msg: SipMessage, addr: tuple[str, int]) -> None:
        video_pt = None
        for line in msg.body.replace("\r", "").split("\n"):
            if line.startswith("m=video "):
                try:
                    offered_pts = [int(value) for value in line.split()[3:]]
                    maps = {int(match.group(1)): match.group(2).upper() for match in re.finditer(r"(?m)^a=rtpmap:(\d+)\s+([^/\s]+)", msg.body)}
                    video_pt = next((pt for pt in offered_pts if maps.get(pt) == "H264"), None)
                except (ValueError, IndexError):
                    pass
                break
        if self.call and self.call[0].header("call-id") == msg.header("call-id"):
            if ";tag=" in msg.header("to").lower():
                if self.runtime.call_state == "answered" and self.call[2] is not None:
                    self._reply(msg, addr, 200, "OK", self._video_sdp(self.call[2]), "application/sdp")
                else:
                    self._reply(msg, addr, 491, "Request Pending")
            elif self._call_last_reply:
                code, reason, body, content_type = self._call_last_reply
                self._reply(self.call[0], addr, code, reason, body, content_type)
            return
        if self.call:
            self._reply(msg, addr, 486, "Busy Here")
            return
        if self.preview_dialog:
            self._abort_preview("preview_interrupted_by_call")
        parsed_media_lines = tuple(
            line[2:].split()
            for line in msg.body.replace("\r", "").split("\n")
            if line.startswith("m=") and line[2:].split()
        )
        self.media_lines = parsed_media_lines or (
            (["video", "0", "RTP/AVP", str(video_pt)],) if video_pt is not None else ()
        )
        fmtp_match = re.search(rf"(?m)^a=fmtp:{video_pt}\s+(.+)$", msg.body) if video_pt is not None else None
        self.video_fmtp = ""
        self.sps_pps = b""
        if fmtp_match:
            fmtp_parts = [part.strip() for part in fmtp_match.group(1).split(";") if part.strip()]
            for part in fmtp_parts:
                if part.lower().startswith("sprop-parameter-sets="):
                    for encoded in part.split("=", 1)[1].split(","):
                        try:
                            nal = base64.b64decode(encoded, validate=True)
                        except (ValueError, base64.binascii.Error):
                            continue
                        self.sps_pps += b"\x00\x00\x00\x01" + nal
            self.video_fmtp = ";".join(part for part in fmtp_parts if not part.lower().startswith("sprop-parameter-sets="))
        self._reset_video_stats()
        tag = secrets.token_hex(6)
        self.call = (msg, addr, video_pt, tag)
        self._call_last_reply = None
        self.runtime.caller = "gateway"
        self.runtime.call_state = "ringing"
        self._reply(msg, addr, 100, "Trying")
        video_available = False
        if video_pt is not None:
            try:
                await self._open_media({video_pt, 103})
            except OSError as err:
                _LOGGER.warning("Could not bind TKS video RTP port (%s)", type(err).__name__)
                self.call = (msg, addr, None, tag)
                self._reply(msg, addr, 180, "Ringing")
            else:
                self.media_active = True
                self._reply(msg, addr, 183, "Session Progress", self._video_sdp(video_pt), "application/sdp")
                _LOGGER.info("TKS H.264 media negotiated (payload type %d, RTP port %d)", video_pt, self.media_port)
                video_available = True
        else:
            self._reply(msg, addr, 180, "Ringing")
        self._changed()
        self.hass.bus.async_fire(
            EVENT_TKS_RINGING,
            {
                "entry_id": self.entry_id,
                "call_state": "ringing",
                "signal": "incoming_sip_invite",
                "origin": "unknown",
                "origin_confidence": "not_identified",
                "video_available": video_available,
            },
        )
        self._ring_task = self.hass.async_create_background_task(
            self._ring_timeout(msg), "gira_tks_ring_timeout")
        if self.auto_answer and video_available:
            await self.answer()

    async def _open_media(self, payload_types: set[int]) -> None:
        loop = asyncio.get_running_loop()
        for attempt in range(MEDIA_BIND_RETRIES):
            try:
                self.media_transport, self.media_protocol = await loop.create_datagram_endpoint(
                    lambda: H264Rtp(payload_types, self.gateway, self._publish),
                    local_addr=(self.local_ip, self.media_port),
                )
                return
            except OSError:
                if attempt == MEDIA_BIND_RETRIES - 1:
                    raise
                await asyncio.sleep(MEDIA_BIND_RETRY_DELAY)

    async def _ring_timeout(self, original: SipMessage) -> None:
        await asyncio.sleep(GATEWAY_RING_TIMEOUT)
        if self.call and self.call[0] is original and self.runtime.call_state == "ringing":
            self._reply(original, self.call[1], 486, "Busy Here")
            self._end_call("missed")

    async def answer(self) -> None:
        if not self.call or self.runtime.call_state != "ringing":
            return
        msg, addr, payload, _tag = self.call
        if payload is None or not self.media_active:
            self._reply(msg, addr, 488, "Not Acceptable Here")
            self._end_call("video_unavailable")
            return
        self._reply(msg, addr, 200, "OK", self._video_sdp(payload), "application/sdp")
        self.runtime.call_state = "answered"
        self._changed()
        self._media_watchdog_task = self.hass.async_create_background_task(
            self._answered_media_watchdog(msg), "gira_tks_media_watchdog")

    async def _answered_media_watchdog(self, original: SipMessage) -> None:
        last_packets = -1
        last_change = time.monotonic()
        while True:
            await asyncio.sleep(ANSWERED_MEDIA_CHECK_INTERVAL)
            if not self.call or self.call[0] is not original or self.runtime.call_state != "answered":
                return
            packets = self.media_protocol.packets if self.media_protocol else 0
            now = time.monotonic()
            if packets != last_packets:
                last_packets, last_change = packets, now
            elif now - last_change >= ANSWERED_MEDIA_TIMEOUT:
                _LOGGER.info("Ending answered TKS call: no video packets for %.0f s", ANSWERED_MEDIA_TIMEOUT)
                self._send_call_bye()
                self._end_call("media_timeout")
                return

    async def decline(self) -> None:
        if self.preview_dialog:
            await self.stop_camera_preview()
            return
        if self.call and self.runtime.call_state == "ringing":
            msg, addr, _payload, _tag = self.call
            self._reply(msg, addr, 486, "Busy Here")
            self._end_call("declined")
        elif self.call and self.runtime.call_state == "answered":
            self._send_call_bye()
            self._end_call("ended_local")

    def _send_call_bye(self) -> None:
        if not self.call:
            return
        msg, addr, _payload, tag = self.call
        target = re.search(r"<([^>]+)>", msg.header("contact"))
        uri = target.group(1) if target else f"sip:{self.gateway}"
        local_to = msg.header("to")
        if ";tag=" not in local_to.lower():
            local_to += f";tag={tag}"
        bye = (f"BYE {uri} SIP/2.0\r\nVia: SIP/2.0/UDP {self.local_ip}:{self.sip_port};branch=z9hG4bK{secrets.token_hex(8)};rport\r\n"
               f"Max-Forwards: 70\r\nFrom: {local_to}\r\nTo: {msg.header('from')}\r\n"
               f"Call-ID: {msg.header('call-id')}\r\nCSeq: 2 BYE\r\nContent-Length: 0\r\n\r\n")
        self.send(bye, addr)

    async def send_tks_door_opener(self) -> None:
        await self._send_tks_command(tks_crypto.DOOR_OPENER, "door opener")

    async def send_tks_light(self) -> None:
        await self._send_tks_command(tks_crypto.LIGHT, "light")

    async def send_tks_switching_action(self) -> None:
        """Trigger the switching action on function key 1, like the TKS Communicator.

        The Communicator first queries the participant's switching action, then
        sends the trigger. The gateway answers 01 02 00 (rejected) unless a
        switching action is assigned to the participant's function key.
        """
        async with self._tks_lock:
            listing = await self._tks_message_exchange(tks_crypto.SWITCH_LIST_QUERY, query=True)
            _LOGGER.debug("Gira TKS switching action list for this participant: %s",
                          listing.rstrip(b"\0")[:64].hex(" "))
            try:
                await self._tks_message_exchange(tks_crypto.FUNCTION_KEY_SWITCHING)
            except tks_crypto.CommandRejected as err:
                raise tks_crypto.CommandRejected(
                    "Gateway rejected the switching action (01 02 00): no switching action is "
                    "assigned to function key 1 of this TKS-Communicator participant in the "
                    "Gira TKS Assistant"
                ) from err
        _LOGGER.info("Gira TKS switching action command acknowledged by the gateway")

    async def _send_tks_command(self, command: bytes, label: str) -> None:
        async with self._tks_lock:
            await self._tks_message_exchange(command)
        _LOGGER.info("Gira TKS %s command acknowledged by the gateway", label)

    async def _tks_message_exchange(self, command: bytes, query: bool = False):
        if self.runtime.status != "registered":
            raise RuntimeError("SIP is not registered")
        loop = asyncio.get_running_loop()
        key0 = tks_crypto.message_key(self.password)
        challenge = tks_crypto.make_challenge()
        self._tks_call_id = f"{secrets.token_hex(12)}@{self.local_ip}"
        self._tks_tag = secrets.token_hex(6)
        self._tks_cseq = 0
        self._tks_handshake = {
            "k1": tks_crypto.next_stage_key(challenge),
            "k2": None,
            "reply02": loop.create_future(),
            "ack03": loop.create_future(),
            "last_error": None,
            "answered": set(),
            "query": query,
        }
        try:
            sent = self._send_tks_message(tks_crypto.encode_body(0x01, tks_crypto.encrypt_block(challenge, key0)))
            try:
                k2 = await self._tks_wait("reply02", "02 reply", sent, self._tks_cseq)
                self._tks_handshake["k2"] = k2
                block = tks_crypto.command_block(command)
                sent = self._send_tks_message(tks_crypto.encode_body(0x03, tks_crypto.encrypt_block(block, k2)))
                return await self._tks_wait("ack03", "03 acknowledgement", sent, self._tks_cseq)
            finally:
                # Always close the exchange, also when it is aborted after the 01,
                # so the gateway does not keep waiting on a half-open handshake.
                self._send_tks_message("04")
        finally:
            self._tks_handshake = None

    async def _tks_wait(self, slot: str, label: str, resend: str = "", cseq: int = 0):
        handshake = self._tks_handshake
        assert handshake is not None
        future = handshake[slot]
        deadline = time.monotonic() + TKS_STEP_TIMEOUT
        try:
            if resend and TKS_RESEND_AFTER < TKS_STEP_TIMEOUT:
                await asyncio.wait((future,), timeout=TKS_RESEND_AFTER)
                if (not future.done() and not handshake.get("last_error")
                        and cseq not in handshake["answered"]):
                    _LOGGER.debug("Gira TKS %s missing, resending the MESSAGE once", label)
                    self.send(resend)
            result = await asyncio.wait_for(future, max(0.0, deadline - time.monotonic()))
        except TimeoutError as err:
            detail = handshake.get("last_error")
            if detail:
                raise tks_crypto.HandshakeError(detail) from err
            raise TimeoutError(
                f"No {label} from the gateway within {TKS_STEP_TIMEOUT:.0f} s "
                "(check SIP account/registration)"
            ) from err
        if isinstance(result, Exception):
            raise result
        return result

    def _tks_incoming(self, body: str) -> None:
        handshake = self._tks_handshake
        if handshake is None:
            return
        if body.upper() == TKS_REJECTED_BODY:
            # Observed on firmware 05.04.00.08 instead of the 02 reply while the
            # same participant was also logged in with the TKS Communicator.
            error = tks_crypto.HandshakeError(
                "Gateway answered 0402 instead of a 02 reply; is the SIP participant "
                "also logged in elsewhere (e.g. the TKS Communicator)?"
            )
            handshake["last_error"] = str(error)
            _LOGGER.warning("%s", error)
            for slot in ("reply02", "ack03"):
                if not handshake[slot].done():
                    handshake[slot].set_result(error)
            return
        if (body.upper() == TKS_QUERY_CLOSED_BODY and handshake.get("query") and handshake["k2"]
                and not handshake["ack03"].done()):
            handshake["ack03"].set_result(b"")
            return
        if len(body) != 2 + 2 * tks_crypto.BLOCK_BYTES:
            _LOGGER.debug("Gira TKS MESSAGE during handshake with unexpected body: length %d, "
                          "head %r, tail %r", len(body), body[:6], body[-6:])
            return
        kind = body[:2].upper()
        try:
            if kind == "02" and not handshake["reply02"].done():
                handshake["reply02"].set_result(tks_crypto.stage3_key_from_reply(body, handshake["k1"]))
            elif kind == "03" and handshake["k2"] and not handshake["ack03"].done():
                if handshake.get("query"):
                    # A query returns data instead of an ACK.
                    plain = tks_crypto.decrypt_block(tks_crypto.decode_body(body, 0x03), handshake["k2"])
                    handshake["ack03"].set_result(plain)
                else:
                    try:
                        tks_crypto.check_ack(body, handshake["k2"])
                    except tks_crypto.CommandRejected as err:
                        handshake["last_error"] = str(err)
                        _LOGGER.warning("%s", err)
                        handshake["ack03"].set_result(err)
                        return
                    except tks_crypto.HandshakeError:
                        plain = tks_crypto.decrypt_block(tks_crypto.decode_body(body, 0x03), handshake["k2"])
                        _LOGGER.debug("Gira TKS 03 reply plaintext: %s", plain.rstrip(b"\0")[:48].hex(" "))
                        raise
                    handshake["ack03"].set_result(True)
        except tks_crypto.HandshakeError as err:
            handshake["last_error"] = str(err)
            _LOGGER.warning("Gira TKS type-%s answer ignored: %s", kind, err)

    def _tks_response(self, msg: SipMessage) -> None:
        parts = msg.start_line.split(None, 2)
        try:
            code = int(parts[1])
        except (IndexError, ValueError):
            code = 0
        cseq = msg.header("cseq").strip()
        if 100 <= code < 300:
            _LOGGER.debug("Gira TKS MESSAGE answered %s (%s)", code, cseq)
            handshake = self._tks_handshake
            number = cseq.split()[0] if cseq else ""
            if handshake is not None and number.isdigit():
                handshake["answered"].add(int(number))
            return
        reason = parts[2] if len(parts) > 2 else ""
        hint = ""
        if code in (401, 407):
            hint = "; the gateway requires authentication for MESSAGE"
        error = RuntimeError(f"Gateway rejected TKS MESSAGE: {code} {reason} ({cseq}){hint}")
        _LOGGER.warning("%s", error)
        handshake = self._tks_handshake
        if handshake is None:
            return
        for slot in ("reply02", "ack03"):
            if not handshake[slot].done():
                handshake[slot].set_result(error)

    def _send_tks_message(self, body: str) -> str:
        self._tks_cseq += 1
        account = f"sip:{self.username}@{self.gateway}"
        lines = [
            f"MESSAGE {account} SIP/2.0",
            f"Via: SIP/2.0/UDP {self.local_ip}:{self.sip_port};branch=z9hG4bK{secrets.token_hex(8)};rport",
            "Max-Forwards: 70",
            f"From: <{account}>;tag={self._tks_tag}",
            f"To: <{account}>",
            f"Call-ID: {self._tks_call_id}",
            f"CSeq: {self._tks_cseq} MESSAGE",
            "Content-Type: text/plain",
            f"Content-Length: {len(body)}",
        ]
        request = "\r\n".join(lines) + "\r\n\r\n" + body
        self.send(request)
        return request

    async def start_camera_preview(self) -> None:
        async with self._preview_lock:
            await self._start_camera_preview()

    async def _start_camera_preview(self) -> None:
        if self.preview_dialog:
            return
        if not self._registered or not self.transport:
            raise RuntimeError("TKS SIP participant is not registered")
        if self.call or self.media_active:
            raise RuntimeError("A TKS call or video session is already active")

        self._reset_video_stats()
        try:
            await self._open_media({103, 114})
        except OSError as err:
            self.runtime.call_state = "preview_port_unavailable"
            self._changed()
            raise RuntimeError("Could not bind the configured H.264 RTP address/port") from err

        request_uri = f"sip:CC-1@{self.gateway}"
        local_tag = secrets.token_hex(8)
        call_id = f"{secrets.token_hex(12)}@{self.local_ip}"
        branch = f"z9hG4bK{secrets.token_hex(8)}"
        from_header = f"<sip:{self.username}@{self.gateway}>;tag={local_tag}"
        to_header = f"<sip:CC-1@{self.gateway}>"
        started_dialog = SipPreviewDialog(request_uri, call_id, local_tag, branch, from_header, to_header)
        self.preview_dialog = started_dialog
        self.media_active = True
        self.runtime.call_state = "preview_starting"
        self.runtime.caller = "camera_preview"
        self._changed()

        try:
            response = await self._send_preview_invite()
            dialog = self.preview_dialog
            if not dialog:
                raise RuntimeError("Camera preview ended during SIP negotiation")
            dialog.invite_pending = False
            code = int(response.start_line.split()[1])
            if code in (401, 407):
                challenge = response.header("www-authenticate") or response.header("proxy-authenticate")
                auth = _digest(challenge, self.username, self.password, "INVITE", request_uri)
                if code == 407:
                    auth = "Proxy-" + auth
                dialog.cseq = 2
                dialog.branch = f"z9hG4bK{secrets.token_hex(8)}"
                dialog.invite_pending = True
                response = await self._send_preview_invite(auth)
                dialog.invite_pending = False
                code = int(response.start_line.split()[1])
            if code < 200 or code >= 300:
                raise RuntimeError(f"Gateway refused camera preview (SIP {code})")
            dialog = self.preview_dialog
            if not dialog:
                raise RuntimeError("Camera preview ended during SIP negotiation")
            dialog.remote_to = response.header("to")
            dialog.remote_target = self._sip_uri(response.header("contact")) or request_uri
            dialog.accepted = True
            self._send_preview_ack(dialog)
            self.runtime.call_state = "preview_active"
            self._changed()
            self._preview_task = self.hass.async_create_background_task(
                self._preview_timeout(), "gira_tks_preview_timeout")
            _LOGGER.info("Gira TKS independent camera preview started (H.264 RTP payload 103/114)")
        except BaseException:
            if self.preview_dialog is started_dialog:
                if started_dialog.invite_pending:
                    self._send_preview_cancel(started_dialog)
                elif started_dialog.accepted:
                    self._send_preview_bye(started_dialog)
                self._end_call("preview_failed")
            raise

    async def ensure_preview_for_viewer(self) -> bool:
        if self._preview_running():
            return True
        task = self._preview_start_task
        if task is None or task.done():
            if self.call or self.media_active:
                return False
            if time.monotonic() < self._preview_auto_block_until:
                return False
            task = self.hass.async_create_background_task(
                self.start_camera_preview(), "gira_tks_preview_start")
            task.add_done_callback(_consume_task_exception)
            self._preview_start_task = task
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            _LOGGER.warning("Could not start TKS SIP camera preview (%s)", type(err).__name__)
            return False
        return self._preview_running()

    def _preview_running(self) -> bool:
        dialog = self.preview_dialog
        return bool(dialog and dialog.accepted and self.media_active)

    def _abort_preview(self, state: str) -> None:
        dialog = self.preview_dialog
        if not dialog:
            return
        if dialog.accepted:
            self._send_preview_bye(dialog)
        else:
            self._send_preview_cancel(dialog)
        for future in self._response_waiters.values():
            if not future.done():
                future.set_exception(RuntimeError("Camera preview interrupted by an incoming call"))
        _LOGGER.info("Gira TKS camera preview stopped for an incoming call")
        self._end_call(state)

    def _preview_bye_request(self, dialog: SipPreviewDialog) -> str:
        return (
            f"BYE {dialog.remote_target or dialog.request_uri} SIP/2.0\r\n"
            f"Via: SIP/2.0/UDP {self.local_ip}:{self.sip_port};branch=z9hG4bK{secrets.token_hex(8)};rport\r\n"
            "Max-Forwards: 70\r\n"
            f"From: {dialog.from_header}\r\nTo: {dialog.remote_to or dialog.to_header}\r\n"
            f"Call-ID: {dialog.call_id}\r\nCSeq: {dialog.cseq + 1} BYE\r\nContent-Length: 0\r\n\r\n"
        )

    def _send_preview_bye(self, dialog: SipPreviewDialog) -> None:
        self.send(self._preview_bye_request(dialog))

    def _send_non2xx_ack(self, dialog: SipPreviewDialog, response: SipMessage) -> None:
        cseq_match = re.match(r"\s*(\d+)", response.header("cseq"))
        via = response.headers.get("via", [""])[0]
        if not cseq_match or not via:
            return
        request = (
            f"ACK {dialog.request_uri} SIP/2.0\r\n"
            f"Via: {via}\r\n"
            "Max-Forwards: 70\r\n"
            f"From: {dialog.from_header}\r\nTo: {response.header('to')}\r\n"
            f"Call-ID: {dialog.call_id}\r\nCSeq: {cseq_match.group(1)} ACK\r\nContent-Length: 0\r\n\r\n"
        )
        self.send(request)

    async def _send_preview_invite(self, auth: str = "") -> SipMessage:
        dialog = self.preview_dialog
        if not dialog:
            raise RuntimeError("No camera preview dialog")
        loop = asyncio.get_running_loop()
        key = (dialog.cseq, "INVITE")
        future: asyncio.Future[SipMessage] = loop.create_future()
        self._response_waiters[key] = future
        body = (
            f"v=0\r\n"
            f"o=amsip 0 0 IN IP4 {self.local_ip}\r\n"
            f"s=talk\r\n"
            f"c=IN IP4 {self.local_ip}\r\n"
            f"t=0 0\r\n"
            f"m=audio 0 RTP/AVP 0 8 101\r\n"
            f"a=rtpmap:0 PCMU/8000\r\n"
            f"a=rtpmap:8 PCMA/8000\r\n"
            f"a=rtpmap:101 telephone-event/8000\r\n"
            f"a=fmtp:101 0-11\r\n"
            f"m=video {self.media_port} RTP/AVP 114\r\n"
            f"a=rtpmap:114 H264/90000\r\n"
            f"a=fmtp:114 profile-level-id=42800c; packetization-mode=1\r\n"
        )
        lines = [
            f"INVITE {dialog.request_uri} SIP/2.0",
            f"Via: SIP/2.0/UDP {self.local_ip}:{self.sip_port};branch={dialog.branch};rport",
            "Max-Forwards: 70",
            f"From: {dialog.from_header}",
            f"To: {dialog.to_header}",
            f"Call-ID: {dialog.call_id}",
            f"CSeq: {dialog.cseq} INVITE",
            f"Contact: <sip:{self.username}@{self.local_ip}:{self.sip_port}>",
            f"User-Agent: {USER_AGENT}",
            "Content-Type: application/sdp",
            f"Content-Length: {len(body.encode('latin-1'))}",
        ]
        if auth:
            header = "Proxy-Authorization" if auth.startswith("Proxy-") else "Authorization"
            if header == "Proxy-Authorization":
                auth = auth.removeprefix("Proxy-")
            lines.append(f"{header}: {auth}")
        self.send("\r\n".join(lines) + "\r\n\r\n" + body)
        try:
            return await asyncio.wait_for(future, 8)
        finally:
            self._response_waiters.pop(key, None)

    def _send_preview_ack(self, dialog: SipPreviewDialog) -> None:
        request = (
            f"ACK {dialog.remote_target} SIP/2.0\r\n"
            f"Via: SIP/2.0/UDP {self.local_ip}:{self.sip_port};branch=z9hG4bK{secrets.token_hex(8)};rport\r\n"
            "Max-Forwards: 70\r\n"
            f"From: {dialog.from_header}\r\nTo: {dialog.remote_to}\r\n"
            f"Call-ID: {dialog.call_id}\r\nCSeq: {dialog.cseq} ACK\r\nContent-Length: 0\r\n\r\n"
        )
        self.send(request)

    async def stop_camera_preview(self) -> None:
        async with self._preview_lock:
            await self._stop_camera_preview()

    async def _stop_camera_preview(self) -> None:
        dialog = self.preview_dialog
        if not dialog:
            return
        if dialog.accepted:
            request = self._preview_bye_request(dialog)
            try:
                response = await self._send_preview_end(request, dialog.cseq + 1, "BYE")
                if not response.start_line.startswith("SIP/2.0 200"):
                    _LOGGER.debug("Gateway did not confirm camera preview BYE")
            except (TimeoutError, OSError, RuntimeError) as err:
                _LOGGER.debug("No camera preview BYE response (%s)", type(err).__name__)
            if self.preview_dialog is dialog:
                self._end_call("preview_ended")
                _LOGGER.info("Gira TKS independent camera preview stopped")
        else:
            self._send_preview_cancel(dialog)
            self._end_call("preview_cancelled")

    async def _send_preview_end(self, request: str, cseq: int, method: str) -> SipMessage:
        loop = asyncio.get_running_loop()
        key = (cseq, method)
        future: asyncio.Future[SipMessage] = loop.create_future()
        self._response_waiters[key] = future
        try:
            self.send(request)
            return await asyncio.wait_for(future, 3)
        finally:
            self._response_waiters.pop(key, None)

    def _send_preview_cancel(self, dialog: SipPreviewDialog) -> None:
        cancel = (
            f"CANCEL {dialog.request_uri} SIP/2.0\r\n"
            f"Via: SIP/2.0/UDP {self.local_ip}:{self.sip_port};branch={dialog.branch};rport\r\n"
            "Max-Forwards: 70\r\n"
            f"From: {dialog.from_header}\r\nTo: {dialog.to_header}\r\n"
            f"Call-ID: {dialog.call_id}\r\nCSeq: {dialog.cseq} CANCEL\r\nContent-Length: 0\r\n\r\n"
        )
        self.send(cancel)

    async def _preview_timeout(self) -> None:
        await asyncio.sleep(GATEWAY_PREVIEW_DURATION)
        if self.preview_dialog:
            await self.stop_camera_preview()

    @staticmethod
    def _sip_uri(value: str) -> str:
        match = re.search(r"<([^>]+)>", value)
        return match.group(1) if match else ""

    def _reset_video_stats(self) -> None:
        self.runtime.video_packets = 0
        self.runtime.video_sequence_gaps = 0
        self.runtime.video_timestamp_changes = 0
        self.runtime.video_sps_count = 0
        self.runtime.video_pps_count = 0
        self.runtime.video_idr_count = 0
        self.runtime.video_last_nal_type = None
        self.runtime.stream_queue_drops = 0
        self.runtime.stream_bytes_sent = 0
        self._reset_ts_stream()

    def _video_sdp(self, payload: int) -> str:
        session_id = int(self.call[3], 16) if self.call else 1
        sections = []
        for media_line in self.media_lines:
            media = media_line[0]
            if media == "video":
                fmtp = f"a=fmtp:{payload} {self.video_fmtp}\r\n" if self.video_fmtp else ""
                sections.append(f"m=video {self.media_port} RTP/AVP {payload}\r\na=rtpmap:{payload} H264/90000\r\n{fmtp}a=recvonly\r\n")
            else:
                sections.append(f"m={media} 0 {' '.join(media_line[2:])}\r\n")
        return (f"v=0\r\no=- {session_id} 1 IN IP4 {self.local_ip}\r\n"
                f"s=Gira TKS\r\nc=IN IP4 {self.local_ip}\r\nt=0 0\r\n{''.join(sections)}")

    def _reply(self, req: SipMessage, addr, code: int, reason: str, body: str = "", content_type: str = "") -> None:
        to = req.header("to")
        if req.start_line.startswith("INVITE ") and code not in (100,) and ";tag=" not in to.lower():
            tag = self.call[3] if self.call and self.call[0] is req else secrets.token_hex(6)
            to += f";tag={tag}"
        lines = [f"SIP/2.0 {code} {reason}"]
        lines.extend(f"Via: {v}" for v in req.headers.get("via", []))
        lines += [f"From: {req.header('from')}", f"To: {to}", f"Call-ID: {req.header('call-id')}", f"CSeq: {req.header('cseq')}"]
        if content_type:
            lines.append(f"Content-Type: {content_type}")
        if req.start_line.startswith("INVITE ") and code in (183, 200):
            lines.append(f"Contact: <sip:{self.username}@{self.local_ip}:{self.sip_port}>")
        lines.append(f"Content-Length: {len(body.encode('latin-1'))}")
        if self.call and self.call[0] is req and code >= 180:
            self._call_last_reply = (code, reason, body, content_type)
        self.send("\r\n".join(lines) + "\r\n\r\n" + body, addr)

    def _publish(self, chunk: bytes, rtp_timestamp: int | None = None, end_of_frame: bool = False) -> None:
        nal = chunk[4:] if chunk.startswith(b"\x00\x00\x00\x01") else b""
        if nal:
            kind = nal[0] & 0x1F
            if kind == 7:
                self._sps = chunk
            elif kind == 8:
                self._pps = chunk
            if self._sps and self._pps:
                self.sps_pps = self._sps + self._pps
        self._collect_access_unit(chunk, rtp_timestamp, end_of_frame)
        self.runtime.video_packets = self.media_protocol.packets if self.media_protocol else 0
        if self.media_protocol:
            self.runtime.video_sequence_gaps = self.media_protocol.sequence_gaps
            self.runtime.video_timestamp_changes = self.media_protocol.timestamp_changes
        if nal:
            kind = nal[0] & 0x1F
            self.runtime.video_last_nal_type = kind
            if kind == 7:
                self.runtime.video_sps_count += 1
            elif kind == 8:
                self.runtime.video_pps_count += 1
            elif kind == 5:
                self.runtime.video_idr_count += 1
        now = time.monotonic()
        if self.runtime.video_packets != self._last_packet_report and now - self._last_packet_event >= 1:
            self._last_packet_report = self.runtime.video_packets
            self._last_packet_event = now
            self._changed()

    def _end_call(self, state: str) -> None:
        if self.preview_dialog is not None:
            self._preview_auto_block_until = time.monotonic() + PREVIEW_AUTO_RESTART_BLOCK
        self._call_last_reply = None
        self.runtime.call_state = state
        self.runtime.caller = ""
        self.call = None
        self.preview_dialog = None
        if self._preview_task and self._preview_task is not asyncio.current_task():
            self._preview_task.cancel()
        self._preview_task = None
        self.media_active = False
        if self._ring_task and self._ring_task is not asyncio.current_task():
            self._ring_task.cancel()
        self._ring_task = None
        if self._media_watchdog_task and self._media_watchdog_task is not asyncio.current_task():
            self._media_watchdog_task.cancel()
        self._media_watchdog_task = None
        self.ts_channel.close_all()
        self._reset_ts_stream()
        if self.media_transport:
            self.media_transport.close()
            self.media_transport = None
        self.media_protocol = None
        self.sps_pps = b""
        self._sps = b""
        self._pps = b""
        self._changed()

    async def _unregister(self) -> None:
        if not self._registered or not self.transport:
            return
        uri = f"sip:{self.gateway}:5060"
        self._cseq += 1
        cseq = self._cseq
        self.send(self._register(cseq, expires=0))
        response = await self._response(cseq, 3)
        if response.startswith(("SIP/2.0 401", "SIP/2.0 407")):
            challenge_msg = self._last_response
            challenge = challenge_msg.header("www-authenticate") or challenge_msg.header("proxy-authenticate")
            auth = _digest(challenge, self.username, self.password, "REGISTER", uri)
            self._cseq += 1
            cseq = self._cseq
            request = self._register(cseq, auth, expires=0)
            if response.startswith("SIP/2.0 407"):
                request = request.replace("Authorization:", "Proxy-Authorization:")
            self.send(request)
            await self._response(cseq, 3)
        self._registered = False

    async def stop(self) -> None:
        if self.call:
            await self.decline()
        if self.preview_dialog:
            await self.stop_camera_preview()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        try:
            await self._unregister()
        except (TimeoutError, OSError, ValueError):
            _LOGGER.debug("TKS SIP unregister did not finish; the Gateway will expire it")
        self._end_call("idle")
        if self.transport:
            self.transport.close()
            self.transport = None
            if self._transport_closed is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(self._transport_closed), 2)
                except TimeoutError:
                    _LOGGER.debug("SIP socket close not confirmed within 2 s")
        self._registered = False
        self.runtime.status = "stopped"
        self._changed()
