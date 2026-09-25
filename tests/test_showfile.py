"""conductor/showfile.py: what one unit's show file carries per cue.

The delay tables in particular (review F5, 2026-09-25): every cue of
every unit show carries a table for every board - a cue without a sweep
the all-NO_DELAY one - so a board never keeps a sweep from an earlier
upload in a slot this show uses without one.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conductor.sequence import NO_DELAY
from conductor.server import Workspace
from conductor.showfile import NO_SWEEP_TABLE
from tests.test_look import GRID, MAP, SKIRT_GRID, SKIRT_MAP

P1 = "Look20-Top_color_pattern01_grid.csv"
P2 = "Look20-Top_color_pattern02_grid.csv"
S1 = "Look20-Skirt_color_pattern01_grid.csv"
ACCENT = "side,row,shift,1,2,3\nfront,1,0.5,0x08,-,0\nfront,0,0,-,0,0\nback,0,0,0,-,0\n"


def workspace(tmp_path):
    ws = Workspace(tmp_path / "ws")
    ws.save("Look20-Top_map.csv", MAP)
    ws.save(P1, GRID)
    ws.save(P2, ACCENT)
    ws.save("Look20-Skirt_map.csv", SKIRT_MAP)
    ws.save(S1, SKIRT_GRID)
    ws.assign("Look20-Top", "radxa-02")
    ws.assign("Look20-Skirt", "radxa-02")
    return ws


def cue(id_, item, at, design, **more):
    return dict({"id": id_, "item": item, "at": at, "design": design}, **more)


def tables(entry) -> "dict[str, tuple[int, ...]]":
    return {a: struct.unpack(">64H", bytes.fromhex(h))
            for a, h in entry["delays"].items()}


def test_the_no_sweep_table_is_all_no_delay():
    assert len(NO_SWEEP_TABLE) == 128
    assert set(struct.unpack(">64H", NO_SWEEP_TABLE)) == {NO_DELAY}


def test_a_show_without_any_sweep_still_carries_a_table_per_board_and_cue(tmp_path):
    # The boards may hold delay tables from an EARLIER upload that had
    # sweeps; only a table in the file makes the unit clear them
    # (0x25) for this slot - so no cue may go out without one.
    ws = workspace(tmp_path)
    ws.set_timeline(60, [cue("a", "Look20-Top", 0, P1),
                         cue("b", "Look20-Skirt", 0, S1),
                         cue("c", "Look20-Top", 20, P2, partial=True)],
                    refresh=1.0)
    shows, problems = ws.compile_show()
    assert problems == []
    show = shows["radxa-02"]
    for entry in show["cues"]:
        assert sorted(entry["delays"]) == [str(a) for a in show["boards"]]
        assert all(t == struct.unpack(">64H", NO_SWEEP_TABLE)
                   for t in tables(entry).values())
        assert entry["span"] == 0.0


def test_a_sweep_gives_its_own_boards_a_table_and_the_rest_no_delay(tmp_path):
    # Top and Skirt share radxa-02. A sweep on the Top at 0:20 must not
    # leave the Skirt's boards without a table (they used to be left out
    # of `delays` altogether at that moment), and the preset before it -
    # no sweep anywhere - still carries the all-NO_DELAY tables.
    ws = workspace(tmp_path)
    ws.set_timeline(60, [cue("a", "Look20-Top", 0, P1),
                         cue("b", "Look20-Skirt", 0, S1),
                         cue("c", "Look20-Top", 20, P1,
                             transition="custom", sequence="top_down",
                             span_s=2.0)],
                    refresh=1.0)
    shows, problems = ws.compile_show()
    assert problems == []
    show = shows["radxa-02"]
    preset, swept = show["cues"]
    assert all(set(t) == {NO_DELAY} for t in tables(preset).values())
    by_board = tables(swept)
    assert sorted(by_board) == [str(a) for a in show["boards"]]
    timed = {a for a, t in by_board.items() if set(t) != {NO_DELAY}}
    assert timed                                    # the Top's boards sweep
    assert timed < set(by_board)                    # the Skirt's do not
    assert swept["span"] == 2.0
    assert max(v for t in by_board.values() for v in t if v != NO_DELAY) == 200


def test_a_custom_transition_at_span_zero_sweeps_nothing(tmp_path):
    # timeline.sweeps()' own definition, honoured per cue: a custom
    # sequence left at 0 s is not a sweep, so its boards get the
    # clearing table rather than one of all-zero frames.
    ws = workspace(tmp_path)
    ws.set_timeline(60, [cue("a", "Look20-Top", 0, P1),
                         cue("c", "Look20-Top", 20, P1,
                             transition="custom", sequence="top_down",
                             span_s=0.0)],
                    refresh=1.0)
    shows, problems = ws.compile_show()
    assert problems == []
    for entry in shows["radxa-02"]["cues"]:
        assert all(set(t) == {NO_DELAY} for t in tables(entry).values())
