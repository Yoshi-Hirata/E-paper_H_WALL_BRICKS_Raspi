"""Board profiles: the pin translation must stay honest.

These numbers cannot be checked by running the code on a laptop, so the
tests encode the arithmetic and the invariants that would catch a typo
before it becomes an afternoon with a multimeter.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from ui.boards import (CUBIE_A7Z, PI_ZERO_2W, PROFILES, BoardProfile, Line,
                       detect_board)
from ui.config import BUTTON_PINS


@pytest.mark.parametrize("profile", PROFILES.values(), ids=lambda p: p.key)
def test_every_hat_control_is_mapped(profile: BoardProfile):
    assert set(profile.buttons) == set(BUTTON_PINS)


@pytest.mark.parametrize("profile", PROFILES.values(), ids=lambda p: p.key)
def test_no_line_is_used_twice(profile: BoardProfile):
    used = list(profile.buttons.values()) + [profile.dc, profile.rst, profile.bl]
    assert len(set(used)) == len(used), "two functions share a GPIO line"


def test_pi_profile_matches_the_bcm_numbers_the_hat_documents():
    # On a Pi the BCM number is the gpiochip0 line number, so the profile
    # should agree with the pin map the rest of the code has always used.
    for name, bcm in BUTTON_PINS.items():
        assert PI_ZERO_2W.buttons[name] == Line(0, bcm)


def test_cubie_lines_follow_the_documented_bank_formula():
    # chip0: 32 * bank(PA=0..PK=10) + n, chip1 restarts at PL=0.
    # Confirmed against the hardware, where gpiochip0 reports 352 lines
    # (11 banks) and gpiochip1 reports 64 (2 banks).
    def chip0(bank: str, n: int) -> Line:
        return Line(0, 32 * ("ABCDEFGHIJK".index(bank)) + n)

    def chip1(bank: str, n: int) -> Line:
        return Line(1, 32 * ("LM".index(bank)) + n)

    assert CUBIE_A7Z.buttons["key1"] == chip0("B", 7)     # PB7,  pin 40
    assert CUBIE_A7Z.buttons["key2"] == chip0("B", 8)     # PB8,  pin 38
    assert CUBIE_A7Z.buttons["key3"] == chip0("B", 4)     # PB4,  pin 36
    assert CUBIE_A7Z.buttons["left"] == chip0("B", 2)     # PB2,  pin 29
    assert CUBIE_A7Z.buttons["up"] == chip0("B", 3)       # PB3,  pin 31
    assert CUBIE_A7Z.buttons["down"] == chip0("B", 6)     # PB6,  pin 35
    assert CUBIE_A7Z.buttons["press"] == chip1("M", 3)    # PM3,  pin 33
    assert CUBIE_A7Z.buttons["right"] == chip1("M", 4)    # PM4,  pin 37
    assert CUBIE_A7Z.dc == chip1("L", 5)                  # PL5,  pin 22
    assert CUBIE_A7Z.rst == chip1("L", 6)                 # PL6,  pin 13
    assert CUBIE_A7Z.bl == chip0("J", 25)                 # PJ25, pin 18


def test_cubie_lines_fit_the_chips_they_name():
    # gpiochip0 has 352 lines, gpiochip1 has 64 - measured on the board.
    sizes = {0: 352, 1: 64}
    lines = list(CUBIE_A7Z.buttons.values()) + [CUBIE_A7Z.dc, CUBIE_A7Z.rst,
                                                CUBIE_A7Z.bl]
    for line in lines:
        assert line.line < sizes[line.chip], f"{line} is past the end of its chip"


def test_spi_differs_per_board_but_the_header_pins_do_not():
    # Both boards expose SPI on header pins 19/21/23/24; only the bus
    # number differs, which is why the HAT needs no rewiring.
    assert (PI_ZERO_2W.spi_bus, PI_ZERO_2W.spi_device) == (0, 0)
    assert (CUBIE_A7Z.spi_bus, CUBIE_A7Z.spi_device) == (1, 0)


def test_detection_reads_the_device_tree(tmp_path, monkeypatch):
    import ui.boards as boards

    for compatible, expected in (
            (b"radxa,cubie-a7z\0allwinner,sun60i-a733\0", CUBIE_A7Z),
            (b"raspberrypi,model-zero-2-w\0brcm,bcm2837\0", PI_ZERO_2W),
    ):
        path = tmp_path / "compatible"
        path.write_bytes(compatible)
        monkeypatch.setattr(boards, "_compatible",
                            lambda p=path: p.read_bytes().decode().replace("\0", " "))
        assert detect_board() is expected


def test_unknown_board_falls_back_rather_than_crashing(monkeypatch):
    import ui.boards as boards

    monkeypatch.setattr(boards, "_compatible", lambda: "some,unknown-board")
    assert detect_board() is PI_ZERO_2W
