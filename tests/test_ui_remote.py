"""Remote cues on the unit: ui/remote.py, ui/agent.py, runner remote mode.

The serial bus is the FakeBus of tests/test_ui_runner.py; the agent is
the real HTTP server on an ephemeral localhost port, so what is tested
is what the show PC will talk to.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ui import render
from ui.agent import Agent
from ui.app import App, Screen
from ui.config import HEIGHT, WIDTH
from ui.display import NullDisplay
from ui.inputs import ScriptedInput
from ui.patterns import BY_KEY
from ui.remote import (ARMED, FAILED, FIRED, LOCAL, READY, STANDBY,
                       RemoteError, RemoteSession)
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
