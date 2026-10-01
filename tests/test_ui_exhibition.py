"""EXHIBITION: the menu row and ui/exhibition.py.

The Conductor is replaced by a fake that models just enough of its API
(/api/fleet, /api/show/export, /api/fleet/start, /api/fleet/stop,
/api/loop) - so the whole flow (the row appearing, KEY1 *held* to START
and to STOP, KEY3 *held* for LOOP, the Conductor's refusals on the
screen, KEY2 leaving the run alone, the HAT loop never waiting on HTTP)
is covered without a socket, and without ever touching the production
Conductor on 127.0.0.1:8765.
"""

from __future__ import annotations

import sys
import threading
import time
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ui import render
from ui.app import App, Screen
from ui.config import EVENTS, HEIGHT, WIDTH
from ui.display import NullDisplay
from ui.exhibition import (CONDUCTOR_URL, DONE, FAILED, IDLE, LOST_AFTER_S,
                           LOST_MISSES, MENU_LABEL, MENU_LABEL_NONE, SENDING,
                           Exhibition, format_clock)
from ui.inputs import ScriptedInput
from tests.test_ui_app import FakeRunner
from tests.test_ui_runner import wait_until

UNITS = [f"radxa-0{n}" for n in range(1, 8)]
DURATION = 654.0                # 10:54
COUNTDOWN = 11.0


class FakeConductor:
    """Stands in for the Conductor's HTTP API, as the injectable http
    function. `run` is the run object /api/fleet serves (None: idle);
    `down` refuses every connection; `old` leaves out the loop and
    speaker keys (a Conductor from before Coder Z); `refuse` makes START
    a 400 with that error; `release` holds every request so a test can
    look at the screen while one is in flight. `remote`, when given, is
    the unit's own FakeRemote: a START arms it (the Conductor drives
    this very unit), as on radxa-05.

    Like Coder Z's Conductor, the ENDED run stays in place while LOOP
    waits for the next one (`wait()`), and STOP clears both."""

    def __init__(self, uploaded=True, offline=(), remote=None):
        self.run = None
        self.uploaded = uploaded
        self.offline = set(offline)
        self.remote = remote
        self.loop = {"on": False, "wait_s": 30.0, "next_in_s": None}
        self.speaker = {"available": True, "error": None, "volume": 70,
                        "applied": "bluez"}
        # The Bluetooth side (Coder AC's Conductor), merged into `speaker`
        # unless `no_bluetooth` (a Conductor from before those keys).
        self.device = {"mac": "2C:41:A1:0B:7E:19", "name": "Bose Flex SoundLink",
                       "paired": True, "trusted": True, "connected": True,
                       "sink_present": True,
                       "last_connected_at": time.time() - 12 * 60,
                       "last_error": None}
        self.connection = "connected"
        self.reconnect = {"attempts": 0, "next_in_s": None, "last_error": None}
        self.pairing = None
        self.bluetooth = True           # speaker.bluetooth: False = a wired sink
        self.no_bluetooth = False       # an older Conductor: none of these keys
        self.no_speaker = False         # started without --speaker: speaker null
        self.connect_error = None       # a 200 {"ok": false, "error": ...}
        self.pair_error = None
        self.down = False
        self.old = False
        self.no_volume = False          # a Conductor without /api/speaker/volume
        self.refuse = None
        self.calls = []
        self.release = threading.Event()
        self.release.set()
        self.hold_paths = None          # None: `release` holds every path
        self.preset = None              # the top-level preset object, if any

    def wait(self, next_in_s=25.0):
        """LOOP between runs: the run ended, the next one is pending."""
        self.loop["on"] = True
        self.loop["next_in_s"] = next_in_s
        self.run = {"t0": 0.0, "state": "running", "held_at": None,
                    "now": DURATION + 3.0, "force": False}

    def retrying(self):
        """LOOP's restart is being retried (a unit not ready): the ended
        run stays, `loop.waiting` is set, no next_in_s yet."""
        self.loop["on"] = True
        self.loop["next_in_s"] = None
        self.loop["waiting"] = True
        self.run = {"t0": 0.0, "state": "running", "held_at": None,
                    "now": DURATION + 3.0, "force": False}

    def presetting(self, painted=("radxa-01", "radxa-02", "radxa-03",
                                  "radxa-04", "radxa-05")):
        """START's first stage: the first picture going onto the units."""
        self.run = {"t0": 0.0, "state": "preset", "phase": "preset",
                    "held_at": None, "now": 0.0, "force": False}
        self.preset = {"targets": list(UNITS), "painted": list(painted),
                       "waiting_for": [u for u in UNITS if u not in painted],
                       "elapsed_s": 4.2, "cap_s": 45, "lead_s": COUNTDOWN,
                       "loop": False}

    def posts(self):
        return [(path, body) for method, path, body, _ in self.calls
                if method == "POST"]

    def fleet(self):
        snap = {"units": [{"name": u, "online": u not in self.offline}
                          for u in UNITS],
                "run": None if self.run is None else dict(self.run),
                "shows": ({u: {"id": "abc", "cues": 18} for u in UNITS}
                          if self.uploaded else {}),
                "show_duration": DURATION if self.uploaded else None,
                "start_at": 0.0, "last_fire": None, "corrections": [],
                "prepared": {}, "timeline": {}}
        if self.preset is not None:
            snap["preset"] = dict(self.preset)
        if not self.old:
            snap["loop"] = dict(self.loop)
            snap["speaker"] = dict(self.speaker)
            if self.no_volume:
                snap["speaker"].pop("volume", None)
                snap["speaker"].pop("applied", None)
            if not self.no_bluetooth:
                # (Garbage a test plants - a string for `reconnect` - is
                # served as-is, so the UI side is what has to cope.)
                copy = lambda v: dict(v) if isinstance(v, dict) else v  # noqa: E731
                snap["speaker"].update({
                    "device": copy(self.device), "connection": self.connection,
                    "reconnect": copy(self.reconnect),
                    "pairing": copy(self.pairing), "bluetooth": self.bluetooth})
            if self.no_speaker:
                snap["speaker"] = None
        return snap

    def _pairing_active(self):
        return (isinstance(self.pairing, dict)
                and self.pairing.get("phase") in ("scanning", "pairing", "connecting"))

    def _speaker_command(self, command, body):
        """Coder AC's contract. connect: never refused for a run, 409 only
        while a pairing is in progress (no force), answers "connecting"
        (async - `finish_connect` is the outcome the poll then shows).
        pair: 409 "show running - …" during a run unless forced, 409
        "re-pairing is already in progress" (no force), else the pairing
        starts and the poll shows its phases (`advance_pairing`)."""
        if self.no_bluetooth or self.no_speaker:
            return 404, "not found"
        if command == "connect":
            if self._pairing_active():
                return 409, {"error": "re-pairing is in progress - wait for it"}
            self.connection = "connecting"
            return 200, {"ok": True, "connection": "connecting", "error": None}
        if self._pairing_active():
            return 409, {"error": "re-pairing is already in progress"}
        if self.run is not None and not body.get("force"):
            return 409, {"error": "show running - STOP it first, or force"}
        if self.pair_error:
            self.connection = "disconnected"
            return 200, {"ok": False, "connection": "disconnected",
                         "error": self.pair_error}
        self.pairing = {"phase": "scanning", "note": "looking for the Bose",
                        "started_at": time.time()}
        self.connection = "pairing"
        return 200, {"ok": True, "connection": "pairing", "error": None}

    def finish_connect(self, ok=True, error="Permission denied"):
        """The async connect came to its end: connected, or back to
        disconnected with the error on the device."""
        if ok:
            self.connection = "connected"
            if self.device is None:
                self.device = {"mac": "2C:41:A1:0B:7E:19", "name": "Bose Flex SoundLink",
                               "paired": True, "trusted": True, "connected": True,
                               "sink_present": True, "last_connected_at": time.time(),
                               "last_error": None}
            self.device["connected"] = True
            self.device["last_error"] = None
            self.device["last_connected_at"] = time.time()
            self.reconnect = {"attempts": 0, "next_in_s": None, "last_error": None}
        else:
            self.connection = "disconnected"
            if self.device is not None:
                self.device["connected"] = False
                self.device["last_error"] = error

    def advance_pairing(self, phase, note=None):
        """The Conductor's pairing moved on: pairing… / connecting… /
        done (connected) / failed (back to disconnected)."""
        self.pairing = {"phase": phase, "note": note, "started_at": time.time()}
        if phase == "done":
            self.connection = "connected"
            self.device = {"mac": "2C:41:A1:0B:7E:19", "name": "Bose Flex SoundLink",
                           "paired": True, "trusted": True, "connected": True,
                           "sink_present": True, "last_connected_at": time.time(),
                           "last_error": None}
        elif phase == "failed":
            self.connection = "no_device" if self.device is None else "disconnected"
        else:
            self.connection = "pairing"

    def __call__(self, method, url, body, timeout):
        assert url.startswith(CONDUCTOR_URL)
        path = url[len(CONDUCTOR_URL):]
        self.calls.append((method, path, body, timeout))
        if self.hold_paths is None or path in self.hold_paths:
            self.release.wait(5.0)
        if self.down:
            # What urllib really raises for a port nobody listens on.
            raise urllib.error.URLError(
                ConnectionRefusedError(111, "Connection refused"))
        if method == "GET" and path == "/api/fleet":
            assert timeout <= 1.0                  # the 1 s probe
            return 200, self.fleet()
        if method == "GET" and path == "/api/show/export":
            return 200, {"workspace": "AZ_show_2026", "duration": DURATION,
                         "cues": [{"t": i * 30.0} for i in range(18)],
                         "start_countdown_s": COUNTDOWN}
        if method == "POST" and path == "/api/fleet/start":
            assert body == {}
            if not self.uploaded:
                return 200, {"units": {}, "note": "Nothing uploaded yet - "
                                                  "Upload first."}
            if self.run is not None:
                return 200, {"units": {}, "note": "The show is already running."}
            if self.refuse:
                return 400, {"error": self.refuse}
            self.run = {"t0": 0.0, "state": "running", "held_at": None,
                        "now": -COUNTDOWN, "force": False}
            if self.remote is not None:
                self.remote.active = True       # /show/run armed this unit
            units = {u: ({"ok": True} if u not in self.offline
                         else {"ok": False, "error": "offline"}) for u in UNITS}
            return 200, {"units": units, "lead_s": COUNTDOWN, "from_s": 0.0}
        if method == "POST" and path == "/api/fleet/stop":
            assert body == {}
            self.run = None
            self.preset = None
            self.loop["next_in_s"] = None       # ...and the pending restart
            self.loop["waiting"] = False
            return 200, {"units": {u: {"ok": True} for u in UNITS}}
        if method == "POST" and path == "/api/loop":
            assert isinstance(body.get("on"), bool)
            self.loop["on"] = body["on"]
            self.loop["next_in_s"] = None
            return 200, dict(self.loop)
        if method == "POST" and path == "/api/speaker/volume":
            if self.no_volume:
                return 404, "not found"
            if "delta" in body:
                volume = self.speaker["volume"] + int(body["delta"])
            else:
                volume = int(body["volume"])
            self.speaker["volume"] = max(0, min(100, volume))
            return 200, {"volume": self.speaker["volume"],
                         "applied": self.speaker["applied"], "error": None}
        if method == "POST" and path == "/api/speaker/connect":
            return self._speaker_command("connect", body)
        if method == "POST" and path == "/api/speaker/pair":
            return self._speaker_command("pair", body)
        return 404, "not found"


class FakeRemote:
    """The unit's own RemoteSession, as App reads it: `active` while a
    Conductor (the PC's, or the one on this unit) has it armed;
    release() is what KEY2 on REMOTE does - player.stop on the unit."""

    def __init__(self, active=False):
        self.active = active
        self.busy = None
        self.released = 0

    def owned(self):
        return self.active

    def release(self):
        self.released += 1
        self.active = False

    def status(self):
        return {"active": self.active, "phase": "armed" if self.active else "local",
                "cue": None, "label": "", "error": None, "saved": [],
                "failed": [], "prepare_s": None, "fire_at": None,
                "fired_at": None, "late_ms": None, "verify": None,
                "boards": [1], "live": [1], "boards_source": "cli",
                "absent": [], "group_count": 1, "no_sweep": [],
                "standby_ready": False}


def make_exhibition(**kwargs):
    fake = FakeConductor(**kwargs)
    ex = Exhibition(http=fake, poll_open_s=60.0, poll_idle_s=60.0,
                    echo_log=False)
    return ex, fake


def make_app(ex, locked=False, remote=None):
    runner = FakeRunner()
    app = App(NullDisplay(), ScriptedInput(()), runner, port_label="/dev/fake",
              exhibition=ex, host="radxa-05", locked=locked, remote=remote)
    return app, runner


def enter(app):
    app.select("exhibition")
    app.handle("key1")


def gone(fake, ex):
    """The Conductor service stopped: LOST_MISSES probes in a row go
    unanswered AND LOST_AFTER_S pass, which is what makes
    Exhibition.present() False (one miss does not - review of 73c8fdc,
    HIGH-1; neither alone does - re-review, MED-A). The time is taken
    by moving the first miss back, since most tests run on the real
    clock."""
    fake.down = True
    for _ in range(LOST_MISSES):
        ex.poll()
    assert ex.available is False and ex.present()      # the misses alone: no
    ex.lost_at -= LOST_AFTER_S
    assert not ex.present()


# ---- the row ----

def test_the_row_sits_after_wifi_and_reads_no_conductor_until_one_answers():
    from ui.boardinfo import BoardInfo
    from ui.puller import RepoPuller
    from ui.rebooter import Rebooter
    from ui.wifi import Wifi

    ex, fake = make_exhibition()
    wifi = Wifi(run=lambda a, t: (127, "nmcli not installed"), echo_log=False)
    app = App(NullDisplay(), ScriptedInput(()), FakeRunner(), puller=RepoPuller(),
              rebooter=Rebooter(), wifi=wifi, exhibition=ex,
              boardinfo=BoardInfo(None))
    keys = [p.key for p in app.patterns]
    assert keys[-5:] == ["pull", "reboot", "wifi", "exhibition", "boardinfo"], keys
    row = app.patterns[-2]
    # Nothing asked yet: the row is the "no conductor" one, and the menu's
    # redraw key sees the label flip when the cache lands.
    assert row.label == MENU_LABEL_NONE == "EXHIBITION  (no conductor)"
    before = app._display_key()
    ex.poll()
    assert ex.available is True
    assert row.label == MENU_LABEL == "EXHIBITION"
    assert app._display_key() != before
    fake.down = True
    ex.poll()
    assert ex.available is False and row.label == MENU_LABEL_NONE
    assert "Connection refused" in ex.fleet_error
    plain = App(NullDisplay(), ScriptedInput(()), FakeRunner())
    assert "exhibition" not in [p.key for p in plain.patterns]


def test_the_reader_thread_polls_faster_while_the_screen_is_open():
    fake = FakeConductor()
    ex = Exhibition(http=fake, poll_open_s=0.05, poll_idle_s=60.0,
                    echo_log=False)
    try:
        ex.start_reader()
        assert wait_until(lambda: ex.available is True)
        first = len(fake.calls)
        time.sleep(0.2)
        assert len(fake.calls) == first          # idle: the slow poll
        ex.open()                                # the screen: fast poll
        assert wait_until(lambda: len(fake.calls) >= first + 4, timeout=2.0)
        # Only GETs, and the show's name once.
        assert fake.posts() == []
        assert [c[1] for c in fake.calls].count("/api/show/export") == 1
        assert ex.show == {"name": "AZ_show_2026", "cues": 18,
                           "duration": DURATION}
    finally:
        ex.shutdown()


def test_without_a_conductor_the_screen_is_a_note_and_nothing_is_sent():
    ex, fake = make_exhibition()
    fake.down = True
    ex.poll()
    app, _ = make_app(ex)
    enter(app)
    assert app.screen is Screen.EXHIBITION
    for event in ("key1", "key1_hold", "key3_hold", "up", "down", "press"):
        app.handle(event)
    ex.join(0.2)
    assert fake.posts() == []
    assert ex.status_text().startswith("no conductor: ")
    app.handle("key2")
    assert app.screen is Screen.MENU and not ex.is_open


# ---- what the screen says ----

def test_the_texts_for_every_state():
    ex, fake = make_exhibition()
    ex.open()
    ex.poll()
    assert ex.show_lines() == ("AZ_show_2026", "18 cues · 10:54")
    assert ex.run_text() == "idle"
    assert ex.fleet_text() == "units 7/7 online"
    assert ex.loop_text() == "LOOP off" and ex.loop_on() is False
    assert ex.speaker_text() == "speaker ok · vol 70% (bluez)"
    assert ex.speaker_available() and ex.volume_supported()
    assert ex.active is False and ex.status_text() == ""

    fake.run = {"t0": 0.0, "state": "running", "now": -10.6}
    fake.offline = {"radxa-03"}
    ex.poll()
    assert ex.run_text() == "countdown -0:11"
    assert ex.fleet_text() == "units 6/7 online"
    assert ex.active is True
    fake.run = {"t0": 0.0, "state": "running", "now": 200.4}
    ex.poll()
    assert ex.run_text() == "3:20 / 10:54 running"
    fake.run = {"t0": 0.0, "state": "holding", "held_at": 1.0, "now": 200.4}
    ex.poll()
    assert ex.run_text() == "hold 3:20 / 10:54"
    fake.run = {"t0": 0.0, "state": "running", "now": 700.0}
    ex.poll()
    assert ex.run_text() == "ended 10:54 / 10:54"

    # LOOP between runs: the Conductor keeps the ended run in place and
    # says when the next one starts - the wait wins over "ended".
    fake.wait(25.0)
    fake.speaker = {"available": False, "error": "no ALSA playback device\n"
                                                 "second line"}
    ex.poll()
    assert fake.run is not None
    assert ex.run_text() == "next run in 0:25"
    assert ex.active is True and ex.waiting is True
    assert ex.loop_text() == "LOOP on"
    assert ex.speaker_text() == "no speaker - no ALSA playback device"
    fake.run = None                              # ...and with no run at all
    ex.poll()
    assert ex.run_text() == "next run in 0:25" and ex.active is True
    fake.loop["next_in_s"] = None
    ex.poll()
    assert ex.run_text() == "idle" and ex.active is False and not ex.waiting

    # A Conductor from before loop and speaker existed.
    fake.old = True
    fake.loop["on"] = True
    fake.loop["next_in_s"] = 25.0
    ex.poll()
    assert ex.loop_text() == "loop ?" and ex.loop_on() is None
    assert ex.speaker_text() == "speaker ?"
    assert ex.run_text() == "idle"

    fake.uploaded = False
    ex.poll()
    assert ex.show_lines() == ("AZ_show_2026", "18 cues · 10:54 · not uploaded")
    assert format_clock(0) == "0:00" and format_clock(654) == "10:54"


def test_a_running_clock_moves_between_polls():
    clock = [100.0]
    fake = FakeConductor()
    ex = Exhibition(http=fake, clock=lambda: clock[0], echo_log=False)
    fake.run = {"t0": 0.0, "state": "running", "now": 10.0}
    ex.poll()
    assert ex.run_text() == "0:10 / 10:54 running"
    clock[0] += 4.0
    assert ex.run_text() == "0:14 / 10:54 running"
    fake.run["state"] = "holding"
    ex.poll()
    clock[0] += 4.0
    assert ex.run_text() == "hold 0:10 / 10:54"     # held: stands still
    fake.wait(5.0)
    ex.poll()
    clock[0] += 3.0
    assert ex.run_text() == "next run in 0:02"
    clock[0] += 10.0
    assert ex.run_text() == "next run in 0:00"


# ---- the keys ----

def test_only_a_held_key1_starts_and_the_answer_is_shown():
    ex, fake = make_exhibition()
    ex.poll()
    app, runner = make_app(ex)
    enter(app)
    assert app.screen is Screen.EXHIBITION and ex.is_open
    assert runner.stops == 0                    # the runner keeps the port
    for event in ("key1", "press", "up", "down", "key3"):
        app.handle(event)
    app.blanked = False
    ex.join(0.2)
    assert fake.posts() == []                   # nothing started
    app.handle("key1_hold")
    assert wait_until(lambda: ex.phase == DONE)
    assert fake.posts() == [("/api/fleet/start", {})]
    assert ex.note == "START in 11 s · 7/7 units"
    assert ex.status_text() == ex.note
    assert ex.active is True                    # the poll after the answer
    assert ex.run_text() == "countdown -0:11"
    assert fake.run is not None
    # Moving reads the verdict away; the run stays.
    app.handle("down")
    assert ex.phase == IDLE and ex.status_text() == ""
    assert fake.run is not None


def test_a_held_key1_stops_while_a_run_or_its_countdown_exists():
    ex, fake = make_exhibition()
    fake.run = {"t0": 0.0, "state": "running", "now": -8.0}
    ex.poll()
    app, _ = make_app(ex)
    enter(app)
    assert ex.active
    app.handle("key1_hold")
    assert wait_until(lambda: ex.phase == DONE)
    assert fake.posts() == [("/api/fleet/stop", {})]
    assert ex.note == "STOP · 7/7 units"
    assert fake.run is None and ex.active is False
    assert ex.run_text() == "idle"
    # ...and now a hold starts again.
    app.handle("up")
    app.handle("key1_hold")
    assert wait_until(lambda: ex.phase == DONE)
    assert fake.posts()[-1] == ("/api/fleet/start", {})


def test_a_held_key3_toggles_loop_and_a_plain_key3_blanks():
    ex, fake = make_exhibition()
    ex.poll()
    app, _ = make_app(ex)
    enter(app)
    app.handle("key3_hold")
    assert wait_until(lambda: ex.phase == DONE)
    assert fake.posts() == [("/api/loop", {"on": True})]
    assert ex.note == "LOOP on" and ex.loop_text() == "LOOP on"
    assert fake.loop["on"] is True
    app.handle("key3_hold")
    assert wait_until(lambda: len(fake.posts()) == 2)
    assert wait_until(lambda: ex.phase == DONE and ex.note == "LOOP off")
    assert fake.posts()[-1] == ("/api/loop", {"on": False})
    assert ex.loop_text() == "LOOP off"
    assert app.screen is Screen.EXHIBITION and not app.blanked
    # A plain KEY3 is still "screen off", here as everywhere.
    app.handle("key3")
    assert app.blanked
    ex.join(0.2)
    assert len(fake.posts()) == 2
    # An unknown flag (older Conductor) is turned on.
    fake.old = True
    fake.loop["on"] = True
    ex.poll()
    assert ex.loop_on() is None
    ex.toggle_loop()
    assert wait_until(lambda: len(fake.posts()) == 3)
    assert fake.posts()[-1] == ("/api/loop", {"on": True})


def test_key3_held_anywhere_else_blanks_like_a_plain_key3():
    ex, _ = make_exhibition()
    ex.poll()
    app, _ = make_app(ex)
    assert app.screen is Screen.MENU
    app.handle("key3_hold")
    assert app.blanked
    app.handle("key3_hold")                     # any press wakes, nothing else
    assert not app.blanked and app.screen is Screen.MENU
    assert "key3_hold" in EVENTS and "key1_hold" in EVENTS


def test_refusals_show_the_conductors_words_first_line_only():
    ex, fake = make_exhibition(uploaded=False)
    ex.poll()
    app, _ = make_app(ex)
    enter(app)
    app.handle("key1_hold")
    assert wait_until(lambda: ex.phase == DONE)
    # A 200 with a note is the Conductor's soft refusal, verbatim.
    assert ex.note == "Nothing uploaded yet - Upload first."
    assert fake.run is None

    fake.uploaded = True
    fake.refuse = ("every unit holds an older upload than the timeline on "
                   "screen - Upload again before the show\nmore detail")
    app.handle("up")
    app.handle("key1_hold")
    assert wait_until(lambda: ex.phase == FAILED)
    assert ex.note == ("ERROR every unit holds an older upload than the "
                       "timeline on screen - Upload again before the show")
    assert any(line.endswith("before the show") for line in ex.recent(5))

    fake.refuse = None
    fake.run = {"t0": 0.0, "state": "running", "now": 5.0}
    ex.poll()
    fake.run = None                              # gone between poll and press
    fake.down = True
    app.handle("up")
    app.handle("key1_hold")                      # STOP, to a dead Conductor
    assert wait_until(lambda: ex.phase == FAILED)
    # urllib's wrapper unwrapped: the socket's own words, nothing else.
    assert ex.note == "ERROR Connection refused"
    assert fake.posts()[-1] == ("/api/fleet/stop", {})
    assert ex.available is False                 # the poll after it saw that
    assert ex.status_text() == ex.note
    assert ex.fleet_error == "Connection refused"

    # A unit that refused the START is named.
    fake.down = False
    fake.offline = {"radxa-04"}
    ex.poll()
    app.handle("up")
    app.handle("key1_hold")
    assert wait_until(lambda: ex.phase == DONE)
    assert ex.note == "START in 11 s · 6/7 units - radxa-04: offline"


def test_key2_goes_back_and_leaves_the_run_to_the_conductor():
    ex, fake = make_exhibition()
    fake.run = {"t0": 0.0, "state": "running", "now": 42.0}
    ex.poll()
    app, _ = make_app(ex)
    enter(app)
    assert ex.is_open
    app.handle("key2")
    assert app.screen is Screen.MENU
    assert not ex.is_open
    assert fake.posts() == []                    # no STOP, no anything
    assert fake.run is not None
    # KEY2 during a command: back, the command completes on its own.
    enter(app)
    fake.release.clear()
    app.handle("key1_hold")                      # STOP
    assert ex.busy and ex.phase == SENDING
    assert ex.status_text() == "sending…"
    for event in ("up", "key1_hold", "key3_hold", "key1"):
        app.handle(event)
    assert len(fake.posts()) == 1                # nothing piled on
    app.handle("key2")
    assert app.screen is Screen.MENU and ex.busy
    fake.release.set()
    assert wait_until(lambda: ex.phase == DONE)
    assert fake.run is None


def test_a_held_key1_stops_the_loops_pending_restart_too():
    ex, fake = make_exhibition()
    fake.wait(25.0)
    ex.poll()
    app, _ = make_app(ex)
    enter(app)
    assert ex.run_text() == "next run in 0:25" and ex.active
    app.draw()                                   # the STOP hint renders
    app.handle("key1_hold")
    assert wait_until(lambda: ex.phase == DONE)
    assert fake.posts() == [("/api/fleet/stop", {})]
    assert fake.run is None and fake.loop["next_in_s"] is None
    assert ex.run_text() == "idle" and not ex.active


def test_the_preset_stage_shows_who_painted_and_a_held_key1_stops_it():
    ex, fake = make_exhibition()
    fake.presetting()
    ex.poll()
    assert ex.run_text() == "preset… 5/7 painted"
    assert ex.active is True and not ex.waiting
    app, _ = make_app(ex)
    enter(app)
    app.draw()                                   # amber, the STOP hint
    fake.presetting(painted=UNITS)
    ex.poll()
    assert ex.run_text() == "preset… 7/7 painted"
    # No preset object (a Conductor between versions): still the stage.
    fake.preset = None
    ex.poll()
    assert ex.run_text() == "preset…" and ex.active
    fake.presetting()
    ex.poll()
    app.handle("key1_hold")
    assert wait_until(lambda: ex.phase == DONE)
    assert fake.posts() == [("/api/fleet/stop", {})]
    assert fake.run is None and fake.preset is None
    assert ex.run_text() == "idle" and not ex.active
    # The stage flows on: preset -> countdown -> running.
    fake.run = {"t0": 0.0, "state": "running", "now": -COUNTDOWN}
    ex.poll()
    assert ex.run_text() == "countdown -0:11"


def test_a_loop_restart_being_retried_reads_loop_waiting():
    ex, fake = make_exhibition()
    fake.retrying()
    ex.poll()
    assert fake.run is not None
    assert ex.run_text() == "loop waiting"       # not "ended 10:54 / 10:54"
    assert ex.active is True and ex.waiting is True and ex.loop_waiting()
    assert ex.loop_text() == "LOOP on"
    app, _ = make_app(ex)
    enter(app)
    app.draw()                                   # amber, "(loop waiting)" hint
    # The pending restart found its moment: next_in_s wins the line.
    fake.loop["next_in_s"] = 12.0
    ex.poll()
    assert ex.run_text() == "next run in 0:12"
    fake.retrying()
    ex.poll()
    app.handle("key1_hold")                      # STOP ends the retrying too
    assert wait_until(lambda: ex.phase == DONE)
    assert fake.posts() == [("/api/fleet/stop", {})]
    assert ex.run_text() == "idle" and not ex.active and not ex.loop_waiting()
    # `waiting` with the loop off means nothing.
    fake.loop["waiting"] = True
    fake.loop["on"] = False
    ex.poll()
    assert ex.run_text() == "idle" and not ex.waiting


# ---- the unit that IS the Conductor (HIGH-1) ----

def test_start_arms_this_units_own_session_and_the_screen_stays_exhibition():
    # radxa-05: fleet.json points the local Conductor at 127.0.0.1:8787,
    # so START arms this unit's own RemoteSession. The follow used to
    # flip every screen to REMOTE - where KEY1 held cannot STOP and KEY2
    # is release() = player.stop, this unit dropping out of its own run.
    remote = FakeRemote()
    ex, fake = make_exhibition(remote=remote)
    ex.poll()
    app, _ = make_app(ex, remote=remote)
    enter(app)
    app.handle("key1_hold")                      # START
    assert wait_until(lambda: ex.phase == DONE)
    assert remote.active is True
    for _ in range(3):
        app.tick(wait=0.0)
    assert app.screen is Screen.EXHIBITION       # never left by the follow
    assert ex.active and ex.run_text() == "countdown -0:11"
    app.handle("key1_hold")                      # ...so STOP is possible
    assert wait_until(lambda: fake.run is None)
    assert wait_until(lambda: ex.phase == DONE)
    assert remote.released == 0
    # KEY2 leaves the run alone (the session stays armed all day on the
    # real unit) and the follow does not drag the menu back.
    fake.run = {"t0": 0.0, "state": "running", "now": 12.0}
    ex.poll()
    app.handle("key2")
    assert app.screen is Screen.MENU and remote.released == 0
    for _ in range(3):
        app.tick(wait=0.0)
    assert app.screen is Screen.MENU
    assert remote.active is True
    # The row is still there and opens again...
    enter(app)
    assert app.screen is Screen.EXHIBITION
    app.handle("key2")
    # ...and once the session is really let go of, the follow is back
    # to normal (nothing to follow, the menu stays).
    remote.active = False
    app.tick(wait=0.0)
    assert app.screen is Screen.MENU
    assert app._remote_dismissed is False


def test_the_follow_shows_exhibition_not_remote_where_the_conductor_is_local():
    remote = FakeRemote()
    ex, fake = make_exhibition(remote=remote)
    ex.poll()
    app, _ = make_app(ex, remote=remote)
    assert app.screen is Screen.MENU
    fake.run = {"t0": 0.0, "state": "running", "now": 12.0}
    ex.poll()
    remote.active = True                         # the local Conductor armed it
    app.tick(wait=0.0)
    assert app.screen is Screen.EXHIBITION and ex.is_open
    assert remote.released == 0
    # A REMOTE screen reached before the Conductor was known (or with
    # the PC driving): KEY2 must not release() while the local run is on.
    app.screen = Screen.REMOTE
    app.handle("key2")
    assert app.screen is Screen.MENU and remote.released == 0
    app.tick(wait=0.0)
    assert app.screen is Screen.MENU             # dismissed, not bounced
    # No local run any more - but the Conductor is still here, and its
    # cache may be 30 s old: KEY2 on REMOTE still does not release.
    app._remote_dismissed = False
    fake.run = None
    ex.poll()
    app.screen = Screen.REMOTE
    app.handle("key2")
    assert remote.released == 0 and remote.active is True
    # Only with no Conductor on this unit does KEY2 release, as it
    # always did on the garment units - and one missed probe is not
    # "no Conductor" (HIGH-1): it takes LOST_MISSES in a row.
    fake.down = True
    ex.poll()
    app.screen = Screen.REMOTE
    app.handle("key2")
    assert remote.released == 0 and remote.active is True
    gone(fake, ex)
    app.screen = Screen.REMOTE
    app.handle("key2")
    assert remote.released == 1 and remote.active is False


def test_without_a_local_conductor_the_follow_is_unchanged():
    remote = FakeRemote()
    ex, fake = make_exhibition()
    fake.down = True
    ex.poll()
    app, _ = make_app(ex, remote=remote)
    remote.active = True                         # the show PC took the unit
    app.tick(wait=0.0)
    assert app.screen is Screen.REMOTE
    app.handle("key2")
    assert app.screen is Screen.MENU and remote.released == 1
    # The "(no conductor)" note is followed like any other screen on a
    # garment unit: the PC arming it shows REMOTE (byte-for-byte the old
    # behaviour on the nine units - review of 13c8dcc, LOW-2).
    enter(app)
    assert app.screen is Screen.EXHIBITION
    remote.active = True
    app.tick(wait=0.0)
    assert app.screen is Screen.REMOTE
    app.handle("key2")
    assert app.screen is Screen.MENU and remote.released == 2


def test_a_session_armed_before_the_first_probe_moves_to_exhibition_once_known():
    # A UI restart on radxa-05: the Conductor arms this unit before the
    # reader has answered once (available is None), so the follow shows
    # REMOTE; the moment the Conductor is known the screen moves over,
    # and KEY2 on that REMOTE never released (review of 13c8dcc, MED-1).
    remote = FakeRemote(active=True)
    ex, fake = make_exhibition(remote=remote)
    fake.run = {"t0": 0.0, "state": "running", "now": 12.0}
    app, _ = make_app(ex, remote=remote)
    assert ex.available is None
    app.tick(wait=0.0)
    assert app.screen is Screen.REMOTE
    ex.poll()                                    # the reader answers
    app.tick(wait=0.0)
    assert app.screen is Screen.EXHIBITION and ex.is_open
    assert remote.released == 0
    # The same from REMOTE with KEY2 pressed in the meantime: the
    # Conductor's presence decides, not its (up to 30 s old) run.
    app.screen = Screen.REMOTE
    fake.run = None
    ex.poll()
    app.handle("key2")
    assert app.screen is Screen.MENU and remote.released == 0
    app.tick(wait=0.0)
    assert app.screen is Screen.MENU             # dismissed
    # The Conductor service stopped: REMOTE and its release() come back
    # - after LOST_MISSES unanswered probes, not on the first (HIGH-1).
    fake.down = True
    ex.poll()
    app.tick(wait=0.0)
    assert app._remote_dismissed is True and app.screen is Screen.MENU
    gone(fake, ex)
    app.tick(wait=0.0)
    assert app._remote_dismissed is False
    assert app.screen is Screen.REMOTE
    app.handle("key2")
    assert remote.released == 1


def test_one_missed_probe_does_not_move_exhibition_to_remote():
    # A timed-out probe used to flip the screen to REMOTE for a tick -
    # where KEY2 is release() = this unit out of its own run - and back
    # to EXHIBITION, where the same KEY1 hold is STOP (review of
    # 73c8fdc, HIGH-1). The Conductor counts as present until BOTH
    # LOST_MISSES probes in a row went unanswered AND LOST_AFTER_S
    # passed since the first miss (MED-A: a Conductor restart watched
    # from SPEAKER's 2 s polls still gets its 15 s).
    clock = [1000.0]
    remote = FakeRemote()
    fake = FakeConductor(remote=remote)
    ex = Exhibition(http=fake, clock=lambda: clock[0], poll_open_s=60.0,
                    poll_idle_s=60.0, echo_log=False)
    fake.run = {"t0": 0.0, "state": "running", "now": 12.0}
    ex.poll()
    app, _ = make_app(ex, remote=remote)
    remote.active = True
    app.tick(wait=0.0)
    assert app.screen is Screen.EXHIBITION
    assert ex.poll_interval() == 60.0            # the screen's own pace here
    fake.down = True
    for _ in range(LOST_MISSES - 1):
        ex.poll()                                # misses, short of the rule
        clock[0] += 1.0
        app.tick(wait=0.0)
        assert ex.present() and app._local_conductor()
        assert app.screen is Screen.EXHIBITION
        assert app._remote_dismissed is False
    app.handle("key2")                           # back out meanwhile...
    assert app.screen is Screen.MENU and remote.released == 0
    assert app._remote_dismissed is True         # ...dismissed, not released
    # While a miss stands the reader keeps the 5 s pace, screen or no
    # screen, so the return is seen inside the grace (MED-B).
    assert not ex.is_open and ex.lost_at is not None
    assert ex.poll_interval() == ex.poll_open_s
    fake.down = False
    ex.poll()                                    # back: nothing happened
    assert ex.misses == 0 and ex.lost_at is None and ex.present()
    assert ex.poll_interval() == ex.poll_idle_s
    app.tick(wait=0.0)
    assert app.screen is Screen.MENU and remote.released == 0
    enter(app)
    assert app.screen is Screen.EXHIBITION
    # Three misses in a row within the 15 s (SPEAKER's 2 s polls do that
    # in 6 s): still present - the time has to pass too (MED-A)...
    fake.down = True
    for _ in range(LOST_MISSES + 2):
        ex.poll()
        clock[0] += 2.0
    assert ex.misses == LOST_MISSES + 2 and ex.present()
    app.tick(wait=0.0)
    assert app.screen is Screen.EXHIBITION
    # ...and the time alone is not enough either: one miss, 15 s.
    fake.down = False
    ex.poll()
    fake.down = True
    ex.poll()
    clock[0] += LOST_AFTER_S + 1.0
    assert ex.misses == 1 and ex.present()
    app.tick(wait=0.0)
    assert app.screen is Screen.EXHIBITION
    # Both: really gone.
    for _ in range(LOST_MISSES - 1):
        ex.poll()
    assert ex.misses == LOST_MISSES and not ex.present()
    app.tick(wait=0.0)
    assert app.screen is Screen.REMOTE           # the garment-unit rule again
    # Never answered at all (a garment unit): not present from the
    # start, and the reader's pace is the idle one (lost_at never set).
    fresh = Exhibition(http=fake, echo_log=False)
    fresh.poll()
    assert fresh.available is False and not fresh.present()
    assert fresh.lost_at is None and fresh.poll_interval() == fresh.poll_idle_s


def test_the_menu_refuses_the_port_taking_rows_while_the_session_is_armed():
    # radxa-05 sits on the menu with its own Conductor's session armed
    # (KEY2 dismissed the screen): STANDBY, a pattern, FW VERSION and
    # UPDATE FW must not take the port from under the run (review of
    # 13c8dcc, HIGH-2). WIFI / EXHIBITION / BOARD INFO / GIT PULL /
    # REBOOT stay as they are.
    from ui.boardinfo import BoardInfo
    from ui.puller import RepoPuller
    from ui.rebooter import Rebooter
    from ui.updater import MenuEntry

    class FakeWorker:
        """UPDATE FW / FW VERSION as the App opens them: a scan on entry."""

        def __init__(self, key, label):
            self.menu_entry = MenuEntry(key, label, "")
            self.busy = False
            self.scans = 0

        def reset(self):
            pass

        def scan(self):
            self.scans += 1

    remote = FakeRemote(active=True)
    ex, fake = make_exhibition(remote=remote)
    fake.run = {"t0": 0.0, "state": "running", "now": 12.0}
    ex.poll()
    runner = FakeRunner()
    updater = FakeWorker("update", "UPDATE FW")
    versions = FakeWorker("versions", "FW VERSION")
    app = App(NullDisplay(), ScriptedInput(()), runner, port_label="/dev/fake",
              exhibition=ex, remote=remote, host="radxa-05", updater=updater,
              versions=versions, puller=RepoPuller(), rebooter=Rebooter(),
              boardinfo=BoardInfo(None))
    app._remote_dismissed = True
    app.screen = Screen.MENU
    pattern = next(p.key for p in app.patterns
                   if p.key not in ("standby", "update", "versions", "pull",
                                    "reboot", "wifi", "exhibition", "boardinfo"))
    for key in ("standby", pattern, "versions", "update"):
        app.select(key)
        app.handle("key1")
        assert app.screen is Screen.MENU, key
        assert app._standby_status() == "conductor holds this unit - see EXHIBITION"
        app.handle("key1_hold")                  # the hold starts a row too
        assert app.screen is Screen.MENU, key
    assert runner.standbys == 0 and runner.starts == [] and runner.stops == 0
    assert updater.scans == 0 and versions.scans == 0
    for key, screen in (("pull", Screen.PULL), ("reboot", Screen.REBOOT),
                        ("boardinfo", Screen.BOARDINFO),
                        ("exhibition", Screen.EXHIBITION)):
        app.select(key)
        app.handle("key1")
        assert app.screen is screen, key
        app.handle("key2")
    assert runner.stops == 0
    # Without a local Conductor the words name the PC...
    gone(fake, ex)
    app._remote_dismissed = True
    app.select("standby")
    app.handle("key1")
    assert app._standby_status() == "PC holds this unit - release it on the PC"
    assert runner.standbys == 0
    # ...and once the session is released the rows work again.
    remote.active = False
    app.select("standby")
    app.handle("key1")
    assert runner.standbys == 1


def test_a_command_thread_that_cannot_start_is_a_verdict():
    ex, fake = make_exhibition()
    ex.poll()
    original = threading.Thread.start

    def refuse(self):
        raise RuntimeError("can't start new thread")
    threading.Thread.start = refuse
    try:
        ex.start()
    finally:
        threading.Thread.start = original
    assert ex.phase == FAILED and not ex.busy
    assert ex.note == "ERROR could not start: can't start new thread"
    assert fake.posts() == []


def test_urllib_errors_read_as_their_reason():
    from ui.exhibition import _why

    assert _why(urllib.error.URLError(
        ConnectionRefusedError(111, "Connection refused"))) == "Connection refused"
    assert _why(urllib.error.URLError("timed out")) == "timed out"
    assert _why(TimeoutError("timed out")) == "timed out"
    assert _why(OSError(113, "No route to host")) == "No route to host"
    assert _why(ValueError()) == "ValueError"


# ---- the speaker's volume (LEFT / RIGHT) ----

def test_left_and_right_send_volume_deltas_and_the_line_follows():
    ex, fake = make_exhibition()
    ex.poll()
    app, _ = make_app(ex)
    enter(app)
    app.handle("right")
    assert wait_until(lambda: not ex._volume_inflight)
    assert fake.posts() == [("/api/speaker/volume", {"delta": 5})]
    assert ex.speaker_text() == "speaker ok · vol 75% (bluez)"
    assert ex.phase == DONE and ex.note == "vol 75%"
    assert not ex.busy                           # not a SENDING command
    app.handle("left")
    assert wait_until(lambda: not ex._volume_inflight)
    app.handle("left")
    assert wait_until(lambda: not ex._volume_inflight)
    assert [b["delta"] for p, b in fake.posts()[1:]] == [-5, -5]
    assert fake.speaker["volume"] == 65
    assert ex.speaker_text() == "speaker ok · vol 65% (bluez)"
    # A plain press, repeatable - and never a START.
    assert all(p == "/api/speaker/volume" for p, _ in fake.posts())
    assert fake.run is None


def test_presses_during_a_volume_request_add_up_into_one_more():
    ex, fake = make_exhibition()
    ex.poll()
    app, _ = make_app(ex)
    enter(app)
    fake.release.clear()                         # the first request hangs
    app.handle("right")
    assert wait_until(lambda: ex._volume_inflight)
    for event in ("right", "right", "left", "right"):
        app.handle(event)                        # +5 +5 -5 +5 = +10, waiting
    assert len(fake.posts()) == 1                # nothing queued behind it
    assert ex._volume_wanted == 10
    app.handle("key1_hold")                      # other keys still live
    fake.release.set()
    assert wait_until(lambda: not ex._volume_inflight)
    ex.join_volume(2.0)
    volume_posts = [b for p, b in fake.posts() if p == "/api/speaker/volume"]
    assert volume_posts == [{"delta": 5}, {"delta": 10}]
    assert fake.speaker["volume"] == 85
    assert wait_until(lambda: ex.phase == DONE and fake.run is not None)
    assert ex.speaker_text() == "speaker ok · vol 85% (bluez)"


def test_a_volume_answer_during_a_start_leaves_sending_and_the_key_gate_alone():
    # START is out (held by the fake); a volume request answers meanwhile.
    # Its verdict must not turn SENDING into DONE - that would reopen the
    # keys while START is still in flight (review of 7c68f87, MED-1).
    ex, fake = make_exhibition()
    ex.poll()
    app, _ = make_app(ex)
    enter(app)
    fake.hold_paths = {"/api/fleet/start"}
    fake.release.clear()
    app.handle("key1_hold")                      # START, held
    assert ex.busy and ex.phase == SENDING
    ex.adjust_volume(5)                          # answers at once
    assert wait_until(lambda: not ex._volume_inflight)
    ex.join_volume(2.0)
    assert fake.speaker["volume"] == 75
    assert ex.phase == SENDING and ex.busy       # untouched
    assert ex.status_text() == "sending…"
    assert ex.speaker_text() == "speaker ok · vol 75% (bluez)"
    for event in ("up", "key1_hold", "key3_hold"):
        app.handle(event)                        # the gate still holds
    assert [p for p, _ in fake.posts()] == ["/api/fleet/start",
                                            "/api/speaker/volume"]
    # The unsupported note is gated the same way.
    fake.no_volume = True
    ex.poll()
    ex.adjust_volume(5)
    assert ex.phase == SENDING and ex.note == ""
    fake.no_volume = False
    fake.release.set()
    assert wait_until(lambda: ex.phase == DONE)
    assert ex.note == "START in 11 s · 7/7 units"


def test_garbage_volume_values_read_as_vol_question_mark():
    ex, fake = make_exhibition()
    for value in ("70", True, None, float("nan"), float("inf"), [70]):
        fake.speaker["volume"] = value
        ex.poll()
        assert ex.speaker_text() == "speaker ok · vol ?", value
        assert ex.volume_supported()             # the key is there
    fake.speaker["volume"] = 130.7
    fake.speaker["applied"] = None
    ex.poll()
    assert ex.speaker_text() == "speaker ok · vol 100%"
    fake.speaker["applied"] = 3
    ex.poll()
    assert ex.speaker_text() == "speaker ok · vol 100%"
    # ...and a garbage reply to a request is no crash either.
    fake.speaker["volume"] = 40
    fake.speaker["applied"] = "pulse"
    ex.poll()
    original = fake.__call__

    def odd(method, url, body, timeout):
        if url.endswith("/api/speaker/volume"):
            return 200, {"volume": "loud", "applied": "pulse", "error": None}
        return original(method, url, body, timeout)
    ex._http = odd
    ex.adjust_volume(5)
    assert wait_until(lambda: not ex._volume_inflight)
    ex.join_volume(2.0)
    assert ex.note == "volume set" and ex.phase == DONE
    assert ex.speaker_text() == "speaker ok · vol ?"


def test_an_older_conductor_without_volume_only_says_so():
    ex, fake = make_exhibition()
    fake.no_volume = True
    ex.poll()
    assert ex.speaker_text() == "speaker ok · vol ?"
    assert ex.speaker_available() and not ex.volume_supported()
    app, _ = make_app(ex)
    enter(app)
    app.handle("right")
    app.handle("left")
    ex.join_volume(0.2)
    assert fake.posts() == []
    assert ex.status_text() == "volume: not supported by this conductor"
    app.handle("up")                             # read away like any verdict
    assert ex.status_text() == ""
    # No speaker at all: nothing is sent, nothing is said.
    fake.no_volume = False
    fake.speaker = {"available": False, "error": "no ALSA playback device"}
    ex.poll()
    app.handle("right")
    ex.join_volume(0.2)
    assert fake.posts() == [] and ex.status_text() == ""
    # A refused request is a verdict, not a crash.
    fake.speaker = {"available": True, "error": None, "volume": 50,
                    "applied": None}
    ex.poll()
    assert ex.speaker_text() == "speaker ok · vol 50%"
    fake.down = True
    app.handle("right")
    assert wait_until(lambda: not ex._volume_inflight)
    assert ex.note == "ERROR volume: Connection refused"


def test_the_hat_loop_never_waits_on_http():
    ex, fake = make_exhibition()
    ex.poll()
    fake.release.clear()                         # every request now hangs
    app, _ = make_app(ex)
    started = time.monotonic()
    enter(app)                                   # open(): the poll is the reader's
    app.draw()
    app.handle("key1_hold")                      # START, on its worker
    app.draw()
    app.tick(wait=0.0)
    assert time.monotonic() - started < 0.5
    assert ex.busy
    fake.release.set()
    assert wait_until(lambda: ex.phase == DONE)
    ex.shutdown()


def test_locked_unit_cannot_start():
    ex, fake = make_exhibition()
    ex.poll()
    app, _ = make_app(ex, locked=True)
    app.select("exhibition")
    app.handle("key1")
    app.handle("key1_hold")
    app.handle("key3_hold")
    ex.join(0.2)
    assert app.screen is Screen.MENU
    assert fake.posts() == []


# ---- the screen ----

def test_the_screen_renders_every_state():
    show = ("AZ_show_2026", "18 cues · 10:54")
    cases = [
        (True, show, "idle", "units 7/7 online", "LOOP off", "speaker ok",
         IDLE, "", False),
        (True, show, "idle", "units 7/7 online", "LOOP off",
         "speaker ok · vol 70% (bluez)", DONE, "vol 70%", False),
        (True, show, "idle", "units 7/7 online", "LOOP off",
         "speaker ok · vol ?", DONE, "volume: not supported by this conductor",
         False),
        (True, show, "countdown -0:11", "units 7/7 online", "LOOP off",
         "speaker ok", SENDING, "sending…", True),
        (True, show, "3:20 / 10:54 running", "units 6/7 online", "LOOP on",
         "no speaker - no ALSA playback device", DONE,
         "START in 11 s · 7/7 units", True),
        (True, show, "hold 3:20 / 10:54", "units 7/7 online", "loop ?",
         "speaker ?", DONE, "STOP · 7/7 units", True),
        (True, show, "next run in 0:25", "units 7/7 online", "LOOP on",
         "speaker ok", DONE, "LOOP on", False),
        (True, show, "preset… 5/7 painted", "units 7/7 online", "LOOP off",
         "speaker ok", DONE, "START in 11 s · 7/7 units", True),
        (True, show, "loop waiting", "units 6/7 online", "LOOP on",
         "speaker ok", IDLE, "", True),
        (True, ("AZ_show_2026", "18 cues · 10:54 · not uploaded"), "idle",
         "units 0/7 online", "LOOP off", "speaker ok", FAILED,
         "ERROR every unit holds an older upload than the timeline on "
         "screen - Upload again before the show", False),
        (False, ("", ""), "", "", "", "", IDLE,
         "no conductor: Connection refused", False),
        (None, ("", ""), "", "", "", "", IDLE, "", False),
    ]
    for available, lines, run, fleet, loop, speaker, phase, status, active in cases:
        for volume_keys in (False, True):
            image = render.exhibition_screen(available, lines, run, fleet, loop,
                                             speaker, phase, status=status,
                                             active=active, host="radxa-05",
                                             volume_keys=volume_keys)
            assert image.size == (WIDTH, HEIGHT)
    assert render.exhibition_screen(True, show, "idle", "", "", "", IDLE,
                                    locked=True).size == (WIDTH, HEIGHT)
    # The `< > volume` hint follows volume_supported(), not the speaker
    # line: with and without it the key line differs (review LOW-3).
    plain = render.exhibition_screen(True, show, "idle", "", "", "speaker ok · vol ?",
                                     IDLE, host="radxa-05")
    keyed = render.exhibition_screen(True, show, "idle", "", "", "speaker ok · vol ?",
                                     IDLE, host="radxa-05", volume_keys=True)
    assert plain.tobytes() != keyed.tobytes()


def test_the_app_draws_exhibition_and_repaints_only_on_change():
    ex, fake = make_exhibition()
    ex.poll()
    app, _ = make_app(ex)
    enter(app)
    app.draw()
    key = app._display_key()
    assert key[0] == "exhibition"
    frames = app.display.frames
    app.tick(wait=0.0)
    app.tick(wait=0.0)
    assert app.display.frames == frames          # idle: no repaint
    fake.run = {"t0": 0.0, "state": "running", "now": 3.0}
    ex.poll()                                    # the reader landed a run
    app.tick(wait=0.0)
    assert app.display.frames == frames + 1
    assert app._display_key() != key
