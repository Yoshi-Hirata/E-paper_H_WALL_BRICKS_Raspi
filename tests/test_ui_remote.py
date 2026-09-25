"""Remote cues on the unit: ui/remote.py, ui/agent.py, runner remote mode.

The serial bus is the FakeBus of tests/test_ui_runner.py; the agent is
the real HTTP server on an ephemeral localhost port, so what is tested
is what the show PC will talk to.
"""

from __future__ import annotations

import json
import struct
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

from epaper.protocol import Frame
from ui import render
from ui.agent import Agent
from ui.app import App, Screen
from ui.config import HEIGHT, WIDTH
from ui.display import NullDisplay
from ui.inputs import ScriptedInput
from ui.patterns import BY_KEY
from ui.remote import (ARMED, FAILED, FIRED, LOCAL, READY, STANDBY,
                       RemoteError, RemoteSession)
from ui.runner import NO_DELAY
from tests.test_ui_runner import FakeBus, make_runner, wait_until

SAVE, SHOW, STOP, CFG = 0x13, 0x1D, 0x17, 0x1B


def array(color: int) -> bytes:
    return bytes([0xFE] + [color] * 60 + [0xFF, 0xFF, 0xFE])


def make_session(bus=None, **kwargs):
    bus = bus or FakeBus()
    runner = make_runner(bus, **kwargs)
    return RemoteSession(runner), runner, bus


def shows(bus):
    return [f for f in bus.sent if f.cmd == SHOW]


# ---- prepare, then fire ----

def test_prepare_saves_every_board_and_shows_nothing():
    session, runner, bus = make_session()
    session.prepare("c1", {1: array(3), 2: array(4), 3: array(5)}, label="Look22 P01")
    assert wait_until(lambda: session.phase == READY)
    saves = [f for f in bus.requested if f.cmd == SAVE]
    assert [f.dest for f in saves] == [1, 2, 3]
    assert saves[0].data[2:] == array(3)            # [slot][flags][64 bytes]
    assert all(f.dev_type == 0x03 for f in saves)
    assert shows(bus) == []                         # nothing on the glass yet
    status = session.status()
    assert status["saved"] == [1, 2, 3] and status["failed"] == []
    assert status["boards"] == [1, 2, 3] and status["active"]
    assert status["prepare_s"] is not None
    assert runner.remote is session
    runner.stop()


def test_fire_sends_one_show_at_the_named_instant():
    session, runner, bus = make_session()
    session.prepare("c1", {1: array(3), 2: array(4)})
    assert wait_until(lambda: session.phase == READY)
    at = time.monotonic() + 0.30
    session.fire("c1", at)
    assert session.phase == ARMED and shows(bus) == []
    assert wait_until(lambda: session.phase == FIRED)
    assert len(shows(bus)) == 1 and shows(bus)[0].dest == 0xFF
    status = session.status()
    assert status["fired_at"] >= at                 # never early
    assert 0 <= status["late_ms"] < 50
    runner.stop()


def test_fire_time_may_arrive_while_the_boards_are_still_loading():
    session, runner, bus = make_session()
    session.prepare("c1", {1: array(1)})
    session.fire("c1", time.monotonic() + 0.2)      # before READY
    assert wait_until(lambda: session.phase == FIRED)
    assert len(shows(bus)) == 1
    runner.stop()


def test_a_fire_time_already_past_fires_at_once_and_says_how_late():
    session, runner, bus = make_session()
    session.prepare("c1", {1: array(1)})
    assert wait_until(lambda: session.phase == READY)
    session.fire("c1", time.monotonic() - 1.5)
    assert wait_until(lambda: session.phase == FIRED)
    assert session.status()["late_ms"] >= 1500
    runner.stop()


def test_prepare_refuses_to_displace_a_cue_about_to_fire():
    # Found in the timing review: _fire_at() (ui/runner.py) sends the
    # broadcast and only then calls session.fired(cue_id, ...); fired()
    # matches on cue_id, so a prepare() landing in that gap moves cue_id
    # on first and the fire is never tallied - applied stays stale and
    # the fire silently never happened as far as the session is concerned.
    session, runner, bus = make_session()
    session.prepare("c1", {1: array(1)})
    assert wait_until(lambda: session.phase == READY)
    session.fire("c1", time.monotonic() + 0.02)      # inside FIRE_IMMINENT_S
    with pytest.raises(RemoteError, match="about to fire"):
        session.prepare("c2", {1: array(2)})
    assert session.cue_id == "c1" and session.phase == ARMED   # not displaced
    assert wait_until(lambda: session.phase == FIRED)
    assert len(shows(bus)) == 1
    # Once it has actually fired, a new prepare is not blocked.
    session.prepare("c2", {1: array(2)})
    assert wait_until(lambda: session.phase == READY and session.cue_id == "c2")
    runner.stop()


def test_prepare_is_not_blocked_before_a_fire_time_is_even_set():
    session, runner, bus = make_session()
    session.prepare("c1", {1: array(1)})
    assert wait_until(lambda: session.phase == READY)      # no fire() yet
    session.prepare("c2", {1: array(2)})                   # not ARMED: fine
    assert wait_until(lambda: session.phase == READY and session.cue_id == "c2")
    runner.stop()


def test_cancel_disarms_and_a_new_time_rearms():
    session, runner, bus = make_session()
    session.prepare("c1", {1: array(1)})
    assert wait_until(lambda: session.phase == READY)
    session.fire("c1", time.monotonic() + 0.4)
    session.cancel()
    assert session.phase == READY
    time.sleep(0.6)
    assert shows(bus) == []
    session.fire("c1", time.monotonic() + 0.1)
    assert wait_until(lambda: session.phase == FIRED)
    assert len(shows(bus)) == 1
    runner.stop()


def test_a_second_cue_reuses_the_bus_and_the_known_boards():
    session, runner, bus = make_session()
    session.prepare("c1", {1: array(1), 2: array(1)})
    assert wait_until(lambda: session.phase == READY)
    probes = len([f for f in bus.requested if f.cmd == CFG])
    session.prepare("c2", {1: array(2), 2: array(2)})
    assert wait_until(lambda: session.phase == READY and session.cue_id == "c2")
    assert len([f for f in bus.requested if f.cmd == CFG]) == probes   # no re-setup
    with pytest.raises(RemoteError):
        session.fire("c1", time.monotonic())        # the old cue is gone
    runner.stop()


def test_a_different_garment_re_probes_its_own_boards():
    session, runner, bus = make_session()
    session.prepare("c1", {1: array(1), 2: array(1)})
    assert wait_until(lambda: session.phase == READY)
    session.prepare("c2", {1: array(2), 2: array(2), 3: array(2), 4: array(2)})
    assert wait_until(lambda: session.phase == READY and session.cue_id == "c2")
    assert runner.boards == [1, 2, 3, 4] and runner.live == [1, 2, 3, 4]
    assert session.status()["saved"] == [1, 2, 3, 4]
    runner.stop()


def test_sockets_already_known_empty_get_one_probe_not_three():
    bus = PickyBus({2, 3})
    session, runner, _ = make_session(bus)
    session.prepare("c1", {1: array(1), 2: array(1), 3: array(1)})
    assert wait_until(lambda: session.phase == READY)
    first = len([f for f in bus.requested if f.dest == 2 and f.cmd == STOP])
    assert first == 3                               # unknown: every sweep
    session.prepare("c2", {1: array(2), 2: array(2)})     # a different list
    assert wait_until(lambda: session.phase == READY and session.cue_id == "c2")
    again = len([f for f in bus.requested if f.dest == 2 and f.cmd == STOP])
    assert again - first == 1                       # known empty: one look
    assert session.status()["saved"] == [1] and session.status()["failed"] == [2]
    runner.stop()


# ---- faults ----

class PickyBus(FakeBus):
    """Boards in `silent` never answer anything."""

    def __init__(self, silent):
        super().__init__()
        self.silent = set(silent)

    def request(self, frame, retries=3):
        if frame.dest in self.silent:
            self.requested.append(frame)
            return None
        return super().request(frame, retries)


def test_a_missing_board_is_reported_and_the_rest_still_fire():
    session, runner, bus = make_session(PickyBus({2}))
    session.prepare("c1", {1: array(1), 2: array(1), 3: array(1)})
    assert wait_until(lambda: session.phase == READY)
    status = session.status()
    assert status["saved"] == [1, 3] and status["failed"] == [2]
    assert status["live"] == [1, 3]
    session.fire("c1", time.monotonic() + 0.05)
    assert wait_until(lambda: session.phase == FIRED)
    runner.stop()


def test_no_board_at_all_is_a_failed_cue():
    session, runner, bus = make_session(PickyBus({1, 2}))
    session.prepare("c1", {1: array(1), 2: array(1)})
    assert wait_until(lambda: session.phase == FAILED)
    assert session.status()["error"]
    assert shows(bus) == []
    runner.stop()


def test_bad_requests_are_refused_before_touching_the_port():
    session, runner, bus = make_session()
    for boards in ({}, {0: array(1)}, {300: array(1)}, {1: b"short"}):
        with pytest.raises(RemoteError):
            session.prepare("c1", boards)
    with pytest.raises(RemoteError):
        session.fire("nope", time.monotonic())
    session.busy = lambda: True
    with pytest.raises(RemoteError):
        session.prepare("c1", {1: array(1)})
    with pytest.raises(RemoteError):
        session.standby()
    assert not runner.running and bus.requested == []


def test_the_guard_stop_follows_a_fire_unless_a_new_cue_comes_first():
    session, runner, bus = make_session(guard_delay=0.15)
    session.prepare("c1", {1: array(1)})
    assert wait_until(lambda: session.phase == READY)
    before = len([f for f in bus.sent if f.cmd == STOP and f.dest == 0xFF])
    session.fire("c1", time.monotonic() + 0.02)
    assert wait_until(lambda: session.phase == FIRED)
    assert wait_until(lambda: len([f for f in bus.sent if f.cmd == STOP
                                   and f.dest == 0xFF]) == before + 1)
    runner.stop()


def guards(bus):
    return len([f for f in bus.sent if f.cmd == STOP and f.dest == 0xFF])


def test_the_guard_stop_is_sized_from_the_cues_own_refresh_and_span():
    """F1, 2026-09-26: the guard was a flat 12 s after every fire, which
    is one 7 s refresh plus 5 s. A sweep is not finished at the refresh -
    its last scale only STARTS at the span, and a span may be 30 s - so
    the broadcast STOP could land inside the change."""
    runner = make_runner(FakeBus(), guard_delay=12.0)

    class Cue:
        span_s = refresh_s = None

    cue = Cue()
    assert runner._guard_for(cue) == 12.0           # an old body: unchanged
    cue.span_s, cue.refresh_s = 0.0, 7.0
    assert runner._guard_for(cue) == 12.0           # no sweep: also unchanged
    cue.span_s = 3.0
    assert runner._guard_for(cue) == 15.0           # 7 + 3 + the same 5 margin
    cue.span_s, cue.refresh_s = 30.0, 16.0          # MAX_DELAY_S on a slow panel
    assert runner._guard_for(cue) == 51.0
    cue.refresh_s = None                            # span alone: 7 s assumed
    assert runner._guard_for(cue) == 42.0
    cue.span_s, cue.refresh_s = 0.0, 1.0            # never EARLIER than before
    assert runner._guard_for(cue) == 12.0
    # ...and never so late that the guard is effectively off: nothing
    # real gets near GUARD_MAX_S (120 s span over a 60 s refresh is the
    # honest worst case), but a wild number must not silently hand the
    # wall back to the factory autoplay.
    cue.span_s, cue.refresh_s = 5000.0, 60.0
    assert runner._guard_for(cue) == 200.0


def test_a_swept_cue_holds_the_guard_stop_off_until_the_sweep_is_over():
    session, runner, bus = make_session(guard_delay=0.15)
    session.prepare("c1", {1: array(1)}, span_s=0.3, refresh_s=0.2)
    assert wait_until(lambda: session.phase == READY)
    before = guards(bus)
    session.fire("c1", time.monotonic() + 0.02)
    assert wait_until(lambda: session.phase == FIRED)
    fired = session.fired_at
    assert wait_until(lambda: guards(bus) == before + 1)
    # refresh 0.2 + span 0.3, not the flat 0.15 s this runner was given.
    assert time.monotonic() - fired >= 0.5 - 0.02
    runner.stop()


def test_a_cue_with_no_sweep_keeps_the_flat_guard():
    session, runner, bus = make_session(guard_delay=0.15)
    session.prepare("c1", {1: array(1)}, span_s=0.0, refresh_s=0.05)
    assert wait_until(lambda: session.phase == READY)
    before = guards(bus)
    session.fire("c1", time.monotonic() + 0.02)
    assert wait_until(lambda: session.phase == FIRED)
    fired = session.fired_at
    assert wait_until(lambda: guards(bus) == before + 1)
    assert time.monotonic() - fired < 1.0
    runner.stop()


def test_junk_span_and_refresh_are_read_as_not_said():
    """Advisory numbers: a cue is never refused over one, and the guard
    falls back to the flat delay rather than to something nonsensical."""
    session, runner, bus = make_session()
    for span, refresh in (("soon", -4), (float("nan"), {}),
                          (float("inf"), float("inf"))):
        session.prepare("c1", {1: array(1)}, span_s=span, refresh_s=refresh)
        assert wait_until(lambda: session.phase == READY)
        assert session.span_s is None and session.refresh_s is None
        assert runner._guard_for(session) == runner.guard_delay
    runner.stop()


def test_the_sweep_log_names_the_span_when_this_board_falls_short_of_it():
    """radxa-01, 2026-09-26: a centre sweep of a 3 s span logged "last
    starts +2.44 s" on every board, because a garment's farthest scales
    sit on some OTHER board. The line says so now."""
    bus = FakeBus()
    runner = make_runner(bus)
    frames = [NO_DELAY] * 64
    frames[1], frames[2] = 0, 244
    assert runner._save_delays(bus, 20, 7, struct.pack(">64H", *frames),
                               dev_type=3, span_s=3.0)
    line = [m for m in runner.log if "sweep table saved" in m][-1]
    assert "last starts +2.44 s of a 3.00 s span" in line
    assert "farthest scales are on other boards" in line
    # The board that does carry the last scale says only what it did.
    assert runner._save_delays(bus, 20, 8, table(300), dev_type=3, span_s=3.0)
    line = [m for m in runner.log if "sweep table saved" in m][-1]
    assert line.endswith("last starts +3.00 s")
    # And a caller that never said a span says nothing either.
    assert runner._save_delays(bus, 20, 9, table(244), dev_type=3)
    line = [m for m in runner.log if "sweep table saved" in m][-1]
    assert line.endswith("last starts +2.44 s")
    runner.stop()


def test_standby_and_release_hand_the_unit_over_and_back():
    session, runner, bus = make_session()
    session.standby()
    assert session.phase == STANDBY and session.active
    assert wait_until(lambda: runner.standby_ready)
    assert runner.remote is None                    # the white is a pattern
    session.prepare("c1", {1: array(1)})            # ...and a cue takes over
    assert wait_until(lambda: session.phase == READY)
    session.release()
    assert session.phase == LOCAL and not session.active
    assert not runner.running
    runner.start(BY_KEY["solid"])                   # the local menu works again
    assert wait_until(lambda: runner.cycle >= 1)
    runner.stop()


# ---- the agent over HTTP ----

@pytest.fixture
def agent():
    session, runner, bus = make_session()
    agent = Agent(session, port=0, host="127.0.0.1", commit="abc1234",
                  name="radxa-03")
    agent.start()
    yield agent, session, runner, bus
    agent.stop()
    runner.stop()


def call(agent, path, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(f"http://127.0.0.1:{agent.port}{path}",
                                     data=data)
    if token:
        request.add_header("X-Show-Token", token)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_status_names_the_unit_and_serves_the_clock(agent):
    agent, session, runner, bus = agent
    before = time.monotonic()
    code, status = call(agent, "/status")
    after = time.monotonic()
    assert code == 200
    assert status["host"] == "radxa-03" and status["commit"] == "abc1234"
    assert status["phase"] == LOCAL and status["active"] is False
    assert before <= status["clock"]["mono"] <= after
    code, clock = call(agent, "/clock")
    assert code == 200 and set(clock) == {"mono", "wall"}


def test_prepare_and_fire_over_http(agent):
    agent, session, runner, bus = agent
    code, status = call(agent, "/prepare", {
        "cue": "c7", "label": "Look22 P02", "dev_type": 3,
        "boards": {"1": array(3).hex(), "2": array(4).hex()}})
    assert code == 200 and status["phase"] in ("preparing", "ready")
    assert wait_until(lambda: call(agent, "/status")[1]["phase"] == "ready")
    unit_now = call(agent, "/clock")[1]["mono"]
    code, status = call(agent, "/fire", {"cue": "c7", "at": unit_now + 0.2})
    assert code == 200 and status["phase"] == "armed"
    assert wait_until(lambda: call(agent, "/status")[1]["phase"] == "fired")
    status = call(agent, "/status")[1]
    assert status["label"] == "Look22 P02" and 0 <= status["late_ms"] < 50
    assert len(shows(bus)) == 1
    assert any("fired" in line for line in status["log"])
    assert call(agent, "/release", {})[1]["phase"] == LOCAL


def test_http_errors_are_answers_not_crashes(agent):
    agent, session, runner, bus = agent
    assert call(agent, "/nowhere")[0] == 404
    assert call(agent, "/fire", {"cue": "zz", "at": 1.0})[0] == 409
    assert call(agent, "/prepare", {"cue": "c1"})[0] == 400          # no boards
    assert call(agent, "/prepare", {"cue": "c1", "boards": {"1": "zz"}})[0] == 400
    assert call(agent, "/prepare", {"cue": "c1", "boards": {"1": "00"}})[0] == 409
    assert call(agent, "/status")[0] == 200                         # still up


def test_a_token_when_set_is_required_everywhere():
    session, runner, bus = make_session()
    agent = Agent(session, port=0, host="127.0.0.1", token="s3cret")
    agent.start()
    try:
        assert call(agent, "/status")[0] == 401
        assert call(agent, "/status", token="wrong")[0] == 401
        assert call(agent, "/standby", {}, token=None)[0] == 401
        assert call(agent, "/status", token="s3cret")[0] == 200
        assert not runner.running
    finally:
        agent.stop()


# ---- the LCD follows ----

def make_app(session, runner, **kwargs):
    return App(NullDisplay(), ScriptedInput(()), runner, remote=session,
               host="radxa-03", **kwargs)


def test_the_screen_follows_who_drives_the_unit():
    session, runner, bus = make_session()
    app = make_app(session, runner)
    app.tick(wait=0.0)
    assert app.screen is Screen.MENU
    session.prepare("c1", {1: array(1)}, label="Look22 P01")
    app.tick(wait=0.0)
    assert app.screen is Screen.REMOTE
    assert wait_until(lambda: session.phase == READY)
    before = app.display.frames
    app.tick(wait=0.0)
    assert app.display.frames == before + 1          # READY repaints
    for event in ("key1", "up", "press", "key1_hold"):
        app.handle(event)                            # only KEY2 leaves
    assert app.screen is Screen.REMOTE and runner.remote is session
    app.handle("key2")
    assert app.screen is Screen.MENU and not session.active
    assert not runner.running


def test_release_from_the_pc_returns_the_menu():
    session, runner, bus = make_session()
    app = make_app(session, runner)
    session.prepare("c1", {1: array(1)})
    app.tick(wait=0.0)
    assert app.screen is Screen.REMOTE
    session.release()
    app.tick(wait=0.0)
    assert app.screen is Screen.MENU


def test_a_locked_unit_cannot_be_knocked_out_of_the_show():
    session, runner, bus = make_session()
    app = make_app(session, runner, locked=True)
    session.prepare("c1", {1: array(1)})
    app.tick(wait=0.0)
    app.handle("key2")
    assert app.screen is Screen.REMOTE and session.active
    runner.stop()


def test_busy_workers_keep_the_pc_out():
    class Busy:
        busy = True
        menu_entry = BY_KEY["solid"]

    session, runner, bus = make_session()
    app = make_app(session, runner, updater=Busy())
    with pytest.raises(RemoteError):
        session.prepare("c1", {1: array(1)})
    assert app.screen is Screen.MENU and not runner.running


def test_remote_screen_renders_every_phase():
    base = {"cue": "c1", "label": "Look22 P02", "boards": [1, 2], "live": [1, 2],
            "saved": [1, 2], "failed": [], "error": None, "fire_at": None,
            "fired_at": None, "late_ms": None, "standby_ready": True}
    for phase in ("preparing", "ready", "armed", "fired", "failed", "standby"):
        status = dict(base, phase=phase)
        if phase == "armed":
            status["fire_at"] = 12.0
        if phase == "fired":
            status.update(fire_at=12.0, fired_at=12.004, late_ms=4.0)
        if phase == "failed":
            status.update(saved=[], failed=[1, 2], error="no board took it")
        image = render.remote_screen(status, ["13:00:00 x"], now=10.0,
                                     host="radxa-03")
        assert image.size == (WIDTH, HEIGHT)


# ---- found in review (2026-09-21) ----

def test_a_worker_that_will_not_stop_is_never_joined_by_a_second_one(monkeypatch):
    """Two workers on one bus would each send the broadcast show."""
    import threading

    from ui import runner as runner_mod

    monkeypatch.setattr(runner_mod, "OLD_WORKER_PATIENCE_S", 0.3)
    bus = FakeBus()
    release, entered = threading.Event(), threading.Event()
    request = bus.request

    def wedged(frame, retries=3):
        if frame.cmd == SAVE and not release.is_set():
            entered.set()
            release.wait(5.0)               # a CDC that stopped draining
        return request(frame, retries)
    bus.request = wedged
    session, runner, _ = make_session(bus)
    session.prepare("c1", {1: array(1)})
    assert entered.wait(5.0)
    runner.stop(timeout=0.2)                # gives up waiting for it
    assert runner._lingering is not None and runner._lingering.is_alive()
    assert runner._stop.is_set()

    session.prepare("c2", {1: array(2)})    # must not start a second worker
    assert session.phase == FAILED and "bus busy" in session.status()["error"]
    assert runner._stop.is_set() and not runner.running

    release.set()                           # the old worker gets its answer...
    assert wait_until(lambda: not runner._lingering.is_alive())
    session.prepare("c3", {1: array(3)})    # ...and now the bus is free
    assert wait_until(lambda: session.phase == READY)
    session.fire("c3", time.monotonic() + 0.05)
    assert wait_until(lambda: session.phase == FIRED)
    assert len(shows(bus)) == 1
    runner.stop()


def test_standby_and_prepare_at_the_same_time_leave_one_worker():
    import threading

    session, runner, bus = make_session()
    for _ in range(5):
        a = threading.Thread(target=session.standby)
        b = threading.Thread(target=session.prepare,
                             args=("c1", {1: array(1)}))
        a.start(); b.start(); a.join(); b.join()
        assert wait_until(lambda: runner.running)
        # Whichever came last owns the port, and it is a consistent state.
        assert (runner.remote is session) == (runner.pattern is None)
    runner.stop()
    assert runner.error is None or "AttributeError" not in runner.error


def test_agent_answers_a_malformed_body_instead_of_dying(agent):
    agent, session, runner, bus = agent
    assert call(agent, "/prepare", {"cue": "x", "boards": ["aa"]})[0] == 400
    assert call(agent, "/show/load", {"id": "s", "cues": ["q"], "refresh_s": 7,
                                      "duration": 60})[0] in (400, 409)
    assert call(agent, "/status")[0] == 200


def test_agent_connections_time_out_instead_of_leaking_threads():
    from ui.agent import _Handler

    assert _Handler.timeout and _Handler.timeout <= 30


# ---- sweeps: a delay table per board, before the colours ----
# Tables are now 64 sockets of uint16, big-endian (V1.4 7.4, 10 ms frames):
# 128 bytes, NO_DELAY = 0xFFFF. `table(value)` puts `value` frames on every
# socket but the two that never carry a scale.

DELAY, CLEAR = 0x1F, 0x25


def table(value: int) -> bytes:
    return struct.pack(">64H", *([NO_DELAY] + [value] * 62 + [NO_DELAY]))


def all_no_delay() -> bytes:
    return struct.pack(">64H", *([NO_DELAY] * 64))


def test_a_delay_table_of_the_wrong_length_is_refused():
    # 64 bytes was the table's old (0.1 s, one byte a socket) length; it
    # is refused now that a table is 128 bytes of uint16 frames.
    session, runner, bus = make_session()
    with pytest.raises(RemoteError):
        session.prepare("c1", {1: array(3)}, delays={1: bytes([0xFF] * 64)})
    assert session.phase == LOCAL
    runner.stop()


def test_delay_tables_go_out_before_the_colours_and_only_when_they_change():
    session, runner, bus = make_session()
    session.prepare("c1", {1: array(3), 2: array(4)}, delays={1: table(20), 2: table(50)})
    assert wait_until(lambda: session.phase == READY)
    cmds = [(f.cmd, f.dest) for f in bus.requested if f.cmd in (DELAY, SAVE)]
    assert cmds == [(DELAY, 1), (DELAY, 1), (SAVE, 1), (DELAY, 2), (DELAY, 2), (SAVE, 2)]
    low, high = [f for f in bus.requested if f.cmd == DELAY][:2]
    # 20 frames (0.2 s) a socket: low bytes 20, high bytes 0, "last" on the
    # high frame. Sockets 0 and 63 carry no scale, so they get the sweep's
    # LAST frame (here the same 20) rather than frame 0 - see _save_delays().
    assert low.data == bytes([19, 0]) + bytes([20] * 64)
    assert high.data == bytes([19, 0x03]) + bytes(64)
    # The same tables again: not written again. A new one for board 2 is.
    n = len(bus.requested)
    session.prepare("c2", {1: array(6), 2: array(7)}, delays={1: table(20), 2: table(90)})
    assert wait_until(lambda: session.phase == READY and session.cue_id == "c2")
    later = [(f.cmd, f.dest) for f in bus.requested[n:] if f.cmd in (DELAY, SAVE)]
    assert later == [(SAVE, 1), (DELAY, 2), (DELAY, 2), (SAVE, 2)]
    assert session.status()["no_sweep"] == []
    # A table of "no delay" everywhere is 0x25: the board forgets the sweep.
    n = len(bus.requested)
    session.prepare("c3", {1: array(8), 2: array(9)}, delays={1: all_no_delay(), 2: table(90)})
    assert wait_until(lambda: session.phase == READY and session.cue_id == "c3")
    later = [(f.cmd, f.dest) for f in bus.requested[n:] if f.cmd in (DELAY, CLEAR, SAVE)]
    assert later == [(CLEAR, 1), (SAVE, 1), (SAVE, 2)]
    runner.stop()


def test_a_board_whose_firmware_has_no_sweeps_still_gets_the_cue():
    class OldFirmware(FakeBus):
        def request(self, frame, retries=3):
            ack = super().request(frame, retries)
            if frame.cmd == DELAY:
                ack.cmd = 0x83                      # ACK_INVALID_CMD
            return ack

    session, runner, bus = make_session(OldFirmware())
    session.prepare("c1", {1: array(3)}, delays={1: table(10)})
    assert wait_until(lambda: session.phase == READY)
    assert session.saved == [1] and session.failed == []
    assert session.status()["no_sweep"] == [1]
    session.prepare("c2", {1: array(4)}, delays={1: table(20)})
    assert wait_until(lambda: session.phase == READY and session.cue_id == "c2")
    assert [f.cmd for f in bus.requested].count(DELAY) == 1   # asked once
    runner.stop()


def test_agent_passes_delays_through(agent):
    agent, session, runner, bus = agent
    status, body = call(agent, "/prepare", {"cue": "x", "boards": {"1": array(2).hex()},
                                            "delays": {"1": table(30).hex()}})
    assert status == 200, body
    assert wait_until(lambda: session.phase == READY)
    assert any(f.cmd == DELAY and f.data[2:] == bytes([30] * 64)
               for f in bus.requested)


def test_frames_are_sent_as_uint16_low_then_high():
    bus = FakeBus()
    runner = make_runner(bus)
    # 300 = 0x012C: low byte 0x2C, high byte 0x01 - needs both frames.
    wide = struct.pack(">64H", *([NO_DELAY] + [300] * 62 + [NO_DELAY]))
    assert runner._save_delays(bus, 20, 7, wide, dev_type=3)
    low, high = [f for f in bus.requested if f.cmd == DELAY][:2]
    assert low.data[2:] == bytes([0x2C] * 64)
    assert high.data[2:] == bytes([0x01] * 64)
    runner.stop()


def test_an_unused_socket_starts_with_the_last_scale_not_at_t0():
    """A socket with no scale on it gets max(frames), not 0 (F4,
    2026-09-26). On correct firmware the value is ignored either way -
    the board has no segment there - but 0 means "repaint at T0", which
    on a firmware that ever did act on it is a flash at the wrong end of
    the garment, while the last frame is invisible behind the sweep."""
    bus = FakeBus()
    runner = make_runner(bus)
    # Sockets 1..3 sweep at 0 / 50 / 120 frames; everything else is unused.
    frames = [NO_DELAY] * 64
    frames[1], frames[2], frames[3] = 0, 50, 120
    assert runner._save_delays(bus, 20, 7, struct.pack(">64H", *frames), dev_type=3)
    low = [f for f in bus.requested if f.cmd == DELAY][0]
    sent = low.data[2:]
    assert (sent[1], sent[2], sent[3]) == (0, 50, 120)   # the real scales
    assert sent[0] == sent[4] == sent[63] == 120         # the unused sockets
    runner.stop()


def test_a_table_of_no_delay_everywhere_clears_the_pipeline():
    bus = FakeBus()
    runner = make_runner(bus)
    assert runner._save_delays(bus, 20, 3, all_no_delay(), dev_type=3)
    assert [f.cmd for f in bus.requested] == [CLEAR]
    runner.stop()


class FailOnceBus(FakeBus):
    """ACKs everything except the one frame it is told to fail, once."""

    def __init__(self, fail_cmd: int, board: int):
        super().__init__()
        self.fail_cmd, self.board = fail_cmd, board
        self.failed_once = False

    def request(self, frame, retries=3):
        self.requested.append(frame)
        if (not self.failed_once and frame.cmd == self.fail_cmd
                and frame.dest == self.board):
            self.failed_once = True
            return None
        return Frame(dest=0x00, src=frame.dest, dev_type=0xFF, cmd=self.ack_cmd)


def test_a_failed_save_forgets_the_boards_delay_table_so_it_is_resent():
    # save_color failing after _save_delays already succeeded used to
    # leave _delays_sent pointing at a table the board may no longer
    # hold once it is reconfigured (slot_config resets it) - the retry
    # would then wrongly skip resending it.
    bus = FailOnceBus(SAVE, 1)
    runner = make_runner(bus, save_attempts=1, command_attempts=1)
    runner.live = [1]
    job = {"dev_type": 3, "boards": {1: array(3)}, "delays": {1: table(20)}}
    try:
        saved, failed = runner._save_cue(bus, 20, job)
        assert failed == [1] and saved == []
        assert (1, 19) not in runner._cfg_done       # every slot re-verified
        assert (1, 19) not in runner._delays_sent    # forgotten, not stale

        saved2, failed2 = runner._save_cue(bus, 20, job)
        assert saved2 == [1] and failed2 == []
        delay_frames = [f for f in bus.requested if f.cmd == DELAY and f.dest == 1]
        assert len(delay_frames) == 4                # 2 (low + high), twice
    finally:
        runner.stop()


# ---- pre-burn (2026-09-24): manual cues stay slot 19; a show burns 1..19 ----

def test_no_per_board_stop_in_the_manual_save_path():
    # docs/MERIS_REPLY_3SLOT.pdf: 0x13 is pure storage, never needs the
    # board silenced first. Setup/probing still sends a per-board stop
    # (liveness, unrelated to the save path) - so what is checked is
    # that a SECOND save adds none beyond what setup already sent once.
    session, runner, bus = make_session()
    session.prepare("c1", {1: array(1), 2: array(1), 3: array(1)})
    assert wait_until(lambda: session.phase == READY)
    before = len([f for f in bus.requested if f.cmd == STOP and f.dest != 0xFF])
    session.prepare("c2", {1: array(2), 2: array(2), 3: array(2)})
    assert wait_until(lambda: session.phase == READY and session.cue_id == "c2")
    after = len([f for f in bus.requested if f.cmd == STOP and f.dest != 0xFF])
    assert after == before
    runner.stop()


def test_one_broadcast_stop_when_the_worker_takes_the_port():
    session, runner, bus = make_session()
    session.prepare("c1", {1: array(1)})
    assert wait_until(lambda: session.phase == READY)
    assert len([f for f in bus.sent if f.cmd == STOP and f.dest == 0xFF]) == 1
    session.prepare("c2", {1: array(2)})
    assert wait_until(lambda: session.phase == READY and session.cue_id == "c2")
    assert len([f for f in bus.sent if f.cmd == STOP and f.dest == 0xFF]) == 1
    runner.stop()


def test_arm_needs_no_boards_and_fires_the_given_slot():
    session, runner, bus = make_session()
    session.arm("c1", 5, dev_type=3, label="Look 1")
    assert session.phase == READY and session.slot == 5
    session.fire("c1", time.monotonic() + 0.05)
    assert wait_until(lambda: session.phase == FIRED)
    shows_ = shows(bus)
    assert len(shows_) == 1 and shows_[0].data[0] == 5
    assert [f for f in bus.requested if f.cmd == SAVE] == []   # nothing written
    runner.stop()


def test_standby_paints_slot_0():
    session, runner, bus = make_session()
    session.standby()
    assert wait_until(lambda: runner.standby_ready)
    shows_ = shows(bus)
    assert shows_ and all(f.data[0] == 0 for f in shows_)
    runner.stop()


def test_burn_writes_every_cue_to_its_own_slot_in_order():
    session, runner, bus = make_session()
    cues = [{"slot": 1, "boards": {1: array(1), 2: array(1)}, "delays": {}},
            {"slot": 2, "boards": {1: array(2), 2: array(2)}, "delays": {}}]
    session.burn(cues, dev_type=3)
    assert wait_until(lambda: (session.burn_status() or {}).get("state")
                      == "burned")
    saves = [f for f in bus.requested if f.cmd == SAVE]
    assert [(f.dest, f.data[0], f.data[3]) for f in saves] == [
        (1, 1, 1), (2, 1, 1), (1, 2, 2), (2, 2, 2)]
    status = session.burn_status()
    assert status["done"] == 4 and status["total"] == 4 and status["failed"] == []
    runner.stop()


def test_burn_reports_progress_as_it_goes():
    session, runner, bus = make_session()
    cues = [{"slot": n, "boards": {b: array(1) for b in range(1, 4)},
            "delays": {}} for n in range(1, 4)]
    session.burn(cues, dev_type=3)
    assert wait_until(lambda: (session.burn_status() or {}).get("done", 0) > 0)
    assert wait_until(lambda: (session.burn_status() or {}).get("state")
                      == "burned")
    assert session.burn_status()["total"] == 9
    runner.stop()


def test_reburn_of_an_unchanged_show_writes_nothing():
    session, runner, bus = make_session()
    cues = [{"slot": 1, "boards": {1: array(1)}, "delays": {}}]
    session.burn(cues, dev_type=3)
    assert wait_until(lambda: (session.burn_status() or {}).get("state")
                      == "burned")
    n = len([f for f in bus.requested if f.cmd == SAVE])
    session.burn(cues, dev_type=3)          # identical content and slot
    assert wait_until(lambda: session.burn_status()["done"] == 1
                      and session.burn_status()["state"] == "burned")
    assert len([f for f in bus.requested if f.cmd == SAVE]) == n
    runner.stop()


def test_a_changed_cue_rewrites_only_its_own_slot():
    session, runner, bus = make_session()
    cues = [{"slot": 1, "boards": {1: array(1)}, "delays": {}},
            {"slot": 2, "boards": {1: array(2)}, "delays": {}}]
    session.burn(cues, dev_type=3)
    assert wait_until(lambda: (session.burn_status() or {}).get("state")
                      == "burned")
    n = len([f for f in bus.requested if f.cmd == SAVE])
    cues2 = [{"slot": 1, "boards": {1: array(1)}, "delays": {}},   # unchanged
             {"slot": 2, "boards": {1: array(9)}, "delays": {}}]   # changed
    session.burn(cues2, dev_type=3)
    assert wait_until(lambda: session.burn_status()["done"] == 2
                      and session.burn_status()["state"] == "burned")
    new_saves = [f for f in bus.requested if f.cmd == SAVE][n:]
    assert [(f.data[0], f.data[3]) for f in new_saves] == [(2, 9)]
    runner.stop()


def test_a_board_missing_at_burn_time_is_reported_by_board_and_slot():
    session, runner, bus = make_session(PickyBus({2}))
    cues = [{"slot": 1, "boards": {1: array(1), 2: array(1)}, "delays": {}}]
    session.burn(cues, dev_type=3)
    assert wait_until(lambda: (session.burn_status() or {}).get("state")
                      == "failed")
    assert session.burn_status()["failed"] == [[2, 1]]
    runner.stop()


def test_cfg_and_delay_caches_are_per_board_and_slot():
    session, runner, bus = make_session()
    cues = [{"slot": 1, "boards": {1: array(1)}, "delays": {1: table(20)}},
            {"slot": 2, "boards": {1: array(1)}, "delays": {1: table(20)}}]
    session.burn(cues, dev_type=3)
    assert wait_until(lambda: (session.burn_status() or {}).get("state")
                      == "burned")
    cfg_frames = [f for f in bus.requested if f.cmd == CFG and f.dest == 1]
    # One for setup's own probe (slot 19, the runner's default), then one
    # per burned slot - never skipped even though board 1 was already
    # configured for a DIFFERENT slot.
    assert [f.data[0] for f in cfg_frames] == [19, 1, 2]
    delay_frames = [f for f in bus.requested if f.cmd == DELAY]
    assert len(delay_frames) == 4                        # low+high, per slot
    runner.stop()


def test_a_dropped_board_forgets_every_slots_cache():
    session, runner, bus = make_session()
    cues = [{"slot": 1, "boards": {1: array(1)}, "delays": {}}]
    session.burn(cues, dev_type=3)
    assert wait_until(lambda: (session.burn_status() or {}).get("state")
                      == "burned")
    assert (1, 1) in runner._cfg_done and (1, 1) in runner._burn_cache
    runner._drop(1)
    assert (1, 1) not in runner._cfg_done
    assert (1, 1) not in runner._burn_cache
    runner.stop()


def test_a_manual_prepare_invalidates_the_burn_cache_for_its_slot():
    # "the unit marks that slot dirty so the next /show/load re-burns it"
    session, runner, bus = make_session()
    cues = [{"slot": 19, "boards": {1: array(1)}, "delays": {}}]
    session.burn(cues, dev_type=3)
    assert wait_until(lambda: (session.burn_status() or {}).get("state")
                      == "burned")
    assert (1, 19) in runner._burn_cache
    session.prepare("manual", {1: array(9)})     # slot 19, the default
    assert wait_until(lambda: session.phase == READY)
    assert (1, 19) not in runner._burn_cache
    runner.stop()


def test_cancel_burn_stops_a_burn_in_progress():
    session, runner, bus = make_session()
    cues = [{"slot": n, "boards": {1: array(1)}, "delays": {}}
            for n in range(1, 19)]
    session.burn(cues, dev_type=3)
    session.cancel_burn()
    time.sleep(0.2)
    # "cancelled", never back to None (review F1: None read as "nothing
    # to worry about" to ShowPlayer's gate). The operator's own STOP
    # needs no reason, so the key is simply absent.
    assert session.burn_status()["state"] == "cancelled"
    assert "reason" not in session.burn_status()
    assert session.status()["burn"]["state"] == "cancelled"
    runner.stop()


def test_cancel_burn_leaves_a_finished_burn_alone():
    session, runner, bus = make_session()
    session.burn([{"slot": 1, "boards": {1: array(1)}, "delays": {}}], dev_type=3)
    assert wait_until(lambda: (session.burn_status() or {}).get("state")
                      == "burned")
    session.cancel_burn()                # STOP on a running show
    assert session.burn_status()["state"] == "burned"
    runner.stop()


def test_a_burn_probes_the_boards_it_has_never_heard_from_once(tmp_path):
    # Real unit, 2026-09-25: a board that is not there costs about 1.5 s
    # of serial timeout, and 14 of them (a 16-board garment on a wall
    # with two boards powered) used to be paid INSIDE the first slot's
    # writes - 22 s of "writing 0/64" followed by 0.3 s a slot. Paid
    # once before slot 1 now, with the absent boards named in the log.
    session, runner, bus = make_session(PickyBus({3, 4}))
    runner.boards = [1, 2, 3, 4]
    runner.live, runner.absent = [1, 2], set()          # 3, 4 unheard of
    cues = [{"slot": 1, "boards": {b: array(1) for b in (1, 2, 3, 4)},
             "delays": {}},
            {"slot": 2, "boards": {b: array(2) for b in (1, 2, 3, 4)},
             "delays": {}}]
    runner._probe_burn_boards(bus, 4, {"cues": cues, "dev_type": 3,
                                       "epoch": 1})
    assert runner.absent == {3, 4} and runner.live == [1, 2]
    probes = [f.dest for f in bus.requested if f.dest in (3, 4)]
    assert probes == [3, 4]                  # one short probe each, once
    assert any("2 boards absent (3-4) - skipped" in line
               for line in runner.recent(10))
    runner.stop()


def test_a_finished_burn_logs_its_timing_and_how_many_boards_answered():
    session, runner, bus = make_session(PickyBus({2}))
    session.burn([{"slot": 1, "boards": {1: array(1), 2: array(1)},
                   "delays": {}}], dev_type=3)
    assert wait_until(lambda: (session.burn_status() or {}).get("state")
                      == "failed")
    log = runner.recent(20)
    assert any("1 board absent (2) - skipped" in line for line in log)
    assert any("burn done: 1/2 in " in line
               and "(probe " in line
               and "1 live boards, 1 absent: 2)" in line for line in log)
    runner.stop()


class SlowProbeBus(PickyBus):
    """A board that is not there costs a serial timeout before it is
    given up on (about 1.5 s on the real bus; a fraction of that here)."""

    def __init__(self, silent, delay: float = 0.3):
        super().__init__(silent)
        self._delay = delay

    def request(self, frame, retries=3):
        if frame.dest in self.silent:
            time.sleep(self._delay)
        return super().request(frame, retries)


@pytest.mark.parametrize("sweeps, ahead", [(1, 0.5), (3, 2.0)])
def test_a_cue_due_during_the_probing_sweep_still_fires_on_time(sweeps, ahead):
    # radxa-01, 2026-09-25: a unit that restarted mid-show came back,
    # fired the cue it owed at once - and then the start-up probe of six
    # absent boards (15 s) sat on the NEXT cue, which went out 4 s late.
    # A broadcast trigger needs no board probed, so the sweep waits it
    # out and sends it first.
    # sweeps=3 puts the cue in the GAP between two sweeps, which used to
    # be a flat sleep (R2, review round 3).
    session, runner, bus = make_session(SlowProbeBus(set(range(3, 9))),
                                        boards=list(range(1, 9)),
                                        probe_sweeps=sweeps,
                                        probe_sweep_delay=0.6)
    at = time.monotonic() + ahead         # due in the middle of the probing
    session.arm("c1", 2)                  # already burned into slot 2
    session.fire("c1", at)
    assert wait_until(lambda: session.phase == FIRED, timeout=12)
    assert abs(session.fired_at - at) < 0.05
    shows = [f for f in bus.sent if f.cmd == SHOW]
    assert [f.data[0] for f in shows] == [2]
    # ...and the probing finished afterwards, as it always would.
    assert wait_until(lambda: runner.absent == set(range(3, 9)), timeout=12)
    assert runner.live == [1, 2]
    runner.stop()


class _SlowBurnBus(FakeBus):
    def request(self, frame, retries=3):
        if frame.cmd == SAVE:
            time.sleep(0.05)
        return super().request(frame, retries)


def test_a_worker_stopped_mid_burn_cancels_the_burn_with_its_reason():
    # Review F4: the worker used to return on _stop with the state left
    # at "burning" for ever. Review round 2: and then with "failed" over
    # pairs nobody ever tried - the PC offered a force this unit refuses.
    session, runner, bus = make_session(_SlowBurnBus())
    cues = [{"slot": n, "boards": {1: array(n)}, "delays": {}}
            for n in range(1, 19)]
    session.burn(cues, dev_type=3)
    assert wait_until(lambda: (session.burn_status() or {}).get("done", 0) > 0)
    runner.stop()                        # KEY2, KEY1 on a pattern, shutdown
    status, complete = session.burn_record()
    assert status["state"] == "cancelled" and not complete
    assert status["reason"] == "interrupted: the port was taken"
    assert status["done"] < status["total"] == 18
    assert any("burn interrupted" in line for line in runner.recent(20))


def test_a_burn_queued_while_the_worker_is_stopping_is_cancelled_not_stuck():
    # runner.stop() waits for the worker to leave the bus; a burn() that
    # lands meanwhile still sees `runner.remote` set and only queues its
    # job - for a worker that is on its way out and, before this fix,
    # would never have said so.
    import threading

    session, runner, bus = make_session(_SlowBurnBus())
    cues = [{"slot": n, "boards": {1: array(n)}, "delays": {}}
            for n in range(1, 19)]
    session.burn(cues, dev_type=3)
    assert wait_until(lambda: (session.burn_status() or {}).get("done", 0) > 0)
    stopper = threading.Thread(target=runner.stop)
    stopper.start()                      # joins the worker mid-save
    session.burn([{"slot": 2, "boards": {1: array(2)}, "delays": {}}], dev_type=3)
    stopper.join(timeout=5)
    assert wait_until(lambda: session.burn_status()["state"] != "burning")
    status, complete = session.burn_record()
    # Nothing of it was even tried, so it is cancelled with its reason -
    # not a "failed" listing pairs no board refused (review round 2).
    assert status["state"] == "cancelled" and not complete
    assert status["reason"] == "the worker was stopped first"
    assert any("burn never started" in line for line in runner.recent(20))
