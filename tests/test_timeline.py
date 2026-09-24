"""conductor/timeline.py: what a cue's time means, and what a unit can do.

Pure rules, no files: the items are handed in as the facts the rules
need (unit, board count, which designs pass as full / partial cues).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conductor.timeline import (DEFAULT_LEAD_S, REFRESH_S, UNIT_PREP_MARGIN_S,
                                UNIT_SAVE_S_PER_BOARD, UNIT_SETUP_S,
                                UNIT_SETUP_S_PER_BOARD, apply_transitions,
                                clean, effective_refresh, ends, format_clock,
                                min_interval, parse_clock, resolve, times,
                                validate)

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
    assert REFRESH_S == 7.0            # latest firmware, reported 2026-09-21
    with pytest.raises(ValueError):
        parse_clock("soon")
    # Hostile JSON (None, a dict) is the same "not a time", never a
    # TypeError the caller has to expect.
    with pytest.raises(ValueError):
        parse_clock(None)
    with pytest.raises(ValueError):
        parse_clock({})


def test_the_unit_constants_are_mirrored_from_their_real_sources():
    # conductor/ and ui/ do not import each other (they run on different
    # machines), so these are copies, not the same object - this test is
    # what keeps them from drifting apart silently.
    from ui import showplay
    assert UNIT_SAVE_S_PER_BOARD == showplay.SAVE_S_PER_BOARD
    assert UNIT_PREP_MARGIN_S == showplay.PREP_MARGIN_S
    assert UNIT_SETUP_S == showplay.SETUP_S
    assert UNIT_SETUP_S_PER_BOARD == showplay.SETUP_S_PER_BOARD
    from conductor.fleet import DEFAULT_LEAD_S as FLEET_DEFAULT_LEAD_S
    assert DEFAULT_LEAD_S == FLEET_DEFAULT_LEAD_S


def test_start_is_the_send_instant_and_complete_adds_refresh_and_sweep():
    assert times(cue("a", "Look22", "1:00", "p1")) == (60, 67)
    # 0:00 is the preset: sent one refresh before the show even begins,
    # so it is already complete when it starts.
    assert times(cue("a", "Look22", 0, "p1")) == (-7, 0)
    # A unit on older firmware: the show carries its own refresh time.
    assert times(cue("a", "Look22", "1:00", "p1"), refresh=16) == (60, 76)
    swept = cue("a", "Look22", 60, "g1.csv")
    swept["sweep"] = {"sequence": "top_down", "span_s": 4.0, "source": "cue"}
    swept["span"] = 4.0
    assert times(swept, 7.0) == (60.0, 71.0)            # 7 s refresh + 4 s sweep


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


def test_min_interval_grows_with_the_boards_to_write():
    # Refresh-bound while the unit's own write time (its constants, not
    # this module's) stays under refresh + the 1 s gap (8 s).
    assert min_interval(16) == pytest.approx(8.0)          # 16x0.25+2 = 6.0 < 8
    assert min_interval(24) == pytest.approx(8.0)          # 24x0.25+2 = 8.0 exactly
    # Write-bound once the unit's own write time passes that.
    assert min_interval(27) == pytest.approx(8.75)         # 27x0.25+2
    assert min_interval(32) == pytest.approx(10.0)         # 32x0.25+2
    assert min_interval(36) == pytest.approx(11.0)         # 36x0.25+2
    # A sweeping cue doubles the write term: its delay tables are written too.
    assert min_interval(16, sweep=True) == pytest.approx(10.0)   # 16x0.25x2+2
    assert min_interval(16, refresh=16) == pytest.approx(17.0)   # 16 + 1 s gap


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


def test_the_interval_is_never_shorter_than_the_units_prepare_lead():
    # The review's point: the conductor must never bless a spacing tighter
    # than the UNIT's own prepare lead, even where refresh + gap alone
    # would say it is fine.
    items32 = {"look22": {"item": "Look22", "unit": "radxa-05", "boards": 32,
                          "designs": {"p1": OK, "p2": OK}}}
    a = cue("a", "Look22", 60, "p1")
    b = cue("b", "Look22", 68.0, "p2")             # 8.0 s: refresh + gap only
    found, _ = validate([a, b], items32, 600, 7.0)
    assert found["b"] == [
        "only 8.0 s after the previous send on radxa-05; writing its 32 "
        "boards needs 10.0 s (32 × 0.25 s + 2.0 s)"]
    # 24 boards write in 8.0 s - exactly what 8.0 s apart provides.
    items24 = {"look22": {"item": "Look22", "unit": "radxa-05", "boards": 24,
                          "designs": {"p1": OK, "p2": OK}}}
    assert validate([a, b], items24, 600, 7.0)[0]["b"] == []


def test_a_sweeping_cue_doubles_the_write_term():
    items = {"look22": {"item": "Look22", "unit": "radxa-06", "boards": 16,
                        "designs": {"p1": OK,
                                    "g1.csv": {"full": True, "partial": True}}}}
    a = cue("a", "Look22", 60, "p1")
    b = cue("b", "Look22", 68.0, "g1.csv")         # 8.0 s: fine without a sweep
    assert validate([a, b], items, 600, 7.0)[0]["b"] == []
    b["sweep"] = {"sequence": "top_down", "span_s": 2.0, "source": "cue"}
    b["span"] = 2.0                        # now sweeps: its delay tables are
    found, _ = validate([a, b], items, 600, 7.0)   # written too - double the write
    assert found["b"] == [
        "only 8.0 s after the previous send on radxa-06; writing its 16 "
        "boards needs 10.0 s (16 × 0.25 s × 2 (its delay tables) "
        "+ 2.0 s)"]


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
        "is needed (7.0 s refresh + 4.0 s sweep + 1.0 s gap)"]


def test_a_plain_show_has_no_problems():
    cues = [cue("a", "Look22", 0, "p1"), cue("b", "Look22", "2:00", "p2"),
            cue("c", "Look22", "5:30", "p1")]
    found, warnings = problems(cues)
    assert all(not v for v in found.values()) and warnings == []


def test_refreshes_on_one_unit_need_room():
    # b's picture completes at 107 (7 s refresh); a send from 107 up to
    # (but not past) 115 is "before the picture is complete", a separate
    # rule (tested above) - this is about the room a bus needs AFTER that.
    b = cue("b", "Look22", 100, "p2")
    tight = cue("c", "Look22", 107.3, "p1")       # 7.3 s after send: too tight
    found, _ = problems([b, tight])
    assert found["c"] == [
        "only 7.3 s after the previous send on radxa-03; at least 8.0 s "
        "is needed (7.0 s refresh + 1.0 s gap)"]
    # 8 s apart (refresh + the 1 s gap) is enough room at a 7 s refresh -
    # not at a 16 s one, where that same gap needs 17 s.
    fine = cue("c", "Look22", 108, "p1")
    assert problems([b, fine])[0]["c"] == []
    tight16 = cue("c", "Look22", 116.3, "p1")     # 16.3 s after send
    found, _ = validate([b, tight16], ITEMS, 600, refresh=16)
    assert found["c"] == [
        "only 16.3 s after the previous send on radxa-03; at least 17.0 s "
        "is needed (16.0 s refresh + 1.0 s gap)"]
    fine16 = cue("c", "Look22", 117, "p1")
    assert validate([b, fine16], ITEMS, 600, refresh=16)[0]["c"] == []


def test_the_first_cue_must_leave_time_to_write_the_boards_after_start():
    # The very first pairing on a unit's bus (preset -> first real cue) is
    # floored at the unit's full rejoin lead, not just refresh + gap: the
    # unit only starts writing once /show/run actually lands
    # (conductor/fleet.py's DEFAULT_LEAD_S before T0), so a unit that is
    # only just rejoining right there pays full setup, not just a write.
    found, _ = problems([cue("a", "Look22", 0, "p1"),
                         cue("b", "Look22", 1, "p2")])       # 0:01: too tight
    assert found["b"] == [
        "only 8.0 s after the previous send on radxa-03; the unit may "
        "still be rejoining and needs at least 10.4 s (16 × 0.25 s + "
        "2.0 s prep + 1.0 s setup + 16 × 0.15 s probe + 1.0 s gap)"]
    found, _ = problems([cue("a", "Look22", 0, "p1"),
                         cue("b", "Look22", 3.4, "p2")])     # 10.4 s: fine
    assert found["b"] == []


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
                 cue("b", "Look20-Skirt", 59, "s1")]     # 6 s: writing 32
    found, _ = problems(staggered)                       # boards needs 10 s
    assert found["b"] == [
        "only 6.0 s after the previous send on radxa-02; writing its 32 "
        "boards needs 10.0 s (32 × 0.25 s + 2.0 s)"]
    # Another unit is another bus: no conflict with Look22 ten seconds on.
    found, _ = problems(same_moment + [cue("c", "Look22", 63, "p1")])
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
