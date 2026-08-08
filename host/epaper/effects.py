"""Animated effect patterns with a hard no-adjacent-same-color guarantee.

Each generator returns {triangle_number: color_code} for one animation
cycle. A repair pass then enforces that edge-adjacent triangles never
share a color (each triangle has at most 3 neighbours, so any palette
of 4+ colors always admits a fix).
"""

from __future__ import annotations

from .geometry import ADJACENCY, POINTS_UP, RING, SPIRAL_POS


def validate(pattern: dict[int, int]) -> bool:
    return all(pattern[t] != pattern[n]
               for t in pattern for n in ADJACENCY[t])


def repair(pattern: dict[int, int], palette: list[int]) -> dict[int, int]:
    """Single deterministic pass; picks the cyclically nearest free color."""
    if len(palette) < 4:
        raise ValueError("palette needs at least 4 colors")
    size = len(palette)
    for tri in sorted(pattern):
        used = {pattern[n] for n in ADJACENCY[tri]}
        if pattern[tri] not in used:
            continue
        base = palette.index(pattern[tri])
        for step in (1, -1, 2, -2, 3, -3):
            candidate = palette[(base + step) % size]
            if candidate not in used:
                pattern[tri] = candidate
                break
    assert validate(pattern)
    return pattern


def gradient_pattern(cycle: int, palette: list[int]) -> dict[int, int]:
    """Concentric waves radiating from the center outward.

    Color follows the ring index shifted by cycle, so each wave moves
    one ring outward per cycle. Up/down triangles get a half-palette
    offset, which keeps same-ring neighbours apart (edge-adjacent
    triangles always have opposite orientation).
    """
    size = len(palette)
    half = size // 2
    pattern = {
        tri: palette[(RING[tri] - cycle + (0 if POINTS_UP[tri] else half))
                     % size]
        for tri in ADJACENCY
    }
    return repair(pattern, palette)


def spiral_pattern(cycle: int, palette: list[int]) -> dict[int, int]:
    """Colors advancing along a clockwise outside-in spiral.

    Consecutive spiral positions always differ, and the whole pattern
    shifts one position per cycle, reading as a clockwise rotation that
    winds toward the center.
    """
    size = len(palette)
    pattern = {
        tri: palette[(SPIRAL_POS[tri] - cycle) % size]
        for tri in ADJACENCY
    }
    return repair(pattern, palette)
