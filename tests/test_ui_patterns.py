import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

import pytest

from epaper.effects import validate
from epaper.pattern import VALID_TRIANGLES, build_hexagon_array
from ui.patterns import DEFAULT_PALETTE, PATTERNS

BOARDS = [0x01, 0x02]


@pytest.mark.parametrize("pattern", PATTERNS, ids=lambda p: p.key)
def test_pattern_covers_every_board_and_triangle(pattern):
    frame = pattern(0, BOARDS, DEFAULT_PALETTE, random.Random(1))
    assert set(frame) == set(BOARDS)
    for colors in frame.values():
        assert set(colors) == set(VALID_TRIANGLES)
        assert all(c in DEFAULT_PALETTE for c in colors.values())


@pytest.mark.parametrize("pattern", PATTERNS, ids=lambda p: p.key)
def test_pattern_output_is_encodable(pattern):
    # build_hexagon_array is strict about triangle numbers and color codes;
    # a pattern that trips it would only fail once it hit the hardware.
    frame = pattern(3, BOARDS, DEFAULT_PALETTE, random.Random(2))
    for colors in frame.values():
        assert len(build_hexagon_array(colors)) == 64


@pytest.mark.parametrize("key", ["wave", "gradient", "spiral", "mirror"])
def test_effect_patterns_never_place_same_color_side_by_side(key):
    pattern = next(p for p in PATTERNS if p.key == key)
    for cycle in range(8):
        for colors in pattern(cycle, BOARDS).values():
            assert validate(colors)


def test_wave_assigns_a_different_effect_per_board():
    frame = PATTERNS[0](0, BOARDS)
    assert frame[0x01] != frame[0x02]


def test_solid_uses_one_color_and_advances_each_cycle():
    first = PATTERNS[-1](0, BOARDS)[0x01]
    second = PATTERNS[-1](1, BOARDS)[0x01]
    assert len(set(first.values())) == 1
    assert set(first.values()) != set(second.values())


def test_random_is_reproducible_for_a_given_seed():
    a = PATTERNS[4](0, BOARDS, DEFAULT_PALETTE, random.Random(7))
    b = PATTERNS[4](0, BOARDS, DEFAULT_PALETTE, random.Random(7))
    assert a == b


def test_labels_fit_the_lcd_menu():
    for pattern in PATTERNS:
        assert len(pattern.label) <= 14
