"""SPEAKER: the menu row and ui/speaker.py.

The same fake Conductor as EXHIBITION's (tests/test_ui_exhibition.py),
grown the Bluetooth side of its speaker object (`device`, `connection`,
`reconnect`, `pairing`) and the two commands (/api/speaker/connect,
/api/speaker/pair - 409 while a run is on unless forced). Everything
here runs without a socket and never touches the Conductor on
127.0.0.1:8765.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ui import render
from ui.app import App, Screen
from ui.config import HEIGHT, WIDTH
from ui.display import NullDisplay
from ui.exhibition import SPEAKER_LOST_TEXT, VOLUME_UNSUPPORTED, Exhibition
from ui.inputs import ScriptedInput
from ui.speaker import (BUSY, CONNECTING, DONE, FAILED, IDLE, INSTRUCTION,
                        MENU_LABEL, MENU_LABEL_GONE, MENU_LABEL_NONE,
                        MUSIC_LOST, NOT_BLUETOOTH, NOT_SUPPORTED, PAIR_CONFIRM,
                        PAIR_CONFIRM_S, PAIR_FORCE, PAIRING_STALE_S, WIRED,
                        Speaker, format_error)
from tests.test_ui_app import FakeRunner
from tests.test_ui_exhibition import FakeConductor, FakeRemote, gone
from tests.test_ui_runner import wait_until


def make_speaker(clock=None, wall=None, **kwargs):
    fake = FakeConductor(**kwargs)
    ex = Exhibition(http=fake, poll_open_s=60.0, poll_idle_s=60.0,
                    poll_speaker_s=60.0, echo_log=False,
                    clock=clock or time.monotonic)
    speaker = Speaker(ex, wall=wall or time.time)     # the Exhibition's clock
    return speaker, ex, fake


def make_app(speaker, ex, locked=False, remote=None):
    runner = FakeRunner()
    app = App(NullDisplay(), ScriptedInput(()), runner, port_label="/dev/fake",
              exhibition=ex, speaker=speaker, host="radxa-05", locked=locked,
              remote=remote)
    return app, runner


def enter(app):
    app.select("speaker")
    app.handle("key1")


# ---- the row ----

def test_the_row_appears_after_exhibition_once_seen_and_its_label_follows():
    from ui.boardinfo import BoardInfo
    from ui.puller import RepoPuller
    from ui.rebooter import Rebooter
    from ui.wifi import Wifi

    speaker, ex, fake = make_speaker()
    wifi = Wifi(run=lambda a, t: (127, "nmcli not installed"), echo_log=False)
    app = App(NullDisplay(), ScriptedInput(()), FakeRunner(), puller=RepoPuller(),
              rebooter=Rebooter(), wifi=wifi, exhibition=ex, speaker=speaker,
              boardinfo=BoardInfo(None))
    keys = [p.key for p in app.patterns]
    # Nothing asked yet: no row (a garment unit never grows one).
    assert keys[-5:] == ["pull", "reboot", "wifi", "exhibition", "boardinfo"], keys
    fake.down = True
    ex.poll()
    app.tick(wait=0.0)
    assert "speaker" not in [p.key for p in app.patterns]
    fake.down = False
    before = app._display_key()
    ex.poll()
    app.tick(wait=0.0)                           # _idle_tasks puts it in
    keys = [p.key for p in app.patterns]
    assert keys[-6:] == ["pull", "reboot", "wifi", "exhibition", "speaker",
                         "boardinfo"], keys
    row = app.patterns[-2]
    assert row.label == MENU_LABEL == "SPEAKER"
    assert row.detail == "connect / re-pair the Bluetooth speaker"
    assert app._display_key() != before
    # The cursor stays on its row while the label changes under it.
    app.select("boardinfo")
    fake.no_speaker = True                       # started without --speaker
    ex.poll()
    app.tick(wait=0.0)
    assert app.patterns[app.selected].key == "boardinfo"
    assert row.label == MENU_LABEL_NONE == "SPEAKER  (no speaker)"
    assert row.detail == "the Conductor runs without --speaker"
    assert speaker.present and not speaker.configured
    # The Conductor goes: the row stays, like EXHIBITION's, and says so
    # (no flapping list, no cursor drift - review of 73c8fdc, LOW-1).
    fake.no_speaker = False
    gone(fake, ex)
    app.tick(wait=0.0)
    assert [p.key for p in app.patterns][-3:] == ["exhibition", "speaker",
                                                  "boardinfo"]
    assert row.label == MENU_LABEL_GONE == "SPEAKER  (no conductor)"
    assert row.detail == "needs the Conductor on this unit"
    assert speaker.mode() == "missing"
    assert app.patterns[app.selected].key == "boardinfo"
    fake.down = False
    ex.poll()
    assert row.label == MENU_LABEL
    # A wired output: the row is there, the detail says volume only.
    fake.bluetooth = False
    ex.poll()
    assert row.label == MENU_LABEL and row.detail == "wired speaker: volume only"
    # Without a Speaker wired in, nothing of this exists.
    plain = App(NullDisplay(), ScriptedInput(()), FakeRunner(), exhibition=ex)
    ex.poll()
    plain.tick(wait=0.0)
    assert "speaker" not in [p.key for p in plain.patterns]


def test_the_reader_polls_every_two_seconds_while_the_screen_is_open():
    fake = FakeConductor()
    ex = Exhibition(http=fake, poll_open_s=60.0, poll_idle_s=60.0,
                    poll_speaker_s=0.05, echo_log=False)
    speaker = Speaker(ex)
    assert ex.poll_interval() == 60.0
    try:
        ex.start_reader()
        assert wait_until(lambda: ex.available is True)
        first = len(fake.calls)
        time.sleep(0.2)
        assert len(fake.calls) == first          # idle: the slow poll
        speaker.open()
        assert ex.speaker_open and ex.poll_interval() == 0.05
        assert wait_until(lambda: len(fake.calls) >= first + 4, timeout=2.0)
        assert fake.posts() == []                # only GETs
        assert "/api/show/export" not in [c[1] for c in fake.calls]
        speaker.close()
        assert not ex.speaker_open and ex.poll_interval() == 60.0
        # EXHIBITION open at the same time: the faster of the two wins.
        ex.is_open = True
        assert ex.poll_interval() == 60.0
        ex.speaker_open = True
        assert ex.poll_interval() == 0.05
    finally:
        ex.shutdown()


def test_a_screen_left_by_any_path_gives_up_its_fast_poll():
    # The follow (or anything that sets the screen) can leave SPEAKER /
    # EXHIBITION without their close(): _idle_tasks brings the reader's
    # pace back in line (review of 73c8fdc, LOW-3).
    speaker, ex, fake = make_speaker()
    ex.poll()
    app, _ = make_app(speaker, ex)
    app.tick(wait=0.0)
    enter(app)
    assert ex.speaker_open and speaker.is_open
    app.screen = Screen.MENU                     # no KEY2
    app.tick(wait=0.0)
    assert not ex.speaker_open and not speaker.is_open
    app.select("exhibition")
    app.handle("key1")
    assert ex.is_open
    app.screen = Screen.MENU
    app.tick(wait=0.0)
    assert not ex.is_open
    # ...and the screens themselves are left alone while they are up.
    enter(app)
    app.tick(wait=0.0)
    assert ex.speaker_open and app.screen is Screen.SPEAKER


# ---- what the screen says ----

def test_the_texts_for_every_state():
    clock = [1000.0]
    wall = [2_000_000.0]
    speaker, ex, fake = make_speaker(clock=lambda: clock[0], wall=lambda: wall[0])
    fake.device["last_connected_at"] = wall[0] - 12 * 60
    ex.poll()
    assert speaker.mode() == "ok" and speaker.supported
    assert speaker.status_word() == "READY"
    assert speaker.device_text() == "Bose Flex SoundLink"
    assert speaker.state_text() == "connected · vol 70%"
    assert speaker.detail_text() == ("", "")
    assert speaker.seen_text() == "connected 12 min ago"
    assert speaker.banner() == "" and not speaker.busy
    assert speaker.volume_keys()
    assert ex.speaker_text() == "speaker ok · vol 70% (bluez)"

    # Lost, with bluez's words - and a run on: MUSIC LOST.
    fake.connection = "disconnected"
    fake.device["connected"] = False
    fake.device["last_error"] = ("Failed to connect: org.bluez.Error.Failed "
                                 "br-connection-permission-denied: Permission "
                                 "denied\nsecond line")
    fake.run = {"t0": 0.0, "state": "running", "now": 42.0}
    ex.poll()
    assert speaker.state_text() == "NOT CONNECTED"
    assert speaker.detail_text() == ("Permission denied - another phone?", "err")
    assert speaker.banner() == MUSIC_LOST == "MUSIC LOST"
    assert ex.speaker_text() == SPEAKER_LOST_TEXT == "speaker LOST - see SPEAKER"
    fake.run = None
    ex.poll()
    assert speaker.banner() == ""                # idle: no music to lose
    fake.device["last_error"] = "Device 2C:41:A1:0B:7E:19 not available"
    ex.poll()
    assert speaker.detail_text() == ("Device 2C:41:A1:0B:7E:19 not available", "err")

    # The Conductor's own reconnect: the countdown moves with the clock.
    fake.reconnect = {"attempts": 3, "next_in_s": 25.0,
                      "last_error": "Permission denied"}
    ex.poll()
    assert speaker.detail_text() == ("reconnect in 25 s (3 tries)", "warn")
    clock[0] += 4.0
    assert speaker.detail_text() == ("reconnect in 21 s (3 tries)", "warn")
    fake.reconnect["attempts"] = 1
    ex.poll()
    assert speaker.detail_text() == ("reconnect in 25 s (1 try)", "warn")
    fake.reconnect = {"attempts": 0, "next_in_s": 25.0, "last_error": None}
    ex.poll()
    assert speaker.detail_text() == ("reconnect in 25 s", "warn")
    fake.reconnect = {"attempts": 0, "next_in_s": None,
                      "last_error": "Permission denied"}
    fake.device["last_error"] = None
    ex.poll()
    # No device error of its own: the reconnect's is shown.
    assert speaker.detail_text() == ("Permission denied - another phone?", "err")

    fake.connection = "connecting"
    fake.reconnect["last_error"] = None
    ex.poll()
    assert speaker.state_text() == "connecting…"
    assert speaker.detail_text() == ("", "")
    assert not speaker.busy                      # the Conductor's own retry

    # Pairing, phase by phase - from the poll, the keys shut meanwhile.
    fake.pairing = {"phase": "scanning", "note": "looking for the Bose",
                    "started_at": wall[0] - 3}
    fake.connection = "pairing"
    ex.poll()
    assert speaker.state_text() == "pairing: scanning…"
    assert speaker.detail_text() == ("looking for the Bose", "")
    assert speaker.busy and speaker.pairing_active
    assert speaker.status_word() == "BUSY"
    assert speaker.show_instruction()
    fake.advance_pairing("pairing")
    ex.poll()
    assert speaker.state_text() == "pairing: pairing…"
    fake.advance_pairing("connecting")
    ex.poll()
    assert speaker.state_text() == "pairing: connecting…"
    fake.advance_pairing("failed", "Permission denied")
    ex.poll()
    assert speaker.state_text() == "NOT CONNECTED"
    assert speaker.detail_text() == ("pairing failed - Permission denied - "
                                     "another phone?", "err")
    assert not speaker.busy and speaker.status_word() == "READY"
    fake.advance_pairing("done")
    fake.device["last_connected_at"] = wall[0] - 20
    ex.poll()
    assert speaker.state_text() == "connected · vol 70%"
    assert speaker.seen_text() == "connected just now"
    assert speaker.detail_text() == ("", "")
    # `pairing` null but connection "pairing" (between the answer and the
    # first poll): still said.
    fake.pairing = None
    fake.connection = "pairing"
    ex.poll()
    assert speaker.state_text() == "pairing…"

    # No device at all.
    fake.device = None
    fake.connection = "no_device"
    fake.speaker["error"] = "no paired speaker"
    ex.poll()
    assert speaker.device_text() == "no speaker paired"
    assert speaker.state_text() == "NOT CONNECTED"
    assert speaker.detail_text() == ("no paired speaker", "err")
    assert speaker.seen_text() == ""
    assert ex.speaker_text() == SPEAKER_LOST_TEXT
    fake.speaker["error"] = None
    fake.device = {"mac": "2C:41:A1:0B:7E:19", "name": "", "paired": False,
                   "trusted": False, "connected": False, "sink_present": False,
                   "last_connected_at": None, "last_error": None}
    ex.poll()
    assert speaker.device_text() == "2C:41:A1:0B:7E:19"   # the name is empty
    assert speaker.seen_text() == "never connected"
    assert speaker.detail_text() == ("", "")
    # Garbage never raises on the HAT loop.
    fake.device["last_connected_at"] = "not a date"
    fake.connection = 7
    fake.reconnect = "soon"
    fake.pairing = "yes"
    fake.speaker["volume"] = "loud"
    ex.poll()
    assert speaker.seen_text() == "never connected"
    assert speaker.state_text() == "?"
    assert speaker.detail_text() == ("", "")
    assert speaker.key()
    fake.connection = "connected"
    ex.poll()
    assert speaker.state_text() == "connected · vol ?"

    # A `pairing` left over from long ago is not a pairing in progress:
    # the keys must not stay shut on it (review of 73c8fdc, LOW-4).
    fake.speaker["volume"] = 70
    fake.pairing = {"phase": "scanning", "note": "old", "started_at": None}
    ex.poll()
    assert speaker.pairing_active                # no stamp: trusted
    fake.pairing["started_at"] = wall[0] - PAIRING_STALE_S + 5
    ex.poll()
    assert speaker.pairing_active and speaker.busy
    fake.pairing["started_at"] = wall[0] - PAIRING_STALE_S - 1
    ex.poll()
    assert not speaker.pairing_active and not speaker.busy
    assert speaker.state_text() == "connected · vol 70%"
    assert speaker.status_word() == "READY" and not speaker.show_instruction()


def test_a_wired_output_is_the_volume_alone():
    # speaker.bluetooth false (--speaker-output pulse with a wired sink):
    # no link to lose, nothing to connect or pair (review of 73c8fdc,
    # MED-2).
    speaker, ex, fake = make_speaker()
    fake.bluetooth = False
    fake.connection = "no_device"
    fake.device = None
    fake.run = {"t0": 0.0, "state": "running", "now": 42.0}
    fake.pairing = {"phase": "scanning", "note": "", "started_at": time.time()}
    ex.poll()
    assert speaker.mode() == "wired" and not speaker.bluetooth
    assert speaker.device_text() == WIRED == "wired / not Bluetooth"
    assert speaker.state_text() == "vol 70%"
    assert speaker.detail_text() == ("", "") and speaker.seen_text() == ""
    assert speaker.banner() == ""                # never MUSIC LOST
    assert not speaker.busy and not speaker.show_instruction()
    assert speaker.volume_keys()
    assert ex.speaker_text() == "speaker ok · vol 70% (bluez)"   # never LOST
    app, _ = make_app(speaker, ex)
    app.tick(wait=0.0)
    enter(app)
    for event in ("key1_hold", "key3_hold", "key3_hold", "key1", "up"):
        app.handle(event)
    speaker.join(0.2)
    assert fake.posts() == []                    # no holds
    app.handle("key1_hold")
    assert speaker.status_text() == NOT_BLUETOOTH
    assert not speaker.confirming
    app.handle("right")                          # the volume still works
    assert wait_until(lambda: not ex._volume_inflight)
    ex.join_volume(2.0)
    assert fake.posts() == [("/api/speaker/volume", {"delta": 5})]
    assert speaker.state_text() == "vol 75%"
    app.draw()
    app.handle("key2")
    assert app.screen is Screen.MENU


def test_last_connected_at_reads_numbers_and_iso_strings():
    from ui.speaker import _ago, _epoch

    assert _epoch(1_700_000_000) == 1_700_000_000.0
    assert _epoch(1_700_000_000.5) == 1_700_000_000.5
    assert _epoch("2026-10-01T12:00:00+00:00") == 1_790_856_000.0
    assert _epoch("2026-10-01T12:00:00Z") == 1_790_856_000.0
    assert _epoch("2026-10-01T12:00:00") == 1_790_856_000.0     # naive: UTC
    for junk in (None, True, "", "yesterday", float("nan"), [1]):
        assert _epoch(junk) is None, junk
    assert _ago(5) == "just now" and _ago(59.9) == "just now"
    assert _ago(60) == "1 min ago" and _ago(12 * 60 + 30) == "12 min ago"
    assert _ago(3600) == "1 h ago" and _ago(5 * 3600) == "5 h ago"
    assert _ago(3 * 86400) == "3 d ago"
    assert format_error("") == "" and format_error(None) == ""
    assert format_error("\n  timed out \nmore") == "timed out"
    assert format_error("org.bluez.Error.Failed: Permission denied") == \
        "Permission denied - another phone?"


def test_an_older_conductor_degrades_and_a_conductor_without_speaker_is_a_note():
    speaker, ex, fake = make_speaker()
    fake.no_bluetooth = True
    ex.poll()
    assert speaker.present and speaker.configured and not speaker.supported
    assert speaker.mode() == "old"
    assert speaker.device_text() == "speaker ?"
    assert speaker.state_text() == "speaker ok · vol 70% (bluez)"
    assert speaker.detail_text() == ("", "") and speaker.seen_text() == ""
    assert speaker.banner() == "" and not speaker.show_instruction()
    app, _ = make_app(speaker, ex)
    enter(app)
    assert app.screen is Screen.SPEAKER and speaker.is_open
    app.handle("key1_hold")
    speaker.join(0.2)
    assert fake.posts() == []
    assert speaker.status_text() == f"connect: {NOT_SUPPORTED}"
    assert speaker.status_text() == "connect: not supported by this conductor"
    app.handle("key3_hold")
    speaker.join(0.2)
    assert fake.posts() == []
    assert speaker.status_text() == "pair: not supported by this conductor"
    assert not speaker.confirming
    app.handle("up")                             # read away
    assert speaker.status_text() == ""
    app.handle("right")                          # the volume still works
    assert wait_until(lambda: not ex._volume_inflight)
    assert fake.posts() == [("/api/speaker/volume", {"delta": 5})]
    app.handle("key2")
    assert app.screen is Screen.MENU and not speaker.is_open

    # Without --speaker: the screen is a note, nothing is sent.
    fake.no_bluetooth = False
    fake.no_speaker = True
    ex.poll()
    assert speaker.mode() == "none"
    app.tick(wait=0.0)
    enter(app)
    assert app.screen is Screen.SPEAKER
    for event in ("key1", "key1_hold", "key3_hold", "up", "left", "right"):
        app.handle(event)
    speaker.join(0.2)
    ex.join_volume(0.2)
    assert len(fake.posts()) == 1                # nothing new
    app.handle("key2")
    assert app.screen is Screen.MENU

    # The Conductor gone under the open screen: a note, KEY2 the way back.
    fake.no_speaker = False
    ex.poll()
    app.tick(wait=0.0)
    enter(app)
    fake.down = True
    ex.poll()
    app.tick(wait=0.0)
    assert app.screen is Screen.SPEAKER          # not yanked to the menu
    assert speaker.mode() == "missing"
    assert speaker.status_text() == "no conductor: Connection refused"
    app.handle("key1_hold")
    speaker.join(0.2)
    assert len(fake.posts()) == 1
    app.handle("key2")
    assert app.screen is Screen.MENU


# ---- the keys ----

def test_only_a_held_key1_connects_and_the_answer_is_shown():
    speaker, ex, fake = make_speaker()
    fake.connection = "disconnected"
    fake.device["connected"] = False
    fake.device["last_error"] = "Permission denied"
    ex.poll()
    app, runner = make_app(speaker, ex)
    app.tick(wait=0.0)
    enter(app)
    assert app.screen is Screen.SPEAKER and speaker.is_open
    assert runner.stops == 0                     # the runner keeps the port
    for event in ("key1", "press", "up", "down", "key3"):
        app.handle(event)
    app.blanked = False
    speaker.join(0.2)
    assert fake.posts() == []                    # nothing sent
    app.handle("key1_hold")
    # The Conductor answers "connecting" and gets on with it: the verdict
    # is amber `connecting…`, never a green DONE - the poll decides
    # (review of 73c8fdc, MED-1).
    assert wait_until(lambda: speaker.note == CONNECTING)
    assert fake.posts() == [("/api/speaker/connect", {})]
    assert speaker.phase == IDLE and speaker.status_word() == "READY"
    assert speaker.status_text() == "connecting…"
    assert speaker.state_text() == "connecting…"            # the poll after
    assert not speaker.busy                      # the keys are back
    fake.finish_connect(ok=False, error="Permission denied")
    ex.poll()
    assert speaker.state_text() == "NOT CONNECTED"
    assert speaker.detail_text() == ("Permission denied - another phone?", "err")
    app.handle("key1_hold")
    assert wait_until(lambda: len(fake.posts()) == 2)
    assert wait_until(lambda: speaker.note == CONNECTING)
    fake.finish_connect()
    ex.poll()
    assert speaker.state_text() == "connected · vol 70%"
    assert speaker.detail_text() == ("", "")
    assert fake.connection == "connected"
    # Moving reads the verdict away.
    app.handle("down")
    assert speaker.phase == IDLE and speaker.status_text() == ""
    # Never refused for a run (connecting is harmless; no force path)...
    fake.run = {"t0": 0.0, "state": "running", "now": 42.0}
    ex.poll()
    app.handle("key1_hold")
    assert wait_until(lambda: speaker.note == CONNECTING)
    assert fake.posts()[-1] == ("/api/speaker/connect", {})
    assert not speaker.forcing
    # ...but 409 while a pairing is in progress: the words, verbatim, red.
    fake.run = None
    fake.pairing = {"phase": "scanning", "note": "", "started_at": time.time()}
    fake.connection = "pairing"
    ex.poll()
    assert speaker.busy                          # the poll says pairing
    speaker.connect()                            # (the App would not even ask)
    speaker.join(0.2)
    assert len(fake.posts()) == 3
    fake.pairing = None
    fake.connection = "disconnected"
    ex.poll()
    fake.pairing = {"phase": "scanning", "note": "", "started_at": time.time()}
    app.handle("key1_hold")                      # the cache lagged: 409
    assert wait_until(lambda: speaker.phase == FAILED)
    assert speaker.note == "ERROR re-pairing is in progress - wait for it"
    assert speaker.status_word() == "BUSY"        # the poll after saw the pairing
    assert not speaker.forcing
    fake.pairing = None
    ex.poll()
    assert speaker.status_word() == "FAILED"
    # A connected answer straight away is a plain DONE.
    fake.pairing = None
    fake.connection = "disconnected"
    ex.poll()
    original = fake.__call__

    def direct(method, url, body, timeout):
        if url.endswith("/api/speaker/connect"):
            fake.finish_connect()
            return 200, {"ok": True, "connection": "connected", "error": None}
        return original(method, url, body, timeout)
    ex._http = direct
    app.handle("up")
    app.handle("key1_hold")
    assert wait_until(lambda: speaker.phase == DONE)
    assert speaker.note == "connected" and speaker.status_word() == "DONE"
    ex._http = original
    # ...and a dead Conductor is the socket's own reason.
    fake.down = True
    app.handle("up")
    app.handle("key1_hold")
    assert wait_until(lambda: speaker.phase == FAILED)
    assert speaker.note == "ERROR Connection refused"
    assert fake.posts()[-1] == ("/api/speaker/connect", {})


def test_pairing_takes_two_holds_and_follows_the_polls_phases():
    clock = [500.0]
    speaker, ex, fake = make_speaker(clock=lambda: clock[0])
    fake.device = None
    fake.connection = "no_device"
    ex.poll()
    app, _ = make_app(speaker, ex)
    app.tick(wait=0.0)
    enter(app)
    assert not speaker.show_instruction()
    app.handle("key3_hold")                      # the first: ask
    speaker.join(0.2)
    assert fake.posts() == []
    assert speaker.confirming and speaker.status_text() == PAIR_CONFIRM
    assert speaker.status_text() == "hold KEY3 again = pair"
    assert speaker.show_instruction() and speaker.status_word() == "READY"
    assert INSTRUCTION == ("put the Bose in pairing mode (hold its Bluetooth "
                           "button) and switch off phones' Bluetooth")
    # Too late: the next hold is a first hold again.
    clock[0] += PAIR_CONFIRM_S + 0.1
    assert not speaker.confirming and not speaker.show_instruction()
    app.handle("key3_hold")
    speaker.join(0.2)
    assert fake.posts() == [] and speaker.confirming
    # In time: the pair goes out, and the pairing runs on the Conductor.
    clock[0] += 5.0
    app.handle("key3_hold")
    assert wait_until(lambda: speaker.phase == DONE)
    assert fake.posts() == [("/api/speaker/pair", {})]
    assert speaker.note == "pairing started"
    assert speaker.state_text() == "pairing: scanning…"
    assert speaker.busy and speaker.status_word() == "BUSY"
    assert speaker.show_instruction()
    # Only KEY2 is heard while it pairs - but the poll keeps the screen live.
    for event in ("key1_hold", "key3_hold", "up", "left", "right"):
        app.handle(event)
    speaker.join(0.2)
    ex.join_volume(0.2)
    assert len(fake.posts()) == 1
    fake.advance_pairing("connecting")
    ex.poll()
    assert speaker.state_text() == "pairing: connecting…" and speaker.busy
    fake.advance_pairing("done")
    ex.poll()
    assert not speaker.busy and speaker.status_word() == "DONE"
    assert speaker.state_text() == "connected · vol 70%"
    assert speaker.device_text() == "Bose Flex SoundLink"
    assert not speaker.show_instruction()
    # A pairing that fails is red, with the Conductor's note.
    app.handle("up")
    app.handle("key3_hold")
    clock[0] += 1.0
    app.handle("key3_hold")
    assert wait_until(lambda: speaker.phase == DONE and len(fake.posts()) == 2)
    fake.advance_pairing("failed", "no device found in pairing mode")
    ex.poll()
    assert speaker.state_text() == "NOT CONNECTED"
    assert speaker.detail_text() == ("pairing failed - no device found in "
                                     "pairing mode", "err")
    assert not speaker.busy
    # KEY2 during a pairing leaves it to the Conductor.
    app.handle("up")
    app.handle("key3_hold")
    app.handle("key3_hold")
    assert wait_until(lambda: len(fake.posts()) == 3)
    assert wait_until(lambda: speaker.phase == DONE)
    assert speaker.busy
    app.handle("key2")
    assert app.screen is Screen.MENU and not speaker.is_open
    assert fake.connection == "pairing"
    # A refused pair (200, ok false) is a verdict.
    fake.advance_pairing("done")
    fake.pair_error = "bluetoothctl: not found"
    ex.poll()
    enter(app)
    app.handle("key3_hold")
    app.handle("key3_hold")
    assert wait_until(lambda: speaker.phase == FAILED)
    assert speaker.note == "ERROR bluetoothctl: not found"


def test_a_409_during_a_run_asks_for_one_more_hold_and_then_forces():
    clock = [100.0]
    speaker, ex, fake = make_speaker(clock=lambda: clock[0])
    fake.run = {"t0": 0.0, "state": "running", "now": 42.0}
    fake.connection = "disconnected"
    fake.device["connected"] = False
    ex.poll()
    app, _ = make_app(speaker, ex)
    app.tick(wait=0.0)
    enter(app)
    assert speaker.banner() == "MUSIC LOST"
    app.handle("key3_hold")                      # ask
    app.handle("key3_hold")                      # pair -> 409 "show running"
    assert wait_until(lambda: len(fake.posts()) == 1)
    assert wait_until(lambda: speaker.forcing)
    assert fake.posts() == [("/api/speaker/pair", {})]
    assert speaker.phase == IDLE and speaker.status_word() == "READY"
    assert speaker.status_text() == PAIR_FORCE
    assert speaker.status_text() == "show running - hold KEY3 again to pair anyway"
    assert speaker.show_instruction()
    assert fake.connection == "disconnected"     # nothing happened
    # The next hold forces - within the window.
    clock[0] += 3.0
    app.handle("key3_hold")
    assert wait_until(lambda: len(fake.posts()) == 2)
    assert fake.posts()[-1] == ("/api/speaker/pair", {"force": True})
    assert wait_until(lambda: speaker.phase == DONE)
    assert speaker.note == "pairing started" and speaker.busy
    assert not speaker.forcing
    fake.advance_pairing("done")
    ex.poll()
    assert speaker.banner() == ""                # music back
    # After the window the hold is a plain first hold again - and the
    # prompt goes with the window (review of 73c8fdc, LOW-5).
    fake.connection = "disconnected"
    ex.poll()
    app.handle("up")
    app.handle("key3_hold")
    app.handle("key3_hold")
    assert wait_until(lambda: speaker.forcing)
    assert speaker.status_text() == PAIR_FORCE
    clock[0] += PAIR_CONFIRM_S + 1.0
    assert not speaker.forcing
    assert speaker.status_text() == "" and not speaker.show_instruction()
    app.handle("key3_hold")
    speaker.join(0.2)
    assert len(fake.posts()) == 3 and speaker.confirming
    assert speaker.status_text() == PAIR_CONFIRM
    clock[0] += PAIR_CONFIRM_S + 1.0
    assert speaker.status_text() == "" and not speaker.show_instruction()
    # KEY1 meanwhile is no force path: connect is never refused for a run.
    app.handle("key1_hold")
    assert wait_until(lambda: speaker.note == CONNECTING)
    assert fake.posts()[-1] == ("/api/speaker/connect", {})
    assert not speaker.forcing and not speaker.show_instruction()
    fake.finish_connect()
    ex.poll()
    # A pair 409 that is not the run ("re-pairing is already in
    # progress"): verbatim, red, no force.
    fake.run = None
    fake.connection = "disconnected"
    ex.poll()
    app.handle("up")
    app.handle("key3_hold")
    fake.pairing = {"phase": "pairing", "note": "", "started_at": time.time()}
    app.handle("key3_hold")                      # the cache lagged: 409
    assert wait_until(lambda: speaker.phase == FAILED)
    assert speaker.note == "ERROR re-pairing is already in progress"
    assert not speaker.forcing
    fake.pairing = None
    ex.poll()
    # A run-409 is told apart by the Conductor's words, and failing those
    # by the cache's run.
    assert speaker._run_refusal("show running - STOP it first")
    assert speaker._run_refusal("Show running")
    assert not speaker._run_refusal("re-pairing is already in progress")
    assert not speaker._run_refusal("a pairing is on")
    assert not speaker._run_refusal("busy")      # no run in the cache
    fake.run = {"t0": 0.0, "state": "running", "now": 42.0}
    ex.poll()
    assert speaker._run_refusal("busy")


def test_only_key2_is_heard_while_a_request_is_in_flight():
    speaker, ex, fake = make_speaker()
    fake.connection = "disconnected"
    ex.poll()
    app, _ = make_app(speaker, ex)
    app.tick(wait=0.0)
    enter(app)
    fake.release.clear()
    app.handle("key1_hold")
    assert speaker.busy and speaker.phase == BUSY
    assert speaker.status_text() == "sending…" and speaker.status_word() == "BUSY"
    for event in ("up", "key1_hold", "key3_hold", "key1", "left", "right"):
        app.handle(event)
    assert len(fake.posts()) == 1                # nothing piled on
    assert not ex._volume_inflight
    app.handle("key2")
    assert app.screen is Screen.MENU and speaker.busy
    assert not speaker.is_open and not ex.speaker_open
    fake.release.set()
    assert wait_until(lambda: speaker.note == CONNECTING)
    assert speaker.phase == IDLE
    # Re-opening reads the old verdict away.
    enter(app)
    assert speaker.phase == IDLE and speaker.note == ""
    # A verdict whose own poll raises still frees the keys (LOW-7).
    fake.release.clear()
    app.handle("key1_hold")
    assert speaker.busy
    original_poll = ex.poll
    ex.poll = lambda: (_ for _ in ()).throw(RuntimeError("poll broke"))
    fake.release.set()
    assert wait_until(lambda: speaker.phase != BUSY)
    ex.poll = original_poll
    assert speaker.note == CONNECTING and not speaker.busy


def test_left_and_right_are_the_volume_as_on_exhibition():
    speaker, ex, fake = make_speaker()
    ex.poll()
    app, _ = make_app(speaker, ex)
    app.tick(wait=0.0)
    enter(app)
    app.handle("right")
    assert wait_until(lambda: not ex._volume_inflight)
    ex.join_volume(2.0)
    assert fake.posts() == [("/api/speaker/volume", {"delta": 5})]
    assert speaker.state_text() == "connected · vol 75%"
    app.handle("left")
    app.handle("left")
    assert wait_until(lambda: not ex._volume_inflight and
                      fake.speaker["volume"] == 65)
    assert speaker.state_text() == "connected · vol 65%"
    assert speaker.phase == IDLE                 # the volume is not a verdict here
    assert speaker.status_text() == ""
    # A volume ERROR, though, is shown here - this screen has the keys
    # (review of 73c8fdc, LOW-8) - and the redraw key sees it.
    key = speaker.key()
    fake.down = True
    app.handle("right")
    assert wait_until(lambda: not ex._volume_inflight)
    ex.join_volume(2.0)
    assert speaker.status_text() == "ERROR volume: Connection refused"
    assert speaker.key() != key
    fake.down = False
    ex.poll()
    fake.no_volume = True
    ex.poll()
    app.handle("right")
    ex.join_volume(0.2)
    assert speaker.status_text() == VOLUME_UNSUPPORTED
    assert not speaker.volume_keys()
    fake.no_volume = False
    ex.poll()
    app.handle("up")                             # read away, the volume's too
    assert speaker.status_text() == "" and ex.note == ""
    # Back on EXHIBITION the volume verdict is not left lying around.
    app.handle("key2")
    app.select("exhibition")
    app.handle("key1")
    assert ex.phase == "idle" and ex.status_text() == ""
    assert ex.speaker_text() == "speaker ok · vol 65% (bluez)"


def test_the_hat_loop_never_waits_on_http():
    speaker, ex, fake = make_speaker()
    ex.poll()
    fake.release.clear()                         # every request now hangs
    app, _ = make_app(speaker, ex)
    app.tick(wait=0.0)
    started = time.monotonic()
    enter(app)                                   # open(): the poll is the reader's
    app.draw()
    app.handle("key1_hold")                      # connect, on its worker
    app.draw()
    app.tick(wait=0.0)
    app.handle("key2")
    enter(app)
    app.handle("key3_hold")
    app.draw()
    assert time.monotonic() - started < 0.5
    assert speaker.busy
    fake.release.set()
    assert wait_until(lambda: speaker.note == CONNECTING and not speaker.busy)
    ex.shutdown()


def test_a_command_thread_that_cannot_start_is_a_verdict():
    speaker, ex, fake = make_speaker()
    ex.poll()
    original = threading.Thread.start

    def refuse(self):
        raise RuntimeError("can't start new thread")
    threading.Thread.start = refuse
    try:
        speaker.connect()
    finally:
        threading.Thread.start = original
    assert speaker.phase == FAILED and not speaker.busy
    assert speaker.note == "ERROR could not start: can't start new thread"
    assert fake.posts() == []


def test_locked_unit_cannot_connect_or_pair():
    speaker, ex, fake = make_speaker()
    ex.poll()
    app, _ = make_app(speaker, ex, locked=True)
    app.tick(wait=0.0)
    app.select("speaker")
    app.handle("key1")
    app.handle("key1_hold")
    app.handle("key3_hold")
    app.handle("key3_hold")
    speaker.join(0.2)
    assert app.screen is Screen.MENU
    assert fake.posts() == []


def test_a_plain_key3_blanks_and_a_held_key3_elsewhere_still_blanks():
    speaker, ex, fake = make_speaker()
    ex.poll()
    app, _ = make_app(speaker, ex)
    app.tick(wait=0.0)
    enter(app)
    app.handle("key3")
    assert app.blanked
    app.handle("key3")                           # wakes, nothing else
    assert not app.blanked and app.screen is Screen.SPEAKER
    speaker.join(0.2)
    assert fake.posts() == []
    app.handle("key2")
    app.handle("key3_hold")
    assert app.blanked


# ---- the unit that IS the Conductor ----

def test_the_follow_never_leaves_speaker_while_the_conductor_is_local():
    remote = FakeRemote()
    speaker, ex, fake = make_speaker(remote=remote)
    ex.poll()
    app, _ = make_app(speaker, ex, remote=remote)
    app.tick(wait=0.0)
    enter(app)
    # LOOP starts the next run while the operator re-pairs: the session
    # is armed under the screen, which must stay where it is.
    fake.run = {"t0": 0.0, "state": "running", "now": -5.0}
    remote.active = True
    ex.poll()
    for _ in range(3):
        app.tick(wait=0.0)
    assert app.screen is Screen.SPEAKER
    # KEY2 leaves the session alone, and the follow does not drag the
    # menu back to EXHIBITION.
    app.handle("key2")
    assert app.screen is Screen.MENU and remote.released == 0
    assert app._remote_dismissed is True
    for _ in range(3):
        app.tick(wait=0.0)
    assert app.screen is Screen.MENU
    # The port-taking rows are refused meanwhile; SPEAKER itself opens.
    app.select("standby")
    app.handle("key1")
    assert app._standby_status() == "conductor holds this unit - see EXHIBITION"
    enter(app)
    assert app.screen is Screen.SPEAKER
    app.handle("key2")
    remote.active = False
    app.tick(wait=0.0)
    assert app.screen is Screen.MENU and app._remote_dismissed is False


# ---- the screen ----

def test_the_screen_renders_every_state():
    bose = "Bose Flex SoundLink"
    cases = [
        ("ok", "READY", bose, "connected · vol 70%", ("", ""),
         "connected 12 min ago", "", "", "", False),
        ("ok", "DONE", bose, "connected · vol ?", ("", ""),
         "connected just now", "connected", "", "", False),
        ("ok", "READY", bose, "NOT CONNECTED",
         ("Permission denied - another phone?", "err"), "connected 12 min ago",
         "", "MUSIC LOST", "", False),
        ("ok", "READY", bose, "NOT CONNECTED", ("reconnect in 25 s (3 tries)", "warn"),
         "connected 2 h ago", "show running - hold KEY3 again to pair anyway",
         "MUSIC LOST", INSTRUCTION, False),
        ("ok", "READY", bose, "connecting…", ("", ""), "connected 3 d ago",
         "hold KEY3 again = pair", "", INSTRUCTION, False),
        ("ok", "BUSY", "no speaker paired", "pairing: scanning…",
         ("looking for the Bose", ""), "", "pairing started", "", INSTRUCTION,
         True),
        ("ok", "BUSY", bose, "NOT CONNECTED", ("", "err"), "never connected",
         "sending…", "MUSIC LOST", "", True),
        ("ok", "FAILED", bose, "NOT CONNECTED",
         ("pairing failed - no device found in pairing mode", "err"),
         "connected 12 min ago", "ERROR Connection refused", "", "", False),
        ("ok", "READY", "2C:41:A1:0B:7E:19", "?", ("", ""), "never connected",
         "", "", "", False),
        ("ok", "READY", bose, "connecting…", ("", ""), "connected 12 min ago",
         "connecting…", "", "", False),
        ("ok", "READY", bose, "NOT CONNECTED",
         ("Permission denied - another phone?", "err"), "connected 12 min ago",
         "show running - hold KEY3 again to pair anyway", "MUSIC LOST",
         INSTRUCTION, False),
        ("wired", "READY", WIRED, "vol 70%", ("", ""), "", "", "", "", False),
        ("wired", "DONE", WIRED, "vol ?", ("", ""), "", NOT_BLUETOOTH, "", "",
         False),
        ("old", "DONE", "speaker ?", "speaker ok · vol 70% (bluez)", ("", ""), "",
         "connect: not supported by this conductor", "", "", False),
        ("none", "READY", "", "", ("", ""), "", "", "", "", False),
        ("missing", "READY", "", "", ("", ""), "",
         "no conductor: Connection refused", "", "", False),
        ("checking", "READY", "", "", ("", ""), "", "", "", "", False),
    ]
    for (mode, word, device, state, detail, seen, status, banner, instruction,
         busy) in cases:
        for volume_keys in (False, True):
            image = render.speaker_screen(mode, word, device, state, detail, seen,
                                          status=status, banner=banner,
                                          instruction=instruction, busy=busy,
                                          volume_keys=volume_keys,
                                          host="radxa-05")
            assert image.size == (WIDTH, HEIGHT), mode
    assert render.speaker_screen("ok", "READY", bose, "connected · vol 70%",
                                 ("", ""), "", locked=True).size == (WIDTH, HEIGHT)
    # The banner, the state's colour and the hint are really drawn.
    quiet = render.speaker_screen("ok", "READY", bose, "connected · vol 70%",
                                  ("", ""), "", host="radxa-05")
    lost = render.speaker_screen("ok", "READY", bose, "NOT CONNECTED", ("", ""),
                                 "", host="radxa-05")
    banner = render.speaker_screen("ok", "READY", bose, "NOT CONNECTED", ("", ""),
                                   "", banner="MUSIC LOST", host="radxa-05")
    assert quiet.tobytes() != lost.tobytes() != banner.tobytes()
    assert banner.getpixel((12, 132)) == render.ERR     # inside the strip
    assert lost.getpixel((12, 132)) == render.BG
    keyed = render.speaker_screen("ok", "READY", bose, "connected · vol 70%",
                                  ("", ""), "", host="radxa-05", volume_keys=True)
    assert quiet.tobytes() != keyed.tobytes()
    # Instruction + two-line status + banner: the status sits above the
    # strip, nothing of it under the red (review of 73c8fdc, LOW-6) -
    # the rows just above the strip are the divider and black only.
    crowded = render.speaker_screen(
        "ok", "READY", bose, "NOT CONNECTED", ("reconnect in 25 s (3 tries)", "warn"),
        "connected 12 min ago", status="show running - hold KEY3 again to pair anyway",
        banner="MUSIC LOST", instruction=INSTRUCTION, host="radxa-05")
    for y in range(119, 122):
        assert all(crowded.getpixel((x, y)) == render.BG for x in range(8, 232)), y
    status_rows = [y for y in range(88, 118)
                   if any(crowded.getpixel((x, y)) == render.WARN
                          for x in range(8, 232))]
    assert status_rows and min(status_rows) >= 88 and max(status_rows) <= 117
    # The wired screen has no hold keys and a volume hint.
    wired = render.speaker_screen("wired", "READY", WIRED, "vol 70%", ("", ""), "",
                                  host="radxa-05", volume_keys=True)
    assert wired.tobytes() != quiet.tobytes()
    # The EXHIBITION line goes red when the link is lost.
    show = ("AZ_show_2026", "18 cues · 10:54")
    ok = render.exhibition_screen(True, show, "idle", "units 7/7 online",
                                  "LOOP off", "speaker ok · vol 70% (bluez)", "idle")
    gone = render.exhibition_screen(True, show, "idle", "units 7/7 online",
                                    "LOOP off", SPEAKER_LOST_TEXT, "idle")
    assert ok.tobytes() != gone.tobytes()


def test_the_app_draws_speaker_and_repaints_only_on_change():
    speaker, ex, fake = make_speaker()
    ex.poll()
    app, _ = make_app(speaker, ex)
    app.tick(wait=0.0)
    enter(app)
    app.draw()
    key = app._display_key()
    assert key[0] == "speaker"
    frames = app.display.frames
    app.tick(wait=0.0)
    app.tick(wait=0.0)
    assert app.display.frames == frames          # idle: no repaint
    fake.connection = "disconnected"
    fake.run = {"t0": 0.0, "state": "running", "now": 3.0}
    ex.poll()                                    # the reader landed a change
    app.tick(wait=0.0)
    assert app.display.frames == frames + 1
    assert app._display_key() != key
    assert speaker.banner() == "MUSIC LOST"
    # The menu repaints when the row's label flips.
    app.handle("key2")
    app.draw()
    menu_key = app._display_key()
    fake.no_speaker = True
    ex.poll()
    app.tick(wait=0.0)
    assert app._display_key() != menu_key


def test_preview_writes_the_speaker_screens(tmp_path):
    from ui.main import preview

    assert preview(str(tmp_path)) == 0
    for name in ("speaker_connected.png", "speaker_lost.png",
                 "speaker_pairing.png", "speaker_reconnect.png",
                 "speaker_none.png", "speaker_wired.png",
                 "exhibition_speaker_lost.png"):
        assert (tmp_path / name).is_file(), name
