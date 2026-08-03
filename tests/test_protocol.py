import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

from epaper.protocol import Frame, crc16_modbus, decode


def test_crc16_modbus_known_vector():
    # Standard Modbus CRC16 check: "123456789" -> 0x4B37
    assert crc16_modbus(b"123456789") == 0x4B37


def test_stop_frame_layout():
    f = Frame(dest=0xFF, src=0x00, dev_type=0x00, cmd=0x17,
              group_no=1, group_count=1, chip_count=2)
    raw = f.encode()
    # Doc example: AA 55 FF 00 00 17 00 01 01 02 00 00 XX XX 55 AA
    assert raw[:2] == b"\xaa\x55"
    assert raw[2:12] == bytes([0xFF, 0x00, 0x00, 0x17, 0x00,
                               0x01, 0x01, 0x02, 0x00, 0x00])
    assert raw[-2:] == b"\x55\xaa"
    crc = crc16_modbus(raw[2:12])
    assert raw[12] == crc & 0xFF and raw[13] == crc >> 8


def test_roundtrip():
    f = Frame(dest=0x01, src=0x00, dev_type=0x01, cmd=0x16,
              group_no=1, group_count=2, chip_count=1, data=b"\x00\x05")
    decoded, rest = decode(f.encode())
    assert rest == b""
    assert decoded is not None
    assert (decoded.dest, decoded.src, decoded.cmd) == (0x01, 0x00, 0x16)
    assert decoded.data == b"\x00\x05"


def test_decode_with_garbage_prefix_and_partial():
    f = Frame(dest=0x00, src=0x01, dev_type=0x01, cmd=0x80)
    raw = f.encode()
    # Garbage before the frame is skipped.
    decoded, rest = decode(b"\x12\x34" + raw)
    assert decoded is not None and decoded.cmd == 0x80
    # Partial frame returns None and keeps the buffer.
    decoded, rest = decode(raw[:8])
    assert decoded is None
    assert rest == raw[:8]


def test_decode_bad_crc_resyncs():
    f = Frame(dest=0x00, src=0x01, dev_type=0x01, cmd=0x80)
    raw = bytearray(f.encode())
    raw[5] ^= 0xFF  # corrupt cmd -> CRC mismatch
    good = Frame(dest=0x00, src=0x02, dev_type=0x01, cmd=0x80).encode()
    decoded, rest = decode(bytes(raw) + good)
    assert decoded is not None
    assert decoded.src == 0x02
