from __future__ import annotations

import secrets

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

BLOCK_BYTES = 256
WIRE_BYTES = BLOCK_BYTES * 2


class ProtocolError(Exception):
    pass


def make_auth_key(username: str, password: str) -> bytes:
    value = (username + password).encode("utf-8")[:15]
    return value.ljust(16, b"\0")


def _aes_block_buffer(data: bytes, key: bytes, *, decrypt: bool) -> bytes:
    if len(data) != BLOCK_BYTES:
        raise ProtocolError("AES buffer must be exactly 256 bytes")
    if len(key) != 16:
        raise ProtocolError("AES key must be exactly 16 bytes")
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    context = cipher.decryptor() if decrypt else cipher.encryptor()
    return context.update(data) + context.finalize()


def encrypt_buffer(data: bytes, key: bytes) -> bytes:
    return _aes_block_buffer(data, key, decrypt=False)


def decrypt_buffer(data: bytes, key: bytes) -> bytes:
    return _aes_block_buffer(data, key, decrypt=True)


def decode_wire_block(wire: bytes) -> bytes:
    if len(wire) != WIRE_BYTES:
        raise ProtocolError(f"expected {WIRE_BYTES} wire bytes")
    try:
        decoded = bytes.fromhex(wire.decode("ascii"))
    except (UnicodeDecodeError, ValueError) as err:
        raise ProtocolError("gateway block is not ASCII hexadecimal") from err
    if len(decoded) != BLOCK_BYTES:
        raise ProtocolError("decoded block has an invalid size")
    return decoded


def encode_wire_block(block: bytes) -> bytes:
    if len(block) != BLOCK_BYTES:
        raise ProtocolError("block must be exactly 256 bytes")
    return block.hex().upper().encode("ascii")


def new_challenge() -> bytes:
    return secrets.token_bytes(BLOCK_BYTES)


def parse_record(block: bytes) -> dict[str, object] | None:
    if len(block) != BLOCK_BYTES or block[0] != 0x10:
        return None

    record_type = block[3]
    if record_type == 0x01:
        record_length = 13
        checksum_offset = 12
        checksum_start = 3
    elif record_type == 0x02:
        record_length = 16
        checksum_offset = 15
        checksum_start = 3
    else:
        return None

    record = block[:record_length]
    expected_checksum = sum(record[checksum_start:checksum_offset]) & 0xFF
    actual_checksum = record[checksum_offset]
    if actual_checksum != expected_checksum:
        return None

    return {
        "frame_type": record_type,
        "record": record.hex(" ").upper(),
        "record_hex": record.hex().upper(),
        "checksum_valid": True,
        **describe_record(record),
    }


FUNCTION_DOOR_CALL = 0x11
FUNCTION_INTERNAL_CALL = 0x12
FUNCTION_FLOOR_CALL = 0x13
FUNCTION_KEYLESS_DOOR = 0x31
FUNCTION_LIGHT = 0x41
FUNCTION_SWITCHING = 0x42
FUNCTION_CODE = 0x44
FUNCTION_KEY_EVENT = 0x45
FUNCTION_END_CALL = 0xAA
FUNCTION_JPEG_STATUS = 0xAB
FUNCTION_DOOR_OPENER_AUTOMATIC = 0xAC
FUNCTION_VOICE_ANNOUNCEMENT = 0xAD
FUNCTION_FORWARDING = 0xC0

FUNCTION_NAMES = {
    FUNCTION_DOOR_CALL: "door_call",
    FUNCTION_INTERNAL_CALL: "internal_call",
    FUNCTION_FLOOR_CALL: "floor_call",
    FUNCTION_KEYLESS_DOOR: "keyless_door_opener",
    FUNCTION_LIGHT: "light",
    FUNCTION_SWITCHING: "switching_action",
    FUNCTION_CODE: "keypad_code",
    FUNCTION_KEY_EVENT: "key_event",
    FUNCTION_END_CALL: "end_call",
    FUNCTION_JPEG_STATUS: "jpeg_status",
    FUNCTION_DOOR_OPENER_AUTOMATIC: "door_opener_automatic",
    FUNCTION_VOICE_ANNOUNCEMENT: "voice_announcement",
    FUNCTION_FORWARDING: "forwarding",
}
FUNCTIONS_WITH_BUTTON = {FUNCTION_DOOR_CALL, FUNCTION_INTERNAL_CALL, FUNCTION_FLOOR_CALL, FUNCTION_SWITCHING}
RESPONSE_FUNCTIONS = {FUNCTION_JPEG_STATUS, FUNCTION_DOOR_OPENER_AUTOMATIC,
                      FUNCTION_VOICE_ANNOUNCEMENT, FUNCTION_FORWARDING}

SENDER_TYPES = {
    0xA0: "door_station", 0xA1: "home_station", 0xA2: "home_station_handset",
    0xA3: "bus_control_unit", 0xA4: "switching_actuator", 0xA5: "tk_gateway",
    0xA6: "extra_gong", 0xA7: "push_button_interface", 0xA8: "tks_ip_gateway",
    0xAA: "homeserver",
}
KEY_EVENTS = {
    0x01: "light", 0x02: "door_opener", 0x10: "call_ended", 0x11: "call_accepted",
}


def describe_record(record: bytes) -> dict[str, object]:
    function = record[5]
    sender = record[11]
    info: dict[str, object] = {
        "function": FUNCTION_NAMES.get(function, f"unknown_{function:02X}"),
        "function_code": f"{function:02X}",
        "sender_type": SENDER_TYPES.get(sender, f"unknown_{sender:02X}"),
        "bus_address": record[6:9].hex().upper(),
    }
    if record[3] == 0x02:
        info["target_address"] = record[12:15].hex().upper()
    if record[3] == 0x01 and function in RESPONSE_FUNCTIONS and sender == 0xA8 and record[4] in (0xC0, 0x80):
        info.update(response_ok=record[4] == 0xC0, status=record[9], id=record[10])
    elif record[3] == 0x01 and function in FUNCTIONS_WITH_BUTTON:
        info["tastennummer"] = record[9] & 0x7F
        info["aufsatz"] = ((record[10] & 0x0F) << 1) + (record[9] >> 7)
        if function == FUNCTION_SWITCHING:
            info["switch"] = "toggle" if record[10] & 0x20 else ("off" if record[10] & 0x10 else "on")
    elif function == FUNCTION_KEY_EVENT:
        info["key_event"] = KEY_EVENTS.get(record[9], f"unknown_{record[9]:02X}")
        if record[3] == 0x02 and sender == 0xA8 and record[10] != 0:
            info["communicator_number"] = record[10]
    return info

