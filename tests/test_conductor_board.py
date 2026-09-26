"""THE SHOW's NOW -> NEXT board: what fires next, and what every garment
is wearing while it waits.

Three halves, in the shape tests/test_conductor_music.py established:

* Plain text tests over conductor/web/index.html - the board is drawn in
  THE SHOW card, it shares the fleet's one poll rather than adding a second
  one, the tile and the row use the same words for a late cue, and no
  library was added to do any of it.
* One headless-browser run of the page's own decision layer. index.html
  marks it off between `<<< SHOWBOARD ... >>>` and `<<< /SHOWBOARD >>>`;
  this file lifts exactly that text out, drops it into a page of its own
  and asks it every question the show asks it on the night (which cue is
  next, what a garment wears while one is in flight, the countdown's two
  colours, HOLD, a seek, LOADED, ENDED, which items are bags, and how a
  Radxa's vitals read).
* One headless-browser run of the whole page against a stand-in server with
  four garments, two of them bags, and a fleet the probe steers.

The last two are behind CONDUCTOR_BROWSER_TESTS=1, like the simulator's own
browser tests - and skipped, not failed, where no browser is installed.
"""
from __future__ import annotations

import http.server
import json
import re
import socket
import subprocess
import sys
import threading
from html import unescape
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
INDEX_HTML = REPO / "conductor" / "web" / "index.html"

sys.path.insert(0, str(REPO))

from conductor import timeline as tl  # noqa: E402
from conductor.server import Workspace  # noqa: E402
from tests.test_designer_build import _dump_dom, _find_browser, _require_browser  # noqa: E402
from tests.test_look import GRID, MAP  # noqa: E402

PAGE = INDEX_HTML.read_text(encoding="utf-8")

# The block index.html marks as the pure decision layer. Kept as a regex over
# the real file (never a copy of the source in here) so the tests below can
# only ever describe the page that ships.
_MODULE = re.search(r"<<< SHOWBOARD:.*?>>>\n(.*?)\n// <<< /SHOWBOARD >>>", PAGE, re.S)


def _strip_comments(source):
    """Line comments out. Every check below is about what the code does, and
    these comments all quote the very things being looked for."""
    return "\n".join(re.sub(r"//.*", "", line) for line in source.splitlines())


def _function_body(name):
    """The code of one top-level `function name(...) {...}`, comments
    removed, found by counting braces."""
    start = PAGE.index(f"function {name}(")
    open_at = PAGE.index("{", start)
    depth = 0
    for i in range(open_at, len(PAGE)):
        if PAGE[i] == "{":
            depth += 1
        elif PAGE[i] == "}":
            depth -= 1
            if depth == 0:
                return _strip_comments(PAGE[open_at:i + 1])
    raise AssertionError(f"{name} is never closed")


# --------------------------------------------------------------- the page

def test_the_decision_layer_is_marked_off_for_the_tests():
    assert _MODULE, "the SHOWBOARD markers are gone from conductor/web/index.html"
    source = _MODULE.group(1)
    assert "SHOWBOARD" in source and "function header(" in source
    # Pure means pure: the block must not reach for the page, the fleet, the
    # workspace or a clock of its own - the caller hands all four in.
    code = _strip_comments(source)
    for forbidden in ("document.", "$(", "fleet", "state.", "performance.now(",
                      "setTimeout", "setInterval"):
        assert forbidden not in code, f"the pure layer reaches for {forbidden!r}"


def test_the_board_is_drawn_in_the_show_card_under_the_big_clock():
    # The slot the board is moved into sits between the big clock and the
    # live LOOK row, which is where the operator was promised it.
    card = PAGE[PAGE.index('id="show-clock"'):PAGE.index('id="show-thumbs"')]
    assert 'id="nn-slot"' in card, "the board's slot is not under the big clock"
    assert "placeBoard();" in PAGE and "paintBoard();" in PAGE


def test_the_board_is_one_node_that_survives_a_re_render():
    # renderFleet() rewrites the whole of #content, and the stage monitor has
    # to live through a render of another tab behind it - so the board is
    # MOVED into its slot, never rendered into it.
    body = _function_body("placeBoard")
    assert "appendChild(boardEl)" in body
    assert 'ui.stage ? $("#nn-stage") : $("#nn-slot")' in body
    assert PAGE.count('boardEl = document.createElement') == 1, \
        "the board is created in more than one place"
    assert PAGE.count('boardEl.id = "nownext"') == 1
    # ...and with nowhere to go it leaves the document, rather than be
    # repainted four times a second inside a closed overlay.
    assert "boardEl.remove()" in body


def test_the_board_shares_the_fleet_poll_instead_of_adding_one():
    # One poll, one clock. The board reads the same extrapolated position the
    # big clock and the seek bar do (fleetShowPos), and adds no fetch of its
    # own.
    body = _function_body("paintBoard")
    assert "fleetShowPos()" in body, "the board invents a position of its own"
    for forbidden in ("fetch(", "api(", "setInterval", "setTimeout"):
        assert forbidden not in body, f"paintBoard reaches for {forbidden!r}"
    # ...and the stage monitor keeps that poll alive over whichever tab it
    # was opened from, or its countdown would simply stop.
    poll = _function_body("pollFleet")
    assert "ui.stage" in poll, "the stage monitor does not keep the fleet poll going"


def test_a_late_cue_reads_the_same_on_the_tile_and_on_the_row():
    # The two used to format late_ms separately; one helper now, so a row and
    # the tile above it can never disagree about a unit.
    assert "function lateText(u)" in PAGE
    assert PAGE.count("lateText(u)") >= 3, "the tile or the row still formats late_ms itself"
    assert "verifyMark(u)" in _function_body("paintBoard"), \
        "the row does not carry the unit's own re-sent / not applied marks"


def test_the_thumbnails_are_not_redrawn_on_every_tick():
    body = _function_body("paintBoard")
    # A garment is only ever DRAWN through boardThumb(), and only behind a
    # key that says its design changed - ten garments re-rendered four times
    # a second is what this exists to avoid.
    assert "renderGarment" not in body, "paintBoard draws a garment on the tick"
    assert "renderGarment" in _function_body("boardThumb")
    assert "c.nowKey !== nowKey" in body and "c.nextKey !== nextKey" in body
    assert body.count("boardThumb(") == 2, \
        "a thumbnail is drawn somewhere other than behind its key"
    assert "circle.setAttribute(\"fill\", fill)" in body, \
        "the live thumbnail is not repainted by fill alone"


def test_the_board_asks_the_internet_for_nothing():
    # The show PC has no internet on the night (CONDUCTOR_START §3).
    board = PAGE[PAGE.index("<<< SHOWBOARD:"):PAGE.index("function renderFleet()")]
    for forbidden in ("http://", "https://", "cdn", "import "):
        assert forbidden not in board, f"the board pulls in {forbidden!r}"


def test_the_fleet_hands_over_a_real_lag_and_an_uptime():
    fleet_py = (REPO / "conductor" / "fleet.py").read_text(encoding="utf-8")
    assert "show_lag_ms=self._show_lag_ms(link, run)" in fleet_py
    assert '"uptime_s": status.get("uptime_s")' in fleet_py
    # ...and sync_ms is still what it always was, now said out loud: half a
    # round trip, an error BAR, never an error.
    assert "NOT a lag" in fleet_py


# ------------------------------------------------- the pure layer, running

def _cue(cid, item, at, design, label, seq="natural", span=0.0, refresh=8.0):
    """A cue exactly as conductor/server.py hands it to the page."""
    raw = {"at": at, "span_s": span}
    paint = tl.complete_s(raw, refresh)
    sent = round(at, 3) if at > 0 else round(-paint, 3)
    return {"id": cid, "item": item, "at": float(at), "sent": sent,
            "complete": round(sent + paint, 3), "design": design,
            "label": label, "sequence": seq, "span": span}


# Two garments and five cues: both change together at 0:30, and one of them
# again at 1:00. The show lasts two minutes.
L23 = [_cue("p23", "Look23", 0, "ivory.csv", "ivory"),
       _cue("c23", "Look23", 30, "scarlet.csv", "scarlet", "top_down", 1.0)]
L24 = [_cue("p24", "Look24", 0, "ivory.csv", "ivory"),
       _cue("c24", "Look24", 30, "gold.csv", "gold", "center", 1.0),
       _cue("d24", "Look24", 60, "indigo.csv", "indigo", "left_right", 2.0)]
ALL = L23 + L24
NAMES = {"p23": "LOOK 23 Tops", "c23": "LOOK 23 Tops",
         "p24": "LOOK 24 Skirt", "c24": "LOOK 24 Skirt", "d24": "LOOK 24 Skirt"}
D = 120.0


CALLS = {
    # ---- which cue is next, per garment
    "row_before_anything": ["rowAt", L23, 10.0],
    "row_mid_flight": ["rowAt", L23, 32.0],
    "row_after_its_last": ["rowAt", L23, 90.0],
    "row_at_the_very_instant": ["rowAt", L23, 30.0],
    "row_of_a_garment_with_two_changes": ["rowAt", L24, 40.0],
    # ---- grouping and the flash
    "ahead_at_zero": ["ahead", ALL, 0.0],
    "ahead_past_the_first": ["ahead", ALL, 40.0],
    "ahead_past_them_all": ["ahead", ALL, 90.0],
    "fired_just_now": ["firedAgo", ALL, 30.4],
    "fired_a_while_ago": ["firedAgo", ALL, 45.0],
    "fired_nothing_yet": ["firedAgo", [], 5.0],
    # ---- the countdown's two colours
    "tone_far": ["tone", 25.0],
    "tone_ten": ["tone", 10.0],
    "tone_just_over_ten": ["tone", 10.4],
    "tone_three": ["tone", 3.0],
    "tone_zero": ["tone", 0.0],
    # ---- whole seconds, never below zero
    "secs_whole": ["secs", 12.0],
    "secs_part": ["secs", 11.2],
    "secs_last": ["secs", 0.3],
    "secs_past": ["secs", -4.0],
    # ---- which items are bags
    "bag_by_item": ["isBag", {"item": "AZ271SG1301", "look": "", "model": "AZ271SG1301"}],
    "bag_by_model": ["isBag", {"item": "Look25", "look": "25", "model": "AZ271SG2301 Bag"}],
    "bag_by_no_look": ["isBag", {"item": "Spare01", "look": "", "model": "AZ271SX0001"}],
    "not_a_bag": ["isBag", {"item": "Look23", "look": "23", "model": "AZ271SB2303 (Tops)"}],
    "not_a_bag_zero_look": ["isBag", {"item": "Look00", "look": "0", "model": "AZ271SB0000"}],
    # ---- the show's own phase
    "phase_no_run": ["phase", None, 0.0, D],
    "phase_running": ["phase", {"state": "running"}, 10.0, D],
    "phase_holding": ["phase", {"state": "holding"}, 10.0, D],
    "phase_ended": ["phase", {"state": "running"}, 121.0, D],
    # ---- the sweep arrows
    "arrow_top_down": ["arrow", "top_down"],
    "arrow_center": ["arrow", "center"],
    "arrow_natural": ["arrow", "natural"],
    "arrow_unknown": ["arrow", "something_else"],
    # ---- a Radxa's vitals
    "vitals_good": ["vitals", {"name": "radxa-01", "online": True, "show_lag_ms": 4.0,
                               "rtt_ms": 6.0, "live": 12, "boards": 12, "late_ms": 8,
                               "uptime_s": 7200}, 12],
    "vitals_behind": ["vitals", {"name": "radxa-02", "online": True, "show_lag_ms": 480.0,
                                 "rtt_ms": 6.0, "live": 12, "boards": 12, "late_ms": None,
                                 "uptime_s": 7200}, 12],
    "vitals_slow_path": ["vitals", {"name": "radxa-03", "online": True, "show_lag_ms": -12.0,
                                    "rtt_ms": 240.0, "live": 12, "boards": 12,
                                    "late_ms": None, "uptime_s": 7200}, 12],
    "vitals_board_missing": ["vitals", {"name": "radxa-04", "online": True, "show_lag_ms": 2.0,
                                        "rtt_ms": 6.0, "live": 11, "boards": 12,
                                        "late_ms": None, "uptime_s": 7200}, 12],
    "vitals_restarted": ["vitals", {"name": "radxa-05", "online": True, "show_lag_ms": 2.0,
                                    "rtt_ms": 6.0, "live": 12, "boards": 12,
                                    "late_ms": None, "uptime_s": 190}, 12],
    "vitals_no_run_yet": ["vitals", {"name": "radxa-06", "online": True, "show_lag_ms": None,
                                     "rtt_ms": 6.0, "live": 12, "boards": 12,
                                     "late_ms": None, "uptime_s": 7200}, 12],
    "vitals_offline": ["vitals", {"name": "radxa-07", "online": False}, 12],
    "vitals_no_unit": ["vitals", None, 12],
}

# The row texts, one per state the board has to be right in.
_ROW_TEXTS = {
    "text_running_before": (L23, 10.0, "running"),
    "text_running_soon": (L23, 22.0, "running"),
    "text_running_imminent": (L23, 28.0, "running"),
    "text_in_flight": (L23, 33.0, "running"),
    "text_after_its_last": (L23, 90.0, "running"),
    "text_loaded": (L23, 0.0, "loaded"),
    "text_holding": (L23, 20.0, "holding"),
    "text_ended": (L23, 121.0, "ended"),
    "text_seeked_back": (L24, 45.0, "running"),
}


_PROBE = """<!doctype html><meta charset="utf-8"><title>showboard</title><body>
<script>
"use strict";
%(module)s
var CALLS = %(calls)s;
var ROWTEXTS = %(rowtexts)s;
var out = { error: null, constants: null, results: {} };
try {
  out.constants = { AMBER_S: SHOWBOARD.AMBER_S, RED_S: SHOWBOARD.RED_S,
                    FIRED_S: SHOWBOARD.FIRED_S, LAG_BAD_MS: SHOWBOARD.LAG_BAD_MS,
                    RTT_SLOW_MS: SHOWBOARD.RTT_SLOW_MS, RESTART_S: SHOWBOARD.RESTART_S,
                    ARROWS: SHOWBOARD.ARROWS };
  for (var name in CALLS)
    out.results[name] = SHOWBOARD[CALLS[name][0]].apply(null, CALLS[name].slice(1));
  for (var key in ROWTEXTS) {
    var spec = ROWTEXTS[key];
    out.results[key] = SHOWBOARD.rowText(SHOWBOARD.rowAt(spec.cues, spec.t), spec.how, spec.duration);
  }
  var HEADS = %(heads)s;
  for (var h in HEADS) {
    var now = HEADS[h];
    now.groups = SHOWBOARD.ahead(now.cues, now.t);
    now.agoS = now.ago ? SHOWBOARD.firedAgo(now.cues, now.t) : null;
    out.results[h] = SHOWBOARD.header(now);
  }
} catch (e) { out.error = String((e && e.stack) || e); }
var pre = document.createElement("pre");
pre.id = "board-out";
pre.textContent = JSON.stringify(out);
document.body.appendChild(pre);
</script></body>
"""


def _heads():
    def one(phase, t, **kw):
        now = {"phase": phase, "t": t, "duration": D, "startAt": 0.0,
               "names": NAMES, "cues": ALL, "ago": True, "leadLeft": None}
        now.update(kw)
        return now
    return {
        "head_far": one("running", 10.0),
        "head_amber": one("running", 21.0),
        "head_red": one("running", 27.5),
        "head_last_second": one("running", 29.6),
        "head_fired": one("running", 30.4),
        "head_after_the_flash": one("running", 31.6),
        "head_second_change": one("running", 40.0),
        "head_nothing_ahead": one("running", 90.0),
        "head_loaded": one("loaded", 0.0),
        "head_loaded_from_a_mark": one("loaded", 20.0, startAt=20.0),
        "head_holding": one("holding", 20.0),
        "head_holding_never_flashes": one("holding", 30.4),
        "head_ended": one("ended", 121.0),
        "head_during_a_next_lead": one("running", 10.0, leadLeft=2.4),
        "head_no_cues_at_all": one("loaded", 0.0, cues=[]),
    }


@pytest.fixture(scope="module")
def board(tmp_path_factory):
    """One headless run of index.html's own decision layer, on its own."""
    assert _MODULE, "the SHOWBOARD markers are gone from conductor/web/index.html"
    tmp = tmp_path_factory.mktemp("showboard")
    _require_browser(tmp)
    rowtexts = {name: {"cues": cues, "t": t, "how": how, "duration": D}
                for name, (cues, t, how) in _ROW_TEXTS.items()}
    page = tmp / "showboard.html"
    page.write_text(_PROBE % {"module": _MODULE.group(1),
                              "calls": json.dumps(CALLS),
                              "rowtexts": json.dumps(rowtexts),
                              "heads": json.dumps(_heads())}, encoding="utf-8")
    url = "file:///" + str(page.resolve()).replace("\\", "/")
    dom = _dump_dom(url, tmp)
    match = re.search(r'<pre id="board-out">(.*?)</pre>', dom or "", re.S)
    assert match, f"no #board-out in the dumped DOM:\n{(dom or '')[:3000]}"
    data = json.loads(unescape(match.group(1)))
    assert data["error"] is None, data["error"]
    return data


def test_the_countdown_thresholds_are_the_ones_the_operator_confirmed(board):
    c = board["constants"]
    assert c["AMBER_S"] == 10 and c["RED_S"] == 3, "the countdown's colours moved"
    assert c["FIRED_S"] == 1, "the FIRED flash is no longer one second"
    r = board["results"]
    assert r["tone_far"] == "" and r["tone_ten"] == "amber"
    assert r["tone_just_over_ten"] == "", "amber starts at ten seconds, not before"
    assert r["tone_three"] == "red" and r["tone_zero"] == "red"


def test_a_countdown_counts_whole_seconds_and_stops_at_zero(board):
    r = board["results"]
    assert r["secs_whole"] == 12, "a whole second was rounded up to the next one"
    assert r["secs_part"] == 12 and r["secs_last"] == 1, \
        "the last part-second must still read as a second to come"
    assert r["secs_past"] == 0, "a cue already gone counted backwards"


def test_a_garment_knows_what_it_wears_and_what_is_coming(board):
    r = board["results"]
    before = r["row_before_anything"]
    assert before["now"]["id"] == "p23", "the 0:00 preset is not what it is wearing"
    assert before["next"]["id"] == "c23" and before["in"] == pytest.approx(20.0)
    assert before["changing"] is None and before["last"] is False
    # Past its last cue there is nothing ahead at all.
    last = r["row_after_its_last"]
    assert last["now"]["id"] == "c23" and last["next"] is None and last["last"] is True


def test_a_cue_in_flight_is_not_counted_down_a_second_time(board):
    # It has already gone out: NOW shows it arriving, NEXT shows what comes
    # after it. Counting it again made a garment look like it changed twice.
    r = board["results"]["row_mid_flight"]
    assert r["changing"]["id"] == "c23", r
    assert r["next"] is None, "the cue already sent is still listed as the next one"
    assert r["left"] == pytest.approx(6.0), "8 s from 0:30 leaves 6 s at 0:32"
    assert r["progress"] == pytest.approx(0.25)
    # ...and at the very instant it goes out it is in flight, not still ahead.
    instant = board["results"]["row_at_the_very_instant"]
    assert instant["changing"]["id"] == "c23" and instant["next"] is None


def test_a_row_says_the_same_things_the_operator_was_promised(board):
    r = board["results"]
    assert r["text_running_before"]["now"] == "ivory"
    assert r["text_running_before"]["next"] == "scarlet"
    assert r["text_running_before"]["when"] == "0:30"
    assert r["text_running_before"]["count"] == 20
    assert r["text_running_before"]["tone"] == ""
    assert r["text_running_soon"]["tone"] == "amber", r["text_running_soon"]
    assert r["text_running_imminent"]["tone"] == "red", r["text_running_imminent"]
    # The transition, as one character and its span.
    assert r["text_running_before"]["arrow"] == "↓", "top_down is not an arrow down"
    assert r["text_running_before"]["span"] == "1.0 s"
    # In flight: a progress line, and NOW already naming the design arriving.
    flight = r["text_in_flight"]
    assert flight["changing"] == "changing… 5 s left", flight
    assert flight["now"] == "scarlet"
    # After the last cue.
    assert r["text_after_its_last"]["next"] == "last design — until the show ends 2:00"


def test_a_row_reads_right_in_every_state_of_the_show(board):
    r = board["results"]
    # LOADED: the preset is worn, the first cue and its time are named, and
    # nothing is counting down yet.
    loaded = r["text_loaded"]
    assert loaded["now"] == "ivory" and loaded["next"] == "scarlet"
    assert loaded["when"] == "0:30" and loaded["count"] is None, loaded
    # HOLD: the countdown is frozen - a number, but no colour and no pulse.
    held = r["text_holding"]
    assert held["count"] == 10 and held["tone"] == "", held
    # ENDED.
    assert r["text_ended"]["next"] == "show ended"
    # A seek: nothing remembered, everything recomputed from the position.
    seeked = r["text_seeked_back"]
    assert seeked["now"] == "gold" and seeked["next"] == "indigo"
    assert seeked["when"] == "1:00" and seeked["count"] == 15


def test_cues_that_fire_together_are_one_entry_in_the_header(board):
    r = board["results"]
    groups = r["ahead_at_zero"]
    assert [g["at"] for g in groups] == [30.0, 60.0], groups
    assert len(groups[0]["cues"]) == 2, "two garments changing together are two entries"
    assert r["ahead_past_the_first"][0]["at"] == 60.0
    assert r["ahead_past_them_all"] == []


def test_the_header_names_the_next_cue_its_time_and_its_garments(board):
    h = board["results"]["head_far"]
    assert h["cap"] == "NEXT" and h["time"] == "0:30"
    assert h["count"] == 20 and h["tone"] == ""
    assert h["garments"] == ["LOOK 23 Tops", "LOOK 24 Skirt"], h
    # ...and the three after it, as time / LOOK / design.
    assert h["following"] == [{"at": "1:00", "what": "LOOK 24 Skirt", "design": "indigo"}], h


def test_the_header_turns_amber_then_red(board):
    r = board["results"]
    assert r["head_amber"]["count"] == 9 and r["head_amber"]["tone"] == "amber"
    assert r["head_red"]["count"] == 3 and r["head_red"]["tone"] == "red"
    # The whole of the last second still reads as a second to come.
    assert r["head_last_second"]["count"] == 1 and r["head_last_second"]["tone"] == "red"


def test_fired_flashes_for_one_second_and_only_while_the_show_runs(board):
    r = board["results"]
    assert r["fired_just_now"] == pytest.approx(0.4)
    assert r["fired_a_while_ago"] == pytest.approx(15.0)
    assert r["fired_nothing_yet"] is None
    assert r["head_fired"]["fired"] is True
    assert r["head_after_the_flash"]["fired"] is False
    # A HOLD freezes the position, so without this guard the flash would
    # simply stay on for as long as the operator held the show.
    assert r["head_holding_never_flashes"]["fired"] is False
    # ...and "loaded" reads the preset (sent before 0:00) as a cue that just
    # went out, which it is not.
    assert r["head_loaded"]["fired"] is False


def test_the_header_says_what_each_state_of_the_show_means(board):
    r = board["results"]
    assert r["head_loaded"]["note"] == "START runs from 0:00"
    assert r["head_loaded_from_a_mark"]["note"] == "START runs from 0:20"
    assert r["head_loaded"]["count"] is None, "nothing counts down before START"
    held = r["head_holding"]
    assert held["cap"] == "NEXT (HELD)"
    assert held["note"] == "HELD at 0:20 — 10 s to the next cue after RESUME", held
    assert held["tone"] == "", "a frozen countdown must not pulse"
    assert r["head_ended"]["cap"] == "SHOW ENDED" and r["head_ended"]["note"] == "show ended"
    assert r["head_nothing_ahead"]["cap"] == "NO CUE AHEAD"
    assert "the show ends 2:00" in r["head_nothing_ahead"]["note"]
    assert r["head_no_cues_at_all"]["note"] == "Nothing on the timeline yet."


def test_a_next_press_is_visible_in_the_header_during_its_lead(board):
    assert board["results"]["head_during_a_next_lead"]["note"] == "NEXT in 3 s"


def test_the_phase_comes_off_the_run_and_the_position(board):
    r = board["results"]
    assert r["phase_no_run"] == "loaded"
    assert r["phase_running"] == "running"
    assert r["phase_holding"] == "holding"
    assert r["phase_ended"] == "ended"


def test_the_three_bags_are_recognised_three_ways(board):
    r = board["results"]
    assert r["bag_by_item"] is True, "an AZ271SG* item is a bag"
    assert r["bag_by_model"] is True, "a model that says Bag is a bag"
    assert r["bag_by_no_look"] is True, "a garment with no LOOK number is a bag"
    assert r["not_a_bag"] is False
    assert r["not_a_bag_zero_look"] is False, "LOOK 0 is a LOOK, not a missing one"


def test_every_sweep_has_its_own_arrow(board):
    a = board["constants"]["ARROWS"]
    assert sorted(a) == ["bottom_up", "center", "left_right", "natural",
                         "right_left", "top_down"], \
        "the arrows and conductor/sequence.py's sequences have drifted apart"
    assert len(set(a.values())) == 6, "two sweeps share an arrow"
    r = board["results"]
    assert r["arrow_top_down"] == "↓" and r["arrow_center"] == "◎"
    assert r["arrow_natural"] == "■"
    assert r["arrow_unknown"] == r["arrow_natural"], \
        "an unknown sequence must fall back, not draw nothing"


def test_a_radxas_vitals_read_in_one_line_and_colour_themselves(board):
    r = board["results"]
    good = r["vitals_good"]
    assert good["text"] == "radxa-01 · lag +4 ms · rtt 6 ms · boards 12/12 · fired +8 ms", good
    assert good["tone"] == "" and good["offline"] is False
    # Red: too far from this PC's clock, or a board that is not answering.
    assert r["vitals_behind"]["tone"] == "red", r["vitals_behind"]
    assert "lag +480 ms" in r["vitals_behind"]["text"]
    assert r["vitals_board_missing"]["tone"] == "red"
    assert r["vitals_board_missing"]["missing"] is True
    assert "boards 11/12" in r["vitals_board_missing"]["text"]
    # Amber: a slow path, but nothing actually wrong yet.
    assert r["vitals_slow_path"]["tone"] == "amber", r["vitals_slow_path"]
    # A restart is worth saying out loud - it is what explains a unit that
    # lost its pictures.
    assert "restarted 3 min ago" in r["vitals_restarted"]["text"]
    assert "restarted" not in r["vitals_good"]["text"]
    # Before a run there is no lag to report, and that is not a fault.
    assert "lag —" in r["vitals_no_run_yet"]["text"]
    assert r["vitals_no_run_yet"]["tone"] == ""
    # Offline, and not assigned at all.
    assert r["vitals_offline"] == {"text": "radxa-07 · unit offline", "tone": "red",
                                   "offline": True, "missing": False}
    assert r["vitals_no_unit"]["text"] == "no unit assigned"
    assert r["vitals_no_unit"]["offline"] is True


# ------------------------------------------------- the whole page, running
#
# The half above asks the pure layer questions. This one boots the real
# conductor/web/index.html in a headless browser against a small stand-in
# server - four garments, two of them bags, three units - and watches what
# the page draws.

def _free_port():
    """A free port at or above 8800 - never 8765, which is the show PC's own
    Conductor and may well be running while these tests are."""
    for port in range(8800, 8900):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise AssertionError("no free port in 8800-8899")


def _unit(name, **kw):
    u = {"name": name, "address": f"192.168.51.10{name[-1]}:8787",
         "online": True, "error": None, "rtt_ms": 6.0, "sync_ms": 3.0,
         "samples": 8, "uptime_s": 7200, "host": name, "commit": "abc1234",
         "phase": "ready", "cue": None, "label": None, "boards": 4, "live": 4,
         "saved": 4, "failed": [], "prepare_s": None, "late_ms": 6,
         "verify": None, "demo_count": 0, "unit_error": None, "log": ["ok"],
         "show": None, "refused": None, "demos": [], "show_lag_ms": 4.0}
    u.update(kw)
    return u


_RUNS = {
    "none": None,
    "running": {"t0": 0.0, "state": "running", "held_at": None, "force": False, "now": 20.0},
    "holding": {"t0": 0.0, "state": "holding", "held_at": 20.0, "force": False, "now": 20.0},
    "ended": {"t0": 0.0, "state": "running", "held_at": None, "force": False, "now": 130.0},
}


class _Stand:
    """index.html, a real /api/state with four garments, and an /api/fleet
    the probe steers."""

    def __init__(self, tmp_path, probe):
        ws = Workspace(tmp_path / "ws")
        for item in ("Look23", "Look24", "Look26", "Look25", "AZ271SG1301"):
            ws.save(f"{item}_map.csv", MAP)
            ws.save(f"{item}_color_ivory_grid.csv", GRID)
            ws.save(f"{item}_color_scarlet_grid.csv", GRID)
        ws.set_label("Look23", "23", "AZ271SB2303 (Tops)")
        ws.set_label("Look24", "24", "AZ271SC6302 (Skirt)")
        ws.set_label("Look26", "26", "AZ271SD1301 (Tops)")   # its unit is offline
        ws.set_label("Look25", "25", "AZ271SG2301 Bag")   # a bag by its model
        ws.set_label("AZ271SG1301", "", "AZ271SG1301")    # ...and one by its item
        for item, unit in (("Look23", "radxa-01"), ("Look24", "radxa-02"),
                           ("Look26", "radxa-03"), ("Look25", "radxa-04"),
                           ("AZ271SG1301", "radxa-05")):
            ws.assign(item, unit)
        ws.set_timeline(120, [
            {"id": "p23", "item": "Look23", "at": 0, "design": "Look23_color_ivory_grid.csv"},
            {"id": "p24", "item": "Look24", "at": 0, "design": "Look24_color_ivory_grid.csv"},
            {"id": "c23", "item": "Look23", "at": 30, "design": "Look23_color_scarlet_grid.csv",
             "transition": "custom", "sequence": "top_down", "span_s": 1.0},
            {"id": "c24", "item": "Look24", "at": 30, "design": "Look24_color_scarlet_grid.csv",
             "transition": "custom", "sequence": "center", "span_s": 1.0},
        ])
        state = ws.state()
        written = ws.written_state()
        page = INDEX_HTML.read_text(encoding="utf-8").replace("</body>", probe + "</body>", 1)
        self.run = "none"
        stand = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _send(self, body, kind):
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, obj):
                self._send(json.dumps(obj).encode("utf-8"), "application/json")

            def do_POST(self):
                self.do_GET()

            def do_GET(self):
                path = self.path.split("?")[0]
                if path in ("/", "/index.html"):
                    return self._send(page.encode("utf-8"), "text/html; charset=utf-8")
                if path == "/api/state":
                    return self._json(state)
                if path == "/api/fleet":
                    return self._json({
                        # radxa-02 is a third of a second behind this PC AND
                        # one board short - either on its own is red.
                        "units": [_unit("radxa-01"),
                                  _unit("radxa-02", show_lag_ms=330.0, live=2),
                                  _unit("radxa-03", online=False, error="no answer"),
                                  _unit("radxa-04"), _unit("radxa-05")],
                        "last_fire": None, "run": _RUNS[stand.run],
                        "shows": {}, "corrections": [], "prepared": {},
                        "start_at": 0.0, "show_duration": 120.0,
                        "burn": {"burned": 0, "total": 0}, "timeline": written})
                if path == "/api/fleet/demos":
                    return self._json({"units": {}, "offline": [], "failed": {}})
                if path == "/test/fleet":
                    stand.run = self.path.split("=")[-1]
                    return self._json({"run": stand.run})
                return self._json({})

        self.port = _free_port()
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


_PAGE_PROBE = """
<script>
(function () {
  var out = { error: null };
  function publish() {
    var pre = document.createElement("pre");
    pre.id = "page-out";
    pre.textContent = JSON.stringify(out);
    document.body.appendChild(pre);
  }
  function wait(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
  function board() { return document.querySelector("#nownext"); }
  function rows() { return board() ? board().querySelectorAll(".nn-row").length : -1; }
  function text(sel) {
    var el = board() && board().querySelector(sel);
    return el ? el.textContent.trim() : null;
  }
  (async function () {
    try {
      for (var i = 0; i < 200 && state === null; i++) await wait(50);
      out.booted = state !== null;
      ui.tab = "fleet"; render();
      await wait(600);

      // 1. The board is in THE SHOW card, under the big clock, with one row
      //    per garment - and the two bags collapsed into their own group.
      out.inCard = !!board() && !!document.querySelector("#nn-slot #nownext");
      out.rowsCollapsed = rows();
      out.bagsButton = (document.querySelector("#nn-bags-btn") || {}).textContent;
      out.loadedNote = text("[data-note]");
      out.loadedTime = text("[data-time]");
      out.loadedCount = text("[data-count]");

      // 2. Open the bags.
      document.querySelector("#nn-bags-btn").click();
      await wait(400);
      out.rowsOpen = rows();
      document.querySelector("#nn-bags-btn").click();
      await wait(400);
      out.rowsClosedAgain = rows();

      // 3. A show is running: the header counts down to 0:30 and the rows
      //    name what is coming.
      await fetch("/test/fleet?run=running");
      await wait(1600);
      out.running = { cap: text("[data-cap]"), time: text("[data-time]"),
                      count: text("[data-count]"), what: text("[data-what]"),
                      follow: text("[data-follow]") };
      var first = board().querySelector(".nn-row");
      out.firstRow = { who: first.querySelector(".nn-who").textContent.trim(),
                       now: first.querySelector("[data-now-name]").textContent,
                       next: first.querySelector("[data-next-name]").textContent,
                       when: first.querySelector("[data-next-when]").textContent.trim(),
                       arrow: first.querySelector("[data-arr] b").textContent,
                       state: first.querySelector("[data-state]").textContent.trim(),
                       vitals: first.querySelector("[data-vit]").textContent,
                       thumbs: first.querySelectorAll(".nn-thumb svg").length };
      var all = board().querySelectorAll(".nn-row");
      out.vitals = [].map.call(all, function (r) {
        return { text: r.querySelector("[data-vit]").textContent,
                 cls: r.querySelector("[data-vit]").className,
                 off: r.classList.contains("off") };
      });

      // 4. HOLD freezes it, and the end of the show says so.
      await fetch("/test/fleet?run=holding");
      await wait(1600);
      out.holding = { cap: text("[data-cap]"), note: text("[data-note]") };
      await fetch("/test/fleet?run=ended");
      await wait(1600);
      out.ended = { cap: text("[data-cap]"), note: text("[data-note]") };
      await fetch("/test/fleet?run=running");
      await wait(1600);

      // 5. The stage monitor: the SAME board, moved, and moved back.
      var before = board();
      document.querySelector("#nn-stage-btn").click();
      await wait(600);
      out.stageOn = { body: document.body.classList.contains("stagemon"),
                      inOverlay: !!document.querySelector("#nn-stage #nownext"),
                      sameNode: document.querySelector("#nn-stage #nownext") === before,
                      rows: rows(), btn: (document.querySelector("#nn-stage-btn") || {}).textContent,
                      count: text("[data-count]") };
      // ...and it keeps counting while the operator is on another tab.
      ui.tab = "items"; render();
      await wait(1600);
      out.stageOffTab = { alive: !!document.querySelector("#nn-stage #nownext"),
                          count: text("[data-count]"), rows: rows() };
      document.querySelector("#nn-stage-btn").click();
      await wait(600);
      out.stageOff = { body: document.body.classList.contains("stagemon"),
                       inOverlay: !!document.querySelector("#nn-stage #nownext"),
                       // No #nn-slot on the Items tab, so the board leaves
                       // the document rather than be repainted inside a
                       // closed overlay.
                       loose: !before.isConnected, sameNode: board() === null };
      ui.tab = "fleet"; render();
      await wait(600);
      out.backInCard = !!document.querySelector("#nn-slot #nownext");
      out.backSameNode = document.querySelector("#nn-slot #nownext") === before;
    } catch (e) { out.error = String((e && e.stack) || e); }
    publish();
  })();
})();
</script>
"""


def _dump_dom_long(url, tmp_path):
    """_dump_dom(), with enough virtual time for the probe's own waits and
    the page's 1 s poll."""
    browser = _find_browser()
    args = [browser, "--headless=new", "--disable-gpu", "--no-sandbox", "--no-first-run",
            f"--user-data-dir={tmp_path / 'user-data'}",
            "--virtual-time-budget=90000", "--dump-dom", url]
    return subprocess.run(args, capture_output=True, timeout=180).stdout.decode(
        "utf-8", errors="replace")


@pytest.fixture(scope="module")
def page(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("boardpage")
    _require_browser(tmp)
    stand = _Stand(tmp, _PAGE_PROBE)
    try:
        dom = _dump_dom_long(stand.url, tmp)
    finally:
        stand.close()
    match = re.search(r'<pre id="page-out">(.*?)</pre>', dom or "", re.S)
    assert match, f"no #page-out in the dumped DOM:\n{(dom or '')[:3000]}"
    data = json.loads(unescape(match.group(1)))
    assert data.get("error") is None, data["error"]
    assert data["booted"], "the page never loaded its state"
    return data


def test_the_board_draws_one_row_per_garment_with_the_bags_collapsed(page):
    assert page["inCard"], "the board is not in THE SHOW card"
    assert page["rowsCollapsed"] == 3, \
        f"expected the three LOOKs and no bags, got {page['rowsCollapsed']} rows"
    assert "Bags (not synchronised)" in page["bagsButton"]
    assert page["bagsButton"].strip().endswith("2"), page["bagsButton"]
    assert page["rowsOpen"] == 5, "opening the group did not add the two bags"
    assert page["rowsClosedAgain"] == 3


def test_before_start_the_board_says_where_start_begins(page):
    assert page["loadedNote"] == "START runs from 0:00", page["loadedNote"]
    assert page["loadedTime"] == "0:30", "the first cue's time is not named"
    assert page["loadedCount"] == "", "something counted down before START"


def test_a_running_show_counts_the_next_cue_down_in_the_header(page):
    h = page["running"]
    assert h["cap"] == "NEXT" and h["time"] == "0:30", h
    assert re.fullmatch(r"−1?\d s", h["count"]), h["count"]
    assert "LOOK 23 Tops" in h["what"] and "LOOK 24 Skirt" in h["what"], h


def test_a_row_shows_both_thumbnails_the_arrow_and_its_units_vitals(page):
    r = page["firstRow"]
    assert "LOOK 23" in r["who"] and "radxa-01" in r["who"], r
    assert r["now"] == "ivory" and r["next"] == "scarlet", r
    assert r["when"].startswith("0:30"), r["when"]
    assert r["arrow"] == "↓", "top_down is not drawn as an arrow down"
    assert r["thumbs"] == 2, "a row is missing its NOW or its NEXT thumbnail"
    assert "+6 ms" in r["state"], r["state"]
    assert r["vitals"].startswith("radxa-01 · lag +4 ms · rtt 6 ms"), r["vitals"]


def test_a_unit_that_is_behind_or_missing_a_board_or_offline_is_red(page):
    v = page["vitals"]
    assert "red" not in v[0]["cls"], v[0]
    assert "red" in v[1]["cls"], "a unit a third of a second behind is not red"
    # Its boards are counted against the GARMENT's own board list (three
    # boards in the map), not against whatever the unit happens to report.
    assert "lag +330 ms" in v[1]["text"] and "boards 2/3" in v[1]["text"], v[1]
    assert v[2]["off"] and "unit offline" in v[2]["text"], v[2]


def test_hold_and_the_end_of_the_show_say_so_on_the_board(page):
    assert page["holding"]["cap"] == "NEXT (HELD)", page["holding"]
    assert page["holding"]["note"].startswith("HELD at "), page["holding"]
    assert "after RESUME" in page["holding"]["note"]
    assert page["ended"]["cap"] == "SHOW ENDED", page["ended"]


def test_the_stage_monitor_moves_the_one_board_and_gives_it_back(page):
    on = page["stageOn"]
    assert on["body"] and on["inOverlay"], on
    assert on["sameNode"], "the stage monitor built a second board"
    assert on["rows"] == 3 and on["count"], on
    assert "Leave stage monitor" in on["btn"], on["btn"]
    # It is the whole screen: the tab behind it may be anything, and the
    # countdown must keep going all the same.
    alive = page["stageOffTab"]
    assert alive["alive"] and alive["rows"] == 3, alive
    assert alive["count"], "the countdown stopped once the operator left the Units tab"
    off = page["stageOff"]
    assert not off["body"] and not off["inOverlay"], off
    assert off["loose"], "the closed overlay is still holding (and repainting) the board"
    assert page["backInCard"], "the board did not go back into THE SHOW card"
    assert page["backSameNode"], "coming back from the stage monitor lost the board"
