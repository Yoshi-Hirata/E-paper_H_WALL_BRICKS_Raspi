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
    assert len(found["c"]) == 1 and "10 秒しか" in found["c"][0]
    assert "基板 16 枚では 14 秒必要" in found["c"][0]
    # 20 s apart is enough at 7 s a refresh - and was not at 16 s.
    cues[2] = cue("c", "Look22", "2:20", "p1")
    assert problems(cues)[0]["c"] == []
    found, _ = validate(cues, ITEMS, 600, refresh=16)
    assert "20 秒しか" in found["c"][0] and "23 秒必要" in found["c"][0]


def test_the_first_cue_must_leave_time_to_write_the_boards_after_start():
    # Sent at 0:03 - the preset's refresh ended at 0:00, and 16 boards
    # take ~3.5 s + margin to write, so 3 s is too tight; 0:40 is fine.
    found, _ = problems([cue("a", "Look22", 0, "p1"),
                         cue("b", "Look22", "0:10", "p2")])
    assert found["b"] and "秒しか" in found["b"][0]
    found, _ = problems([cue("a", "Look22", 0, "p1"),
                         cue("b", "Look22", "0:40", "p2")])
    assert found["b"] == []


def test_done_before_a_refresh_fits_is_refused():
    found, _ = problems([cue("a", "Look22", "0:05", "p1")])
    assert "完成できません" in found["a"][0] and "0:07 以降" in found["a"][0]
    # The same instant as a change that *starts* then is fine.
    found, warnings = problems([cue("a", "Look22", "0:05", "p1", align="start")])
    assert found["a"] == []
    assert "プリセットがありません" in warnings[0]


def test_items_sharing_a_unit_share_its_bus():
    same_moment = [cue("a", "Look20-Top", "1:00", "t1"),
                   cue("b", "Look20-Skirt", "1:00", "s1")]
    found, _ = problems(same_moment)
    assert found["a"] == found["b"] == []           # one refresh for both
    staggered = [cue("a", "Look20-Top", "1:00", "t1"),
                 cue("b", "Look20-Skirt", "1:10", "s1")]
    found, _ = problems(staggered)
    assert "radxa-02" in found["b"][0] and "基板 32 枚" in found["b"][0]
    # Another unit is another bus: no conflict with Look22 ten seconds on.
    found, _ = problems(same_moment + [cue("c", "Look22", "1:10", "p1")])
    assert found["c"] == []


def test_design_must_exist_and_suit_the_kind_of_cue():
    found, _ = problems([cue("a", "Look22", "1:00", "nope"),
                         cue("b", "Look22", "2:00", "accent"),
                         cue("c", "Look22", "3:00", "accent", partial=True),
                         cue("d", "Look22", "4:00", "broken", partial=True),
                         cue("e", "Look99", "5:00", "p1")])
    assert "取り込まれていません" in found["a"][0]
    assert "一部更新" in found["b"][0]
    assert found["c"] == []
    assert "問題があります" in found["d"][0]
    assert "アイテムがありません" in found["e"][0]


def test_after_the_end_and_double_booking():
    found, _ = problems([cue("a", "Look22", "6:00", "p1"),
                         cue("b", "Look22", "6:00", "p2")], duration=300)
    assert any("終了" in p for p in found["a"])
    assert any("同じ瞬間" in p for p in found["b"])


def test_clean_drops_junk_and_sorts():
    cues = clean([{"id": "z", "item": "B", "at": "1:00", "design": "d"},
                  "junk", {"item": "A", "at": -5, "design": "d",
                           "align": "??", "partial": 1}])
    assert [c["item"] for c in cues] == ["A", "B"]
    assert cues[0]["at"] == 0 and cues[0]["align"] == "done"
    assert cues[0]["partial"] is True and cues[0]["id"]
