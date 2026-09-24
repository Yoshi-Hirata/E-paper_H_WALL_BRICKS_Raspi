"""The order the scales change in: ranks, spans and delay tables."""

from __future__ import annotations

import io
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conductor.look import LookMap, default_shift  # noqa: E402
from conductor.sequence import (NO_DELAY, SEQUENCES, TABLE_LEN,  # noqa: E402
                                clean_span, compile_delays, ranks, span_s)

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


def unpack(table: bytes) -> "tuple[int, ...]":
    assert len(table) == TABLE_LEN
    return struct.unpack(">64H", table)


def by_socket(board):
    return lambda tables: {n: unpack(tables[board])[n] for n in range(1, 8)}


def test_rows_top_down_and_bottom_up():
    r = ranks(look(), "top_down")
    assert r[("front", 2, 1)] == 0 and r[("front", 1, 2)] == 1 and r[("front", 0, 2)] == 2
    assert r[("back", 1, 1)] == 1 and r[("back", 0, 2)] == 2     # the same rows
    r = ranks(look(), "bottom_up")
    assert r[("front", 0, 2)] == 0 and r[("front", 2, 3)] == 2 and r[("back", 1, 3)] == 1


def test_left_and_right_are_still_the_audiences_for_each_side():
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


def test_centre_uses_the_maps_own_shift():
    # Row 1 defaults to 0.5 (odd); a map whose own shift column pins it to 0
    # moves the centroid enough that at least one scale's rank changes (real
    # data: AZ271SD1301, 215 of 1482 scales change rank - fix round
    # finding 11). The two maps agree on everything else.
    plain = """side,row,col,board_no,socket,label
front,0,1,1,1,
front,0,2,1,2,
front,1,1,1,3,
front,1,2,1,4,
front,1,3,1,5,
front,1,4,1,6,
front,2,2,1,7,
"""
    with_shift = """side,row,col,board_no,socket,label,shift
front,0,1,1,1,,0
front,0,2,1,2,,0
front,1,1,1,3,,0
front,1,2,1,4,,0
front,1,3,1,5,,0
front,1,4,1,6,,0
front,2,2,1,7,,0
"""
    plain_map = LookMap.parse(io.StringIO(plain), name="p.csv")
    shift_map = LookMap.parse(io.StringIO(with_shift), name="s.csv")
    assert shift_map.shift("front", 1) == 0.0 != default_shift(1)
    plain_ranks = ranks(plain_map, "center")
    shift_ranks = ranks(shift_map, "center")
    assert plain_ranks != shift_ranks
    assert plain_ranks[("front", 0, 1)] != shift_ranks[("front", 0, 1)]


def test_a_span_of_zero_or_a_single_rank_is_no_sweep():
    assert set(ranks(look(), "natural").values()) == {0}
    assert span_s(look(), "natural", 3.0) == 0
    assert span_s(look(), "top_down", 0.0) == 0.0
    assert span_s(look(), "top_down", 3.0) == pytest.approx(3.0)
    assert "natural" in SEQUENCES and len(SEQUENCES) == 6
    # A map whose ranks never move (one scale: nothing to rank against)
    # sweeps nothing either - there is nothing for a delay to time.
    single = LookMap.parse(io.StringIO(
        "side,row,col,board_no,socket,label\nfront,0,1,5,1,\n"), name="s.csv")
    assert span_s(single, "top_down", 3.0) == 0.0
    assert unpack(compile_delays(single, "top_down", 3.0)[1]) == (NO_DELAY,) * 64


def test_delay_tables_are_uint16_frames_of_ten_ms():
    tables = compile_delays(look(), "top_down", 2.0)
    front = by_socket(1)(tables)
    # Ranks 0,0,0 / 1,1,1 / 2 over a 2.0 s span (max rank 2): 0, 100, 200 frames.
    assert front == {1: 0, 2: 0, 3: 0, 4: 100, 5: 100, 6: 100, 7: 200}
    assert unpack(tables[1])[0] == NO_DELAY and unpack(tables[1])[63] == NO_DELAY
    back = by_socket(2)(tables)
    assert back[1] == 100 and back[3] == 200
    for table in tables.values():
        assert len(table) == TABLE_LEN
    # A natural table says "no delay" everywhere - it clears a sweep.
    assert set(unpack(compile_delays(look(), "natural", 2.0)[1])) == {NO_DELAY}


def test_the_last_rank_starts_exactly_span_seconds_after_the_first():
    for span in (0.5, 1.0, 2.37, 12.0):
        tables = compile_delays(look(), "top_down", span)
        last = max(v for t in tables.values() for v in unpack(t) if v != NO_DELAY)
        assert last == round(span / 0.01)              # frames of 10 ms
    # Equal ranks give byte-identical frames wherever they occur.
    tables = compile_delays(look(), "top_down", 3.0)
    front = unpack(tables[1])
    assert front[4] == front[5] == front[6]


def test_sockets_without_a_scale_and_index_0_and_63_are_no_delay():
    tables = compile_delays(look(), "top_down", 1.0)
    for table in tables.values():
        values = unpack(table)
        assert values[0] == NO_DELAY and values[63] == NO_DELAY
    assert unpack(tables[1])[8] == NO_DELAY             # board 17 has no socket 8


def test_the_unit_wide_addressing_is_honoured():
    tables = compile_delays(look(), "bottom_up", 1.0, ids={17: 5, 18: 9})
    assert set(tables) == {5, 9}


def test_span_is_clamped_and_junk_becomes_zero():
    assert clean_span("3.456") == 3.46
    assert clean_span(-5) == 0.0
    assert clean_span(999) == 120.0
    assert clean_span("nope") == 0.0
    assert clean_span(None) == 0.0
    assert clean_span(float("nan")) == 0.0
