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
from conductor.fleet import Fleet  # noqa: E402
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


# ------------------------------------------- the lag the fleet measures

class _Ticks:
    """A PC clock the test moves by hand."""

    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _lag_fleet():
    """A Fleet with one (unreachable) unit, wound up by hand to the state a
    running show leaves it in: a clock measurement, a poll, an uploaded
    show, and the unit reporting that show's T0 on its own clock."""
    ticks = _Ticks()
    fleet = Fleet({"radxa-01": "127.0.0.1:1"}, poll_s=2.0, clock=ticks)
    fleet._may_adopt = False
    link = fleet.links["radxa-01"]
    link._samples.append((0.004, 500.0))     # 4 ms round trip, unit 500 s ahead
    link.last_seen = ticks.t
    fleet.shows["radxa-01"] = {"id": "S1", "cues": [], "duration": 120.0}
    fleet.run = {"t0": 900.0, "state": "running", "held_at": None, "force": False}
    _unit_at(link, 900.0 + 500.0)            # exactly where this PC says
    return fleet, link, ticks


def _unit_at(link, t0, show_id="S1", state="running"):
    link.status = {"phase": "ready", "host": "radxa-01",
                   "show": {"id": show_id, "state": state, "t0": t0,
                            "applied": 1, "cues": 2, "demo": False}}


def test_the_lag_is_the_units_own_t0_against_this_pcs():
    fleet, link, _ = _lag_fleet()
    assert fleet._show_lag_ms(link, fleet.run) == 0.0
    # The unit's T0 is 48 ms LATER than this PC's, so it thinks the show
    # started later and its position is 48 ms smaller: it is behind.
    _unit_at(link, 900.0 + 500.0 + 0.048)
    assert fleet._show_lag_ms(link, fleet.run) == pytest.approx(48.0)
    _unit_at(link, 900.0 + 500.0 - 0.020)
    assert fleet._show_lag_ms(link, fleet.run) == pytest.approx(-20.0)


def test_there_is_no_lag_to_report_without_something_to_compare():
    fleet, link, ticks = _lag_fleet()
    assert fleet._show_lag_ms(link, None) is None, "no run"
    held = dict(fleet.run, state="holding", held_at=ticks.t)
    assert fleet._show_lag_ms(link, held) is None, "a hold carries a position, not a T0"
    _unit_at(link, 1400.0, show_id="OTHER")
    assert fleet._show_lag_ms(link, fleet.run) is None, "a unit on another show"
    _unit_at(link, 1400.0, state="stopped")
    assert fleet._show_lag_ms(link, fleet.run) is None, "a unit not running it"
    _unit_at(link, 1400.0)
    link.last_seen = ticks.t - 60          # gone quiet: STALE_S is 6 s
    assert fleet._show_lag_ms(link, fleet.run) is None, "an offline unit"


def test_a_seek_waits_for_the_unit_to_take_the_new_t0_not_merely_for_a_poll():
    # T0 moves here on the instant; what a unit reports is up to a poll
    # old. Without a gate the whole board went red for a poll after every
    # RESUME, SEEK and NEXT - "-30000 ms" after a 30 s hold - which is
    # exactly when the operator is looking at it.
    #
    # Waiting out a poll does not settle it either (re-review): the poll
    # that follows the move is the one _supervise() computes its
    # correction FROM, so a gate on the clock alone still admits one
    # pre-correction status. The gate is the unit's own answer.
    fleet, link, ticks = _lag_fleet()
    assert fleet._show_lag_ms(link, fleet.run) == 0.0
    before = link.status["show"]["t0"]
    with fleet._run_lock:                      # as seek()/resume()/next_cue() do
        fleet.run["t0"] -= 30.0
        fleet._t0_moved()
    assert fleet.run["t0_set_at"] == ticks.t
    assert fleet._show_lag_ms(link, fleet.run) is None, \
        "the unit is still on the T0 it had before the seek"
    # A whole poll later, still carrying the old T0 - the very status the
    # correction is about to be computed from.
    for _ in range(2):
        ticks.t += 2.0
        link.last_seen = ticks.t
        _unit_at(link, before)
        assert fleet._show_lag_ms(link, fleet.run) is None, \
            "a clock cannot tell a pre-correction poll from a corrected one"
    # It takes the new T0: from here the number means something again.
    ticks.t += 2.0
    link.last_seen = ticks.t
    _unit_at(link, fleet.run["t0"] + 500.0 + 0.004)
    assert fleet._show_lag_ms(link, fleet.run) == pytest.approx(4.0)
    # ...and the gate stays open for this move, drift and all.
    _unit_at(link, fleet.run["t0"] + 500.0 + 0.300)
    assert fleet._show_lag_ms(link, fleet.run) == pytest.approx(300.0)


def test_a_unit_that_took_the_new_t0_and_landed_badly_out_is_still_reported():
    # A gate that only opened once the unit AGREED could never let a real
    # lag through - and |lag| > 200 ms is the one thing the row reddens
    # for. "Changed", not "close", is what opens it.
    fleet, link, ticks = _lag_fleet()
    with fleet._run_lock:
        fleet.run["t0"] -= 30.0
        fleet._t0_moved()
    ticks.t += 2.0
    link.last_seen = ticks.t
    _unit_at(link, fleet.run["t0"] + 500.0 + 0.300)
    assert fleet._show_lag_ms(link, fleet.run) == pytest.approx(300.0)


def test_a_run_adopted_from_the_units_has_nothing_to_wait_for():
    # _adopt() builds the run FROM the units' own T0, so their T0 does not
    # change and never will - the gate opens on agreement instead.
    fleet, link, ticks = _lag_fleet()
    with fleet._run_lock:
        fleet._t0_moved()
    assert fleet._show_lag_ms(link, fleet.run) == 0.0


def test_every_command_that_moves_t0_stamps_it():
    # One helper, called under the lock by every one of them - checked here
    # against the source, because a new command that forgot it would show up
    # as a board that goes red for a poll and nothing else.
    source = (REPO / "conductor" / "fleet.py").read_text(encoding="utf-8")
    assert "def _t0_moved(self)" in source
    assert source.count("self._run_gen += 1") == 1, \
        "a T0 move bumps the generation without stamping the run"
    assert source.count("self._t0_moved()") == 7, \
        "start / seek / hold / resume / next / stop / adopt - one each"


def test_a_hold_and_a_resume_both_stamp_the_run():
    fleet, link, ticks = _lag_fleet()
    fleet.shows.clear()                   # nothing to post to: no targets
    ticks.t += 5.0
    fleet.hold()
    assert fleet.run["state"] == "holding" and fleet.run["t0_set_at"] == ticks.t
    ticks.t += 5.0
    fleet.resume()
    assert fleet.run["state"] == "running" and fleet.run["t0_set_at"] == ticks.t


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


# Two garments on ONE Radxa - a top on boards 1-4 and its skirt on 5-8,
# which is what the fleet's own "7 of 8 answering" cannot pin on either.
TOPS, SKIRT = [1, 2, 3, 4], [5, 6, 7, 8]


def _u(**kw):
    """A unit as /api/fleet reports it, healthy unless said otherwise."""
    u = {"name": "radxa-01", "online": True, "show_lag_ms": 4.0, "sync_ms": 3.0,
         "rtt_ms": 6.0, "live": 8, "boards": 8, "live_ids": [1, 2, 3, 4, 5, 6, 7, 8],
         "late_ms": 8, "uptime_s": 7200}
    u.update(kw)
    return u


CALLS = {
    # ---- which cue is next, per garment
    "row_before_anything": ["rowAt", L23, 10.0],
    "row_mid_flight": ["rowAt", L23, 32.0],
    "row_after_its_last": ["rowAt", L23, 90.0],
    "row_at_the_very_instant": ["rowAt", L23, 30.0],
    "row_of_a_garment_with_two_changes": ["rowAt", L24, 40.0],
    "row_with_no_cues": ["rowAt", [], 10.0],
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
    # ---- which items are bags. No cue fires it is the NECESSARY part;
    #      the name is only the hint on top of that.
    "bag_by_item": ["isBag", {"item": "AZ271SG1301", "look": "", "model": "AZ271SG1301", "cues": []}],
    "bag_by_model": ["isBag", {"item": "Look25", "look": "25", "model": "AZ271SG2301 Bag", "cues": []}],
    "bag_by_no_look": ["isBag", {"item": "Spare01", "look": "", "model": "AZ271SX0001", "cues": []}],
    "not_a_bag": ["isBag", {"item": "Look23", "look": "23", "model": "AZ271SB2303 (Tops)", "cues": L23}],
    "not_a_bag_zero_look": ["isBag", {"item": "Look00", "look": "0", "model": "AZ271SB0000", "cues": []}],
    # A garment whose file is not LookNN has a blank LOOK until someone
    # types one (conductor/server.py), and "Bag-strap Tops" says Bag - but
    # the show changes both of them, so neither may be folded away.
    "no_look_but_cued": ["isBag", {"item": "AZ271SC6302", "look": "", "model": "AZ271SC6302", "cues": L24}],
    "bag_in_the_name_but_cued": ["isBag", {"item": "Look27", "look": "27",
                                           "model": "AZ271SB2701 Bag-strap Tops", "cues": L23}],
    # ...and an AZ271SG* that somehow does get a cue keeps a row of its own.
    "az_bag_with_a_cue": ["isBag", {"item": "AZ271SG1301", "look": "", "model": "AZ271SG1301", "cues": L23}],
    # ---- the show's own phase
    "phase_no_run": ["phase", None, 0.0, D, False],
    "phase_after_a_stop": ["phase", None, 0.0, D, True],
    "phase_running": ["phase", {"state": "running"}, 10.0, D, True],
    "phase_holding": ["phase", {"state": "holding"}, 10.0, D, True],
    "phase_ended": ["phase", {"state": "running"}, 121.0, D, True],
    # ---- the sweep arrows
    "arrow_top_down": ["arrow", "top_down"],
    "arrow_center": ["arrow", "center"],
    "arrow_natural": ["arrow", "natural"],
    "arrow_unknown": ["arrow", "something_else"],
    # ---- a Radxa's vitals. TOPS and SKIRT are two garments on one unit,
    #      which is the case the unit-wide counts cannot answer.
    "vitals_good": ["vitals", _u(), TOPS],
    "vitals_behind": ["vitals", _u(name="radxa-02", show_lag_ms=480.0), TOPS],
    "vitals_slow_path": ["vitals", _u(name="radxa-03", show_lag_ms=-12.0, rtt_ms=240.0), TOPS],
    # Board 2 of the top is not answering, out of a unit that is carrying
    # eight boards in all - "7/8 answering" would have said nothing.
    "vitals_board_missing": ["vitals", _u(name="radxa-04", live_ids=[1, 3, 4, 5, 6, 7, 8]), TOPS],
    # ...and the skirt on that same unit is whole, so its row stays quiet.
    "vitals_other_garment_is_fine": ["vitals", _u(name="radxa-04", live_ids=[1, 3, 4, 5, 6, 7, 8]), SKIRT],
    "vitals_restarted": ["vitals", _u(name="radxa-05", uptime_s=190), TOPS],
    "vitals_just_restarted": ["vitals", _u(name="radxa-05", uptime_s=20), TOPS],
    "vitals_no_run_yet": ["vitals", _u(name="radxa-06", show_lag_ms=None), TOPS],
    "vitals_offline": ["vitals", {"name": "radxa-07", "online": False}, TOPS],
    "vitals_no_unit": ["vitals", None, TOPS],
    # An agent too old to list which boards are answering: the unit-wide
    # counts are all there is, and they only mean anything when this
    # garment is everything the unit carries.
    "vitals_old_agent_one_garment": ["vitals", _u(name="radxa-08", live_ids=None,
                                                  live=3, boards=4), [1, 2, 3, 4]],
    "vitals_old_agent_two_garments": ["vitals", _u(name="radxa-08", live_ids=None,
                                                   live=7, boards=8), TOPS],
    # ---- the board count on its own
    "boards_all_there": ["boardsOf", _u(), TOPS],
    "boards_one_gone": ["boardsOf", _u(live_ids=[1, 3, 4, 5, 6, 7, 8]), TOPS],
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
    # After a STOP the panels keep what they last drew, and nothing here
    # knows what that was - the position has fallen back to start_at.
    "text_kept": (L23, 0.0, "kept"),
    # A garment no cue ever touches: "last design" would be a lie.
    "text_no_cue_at_all": ([], 10.0, "running"),
    "text_no_cue_at_all_loaded": ([], 0.0, "loaded"),
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
        "head_kept": one("kept", 0.0),
        "head_loaded_from_a_mark": one("loaded", 20.0, startAt=20.0),
        "head_holding": one("holding", 20.0),
        "head_holding_never_flashes": one("holding", 30.4),
        "head_ended": one("ended", 121.0),
        # "May I unplug this garment now?" is what the ENDED note answers
        # once the pictures are being taken back out of the slots.
        "head_ended_clearing": one("ended", 121.0, cleared="clearing"),
        "head_ended_cleared": one("ended", 121.0, cleared="cleared"),
        "head_ended_partly": one("ended", 121.0, cleared="partly"),
        "head_running_while_clearing": one("running", 40.0, cleared="cleared"),
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


def test_after_a_stop_the_board_does_not_claim_the_preset_is_back(board):
    # STOP's own button says the panels keep their image; the run goes away
    # and the position falls back to start_at, so recomputing the designs
    # there put the 0:00 preset on the board for garments standing on stage
    # in their last look.
    kept = board["results"]["text_kept"]
    assert kept["now"] == "(whatever the panels kept)", kept
    assert kept["nowUnknown"] is True, "the page would still draw a thumbnail"
    assert kept["changing"] == "", "nothing is changing when nothing is running"
    # START is still what comes next, and from where.
    assert kept["next"] == "scarlet" and kept["when"] == "0:30"
    assert kept["count"] is None
    head = board["results"]["head_kept"]
    assert head["note"] == ("START runs from 0:00 — the panels keep what "
                            "they are showing until it does"), head
    assert head["count"] is None and head["fired"] is False
    # ...and before any show has run, the preset IS what is on the glass.
    assert board["results"]["text_loaded"]["nowUnknown"] is False


def test_a_garment_no_cue_touches_says_so(board):
    r = board["results"]
    assert r["text_no_cue_at_all"]["next"] == "no cue — nothing changes it"
    assert r["text_no_cue_at_all_loaded"]["next"] == "no cue — nothing changes it"
    assert r["text_no_cue_at_all"]["now"] == "(as before the show)"
    assert r["row_with_no_cues"]["none"] is True and r["row_before_anything"]["none"] is False
    # "last design" is for a garment that has run OUT of cues, not one that
    # never had any.
    assert "last design" in r["text_after_its_last"]["next"]


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
    assert r["phase_after_a_stop"] == "kept", \
        "after a STOP the panels keep their picture - that is not 'loaded'"
    assert r["phase_running"] == "running"
    assert r["phase_holding"] == "holding"
    assert r["phase_ended"] == "ended"


def test_a_bag_is_a_garment_no_cue_fires_and_never_merely_a_name(board):
    r = board["results"]
    assert r["bag_by_item"] is True, "an AZ271SG* item with no cue is a bag"
    assert r["bag_by_model"] is True, "a model that says Bag, with no cue, is a bag"
    assert r["bag_by_no_look"] is True, "no LOOK number and no cue is a bag"
    assert r["not_a_bag"] is False
    assert r["not_a_bag_zero_look"] is False, "LOOK 0 is a LOOK, not a missing one"
    # The three the name alone would have folded away, silently, along with
    # everything the show does to them.
    assert r["no_look_but_cued"] is False, \
        "a garment whose file is not LookNN has a blank LOOK - that is not a bag"
    assert r["bag_in_the_name_but_cued"] is False, \
        "\"Bag-strap Tops\" is a top"
    assert r["az_bag_with_a_cue"] is False, \
        "a bag the show actually changes needs a row like any other garment"


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
    # The lag carries its own error bar: half a round trip is how well this
    # unit's clock is known at all, and a lag inside it is not a lag.
    assert good["text"] == "radxa-01 · lag +4 ±3 ms · rtt 6 ms · boards 4/4 · fired +8 ms", good
    assert good["tone"] == "" and good["offline"] is False
    # Red: too far from this PC's clock, or a board that is not answering.
    assert r["vitals_behind"]["tone"] == "red", r["vitals_behind"]
    assert "lag +480 ±3 ms" in r["vitals_behind"]["text"]
    # Amber: a slow path, but nothing actually wrong yet.
    assert r["vitals_slow_path"]["tone"] == "amber", r["vitals_slow_path"]
    # A restart is worth saying out loud - it is what explains a unit that
    # lost its pictures.
    assert "restarted 3 min ago" in r["vitals_restarted"]["text"]
    assert "restarted just now" in r["vitals_just_restarted"]["text"]
    assert "restarted" not in r["vitals_good"]["text"]
    # Before a run there is no lag to report, and that is not a fault.
    assert "lag —" in r["vitals_no_run_yet"]["text"]
    assert r["vitals_no_run_yet"]["tone"] == ""
    # Offline, and not assigned at all.
    assert r["vitals_offline"]["text"] == "radxa-07 · unit offline"
    assert r["vitals_offline"]["tone"] == "red" and r["vitals_offline"]["offline"] is True
    assert r["vitals_no_unit"]["text"] == "no unit assigned"
    assert r["vitals_no_unit"]["offline"] is True
    # Every value that needs one carries its own tooltip, and the lag's
    # says which way its sign runs - which "+48 ms" cannot say for itself.
    lag = [p for p in good["parts"] if p["text"].startswith("lag ")]
    assert len(lag) == 1 and "BEHIND this PC" in lag[0]["title"], lag
    assert " · ".join(p["text"] for p in good["parts"]) == good["text"]


def test_a_missing_board_is_counted_against_its_own_garment(board):
    # A Radxa can carry a top (boards 1-4) and its skirt (5-8). Holding the
    # unit's "7 of 8 answering" up against one garment's four boards read
    # "7/4" and never went red at all.
    r = board["results"]
    assert r["boards_all_there"] == {"have": 4, "want": 4, "missing": False}
    assert r["boards_one_gone"] == {"have": 3, "want": 4, "missing": True}
    missing = r["vitals_board_missing"]
    assert missing["tone"] == "red" and missing["missing"] is True
    assert "boards 3/4" in missing["text"], missing["text"]
    # ...and the other garment on that same unit is whole, so it stays quiet.
    fine = r["vitals_other_garment_is_fine"]
    assert fine["missing"] is False and fine["tone"] == ""
    assert "boards 4/4" in fine["text"], fine["text"]


def test_an_agent_too_old_to_list_its_boards_is_not_guessed_at(board):
    r = board["results"]
    # One garment, the whole unit: the old counts do compare, so a missing
    # board is still caught.
    old_one = r["vitals_old_agent_one_garment"]
    assert "boards 3/4" in old_one["text"] and old_one["missing"] is True
    # Two garments on the unit: the counts belong to neither of them, so
    # the row says "?" rather than a number about somebody else.
    old_two = r["vitals_old_agent_two_garments"]
    assert old_two["missing"] is False and old_two["tone"] == "", old_two
    assert "boards ?/4" in old_two["text"], old_two["text"]


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


# Every garment in the stand-in wears tests.test_look's MAP, whose three
# boards are numbered 17, 18 and 20.
MAP_BOARDS = [17, 18, 20]


def _unit(name, **kw):
    u = {"name": name, "address": f"192.168.51.10{name[-1]}:8787",
         "online": True, "error": None, "rtt_ms": 6.0, "sync_ms": 3.0,
         "samples": 8, "uptime_s": 7200, "host": name, "commit": "abc1234",
         "phase": "ready", "cue": None, "label": None,
         "boards": len(MAP_BOARDS), "live": len(MAP_BOARDS),
         "live_ids": list(MAP_BOARDS), "board_ids": list(MAP_BOARDS),
         "absent": [], "boards_source": "show", "group_count": 3,
         "uploaded_ago_s": 30.0,
         "saved": 3, "failed": [], "prepare_s": None, "late_ms": 6,
         "verify": None, "demo_count": 0, "unit_error": None, "log": ["ok"],
         "show": None, "refused": None, "demos": [], "show_lag_ms": 4.0,
         "clear": None}
    u.update(kw)
    return u


_RUNS = {
    "none": None,
    "running": {"t0": 0.0, "state": "running", "held_at": None, "force": False, "now": 5.0},
    "holding": {"t0": 0.0, "state": "holding", "held_at": 20.0, "force": False, "now": 20.0},
    "ended": {"t0": 0.0, "state": "running", "held_at": None, "force": False, "now": 190.0},
    # A run that will take its pictures out of the slots when it is over -
    # what makes STOP's own confirm say so (2026-09-27).
    "running_clearing": {"t0": 0.0, "state": "running", "held_at": None,
                         "force": False, "now": 5.0,
                         "clear_after_show": True},
}
# A unit's own show, as it reads once the show has been through it: "stop"
# is what /api/fleet says AFTER a STOP - no run, but units that applied
# their cues, which is how the board knows the panels are not on the preset.
_SHOWS = {
    "none": None,
    "ran": {"id": "S1", "state": "stopped", "applied": 2, "cues": 2,
            "demo": False, "burn": {"state": "burned", "done": 6, "total": 6}},
    # After the show, with "Clear pictures after the show" ticked: the
    # pictures have come back out of slots 1-18 (2026-09-27).
    "cleared": {"id": "S1", "state": "ended", "applied": 2, "cues": 2,
                "demo": False,
                "burn": {"state": "cleared", "done": 6, "total": 6,
                         "failed": []}},
}
# ...and what the unit says about the clear itself, beside its show.
_CLEARS = {
    "none": {"state": "none", "done": 0, "total": 0, "failed": []},
    "clearing": {"state": "clearing", "done": 54, "total": 288, "failed": []},
    "cleared": {"state": "cleared", "done": 288, "total": 288, "failed": []},
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
        # The first cue is a whole minute in on purpose. The stand-in pins
        # the run's position, but the page still carries it forward with
        # performance.now() between polls - and under a headless browser's
        # virtual time that gap is not a real second. A minute of headroom
        # means the assertions below are about the board, not about how
        # fast Chrome felt like running the clock.
        ws.set_timeline(180, [
            {"id": "p23", "item": "Look23", "at": 0, "design": "Look23_color_ivory_grid.csv"},
            {"id": "p24", "item": "Look24", "at": 0, "design": "Look24_color_ivory_grid.csv"},
            # Both garments change together at 1:00 (one cue in the header,
            # two garments named), and the skirt again at 1:30 - which is
            # what the "following" list has to show.
            {"id": "c23", "item": "Look23", "at": 60, "design": "Look23_color_scarlet_grid.csv",
             "transition": "custom", "sequence": "top_down", "span_s": 1.0},
            {"id": "c24", "item": "Look24", "at": 60, "design": "Look24_color_scarlet_grid.csv",
             "transition": "custom", "sequence": "center", "span_s": 1.0},
            {"id": "d24", "item": "Look24", "at": 90, "design": "Look24_color_ivory_grid.csv",
             "transition": "custom", "sequence": "left_right", "span_s": 2.0},
        ])
        state = ws.state()
        written = ws.written_state()
        page = INDEX_HTML.read_text(encoding="utf-8").replace("</body>", probe + "</body>", 1)
        self.run = "none"
        self.unit_show = "none"
        self.unit_clear = "none"
        # "Clear pictures after the show": a real setting on this stand-in, so
        # the page's checkbox can be ticked and read back the way it is on the
        # night (the server stores it with the show, not in the browser).
        self.clear_after = False
        self.cleared = []          # units the "Clear pictures now" button hit
        # Seconds left of a STOP's clear window, or None for "none armed":
        # what the page counts down while it runs.
        self.clear_in_s = None
        # Whether this conductor believes it has uploaded to the units. Off by
        # default, so every assertion written before this one sees the fleet it
        # always saw; the clear's own steps turn it on, because "which units
        # may I clear?" is answered from exactly this.
        self.uploaded = False
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
                # The body is always taken off the wire: this is a
                # keep-alive server, and bytes left on it are read as the
                # next request.
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = 0
                raw = self.rfile.read(length) if length > 0 else b"{}"
                path = self.path.split("?")[0]
                if path == "/api/show/clear_after":
                    stand.clear_after = bool(json.loads(raw).get("on"))
                    return self._json({"ok": True})
                if path == "/api/fleet/clear_pictures":
                    body = json.loads(raw or b"{}")
                    names = body.get("units") or ["radxa-01", "radxa-02",
                                                  "radxa-04", "radxa-05"]
                    stand.cleared.append(sorted(names))
                    return self._json({"units": {n: {"ok": True}
                                                 for n in names}})
                self.do_GET()

            def do_GET(self):
                path = self.path.split("?")[0]
                if path in ("/", "/index.html"):
                    return self._send(page.encode("utf-8"), "text/html; charset=utf-8")
                if path == "/api/state":
                    return self._json(dict(state, show=dict(
                        state["show"], clear_after_show=stand.clear_after)))
                if path == "/api/fleet":
                    show = _SHOWS[stand.unit_show]
                    clear = _CLEARS[stand.unit_clear]
                    return self._json({
                        # radxa-02 is a third of a second behind this PC AND
                        # one board short - either on its own is red.
                        "units": [_unit("radxa-01", show=show, clear=clear),
                                  _unit("radxa-02", show_lag_ms=330.0, show=show,
                                        live=2, live_ids=[17, 18], absent=[20],
                                        clear=clear),
                                  _unit("radxa-03", online=False, error="no answer"),
                                  _unit("radxa-04", show=show, clear=clear),
                                  _unit("radxa-05", show=show, clear=clear)],
                        "last_fire": None, "run": _RUNS[stand.run],
                        "shows": ({n: {"id": "S1", "cues": 2, "boards": []}
                                   for n in ("radxa-01", "radxa-02",
                                             "radxa-04", "radxa-05")}
                                  if stand.uploaded else {}),
                        "clear_in_s": stand.clear_in_s,
                        "corrections": [], "prepared": {},
                        "start_at": 0.0, "show_duration": 180.0,
                        "burn": {"burned": 0, "total": 0}, "timeline": written})
                if path == "/api/fleet/demos":
                    return self._json({"units": {}, "offline": [], "failed": {}})
                if path == "/test/fleet":
                    args = dict(p.split("=", 1) for p in
                                self.path.partition("?")[2].split("&") if "=" in p)
                    stand.run = args.get("run", stand.run)
                    stand.unit_show = args.get("show", stand.unit_show)
                    stand.unit_clear = args.get("clear", stand.unit_clear)
                    if "uploaded" in args:
                        stand.uploaded = args["uploaded"] == "1"
                    if "clear_in" in args:
                        stand.clear_in_s = (None if args["clear_in"] == "none"
                                            else float(args["clear_in"]))
                    return self._json({"run": stand.run, "show": stand.unit_show,
                                       "clear": stand.unit_clear,
                                       "uploaded": stand.uploaded})
                if path == "/test/asked":
                    return self._json({"clear_after": stand.clear_after,
                                       "cleared": stand.cleared})
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
                       lagTitle: [].map.call(first.querySelectorAll("[data-vit] span"),
                                             function (s) { return s.title; })
                                   .filter(function (t) { return /BEHIND/.test(t); })[0] || "",
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

      // 4b. STOP: no run at all, but the units have applied their cues, so
      //     the panels are NOT back on the 0:00 preset.
      await fetch("/test/fleet?run=none&show=ran");
      await wait(1600);
      var kept = board().querySelector(".nn-row");
      out.stopped = { note: text("[data-note]"),
                      now: kept.querySelector("[data-now-name]").textContent,
                      thumb: kept.querySelector("[data-now]").style.display,
                      next: kept.querySelector("[data-next-name]").textContent };
      await fetch("/test/fleet?show=none");
      await wait(1600);
      var fresh = board().querySelector(".nn-row");
      out.neverRan = { note: text("[data-note]"),
                       now: fresh.querySelector("[data-now-name]").textContent,
                       thumb: fresh.querySelector("[data-now]").style.display };
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

      // 6. "Clear pictures after the show": the checkbox beside (3) START.
      //    It is stored with the SHOW, so ticking it posts to the server and
      //    a reload of the state brings it back ticked.
      await fetch("/test/fleet?run=none&show=ran&clear=none&uploaded=1");
      await wait(1600);
      var box = function () { return document.querySelector("#show-clear-after"); };
      out.clearBoxExists = !!box();
      out.clearBoxOffAtFirst = box() ? box().checked : null;
      out.clearBoxTitle = box() ? box().parentElement.getAttribute("title") : null;
      box().checked = true;
      box().dispatchEvent(new Event("change", { bubbles: true }));
      await wait(900);
      out.clearAsked = (await (await fetch("/test/asked")).json()).clear_after;
      out.clearBoxOnAfterRender = box() ? box().checked : null;
      // ...and it survives a full reload of the state, which is what
      // "stored with the show" means.
      await refresh(); render();
      await wait(300);
      out.clearBoxStillOn = box() ? box().checked : null;

      // 7. While the units are clearing: amber "clearing n/N" on the tile,
      //    then a quiet "pictures cleared", and START off with the reason.
      await fetch("/test/fleet?clear=clearing");
      await wait(1600);
      out.clearingTile = document.querySelector("#tiles .tile").textContent;
      await fetch("/test/fleet?show=cleared&clear=cleared");
      await wait(1600);
      var tile = document.querySelector("#tiles .tile");
      out.clearedTile = tile.textContent;
      out.startDisabled = document.querySelector("#show-start").disabled;
      out.presetDisabled = document.querySelector("#show-preset").disabled;
      out.clearedHint = document.querySelector("#show-hint").textContent;
      // The board's own ENDED note - the answer to "may I unplug this
      // garment now?" - while the run is still there, past its end.
      await fetch("/test/fleet?run=ended");
      await wait(1600);
      out.clearedBoardNote = text("[data-note]");
      await fetch("/test/fleet?run=none");
      await wait(1600);

      // 8. "Clear pictures now", inside the WRITE TO UNITS dialog.
      document.querySelector("#show-write").click();
      await wait(600);
      var btn = function () { return document.querySelector("#write-clear-now"); };
      out.clearNowExists = !!btn();
      out.clearNowEnabled = btn() ? !btn().disabled : null;
      out.clearNowWhat = (document.querySelector("#write-choice-clear") || {}).textContent;
      // ...and where it sits: full width, under the two destinations, not a
      // third column squeezed in beside them.
      var choices = document.querySelector(".dlg-choices").getBoundingClientRect();
      var box = document.querySelector("#write-choice-clear").getBoundingClientRect();
      out.clearBoxBox = { below: box.top >= choices.bottom - 1,
                          asWide: box.width >= choices.width - 2,
                          onScreen: box.height > 20 && box.width > 200 };
      window.confirm = function (msg) { out.clearNowAsked = msg; return true; };
      btn().click();
      await wait(1200);
      out.clearNowSent = (await (await fetch("/test/asked")).json()).cleared;
      // ...and it is refused outright while a show is running.
      await fetch("/test/fleet?run=running&show=ran&clear=none");
      await wait(1600);
      out.clearNowWhileRunning = btn() ? btn().disabled : null;
      out.clearNowWhy = (document.querySelector("#write-clear-why") || {}).textContent;
      document.querySelector("#write-close").click();
      await wait(400);

      // 9. STOP's own confirm. Without the box it says what it always said;
      //    with it, STOP starts a clock on something irreversible and has to
      //    say so and how to take it back.
      var asked = null;
      window.confirm = function (msg) { asked = msg; return false; };
      await fetch("/test/fleet?run=running&show=ran&clear=none");
      await wait(1600);
      document.querySelector("#show-stop").click();
      await wait(400);
      out.stopPlain = asked;
      asked = null;
      await fetch("/test/fleet?run=running_clearing");
      await wait(1600);
      out.stopRunClears = !!(fleet.run && fleet.run.clear_after_show);
      document.querySelector("#show-stop").click();
      await wait(400);
      out.stopClearing = asked;

      // 10. ...and the window counting down, where the way out of it is.
      await fetch("/test/fleet?run=none&clear_in=21");
      await wait(1600);
      out.windowHint = document.querySelector("#show-hint").textContent;
      await fetch("/test/fleet?clear_in=none");
      await wait(1600);
      out.windowGone = document.querySelector("#show-hint").textContent;

      // 11. The checkbox's own note, with the box ticked.
      out.clearAfterNote = (document.querySelector("#show-clear-after-note")
                            || {}).textContent;
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
    assert page["loadedTime"] == "1:00", "the first cue's time is not named"
    assert page["loadedCount"] == "", "something counted down before START"


def test_a_running_show_counts_the_next_cue_down_in_the_header(page):
    # The run is pinned at 0:05 and the first cue is at 1:00, so the
    # countdown is around 55 s however fast the browser ran its clock.
    h = page["running"]
    assert h["cap"] == "NEXT" and h["time"] == "1:00", h
    assert re.fullmatch(r"−\d\d? s", h["count"]), h["count"]
    assert 40 <= int(h["count"][1:-2]) <= 55, h["count"]
    assert "LOOK 23 Tops" in h["what"] and "LOOK 24 Skirt" in h["what"], h
    # ...and the cue after it, on the right.
    assert page["running"]["follow"].startswith("1:30 · LOOK 24 Skirt · ivory"), \
        page["running"]["follow"]


def test_a_row_shows_both_thumbnails_the_arrow_and_its_units_vitals(page):
    r = page["firstRow"]
    assert "LOOK 23" in r["who"] and "radxa-01" in r["who"], r
    assert r["now"] == "ivory" and r["next"] == "scarlet", r
    assert r["when"].startswith("1:00"), r["when"]
    assert r["arrow"] == "↓", "top_down is not drawn as an arrow down"
    assert r["thumbs"] == 2, "a row is missing its NOW or its NEXT thumbnail"
    assert "+6 ms" in r["state"], r["state"]
    assert r["vitals"].startswith("radxa-01 · lag +4 ±3 ms · rtt 6 ms"), r["vitals"]
    assert "boards 3/3" in r["vitals"], r["vitals"]
    assert "BEHIND this PC" in r["lagTitle"], r["lagTitle"]


def test_a_unit_that_is_behind_or_missing_a_board_or_offline_is_red(page):
    v = page["vitals"]
    assert "red" not in v[0]["cls"], v[0]
    assert "red" in v[1]["cls"], "a unit a third of a second behind is not red"
    # Its boards are counted against the GARMENT's own board list (three
    # boards in the map), not against whatever the unit happens to report.
    assert "lag +330 ±3 ms" in v[1]["text"] and "boards 2/3" in v[1]["text"], v[1]
    assert v[2]["off"] and "unit offline" in v[2]["text"], v[2]


def test_hold_and_the_end_of_the_show_say_so_on_the_board(page):
    assert page["holding"]["cap"] == "NEXT (HELD)", page["holding"]
    assert page["holding"]["note"].startswith("HELD at "), page["holding"]
    assert "after RESUME" in page["holding"]["note"]
    assert page["ended"]["cap"] == "SHOW ENDED", page["ended"]


def test_after_a_stop_the_rows_do_not_put_the_preset_back_on_the_glass(page):
    # No run, so the position falls back to start_at - and recomputing the
    # designs there said "ivory" for garments standing on stage in whatever
    # they last changed into, which is the opposite of what STOP's own
    # button promises ("panels keep their image").
    stopped = page["stopped"]
    assert stopped["now"] == "(whatever the panels kept)", stopped
    assert stopped["thumb"] == "none", "a design nobody chose was still drawn"
    assert "the panels keep what they are showing" in stopped["note"], stopped["note"]
    assert stopped["next"] == "scarlet", "START is still what comes next"
    # ...and before any show has run, the preset IS on the glass.
    fresh = page["neverRan"]
    assert fresh["now"] == "ivory", fresh
    assert fresh["thumb"] != "none"
    assert fresh["note"] == "START runs from 0:00", fresh["note"]


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

# ---- "Clear pictures after the show" ----
# 2026-09-27: a garment unplugged with its boards still on battery restarted
# the factory autoplay and cycled slots 0-18 - it replayed the show's
# pictures on its own. The page's part of the answer: one checkbox, the tile
# rows that say where the clear has got to, and the button that does it now.

def test_the_checkbox_sits_next_to_start_and_is_stored_with_the_show():
    bar = PAGE[PAGE.index('id="show-start"'):PAGE.index('id="show-write"')]
    assert 'id="show-clear-after"' in bar, \
        "the checkbox is not next to (3) START"
    assert "Clear pictures after the show" in bar
    # Ticked from the SERVER's answer, never from a browser-local flag: it
    # belongs to the evening, not to whoever's tab is open.
    assert "state.show.clear_after_show" in bar
    assert "localStorage" not in bar
    # The operator's own Japanese tooltip, word for word.
    assert "ショー終了後にスロット 1〜18 を削除" in PAGE
    assert "Radxa を外した後の自動巡回で本番の絵が出ないようにする" in PAGE
    # ...and a change posts it, then re-reads the state, so a refusal puts
    # the tick back where the show says it belongs.
    handler = PAGE[PAGE.index('if (id === "show-clear-after")'):]
    handler = handler[:handler.index("if (id === \"show-manual\")")]
    assert '"/api/show/clear_after"' in handler and "{ on }" in handler
    assert "await refresh()" in handler


def test_the_stop_confirm_says_the_pictures_are_about_to_go():
    # STOP is also how a director aborts a show half way through - the
    # ordinary reason to press it - and a clear cannot be undone without a
    # three-minute Upload. The confirm has to say so (review, 2026-09-27).
    body = _function_body("stopQuestion")
    assert "Stop the show? The panels keep what they are showing." in body
    assert "fleet?.run?.clear_after_show" in body, \
        "the warning is added whether or not this run clears"
    assert "Pictures will be cleared ${CLEAR_AFTER_STOP_S} s after STOP" in body
    assert "unless you START again" in body
    assert "clearing cannot be undone (Upload again)" in body
    # ...and it is what the button actually asks.
    assert "confirm(stopQuestion())" in PAGE
    assert 'confirm("Stop the show? The panels keep what they are showing.")' \
        not in PAGE, "the old, silent confirm is still on the button"
    assert "const CLEAR_AFTER_STOP_S = 30;" in PAGE


def test_the_window_is_counted_down_where_the_way_out_is():
    body = _function_body("showClockText")
    assert "fleet.clear_in_s" in body
    assert "Pictures will be cleared on every unit in" in body
    assert "press ③ START (or ② Show preset) to keep them" in body
    assert "cannot" in body and "uploaded again" in body


def test_the_checkbox_says_the_units_only_learn_it_at_upload():
    # Ticking it after an Upload never reaches the units (the show keeps its
    # id by design), so the unit-side fallback - the PC vanishing before the
    # show ends - exists only for units Uploaded after the tick.
    note = _function_body("clearAfterNote")
    assert "tick before ① Upload for the units to clear on their own" in note
    assert "state.show.clear_after_show" in note, \
        "the note is shown even with the box unticked"
    assert 'id="show-clear-after-note"' in PAGE
    # ...and the tooltip says the whole of it (the source wraps the sentence,
    # so this looks for its distinctive halves rather than one long line).
    title = PAGE[PAGE.index("const CLEAR_AFTER_TITLE"):]
    title = title[:title.index("\nfunction clearAfterNote")]
    assert "This PC sends the clear itself when the show ends or is stopped" \
        in title
    assert "The UNITS only learn it at the " in title
    assert "next ① Upload" in title
    assert "if they are to clear on their own " in title


def test_the_hold_button_has_readable_ink_in_both_schemes():
    # An amber SURFACE: white on dark mode's #ffb860 is 1.71:1.
    rule = PAGE[PAGE.index(".go button.hold {"):]
    rule = rule[:rule.index("}") + 1]
    assert "var(--warn-ink)" in rule, rule
    assert "#fff" not in rule, rule


def test_the_tile_says_where_the_clear_has_got_to():
    body = _function_body("clearText")
    assert '"clearing "' not in body     # ...it is a template, see below
    assert "clearing ${esc(c.done)}" in body and "/${esc(c.total)}" in body
    assert '"vf re"' in body, "the progress is not amber"
    assert "pictures cleared" in body and '"vf ok"' in body
    # Grey, not green: this is the thing the operator asked for, not a thing
    # that went well.
    css = PAGE[PAGE.index(".tile .vf.re"):PAGE.index(".tile .vf.re") + 400]
    assert ".tile .vf.ok { color: var(--dim)" in css
    # ...and the burn row says the pictures have to be uploaded again.
    pictures = _function_body("picturesText")
    assert '"cleared — Upload again"' in pictures
    assert '"cleared partially — Upload again"' in pictures
    assert "clearText(u)" in PAGE[PAGE.index("function renderTiles"):], \
        "the tile does not show the clear at all"


def test_start_and_preset_are_off_while_any_unit_is_cleared():
    body = _function_body("showClockText")
    assert "clearedUnits()" in body
    assert '"show-start": want > 0 && !cleared.length' in body
    assert '"show-preset"' in body and "!cleared.length" in body
    # ...and the hint names the units and what to do about them.
    assert "Pictures were cleared after the last show on" in body
    assert "① Upload writes them again" in body
    gate = _function_body("clearedUnits")
    assert 'state === "cleared"' in gate


def test_the_dialog_has_a_clear_pictures_now_button():
    body = _function_body("writeClearHtml")
    assert 'id="write-clear-now"' in body and "Clear pictures now" in body
    assert "Deletes slots 1–18" in body
    # It says that nothing is repainted and that an Upload is needed next.
    assert "keep the look they are showing" in body
    assert "Upload again before the next START" in body
    # The same units and LOOK choice as Upload, refused while a run exists.
    state = _function_body("writeState")
    assert "clearTargets" in state and "clearWhy" in state
    assert "The show is running — press STOP first." in state
    send = _function_body("writeClearNow")
    assert '"clear_pictures"' in send
    assert "s.only ? { units: s.only } : {}" in send
    assert "clearNowQuestion" in send
    # ...and it asks first, in full.
    question = PAGE[PAGE.index("const clearNowQuestion ="):]
    question = question[:question.index("function writeDialogHtml")]
    assert "nothing is repainted" in question and "nothing goes white" in question
    assert "uploaded again first" in question


def test_the_ended_note_says_whether_the_pictures_are_cleared(board):
    r = board["results"]
    # "May I unplug this garment now?" is what this line answers.
    assert r["head_ended"]["note"] == "show ended"
    assert r["head_ended_clearing"]["note"] == "show ended — clearing the pictures"
    assert r["head_ended_cleared"]["note"] == "show ended — pictures cleared"
    assert r["head_ended_partly"]["note"] == \
        "show ended — pictures NOT cleared everywhere"
    # Only the ENDED note: a running show's header is about the next cue.
    assert "cleared" not in r["head_running_while_clearing"]["note"]


@pytest.mark.parametrize("key", ["clearBoxExists", "clearNowExists"])
def test_the_clear_controls_are_really_on_the_page(page, key):
    assert page[key] is True, key


def test_the_checkbox_posts_to_the_server_and_comes_back_ticked(page):
    assert page["clearBoxOffAtFirst"] is False, "it must default to off"
    assert "ショー終了後にスロット 1〜18 を削除" in (page["clearBoxTitle"] or "")
    assert page["clearAsked"] is True, "ticking it never reached the server"
    assert page["clearBoxOnAfterRender"] is True
    # The whole point of storing it with the show: a reload finds it ticked.
    assert page["clearBoxStillOn"] is True


def test_the_tile_and_the_board_follow_the_clear(page):
    assert "clearing 54/288" in page["clearingTile"], page["clearingTile"]
    assert "pictures cleared" in page["clearedTile"], page["clearedTile"]
    assert "cleared — Upload again" in page["clearedTile"], page["clearedTile"]
    assert page["startDisabled"] is True, "START is still offered"
    assert page["presetDisabled"] is True
    assert "Pictures were cleared after the last show on" in page["clearedHint"]
    assert "radxa-01" in page["clearedHint"]
    assert page["clearedBoardNote"] == "show ended — pictures cleared", \
        page["clearedBoardNote"]


def test_stop_warns_about_the_clear_only_when_this_run_clears(page):
    # Without the box, the confirm is the one it always was.
    assert page["stopPlain"] == \
        "Stop the show? The panels keep what they are showing.", page["stopPlain"]
    # With it, STOP starts a clock on something that cannot be undone.
    assert page["stopRunClears"] is True, "the stand's run does not clear"
    said = page["stopClearing"] or ""
    assert said.startswith("Stop the show? The panels keep what they are "
                           "showing."), said
    assert "Pictures will be cleared 30 s after STOP" in said, said
    assert "unless you START again" in said, said
    assert "clearing cannot be undone (Upload again)" in said, said


def test_the_window_counts_down_on_the_panel(page):
    hint = page["windowHint"] or ""
    assert "Pictures will be cleared on every unit in 21 s" in hint, hint
    assert "press ③ START (or ② Show preset) to keep them" in hint, hint
    assert "uploaded again" in hint, hint
    # ...and it goes when the window does.
    assert "will be cleared on every unit" not in (page["windowGone"] or "")


def test_the_checkbox_carries_its_note_when_ticked(page):
    assert "tick before ① Upload for the units to clear on their own" \
        in (page["clearAfterNote"] or ""), page["clearAfterNote"]


def test_the_clear_now_button_asks_first_and_then_sends(page):
    assert page["clearNowEnabled"] is True
    assert "Deletes slots 1–18" in page["clearNowWhat"], page["clearNowWhat"]
    assert "keep the look they are showing" in page["clearNowWhat"]
    # It is the one thing in the dialog that takes something away, so it sits
    # full width UNDER the two destinations rather than beside them.
    assert page["clearBoxBox"] == {"below": True, "asWide": True,
                                   "onScreen": True}, page["clearBoxBox"]
    asked = page["clearNowAsked"] or ""
    assert "Delete slots 1–18 on" in asked, asked
    assert "nothing goes white" in asked and "uploaded again first" in asked
    assert page["clearNowSent"], "the confirm was accepted but nothing was sent"
    # ...and while a show is running the button is off, with the reason.
    assert page["clearNowWhileRunning"] is True
    assert "press STOP first" in (page["clearNowWhy"] or ""), page["clearNowWhy"]
