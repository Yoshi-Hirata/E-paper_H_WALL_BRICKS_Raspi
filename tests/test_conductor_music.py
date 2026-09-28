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

import http.server
import io
import json
import math
import re
import socket
import struct
import subprocess
import sys
import threading
import wave
from html import unescape
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
INDEX_HTML = REPO / "conductor" / "web" / "index.html"

sys.path.insert(0, str(REPO))

from conductor.server import Workspace  # noqa: E402
from tests.test_designer_build import _dump_dom, _find_browser, _require_browser  # noqa: E402
from tests.test_look import GRID, MAP  # noqa: E402

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
    code = _strip_comments(source)
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


def _strip_comments(source):
    """Line comments out. Every check below is about what the code does, and
    these comments all quote the very things being looked for."""
    return "\n".join(re.sub(r"//.*", "", line) for line in source.splitlines())


def _function_body(name):
    """The code of one top-level `function name(...) {...}`, comments removed,
    found by counting braces - a regex to the next `\\n}` silently swallowed
    the function after a one-line body, which made these checks pass on
    functions they had never read."""
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


def test_the_preference_is_per_browser_and_survives_a_localstorage_that_throws():
    for fn in ("loadShowMusicPref", "saveShowMusicPref", "loadMusicVolumePref",
               "loadMusicMutedPref", "saveMusicAudioPrefs"):
        body = _function_body(fn)
        assert "localStorage" in body, f"{fn} does not touch localStorage at all"
        assert "try {" in body and "catch" in body, f"{fn} does not guard localStorage"
    assert '"show.music"' in PAGE
    # Default ON: anything but an explicit "off" means the music plays.
    assert 'localStorage.getItem("show.music") !== "off"' in PAGE


def test_a_volume_that_was_never_stored_is_full_not_silent():
    # Number(null) is 0, and 0 passes a plain 0..1 range check - which left a
    # fresh browser with the show AND the preview silent and the slider at the
    # far left (review finding). The behaviour itself is checked in the page,
    # below; this pins the guard that has to come before the Number().
    body = _function_body("loadMusicVolumePref")
    guard = body.index("return 1")
    assert guard < body.index("Number("), \
        "loadMusicVolumePref reaches Number() before it has ruled out a missing value"
    assert "raw === null" in body


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


def test_the_page_still_asks_the_internet_for_nothing():
    # The show PC has no internet on the night (CONDUCTOR_START §3: the
    # network is the units' private router and nothing else), so "no new
    # libraries" is not a style rule - a CDN script, stylesheet, font or
    # image would simply never arrive. Every absolute http(s) reference is
    # caught here, not just <script src=.
    for pattern in (r'\bsrc\s*=\s*"https?:', r'\bhref\s*=\s*"https?:',
                    r"@import", r'url\(\s*["\']?https?:', r'\bimport\s*\(\s*["\']https?:'):
        hit = re.search(pattern, PAGE)
        assert not hit, f"the page fetches from outside: {hit.group(0)!r}"


def test_the_music_never_stops_a_timeline_preview_that_is_running():
    # THE review BLOCKER: with a track loaded and no show running, the 250 ms
    # tick planned "stopped" and applyMusicPlan dutifully paused the element -
    # so ▶ Play died within a quarter of a second, every time. The guard is
    # ui.playing (the preview owns the element then), NOT showMusicOwns():
    # turning the music off DURING a show must still stop the show's audio,
    # and showMusicOwns() would have let that through. Behaviour is checked
    # in the page, below; this pins which guard it is.
    body = _function_body("applyMusicPlan")
    head = body[:body.index('p.action === "seek"')]
    assert "if (ui.playing) return;" in head, \
        "applyMusicPlan no longer stands down for a running Timeline preview"
    assert "showMusicOwns()" not in head, \
        "applyMusicPlan gates on showMusicOwns(), so turning the music off " \
        "mid-show would leave the show's own audio playing"


def test_the_fleet_is_polled_whenever_the_music_could_be_following_a_show():
    # /api/fleet is what tells the page a run exists, so polling only once it
    # already knows of one (showMusicOwns()) meant the Items tab - the tab the
    # page opens on - never found out and the music never joined.
    body = _function_body("musicWantsFleet")
    assert "showMusic.on" in body and "playerUrl" in body
    assert "fleet" not in body, "musicWantsFleet waits for the very thing it is meant to fetch"
    poll = _function_body("pollFleet")
    assert "musicWantsFleet()" in poll and "showMusicOwns()" not in poll


def test_the_music_lets_go_of_the_element_once_the_show_has_run_out():
    # A run that is past its duration is over as far as the music goes (the
    # track stopped at the end), so ▶ Play must not still answer "THE SHOW is
    # playing the music" while nothing is playing at all.
    body = _function_body("showMusicOwns")
    assert "fleetDuration()" in body and "musicShowTime()" in body, \
        "showMusicOwns() owns the element for any run, ended or not"


def test_only_one_conductor_tab_plays_the_track():
    assert "BroadcastChannel" in PAGE, "nothing stops two tabs playing the track twice"
    assert '"another tab"' in PAGE and "another Conductor tab is playing it" in PAGE
    body = _function_body("otherTabHasMusic")
    # The lease lapses, so a closed tab does not silence the others forever...
    assert "MUSIC_LEASE_MS" in body
    # ...and two tabs that start in the same breath settle it the same way in
    # both, so exactly one gives way rather than both or neither.
    assert "MUSIC_TAB_ID < showMusic.otherTabId" in body


# ------------------------------------------------------- the browser half

# Every case is `plan()`'s input; the expectations live in Python, below.
#
# claimPending defaults to True - "this tab has already told the others it is
# taking the track" - because that is the state every case but the very first
# tick of a start is in. The announcement itself gets its own cases.
def _case(**over):
    base = dict(on=True, hasTrack=True, runState="running", showTime=95.0,
                duration=300.0, trackDuration=300.0, audioPaused=False,
                audioTime=95.0, lag=0.0, otherTab=False, claimPending=True,
                blocked=False, nowMs=100000.0, lastSeekMs=0.0)
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
    # 2026-09-28: the real track is 10:54 (654.25 s). A 10:00 show stops it
    # at END with 0:54 still to play; a 15:00 show runs on past its end.
    "track_longer_than_the_show_at_end": _case(trackDuration=654.25, duration=600.0,
                                               showTime=600.0, audioTime=600.0),
    "track_longer_than_the_show_before_end": _case(trackDuration=654.25, duration=600.0,
                                                   showTime=599.0, audioTime=599.0),
    "fifteen_minute_show_after_the_track": _case(trackDuration=654.25, duration=900.0,
                                                 showTime=700.0, audioTime=654.25),
    # The START lead counts down with a negative position.
    "lead_in_before_zero": _case(showTime=-2.5, audioPaused=True),
    "lead_in_with_audio_left_running": _case(showTime=-2.5),
    # ...and the 11 s "Countdown before START" (2026-09-29) is the same
    # negative position, only longer: plan() is untouched by it.
    "countdown_eleven": _case(showTime=-11.0, audioPaused=True),
    "countdown_eleven_audio_running": _case(showTime=-11.0),
    "countdown_last_tenth": _case(showTime=-0.1, audioPaused=True),
    "countdown_reaches_zero": _case(showTime=0.0, audioTime=0.0, audioPaused=True),
    # Autoplay refused after a reload.
    "autoplay_blocked": _case(blocked=True, audioPaused=True),
    "joined_after_the_click": _case(blocked=False, audioPaused=True),
    # The toggle, and no track at all.
    "toggled_off_while_playing": _case(on=False),
    "toggled_off_already_silent": _case(on=False, audioPaused=True),
    "no_track": _case(hasTrack=False, audioPaused=True),
    "no_track_but_audio_running": _case(hasTrack=False),
    # Another Conductor tab on this PC has the track.
    "other_tab_has_it": _case(otherTab=True, audioPaused=True),
    "other_tab_wins_the_tie": _case(otherTab=True),
    # The tick before any sound: say so first, play next time.
    "announce_before_playing": _case(audioPaused=True, claimPending=False),
    "play_once_the_claim_stood": _case(audioPaused=True, claimPending=True),
    # Nothing but a start announces itself - a drift correction on a track
    # this tab is already playing is not a new claim.
    "a_correction_is_not_a_claim": _since(9000, audioTime=95.4, claimPending=False),
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


def test_the_show_clock_is_the_master_at_both_ends_of_the_music(plans):
    r = plans["results"]
    # Music longer than the show: it plays right up to END, and END stops it.
    assert r["track_longer_than_the_show_before_end"]["state"] == "playing"
    assert r["track_longer_than_the_show_at_end"]["action"] == "stop"
    assert r["track_longer_than_the_show_at_end"]["state"] == "stopped"
    # Music shorter than the show: silence after it, never a restart - and
    # nothing here moves the show (the fleet's clock runs on to END).
    assert r["fifteen_minute_show_after_the_track"]["action"] == "stop"
    assert r["fifteen_minute_show_after_the_track"]["state"] == "track ended"


def test_the_start_lead_does_not_play_the_track_early(plans):
    r = plans["results"]
    assert r["lead_in_before_zero"]["action"] == "none"
    assert r["lead_in_before_zero"]["state"] == "paused"
    assert r["lead_in_with_audio_left_running"]["action"] == "pause"


def test_the_eleven_second_countdown_plays_nothing_until_zero(plans):
    # The exact answers plan() gave the 3 s lead before the countdown
    # existed: silent while negative, and the track starts AT 0:00.
    r = plans["results"]
    silent = {"action": "none", "at": None, "state": "paused", "join": False}
    assert r["countdown_eleven"] == silent
    assert r["countdown_last_tenth"] == silent
    assert r["countdown_eleven_audio_running"] == dict(silent, action="pause")
    assert r["countdown_reaches_zero"] == {"action": "play", "at": 0.0,
                                           "state": "playing", "join": False}


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


def test_a_tab_that_does_not_have_the_track_stands_down_and_says_so(plans):
    r = plans["results"]
    assert r["other_tab_has_it"]["state"] == "another tab"
    assert r["other_tab_has_it"]["action"] == "none"
    # Two tabs that both started on the same START: the one that loses the tie
    # does not merely refrain from starting, it stops.
    assert r["other_tab_wins_the_tie"]["action"] == "stop"
    assert r["other_tab_wins_the_tie"]["state"] == "another tab"


def test_a_tab_says_it_is_taking_the_track_before_it_makes_a_sound(plans):
    # Claiming only after play() had been called left two tabs that started on
    # the same START overlapping for a tick - a quarter of a second of doubled
    # audio, which is the very thing the channel is for.
    first = plans["results"]["announce_before_playing"]
    assert first["action"] == "claim", first
    assert first["at"] == 95.0, "the announcement already knows where it will start"
    assert first["state"] == "playing", "the readout should not flicker for one tick"
    # Unchallenged a tick later, it plays - from the same place.
    second = plans["results"]["play_once_the_claim_stood"]
    assert second["action"] == "play" and second["at"] == 95.0, second
    # A correction to a track already playing is not a start; it needs no
    # announcement and must not be delayed by one.
    assert plans["results"]["a_correction_is_not_a_claim"]["action"] == "seek"


def test_no_case_ever_asks_the_show_to_move(plans):
    # The fleet's clock is the master: the answer only ever moves the audio.
    for name, p in plans["results"].items():
        assert p["action"] in ("none", "claim", "play", "seek", "pause", "stop"), (name, p)
        assert set(p) == {"action", "at", "state", "join"}, (name, p)


# ------------------------------------------------- the whole page, running
#
# The half above asks plan() questions. Everything the review actually caught
# lived on the other side of it - in applyMusicPlan(), showMusicOwns(), the
# poll condition and the volume preference - so this half boots the real
# conductor/web/index.html in a headless browser against a small stand-in
# server and watches what the page does.
#
# The stand-in rather than the real conductor: /api/state comes from a real
# Workspace (so the page gets exactly the shape it ships against), but
# /api/fleet has to say "a show just started" on cue, and building a fleet
# with a live run for that would be testing the fleet, not the page. The
# probe steers it through /test/fleet.

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


def _wav(seconds=30, rate=8000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(b"".join(
            struct.pack("<h", int(3000 * math.sin(2 * math.pi * 440 * n / rate)))
            for n in range(rate * seconds)))
    return buf.getvalue()


_RUNS = {
    "none": None,
    "running": {"t0": 0.0, "state": "running", "held_at": None, "force": False, "now": 12.0},
    # The START lead: the fleet counts down to t0 with a negative position.
    "lead": {"t0": 0.0, "state": "running", "held_at": None, "force": False, "now": -4.0},
    # Past the end of a 600 s show: the run is still there (only STOP clears
    # it) but the music is over and must let go of the element.
    "ended": {"t0": 0.0, "state": "running", "held_at": None, "force": False, "now": 601.0},
}


class _Stand:
    """index.html, a real /api/state, and an /api/fleet the probe steers."""

    def __init__(self, tmp_path, probe):
        ws = Workspace(tmp_path / "ws")
        ws.save("Look22_map.csv", MAP)
        ws.save("Look22_color_pattern01_grid.csv", GRID)
        wav = _wav()
        ws.save_music("track.wav", io.BytesIO(wav), len(wav))
        state = ws.state()
        timeline = ws.written_state()
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
                if path == "/api/music/file":
                    return self._send(wav, "audio/wav")
                if path == "/api/fleet":
                    return self._json({
                        "units": [], "last_fire": None, "run": _RUNS[stand.run],
                        "shows": {}, "corrections": [], "prepared": {},
                        "start_at": 0.0, "show_duration": 600.0, "timeline": timeline})
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


# One pass over the page, in order, writing everything it saw into #page-out.
# The waits are the page's own heartbeats: the music ticks at 250 ms and the
# fleet poll runs at 1 s, so nothing here waits less than twice either.
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
  function toastText() { return document.querySelector("#toast").textContent; }
  // "" when the Units tab is not on screen and the bar is not in the DOM.
  function readout() { return (document.querySelector("#show-music-state") || {}).textContent || ""; }
  (async function () {
    try {
      // Boot: refresh() has to have landed before anything below means much.
      // `state`/`fleet` are top-level `let`s, so they live in the global
      // lexical scope this script shares with the page's - and are NOT
      // properties of window, which is what an earlier draft looked for.
      for (var i = 0; i < 200 && state === null; i++) await wait(50);
      out.booted = state !== null && !!playerUrl;

      // 1. A fresh profile has never stored a volume.
      out.freshVolume = player.volume;
      out.freshMuted = player.muted;
      out.toggleDefaultsOn = showMusic.on;

      // 2. The controls are really in THE SHOW card.
      ui.tab = "fleet"; render();
      await wait(400);
      var card = document.querySelector("#content .card");
      out.controls = ["show-music-on", "show-music-state", "show-music-join",
                      "show-music-mute", "show-music-vol"].map(function (id) {
        var el = document.querySelector("#" + id);
        return { id: id, there: !!el, inShowCard: !!el && card.contains(el) };
      });
      out.volSlider = Number((document.querySelector("#show-music-vol") || {}).value);
      out.readoutNoRun = readout();

      // 3. THE BLOCKER: a Timeline preview, with a track loaded and no show
      //    running, must survive the music tick (which plans "stopped" the
      //    whole time). 1.5 s is six ticks, six chances to kill it.
      ui.tab = "timeline"; render();
      await wait(300);
      startPlayback();
      out.previewStarted = ui.playing;
      await wait(1500);
      out.previewAlive = { playing: ui.playing, paused: player.paused, toast: toastText() };
      stopPlayback();
      await wait(300);

      // 4. The fleet poll. On the Items tab (where the page opens) nothing
      //    used to be fetched at all, so a START was never noticed.
      ui.tab = "items"; render();
      showMusic.on = false; fleet = null;
      await wait(2400);
      out.fleetWhileMusicOff = fleet === null;
      showMusic.on = true;
      await wait(2400);
      out.fleetWhileMusicOn = fleet !== null;

      // 5. A show starts while the operator is on the Items tab.
      //    Headless runs on virtual time, so the media clock never actually
      //    advances and player.currentTime reads 0 however it is seeked -
      //    what can be seen, and is the real claim, is where the page PUT
      //    the element. Record every write to currentTime from here on.
      var seeks = [];
      // On HTMLMediaElement.prototype, not the Audio instance's immediate
      // prototype (HTMLAudioElement.prototype), which does not carry it.
      var ct = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, "currentTime");
      Object.defineProperty(player, "currentTime", {
        configurable: true,
        get: function () { return ct.get.call(player); },
        set: function (v) { seeks.push(v); ct.set.call(player, v); }
      });
      await fetch("/test/fleet?run=running");
      await wait(2600);
      out.joined = { tab: ui.tab, state: showMusic.plan && showMusic.plan.state,
                     paused: player.paused, owns: showMusicOwns(),
                     seeks: seeks.slice(), showTime: musicShowTime() };

      // 6. The show runs past its end: the music stops AND lets go, so the
      //    Timeline's transport works again.
      await fetch("/test/fleet?run=ended");
      await wait(2600);
      out.afterTheEnd = { state: showMusic.plan && showMusic.plan.state,
                          paused: player.paused, owns: showMusicOwns() };
      ui.tab = "timeline"; render();
      document.querySelector("#toast").textContent = "";
      await wait(300);
      startPlayback();
      await wait(600);
      out.previewAfterTheEnd = { playing: ui.playing, toast: toastText() };

      // 7. ...but while the show is live the transport is refused.
      stopPlayback();
      await fetch("/test/fleet?run=running");
      await wait(1800);
      document.querySelector("#toast").textContent = "";
      startPlayback();
      out.previewDuringTheShow = { playing: ui.playing, toast: toastText() };

      // 8. A tab behind another window that is PLAYING must keep polling, or
      //    it never hears STOP: it plays on off a frozen clock to the end of
      //    the track while the visible tab, told another tab has it, sits in
      //    silence. document.hidden is faked because a headless dump has no
      //    windows to put in front of each other.
      // Every phase from here starts from a known standstill rather than
      // from whatever the one before it left behind: the fleet poll is 1 s
      // and the tick 250 ms, so "it was already playing" is not something to
      // assume across a phase boundary.
      await fetch("/test/fleet?run=none");
      await wait(2600);
      await fetch("/test/fleet?run=running");
      await wait(2600);
      out.beforeHiding = { paused: player.paused, claimed: showMusic.claimed };
      Object.defineProperty(document, "hidden", { configurable: true, get: function () { return true; } });
      document.dispatchEvent(new Event("visibilitychange"));
      await wait(1200);
      out.hiddenAndPlaying = { paused: player.paused, claimed: showMusic.claimed };
      await fetch("/test/fleet?run=none");           // STOP, from the other tab
      await wait(3000);
      out.hiddenAfterStop = { paused: player.paused, claimed: showMusic.claimed,
                              run: fleet && fleet.run };
      Object.defineProperty(document, "hidden", { configurable: true, get: function () { return false; } });
      document.dispatchEvent(new Event("visibilitychange"));
      await wait(600);

      // 9. The claim goes out BEFORE the first sound, not after it. Both are
      //    timestamped: a tab that plays first and announces afterwards
      //    overlaps another tab for a tick.
      //     Both are timestamped at the call site, in this page.
      //
      //     Nothing here waits on a BroadcastChannel actually delivering.
      //     Two earlier drafts did - watching for the claim to come back
      //     through a second channel object - and both were flaky at about
      //     one run in six: a headless dump runs on virtual time, where the
      //     250 ms between two ticks passes in well under a millisecond of
      //     real time, and the browser's own IPC simply loses that race.
      //     Delivery is Chrome's contract, not this page's; what this page
      //     owes is the order, and the order is measured where it is decided.
      var claimAt = null, playAt = null;
      var realPost = musicBus.postMessage;
      musicBus.postMessage = function (m) {
        if (claimAt === null && m && m.type === "playing") claimAt = performance.now();
        return realPost.apply(musicBus, arguments);
      };
      var realPlay = player.play;
      player.play = function () { if (playAt === null) playAt = performance.now(); return realPlay.apply(player, arguments); };
      await fetch("/test/fleet?run=running");
      await wait(2500);
      out.claimOrder = { claimAt: claimAt, playAt: playAt,
                         gap: claimAt !== null && playAt !== null ? playAt - claimAt : null,
                         claimFirst: claimAt !== null && playAt !== null && claimAt <= playAt };
      musicBus.postMessage = realPost;
      player.play = realPlay;

      // 10. A rival tab with a bigger id already has the track: this one
      //     stands down and never makes a sound at all.
      //
      //     The rival's claim is handed to the page's own channel handler
      //     rather than posted on a second BroadcastChannel, for the reason
      //     above: a posted message arrives when Chrome gets round to it,
      //     which under virtual time may be after the ticks that were
      //     supposed to see it. This is the same object, called with the
      //     same shape the browser would deliver.
      await fetch("/test/fleet?run=none");
      await wait(2600);
      var plays = 0;
      player.play = function () { plays++; return realPlay.apply(player, arguments); };
      var rivalClaim = function () {
        musicBus.onmessage({ data: { type: "playing", id: MUSIC_TAB_ID + "z" } });
      };
      var rival = setInterval(rivalClaim, 200);
      rivalClaim();
      await wait(600);                 // the rival's claim is in place first
      await fetch("/test/fleet?run=running");
      await wait(2600);
      ui.tab = "fleet"; render();               // so the readout is on screen to read
      await wait(400);
      out.beatenByARival = { plays: plays, paused: player.paused,
                             state: showMusic.plan && showMusic.plan.state,
                             readout: readout() };
      ui.tab = "items"; render();
      clearInterval(rival);
      player.play = realPlay;

      // 11. A preview running when a show starts is handed over at the moment
      //     the run appears - during the START countdown, not at 0:00.
      await fetch("/test/fleet?run=none");
      await wait(2600);
      ui.tab = "timeline"; render();
      await wait(300);
      startPlayback();
      await wait(400);
      out.previewBeforeTheStart = { playing: ui.playing, paused: player.paused };
      await fetch("/test/fleet?run=lead");
      await wait(1600);
      out.previewAtTheLeadIn = { playing: ui.playing, paused: player.paused,
                                 owns: showMusicOwns(),
                                 state: showMusic.plan && showMusic.plan.state,
                                 btn: (document.querySelector("#tp-play") || {}).textContent };
      await fetch("/test/fleet?run=running");
      await wait(2600);
      out.afterTheLeadIn = { playing: ui.playing, paused: player.paused,
                             state: showMusic.plan && showMusic.plan.state };
    } catch (e) { out.error = String((e && e.stack) || e); }
    publish();
  })();
})();
</script>
"""


def _dump_dom_with_audio(url, tmp_path):
    """_dump_dom(), plus the two things a page that plays audio needs: leave
    to start without a click, and enough virtual time for the probe's own
    waits and the page's 1 s poll."""
    browser = _find_browser()
    args = [browser, "--headless=new", "--disable-gpu", "--no-sandbox", "--no-first-run",
            "--autoplay-policy=no-user-gesture-required", "--mute-audio",
            f"--user-data-dir={tmp_path / 'user-data'}",
            "--virtual-time-budget=90000", "--dump-dom", url]
    return subprocess.run(args, capture_output=True, timeout=180).stdout.decode(
        "utf-8", errors="replace")


@pytest.fixture(scope="module")
def page(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("musicpage")
    _require_browser(tmp)
    stand = _Stand(tmp, _PAGE_PROBE)
    try:
        dom = _dump_dom_with_audio(stand.url, tmp)
    finally:
        stand.close()
    match = re.search(r'<pre id="page-out">(.*?)</pre>', dom or "", re.S)
    assert match, f"no #page-out in the dumped DOM:\n{(dom or '')[:3000]}"
    data = json.loads(unescape(match.group(1)))
    assert data.get("error") is None, data["error"]
    assert data["booted"], "the page never loaded its state or its track"
    return data


def test_a_fresh_browser_gets_full_volume_and_the_music_on(page):
    # Number(null) is 0 and 0 is a legal volume, which is how a brand new
    # profile ended up with a silent show and the slider at the far left.
    assert page["freshVolume"] == 1, "a fresh profile starts with the volume turned down"
    assert page["freshMuted"] is False
    assert page["toggleDefaultsOn"] is True
    assert page["volSlider"] == 100, "the slider disagrees with the element"


def test_the_music_controls_are_drawn_in_the_show_card(page):
    for control in page["controls"]:
        assert control["there"], f"{control['id']} was not drawn"
        assert control["inShowCard"], f"{control['id']} is not in THE SHOW card"
    assert page["readoutNoRun"] == "stopped", page["readoutNoRun"]


def test_a_timeline_preview_survives_the_music_tick_when_no_show_is_running(page):
    # The review BLOCKER, reproduced: press ▶ Play with a track loaded and no
    # show, and it used to die inside 250 ms because the tick planned
    # "stopped" and applyMusicPlan paused the shared element under it.
    assert page["previewStarted"], "▶ Play did not start at all"
    alive = page["previewAlive"]
    assert alive["playing"] is True, \
        f"the preview was killed by the music tick (toast: {alive['toast']!r})"
    assert alive["paused"] is False, "the element was paused under the running preview"


def test_the_fleet_is_only_polled_off_the_units_tab_when_the_music_wants_it(page):
    # With the music off there is nothing to follow, so nothing is fetched...
    assert page["fleetWhileMusicOff"] is True, \
        "the Items tab polls /api/fleet even with the music switched off"
    # ...and with it on the page keeps up with the fleet wherever the operator
    # happens to be looking, which is what makes a START anywhere audible.
    assert page["fleetWhileMusicOn"] is True, \
        "the Items tab never learns a show exists, so the music can never join"


def test_a_show_that_starts_while_the_operator_is_elsewhere_still_plays(page):
    joined = page["joined"]
    assert joined["tab"] == "items", "the probe drifted off the tab it was testing"
    assert joined["state"] == "playing", joined
    assert joined["paused"] is False, "the track never started"
    assert joined["owns"] is True
    # ...and it was put where the show is, not at 0:00 (the run says 0:12).
    assert joined["seeks"], "the element was played without being placed at all"
    assert joined["seeks"][0] == pytest.approx(joined["showTime"], abs=1.0), joined


def test_the_transport_comes_back_once_the_show_has_run_out(page):
    ended = page["afterTheEnd"]
    assert ended["paused"] is True, "the track kept playing past the end of the show"
    assert ended["state"] == "stopped", ended
    assert ended["owns"] is False, "the music still owns the element after the show ended"
    back = page["previewAfterTheEnd"]
    assert back["playing"] is True, \
        f"▶ Play is still refused after the show ended (toast: {back['toast']!r})"
    assert back["toast"] == "", back["toast"]


def test_the_transport_is_refused_while_the_show_is_live(page):
    live = page["previewDuringTheShow"]
    assert live["playing"] is False, "the preview started on top of the show's music"
    assert live["toast"] == "THE SHOW is playing the music", live["toast"]


def test_the_tab_making_the_sound_keeps_listening_from_behind_a_window(page):
    # A hidden tab used to stop polling (the rule for every other tab, and a
    # good one) - so the one holding the track never heard STOP. It played on
    # off a frozen clock to the end of the track while the operator's visible
    # tab, told that another tab had it, sat there in silence.
    assert page["beforeHiding"] == {"paused": False, "claimed": True}, page["beforeHiding"]
    hidden = page["hiddenAndPlaying"]
    assert hidden["paused"] is False, "going behind a window stopped the music"
    assert hidden["claimed"] is True, "the playing tab gave up the track when hidden"
    stopped = page["hiddenAfterStop"]
    assert stopped["run"] is None, "the hidden tab never refreshed the fleet, so it never saw STOP"
    assert stopped["paused"] is True, "STOP did not reach the hidden tab that was playing"
    assert stopped["claimed"] is False, \
        "the hidden tab is still telling the others it has a track it stopped playing"


def test_the_claim_goes_out_before_the_first_sound(page):
    order = page["claimOrder"]
    assert order["claimAt"] is not None, "nothing was announced on the channel at all"
    assert order["playAt"] is not None, "the track never started"
    assert order["claimFirst"], \
        f"play() ran {-order['gap']:.0f} ms before the claim - two tabs " \
        "starting together would overlap for a tick"
    # ...and a whole tick before it, not in the same breath: the point is to
    # give another tab time to answer before any sound is made.
    assert order["gap"] >= 200, \
        f"the claim and the play were {order['gap']:.0f} ms apart - nothing " \
        "could have answered in between"


def test_a_tab_that_is_beaten_to_the_track_never_makes_a_sound(page):
    beaten = page["beatenByARival"]
    assert beaten["plays"] == 0, \
        "the tab played before it found out another one had the track"
    assert beaten["paused"] is True
    assert beaten["state"] == "another tab", beaten
    assert beaten["readout"] == "another Conductor tab is playing it", beaten["readout"]


def test_a_preview_is_handed_over_when_the_show_starts_not_when_it_reaches_zero(page):
    # The START lead counts down with a negative position, during which the
    # music wants silence - and applyMusicPlan's ui.playing guard (rightly)
    # will not act on that. So the hand-over is its own step, at the moment
    # the run appears; without it the preview played all through the
    # countdown and was yanked at 0:00.
    assert page["previewBeforeTheStart"] == {"playing": True, "paused": False}, \
        page["previewBeforeTheStart"]
    lead = page["previewAtTheLeadIn"]
    assert lead["playing"] is False, "the preview played on through the START countdown"
    assert lead["paused"] is True, "the element was still running during the countdown"
    assert lead["owns"] is True, "the show does not own the element during its own lead-in"
    assert lead["state"] == "paused", lead
    assert lead["btn"] == "▶ Play", f"the transport still reads {lead['btn']!r}"
    # ...and once the show is actually under way, the music comes in.
    after = page["afterTheLeadIn"]
    assert after["state"] == "playing", after
    assert after["paused"] is False, "the show started but the track did not"
    assert after["playing"] is False, "the preview came back from the dead"
