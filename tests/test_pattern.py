import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

from epaper.commands import save_color, show_single, slot_config
from epaper.pattern import (
    COLOR_BLUE,
    COLOR_RED,
    COLOR_WHITE,
    COM_MARKER,
    HI_Z,
    VALID_TRIANGLES,
    build_hexagon_array,
)


def test_valid_triangle_set_matches_spec():
    # Spec 13.2: 54 triangles; 1, 18-23, 62, 63 do not exist.
    assert len(VALID_TRIANGLES) == 54
    for missing in [1, 18, 19, 20, 21, 22, 23, 62, 63]:
        assert missing not in VALID_TRIANGLES
    # Numbers visible on Address_H_WALL_BRICKS.jpg
    for present in [2, 17, 24, 51, 61, 8, 56]:
        assert present in VALID_TRIANGLES


def test_all_white_array_layout():
    arr = build_hexagon_array()
    assert len(arr) == 64
    assert arr[0] == COM_MARKER and arr[63] == COM_MARKER
    assert arr[1] == HI_Z and arr[62] == HI_Z
    for i in range(18, 24):
        assert arr[i] == HI_Z
    assert sum(1 for b in arr if b == COLOR_WHITE) == 54


def test_overrides_placed_at_index():
    arr = build_hexagon_array({2: COLOR_RED, 35: COLOR_BLUE})
    assert arr[2] == COLOR_RED
    assert arr[35] == COLOR_BLUE
    assert arr[3] == COLOR_WHITE


def test_rejects_invalid_input():
    with pytest.raises(ValueError):
        build_hexagon_array({1: COLOR_RED})       # missing triangle
    with pytest.raises(ValueError):
        build_hexagon_array({18: COLOR_RED})      # missing triangle
    with pytest.raises(ValueError):
        build_hexagon_array({2: 0x37})            # not a color
    with pytest.raises(ValueError):
        build_hexagon_array(fill=0x21)            # not a color


def test_save_color_frame_shape():
    arr = build_hexagon_array()
    frame = save_color(dest=0x01, slot=19, array64=arr)
    raw = frame.encode()
    # Firmware enforces DataLen <= 66; single-chip save is 66 bytes
    # (slot + flags + 64B array), play params go via 0x1B instead.
    assert raw[6] == 66
    assert len(raw) == 16 + 66
    data = frame.data
    assert data[0] == 19          # slot
    assert data[1] == 0x01        # LAST_FRAME flag
    assert data[2:66] == arr


def test_slot_config_frame_shape():
    frame = slot_config(dest=0x01, slot=19)
    assert len(frame.data) == 8
    assert frame.data[0] == 19
    assert frame.data[3:7] == b"\x00\x00\x00\x00"  # delays 0, little-endian


def test_show_single_frame():
    frame = show_single(dest=0x02, slot=19)
    assert frame.data == bytes([19])
    assert frame.cmd == 0x1D


def test_slot_range_guard():
    arr = build_hexagon_array()
    with pytest.raises(ValueError):
        save_color(dest=0x01, slot=20, array64=arr)
    with pytest.raises(ValueError):
        slot_config(dest=0x01, slot=-1)
