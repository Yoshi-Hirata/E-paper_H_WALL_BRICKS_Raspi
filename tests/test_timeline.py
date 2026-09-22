"""conductor/timeline.py: what a cue's time means, and what a unit can do.

Pure rules, no files: the items are handed in as the facts the rules
need (unit, board count, which designs pass as full / partial cues).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conductor.timeline import (REFRESH_S, clean, format_clock, min_interval,
                                parse_clock, times, validate)

OK = {"full": True, "partial": True}
ITEMS = {
    "look22": {"item": "Look22", "unit": "radxa-03", "boards": 16,
               "designs": {"p1": OK, "p2": OK,
                           "accent": {"full": False, "partial": True},
                           "broken": {"full": False, "partial": False}}},
    "look20-top": {"item": "Look20-Top", "unit": "radxa-02", "boards": 16,
                   "designs": {"t1": OK, "t2": OK}},
    "look20-skirt": {"item": "Look20-Skirt", "unit": "radxa-02", "boards": 16,
                     "designs": {"s1": OK, "s2": OK}},
}


def cue(id_, item, at, design, align="done", partial=False):
    return clean([{"id": id_, "item": item, "at": at, "design": design,
                   "align": align, "partial": partial}])[0]


def problems(cues, duration=600):
    found, warnings = validate(cues, ITEMS, duration)
    return found, warnings


def test_clock_both_ways():
    assert parse_clock("3:05") == 185
    assert parse_clock("1:03:05") == 3785
    assert parse_clock("185") == parse_clock(185) == 185
    assert format_clock(185) == "3:05" and format_clock(-16) == "-0:16"
    assert REFRESH_S == 7.0            # latest firmware, reported 2026-09-21
    with pytest.raises(ValueError):
        parse_clock("soon")


def test_a_time_means_done_unless_the_cue_says_start():
    assert times(cue("a", "Look22", "1:00", "p1")) == (53, 60)
    assert times(cue("a", "Look22", "1:00", "p1", align="start")) == (60, 67)
    # 0:00 is the preset: on the garment before START, whatever the align.
    assert times(cue("a", "Look22", 0, "p1", align="start")) == (-7, 0)
    # A unit on older firmware: the show carries its own refresh time.
    assert times(cue("a", "Look22", "1:00", "p1"), refresh=16) == (44, 60)


def test_min_interval_grows_with_the_boards_to_write():
    assert min_interval(16) == pytest.approx(7 + 3.52 + 3)       # 13.5 s
    assert min_interval(36) == pytest.approx(7 + 7.92 + 3)       # 17.9 s
    assert min_interval(16, refresh=16) == pytest.approx(16 + 3.52 + 3)


def test_a_plain_show_has_no_problems():
    cues = [cue("a", "Look22", 0, "p1"), cue("b", "Look22", "2:00", "p2"),
            cue("c", "Look22", "5:30", "p1", align="start")]
    found, warnings = problems(cues)
    assert all(not v for v in found.values()) and warnings == []


def test_refreshes_on_one_unit_need_room():
    cues = [cue("a", "Look22", 0, "p1"), cue("b", "Look22", "2:00", "p2"),
            cue("c", "Look22", "2:10", "p1")]
    found, _ = problems(cues)
    assert found["b"] == []
    assert len(found["c"]) == 1 and "only 10 s" in found["c"][0]
    assert "16 boards need 14 s" in found["c"][0]
    # 20 s apart is enough at 7 s a refresh - and was not at 16 s.
    cues[2] = cue("c", "Look22", "2:20", "p1")
    assert problems(cues)[0]["c"] == []
    found, _ = validate(cues, ITEMS, 600, refresh=16)
    assert "only 20 s" in found["c"][0] and "need 23 s" in found["c"][0]


def test_the_first_cue_must_leave_time_to_write_the_boards_after_start():
    # Sent at 0:03 - the preset's refresh ended at 0:00, and 16 boards
    # take ~3.5 s + margin to write, so 3 s is too tight; 0:40 is fine.
    found, _ = problems([cue("a", "Look22", 0, "p1"),
                         cue("b", "Look22", "0:10", "p2")])
    assert found["b"] and "only 10 s" in found["b"][0]
    found, _ = problems([cue("a", "Look22", 0, "p1"),
                         cue("b", "Look22", "0:40", "p2")])
    assert found["b"] == []


def test_done_before_a_refresh_fits_is_refused():
    found, _ = problems([cue("a", "Look22", "0:05", "p1")])
    assert "cannot be complete" in found["a"][0] and "0:07 and later" in found["a"][0]
    # The same instant as a change that *starts* then is fine.
    found, warnings = problems([cue("a", "Look22", "0:05", "p1", align="start")])
    assert found["a"] == []
    assert "no preset at 0:00" in warnings[0]


def test_items_sharing_a_unit_share_its_bus():
    same_moment = [cue("a", "Look20-Top", "1:00", "t1"),
                   cue("b", "Look20-Skirt", "1:00", "s1")]
    found, _ = problems(same_moment)
    assert found["a"] == found["b"] == []           # one refresh for both
    staggered = [cue("a", "Look20-Top", "1:00", "t1"),
                 cue("b", "Look20-Skirt", "1:10", "s1")]
    found, _ = problems(staggered)
    assert "radxa-02" in found["b"][0] and "32 boards" in found["b"][0]
    # Another unit is another bus: no conflict with Look22 ten seconds on.
    found, _ = problems(same_moment + [cue("c", "Look22", "1:10", "p1")])
    assert found["c"] == []


def test_design_must_exist_and_suit_the_kind_of_cue():
    found, _ = problems([cue("a", "Look22", "1:00", "nope"),
                         cue("b", "Look22", "2:00", "accent"),
                         cue("c", "Look22", "3:00", "accent", partial=True),
                         cue("d", "Look22", "4:00", "broken", partial=True),
                         cue("e", "Look99", "5:00", "p1")])
    assert "is not loaded" in found["a"][0]
    assert "partial cue" in found["b"][0]
    assert found["c"] == []
    assert "has problems" in found["d"][0]
    assert "no such item" in found["e"][0]


def test_after_the_end_and_double_booking():
    found, _ = problems([cue("a", "Look22", "6:00", "p1"),
                         cue("b", "Look22", "6:00", "p2")], duration=300)
    assert any("after the end" in p for p in found["a"])
    assert any("same moment" in p for p in found["b"])


def test_clean_drops_junk_and_sorts():
    cues = clean([{"id": "z", "item": "B", "at": "1:00", "design": "d"},
                  "junk", {"item": "A", "at": -5, "design": "d",
                           "align": "??", "partial": 1}])
    assert [c["item"] for c in cues] == ["A", "B"]
    assert cues[0]["at"] == 0 and cues[0]["align"] == "done"
    assert cues[0]["partial"] is True and cues[0]["id"]


def test_send_instants_are_compared_to_the_millisecond():
    # 10.3 - 7.3 is 3.000000000000001 in floating point; a change that
    # starts at 3.0 is the same instant and must be seen as such.
    done = cue("a", "Look22", 10.3, "p1")
    start = cue("b", "Look22", 3.0, "p2", align="start")
    assert times(done, refresh=7.3)[0] == times(start, refresh=7.3)[0] == 3.0
    found, _ = validate([done, start], ITEMS, 600, refresh=7.3)
    assert any("same moment" in p for p in found["b"])
    assert not any("only 0 s" in p for p in found["a"] + found["b"])
    # On a shared unit the two items become one broadcast, not a clash.
    top = cue("c", "Look20-Top", 10.3, "t1")
    skirt = cue("d", "Look20-Skirt", 3.0, "s1", align="start")
    found, _ = validate([top, skirt], ITEMS, 600, refresh=7.3)
    assert found["c"] == found["d"] == []


# ---- sweeps ----

def test_a_sweep_lengthens_the_change_and_the_room_after_it():
    swept = cue("a", "Look22", 60, "g1.csv")
    swept.update(sequence="top_down", step_s=0.5, span=4.0)
    sent, complete = times(swept, 7.0)
    assert (sent, complete) == (49.0, 60.0)             # 7 s refresh + 4 s sweep
    swept["align"] = "start"
    assert times(swept, 7.0) == (60.0, 71.0)
    # The next refresh on the unit must wait for the sweep too.
    items = {"look22": {"item": "Look22", "unit": "radxa-01", "boards": 2,
                        "designs": {"g1.csv": {"full": True, "partial": True}}}}
    # Sent at 49; the bus is busy 7 + 4 s, then 2 boards + margin: 14.44 s.
    swept["align"] = "done"
    later = cue("b", "Look22", 49 + 14.44 - 1 + 7, "g1.csv")     # 1 s short
    problems, _ = validate([swept, later], items, 600, 7.0)
    assert problems["b"] and "sweep" in problems["b"][0]
    later["at"] = 49 + 14.44 + 1 + 7
    problems, _ = validate([swept, later], items, 600, 7.0)
    assert problems["b"] == []


def test_a_sweep_without_its_map_is_reported_not_guessed():
    swept = cue("a", "Look22", 60, "g1.csv")
    swept.update(sequence="center", step_s=0.1)          # no span given
    items = {"look22": {"item": "Look22", "unit": None, "boards": 2,
                        "designs": {"g1.csv": {"full": True, "partial": True}}}}
    problems, _ = validate([swept], items, 600, 7.0)
    assert any("map" in p for p in problems["a"])


def test_clean_keeps_sequence_and_step_and_tidies_them():
    cues = clean([{"id": "a", "item": "L", "at": 5, "design": "d",
                   "sequence": "left_right", "step_s": "0.3"},
                  {"id": "b", "item": "L", "at": 6, "design": "d",
                   "sequence": "sideways", "step_s": -1}])
    assert (cues[0]["sequence"], cues[0]["step_s"]) == ("left_right", 0.3)
    assert (cues[1]["sequence"], cues[1]["step_s"]) == ("natural", 0.1)
