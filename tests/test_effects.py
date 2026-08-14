import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

from epaper.effects import gradient_pattern, spiral_pattern, validate
from epaper.geometry import (
    ADDRESS_ROWS,
    ADJACENCY,
    POINTS_UP,
    RING,
    SPIRAL_POS,
    _N_BOUNDARY_EDGES,
)
from epaper.pattern import COLOR_NAMES, VALID_TRIANGLES, build_hexagon_array

PALETTE = list(COLOR_NAMES.values())


def test_address_rows_cover_all_triangles():
    flat = [n for row in ADDRESS_ROWS for n in row]
    assert len(flat) == 54
    assert set(flat) == VALID_TRIANGLES
    assert [len(r) for r in ADDRESS_ROWS] == [7, 9, 11, 11, 9, 7]


def test_adjacency_structure():
    # A side-3 hexagon tiling: 18 boundary triangles with 2 neighbours,
    # 36 interior ones with 3; 18 boundary edges.
    assert _N_BOUNDARY_EDGES == 18
    counts = sorted(len(ADJACENCY[t]) for t in ADJACENCY)
    assert counts.count(2) == 18
    assert counts.count(3) == 36
    for tri, neigh in ADJACENCY.items():
        assert tri not in neigh
        for n in neigh:
            assert tri in ADJACENCY[n]  # symmetric


def test_adjacent_triangles_have_opposite_orientation():
    for tri, neigh in ADJACENCY.items():
        for n in neigh:
            assert POINTS_UP[tri] != POINTS_UP[n]


def test_known_neighbours():
    # Production numbering (first-generation minus one).
    # Corner triangle 50 (top-left): neighbours are 49 (right) and 51 (below).
    assert ADJACENCY[50] == frozenset({49, 51})
    # 39 sits mid-panel (row 3, 5th) between 40, 29 and 38 above.
    assert ADJACENCY[39] == frozenset({40, 29, 38})


def test_rings_and_spiral():
    # Center triangles (38, 39 area) sit in ring 0, corners in the last.
    assert RING[39] == 0 and RING[38] == 0
    assert RING[50] == max(RING.values())
    assert sorted(SPIRAL_POS.values()) == list(range(54))
    # Spiral runs outside-in: ring index never increases along the order.
    order = sorted(SPIRAL_POS, key=SPIRAL_POS.get)
    rings = [RING[t] for t in order]
    assert all(a >= b for a, b in zip(rings, rings[1:]))


def test_effects_never_color_adjacent_same():
    for cycle in range(24):
        for gen in (gradient_pattern, spiral_pattern):
            pattern = gen(cycle, PALETTE)
            assert set(pattern) == VALID_TRIANGLES
            assert validate(pattern)
            build_hexagon_array(pattern)  # also passes array validation


def test_effects_animate_between_cycles():
    for gen in (gradient_pattern, spiral_pattern):
        assert gen(0, PALETTE) != gen(1, PALETTE)
    # Palette length 6 -> the animation loops with period 6.
    assert gradient_pattern(0, PALETTE) == gradient_pattern(6, PALETTE)
    assert spiral_pattern(0, PALETTE) == spiral_pattern(6, PALETTE)
