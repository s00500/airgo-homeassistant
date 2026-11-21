"""Protocol tests, using frames captured from a real AirGo 140 (firmware 122)."""

import importlib.util
from pathlib import Path
import struct
import sys

import pytest

_spec = importlib.util.spec_from_file_location(
    "airgo_protocol",
    Path(__file__).parent.parent / "custom_components/airgo140/protocol.py",
)
p = importlib.util.module_from_spec(_spec)
sys.modules["airgo_protocol"] = p
_spec.loader.exec_module(p)

CAPTURED = {
    1: 104, 2: 122, 3: 101, 4: 140, 5: 27745, 6: 3608, 7: 19, 8: 0, 9: 0, 10: 0, 11: 0,
    12: -5000, 13: -5000, 14: -5000, 15: -5000,
    16: 250, 17: 3, 18: 300, 19: 4, 20: 360, 21: 4, 22: 300, 23: 2, 24: 120, 25: 5,
    26: 5, 27: 0, 28: 0, 29: 140, 30: 140, 31: 1, 32: 5, 33: 4, 34: 0, 35: 0,
}  # fmt: skip
CAPTURED_CRC = 0x7CF9FE24


def read_frame(params, crc=None):
    body = struct.pack("<II", 8 + 4 * len(params) + 4, len(params))
    for pid, value in params.items():
        body += struct.pack("<hH", value, pid)
    return body + struct.pack("<I", p.crc(body) if crc is None else crc)


def short_frame(kind, arg):
    body = struct.pack("<III", 16, kind, arg)
    return body + struct.pack("<I", p.crc(body))


def test_crc_matches_real_device():
    frame = read_frame(CAPTURED, CAPTURED_CRC)
    assert p.crc(frame[:-4]) == CAPTURED_CRC
    assert p.decode_read(frame) == CAPTURED


def test_go_and_python_encode_identically():
    # Same bytes as EncodeCommand(CommandStop) in protocol.go: size, command, CRC.
    frame = p.encode_command(6)
    assert frame[:8] == struct.pack("<II", 12, 6)
    assert struct.unpack_from("<I", frame, 8)[0] == p.crc(frame[:8])


def test_encode_params_round_trip():
    frame = p.encode_params({12: 300, 13: 3})
    assert len(frame) == 20
    assert p.decode_read(frame) == {12: 300, 13: 3}
    with pytest.raises(ValueError):
        p.encode_params({i: 0 for i in range(21)})


def test_decode_notifications():
    assert p.decode_notification(short_frame(3, 6)).ack_command == 6
    assert p.decode_notification(short_frame(3, 8)).ack_rejected
    assert p.decode_notification(short_frame(4, 2)).set_error == 2
    with pytest.raises(p.ProtocolError):
        p.decode_notification(short_frame(99, 0))


@pytest.mark.parametrize(
    "mangle",
    [
        lambda f: f[:15],
        lambda f: b"\x90\x01\x00\x00" + f[4:],
        lambda f: f[:8] + bytes([f[8] ^ 1]) + f[9:],
    ],
)
def test_rejects_corruption(mangle):
    with pytest.raises(p.ProtocolError):
        p.decode_notification(mangle(short_frame(3, 1)))


def test_rejects_count_that_does_not_fit():
    body = struct.pack("<III", 16, 0xFFFFFFFF, 0)
    with pytest.raises(p.ProtocolError):
        p.decode_read(body + struct.pack("<I", p.crc(body)))


@pytest.mark.parametrize(
    ("pid", "fahrenheit", "expected"),
    [
        (16, False, (80, 450)),
        (24, True, (180, 840)),
        (12, False, (80, 450)),
        (17, False, (1, 5)),
        (25, False, (1, 5)),
        (13, False, (1, 5)),
        (29, False, (40, 140)),
        (32, False, (1, 10)),
    ],
)
def test_value_range(pid, fahrenheit, expected):
    assert p.value_range(pid, fahrenheit) == expected


def test_read_only_parameters_are_not_writable():
    with pytest.raises(ValueError):
        p.value_range(7, False)


def test_firmware_string():
    assert p.firmware_string(122) == "1.22"
    assert p.firmware_string(13) == "1.3"
