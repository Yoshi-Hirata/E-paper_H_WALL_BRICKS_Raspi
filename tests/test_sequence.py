"""The order the scales change in: ranks, spans and delay tables."""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conductor.look import LookMap  # noqa: E402
from conductor.sequence import (MAX_UNITS, NO_DELAY, SEQUENCES, clean_step,  # noqa: E402
                                compile_delays, ranks, span_s)

# A little garment: front 3 rows x 3 columns on board 17, back 2 rows on 18.
MAP = """side,row,col,board_no,socket,label
front,2,1,17,1,
front,2,2,17,2,
front,2,3,17,3,
front,1,1,17,4,
front,1,2,17,5,
front,1,3,17,6,
front,0,2,17,7,
back,1,1,18,1,
back,1,3,18,2,
back,0,2,18,3,
"""


def look():
    return LookMap.parse(io.StringIO(MAP), name="m.csv")


def by_socket(board):
    return lambda table: {n: table[board][n] for n in range(1, 8)}


def test_rows_top_down_and_bottom_up():
    r = ranks(look(), "top_down")
    assert r[("front", 2, 1)] == 0 and r[("front", 1, 2)] == 1 and r[("front", 0, 2)] == 2
    assert r[("back", 1, 1)] == 1 and r[("back", 0, 2)] == 2     # the same rows
    r = ranks(look(), "bottom_up")
    assert r[("front", 0, 2)] == 0 and r[("front", 2, 3)] == 2 and r[("back", 1, 3)] == 1


def test_left_and_right_are_the_audiences_for_each_side():
    # Columns run towards the wearer's right. Facing the front, the
    # audience's left is the wearer's right: the last column goes first.
    r = ranks(look(), "left_right")
    assert r[("front", 2, 3)] == 0 and r[("front", 2, 1)] == 2
    # Behind the model the audience's left is the wearer's left.
    assert r[("back", 1, 1)] == 0 and r[("back", 1, 3)] == 2
    r = ranks(look(), "right_left")
    assert r[("front", 2, 1)] == 0 and r[("back", 1, 3)] == 0


def test_centre_is_the_fronts_centroid_and_the_back_uses_the_same_point():
    r = ranks(look(), "center")
    assert r[("front", 1, 2)] == 0                    # the middle scale
    assert r[("front", 2, 1)] >= 1 and r[("front", 0, 2)] >= 1
    assert r[("back", 1, 1)] == r[("front", 1, 1)]    # straight behind


def test_natural_has_no_ranks_and_no_span():
    assert set(ranks(look(), "natural").values()) == {0}
    assert span_s(look(), "natural", 0.1) == 0
    assert span_s(look(), "top_down", 0.1) == pytest.approx(0.2)
    assert span_s(look(), "top_down", 2.0) == pytest.approx(4.0)
    assert "natural" in SEQUENCES and len(SEQUENCES) == 6


def test_delay_tables_are_per_board_by_socket_in_tenths_of_a_second():
    tables = compile_delays(look(), "top_down", 0.5)
    front = by_socket(1)(tables)
    assert front == {1: 0, 2: 0, 3: 0, 4: 5, 5: 5, 6: 5, 7: 10}
    assert tables[1][0] == NO_DELAY and tables[1][63] == NO_DELAY
    assert tables[1][8] == NO_DELAY                     # no scale there
    assert by_socket(2)(tables)[1] == 5 and by_socket(2)(tables)[3] == 10
    # A natural table says "no delay" everywhere - it clears a sweep.
    assert set(compile_delays(look(), "natural", 0.1)[1]) == {NO_DELAY}
    # Long sweeps are capped at what a byte can say.
    capped = compile_delays(look(), "top_down", 5.0)
    assert by_socket(1)(capped)[7] == min(MAX_UNITS, 100)
    assert all(v <= MAX_UNITS or v == NO_DELAY for v in capped[1])


def test_the_unit_wide_addressing_is_honoured():
    tables = compile_delays(look(), "bottom_up", 0.1, ids={17: 5, 18: 9})
    assert set(tables) == {5, 9}


def test_step_is_tidied():
    assert clean_step("0.25") == 0.2 or clean_step("0.25") == 0.3
    assert clean_step(0) == 0.1 and clean_step(99) == 5.0 and clean_step("x") == 0.1
