"""Panel geometry for H_WALL_BRICKS: triangle tiling of a side-3 hexagon.

Rows top to bottom hold 7/9/11/11/9/7 triangles (54 total). Triangle
numbers per row follow Address_H_WALL_BRICKS.jpg / spec 13.1, left to
right. From the tiling we derive centroids, edge-adjacency, concentric
rings (for radial effects) and a clockwise outside-in spiral order.

Coordinates: x rightward (unit = triangle side), y downward in units of
row height H = sqrt(3)/2. Panel center is (0, 3H).
"""

import math

from .pattern import VALID_TRIANGLES

H = math.sqrt(3) / 2
N_RINGS = 5

ADDRESS_ROWS = [
    [51, 50, 44, 43, 35, 34, 24],
    [53, 52, 46, 45, 37, 36, 26, 25, 9],
    [55, 54, 48, 47, 39, 38, 28, 27, 11, 10, 8],
    [56, 57, 49, 41, 40, 30, 29, 13, 12, 6, 7],
    [58, 59, 42, 32, 31, 15, 14, 4, 5],
    [60, 61, 33, 17, 16, 2, 3],
]


def _vertex_key(x: float, y: float) -> tuple[int, int]:
    # x is always a multiple of 0.5, y a multiple of H -> exact int keys.
    return round(x * 2), round(y / H)


def _build():
    verts_of = {}
    up_of = {}
    for r, numbers in enumerate(ADDRESS_ROWS):
        upper = r <= 2
        a = 3 + r if upper else 8 - r  # units on the row's shorter edge
        assert len(numbers) == 2 * a + 1
        for k, num in enumerate(numbers):
            # Within a row triangles alternate orientation; which one the
            # even slots get flips between the upper and lower half.
            points_up = (k % 2 == 0) if upper else (k % 2 == 1)
            if points_up:
                xl = -(a + 1) / 2 + (k if upper else k - 1) * 0.5
                if not upper:
                    xl = -a / 2 + (k - 1) * 0.5
                verts = [(xl, (r + 1) * H), (xl + 1, (r + 1) * H),
                         (xl + 0.5, r * H)]
            else:
                xt = (-a / 2 + (k - 1) * 0.5) if upper \
                    else (-(a + 1) / 2 + k * 0.5)
                verts = [(xt, r * H), (xt + 1, r * H),
                         (xt + 0.5, (r + 1) * H)]
            verts_of[num] = verts
            up_of[num] = points_up

    # Edge-adjacency: two triangles sharing an edge (two vertex keys).
    edge_owners: dict[frozenset, list[int]] = {}
    for num, verts in verts_of.items():
        keys = [_vertex_key(x, y) for x, y in verts]
        for i in range(3):
            edge = frozenset((keys[i], keys[(i + 1) % 3]))
            edge_owners.setdefault(edge, []).append(num)
    adjacency: dict[int, frozenset[int]] = {}
    for num in verts_of:
        neigh = set()
        for owners in edge_owners.values():
            if num in owners:
                neigh.update(o for o in owners if o != num)
        adjacency[num] = frozenset(neigh)

    cx, cy = 0.0, 3 * H
    centroid = {n: (sum(x for x, _ in v) / 3, sum(y for _, y in v) / 3)
                for n, v in verts_of.items()}
    dist = {n: math.hypot(x - cx, y - cy) for n, (x, y) in centroid.items()}
    # Clockwise angle from 12 o'clock (screen coords: y grows downward).
    angle = {n: math.atan2(x - cx, -(y - cy)) % (2 * math.pi)
             for n, (x, y) in centroid.items()}

    d_min, d_max = min(dist.values()), max(dist.values())
    ring = {n: min(int((d - d_min) / (d_max - d_min) * N_RINGS), N_RINGS - 1)
            for n, d in dist.items()}

    # Outside-in, clockwise within each ring.
    spiral = sorted(verts_of, key=lambda n: (-ring[n], angle[n]))
    spiral_pos = {n: i for i, n in enumerate(spiral)}

    boundary_edges = sum(1 for o in edge_owners.values() if len(o) == 1)
    return up_of, adjacency, ring, spiral_pos, boundary_edges


POINTS_UP, ADJACENCY, RING, SPIRAL_POS, _N_BOUNDARY_EDGES = _build()

assert set(ADJACENCY) == VALID_TRIANGLES
