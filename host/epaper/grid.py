"""Grid geometry + effects for the GEN (0x06) production wall.

The 16-color firmware addresses segments 1-60 with no physical layout
implied; the current installation arranges them as a 5-column x 12-row
grid, row-major from the top-left:

    seg 1  2  3  4  5
        6  7  8  9 10
        ...
       56 57 58 59 60

From that we derive orthogonal adjacency, concentric rings around the
grid center (for radial effects) and a clockwise outside-in perimeter
spiral. The effect generators mirror host/epaper/effects.py (which keeps
serving the first-generation triangle panels) including its guarantee:
edge-adjacent segments never share a color.
"""

from __future__ import annotations

import math

from .pattern import SEGMENTS_GEN

GRID_COLS = 5
GRID_ROWS = 12
N_RINGS = 6

XY = {seg: ((seg - 1) % GRID_COLS, (seg - 1) // GRID_COLS)
      for seg in SEGMENTS_GEN}


def _build():
    by_xy = {xy: seg for seg, xy in XY.items()}
    adjacency = {
        seg: frozenset(by_xy[x + dx, y + dy]
                       for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))
                       if (x + dx, y + dy) in by_xy)
        for seg, (x, y) in XY.items()
    }

    cx, cy = (GRID_COLS - 1) / 2, (GRID_ROWS - 1) / 2
    dist = {seg: math.hypot(x - cx, y - cy) for seg, (x, y) in XY.items()}
    d_min, d_max = min(dist.values()), max(dist.values())
    ring = {seg: min(int((d - d_min) / (d_max - d_min) * N_RINGS),
                     N_RINGS - 1)
            for seg, d in dist.items()}

    # Clockwise outside-in spiral: walk the outer rectangle from its
    # top-left corner, then the next rectangle in, and so on.
    order = []
    left, right, top, bottom = 0, GRID_COLS - 1, 0, GRID_ROWS - 1
    while left <= right and top <= bottom:
        for x in range(left, right + 1):
            order.append(by_xy[x, top])
        for y in range(top + 1, bottom + 1):
            order.append(by_xy[right, y])
        if top < bottom:
            for x in range(right - 1, left - 1, -1):
                order.append(by_xy[x, bottom])
        if left < right:
            for y in range(bottom - 1, top, -1):
                order.append(by_xy[left, y])
        left, right, top, bottom = left + 1, right - 1, top + 1, bottom - 1
    spiral_pos = {seg: i for i, seg in enumerate(order)}

    return adjacency, ring, spiral_pos


ADJACENCY, RING, SPIRAL_POS = _build()


def validate(pattern: dict[int, int]) -> bool:
    return all(pattern[s] != pattern[n]
               for s in pattern for n in ADJACENCY[s])


def repair(pattern: dict[int, int], palette: list[int]) -> dict[int, int]:
    """Single deterministic pass; picks the cyclically nearest free color.

    A grid segment has at most 4 neighbours, so any palette of 5+ colors
    always admits a fix.
    """
    if len(palette) < 5:
        raise ValueError("palette needs at least 5 colors")
    size = len(palette)
    for seg in sorted(pattern):
        used = {pattern[n] for n in ADJACENCY[seg]}
        if pattern[seg] not in used:
            continue
        base = palette.index(pattern[seg])
        for step in (1, -1, 2, -2, 3, -3, 4, -4):
            candidate = palette[(base + step) % size]
            if candidate not in used:
                pattern[seg] = candidate
                break
    assert validate(pattern)
    return pattern


def gradient_pattern(cycle: int, palette: list[int]) -> dict[int, int]:
    """Concentric waves radiating from the grid center outward.

    Color follows the ring index shifted by cycle, so each wave moves one
    ring outward per cycle. Checkerboard parity adds a half-palette
    offset, which keeps same-ring orthogonal neighbours apart.
    """
    size = len(palette)
    half = size // 2
    pattern = {
        seg: palette[(RING[seg] - cycle
                      + (0 if (x + y) % 2 == 0 else half)) % size]
        for seg, (x, y) in XY.items()
    }
    return repair(pattern, palette)


def spiral_pattern(cycle: int, palette: list[int]) -> dict[int, int]:
    """Colors advancing along the clockwise outside-in spiral.

    Consecutive spiral positions always differ, and the whole pattern
    shifts one position per cycle, reading as a rotation that winds
    toward the center.
    """
    size = len(palette)
    pattern = {
        seg: palette[(SPIRAL_POS[seg] - cycle) % size]
        for seg in SEGMENTS_GEN
    }
    return repair(pattern, palette)
