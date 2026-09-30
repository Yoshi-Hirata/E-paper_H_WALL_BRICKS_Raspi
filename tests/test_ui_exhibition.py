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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ui import render
from ui.app import App, Screen
from ui.config import EVENTS, HEIGHT, WIDTH
from ui.display import NullDisplay
from ui.exhibition import (CONDUCTOR_URL, DONE, FAILED, IDLE, MENU_LABEL,
                           MENU_LABEL_NONE, SENDING, Exhibition, format_clock)
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
    look at the screen while one is in flight."""

    def __init__(self, uploaded=True, offline=()):
        self.run = None
        self.uploaded = uploaded
        self.offline = set(offline)
        self.loop = {"on": False, "wait_s": 30.0, "next_in_s": None}
        self.speaker = {"available": True, "error": None}
        self.down = False
        self.old = False
        self.refuse = None
        self.calls = []
        self.release = threading.Event()
        self.release.set()

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
        if not self.old:
            snap["loop"] = dict(self.loop)
            snap["speaker"] = dict(self.speaker)
        return snap

    def __call__(self, method, url, body, timeout):
        assert url.startswith(CONDUCTOR_URL)
        path = url[len(CONDUCTOR_URL):]
        self.calls.append((method, path, body, timeout))
        self.release.wait(5.0)
        if self.down:
            raise OSError("[Errno 111] Connection refused")
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
            units = {u: ({"ok": True} if u not in self.offline
                         else {"ok": False, "error": "offline"}) for u in UNITS}
            return 200, {"units": units, "lead_s": COUNTDOWN, "from_s": 0.0}
        if method == "POST" and path == "/api/fleet/stop":
            assert body == {}
            self.run = None
            return 200, {"units": {u: {"ok": True} for u in UNITS}}
        if method == "POST" and path == "/api/loop":
            assert isinstance(body.get("on"), bool)
            self.loop["on"] = body["on"]
            self.loop["next_in_s"] = None
            return 200, dict(self.loop)
        return 404, "not found"


def make_exhibition(**kwargs):
    fake = FakeConductor(**kwargs)
    ex = Exhibition(http=fake, poll_open_s=60.0, poll_idle_s=60.0,
                    echo_log=False)
    return ex, fake


def make_app(ex, locked=False):
    runner = FakeRunner()
    app = App(NullDisplay(), ScriptedInput(()), runner, port_label="/dev/fake",
              exhibition=ex, host="radxa-05", locked=locked)
    return app, runner


def enter(app):
    app.select("exhibition")
    app.handle("key1")


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
    assert ex.speaker_text() == "speaker ok"
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

    fake.run = None
    fake.loop = {"on": True, "wait_s": 30.0, "next_in_s": 25.0}
    fake.speaker = {"available": False, "error": "no ALSA playback device\n"
                                                 "second line"}
    ex.poll()
    assert ex.run_text() == "next run in 0:25"
    assert ex.loop_text() == "LOOP on"
    assert ex.speaker_text() == "no speaker - no ALSA playback device"

    # A Conductor from before loop and speaker existed.
    fake.old = True
    fake.loop["on"] = True
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
    fake.run = None
    fake.loop = {"on": True, "wait_s": 30.0, "next_in_s": 5.0}
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
    for event in ("key1", "press", "up", "down", "left", "right", "key3"):
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
    assert ex.note == "ERROR [Errno 111] Connection refused"
    assert fake.posts()[-1] == ("/api/fleet/stop", {})
    assert ex.available is False                 # the poll after it saw that

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
        (True, show, "countdown -0:11", "units 7/7 online", "LOOP off",
         "speaker ok", SENDING, "sending…", True),
        (True, show, "3:20 / 10:54 running", "units 6/7 online", "LOOP on",
         "no speaker - no ALSA playback device", DONE,
         "START in 11 s · 7/7 units", True),
        (True, show, "hold 3:20 / 10:54", "units 7/7 online", "loop ?",
         "speaker ?", DONE, "STOP · 7/7 units", True),
        (True, show, "next run in 0:25", "units 7/7 online", "LOOP on",
         "speaker ok", DONE, "LOOP on", False),
        (True, ("AZ_show_2026", "18 cues · 10:54 · not uploaded"), "idle",
         "units 0/7 online", "LOOP off", "speaker ok", FAILED,
         "ERROR every unit holds an older upload than the timeline on "
         "screen - Upload again before the show", False),
        (False, ("", ""), "", "", "", "", IDLE,
         "no conductor: [Errno 111] Connection refused", False),
        (None, ("", ""), "", "", "", "", IDLE, "", False),
    ]
    for available, lines, run, fleet, loop, speaker, phase, status, active in cases:
        image = render.exhibition_screen(available, lines, run, fleet, loop,
                                         speaker, phase, status=status,
                                         active=active, host="radxa-05")
        assert image.size == (WIDTH, HEIGHT)
    assert render.exhibition_screen(True, show, "idle", "", "", "", IDLE,
                                    locked=True).size == (WIDTH, HEIGHT)


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
