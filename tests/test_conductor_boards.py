"""The Units tile says when a unit is not working to this show's board list.

2026-09-26: radxa-04 lost its last cue in all three rehearsals and the
Conductor showed nothing wrong - every picture was written. What was wrong
was inside the unit: it had explored the bus in standby, found six empty
sockets past the 16-board garment and went on probing them all show. The
unit now takes the show's list as the only list (ui/runner.py) and reports
which list it is working to; conductor/fleet.py passes that through, and
the tile marks a unit whose list is not this show's.

Two halves, like tests/test_conductor_music.py: plain text tests over
conductor/web/index.html, and one headless-browser run of the page's own
pure comparison, lifted out between the `<<< BOARDLIST ... >>>` markers.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
INDEX_HTML = REPO / "conductor" / "web" / "index.html"

sys.path.insert(0, str(REPO))

from tests.test_designer_build import _dump_dom, _require_browser  # noqa: E402

PAGE = INDEX_HTML.read_text(encoding="utf-8")
_MODULE = re.search(r"<<< BOARDLIST:.*?>>>\n(.*?)\n// <<< /BOARDLIST >>>",
                    PAGE, re.S)

TOOLTIP = ("The unit is probing boards this show does not have; "
           "re-upload or restart the unit.")


# --------------------------------------------------------------- the page

def test_the_comparison_is_marked_off_and_pure():
    assert _MODULE, "the BOARDLIST markers are gone from conductor/web/index.html"
    code = "\n".join(re.sub(r"//.*", "", line)
                     for line in _MODULE.group(1).splitlines())
    assert "function note(" in code and "function restarted(" in code
    # Pure means pure: it is handed a unit's tile data and the show's own
    # ids, and reaches for nothing else.
    for forbidden in ("document.", "$(", "fleet", "esc(", "performance.now(",
                      "setTimeout", "Date."):
        assert forbidden not in code, f"the pure layer reaches for {forbidden!r}"


# ---- the DIP ID cell in BOARDS AND DIP SWITCHES (Designs tab) ----
# The operator changed AZ271SD1301's DIP 27 to 28 on the garment itself
# (2026-09-27): the cell has to be typed in, say when it was, and offer the
# way back to the rank.

def _dip_table() -> str:
    start = PAGE.index("<h2>BOARDS AND DIP SWITCHES")
    return PAGE[start:PAGE.index("</div>", PAGE.index("</table>", start))]


def test_the_dip_id_is_typed_in_and_says_when_it_was_set_by_hand():
    table = _dip_table()
    # An input, like the board number beside it - not a read-only <b>.
    assert 'class="dip' in table and "data-dip-board=" in table
    assert "<b>${b.dip_id}</b>" not in table
    assert "set by hand" in table and "data-dip-clear=" in table
    # SWITCHES ON is still the server's own, so it follows the DIP.
    assert "${esc(b.switches_on)}" in table
    # The amber note, in the words the operator asked for.
    assert ("4+ switches on — reported unreliable; set another ID by hand"
            in table)
    assert 'class="warn"' in table and "b.dip_unreliable" in table
    # ...and the meta sentence under the table says how to use the cell.
    assert ("DIP IDs follow the numbers, smallest first — click a DIP ID to "
            "set the number the board's switches really have") in PAGE


def test_a_typed_dip_id_and_its_revert_go_to_the_one_boards_endpoint():
    # One table, one endpoint (server.py's /api/boards takes `dips`).
    handler = PAGE[PAGE.index("e.target.dataset.dipBoard"):]
    handler = handler[:handler.index("} else if")]
    assert '"/api/boards", { item: ui.item, dips:' in handler
    # An empty cell means "back to the rank", the same as the badge's ✕.
    assert 'raw === "" ? null' in handler
    clear = PAGE[PAGE.index("[data-dip-clear]"):]
    clear = clear[:clear.index("const row =")]
    assert '"/api/boards", { item: ui.item, dips: { [dipClear.dataset.dipClear]: null } }' in clear
    # Enter saves, like the board number and the label.
    assert "input.lab, input.bno, input.dip" in PAGE


def test_the_tile_shows_both_marks_in_the_same_amber_family():
    # The board-list note sits on the Boards row, the restart note beside
    # the pictures - both in the `vf` family the landing check uses, so a
    # unit that will run but is not in the state it should be reads the
    # same wherever the trouble is.
    assert "${boardListMark(u)}" in PAGE and "${restartMark(u)}" in PAGE
    for fn in ("boardListMark", "restartMark"):
        body = PAGE[PAGE.index(f"function {fn}("):]
        body = body[:body.index("\n}")]
        assert 'class="vf re"' in body, f"{fn} does not use the amber marker"
        assert "esc(" in body, f"{fn} writes unescaped text into the tile"
    assert TOOLTIP.split(";")[0] in PAGE
    assert "restarted since Upload" in PAGE


def test_the_tile_reads_the_fields_the_fleet_passes_through():
    # conductor/fleet.py's names, spelled the same on both sides.
    for field in ("board_ids", "absent", "boards_source", "uptime_s",
                  "uploaded_ago_s"):
        assert field in _MODULE.group(1) or f"u.{field}" in PAGE, \
            f"the tile never reads {field}"


# ------------------------------------------------------- the browser half

def unit(**over):
    """A unit as the tile has it (conductor/fleet.py's snapshot)."""
    base = dict(name="radxa-04", online=True, show={"id": "tops", "state": "running"},
                board_ids=list(range(1, 17)), absent=[], boards_source="show",
                group_count=16, uptime_s=6000, uploaded_ago_s=600)
    base.update(over)
    return base


TOPS = {"id": "tops", "boards": list(range(1, 17))}

CASES = {
    # Nothing to say: the unit is on this show's list. Every healthy unit.
    "on_the_shows_list": (unit(), TOPS),
    # radxa-04 on the night: exploring, six empty sockets past the garment.
    "exploring_past_the_garment": (
        unit(boards_source="explore", absent=list(range(17, 23))), TOPS),
    # The same set, but the show's list is not what is in force - the
    # explore can widen again at any reprobe.
    "exploring_the_same_set": (unit(boards_source="explore"), TOPS),
    # A unit started with --boards for another garment.
    "its_own_fixed_list": (unit(boards_source="fixed", board_ids=[1, 2, 3],
                                group_count=3), TOPS),
    # A previous show's list still in force.
    "another_shows_list": (unit(board_ids=list(range(1, 13))), TOPS),
    # One job's boards - a manual /prepare of a few, or a show file whose
    # cues named a board its own list does not have.
    "a_jobs_own_boards": (unit(boards_source="job", board_ids=[5],
                               group_count=5), TOPS),
    # Nothing that can be judged honestly.
    "offline": (unit(online=False), TOPS),
    "no_show_on_the_unit": (unit(show=None), TOPS),
    "no_show_uploaded_for_it": (unit(boards_source="explore"), None),
    "show_file_without_a_list": (unit(boards_source="explore"),
                                 {"id": "tops", "boards": []}),
    "agent_too_old_to_say": (unit(boards_source=None, absent=None), TOPS),
    # A unit playing its own demo from its own menu: the list it is
    # working to is the DEMO's garment, and this show has no business
    # judging it (review, 2026-09-27).
    "playing_its_own_demo": (
        unit(boards_source="explore", absent=list(range(17, 23)),
             show={"id": "demo", "state": "running", "demo": True}), TOPS),
}

RESTART_CASES = {
    "restarted_after_the_upload": unit(uptime_s=30, uploaded_ago_s=600),
    "up_since_before_the_upload": unit(uptime_s=600, uploaded_ago_s=30),
    "within_the_rounding_slack": unit(uptime_s=88, uploaded_ago_s=90),
    "nothing_uploaded_by_this_conductor": unit(uptime_s=30, uploaded_ago_s=None),
    "agent_without_an_uptime": unit(uptime_s=None, uploaded_ago_s=600),
    "offline": unit(online=False, uptime_s=30, uploaded_ago_s=600),
    "playing_its_own_demo": unit(uptime_s=30, uploaded_ago_s=900,
                                 show={"id": "demo", "state": "running",
                                       "demo": True}),
    "no_show_at_all": unit(uptime_s=30, uploaded_ago_s=900, show=None),
}

_PROBE = """<!doctype html><meta charset="utf-8"><title>boardlist</title><body>
<script>
"use strict";
%(module)s
var CASES = %(cases)s, RESTARTS = %(restarts)s;
var out = { error: null, notes: {}, restarts: {}, ranges: null };
try {
  out.ranges = [BOARDLIST.ranges([2, 3, 4, 19]), BOARDLIST.ranges([]),
                BOARDLIST.ranges([7]), BOARDLIST.RESTART_SLACK_S];
  for (var name in CASES)
    out.notes[name] = BOARDLIST.note(CASES[name][0], CASES[name][1]);
  for (var other in RESTARTS)
    out.restarts[other] = BOARDLIST.restarted(RESTARTS[other]);
} catch (e) { out.error = String((e && e.stack) || e); }
var pre = document.createElement("pre");
pre.id = "boards-out";
pre.textContent = JSON.stringify(out);
document.body.appendChild(pre);
</script></body>
"""


@pytest.fixture(scope="module")
def answers(tmp_path_factory):
    """One headless run of the page's own comparison, on its own."""
    assert _MODULE, "the BOARDLIST markers are gone from conductor/web/index.html"
    tmp = tmp_path_factory.mktemp("boardlist")
    _require_browser(tmp)
    page = tmp / "boardlist.html"
    page.write_text(_PROBE % {"module": _MODULE.group(1),
                              "cases": json.dumps(CASES),
                              "restarts": json.dumps(RESTART_CASES)},
                    encoding="utf-8")
    url = "file:///" + str(page.resolve()).replace("\\", "/")
    dom = _dump_dom(url, tmp)
    match = re.search(r'<pre id="boards-out">(.*?)</pre>', dom or "", re.S)
    assert match, f"the probe page wrote nothing: {(dom or '')[-400:]}"
    out = json.loads(match.group(1).replace("&quot;", '"').replace("&amp;", "&")
                     .replace("&lt;", "<").replace("&gt;", ">"))
    assert not out["error"], out["error"]
    return out


def test_the_ranges_read_like_the_units_own_log(answers):
    assert answers["ranges"][:3] == ["2-4,19", "", "7"]


def test_a_unit_on_the_shows_list_is_not_marked(answers):
    for name in ("on_the_shows_list", "offline", "no_show_on_the_unit",
                 "no_show_uploaded_for_it", "show_file_without_a_list",
                 "agent_too_old_to_say", "playing_its_own_demo"):
        assert answers["notes"][name] is None, name


def test_the_rehearsals_own_unit_is_marked_with_both_lists(answers):
    note = answers["notes"]["exploring_past_the_garment"]
    assert note["text"] == "unit list 1-22 (exploring, 17-22 absent) ≠ show 1-16"
    assert note["title"] == TOOLTIP


def test_a_list_that_matches_but_is_not_the_shows_is_marked_too(answers):
    # The explore is still on: it can widen past the garment again at the
    # next reprobe, which is exactly how the night went.
    assert (answers["notes"]["exploring_the_same_set"]["text"]
            == "unit list 1-16 (exploring) — not this show's")


def test_a_unit_on_another_list_names_where_that_list_came_from(answers):
    assert (answers["notes"]["its_own_fixed_list"]["text"]
            == "unit list 1-3 (its own --boards list) ≠ show 1-16")
    assert (answers["notes"]["another_shows_list"]["text"]
            == "unit list 1-12 (from another show) ≠ show 1-16")
    assert (answers["notes"]["a_jobs_own_boards"]["text"]
            == "unit list 5 (a prepare/burn list) ≠ show 1-16")


_PARSE_PROBE = """
<pre id="parse-out"></pre>
<script>
var seen = {};
for (var name of ["BOARDLIST", "boardListMark", "restartMark", "renderTiles"]) {
  try { seen[name] = typeof globalThis[name] !== "undefined"
        ? typeof globalThis[name] : String(eval("typeof " + name)); }
  catch (e) { seen[name] = "threw: " + e; }
}
document.getElementById("parse-out").textContent = JSON.stringify(seen);
</script>
"""


def test_the_whole_page_still_parses_and_defines_the_tile_helpers(tmp_path):
    # The rest of this file lifts the pure block out and runs it alone,
    # which cannot see a syntax error made further up the page - and
    # nothing else loads index.html whole.
    _require_browser(tmp_path)
    page = tmp_path / "index.html"
    page.write_text(PAGE + _PARSE_PROBE, encoding="utf-8")
    dom = _dump_dom("file:///" + str(page.resolve()).replace("\\", "/"), tmp_path)
    match = re.search(r'<pre id="parse-out">(.*?)</pre>', dom or "", re.S)
    assert match, f"the page wrote nothing: {(dom or '')[-400:]}"
    seen = json.loads(match.group(1).replace("&quot;", '"').replace("&amp;", "&")
                      .replace("&lt;", "<").replace("&gt;", ">"))
    assert seen == {"BOARDLIST": "object", "boardListMark": "function",
                    "restartMark": "function", "renderTiles": "function"}, seen


def test_a_restart_after_the_upload_is_marked_and_nothing_else_is(answers):
    assert answers["restarts"]["restarted_after_the_upload"] is True
    for name in ("up_since_before_the_upload", "within_the_rounding_slack",
                 "nothing_uploaded_by_this_conductor",
                 "agent_without_an_uptime", "offline",
                 "playing_its_own_demo", "no_show_at_all"):
        assert answers["restarts"][name] is False, name
