"""AirGo 140 wire protocol. No Home Assistant or Bluetooth imports.

Mirrors protocol.go in this repository; PROTOCOL.md documents both.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
import struct

SERVICE_UUID = "0000ffe0-0000-1000-8000-00805f9b34fb"
PARAMS_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"
COMMAND_UUID = "0000ffe2-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000ffe3-0000-1000-8000-00805f9b34fb"

UNAVAILABLE = -5000
MAX_PARAMS_PER_WRITE = 20

FRAME_PARAMS = 1
FRAME_COMMAND_ACK = 3
FRAME_SET_ERROR = 4
ACK_REJECTED = 8


class Param(IntEnum):
    """Parameter IDs."""

    FIRMWARE = 2
    PD_POWER = 4
    INPUT_MV = 5
    HEATER_MOHM = 6
    TEMPERATURE = 7
    FLAGS = 9
    ACTIVE_GEAR = 11
    TARGET_TEMP = 12
    FAN_LEVEL = 13
    OUTPUT_PERCENT = 15
    GEAR1_TEMP = 16
    GEAR1_FAN = 17
    GEAR5_FAN = 25
    IDLE_TIME = 26
    IDLE_DETECT = 27
    TEMP_UNIT = 28
    PD_LIMIT = 29
    DC_LIMIT = 30
    FAST_COOL = 31
    BACKLIGHT = 32
    VOLUME = 33
    LANGUAGE = 34
    SCREEN_OFF = 35


class Command(IntEnum):
    """Commands written to FFE2. Gears 1-5 are commands 1-5."""

    STOP = 6


class SetError(IntEnum):
    """Reasons the gun rejects a parameter write."""

    WRONG_STATE = 2
    UNKNOWN_ID = 3
    BAD_VALUE = 4


FLAG_LOW_POWER = 1 << 14
FLAG_HEATER_FAULT = 1 << 13


class ProtocolError(ValueError):
    """A frame failed validation."""


def gear_temp_param(gear: int) -> int:
    """Return the stored temperature parameter of gear 1-5."""
    return Param.GEAR1_TEMP + (gear - 1) * 2


def gear_fan_param(gear: int) -> int:
    """Return the stored fan parameter of gear 1-5."""
    return Param.GEAR1_FAN + (gear - 1) * 2


def crc(data: bytes) -> int:
    """CRC-32/MPEG-2 over little-endian 32-bit words, as the CH32 hardware unit."""
    value = 0xFFFFFFFF
    for (word,) in struct.iter_unpack("<I", data):
        value ^= word
        for _ in range(32):
            if value & 0x80000000:
                value = ((value << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
            else:
                value = (value << 1) & 0xFFFFFFFF
    return value


def _validated(frame: bytes) -> bytes:
    if len(frame) < 8 or len(frame) % 4:
        raise ProtocolError(f"bad frame length {len(frame)}")
    size = struct.unpack_from("<I", frame)[0]
    if size < 8 or size % 4 or size > len(frame):
        raise ProtocolError(f"size field {size} does not fit {len(frame)} bytes")
    body = frame[: size - 4]
    if crc(body) != struct.unpack_from("<I", frame, size - 4)[0]:
        raise ProtocolError("CRC mismatch")
    return body


def _decode_params(data: bytes, count: int) -> dict[int, int]:
    if count * 4 != len(data):
        raise ProtocolError(f"count {count} does not fit {len(data)} bytes")
    return {pid: value for value, pid in struct.iter_unpack("<hH", data)}


def decode_read(frame: bytes) -> dict[int, int]:
    """Decode the frame returned by reading FFE1."""
    body = _validated(frame)
    if len(body) < 8:
        raise ProtocolError("short read frame")
    return _decode_params(body[8:], struct.unpack_from("<I", body, 4)[0])


@dataclass
class Notification:
    """A decoded FFE3 frame."""

    params: dict[int, int] = field(default_factory=dict)
    ack_command: int | None = None
    ack_rejected: bool = False
    set_error: int | None = None


def decode_notification(frame: bytes) -> Notification:
    """Decode a frame received on FFE3."""
    body = _validated(frame)
    if len(body) < 12:
        raise ProtocolError("short notification")
    kind, arg = struct.unpack_from("<II", body, 4)
    if kind == FRAME_PARAMS:
        return Notification(params=_decode_params(body[12:], arg))
    if kind == FRAME_COMMAND_ACK:
        if arg == ACK_REJECTED:
            return Notification(ack_rejected=True)
        return Notification(ack_command=arg)
    if kind == FRAME_SET_ERROR:
        return Notification(set_error=arg)
    raise ProtocolError(f"unknown frame type {kind}")


def encode_command(command: int) -> bytes:
    """Build a frame for FFE2."""
    body = struct.pack("<II", 12, command)
    return body + struct.pack("<I", crc(body))


def encode_params(params: dict[int, int]) -> bytes:
    """Build a frame for writing FFE1."""
    if not 1 <= len(params) <= MAX_PARAMS_PER_WRITE:
        raise ValueError(f"{len(params)} parameters in one write")
    size = 8 + 4 * len(params) + 4
    body = struct.pack("<II", size, len(params))
    for pid, value in params.items():
        body += struct.pack("<hH", value, pid)
    return body + struct.pack("<I", crc(body))


def value_range(pid: int, fahrenheit: bool) -> tuple[int, int]:
    """Return the legal range of a writable parameter.

    The firmware only checks target temperature and fan level; stored settings go
    to flash unchecked, so callers must validate.
    """
    if Param.GEAR1_TEMP <= pid <= Param.GEAR5_FAN:
        is_temp = (pid - Param.GEAR1_TEMP) % 2 == 0
    else:
        is_temp = pid == Param.TARGET_TEMP
    if is_temp:
        return (180, 840) if fahrenheit else (80, 450)
    if pid == Param.FAN_LEVEL or Param.GEAR1_TEMP <= pid <= Param.GEAR5_FAN:
        return (1, 5)
    ranges = {
        Param.IDLE_TIME: (0, 30),
        Param.IDLE_DETECT: (0, 30),
        Param.TEMP_UNIT: (0, 1),
        Param.FAST_COOL: (0, 1),
        Param.SCREEN_OFF: (0, 1),
        Param.PD_LIMIT: (40, 140),
        Param.DC_LIMIT: (40, 140),
        Param.BACKLIGHT: (1, 10),
        Param.VOLUME: (0, 5),
        Param.LANGUAGE: (0, 3),
    }
    if pid not in ranges:
        raise ValueError(f"parameter {pid} is not writable")
    return ranges[pid]


def firmware_string(value: int) -> str:
    """Render the firmware parameter: 122 is 1.22, 13 is 1.3."""
    digits = str(value)
    return digits if len(digits) < 2 else f"{digits[0]}.{digits[1:]}"
