from __future__ import annotations

import secrets

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

BLOCK_BYTES = 256

DOOR_OPENER = bytes([0x04, 0x02, 0x04, 0x05])
LIGHT = bytes([0x04, 0x02, 0x05, 0x02, 0x01])


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


REPLY_ANSWER_BYTES = 16
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
    plain = decrypt_block(decode_body(body, 0x02), k1)
    if any(plain[REPLY_ANSWER_BYTES:]):
        raise HandshakeError(
            "02 reply did not decrypt cleanly (wrong SIP password/account "
            "or foreign reply)"
        )
    return next_stage_key(plain)


def check_ack(body: str, k2: bytes) -> None:
    plain = decrypt_block(decode_body(body, 0x03), k2)
    if not plain.startswith(ACK_PREFIX):
        raise HandshakeError(
            f"Unexpected 03 acknowledgement from the gateway (starts with {plain[:3].hex(' ')})"
        )
