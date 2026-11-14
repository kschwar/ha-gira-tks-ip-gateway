from __future__ import annotations

import secrets

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

BLOCK_BYTES = 256

DOOR_OPENER = bytes([0x04, 0x02, 0x04, 0x05])
LIGHT = bytes([0x04, 0x02, 0x05, 0x02, 0x01])
# Trigger the switching action assigned to the participant's TKS-Communicator function key 1
# (decoded from a TKS Communicator capture; verified with a Gira switch actuator 1289 00).
FUNCTION_KEY_SWITCHING = bytes([0x04, 0x02, 0x06, 0x01, 0x01])
# Query the TKS Communicator sends before a switching action; lists the participant's switching action.
SWITCH_LIST_QUERY = bytes([0x03, 0x01, 0x05, 0x00, 0x01])


def message_key(password: str) -> bytes:
    return password.encode("utf-8")[:16].ljust(16, b"\0")


def _ecb(data: bytes, key: bytes, *, decrypt: bool) -> bytes:
    if len(data) != BLOCK_BYTES:
        raise ValueError("TKS block must be exactly 256 bytes")
    if len(key) != 16:
        raise ValueError("AES key must be 16 bytes")
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    ctx = cipher.decryptor() if decrypt else cipher.encryptor()
    return ctx.update(data) + ctx.finalize()


def encrypt_block(plaintext256: bytes, key: bytes) -> bytes:
    return _ecb(plaintext256, key, decrypt=False)


def decrypt_block(ciphertext256: bytes, key: bytes) -> bytes:
    return _ecb(ciphertext256, key, decrypt=True)


def next_stage_key(plaintext256: bytes) -> bytes:
    offset = min(plaintext256[2], 240)
    return plaintext256[offset:offset + 16].ljust(16, b"\0")


def command_block(command: bytes) -> bytes:
    if len(command) > BLOCK_BYTES:
        raise ValueError("command too long")
    return command.ljust(BLOCK_BYTES, b"\0")


def encode_body(type_byte: int, block256: bytes) -> str:
    return f"{type_byte:02X}" + block256.hex().upper()


ACK_PREFIX = bytes([0x01, 0x02, 0x01])


class HandshakeError(Exception):
    pass


def make_challenge() -> bytes:
    challenge = bytearray(secrets.token_bytes(BLOCK_BYTES))
    challenge[2] = secrets.randbelow(241)
    return bytes(challenge)


def decode_body(body: str, type_byte: int) -> bytes:
    body = body.strip()
    if len(body) != 2 + 2 * BLOCK_BYTES or body[:2].upper() != f"{type_byte:02X}":
        raise HandshakeError(f"Not a valid type {type_byte:02X} block")
    try:
        return bytes.fromhex(body[2:])
    except ValueError as err:
        raise HandshakeError("Block is not hex") from err


def stage3_key_from_reply(body: str, k1: bytes) -> bytes:
    """Derive the stage-3 key from the gateway's 02 reply.

    The 02 plaintext is a full 256-byte random block (a counter-challenge),
    not 16 bytes followed by zero padding: captures of the official TKS
    Communicator against firmware 05.04.00.08 show 16 distinct AES blocks in
    the 02 reply, and its key derivation only works with all 256 bytes.  The
    reply therefore cannot be validated on its own; a wrong password shows up
    as an unreadable 03 acknowledgement instead.
    """
    return next_stage_key(decrypt_block(decode_body(body, 0x02), k1))


def check_ack(body: str, k2: bytes) -> None:
    plain = decrypt_block(decode_body(body, 0x03), k2)
    if not plain.startswith(ACK_PREFIX):
        raise HandshakeError(
            f"Unexpected 03 acknowledgement from the gateway (starts with {plain[:3].hex(' ')}; "
            "wrong SIP password or a reply from another exchange)"
        )
