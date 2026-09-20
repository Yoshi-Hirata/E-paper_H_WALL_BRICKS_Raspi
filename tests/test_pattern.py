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
    HI_Z_V11,
    MARKER_V11,
    SEGMENTS_GEN,
    VALID_TRIANGLES,
    build_gen_array,
    build_hexagon_array,
)
from epaper.protocol import DEV_COMMON


def test_valid_triangle_set_matches_spec():
    # Production boards: 54 triangles numbered 1-16 and 23-60; 17-22 and
    # 61-63 do not exist. (The first-generation boards numbered the same
    # layout 2-61; Address_H_WALL_BRICKS.jpg shows that old numbering.)
    assert len(VALID_TRIANGLES) == 54
    for missing in [0, 17, 18, 19, 20, 21, 22, 61, 62, 63]:
        assert missing not in VALID_TRIANGLES
    # Address_H_WALL_BRICKS.jpg numbers, shifted down by one
    for present in [1, 16, 23, 50, 60, 7, 55]:
        assert present in VALID_TRIANGLES


def test_all_white_array_layout():
    arr = build_hexagon_array()
    assert len(arr) == 64
    assert arr[0] == COM_MARKER and arr[63] == COM_MARKER
    assert arr[61] == HI_Z and arr[62] == HI_Z
    for i in range(17, 23):
        assert arr[i] == HI_Z
    assert sum(1 for b in arr if b == COLOR_WHITE) == 54


def test_overrides_placed_at_index():
    arr = build_hexagon_array({2: COLOR_RED, 35: COLOR_BLUE})
    assert arr[2] == COLOR_RED
    assert arr[35] == COLOR_BLUE
    assert arr[3] == COLOR_WHITE


def test_rejects_invalid_input():
    with pytest.raises(ValueError):
        build_hexagon_array({61: COLOR_RED})      # missing triangle
    with pytest.raises(ValueError):
        build_hexagon_array({17: COLOR_RED})      # missing triangle
    with pytest.raises(ValueError):
        build_hexagon_array({2: 0x37})            # not a color
    with pytest.raises(ValueError):
        build_hexagon_array(fill=0x21)            # not a color


def test_gen_array_layout():
    # V1.1 / GEN: segments 1-60 are the payload, FE markers bracket the
    # array, 61-62 stay FF padding (vendor README 2026-08-26).
    assert SEGMENTS_GEN == frozenset(range(1, 61))
    arr = build_gen_array({seg: (seg - 1) % 16 for seg in SEGMENTS_GEN})
    assert len(arr) == 64
    assert arr[0] == MARKER_V11 and arr[63] == MARKER_V11
    assert arr[61] == HI_Z_V11 and arr[62] == HI_Z_V11
    for seg in SEGMENTS_GEN:
        assert arr[seg] == (seg - 1) % 16
    assert arr[1] == 0x00 and arr[16] == 0x0F and arr[17] == 0x00


def test_gen_array_rejects_invalid_input():
    with pytest.raises(ValueError):
        build_gen_array({0: 0x00})        # marker slot, not a segment
    with pytest.raises(ValueError):
        build_gen_array({61: 0x00})       # padding, not a segment
    with pytest.raises(ValueError):
        build_gen_array({1: 0x10})        # beyond the 16-color palette
    with pytest.raises(ValueError):
        build_gen_array(fill=0xFE)


def test_save_color_carries_dev_type():
    arr = build_gen_array()
    frame = save_color(dest=0x01, slot=19, array64=arr, dev_type=DEV_COMMON)
    assert frame.dev_type == DEV_COMMON
    # default stays the hexagon device type for the old patterns
    assert save_color(dest=0x01, slot=19, array64=arr).dev_type == 0x01


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


def test_palette_matches_the_fw_260917_color_chart():
    # FW/FW_260917/260917_16_Color_Chart_ changed.xlsx: value, name,
    # reference RGB. Green is 0x05 and turquoise 0x06 - the V1.1
    # datasheet table (and FW_260903) had those two the other way round.
    from epaper.pattern import COLOR_LABELS_16, COLOR_NAMES_16, COLOR_RGB_16

    spec = {
        0x00: ("white", "White", (255, 255, 255)),
        0x01: ("yellow", "Yellow", (255, 255, 0)),
        0x02: ("blue", "Blue", (0, 0, 255)),
        0x03: ("red", "Red", (255, 0, 0)),
        0x04: ("black", "Black", (0, 0, 0)),
        0x05: ("green", "Green", (0, 200, 0)),
        0x06: ("turquoise", "Turquoise", (64, 224, 208)),
        0x07: ("almond", "Almond", (235, 210, 180)),
        0x08: ("pink", "Light Pink", (255, 209, 220)),
        0x09: ("skyblue", "Sky Blue", (135, 206, 235)),
        0x0A: ("orange", "Orange", (255, 140, 0)),
        0x0B: ("yellowgreen", "Yellow Green", (154, 205, 50)),
        0x0C: ("olivegray", "Olive Gray", (112, 116, 85)),
        0x0D: ("brown", "Brown", (139, 69, 19)),
        0x0E: ("darkbrown", "Dark Brown", (92, 51, 23)),
        0x0F: ("smokeblue", "Smoke Blue", (110, 130, 150)),
    }
    assert len(COLOR_LABELS_16) == len(COLOR_RGB_16) == 16
    for code, (name, label, rgb) in spec.items():
        assert COLOR_NAMES_16[name] == code
        assert COLOR_LABELS_16[code] == label
        assert COLOR_RGB_16[code] == rgb
    assert len(COLOR_NAMES_16) == 16
