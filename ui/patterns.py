"""Catalogue of demo patterns selectable from the LCD HAT menu.

A pattern turns (cycle, boards) into one {triangle: color} map per board.
Pure functions - no serial I/O - so the whole catalogue is testable.
"""

import random
from dataclasses import dataclass
from typing import Callable

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

from epaper.effects import gradient_pattern, spiral_pattern
from epaper.pattern import COLOR_NAMES, VALID_TRIANGLES

DEFAULT_PALETTE = [COLOR_NAMES[c] for c in
                   ("white", "yellow", "red", "blue", "green", "black")]

# Order the solid-colour showcase steps through.
SOLID_SEQUENCE = [COLOR_NAMES[c] for c in
                  ("white", "yellow", "blue", "red", "black", "green")]

Frame = dict[int, dict[int, int]]  # board address -> {triangle: color}


@dataclass(frozen=True)
class Pattern:
    key: str
    label: str          # shown in the menu (keep <= 14 chars for the LCD)
    detail: str         # one-line description under the menu
    build: Callable[[int, list[int], list[int], random.Random], Frame]
    # Seconds between refreshes, when this pattern wants something other
    # than the runner's default. A full-panel repaint measures 9.8 s on
    # the hardware (plus ~0.2 s to save), so nothing below ~11 s leaves
    # the image visible at all.
    interval: float | None = None

    def __call__(self, cycle: int, boards: list[int],
                 palette: list[int] | None = None,
                 rng: random.Random | None = None) -> Frame:
        return self.build(cycle, boards, palette or DEFAULT_PALETTE,
                          rng or random.Random())


def _wave(cycle, boards, palette, rng) -> Frame:
    gens = (gradient_pattern, spiral_pattern)
    return {b: gens[i % len(gens)](cycle, palette)
            for i, b in enumerate(boards)}


def _gradient(cycle, boards, palette, rng) -> Frame:
    return {b: gradient_pattern(cycle, palette) for b in boards}


def _spiral(cycle, boards, palette, rng) -> Frame:
    return {b: spiral_pattern(cycle, palette) for b in boards}


def _mirror(cycle, boards, palette, rng) -> Frame:
    """Gradient on every board, but each board starts a phase apart, so the
    waves chase each other across the panels."""
    return {b: gradient_pattern(cycle + i, palette)
            for i, b in enumerate(boards)}


def _random(cycle, boards, palette, rng) -> Frame:
    return {b: {t: rng.choice(palette) for t in sorted(VALID_TRIANGLES)}
            for b in boards}


def _solid(cycle, boards, palette, rng) -> Frame:
    """Both panels one colour, stepping through SOLID_SEQUENCE."""
    color = SOLID_SEQUENCE[cycle % len(SOLID_SEQUENCE)]
    return {b: {t: color for t in sorted(VALID_TRIANGLES)} for b in boards}


PATTERNS: list[Pattern] = [
    Pattern("wave", "WAVE", "gradient + spiral", _wave),
    Pattern("gradient", "GRADIENT", "rings from center", _gradient),
    Pattern("spiral", "SPIRAL", "clockwise inward", _spiral),
    Pattern("mirror", "MIRROR", "chasing gradients", _mirror),
    Pattern("random", "RANDOM", "random colors", _random),
    # 15 s: the 9.8 s repaint plus ~5 s of the colour standing still.
    Pattern("solid", "SOLID", "W>Y>B>R>K>G, 15s", _solid, interval=15.0),
]

BY_KEY = {p.key: p for p in PATTERNS}
