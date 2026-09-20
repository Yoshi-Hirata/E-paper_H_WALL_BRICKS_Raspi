"""Catalogue of demo patterns selectable from the LCD HAT menu.

A pattern turns (cycle, boards) into one {triangle: color} map per board.
Pure functions - no serial I/O - so the whole catalogue is testable.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Callable

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

from epaper.grid import gradient_pattern, repair, spiral_pattern
from epaper.pattern import (COLOR_LABELS_16, COLOR_NAMES_16, SEGMENTS_GEN,
                            build_gen_array)
from epaper.protocol import DEV_NUMBER_BRAND

# The classic six show colors, in FW_260917 LUT codes (green is 0x05).
DEFAULT_PALETTE = [COLOR_NAMES_16[c] for c in
                   ("white", "yellow", "red", "blue", "green", "black")]

# Order the solid-colour showcase steps through.
SOLID_SEQUENCE = [COLOR_NAMES_16[c] for c in
                  ("white", "yellow", "blue", "red", "black", "green")]

# The full V1.1 palette in LUT order 0x00-0x0F, for the 16-color solid
# sweep: one full-panel color per cycle, wrapping after smoke blue.
SOLID16_SEQUENCE = list(range(16))

Frame = dict[int, dict[int, int]]  # board address -> {segment: color}


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
    # How this pattern's frames go on the wire. Since the 2026-08-28 OTA
    # every UI pattern sends the V1.1 64-byte array (segments 1-60,
    # 16-color LUT, 0xFE/0xFF markers) as NUMBER_BRAND (0x03) - the mode
    # whose index map is exactly 1-60 = P1-P60 per the vendor README.
    # GEN (0x06) and hexagon (0x01) modes never refresh index 12 (and per
    # spec 52) on this firmware - a spacer-position leftover, seen as a
    # permanently stale segment 12 on board 1 (2026-08-28). The original
    # hexagon layout (build_hexagon_array + DEV_H_WALL_BRICKS) stays
    # available for the first-generation panels' host scripts.
    array: Callable[[dict[int, int]], bytes] = build_gen_array
    dev_type: int = DEV_NUMBER_BRAND
    # Optional one-liner about the cycle being shown (e.g. the current
    # color's name); the running screen prints it next to the cycle count.
    caption: Callable[[int], str] | None = None

    def __call__(self, cycle: int, boards: list[int],
                 palette: list[int] | None = None,
                 rng: random.Random | None = None) -> Frame:
        return self.build(cycle, boards, palette or DEFAULT_PALETTE,
                          rng or random.Random())

    def resolve(self, cycle: int) -> tuple["Pattern", int]:
        """Which pattern draws this cycle, and its own cycle number."""
        return self, cycle


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
    return {b: {seg: rng.choice(palette) for seg in sorted(SEGMENTS_GEN)}
            for b in boards}


def _solid(cycle, boards, palette, rng) -> Frame:
    """Both panels one colour, stepping through SOLID_SEQUENCE."""
    color = SOLID_SEQUENCE[cycle % len(SOLID_SEQUENCE)]
    return {b: {seg: color for seg in sorted(SEGMENTS_GEN)} for b in boards}


@dataclass(frozen=True)
class Playlist:
    """Several patterns in rotation, looping forever.

    Duck-types Pattern so the runner and the menu treat both alike; each
    step keeps its own pacing (see Pattern.interval).
    """

    key: str
    label: str
    detail: str
    steps: tuple[tuple[Pattern, int], ...]   # (pattern, cycles to spend)
    interval: float | None = None            # steps decide; kept for parity

    @property
    def period(self) -> int:
        return sum(count for _, count in self.steps)

    def resolve(self, cycle: int) -> tuple[Pattern, int]:
        round_no, position = divmod(cycle, self.period)
        for pattern, count in self.steps:
            if position < count:
                # Keep each pattern's own cycle advancing across rounds, so
                # animations continue instead of restarting every loop.
                return pattern, round_no * count + position
            position -= count
        raise AssertionError("period does not cover the steps")

    def __call__(self, cycle: int, boards: list[int],
                 palette: list[int] | None = None,
                 rng: random.Random | None = None) -> Frame:
        pattern, local = self.resolve(cycle)
        return pattern(local, boards, palette, rng)


_SOLID = Pattern("solid", "SOLID", "W>Y>B>R>K>G, 15s", _solid, interval=15.0)
_RANDOM = Pattern("random", "RANDOM", "random colors", _random, interval=20.0)


def _solid16(cycle, boards, palette, rng) -> Frame:
    """Both panels one colour, sweeping the whole 16-color LUT in order."""
    color = SOLID16_SEQUENCE[cycle % len(SOLID16_SEQUENCE)]
    return {b: {seg: color for seg in sorted(SEGMENTS_GEN)} for b in boards}


def _colors16(cycle, boards, palette, rng) -> Frame:
    """16-color test card: segment n shows color (n-1) % 16, so segments
    1-16 sweep the whole V1.1 palette 0x00-0x0F and 17+ repeat it."""
    ramp = {seg: (seg - 1) % 16 for seg in sorted(SEGMENTS_GEN)}
    return {b: dict(ramp) for b in boards}


# Static test card, not a demo loop: run it, read the wall against the
# palette table (docs/SPECIFICATION), stop it. 60 s interval keeps the
# repaint-per-cycle flash writes rare if it is left running.
COLORS16 = Pattern("colors16", "16COLORS", "seg1-16 = 0x00-0x0F, repeat",
                   _colors16, interval=60.0)

def _solid16_caption(cycle: int) -> str:
    code = SOLID16_SEQUENCE[cycle % len(SOLID16_SEQUENCE)]
    return f"0x{code:02X} {COLOR_LABELS_16[code]}"


# One full-panel color per cycle through the whole LUT, in the
# datasheet's palette-table order; the LCD names the color on screen.
# Same pacing note as SOLID: the panels' full repaint is the real floor,
# so the interval mostly sets how long each color stays readable.
SOLID16 = Pattern("solid16", "SOLID16", "0x00-0x0F sweep, 15s",
                  _solid16, interval=15.0, caption=_solid16_caption)


def _random16(cycle, boards, palette, rng) -> Frame:
    """Every segment a random code from the whole V1.1 LUT (0x00-0x0F),
    ignoring the show palette - RANDOM restricted to the six classic
    colors, this is the 16-color firmware's full range. No two
    edge-adjacent segments share a color (grid.repair), so the field
    reads as 60 tiles rather than blotches."""
    return {b: repair({seg: rng.choice(SOLID16_SEQUENCE)
                       for seg in sorted(SEGMENTS_GEN)}, SOLID16_SEQUENCE)
            for b in boards}


RANDOM16 = Pattern("random16", "RANDOM16", "random 0x00-0x0F, no same nbrs",
                   _random16, interval=20.0)

# SOLID16RANDOM picks its color from the cycle number, not from the
# runner's RNG stream, so the caption (which only gets the cycle) names
# the same color the panels show. The seed is drawn once per process so
# the sequence differs from one service start to the next.
_SOLID16_RANDOM_SEED = random.SystemRandom().getrandbits(32)


def solid16_random_color(cycle: int, seed: int | None = None) -> int:
    """The LUT code SOLID16RANDOM shows on `cycle`: any of the 16, never
    the same as the previous cycle (a repeat would look like a stall)."""
    seed = _SOLID16_RANDOM_SEED if seed is None else seed
    previous = (solid16_random_color(cycle - 1, seed) if cycle > 0 else None)
    choices = [c for c in SOLID16_SEQUENCE if c != previous]
    return random.Random(f"{seed}:{cycle}").choice(choices)


def _solid16_random(cycle, boards, palette, rng) -> Frame:
    """Every panel one and the same color, chosen at random per cycle."""
    color = solid16_random_color(cycle)
    return {b: {seg: color for seg in sorted(SEGMENTS_GEN)} for b in boards}


def _solid16_random_caption(cycle: int) -> str:
    code = solid16_random_color(cycle)
    return f"0x{code:02X} {COLOR_LABELS_16[code]}"


SOLID16RANDOM = Pattern("solid16random", "SOLID16RANDOM", "one random color",
                        _solid16_random, interval=15.0,
                        caption=_solid16_random_caption)

# Alternates a 16-color random field with a wall of one random color:
# one cycle each, so the contrast between the two is what the viewer
# sees. Change the counts to dwell longer on either.
LOOP16 = Playlist("loop16", "RND16+SOLID16",
                  "random field, then one random solid",
                  steps=((RANDOM16, 1), (SOLID16RANDOM, 1)))


def _white(cycle, boards, palette, rng) -> Frame:
    return {b: {seg: COLOR_NAMES_16["white"] for seg in sorted(SEGMENTS_GEN)}
            for b in boards}


# Leads the menu, but it is a state, not a demo: every sector white,
# every configured board probed, the result reported on the menu. The
# App runs it through runner.standby() - one shot - because start()
# would loop a 16 s white-on-white repaint forever. It is also what the
# boot sequence applies as soon as the link comes up, so the wall never
# idles on whatever vendor demo frame happened to be mid-play.
#
# GEN covers all 60 segments, so white means white - the old hexagon
# layout skipped 17-22 as holes and a 16COLORS card left colors sitting
# there through standby (seen on board 1, 2026-08-28).
STANDBY = Pattern("standby", "STANDBY", "white + link check", _white)

PATTERNS: list[Pattern | Playlist] = [
    STANDBY,
    COLORS16,
    SOLID16,
    RANDOM16,
    LOOP16,
    SOLID16RANDOM,
    # Default loop: one full colour sweep, then a spell of random fields.
    Playlist("loop", "SOLID+RANDOM", "6 colors, then 6 random",
             steps=((_SOLID, 6), (_RANDOM, 6))),
    Pattern("wave", "WAVE", "gradient + spiral", _wave),
    Pattern("gradient", "GRADIENT", "rings from center", _gradient),
    Pattern("spiral", "SPIRAL", "clockwise inward", _spiral),
    Pattern("mirror", "MIRROR", "chasing gradients", _mirror),
    _RANDOM,
    _SOLID,
]

BY_KEY = {p.key: p for p in PATTERNS}
