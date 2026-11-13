"""Tests for the TKS MESSAGE handshake crypto.

The vectors are synthetic (test password, random blocks) but follow the
structure observed in captures of the official Gira TKS Communicator against
an IP gateway with firmware 05.04.00.08:

* 02 reply: a full 256-byte random block encrypted with k1 (16 distinct AES
  blocks on the wire, no zero padding),
* 03 command from the client: the command padded with zeros,
* 03 acknowledgement from the gateway: starts with 01 02 01, rest random.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "tks_crypto",
    Path(__file__).resolve().parents[1] / "custom_components" / "gira_tks" / "tks_crypto.py",
)
tks = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(tks)

PASSWORD = "TestPassword1234"  # synthetic, 16 characters like the field report


def _gateway_reply(challenge_cipher: bytes, password: str, reply_offset: int = 169):
    """Simulate the gateway: decrypt 01, answer with an unpadded 02."""
    challenge = tks.decrypt_block(challenge_cipher, tks.message_key(password))
    k1 = tks.next_stage_key(challenge)
    reply = bytearray(os.urandom(tks.BLOCK_BYTES))
    reply[2] = reply_offset
    k2 = tks.next_stage_key(bytes(reply))
    return tks.encode_body(0x02, tks.encrypt_block(bytes(reply), k1)), k2


def _gateway_ack(k2: bytes) -> str:
    ack = tks.ACK_PREFIX + os.urandom(tks.BLOCK_BYTES - len(tks.ACK_PREFIX))
    return tks.encode_body(0x03, tks.encrypt_block(ack, k2))


def _distinct_blocks(body: str) -> int:
    data = bytes.fromhex(body[2:])
    return len({data[i:i + 16] for i in range(0, len(data), 16)})


@pytest.mark.parametrize("password", [PASSWORD, "Short12", "ExactlySixteen16", "x" * 20])
def test_full_handshake_with_unpadded_02_reply(password: str) -> None:
    challenge = tks.make_challenge()
    sent01 = tks.encrypt_block(challenge, tks.message_key(password))
    k1 = tks.next_stage_key(challenge)

    body02, gateway_k2 = _gateway_reply(sent01, password)
    assert _distinct_blocks(body02) == 16  # what the real gateway sends

    k2 = tks.stage3_key_from_reply(body02, k1)
    assert k2 == gateway_k2

    body03 = tks.encode_body(0x03, tks.encrypt_block(tks.command_block(tks.DOOR_OPENER), k2))
    assert _distinct_blocks(body03) == 2  # padded command, as in the capture
    assert tks.decrypt_block(bytes.fromhex(body03[2:]), gateway_k2)[:4] == tks.DOOR_OPENER

    tks.check_ack(_gateway_ack(gateway_k2), k2)


def test_wrong_password_is_detected_at_the_ack() -> None:
    challenge = tks.make_challenge()
    sent01 = tks.encrypt_block(challenge, tks.message_key(PASSWORD))
    body02, gateway_k2 = _gateway_reply(sent01, "SomethingElse999")
    k2 = tks.stage3_key_from_reply(body02, tks.next_stage_key(challenge))
    assert k2 != gateway_k2
    with pytest.raises(tks.HandshakeError):
        tks.check_ack(_gateway_ack(gateway_k2), k2)


@pytest.mark.parametrize("offset", [0, 1, 169, 240])
def test_next_stage_key_offsets(offset: int) -> None:
    block = bytearray(range(256))
    block[2] = offset
    expected = bytes(block[offset:offset + 16])
    assert tks.next_stage_key(bytes(block)) == expected


def test_make_challenge_offset_in_range() -> None:
    for _ in range(200):
        assert tks.make_challenge()[2] <= 240


def test_message_key_padding() -> None:
    assert tks.message_key("abc") == b"abc" + bytes(13)
    assert tks.message_key("A" * 20) == b"A" * 16


def test_decode_body_rejects_wrong_type_and_length() -> None:
    body = tks.encode_body(0x02, bytes(256))
    with pytest.raises(tks.HandshakeError):
        tks.decode_body(body, 0x03)
    with pytest.raises(tks.HandshakeError):
        tks.decode_body(body[:-2], 0x02)
