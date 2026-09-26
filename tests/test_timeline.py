"""conductor/timeline.py: what a cue's time means, and what a unit can do.

Pure rules, no files: the items are handed in as the facts the rules
need (unit, board count, which designs pass as full / partial cues).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conductor.timeline import (MAX_CUES_PER_UNIT, PANEL_REPAINT_S, REFRESH_S,
                                apply_transitions, clean, complete_s,
                                effective_refresh, ends, format_clock,
                                min_interval, panel_refresh, panel_repaint_of,
                                parse_clock, resolve, times, validate)

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


def cue(id_, item, at, design, partial=False, refresh_s=None):
    return clean([{"id": id_, "item": item, "at": at, "design": design,
                   "partial": partial, "refresh_s": refresh_s}])[0]


def problems(cues, duration=600):
    found, warnings = validate(cues, ITEMS, duration)
    return found, warnings


def test_clock_both_ways():
    assert parse_clock("3:05") == 185
    assert parse_clock("1:03:05") == 3785
    assert parse_clock("185") == parse_clock(185) == 185
    assert format_clock(185) == "3:05" and format_clock(-16) == "-0:16"
    # The MODELLED refresh, the effect included (2026-09-26, the operator:
    # 8 s covers the ~7 s repaint of the latest firmware plus the 1 s sweep
    # a production cue uses), and the physical repaint it is built on.
    assert REFRESH_S == 8.0
    assert PANEL_REPAINT_S == 7.0      # latest firmware, reported 2026-09-21
    with pytest.raises(ValueError):
        parse_clock("soon")
    # Hostile JSON (None, a dict) is the same "not a time", never a
    # TypeError the caller has to expect.
    with pytest.raises(ValueError):
        parse_clock(None)
    with pytest.raises(ValueError):
        parse_clock({})


def test_start_is_the_send_instant_and_complete_adds_refresh_and_sweep():
    # The modelled refresh is the whole change, the effect included.
    assert times(cue("a", "Look22", "1:00", "p1")) == (60, 68)
    # 0:00 is the preset: sent one refresh before the show even begins,
    # so it is already complete when it starts.
    assert times(cue("a", "Look22", 0, "p1")) == (-8, 0)
    # A unit on older firmware: the show carries its own refresh time.
    assert times(cue("a", "Look22", "1:00", "p1"), refresh=16) == (60, 76)
    swept = cue("a", "Look22", 60, "g1.csv")
    swept["sweep"] = {"sequence": "top_down", "span_s": 4.0, "source": "cue"}
    swept["span"] = 4.0
    # 7 s repaint + 4 s sweep runs past the 8 s refresh, so it wins.
    assert times(swept, 7.0) == (60.0, 71.0)
    assert times(swept) == (60.0, 71.0)


def test_complete_is_the_refresh_unless_the_sweep_really_runs_longer():
    """The operator's 2026-09-26 decision: a cue's refresh is the seconds
    from its send to "picture complete", the sweep INCLUDED - 8 s covers
    the 7 s repaint plus the 1 s span a production cue uses. A sweep only
    lengthens a cue when it genuinely finishes later."""
    natural = cue("a", "Look22", 60, "p1")
    assert complete_s(natural) == 8.0                   # budgeted as 8 s
    swept = cue("b", "Look22", 60, "g1.csv")
    swept["sweep"] = {"sequence": "top_down", "span_s": 1.0, "source": "cue"}
    swept["span"] = 1.0
    assert complete_s(swept) == 8.0                     # 7 + 1, inside the 8
    assert times(swept) == (60.0, 68.0)
    long_sweep = dict(swept, span=7.0)
    assert complete_s(long_sweep) == 14.0               # 7 s repaint + 7 s sweep
    assert times(long_sweep) == (60.0, 74.0)
    # A cue's own refresh above panel + span wins outright.
    own = dict(swept, refresh_s=12.0)
    assert complete_s(own) == 12.0
    # A legacy show still set to 7.0: a swept cue becomes 8 s by the max
    # rule, a natural one stays 7 s until the operator changes the setting.
    assert complete_s(swept, refresh=7.0) == 8.0
    assert complete_s(natural, refresh=7.0) == 7.0
    # A refresh set BELOW one physical repaint (a bench run, the
    # compressed shows the tests drive) is the operator's own statement
    # about the panel: the 7 s constant is not put back on top of it.
    assert panel_repaint_of(natural, refresh=1.0) == 1.0
    assert complete_s(natural, refresh=1.0) == 1.0
    assert complete_s(dict(swept, span=2.0), refresh=1.0) == 3.0


def test_a_cue_may_carry_its_own_refresh_time():
    own = cue("a", "Look22", "1:00", "p1", refresh_s=3.0)
    assert own["refresh_s"] == 3.0
    assert effective_refresh(own, 7.0) == 3.0
    assert times(own, refresh=7.0) == (60, 63)          # its own 3 s, not the show's
    plain = cue("b", "Look22", "1:00", "p1")
    assert plain["refresh_s"] is None
    assert effective_refresh(plain, 7.0) == 7.0
    # Kept whatever its range - validate() is where it becomes a problem,
    # not clean() silently clamping it.
    bad = cue("c", "Look22", "1:00", "p1", refresh_s=99)
    assert bad["refresh_s"] == 99.0
    found, _ = problems([bad])
    assert any("1-60" in p for p in found["c"])
    # Junk falls back to the show's own refresh time (None), same as
    # never setting one.
    assert cue("d", "Look22", 0, "p1", refresh_s="fast")["refresh_s"] is None


def test_end_is_the_next_cues_start_or_the_shows_end():
    a = cue("a", "Look22", 0, "p1")
    b = cue("b", "Look22", "1:00", "p2")
    c = cue("c", "Look20-Top", "0:30", "t1")
    result = ends([a, b, c], refresh=7.0, duration=600)
    assert result["a"] == (60, "next")          # the next Look22 cue's Start
    assert result["b"] == (600, "show")         # the last on its track
    assert result["c"] == (600, "show")         # the only cue of its item here


def test_a_next_cue_before_the_picture_is_complete_is_a_problem_in_those_words():
    items = {"look22": {"item": "Look22", "unit": "radxa-01", "boards": 2,
                        "designs": {"p1": OK, "p2": OK}}}
    early = cue("a", "Look22", 60, "p1")             # complete at 67
    late = cue("b", "Look22", 65, "p2")              # starts before that
    found, _ = validate([early, late], items, 600, 7.0)
    assert any("previous picture is complete" in p and "1:07" in p
              for p in found["b"])
    # It replaces the bus-spacing message for this pair (the refresh term
    # is what would have bound here) - not add to it.
    assert not any("after the previous" in p for p in found["b"])
    # Comfortably clear of both the overlap and the bus-room rule: no
    # problem at all.
    late["at"] = 72
    found, _ = validate([early, late], items, 600, 7.0)
    assert found["b"] == []


def test_clean_drops_align_and_keeps_refresh_s():
    cues = clean([{"id": "z", "item": "B", "at": "1:00", "design": "d",
                   "align": "start"},
                  {"id": "y", "item": "A", "at": 5, "design": "d",
                   "align": "done", "refresh_s": "3.456"}])
    assert "align" not in cues[0] and "align" not in cues[1]
    assert [c["id"] for c in cues] == ["y", "z"]        # sorted by "at": A, B
    assert cues[0]["refresh_s"] == 3.5          # rounded to 1 decimal
    assert clean([{"id": "x", "item": "A", "at": 0, "design": "d",
                   "refresh_s": "fast"}])[0]["refresh_s"] is None
    assert clean([{"id": "x", "item": "A", "at": 0, "design": "d"}]
                )[0]["refresh_s"] is None


def test_min_interval_is_just_refresh_and_gap_whatever_the_board_count():
    # Every picture is burned into its slot at Upload time (showfile.py):
    # a running send is a broadcast trigger, nothing is written - so the
    # board count no longer bounds the spacing, only refresh + the 1 s gap.
    # 9.0 s with the effect-inclusive refresh, whatever the cue does: a
    # production sweep is already inside the 8 s (2026-09-26).
    assert min_interval(16) == pytest.approx(9.0)
    assert min_interval(36) == pytest.approx(9.0)
    assert min_interval(96) == pytest.approx(9.0)          # still 9.0: unaffected
    assert min_interval(16, refresh=16) == pytest.approx(17.0)   # 16 + 1 s gap
    assert min_interval(16, refresh=7.0) == pytest.approx(8.0)   # a legacy show


def test_the_next_refresh_may_start_one_second_after_the_previous_is_complete():
    # The director's 1 s gap after the picture completes is all two sends
    # on one unit need, as long as writing 16 boards (well under that,
    # with the unit's own constants) does not need more room.
    items = {"look22": {"item": "Look22", "unit": "radxa-04", "boards": 16,
                        "designs": {"p1": OK, "p2": OK}}}
    a = cue("a", "Look22", 60, "p1")               # complete at 67
    fine = cue("b", "Look22", 68.0, "p2")          # 8.0 s later: 7 s + 1 s gap
    assert validate([a, fine], items, 600, 7.0)[0]["b"] == []
    tight = cue("b", "Look22", 67.9, "p2")         # 7.9 s later: 0.1 s short
    found, _ = validate([a, tight], items, 600, 7.0)
    assert found["b"] == [
        "only 7.9 s after the previous send on radxa-04; at least 8.0 s "
        "is needed (7.0 s refresh + 1.0 s gap)"]


def test_the_first_cue_after_the_preset_only_needs_refresh_and_gap():
    # Nothing is written once the show runs any more - every picture was
    # burned into its slot at Upload time - so even the very first cue
    # after the preset only needs the ordinary refresh + gap floor, board
    # count and all the old "rejoining" concerns aside.
    items = {"look22": {"item": "Look22", "unit": "radxa-08", "boards": 96,
                        "designs": {"p1": OK, "p2": OK}}}
    preset = cue("p", "Look22", 0, "p1")
    tight = cue("f", "Look22", 0.9, "p2")          # 7.9 s send-to-send: short
    found, _ = validate([preset, tight], items, 600, 7.0)
    assert found["f"] == [
        "only 7.9 s after the previous send on radxa-08; at least 8.0 s "
        "is needed (7.0 s refresh + 1.0 s gap)"]
    fine = cue("f", "Look22", 1.0, "p2")           # 8.0 s: fine
    assert validate([preset, fine], items, 600, 7.0)[0]["f"] == []


def test_a_unit_may_carry_at_most_eighteen_show_pictures():
    # A board has MAX_CUES_PER_UNIT (18) usable slots for the show - slot
    # 0 is the standby white, slot 19 the manual one-shot, never the
    # show's own timeline. The 19th distinct send on one unit's bus does
    # not fit, however comfortably spaced, and every cue from there on is
    # named in the problem.
    assert MAX_CUES_PER_UNIT == 18
    items = {"look22": {"item": "Look22", "unit": "radxa-09", "boards": 4,
                        "designs": {"p1": OK, "p2": OK}}}
    cues = [cue("preset", "Look22", 0, "p1")]
    for index in range(1, 19):                     # 18 more: 19 pictures total
        design = "p1" if index % 2 else "p2"
        cues.append(cue(f"c{index}", "Look22", index * 10.0, design))
    found, _ = validate(cues, items, 600, 7.0)
    # The first 18 pictures (the preset plus 17 more) fit; the 19th does not.
    assert all(found[c["id"]] == [] for c in cues[:18])
    assert found["c18"] == [
        "radxa-09 carries 19 pictures but a board holds 18 show pictures "
        "(slot 0 is the white standby, slot 19 the manual one-shot) - "
        "merge or remove cues"]


def test_coincident_cues_on_a_shared_unit_count_as_one_picture():
    # Look20-Top and Look20-Skirt share a unit; two cues at the very same
    # instant are one broadcast (showfile.py) - and so one picture toward
    # the 18-picture limit, not two.
    items = {"look20-top": {"item": "Look20-Top", "unit": "radxa-10",
                            "boards": 4, "designs": {"t1": OK}},
             "look20-skirt": {"item": "Look20-Skirt", "unit": "radxa-10",
                              "boards": 4, "designs": {"s1": OK}}}
    cues = [cue("pt", "Look20-Top", 0, "t1"), cue("ps", "Look20-Skirt", 0, "s1")]
    for index in range(1, 18):                     # 17 more shared moments
        cues.append(cue(f"t{index}", "Look20-Top", index * 10.0, "t1"))
        cues.append(cue(f"s{index}", "Look20-Skirt", index * 10.0, "s1"))
    found, _ = validate(cues, items, 600, 7.0)      # 18 moments: exactly fits
    assert all(found[c["id"]] == [] for c in cues)
    # A 19th moment tips it over, for every cue sent at that instant.
    cues += [cue("t18", "Look20-Top", 180.0, "t1"),
            cue("s18", "Look20-Skirt", 180.0, "s1")]
    found, _ = validate(cues, items, 600, 7.0)
    assert found["t18"] and "carries 19 pictures" in found["t18"][0]
    assert found["s18"] and "carries 19 pictures" in found["s18"][0]


def test_the_previous_cues_own_refresh_time_sets_the_gap():
    items = {"look22": {"item": "Look22", "unit": "radxa-07", "boards": 16,
                        "designs": {"p1": OK, "p2": OK}}}
    a = cue("a", "Look22", 60, "p1", refresh_s=20.0)   # its own, slower refresh
    fine = cue("b", "Look22", 81.0, "p2")              # 21 s later: 20 + 1 s gap
    assert validate([a, fine], items, 600, 7.0)[0]["b"] == []
    tight = cue("b", "Look22", 80.5, "p2")             # 20.5 s: short of 21 s
    found, _ = validate([a, tight], items, 600, 7.0)
    assert found["b"] == [
        "only 20.5 s after the previous send on radxa-07; at least 21.0 s "
        "is needed (20.0 s refresh + 1.0 s gap)"]


def test_a_sweep_adds_its_span_before_the_gap():
    # A sweep lengthens the previous picture, so its span counts before
    # the director's gap - not instead of it.
    items = {"look22": {"item": "Look22", "unit": "radxa-01", "boards": 2,
                        "designs": {"g1.csv": {"full": True, "partial": True}}}}
    swept = cue("a", "Look22", 49, "g1.csv")
    swept["sweep"] = {"sequence": "top_down", "span_s": 4.0, "source": "cue"}
    swept["span"] = 4.0                            # complete at 49+7+4 = 60
    fine = cue("b", "Look22", 61, "g1.csv")         # 12 s later: 7+4+1
    assert validate([swept, fine], items, 600, 7.0)[0]["b"] == []
    tight = cue("b", "Look22", 60.5, "g1.csv")      # 11.5 s: short of 12 s
    found, _ = validate([swept, tight], items, 600, 7.0)
    assert found["b"] == [
        "only 11.5 s after the previous send on radxa-01; at least 12.0 s "
        "is needed (7.0 s panel repaint + 4.0 s sweep + 1.0 s gap)"]


def test_a_plain_show_has_no_problems():
    cues = [cue("a", "Look22", 0, "p1"), cue("b", "Look22", "2:00", "p2"),
            cue("c", "Look22", "5:30", "p1")]
    found, warnings = problems(cues)
    assert all(not v for v in found.values()) and warnings == []


def test_refreshes_on_one_unit_need_room():
    # b's picture completes at 108 (the 8 s modelled refresh); a send from
    # 108 up to (but not past) 116 is "before the picture is complete", a
    # separate rule (tested above) - this is about the room AFTER that.
    b = cue("b", "Look22", 100, "p2")
    tight = cue("c", "Look22", 108.3, "p1")       # 8.3 s after send: too tight
    found, _ = problems([b, tight])
    assert found["c"] == [
        "only 8.3 s after the previous send on radxa-03; at least 9.0 s "
        "is needed (8.0 s refresh + 1.0 s gap)"]
    # 9 s apart (refresh + the 1 s gap) is enough room at the 8 s default -
    # not at a 16 s refresh, where that same gap needs 17 s.
    fine = cue("c", "Look22", 109, "p1")
    assert problems([b, fine])[0]["c"] == []
    tight16 = cue("c", "Look22", 116.3, "p1")     # 16.3 s after send
    found, _ = validate([b, tight16], ITEMS, 600, refresh=16)
    assert found["c"] == [
        "only 16.3 s after the previous send on radxa-03; at least 17.0 s "
        "is needed (16.0 s refresh + 1.0 s gap)"]
    fine16 = cue("c", "Look22", 117, "p1")
    assert validate([b, fine16], ITEMS, 600, refresh=16)[0]["c"] == []


def test_no_preset_is_a_warning():
    found, warnings = problems([cue("a", "Look22", "0:05", "p1")])
    assert found["a"] == []
    assert "no preset at 0:00" in warnings[0]


def test_the_preset_is_sent_one_refresh_before_the_show():
    preset = cue("a", "Look22", 0, "p1")
    assert times(preset, refresh=7.3) == (-7.3, 0.0)
    # Two items on one unit at the very same instant are one broadcast,
    # not a bus clash.
    top = cue("c", "Look20-Top", 10.3, "t1")
    skirt = cue("d", "Look20-Skirt", 10.3, "s1")
    found, _ = validate([top, skirt], ITEMS, 600, refresh=7.3)
    assert found["c"] == found["d"] == []


def test_items_sharing_a_unit_share_its_bus():
    same_moment = [cue("a", "Look20-Top", 53, "t1"),
                   cue("b", "Look20-Skirt", 53, "s1")]
    found, _ = problems(same_moment)
    assert found["a"] == found["b"] == []           # one refresh for both
    staggered = [cue("a", "Look20-Top", 53, "t1"),
                 cue("b", "Look20-Skirt", 59, "s1")]     # 6 s: short of the
    found, _ = problems(staggered)                       # 9 s refresh+gap floor
    assert found["b"] == [
        "only 6.0 s after the previous send on radxa-02; at least 9.0 s "
        "is needed (8.0 s refresh + 1.0 s gap)"]
    # Another unit is another bus: no conflict with Look22 ten seconds on.
    found, _ = problems(same_moment + [cue("c", "Look22", 63, "p1")])
    assert found["c"] == []


def test_a_sweep_on_one_item_of_a_shared_unit_sets_the_room_for_both():
    # Look20-Top and Look20-Skirt share a unit and an instant (one
    # broadcast) - when only Top's cue sweeps, the room after that send
    # must still account for the LONGER of the two (found in review: the
    # old code used whichever cue happened to sort last, not the max).
    top = cue("a", "Look20-Top", 53, "t1")
    top["sweep"] = {"sequence": "top_down", "span_s": 5.0, "source": "cue"}
    top["span"] = 5.0                              # Top's picture: 7 + 5 = 12 s
    skirt = cue("b", "Look20-Skirt", 53, "s1")      # Skirt: plain, no sweep
    next_top = cue("c", "Look20-Top", 53 + 13 - 1, "t1")   # 1 s short of 7+5+1
    found, _ = validate([top, skirt, next_top], ITEMS, 600, 7.0)
    assert found["c"] == [
        "only 12.0 s after the previous send on radxa-02; at least 13.0 s "
        "is needed (7.0 s panel repaint + 5.0 s sweep + 1.0 s gap)"]
    next_top["at"] = 53 + 13
    found, _ = validate([top, skirt, next_top], ITEMS, 600, 7.0)
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
                           "partial": 1}])
    assert [c["item"] for c in cues] == ["A", "B"]
    assert cues[0]["at"] == 0
    assert cues[0]["partial"] is True and cues[0]["id"]
    assert "align" not in cues[0]


# ---- sweeps ----

def test_a_sweep_lengthens_the_change_and_the_room_after_it():
    swept = cue("a", "Look22", 49, "g1.csv")
    swept["sweep"] = {"sequence": "top_down", "span_s": 4.0, "source": "cue"}
    swept["span"] = 4.0
    assert times(swept, 7.0) == (49.0, 60.0)            # 7 s refresh + 4 s sweep
    # The next refresh on the unit must wait for the sweep too.
    items = {"look22": {"item": "Look22", "unit": "radxa-01", "boards": 2,
                        "designs": {"g1.csv": {"full": True, "partial": True}}}}
    # Sent at 49; the previous cue's picture (7 + 4 s sweep) plus the 1 s
    # gap is 12 s - more than writing 2 boards would ever need on its own.
    later = cue("b", "Look22", 49 + 12 - 1, "g1.csv")     # 1 s short
    problems, _ = validate([swept, later], items, 600, 7.0)
    assert problems["b"] and "sweep" in problems["b"][0]
    later["at"] = 49 + 12 + 1
    problems, _ = validate([swept, later], items, 600, 7.0)
    assert problems["b"] == []


def test_a_sweep_without_its_map_is_reported_not_guessed():
    swept = cue("a", "Look22", 60, "g1.csv")
    swept["sweep"] = {"sequence": "center", "span_s": 0.1, "source": "cue"}
    # No span given: the map that would time it is missing.
    items = {"look22": {"item": "Look22", "unit": None, "boards": 2,
                        "designs": {"g1.csv": {"full": True, "partial": True}}}}
    problems, _ = validate([swept], items, 600, 7.0)
    assert any("map" in p for p in problems["a"])


def test_a_sweep_longer_than_thirty_seconds_is_a_problem_on_the_cue():
    swept = cue("a", "Look22", 60, "g1.csv")
    items = {"look22": {"item": "Look22", "unit": "radxa-01", "boards": 2,
                        "designs": {"g1.csv": {"full": True, "partial": True}}}}
    swept["sweep"] = {"sequence": "top_down", "span_s": 30.0, "source": "cue"}
    swept["span"] = 30.0
    assert validate([swept], items, 600, 7.0)[0]["a"] == []      # exactly 30 s: fine
    swept["sweep"]["span_s"] = 30.1
    swept["span"] = 30.1
    problems, _ = validate([swept], items, 600, 7.0)
    assert any("30 s" in p for p in problems["a"])


def test_sweeps_agrees_with_showfile_a_custom_cue_with_zero_span_does_not_sweep():
    from conductor.timeline import sweeps
    natural = cue("a", "Look22", 60, "g1.csv")
    natural["sweep"] = {"sequence": "natural", "span_s": 0.0, "source": "design"}
    assert sweeps(natural) is False
    zero_span = cue("b", "Look22", 60, "g1.csv")
    zero_span["sweep"] = {"sequence": "top_down", "span_s": 0.0, "source": "cue"}
    assert sweeps(zero_span) is False           # a sequence chosen, but no span
    real = cue("c", "Look22", 60, "g1.csv")
    real["sweep"] = {"sequence": "top_down", "span_s": 2.0, "source": "cue"}
    assert sweeps(real) is True
    assert sweeps({}) is False                  # no "sweep" key at all


def test_clean_takes_transition_sequence_and_span_and_drops_step_s():
    cues = clean([{"id": "a", "item": "L", "at": 5, "design": "d",
                   "transition": "custom", "sequence": "left_right",
                   "span_s": "3.456", "step_s": 9},
                  {"id": "b", "item": "L", "at": 6, "design": "d",
                   "sequence": "sideways", "span_s": -1},
                  {"id": "c", "item": "L", "at": 7, "design": "d"}])
    assert (cues[0]["transition"], cues[0]["sequence"], cues[0]["span_s"]) == \
        ("custom", "left_right", 3.46)
    assert "step_s" not in cues[0]
    assert (cues[1]["transition"], cues[1]["sequence"], cues[1]["span_s"]) == \
        ("design", "natural", 0.0)
    assert cues[2]["transition"] == "design"            # the default


def test_a_cue_inherits_its_designs_transition_and_may_override_it():
    transitions = {"g1.csv": {"sequence": "top_down", "span_s": 3.0}}
    inherited = cue("a", "Look22", 60, "g1.csv")
    assert resolve(inherited, transitions) == \
        {"sequence": "top_down", "span_s": 3.0, "source": "design"}
    custom = cue("b", "Look22", 60, "g1.csv")
    custom.update(transition="custom", sequence="center", span_s=1.5)
    assert resolve(custom, transitions) == \
        {"sequence": "center", "span_s": 1.5, "source": "cue"}
    # No entry for the design: natural, whatever the design's own default.
    plain = cue("c", "Look22", 60, "other.csv")
    assert resolve(plain, transitions)["sequence"] == "natural"
    # A malformed transitions entry (hostile import) must not crash this -
    # it is treated as if nothing had been set.
    assert resolve(inherited, {"g1.csv": "oops"})["sequence"] == "natural"
    assert resolve(inherited, {"g1.csv": 5})["sequence"] == "natural"
    cues = [inherited, custom]
    apply_transitions(cues, transitions)
    assert cues[0]["sweep"] == {"sequence": "top_down", "span_s": 3.0,
                               "source": "design"}
    assert cues[1]["sweep"] == {"sequence": "center", "span_s": 1.5,
                               "source": "cue"}


def test_what_a_unit_is_told_keeps_its_guard_after_the_real_end():
    """panel_refresh() is the `refresh_s` conductor/showfile.py puts in a
    unit's show file, and the unit places its guard STOP (the broadcast
    0x17 that stops a finished slot running into the factory autoplay) at
    `refresh_s + span + margin` after the fire - ui/runner.py's
    _guard_for(), margin = guard_delay - GUARD_REFRESH_S. The arithmetic
    is repeated here so the two sides cannot drift apart silently.
    """
    import inspect

    from ui.runner import GUARD_MAX_S, GUARD_REFRESH_S, DemoRunner

    guard_delay = inspect.signature(DemoRunner.__init__) \
        .parameters["guard_delay"].default
    assert (guard_delay, GUARD_REFRESH_S) == (12.0, 7.0)
    margin = max(0.0, guard_delay - GUARD_REFRESH_S)        # 5.0 s

    def guard(refresh_s, span):                             # _guard_for()
        return max(guard_delay, min(GUARD_MAX_S, refresh_s + span + margin))

    natural = cue("a", "Look22", 60, "p1")
    swept = cue("b", "Look22", 60, "g1.csv")
    swept["sweep"] = {"sequence": "top_down", "span_s": 1.0, "source": "cue"}
    swept["span"] = 1.0

    # What the unit is told, with the show at the 8 s default: the cue's
    # own refresh, floored at one physical repaint.
    assert panel_refresh(natural) == panel_refresh(swept) == 8.0
    # A legacy show (7.0 s) is exactly at the floor; a bench show below it
    # is lifted to it, because a board really does take PANEL_REPAINT_S.
    assert panel_refresh(natural, refresh=7.0) == 7.0
    assert panel_refresh(natural, refresh=1.0) == PANEL_REPAINT_S
    # ...and a board on older firmware keeps its own, longer value.
    assert panel_refresh(cue("c", "Look22", 60, "p1", refresh_s=16.0)) == 16.0

    # The guard must land AFTER the picture is really finished. A swept
    # production cue is done at PANEL_REPAINT_S + 1 = 8 s; the guard is
    # 14 s with what we send (8.0), 13 s if we sent the bare repaint, so
    # neither can fall inside the sweep.
    real_end = PANEL_REPAINT_S + 1.0
    assert real_end == 8.0 == complete_s(swept)
    assert guard(panel_refresh(swept), 1.0) == 14.0 > real_end
    assert guard(PANEL_REPAINT_S, 1.0) == 13.0 > real_end
    assert guard(panel_refresh(natural), 0.0) == 13.0 > PANEL_REPAINT_S

    # It does NOT land before the next send at the new 9 s minimum - and
    # cannot be made to: the flat guard_delay floor alone is 12 s, which
    # already exceeded the old 8 s minimum too. Every fire RESETS the
    # deadline (ui/runner.py: `guard_due = time.monotonic() +
    # self._guard_for(session)` on each fire), so between two cues the
    # STOP is simply never sent - the next cue's own trigger is what keeps
    # the board off the factory autoplay, and the STOP goes out after the
    # last cue of the run.
    assert min_interval(16) == 9.0 < guard_delay
    assert guard(PANEL_REPAINT_S, 1.0) > min_interval(16)
