"""SipClient behaviour against a simulated gateway (switching handshake, calls, stream).

No network and no Home Assistant: aiohttp/homeassistant are stubbed, the UDP
transport is a fake that hands each sent datagram to an in-memory gateway.
Password, keys and bodies are synthetic.

Run: py -m pytest tests (from the repository root)
"""
import asyncio
import importlib.util
import pathlib
import sys
import types
import unittest
from unittest import mock

_PKG_DIR = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "gira_tks"


def _load_sip():
    """Load sip.py as gira_tks_test.sip with stubbed third-party imports."""
    stubs = {}
    aiohttp = types.ModuleType("aiohttp")
    aiohttp.web = mock.MagicMock()
    stubs["aiohttp"] = aiohttp
    ha = types.ModuleType("homeassistant")
    ha_components = types.ModuleType("homeassistant.components")
    ha_http = types.ModuleType("homeassistant.components.http")
    ha_http.HomeAssistantView = type("HomeAssistantView", (), {})
    ha_http.KEY_HASS = "hass"
    ha_core = types.ModuleType("homeassistant.core")
    ha_core.HomeAssistant = object
    ha_helpers = types.ModuleType("homeassistant.helpers")
    ha_dispatcher = types.ModuleType("homeassistant.helpers.dispatcher")
    ha_dispatcher.async_dispatcher_send = lambda hass, signal, *args: None
    stubs.update({
        "homeassistant": ha, "homeassistant.components": ha_components,
        "homeassistant.components.http": ha_http, "homeassistant.core": ha_core,
        "homeassistant.helpers": ha_helpers, "homeassistant.helpers.dispatcher": ha_dispatcher,
    })
    pkg = types.ModuleType("gira_tks_test")
    pkg.__path__ = [str(_PKG_DIR)]
    modules = {**stubs, pkg.__name__: pkg}
    previous = {name: sys.modules.get(name) for name in modules}
    sys.modules.update(modules)
    try:
        for sub in ("const", "tks_crypto", "sip"):
            spec = importlib.util.spec_from_file_location(f"gira_tks_test.{sub}", _PKG_DIR / f"{sub}.py")
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
        return sys.modules["gira_tks_test.sip"], sys.modules["gira_tks_test.tks_crypto"]
    finally:
        for name, module in previous.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


sip, tks = _load_sip()
GW = "192.0.2.10"
PASSWORD = "synthetic-pass"


class FakeGateway:
    """Answers our TKS MESSAGEs like the gateway does."""

    def __init__(self, client, *, password=PASSWORD, reject_code=None, send_ack=True,
                 ack_prefix=tks.ACK_PREFIX, foreign_02_first=False, drop_first=(),
                 unpadded_reply=False, reject_body=None, replies=None):
        self.client = client
        self.password = password
        self.reject_code = reject_code
        self.send_ack = send_ack
        self.ack_prefix = ack_prefix
        self.foreign_02_first = foreign_02_first
        self.unpadded_reply = unpadded_reply
        self.reject_body = reject_body
        self.replies = replies or {}
        self.commands = []
        self.k2 = None
        self.sent_types = []
        self.command_plain = None
        self.ok_replies = 0
        self.raw_messages = []
        self.other_requests = []
        self.drop_first = set(drop_first)
        self._seq = 0

    # --- transport side -----------------------------------------------------
    def sendto(self, data, addr):
        text = data.decode("latin-1")
        if text.startswith("SIP/2.0 200"):
            self.ok_replies += 1
            return
        msg = sip.parse_sip(data)
        if not msg or not msg.start_line.startswith("MESSAGE"):
            if msg:
                self.other_requests.append(msg.start_line)
            return
        body = msg.body.strip()
        self.sent_types.append(body[:2])
        self.raw_messages.append(data)
        if body[:2] in self.drop_first:
            self.drop_first.discard(body[:2])
            return
        asyncio.get_running_loop().call_soon(self._react, msg, body)

    def _react(self, msg, body):
        if self.reject_code:
            self._respond(msg, self.reject_code, "Unauthorized")
            return
        self._respond(msg, 200, "OK")
        kind = body[:2]
        if kind == "01":
            if self.reject_body:
                self._message(self.reject_body)
                return
            challenge = tks.decrypt_block(bytes.fromhex(body[2:]), tks.message_key(self.password))
            k1 = tks.next_stage_key(challenge)
            if self.foreign_02_first:
                self._message(tks.encode_body(0x02, bytes(range(256))))
            reply = bytes([7, 7, 3]) + bytes(range(100, 113))
            reply += bytes(range(16, 256)) if self.unpadded_reply else bytes(240)
            self.k2 = tks.next_stage_key(reply)
            self._message(tks.encode_body(0x02, tks.encrypt_block(reply, k1)))
        elif kind == "03":
            self.command_plain = tks.decrypt_block(bytes.fromhex(body[2:]), self.k2)
            self.commands.append(self.command_plain.rstrip(b"\0"))
            planned = self.replies.get(self.command_plain.rstrip(b"\0"))
            if planned is not None:
                for plain in planned:
                    self._message(tks.encode_body(0x03, tks.encrypt_block(plain.ljust(256, b"\0"), self.k2)))
                self._message("0401")
            elif self.send_ack:
                ack = self.ack_prefix.ljust(256, b"\0")
                self._message(tks.encode_body(0x03, tks.encrypt_block(ack, self.k2)))

    # --- helpers --------------------------------------------------------------
    def _deliver(self, raw):
        asyncio.get_running_loop().create_task(
            self.client._handle(sip.parse_sip(raw.encode("latin-1")), (GW, 5060)))

    def _respond(self, msg, code, reason):
        headers = "".join(
            f"{name}: {msg.header(name.lower())}\r\n" for name in ("Via", "From", "To", "Call-ID", "CSeq"))
        self._deliver(f"SIP/2.0 {code} {reason}\r\n{headers}Content-Length: 0\r\n\r\n")

    def _message(self, body):
        self._seq += 1
        self._deliver(
            f"MESSAGE sip:user@198.51.100.2:5060 SIP/2.0\r\n"
            f"Via: SIP/2.0/UDP {GW}:5060;branch=z9hG4bKgw{self._seq}\r\n"
            f"From: <sip:user@{GW}>;tag=gw\r\nTo: <sip:user@{GW}>\r\n"
            f"Call-ID: gw{self._seq}@{GW}\r\nCSeq: {self._seq} MESSAGE\r\n"
            f"Content-Type: text/plain\r\nContent-Length: {len(body)}\r\n\r\n{body}")


def make_client(**gateway_kwargs):
    hass = mock.MagicMock()
    client = sip.SipClient(hass, "entry", GW, "user", PASSWORD, 5070, 5072, False, "198.51.100.2")
    client.runtime.status = "registered"
    gateway = FakeGateway(client, **gateway_kwargs)
    client.transport = gateway
    return client, gateway


class TestDoorOpenerHandshake(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patcher = mock.patch.object(sip, "TKS_STEP_TIMEOUT", 0.3)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_happy_path_sends_01_03_04_and_gateway_reads_command(self):
        client, gw = make_client()
        await client.send_tks_door_opener()
        self.assertEqual(gw.sent_types, ["01", "03", "04"])
        self.assertEqual(gw.command_plain, tks.command_block(tks.DOOR_OPENER))
        self.assertIsNone(client._tks_handshake)
        self.assertEqual(gw.ok_replies, 2)  # we acked the gateway's 02 and 03

    async def test_unpadded_reply_completes_exchange(self):
        client, gateway = make_client(unpadded_reply=True)
        await client.send_tks_door_opener()
        self.assertEqual(gateway.command_plain, tks.command_block(tks.DOOR_OPENER))
        self.assertEqual(gateway.sent_types, ["01", "03", "04"])

    async def test_wrong_password_fails_at_acknowledgement(self):
        client, gateway = make_client(password="other-pass")
        with self.assertRaisesRegex(tks.HandshakeError, "acknowledgement"):
            await client.send_tks_door_opener()
        self.assertEqual(gateway.sent_types, ["01", "03", "04"])

    async def test_foreign_reply_fails_at_acknowledgement(self):
        client, gateway = make_client(foreign_02_first=True)
        with self.assertRaisesRegex(tks.HandshakeError, "acknowledgement"):
            await client.send_tks_door_opener()
        self.assertIsNone(client._tks_handshake)
        self.assertEqual(gateway.sent_types, ["01", "03", "04"])

    async def test_gateway_rejection_closes_exchange_before_command(self):
        client, gateway = make_client(reject_body="0402")
        with self.assertRaisesRegex(tks.HandshakeError, "0402"):
            await client.send_tks_door_opener()
        self.assertEqual(gateway.sent_types, ["01", "04"])
        self.assertIsNone(client._tks_handshake)

    async def test_rejection_is_reported_with_code(self):
        client, gw = make_client(reject_code=401)
        with self.assertRaisesRegex(RuntimeError, "401"):
            await client.send_tks_door_opener()
        self.assertEqual(gw.sent_types, ["01", "04"])

    async def test_missing_ack_times_out_but_still_closes_with_04(self):
        client, gw = make_client(send_ack=False)
        with self.assertRaisesRegex(TimeoutError, "03 acknowledgement"):
            await client.send_tks_door_opener()
        self.assertEqual(gw.sent_types, ["01", "03", "04"])

    async def test_unexpected_ack_content_fails(self):
        client, gw = make_client(ack_prefix=bytes([0x09, 0x09, 0x09]))
        with self.assertRaisesRegex(tks.HandshakeError, "acknowledgement"):
            await client.send_tks_door_opener()
        self.assertEqual(gw.sent_types, ["01", "03", "04"])

    async def test_light_sends_light_command(self):
        client, gw = make_client()
        await client.send_tks_light()
        self.assertEqual(gw.command_plain, tks.command_block(tks.LIGHT))

    async def test_concurrent_presses_run_one_after_another(self):
        client, gw = make_client()
        await asyncio.gather(client.send_tks_door_opener(), client.send_tks_light())
        self.assertEqual(gw.sent_types, ["01", "03", "04", "01", "03", "04"])

    async def test_not_registered_sends_nothing(self):
        client, gw = make_client()
        client.runtime.status = "error"
        with self.assertRaises(RuntimeError):
            await client.send_tks_door_opener()
        self.assertEqual(gw.sent_types, [])


class TestSwitchingAction(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patcher = mock.patch.object(sip, "TKS_STEP_TIMEOUT", 0.5)
        patcher.start()
        self.addCleanup(patcher.stop)

    QUERY_REPLY = [bytes([0x02, 0x04, 0x01, 0x00, 0x01])]
    ACK = [tks.ACK_PREFIX]
    REJECT = [tks.REJECT_PREFIX]

    async def test_queries_then_triggers_function_key_1(self):
        client, gw = make_client(replies={
            tks.SWITCH_LIST_QUERY: self.QUERY_REPLY, tks.FUNCTION_KEY_SWITCHING: self.ACK})
        await client.send_tks_switching_action()
        self.assertEqual(gw.commands, [tks.SWITCH_LIST_QUERY, tks.FUNCTION_KEY_SWITCHING])
        self.assertEqual(gw.sent_types, ["01", "03", "04", "01", "03", "04"])
        self.assertIsNone(client._tks_handshake)

    async def test_query_with_several_records_uses_the_first(self):
        records = [bytes([0x02, 0x02, 0x01, 0x00, n]) for n in (1, 2, 3)]
        client, gw = make_client(replies={tks.SWITCH_LIST_QUERY: records})
        async with client._tks_lock:
            listing = await client._tks_message_exchange(tks.SWITCH_LIST_QUERY, query=True)
        self.assertTrue(listing.startswith(records[0]))

    async def test_query_without_records_returns_empty_without_waiting(self):
        client, gw = make_client(replies={tks.SWITCH_LIST_QUERY: []})
        async with client._tks_lock:
            with mock.patch.object(sip, "TKS_STEP_TIMEOUT", 10.0):
                listing = await client._tks_message_exchange(tks.SWITCH_LIST_QUERY, query=True)
        self.assertEqual(listing, b"")

    async def test_unassigned_function_key_fails_fast_with_hint(self):
        client, gw = make_client(replies={
            tks.SWITCH_LIST_QUERY: self.QUERY_REPLY, tks.FUNCTION_KEY_SWITCHING: self.REJECT})
        loop = asyncio.get_running_loop()
        started = loop.time()
        with mock.patch.object(sip, "TKS_STEP_TIMEOUT", 10.0):
            with self.assertRaisesRegex(tks.CommandRejected, "function key 1"):
                await client.send_tks_switching_action()
        self.assertLess(loop.time() - started, 5.0)
        self.assertEqual(gw.sent_types, ["01", "03", "04", "01", "03", "04"])
        self.assertIsNone(client._tks_handshake)

    async def test_rejected_door_opener_reports_rejection(self):
        client, gw = make_client(replies={tks.DOOR_OPENER: self.REJECT})
        with self.assertRaisesRegex(tks.CommandRejected, "01 02 00"):
            await client.send_tks_door_opener()
        self.assertEqual(gw.sent_types, ["01", "03", "04"])

    def test_check_ack_distinguishes_rejection_from_garbage(self):
        k2 = bytes(range(16))
        reject = tks.encode_body(0x03, tks.encrypt_block(tks.REJECT_PREFIX.ljust(256, b"\0"), k2))
        garbage = tks.encode_body(0x03, tks.encrypt_block(bytes([9, 9, 9]).ljust(256, b"\0"), k2))
        with self.assertRaises(tks.CommandRejected):
            tks.check_ack(reject, k2)
        with self.assertRaises(tks.HandshakeError) as ctx:
            tks.check_ack(garbage, k2)
        self.assertNotIsInstance(ctx.exception, tks.CommandRejected)


class TestTksResend(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for name, value in (("TKS_STEP_TIMEOUT", 0.6), ("TKS_RESEND_AFTER", 0.1)):
            patcher = mock.patch.object(sip, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_lost_01_is_resent_once_identically(self):
        client, gw = make_client(drop_first={"01"})
        await client.send_tks_door_opener()
        self.assertEqual(gw.sent_types, ["01", "01", "03", "04"])
        self.assertEqual(gw.raw_messages[0], gw.raw_messages[1])

    async def test_lost_03_is_resent_once_identically(self):
        client, gw = make_client(drop_first={"03"})
        await client.send_tks_light()
        self.assertEqual(gw.sent_types, ["01", "03", "03", "04"])
        self.assertEqual(gw.raw_messages[1], gw.raw_messages[2])
        self.assertEqual(gw.command_plain, tks.command_block(tks.LIGHT))

    async def test_answered_03_is_never_repeated_without_ack(self):
        # The gateway answered 200 OK to our 03 but its ack is missing: the
        # command must not be sent again, or it could switch twice.
        client, gw = make_client(send_ack=False)
        with self.assertRaisesRegex(TimeoutError, "03 acknowledgement"):
            await client.send_tks_door_opener()
        self.assertEqual(gw.sent_types, ["01", "03", "04"])


def _invite(call_id="call1@gw"):
    raw = (f"INVITE sip:user@198.51.100.2:5070 SIP/2.0\r\n"
           f"Via: SIP/2.0/UDP {GW}:5060;branch=z9hG4bKinv\r\n"
           f"From: <sip:gw@{GW}>;tag=gwtag\r\nTo: <sip:user@198.51.100.2>\r\n"
           f"Call-ID: {call_id}\r\nCSeq: 1 INVITE\r\nContact: <sip:gw@{GW}:5060>\r\n"
           f"Content-Length: 0\r\n\r\n")
    return sip.parse_sip(raw.encode("latin-1"))


class TestAnsweredMediaWatchdog(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for name, value in (("ANSWERED_MEDIA_TIMEOUT", 0.15), ("ANSWERED_MEDIA_CHECK_INTERVAL", 0.03)):
            patcher = mock.patch.object(sip, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _answered_client(self):
        client, gw = make_client()
        msg = _invite()
        client.call = (msg, (GW, 5060), 96, "abc123")
        client.runtime.call_state = "answered"
        client.media_protocol = types.SimpleNamespace(packets=0)
        return client, gw, msg

    async def test_silent_answered_call_is_ended_with_bye(self):
        client, gw, msg = self._answered_client()
        await asyncio.wait_for(client._answered_media_watchdog(msg), 2)
        self.assertIsNone(client.call)
        self.assertEqual(client.runtime.call_state, "media_timeout")
        self.assertTrue(any(line.startswith("BYE ") for line in gw.other_requests))

    async def test_call_with_media_stays_answered(self):
        client, gw, msg = self._answered_client()
        task = asyncio.create_task(client._answered_media_watchdog(msg))
        for _ in range(12):
            await asyncio.sleep(0.03)
            client.media_protocol.packets += 1
        self.assertFalse(task.done())
        self.assertEqual(client.runtime.call_state, "answered")
        client.runtime.call_state = "ended"
        await asyncio.wait_for(task, 1)
        self.assertEqual(gw.other_requests, [])


class TestStreamResync(unittest.IsolatedAsyncioTestCase):
    async def test_overflowing_viewer_waits_for_next_idr(self):
        client, _gw = make_client()
        start = b"\x00\x00\x00\x01"
        sps, pps, idr, p_frame = start + b"\x67a", start + b"\x68b", start + b"\x65c", start + b"\x41d"
        queue = asyncio.Queue(maxsize=2)
        client.stream_clients.add(queue)
        for chunk in (sps, pps, idr, p_frame):
            client._publish(chunk)
        self.assertTrue(queue.full())
        client._publish(p_frame)  # overflow: backlog dropped, viewer resyncs
        self.assertTrue(queue.empty())
        self.assertIn(queue, client.stream_resync_clients)
        client._publish(p_frame)  # still waiting for an IDR frame
        self.assertTrue(queue.empty())
        client._publish(idr)
        self.assertEqual(queue.get_nowait(), sps + pps + idr)
        self.assertNotIn(queue, client.stream_resync_clients)


class TestParseSip(unittest.TestCase):
    def test_negative_content_length_gives_empty_body(self):
        msg = sip.parse_sip(b"MESSAGE sip:a SIP/2.0\r\nContent-Length: -5\r\n\r\nabcdef")
        self.assertEqual(msg.body, "")


if __name__ == "__main__":
    unittest.main()
