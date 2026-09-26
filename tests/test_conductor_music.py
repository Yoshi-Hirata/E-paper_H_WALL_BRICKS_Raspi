"""THE SHOW's music: the Conductor page plays the operator's track in
lock-step with the fleet's show clock.

Two halves:

* Plain text tests over conductor/web/index.html - the controls exist, the
  preference is wrapped in a try/catch, the Timeline's transport stands down
  while a show owns the audio, and no library was added to do any of it.
* One headless-browser run of the page's own decision layer. index.html marks
  it off between `<<< SHOWMUSIC ... >>>` and `<<< /SHOWMUSIC >>>`; this file
  lifts exactly that text out, drops it into a page of its own and asks it
  every question the show asks it on the night (start at an offset, HOLD,
  RESUME, STOP, a seek, drift, a re-seek storm, autoplay refused, the toggle
  off, no track). Behind CONDUCTOR_BROWSER_TESTS=1, like the simulator's own
  browser tests - and skipped, not failed, where no browser is installed.
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

from tests.test_designer_build import _dump_dom, _require_browser  # noqa: E402

PAGE = INDEX_HTML.read_text(encoding="utf-8")

# The block index.html marks as the pure decision layer. Kept as a regex over
# the real file (never a copy of the source in here) so the tests below can
# only ever describe the page that ships.
_MODULE = re.search(r"<<< SHOWMUSIC:.*?>>>\n(.*?)\n// <<< /SHOWMUSIC >>>", PAGE, re.S)


# --------------------------------------------------------------- the page

def test_the_decision_layer_is_marked_off_for_the_tests():
    assert _MODULE, "the SHOWMUSIC markers are gone from conductor/web/index.html"
    source = _MODULE.group(1)
    assert "SHOWMUSIC" in source and "function plan(" in source
    # Pure means pure: the block must not reach for the page, the fleet, the
    # audio element or a clock of its own - the caller hands all four in.
    # (Its comments name all four, which is why they are stripped first.)
    code = "\n".join(re.sub(r"//.*", "", line) for line in source.splitlines())
    for forbidden in ("document.", "$(", "fleet", "player.", "performance.now(", "setTimeout"):
        assert forbidden not in code, f"the pure layer reaches for {forbidden!r}"


def test_the_show_panel_has_its_own_music_controls():
    # All four controls, in THE SHOW card (musicBlockHtml is rendered there).
    for control in ("show-music-on", "show-music-state", "show-music-join",
                    "show-music-mute", "show-music-vol"):
        assert f'id="{control}"' in PAGE, f"{control} is missing"
    assert "${musicBlockHtml()}" in PAGE, "the music bar is not rendered in THE SHOW card"
    # The toggle is on by default and says what it is.
    assert "Music with THE SHOW" in PAGE
    # The autoplay case names itself in the operator's own words.
    assert "Music: click to join" in PAGE


def test_the_preference_is_per_browser_and_survives_a_localstorage_that_throws():
    for fn in ("loadShowMusicPref", "saveShowMusicPref", "loadMusicVolumePref",
               "loadMusicMutedPref", "saveMusicAudioPrefs"):
        body = re.search(r"function %s\(\) \{(.*?)\n\}" % fn, PAGE, re.S) \
            or re.search(r"function %s\(\) \{(.*?)\}\n" % fn, PAGE, re.S)
        assert body, f"{fn} is gone"
        assert "try {" in body.group(1) and "catch" in body.group(1), \
            f"{fn} does not guard localStorage"
    assert '"show.music"' in PAGE
    # Default ON: anything but an explicit "off" means the music plays.
    assert 'localStorage.getItem("show.music") !== "off"' in PAGE


def test_the_timeline_transport_stands_down_while_a_show_owns_the_audio():
    # One <audio> element for both transports - that is what makes "never two
    # players at once" structural rather than a promise.
    assert PAGE.count("new Audio()") == 1, "a second audio element appeared"
    assert 'const SHOW_OWNS_MUSIC_NOTE = "THE SHOW is playing the music";' in PAGE
    for fn in ("function startPlayback()", "function stopPlayback("):
        start = PAGE.index(fn)
        body = PAGE[start:start + 900]
        assert "showMusicOwns()" in body and "SHOW_OWNS_MUSIC_NOTE" in body, \
            f"{fn} does not refuse while THE SHOW is playing the music"
    # ...and the preview's own scrub does not drag the room's music either.
    assert "playerUrl && !showMusicOwns()" in PAGE


def test_nothing_was_added_to_the_page_from_outside():
    # No libraries: the whole feature is this page's own script.
    assert "<script src=" not in PAGE and "<link rel=\"stylesheet\"" not in PAGE


# ------------------------------------------------------- the browser half

# Every case is `plan()`'s input; the expectations live in Python, below.
def _case(**over):
    base = dict(on=True, hasTrack=True, runState="running", showTime=95.0,
                duration=300.0, trackDuration=300.0, audioPaused=False,
                audioTime=95.0, lag=0.0, blocked=False, nowMs=100000.0, lastSeekMs=0.0)
    base.update(over)
    return base


# nowMs - lastSeekMs is what the rate limits read; these two spell it out.
def _since(ms, **over):
    return _case(nowMs=100000.0, lastSeekMs=100000.0 - ms, **over)


CASES = {
    # START from the seek bar / a remembered start_at: begin mid-track.
    "start_at_offset": _case(audioPaused=True, showTime=95.0),
    "start_from_zero": _case(audioPaused=True, showTime=0.0, audioTime=0.0),
    # In step: leave it alone.
    "in_step": _case(audioTime=95.05),
    # Drift: over the threshold, but not before the rate limit lets go.
    "drift_too_soon": _since(1000, audioTime=95.4),
    "drift_after_the_limit": _since(2100, audioTime=95.4),
    # The threshold itself, to the bit: DRIFT_S away is still "in step".
    "drift_exactly_at_the_threshold": _since(9000, showTime=0.0, audioTime=0.15),
    "drift_just_over_the_threshold": _since(9000, showTime=0.0, audioTime=0.2),
    "drift_behind_as_well_as_ahead": _since(9000, audioTime=94.6),
    # A seek is a move, not drift: it lands at once.
    "seek_jump": _since(500, showTime=200.0, audioTime=20.0),
    "seek_jump_back_to_back": _since(100, showTime=200.0, audioTime=20.0),
    # What a move costs this browser is aimed off, not re-seeked away.
    "start_aims_past_the_elements_own_lag": _case(audioPaused=True, showTime=95.0, lag=-0.2),
    "correction_aims_past_the_lag_too": _since(9000, audioTime=95.4, lag=-0.2),
    # HOLD / RESUME.
    "hold_while_playing": _case(runState="holding", showTime=95.0),
    "hold_already_paused": _case(runState="holding", audioPaused=True),
    "resume_from_the_held_spot": _case(audioPaused=True, showTime=137.0),
    # STOP, and the end of the show.
    "stopped_while_playing": _case(runState=None),
    "stopped_already": _case(runState=None, audioPaused=True),
    "show_ended": _case(showTime=300.0),
    "track_shorter_than_the_show": _case(trackDuration=100.0, showTime=120.0),
    # The START lead counts down with a negative position.
    "lead_in_before_zero": _case(showTime=-2.5, audioPaused=True),
    "lead_in_with_audio_left_running": _case(showTime=-2.5),
    # Autoplay refused after a reload.
    "autoplay_blocked": _case(blocked=True, audioPaused=True),
    "joined_after_the_click": _case(blocked=False, audioPaused=True),
    # The toggle, and no track at all.
    "toggled_off_while_playing": _case(on=False),
    "toggled_off_already_silent": _case(on=False, audioPaused=True),
    "no_track": _case(hasTrack=False, audioPaused=True),
    "no_track_but_audio_running": _case(hasTrack=False),
}

_PROBE = """<!doctype html><meta charset="utf-8"><title>showmusic</title><body>
<script>
"use strict";
%(module)s
var CASES = %(cases)s;
var out = { error: null, constants: null, results: {} };
try {
  out.constants = { DRIFT_S: SHOWMUSIC.DRIFT_S, RESEEK_MS: SHOWMUSIC.RESEEK_MS,
                    JUMP_S: SHOWMUSIC.JUMP_S, JUMP_MS: SHOWMUSIC.JUMP_MS };
  for (var name in CASES) out.results[name] = SHOWMUSIC.plan(CASES[name]);
} catch (e) { out.error = String((e && e.stack) || e); }
var pre = document.createElement("pre");
pre.id = "music-out";
pre.textContent = JSON.stringify(out);
document.body.appendChild(pre);
</script></body>
"""


@pytest.fixture(scope="module")
def plans(tmp_path_factory):
    """One headless run of index.html's own decision layer, on its own."""
    assert _MODULE, "the SHOWMUSIC markers are gone from conductor/web/index.html"
    tmp = tmp_path_factory.mktemp("showmusic")
    _require_browser(tmp)
    page = tmp / "showmusic.html"
    page.write_text(_PROBE % {"module": _MODULE.group(1), "cases": json.dumps(CASES)},
                    encoding="utf-8")
    url = "file:///" + str(page.resolve()).replace("\\", "/")
    dom = _dump_dom(url, tmp)
    match = re.search(r'<pre id="music-out">(.*?)</pre>', dom or "", re.S)
    assert match, f"no #music-out in the dumped DOM:\n{(dom or '')[:3000]}"
    data = json.loads(unescape(match.group(1)))
    assert data["error"] is None, data["error"]
    return data


def test_the_thresholds_are_the_ones_the_spec_asks_for(plans):
    c = plans["constants"]
    assert c["DRIFT_S"] == 0.15, "the drift threshold moved"
    assert c["RESEEK_MS"] == 2000, "the re-seek rate limit moved"
    assert c["JUMP_S"] > c["DRIFT_S"] and c["JUMP_MS"] < c["RESEEK_MS"]


def test_start_begins_at_the_show_position_the_fleet_reports(plans):
    p = plans["results"]["start_at_offset"]
    assert p["action"] == "play" and p["at"] == 95.0, p
    assert p["state"] == "playing" and not p["join"]
    assert plans["results"]["start_from_zero"]["at"] == 0.0


def test_audio_in_step_with_the_show_is_left_alone(plans):
    p = plans["results"]["in_step"]
    assert p["action"] == "none" and p["state"] == "playing", p


def test_drift_is_corrected_but_never_more_often_than_the_rate_limit(plans):
    r = plans["results"]
    assert r["drift_too_soon"]["action"] == "none", r["drift_too_soon"]
    assert r["drift_after_the_limit"]["action"] == "seek", r["drift_after_the_limit"]
    assert r["drift_after_the_limit"]["at"] == 95.0
    # The threshold is a floor, not a ceiling: exactly 0.15 s is still in step.
    assert r["drift_exactly_at_the_threshold"]["action"] == "none"
    assert r["drift_just_over_the_threshold"]["action"] == "seek"
    # Behind the show counts the same as ahead of it.
    assert r["drift_behind_as_well_as_ahead"]["action"] == "seek"


def test_the_audio_is_aimed_past_what_a_move_costs_this_browser(plans):
    # An element that lands 0.2 s behind wherever it is sent is aimed 0.2 s
    # ahead, so it arrives in step - the alternative is a track that plays
    # 0.2 s late all night and is re-seeked (audibly) every 2 s for it.
    r = plans["results"]
    assert r["start_aims_past_the_elements_own_lag"]["at"] == pytest.approx(95.2), \
        r["start_aims_past_the_elements_own_lag"]
    assert r["correction_aims_past_the_lag_too"]["action"] == "seek"
    assert r["correction_aims_past_the_lag_too"]["at"] == pytest.approx(95.2)
    # With nothing measured yet (lag 0) the aim is the show time itself.
    assert plans["results"]["start_at_offset"]["at"] == 95.0


def test_a_seek_jumps_the_audio_instead_of_waiting_out_the_drift_limit(plans):
    r = plans["results"]
    jump = r["seek_jump"]
    assert jump["action"] == "seek" and jump["at"] == 200.0, jump
    # ...but two jumps in the same breath are still spaced, so a browser that
    # answers a seek slowly cannot be driven into a storm.
    assert r["seek_jump_back_to_back"]["action"] == "none"


def test_hold_pauses_and_resume_continues_from_the_show_time(plans):
    r = plans["results"]
    assert r["hold_while_playing"] == {"action": "pause", "at": None,
                                       "state": "paused", "join": False}
    assert r["hold_already_paused"]["action"] == "none"
    assert r["hold_already_paused"]["state"] == "paused"
    resume = r["resume_from_the_held_spot"]
    assert resume["action"] == "play" and resume["at"] == 137.0, resume


def test_stop_and_the_end_of_the_show_stop_the_audio(plans):
    r = plans["results"]
    assert r["stopped_while_playing"]["action"] == "stop"
    assert r["stopped_while_playing"]["state"] == "stopped"
    # Nothing to do twice: a stop is not re-sent every 250 ms.
    assert r["stopped_already"]["action"] == "none"
    assert r["show_ended"]["action"] == "stop"
    assert r["show_ended"]["state"] == "stopped"
    # A track shorter than the show stops where it runs out and says so,
    # instead of being started again at a position it cannot reach.
    assert r["track_shorter_than_the_show"]["action"] == "stop"
    assert r["track_shorter_than_the_show"]["state"] == "track ended"


def test_the_start_lead_does_not_play_the_track_early(plans):
    r = plans["results"]
    assert r["lead_in_before_zero"]["action"] == "none"
    assert r["lead_in_before_zero"]["state"] == "paused"
    assert r["lead_in_with_audio_left_running"]["action"] == "pause"


def test_autoplay_refused_asks_for_one_click_and_retries_nothing(plans):
    blocked = plans["results"]["autoplay_blocked"]
    assert blocked["action"] == "none", "a refused play() must not be retried on a timer"
    assert blocked["state"] == "blocked" and blocked["join"] is True, blocked
    # ...and the click itself (which clears `blocked`) starts the track at the
    # show's position.
    joined = plans["results"]["joined_after_the_click"]
    assert joined["action"] == "play" and joined["at"] == 95.0
    assert joined["join"] is False


def test_the_toggle_off_means_silent_and_no_track_means_nothing_plays(plans):
    r = plans["results"]
    assert r["toggled_off_while_playing"]["action"] == "stop"
    assert r["toggled_off_while_playing"]["state"] == "off"
    assert r["toggled_off_already_silent"]["action"] == "none"
    assert r["no_track"]["action"] == "none"
    assert r["no_track"]["state"] == "no track"
    # "No track" wins over everything, including audio somehow left running.
    assert r["no_track_but_audio_running"]["action"] == "stop"
    assert r["no_track_but_audio_running"]["state"] == "no track"


def test_no_case_ever_asks_the_show_to_move(plans):
    # The fleet's clock is the master: the answer only ever moves the audio.
    for name, p in plans["results"].items():
        assert p["action"] in ("none", "play", "seek", "pause", "stop"), (name, p)
        assert set(p) == {"action", "at", "state", "join"}, (name, p)
