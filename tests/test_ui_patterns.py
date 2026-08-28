import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

import pytest

from epaper.grid import validate
from epaper.pattern import COLOR_NAMES_16, SEGMENTS_GEN
from epaper.protocol import DEV_NUMBER_BRAND
from ui.patterns import BY_KEY, COLORS16, DEFAULT_PALETTE, PATTERNS

BOARDS = [0x01, 0x02]


@pytest.mark.parametrize("pattern", PATTERNS, ids=lambda p: p.key)
def test_pattern_covers_every_board_and_segment(pattern):
    frame = pattern(0, BOARDS, DEFAULT_PALETTE, random.Random(1))
    assert set(frame) == set(BOARDS)
    for colors in frame.values():
        assert set(colors) == set(SEGMENTS_GEN)
        assert all(0 <= c < 16 for c in colors.values())


@pytest.mark.parametrize("pattern", PATTERNS, ids=lambda p: p.key)
def test_pattern_output_is_encodable(pattern):
    # The array builder is strict about segment numbers and color codes;
    # a pattern that trips it would only fail once it hit the hardware.
    frame = pattern(3, BOARDS, DEFAULT_PALETTE, random.Random(2))
    for colors in frame.values():
        active, _ = pattern.resolve(3)
        assert active.dev_type == DEV_NUMBER_BRAND
        assert len(active.array(colors)) == 64


def test_solid16_sweeps_the_whole_lut_and_wraps():
    solid16 = BY_KEY["solid16"]
    for cycle in range(16):
        frame = solid16(cycle, BOARDS)
        for board in BOARDS:
            assert set(frame[board].values()) == {cycle}
    assert solid16(16, BOARDS)[0x01] == solid16(0, BOARDS)[0x01]
    assert solid16.interval >= 11.0     # stays above the repaint time


def test_solid16_captions_name_the_datasheet_colors():
    caption = BY_KEY["solid16"].caption
    assert caption(0) == "0x00 White"
    assert caption(5) == "0x05 Turquoise"
    assert caption(11) == "0x0B Yellow Green"
    assert caption(15) == "0x0F Smoke Blue"
    assert caption(16) == "0x00 White"      # wraps with the sweep


def test_colors16_ramps_and_repeats():
    # Segments 1-16 sweep the palette 0x00-0x0F; 17+ repeat the sweep.
    frame = COLORS16(0, BOARDS)
    for colors in frame.values():
        for seg in range(1, 17):
            assert colors[seg] == seg - 1
        for seg in sorted(SEGMENTS_GEN):
            assert colors[seg] == (seg - 1) % 16
    assert COLORS16.dev_type == DEV_NUMBER_BRAND


@pytest.mark.parametrize("key", ["wave", "gradient", "spiral", "mirror"])
def test_effect_patterns_never_place_same_color_side_by_side(key):
    pattern = next(p for p in PATTERNS if p.key == key)
    for cycle in range(8):
        for colors in pattern(cycle, BOARDS).values():
            assert validate(colors)


def test_wave_assigns_a_different_effect_per_board():
    frame = BY_KEY["wave"](0, BOARDS)
    assert frame[0x01] != frame[0x02]


def test_solid_paints_one_color_and_advances_each_cycle():
    solid = BY_KEY["solid"]
    first = solid(0, BOARDS)[0x01]
    second = solid(1, BOARDS)[0x01]
    assert len(set(first.values())) == 1
    assert set(first.values()) != set(second.values())


def test_solid_follows_the_requested_color_order():
    solid = BY_KEY["solid"]
    order = ["white", "yellow", "blue", "red", "black", "green"]
    for cycle, name in enumerate(order):
        frame = solid(cycle, BOARDS)
        for board in BOARDS:                 # both panels show the same color
            assert set(frame[board].values()) == {COLOR_NAMES_16[name]}
    # and it wraps back to the start
    assert solid(6, BOARDS)[0x01] == solid(0, BOARDS)[0x01]


def test_loop_playlist_alternates_solid_then_random():
    loop = BY_KEY["loop"]
    assert loop.period == 12
    solid_cycles = [loop.resolve(c) for c in range(6)]
    random_cycles = [loop.resolve(c) for c in range(6, 12)]
    assert all(p.key == "solid" for p, _ in solid_cycles)
    assert all(p.key == "random" for p, _ in random_cycles)
    # and it wraps back into the colour sweep
    assert loop.resolve(12)[0].key == "solid"


def test_loop_keeps_each_step_advancing_across_rounds():
    loop = BY_KEY["loop"]
    # Second time through, SOLID must continue its colour order rather
    # than replay the first colour with a stale cycle number.
    assert loop.resolve(0)[1] == 0
    assert loop.resolve(12)[1] == 6      # solid's own 7th cycle -> white again
    assert loop(12, BOARDS)[0x01] == loop(0, BOARDS)[0x01]


def test_loop_paints_solid_colors_in_the_requested_order():
    loop = BY_KEY["loop"]
    order = ["white", "yellow", "blue", "red", "black", "green"]
    for cycle, name in enumerate(order):
        assert set(loop(cycle, BOARDS)[0x01].values()) == {COLOR_NAMES_16[name]}


def test_solid_paces_itself_above_the_measured_repaint_time():
    # A full-panel repaint measures 9.8 s on the hardware; a shorter
    # interval would command the next color before this one is visible.
    assert BY_KEY["solid"].interval >= 11.0


def test_random_is_reproducible_for_a_given_seed():
    a = BY_KEY["random"](0, BOARDS, DEFAULT_PALETTE, random.Random(7))
    b = BY_KEY["random"](0, BOARDS, DEFAULT_PALETTE, random.Random(7))
    assert a == b


def test_labels_fit_the_lcd_menu():
    for pattern in PATTERNS:
        assert len(pattern.label) <= 14
