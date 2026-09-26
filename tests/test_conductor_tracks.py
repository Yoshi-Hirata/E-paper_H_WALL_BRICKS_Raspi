"""Placing a design on the Conductor's timeline: the per-row "+", the
hover ghost, the hints that say a track can be clicked at all, and the
1-second snap.

The affordances the designers' simulator grew on 2026-09-25/26 after
watching people fail to find them; this file is the Conductor's own half.
One headless run of the REAL conductor/web/index.html against a REAL
Workspace, so the times the "+" places are the times the server then
accepts.
"""
from __future__ import annotations

import json
import re
import sys
from html import unescape
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
INDEX_HTML = REPO / "conductor" / "web" / "index.html"

sys.path.insert(0, str(REPO))

from conductor import timeline  # noqa: E402
from tests.test_conductor_intake import _Stand  # noqa: E402
from tests.test_designer_build import _dump_dom, _require_browser  # noqa: E402

PAGE = INDEX_HTML.read_text(encoding="utf-8")


# ------------------------------------------------------- the page as text

def test_the_append_gap_is_the_models_own_gap():
    """An append that used its own idea of "a reasonable gap" would place
    cues the server's validate() then rejects, which is the one thing the
    button must never do."""
    match = re.search(r"const APPEND_GAP_S = ([0-9.]+);", PAGE)
    assert match, "index.html has no APPEND_GAP_S"
    assert float(match.group(1)) == timeline.GAP_AFTER_REFRESH_S


def test_the_ruler_and_the_playhead_leave_room_for_the_add_column():
    # The ruler row needs a third (empty) cell and the playhead wrapper
    # has to stop short by the same amount, or both run past the tracks.
    assert 'grid-template-columns: 150px 1fr var(--tl-add-w)' in PAGE
    assert 'class="tl-ruler" id="ruler">${ruler}</div><div></div>' in PAGE
    assert 'left:150px;right:var(--tl-add-w)' in PAGE


# ------------------------------------------------------- the page, running

_PROBE = """
<script>
(function () {
  var out = { error: null };
  function publish() {
    var pre = document.createElement("pre");
    pre.id = "tracks-out";
    pre.textContent = JSON.stringify(out);
    document.body.appendChild(pre);
  }
  var wait = ms => new Promise(r => setTimeout(r, ms));
  var $$ = sel => [...document.querySelectorAll(sel)];
  function trackOf(key) { return document.querySelector('.tl-track[data-track="' + key + '"]'); }
  function toastNow() { return (document.querySelector("#toast") || {}).textContent || ""; }
  function hover(track, frac, shift) {
    var box = track.getBoundingClientRect();
    track.dispatchEvent(new PointerEvent("pointermove", { bubbles: true,
      clientX: box.left + box.width * frac, clientY: box.top + box.height / 2,
      shiftKey: !!shift }));
  }
  function ghostNow(track) {
    var g = track.querySelector(".cue-ghost");
    return g ? { at: g.dataset.at, left: g.style.left, label: g.firstChild.textContent } : null;
  }
  function cueRow(key) {
    return state.show.cues.filter(c => c.item === key).sort((a, b) => a.at - b.at)
      .map(c => ({ at: c.at, design: c.design, complete: c.complete }));
  }
  (async function () {
    try {
      await wait(400);
      ui.tab = "timeline"; render();
      await wait(300);

      // 1. Nothing placed yet: both hints, and a "+" on every track row.
      out.empty = {
        inTrack: $$(".tl-empty").map(e => e.textContent.trim()),
        firstHint: ($$(".tl-firsthint")[0] || {}).textContent || "",
        adds: $$("button.tl-add").map(b => ({ item: b.dataset.add, title: b.title,
                                              tag: b.tagName, type: b.type })),
      };

      // 2. The ruler, the tracks and the playhead line up, with the "+"
      //    column left out of all three.
      var ruler = document.querySelector("#ruler").getBoundingClientRect();
      var track = trackOf("Look22").getBoundingClientRect();
      var ph = document.querySelector("#playhead").parentElement.getBoundingClientRect();
      out.alignment = { ruler: [ruler.left, ruler.right], track: [track.left, track.right],
                        playhead: [ph.left, ph.right],
                        addWidth: document.querySelector("button.tl-add").getBoundingClientRect().width };

      // 3. The hover ghost: what a click right there would create.
      hover(trackOf("Look22"), 0.5);
      await wait(60);
      out.ghostMiddle = ghostNow(trackOf("Look22"));
      hover(trackOf("Look22"), 0.5, true);            // Shift: 5 s steps
      await wait(60);
      out.ghostShift = ghostNow(trackOf("Look22"));
      // ...and never on top of an existing cue, nor on a track whose
      // garment has no design to place.
      out.ghostOnEmptyItem = (function () {
        var t = trackOf("Skirt");
        if (!t) return "no Skirt track";
        hover(t, 0.5);
        return ghostNow(t);
      })();

      // 4. The "+" places the first design at 0:00...
      document.querySelector('button.tl-add[data-add="Look22"]').click();
      await wait(500);
      out.firstAppend = cueRow("Look22");
      // ...and the next one exactly one second after the last completes.
      document.querySelector('button.tl-add[data-add="Look22"]').click();
      await wait(500);
      out.secondAppend = cueRow("Look22");
      out.expectedSecond = Math.ceil(+(out.firstAppend[0].complete + APPEND_GAP_S).toFixed(3));
      out.problemsAfterAppend = state.show.cues.reduce((n, c) => n + c.problems.length, 0);

      // 5. The 0:00 block says it is the preset, and the hints are gone.
      out.holds = $$(".cue-hold").map(h => h.textContent.trim());
      out.hintsLeft = $$(".tl-empty").filter(e => e.closest('[data-track="Look22"]')).length;

      // 6. EDIT CUE's own "add next design after this cue".
      ui.cue = state.show.cues.find(c => c.item === "Look22" && c.at <= 0).id;
      ui.editorOpen = true; renderTimeline();
      await wait(120);
      var link = document.querySelector("#cue-append");
      out.appendLink = link ? { text: link.textContent.trim(), title: link.title } : null;
      link.click();
      await wait(500);
      out.afterLink = cueRow("Look22");

      // 7. The time fields read the designers' own mm.ss as well as m:ss,
      //    and the show cannot be set past 99:59.
      out.clocks = ["3:20", "3.20", "3.05", "200", "0.30", "3:75", "", "abc", "1:2:3"]
        .map(s => [s, parseClock(s)]);
      out.maxShow = MAX_SHOW_DURATION_S;
      var dur = document.querySelector("#duration");
      dur.value = "999.00";
      dur.dispatchEvent(new Event("change", { bubbles: true }));
      await wait(500);
      out.clamped = { duration: state.show.duration, toast: toastNow() };
      dur = document.querySelector("#duration");
      dur.value = "8.30";
      dur.dispatchEvent(new Event("change", { bubbles: true }));
      await wait(500);
      out.mmssDuration = state.show.duration;

      // 8. The file buttons are reachable and operable from the keyboard.
      out.fileButtons = [...document.querySelectorAll("label.filebtn")].map(
        b => ({ text: b.textContent.trim().slice(0, 20), tabindex: b.getAttribute("tabindex"),
                role: b.getAttribute("role") }));
      var picked = 0;
      var addCsv = document.querySelector("label.filebtn");
      addCsv.querySelector("input[type=file]").click = function () { picked++; };
      addCsv.focus();
      addCsv.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
      addCsv.dispatchEvent(new KeyboardEvent("keydown", { key: " ", code: "Space", bubbles: true }));
      out.keyboardPicks = picked;

      // 9. A click on empty track space places the design the ghost promised.
      hover(trackOf("Look22"), 0.82);
      await wait(60);
      var promised = ghostNow(trackOf("Look22"));
      var box = trackOf("Look22").getBoundingClientRect();
      trackOf("Look22").dispatchEvent(new MouseEvent("click", { bubbles: true,
        clientX: box.left + box.width * 0.82, clientY: box.top + box.height / 2 }));
      await wait(600);
      out.clickPlaced = { promised: promised,
                          placed: cueRow("Look22").map(c => c.at) };
    } catch (e) { out.error = String((e && e.stack) || e); }
    publish();
  })();
})();
</script>
"""


@pytest.fixture(scope="module")
def tracks(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("tracks")
    _require_browser(tmp)
    stand = _Stand(tmp, _PROBE, designs=2)
    try:
        dom = _dump_dom(stand.url, tmp)
    finally:
        stand.close()
    match = re.search(r'<pre id="tracks-out">(.*?)</pre>', dom or "", re.S)
    assert match, f"no #tracks-out in the dumped DOM:\n{(dom or '')[:3000]}"
    data = json.loads(unescape(match.group(1)))
    assert data.get("error") is None, data["error"]
    return data


def test_every_track_row_offers_its_own_append_button(tracks):
    adds = tracks["empty"]["adds"]
    assert {a["item"] for a in adds} == {"Look22", "Skirt"}, adds
    for add in adds:
        # A real <button>, so it is in the tab order and answers Enter and
        # Space with no handler of its own.
        assert add["tag"] == "BUTTON" and add["type"] == "button", add
        assert "Add the first design (at 0:00)" in add["title"], add


def test_a_track_with_nothing_on_it_says_where_to_click(tracks):
    empty = tracks["empty"]
    assert len(empty["inTrack"]) == 2, empty
    assert all("click here to place the first design at 0:00" == t
               for t in empty["inTrack"]), empty
    # ...and the CUES card, the widest empty space on the screen, says it
    # full size, naming both ways in.
    assert "Nothing placed yet" in empty["firstHint"], empty
    assert "+" in empty["firstHint"], empty


def test_the_ruler_the_tracks_and_the_playhead_still_line_up(tracks):
    a = tracks["alignment"]
    assert abs(a["ruler"][0] - a["track"][0]) < 1, a
    assert abs(a["ruler"][1] - a["track"][1]) < 1, a
    assert abs(a["playhead"][0] - a["track"][0]) < 1, a
    assert abs(a["playhead"][1] - a["track"][1]) < 1, a
    assert a["addWidth"] > 0, a


def test_the_hover_ghost_shows_what_a_click_would_place(tracks):
    ghost = tracks["ghostMiddle"]
    assert ghost, "no ghost was drawn over empty track space"
    assert ghost["label"].startswith("+ "), ghost
    assert " at " in ghost["label"], ghost
    # Whole seconds; with Shift, whole fives.
    assert float(ghost["at"]) == int(float(ghost["at"])), ghost
    assert int(float(tracks["ghostShift"]["at"])) % 5 == 0, tracks["ghostShift"]


def test_no_ghost_on_a_garment_with_no_design_to_place(tracks):
    # A cue with design:null renders as a problem on every later redraw
    # instead of showing anything, so the click is refused - and the ghost
    # must not promise what the click will not do.
    assert tracks["ghostOnEmptyItem"] is None, tracks["ghostOnEmptyItem"]


def test_append_places_the_first_design_at_the_preset(tracks):
    first = tracks["firstAppend"]
    assert len(first) == 1 and first[0]["at"] == 0, first


def test_append_places_the_next_design_one_second_after_the_last_completes(tracks):
    second = tracks["secondAppend"]
    assert len(second) == 2, second
    assert second[1]["at"] == tracks["expectedSecond"], tracks
    # The whole point: the server accepts what the button placed.
    assert tracks["problemsAfterAppend"] == 0, tracks


def test_the_preset_block_says_it_is_the_preset(tracks):
    assert any(h.startswith("PRESET ·") for h in tracks["holds"]), tracks["holds"]
    assert tracks["hintsLeft"] == 0, "the empty-track hint is still on a full track"


def test_edit_cue_offers_the_same_append(tracks):
    link = tracks["appendLink"]
    assert link and "Add next design after this cue" in link["text"], link
    assert re.search(r"\d:\d\d", link["title"]), link
    # It can insert in the MIDDLE of a track - the cue it has open need not
    # be the last one - so the new cue lands right after the preset.
    ats = [c["at"] for c in tracks["afterLink"]]
    assert len(ats) == 3 and ats[1] == tracks["expectedSecond"], tracks["afterLink"]


def test_a_click_places_exactly_what_the_ghost_promised(tracks):
    placed = tracks["clickPlaced"]
    assert float(placed["promised"]["at"]) in placed["placed"], placed


# ------------------------------------------------------------ the polish

def test_a_time_field_reads_the_designers_own_mm_ss(tracks):
    # Their simulator prints and parses mm.ss (SIM.mmss), so a cue sheet
    # copied off their screen has to be typeable here as it stands.
    clocks = dict((k, v) for k, v in tracks["clocks"])
    assert clocks["3:20"] == 200 and clocks["3.20"] == 200
    assert clocks["3.05"] == 185
    assert clocks["0.30"] == 30
    assert clocks["200"] == 200, "a bare number is still seconds on this page"
    # ...and nonsense is still nonsense.
    assert clocks["3:75"] is None and clocks[""] is None
    assert clocks["abc"] is None and clocks["1:2:3"] is None


def test_the_show_cannot_be_set_past_99_59(tracks):
    assert tracks["maxShow"] == 99 * 60 + 59
    clamped = tracks["clamped"]
    assert clamped["duration"] == 99 * 60 + 59, clamped
    # Clamped OUT LOUD: a show length silently different from what was
    # typed is the kind of thing nobody notices until the run-through.
    assert "99:59" in clamped["toast"], clamped
    # A plain mm.ss length still lands where it says.
    assert tracks["mmssDuration"] == 8 * 60 + 30, tracks["mmssDuration"]


def test_the_file_buttons_are_reachable_and_operable_from_the_keyboard(tracks):
    buttons = tracks["fileButtons"]
    assert buttons, "no file buttons on the page at all"
    for button in buttons:
        assert button["tabindex"] == "0", button
        assert button["role"] == "button", button
    # Enter and Space both open the picker - and Space must not also start
    # the show while a file button has the focus.
    assert tracks["keyboardPicks"] == 2, tracks["keyboardPicks"]
