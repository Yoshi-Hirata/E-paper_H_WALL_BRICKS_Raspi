"""Remote cues on the unit: ui/remote.py, ui/agent.py, runner remote mode.

The serial bus is the FakeBus of tests/test_ui_runner.py; the agent is
the real HTTP server on an ephemeral localhost port, so what is tested
is what the show PC will talk to.
"""

from __future__ import annotations

import json
import struct
import sys
import threading
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
from ui.runner import NO_DELAY, DemoRunner
from tests.test_ui_runner import (DegradedMaster, FakeBus, RecoveringBus,
                                  make_runner, wait_until)

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

    def request(self, frame, retries=3, timeout=None):
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


class StampedBus(FakeBus):
    """A FakeBus that remembers WHEN each broadcast STOP went out."""

    def __init__(self):
        super().__init__()
        self.broadcast_stops: list[float] = []

    def send(self, frame):
        if frame.cmd == STOP and frame.dest == 0xFF:
            self.broadcast_stops.append(time.monotonic())
        super().send(frame)


def test_a_fire_from_inside_the_probing_sweep_owns_the_guard():
    """The guard of the LATEST fire wins, not the earliest (review,
    2026-09-26). A unit that restarted mid-show fires its overdue cue
    BEFORE the probing sweep, and the next cue can come due inside that
    sweep, where _fire_before_probing() sends it and leaves its guard
    _guard_owed. Both are absolute deadlines; merging them with min()
    kept the dead cue's earlier one and dropped a broadcast 0x17 into
    the second cue's own sweep."""
    bus = StampedBus()
    runner = make_runner(bus, guard_delay=0.05)
    session = RemoteSession(runner)
    # Armed and already overdue: fired before anything is probed.
    session.arm("c1", 1, span_s=0.05, refresh_s=0.05)
    session.fire("c1", time.monotonic() - 0.01)
    assert wait_until(lambda: session.phase == FIRED and session.cue_id == "c1")
    # The probing sweep _setup() now runs is where the next cue comes due.
    session.arm("c2", 2, span_s=0.6, refresh_s=0.2)
    session.fire("c2", time.monotonic() + 0.05)
    assert wait_until(lambda: session.phase == FIRED and session.cue_id == "c2")
    fired2 = session.fired_at
    assert wait_until(lambda: any(t > fired2 for t in bus.broadcast_stops))
    # c2's own refresh 0.2 + span 0.6 - not c1's guard, long overdue by now.
    assert min(t for t in bus.broadcast_stops if t > fired2) >= fired2 + 0.78
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


# ---- the idle autoplay guard while the PC drives the unit ----
#
# 2026-09-27, three rehearsals and a unit swap: the tops garment (16
# boards) lost cues from about two minutes after START on whichever
# Radxa drove it, the skirt never did, and the lost cues' serial writes
# had BLOCKED for 40-400 ms. The master board had restarted its factory
# autoplay in a gap between two cues (38 s with no 0x17 on the bus at
# all) and was deaf mid-repaint. REMOTE now keeps STANDBY's 60 s
# heartbeat going - see ui/runner.py's REMOTE_GUARD_S.

def test_the_remote_guard_stops_the_autoplay_while_the_worker_is_idle():
    bus = StampedBus()
    runner = make_runner(bus, guard_delay=0.05, remote_guard=0.1)
    session = RemoteSession(runner)
    session.arm("c1", 1)                    # a worker with nothing to do
    assert wait_until(lambda: runner.remote_guard_sent >= 4, timeout=5.0)
    beats = list(bus.broadcast_stops)
    status = session.status()
    runner.stop()
    gaps = [b - a for a, b in zip(beats[-4:], beats[-3:])]
    assert gaps and all(0.09 <= gap <= 0.4 for gap in gaps), gaps
    # Said once, then silent - one line a minute would bury the log.
    said = [line for line in runner.recent(40) if "remote guard" in line]
    assert said == [said[0]] and "stop every 0.1 s while idle" in said[0]
    # ...and counted, so /status can show it did happen.
    assert status["remote_guard_sent"] >= 4


def test_the_remote_guard_goes_out_in_the_gap_between_two_cues():
    """THE case this exists for (review, 2026-09-27). A running show
    never reaches the idle loop: ui/showplay.py arms the next cue the
    moment the current one applies, so session.due() is never None and
    the worker sits inside _fire_at()'s wait for the whole stretch
    between two cues - which is exactly the 11-38 s of silence the
    master board restarted its autoplay in."""
    bus = StampedBus()
    runner = make_runner(bus, guard_delay=0.1, remote_guard=0.1,
                         remote_guard_hold=0.2)
    session = RemoteSession(runner)
    session.arm("c1", 1)
    session.fire("c1", time.monotonic() + 0.05)
    assert wait_until(lambda: session.phase == FIRED, timeout=5.0)
    fired = session.fired_at
    # ...and the next cue is armed at once, a second off, the way a show
    # does it. The worker is inside _fire_at() from here until it fires.
    at2 = time.monotonic() + 1.0
    session.arm("c2", 2)
    session.fire("c2", at2)
    assert wait_until(lambda: session.phase == FIRED and session.cue_id == "c2",
                      timeout=5.0)
    runner.stop()
    beats = [t for t in bus.broadcast_stops if fired < t < at2]
    assert beats, "no autoplay guard at all in the gap between two cues"
    assert runner.remote_guard_sent >= 1
    # Not inside c1's repaint, and not in the run-up to c2 either.
    assert min(beats) >= fired + 0.1 - 0.02
    assert max(beats) <= at2 - 0.2 + 0.02
    # ...and c2 still went out on time.
    assert 0 <= (session.fired_at - at2) * 1000 < 50


def test_the_remote_guard_stands_aside_for_a_cue_and_its_repaint():
    """The two ways a heartbeat could do harm: landing on top of a
    trigger about to go out (REMOTE_GUARD_HOLD_S of clearance), and
    landing inside the repaint that trigger starts (_guard_for()).

    With the pre-cue check left ON: it is never begun inside the hold
    either (review F3), so nothing at all goes out in the run-up here.
    """
    bus = StampedBus()
    runner = make_runner(bus, guard_delay=0.4, remote_guard=0.05)
    session = RemoteSession(runner)
    session.arm("c1", 1)
    assert wait_until(lambda: runner.remote_guard_sent >= 1, timeout=5.0)
    armed_at = time.monotonic()
    session.fire("c1", armed_at + 0.5)
    assert wait_until(lambda: session.phase == FIRED, timeout=5.0)
    fired = session.fired_at
    # Nothing at all in the run-up to the trigger, though the heartbeat
    # was due six times over: a cue within 5 s owns the bus.
    assert [t for t in bus.broadcast_stops if armed_at < t < fired] == []
    assert wait_until(lambda: any(t > fired for t in bus.broadcast_stops),
                      timeout=5.0)
    # ...and the first stop after it is the post-fire guard, a whole
    # guard_delay of repaint later - not a heartbeat inside the picture.
    assert min(t for t in bus.broadcast_stops if t > fired) >= fired + 0.38
    runner.stop()


def test_a_fire_resets_the_remote_guards_clock():
    """Any broadcast 0x17 counts as the heartbeat's own: the guard STOP
    after a fire silences the autoplay just as well, so the next
    heartbeat is a full interval after THAT, not after the last one."""
    bus = StampedBus()
    runner = make_runner(bus, guard_delay=0.3, remote_guard=0.25)
    session = RemoteSession(runner)
    session.arm("c1", 1)
    assert wait_until(lambda: runner.remote_guard_sent >= 1, timeout=5.0)
    session.fire("c1", time.monotonic() + 0.02)
    assert wait_until(lambda: session.phase == FIRED, timeout=5.0)
    fired = session.fired_at
    assert wait_until(lambda: any(t > fired for t in bus.broadcast_stops),
                      timeout=5.0)
    guard_at = min(t for t in bus.broadcast_stops if t > fired)
    assert guard_at >= fired + 0.28          # only the guard, nothing sooner
    assert wait_until(lambda: any(t > guard_at for t in bus.broadcast_stops),
                      timeout=5.0)
    assert min(t for t in bus.broadcast_stops if t > guard_at) >= guard_at + 0.23
    runner.stop()


def test_remote_guard_zero_sends_no_heartbeat_at_all():
    bus = StampedBus()
    runner = make_runner(bus, guard_delay=0.05, remote_guard=0.0)
    session = RemoteSession(runner)
    session.arm("c1", 1)
    # The worker's own opening stop (_setup()) still goes out, once.
    assert wait_until(lambda: len(bus.broadcast_stops) == 1, timeout=5.0)
    time.sleep(0.5)
    assert len(bus.broadcast_stops) == 1
    assert runner.remote_guard_sent == 0
    runner.stop()


def test_the_heartbeat_does_not_move_a_cue():
    bus = StampedBus()
    runner = make_runner(bus, guard_delay=0.05, remote_guard=0.05)
    session = RemoteSession(runner)
    session.arm("c1", 7)
    assert wait_until(lambda: runner.remote_guard_sent >= 3, timeout=5.0)
    at = time.monotonic() + 0.3
    session.fire("c1", at)
    assert wait_until(lambda: session.phase == FIRED, timeout=5.0)
    assert len(shows(bus)) == 1 and shows(bus)[0].dest == 0xFF
    assert 0 <= (session.fired_at - at) * 1000 < 50
    runner.stop()


# ---- the write that blocked ----

class StallingBus(StampedBus):
    """A FakeBus whose broadcast writes BLOCK, the way the CDC of a
    master board that has started repainting does."""

    def __init__(self, seconds: float = 0.12, on_cmd: int = SHOW):
        super().__init__()
        self.block_s, self.on_cmd = seconds, on_cmd

    def send(self, frame):
        if frame.cmd == self.on_cmd and frame.dest == 0xFF:
            time.sleep(self.block_s)
        super().send(frame)


def test_a_show_write_that_blocks_is_timed_named_and_counted():
    """2026-09-27: the cue was not late, it was LOST - and the only
    trace was the fire's own lateness, which says nothing about whose
    fault it was. The write's own clock does."""
    bus = StallingBus(0.12, SHOW)
    runner = make_runner(bus, guard_delay=0.05)
    session = RemoteSession(runner)
    session.arm("c1", 6)
    session.fire("c1", time.monotonic() + 0.05)
    assert wait_until(lambda: session.phase == FIRED, timeout=5.0)
    stall = session.status()["bus_stall"]
    assert stall["frame"] == "show slot 6" and stall["count"] == 1
    assert stall["ms"] >= 100 and stall["at"] > 0
    assert any("bus stalled" in line and "on show slot 6" in line
               for line in runner.recent(20))
    runner.stop()


def test_a_stop_that_blocks_is_named_stop_and_the_stalls_add_up():
    bus = StallingBus(0.08, STOP)
    runner = make_runner(bus, guard_delay=0.05, remote_guard=0.1)
    session = RemoteSession(runner)
    session.arm("c1", 1)
    assert wait_until(lambda: (runner.bus_stall or {}).get("count", 0) >= 2,
                      timeout=5.0)
    stall = session.status()["bus_stall"]
    runner.stop()
    assert stall["frame"] == "stop" and stall["count"] >= 2
    assert stall["ms"] >= 70


def test_a_clean_cue_takes_the_stall_mark_down_but_keeps_the_count():
    """A 60 ms stall in the second minute is worth looking at then, not
    an amber mark on the tile through the encore (review, 2026-09-27)."""
    bus = StallingBus(0.12, SHOW)
    runner = make_runner(bus, guard_delay=0.05)
    session = RemoteSession(runner)
    session.arm("c1", 3)
    session.fire("c1", time.monotonic() + 0.05)
    assert wait_until(lambda: session.phase == FIRED, timeout=5.0)
    assert session.status()["bus_stall"]["ms"] >= 100
    assert session.status()["bus_stall"]["ago_s"] is not None
    bus.block_s = 0.0                       # the board is free again
    session.arm("c2", 4)
    session.fire("c2", time.monotonic() + 0.05)
    assert wait_until(lambda: session.phase == FIRED and session.cue_id == "c2",
                      timeout=5.0)
    stall = session.status()["bus_stall"]
    runner.stop()
    assert stall["ms"] is None and stall["count"] == 1


def test_a_bus_that_takes_the_frame_at_once_reports_no_stall():
    bus = StampedBus()
    runner = make_runner(bus, guard_delay=0.05, remote_guard=0.05)
    session = RemoteSession(runner)
    session.arm("c1", 1)
    session.fire("c1", time.monotonic() + 0.05)
    assert wait_until(lambda: session.phase == FIRED, timeout=5.0)
    assert wait_until(lambda: runner.remote_guard_sent >= 1, timeout=5.0)
    assert session.status()["bus_stall"] is None
    assert not [line for line in runner.recent(40) if "bus stalled" in line]
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


# ---- which board list the unit is working to ----
#
# The 2026-09-26 failure was invisible from the Conductor: every picture
# was written, and what was wrong was the unit's own idea of which
# sockets exist (radxa-04 probed 17-22 all show). /status says it now.

def test_status_says_which_board_list_is_in_force():
    from ui.patterns import BY_KEY
    from tests.test_ui_runner import Wall

    # A unit started with --boards: its own list, no discovery.
    session, runner, bus = make_session(boards=[1, 2, 3], verify_fire=False)
    status = session.status()
    assert status["boards_source"] == "fixed"
    assert status["boards"] == [1, 2, 3] and status["absent"] == []
    assert status["group_count"] == 3

    # A job's list takes over while the PC drives ("show" is reserved
    # for a show file's own garment list - see set_boards())...
    session.prepare("c1", {1: array(3), 2: array(4)})
    assert wait_until(lambda: session.phase == READY)
    status = session.status()
    assert status["boards_source"] == "job"
    assert status["boards"] == [1, 2] and status["group_count"] == 2

    # ...and a show file's list says so, whoever brings it next.
    session.set_boards([1, 2])
    assert wait_until(lambda: runner.boards_source == "show", timeout=5)

    # ...and the unit's own comes back when the port does.
    session.release()
    runner.start(BY_KEY["solid"])
    assert wait_until(lambda: "panels online" in " ".join(runner.log))
    status = session.status()
    assert status["boards_source"] == "fixed"
    assert status["boards"] == [1, 2, 3] and status["group_count"] == 3
    runner.stop()

    # An exploring unit says so, and names what it is still probing -
    # radxa-04's own state on the night: a 16-board garment, six empty
    # sockets past it.
    bus = Wall(set(range(1, 17)))
    session, runner, _ = make_session(bus, boards=None, verify_fire=False)
    runner.start(BY_KEY["solid"])
    assert wait_until(lambda: "panels online" in " ".join(runner.log),
                      timeout=20)
    status = session.status()
    assert status["boards_source"] == "explore"
    assert status["boards"] == list(range(1, 17))
    assert status["absent"] == list(range(17, 23))
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


def test_release_alone_gives_the_unit_its_own_list_back():
    # KEY2 to the MENU without starting a pattern: the unit is its own
    # again from that moment, not from whenever somebody next presses
    # KEY1. A bench unit released like this used to report the show's
    # 16 boards for ever (review, 2026-09-27).
    session, runner, bus = make_session(boards=[1, 2, 3], verify_fire=False)
    session.prepare("c1", {1: array(3), 2: array(4)})
    assert wait_until(lambda: session.phase == READY)
    # One job's boards are a job's, not a show's - nothing here says
    # anything about a garment.
    assert runner.boards == [1, 2] and runner.boards_source == "job"
    session.release()
    assert runner.boards == [1, 2, 3] and runner.boards_source == "fixed"
    assert runner.group_count == 3
    assert session.status()["boards"] == [1, 2, 3]
    assert any("released, back to boards 1-3" in line for line in runner.log)
    runner.stop()


def test_the_board_list_in_force_travels_over_http(agent):
    agent, session, runner, bus = agent
    status = call(agent, "/status")[1]
    assert status["boards_source"] == "fixed"        # make_runner's own list
    assert status["absent"] == [] and status["group_count"] == 20
    assert isinstance(status["uptime_s"], int)
    call(agent, "/prepare", {"cue": "c7", "dev_type": 3,
                             "boards": {"1": array(3).hex(),
                                        "2": array(4).hex()}})
    assert wait_until(lambda: call(agent, "/status")[1]["phase"] == "ready")
    status = call(agent, "/status")[1]
    assert status["boards_source"] == "job" and status["boards"] == [1, 2]
    assert status["group_count"] == 2


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


def test_the_clear_endpoint_needs_a_show_player_and_says_so(agent):
    # This agent fixture has no ShowPlayer at all, which is exactly the
    # refusal a /show/* path must give rather than a 500.
    agent, session, runner, bus = agent
    code, answer = call(agent, "/show/clear", {"show": "abc"})
    assert code == 409 and "no show player" in answer["error"]


def test_the_clear_endpoint_deletes_the_slots_and_reports_it(tmp_path):
    from ui.showplay import ShowPlayer

    session, runner, bus = make_session(boards=[1], verify_fire=False)
    player = ShowPlayer(session, store=tmp_path, tick_s=0.02, grace_s=0.1)
    agent = Agent(session, port=0, host="127.0.0.1", name="radxa-03",
                  player=player)
    agent.start()
    try:
        show = {"id": "abc1234567", "name": "t", "unit": "radxa-03",
                "dev_type": 3, "refresh_s": 0.3, "duration": 2.0,
                "boards": [1], "slot_capacity": 20, "clear_after_show": True,
                "cues": [{"id": "q00", "at": 0.0, "sent": -0.3, "slot": 1,
                          "label": "", "boards": {"1": array(1).hex()},
                          "state": {"1": array(1).hex()}}]}
        assert call(agent, "/show/load", show)[0] == 200
        assert wait_until(lambda: call(agent, "/status")[1]["show"]["burn"]
                          ["state"] == "burned", timeout=5)
        code, answer = call(agent, "/show/clear", {"show": "abc1234567"})
        assert code == 200
        assert answer["show"]["clear_after_show"] is True
        assert wait_until(lambda: call(agent, "/status")[1]["clear"]["state"]
                          == "cleared", timeout=5)
        status = call(agent, "/status")[1]
        assert deletes(bus) == [(1, s) for s in range(1, 19)]
        assert status["show"]["burn"]["state"] == "cleared"
        # ...and START is refused afterwards, with the sentence the PC shows.
        code, answer = call(agent, "/show/run",
                            {"t0": time.monotonic() + 1, "show": "abc1234567",
                             "force": True})
        assert code == 409
        assert "pictures were cleared after the last show" in answer["error"]
        # The wrong show id is a refusal, not a clear of somebody else's.
        code, answer = call(agent, "/show/clear", {"show": "nope"})
        assert code == 409 and "loaded show is abc1234567" in answer["error"]
    finally:
        agent.stop()
        player.close()
        runner.stop()


def test_a_clear_is_refused_over_the_wire_while_a_show_runs(tmp_path):
    from ui.showplay import ShowPlayer

    session, runner, bus = make_session(boards=[1], verify_fire=False)
    player = ShowPlayer(session, store=tmp_path, tick_s=0.02, grace_s=0.1)
    agent = Agent(session, port=0, host="127.0.0.1", name="radxa-03",
                  player=player)
    agent.start()
    try:
        show = {"id": "abc1234567", "name": "t", "unit": "radxa-03",
                "dev_type": 3, "refresh_s": 0.3, "duration": 60.0,
                "boards": [1], "slot_capacity": 20,
                "cues": [{"id": "q00", "at": 0.0, "sent": -0.3, "slot": 1,
                          "label": "", "boards": {"1": array(1).hex()},
                          "state": {"1": array(1).hex()}}]}
        assert call(agent, "/show/load", show)[0] == 200
        assert wait_until(lambda: call(agent, "/status")[1]["show"]["burn"]
                          ["state"] == "burned", timeout=5)
        assert call(agent, "/show/run", {"t0": time.monotonic() + 0.05,
                                         "show": "abc1234567"})[0] == 200
        assert wait_until(lambda: player.state == "running")
        code, answer = call(agent, "/show/clear", {"show": "abc1234567"})
        assert code == 409 and "stop the show first" in answer["error"]
        assert deletes(bus) == []
    finally:
        agent.stop()
        player.close()
        runner.stop()


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
        def request(self, frame, retries=3, timeout=None):
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

    def request(self, frame, retries=3, timeout=None):
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

    def request(self, frame, retries=3, timeout=None):
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
    def request(self, frame, retries=3, timeout=None):
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


# ---- taking the pictures back out of the slots (0x14) ----
# After the show on 2026-09-27 the operator pressed STOP and unplugged the
# Radxa from a garment whose boards were still on battery. A minute later the
# master board restarted the factory autoplay and cycled slots 0-18 - it
# replayed the show's pictures on its own. So they must not be in the slots
# any more when the garment is unplugged.

DELETE = 0x14
CLEAR_ALL = 0x15          # 全消去: FORBIDDEN, never sent (SPECIFICATION 2.4)


class SlowDeleteBus(FakeBus):
    """A 0x14 that takes long enough to be interrupted half way."""

    def request(self, frame, retries=3, timeout=None):
        if frame.cmd == DELETE:
            time.sleep(0.05)
        return super().request(frame, retries)


def deletes(bus):
    """Every 0x14 that went out, as (board, slot)."""
    return [(f.dest, f.data[0]) for f in bus.requested if f.cmd == DELETE]


def burned(session, cues):
    session.burn(cues, dev_type=3)
    assert wait_until(lambda: (session.burn_status() or {}).get("state")
                      == "burned")


def test_a_clear_deletes_exactly_slots_1_to_18_on_every_live_board():
    session, runner, bus = make_session(boards=[1, 2], verify_fire=False)
    burned(session, [{"slot": 1, "boards": {1: array(1), 2: array(1)},
                      "delays": {}}])
    session.clear(range(1, 19))
    assert wait_until(lambda: session.clear_record()["state"] == "cleared")
    # Board by board, slot by slot: 2 x 18 pairs and not one outside 1-18.
    assert deletes(bus) == [(b, s) for b in (1, 2) for s in range(1, 19)]
    record = session.clear_record()
    assert record["done"] == record["total"] == 36 and record["failed"] == []
    assert any("clear: slots 1-18 on 2 boards" in line
               for line in runner.recent(20))
    assert any("clear done: 36/36 in " in line for line in runner.recent(20))
    runner.stop()


def test_a_clear_never_sends_the_forbidden_clear_all():
    session, runner, bus = make_session(boards=[1], verify_fire=False)
    burned(session, [{"slot": 1, "boards": {1: array(1)}, "delays": {}}])
    session.clear(range(1, 19))
    assert wait_until(lambda: session.clear_record()["state"] == "cleared")
    assert not [f for f in bus.sent + bus.requested if f.cmd == CLEAR_ALL]
    runner.stop()


def test_a_clear_repaints_nothing_at_all():
    # The operator's rule (2026-09-27): as long as the Radxa stays
    # connected the garment must keep showing its LAST design. So no
    # broadcast "show slot N" of any kind - and slot 0, the standby
    # white, above all - may go out during or after a clear.
    session, runner, bus = make_session(boards=[1, 2], verify_fire=False)
    burned(session, [{"slot": 1, "boards": {1: array(1), 2: array(1)},
                      "delays": {}}])
    session.arm("q00", 1, dev_type=3)
    session.fire("q00", time.monotonic() + 0.05)
    assert wait_until(lambda: session.phase == FIRED)
    painted = len(shows(bus))
    assert painted == 1                        # the cue itself, and nothing else
    session.clear(range(1, 19))
    assert wait_until(lambda: session.clear_record()["state"] == "cleared")
    time.sleep(0.2)                            # ...and nothing after it either
    assert len(shows(bus)) == painted
    assert not [f for f in shows(bus) if f.data[0] == 0]
    # The unit stays under remote control - not handed back to its own menu,
    # not put into standby - so the heartbeat goes on silencing the autoplay.
    assert session.active is True and runner.standby_ready is False
    runner.stop()


def test_the_autoplay_heartbeat_goes_on_after_a_clear():
    # The unit stays in REMOTE idle: the pictures are gone from the slots,
    # but the garment is still showing the last look it was given and the
    # periodic broadcast 0x17 is what keeps the factory autoplay off it for
    # as long as the Radxa is there (the operator's rule, 2026-09-27).
    session, runner, bus = make_session(boards=[1], verify_fire=False,
                                        remote_guard=0.05)
    burned(session, [{"slot": 1, "boards": {1: array(1)}, "delays": {}}])
    session.clear(range(1, 19))
    assert wait_until(lambda: session.clear_record()["state"] == "cleared")
    sent = runner.remote_guard_sent
    assert wait_until(lambda: runner.remote_guard_sent > sent, timeout=5), \
        "the heartbeat stopped once the slots were emptied"
    # ...and every one of them is a STOP, never a paint.
    assert not shows(bus)
    runner.stop()


def test_a_clear_waits_for_the_last_cue_s_guard():
    # A 0x14 landing inside a repaint is the one thing this must not do,
    # so the first delete may only go out after the guard floor the cue's
    # own broadcast set (here 0.4 s, long enough to observe).
    session, runner, bus = make_session(boards=[1], verify_fire=False,
                                        guard_delay=0.4)
    burned(session, [{"slot": 1, "boards": {1: array(1)}, "delays": {}}])
    session.arm("q00", 1, dev_type=3)
    session.fire("q00", time.monotonic() + 0.02)
    assert wait_until(lambda: session.phase == FIRED)
    fired_at = time.monotonic()
    session.clear([1])
    assert wait_until(lambda: session.clear_record()["state"] == "cleared",
                      timeout=10)
    assert deletes(bus) == [(1, 1)]
    assert time.monotonic() - fired_at >= 0.4
    runner.stop()


def test_a_slot_that_will_not_delete_is_recorded_by_board_and_slot():
    session, runner, bus = make_session(PickyBus({2}), boards=[1, 2],
                                        verify_fire=False)
    # The garment has two boards and board 2 is not there - a burn that
    # names both is what tells the runner so.
    session.burn([{"slot": 1, "boards": {1: array(1), 2: array(1)},
                   "delays": {}}], dev_type=3)
    assert wait_until(lambda: (session.burn_status() or {}).get("state")
                      == "failed")
    session.clear([1, 2])
    assert wait_until(lambda: session.clear_record()["state"] == "failed")
    record = session.clear_record()
    # Board 2 never answers anything, so it is absent: its pairs are the
    # failures, and the whole list is walked either way.
    assert record["failed"] == [[2, 1], [2, 2]]
    assert record["done"] == record["total"] == 4
    assert any("clear failed: 2 slots on boards 2" in line
               for line in runner.recent(20))
    runner.stop()


def test_a_delete_is_acked_and_retried_like_a_save():
    class Flaky(FakeBus):
        """The first 0x14 of each board goes unanswered."""

        def __init__(self):
            super().__init__()
            self.seen = set()

        def request(self, frame, retries=3, timeout=None):
            if frame.cmd == DELETE and frame.dest not in self.seen:
                self.seen.add(frame.dest)
                self.requested.append(frame)
                return None                  # no ACK: the runner retries
            return super().request(frame, retries)

    session, runner, bus = make_session(Flaky(), boards=[1], verify_fire=False,
                                        save_attempts=3)
    burned(session, [{"slot": 1, "boards": {1: array(1)}, "delays": {}}])
    session.clear([1, 2, 3])
    assert wait_until(lambda: session.clear_record()["state"] == "cleared")
    assert len(deletes(bus)) == 4        # three slots, and the one retry
    assert session.clear_record()["failed"] == []
    runner.stop()


def test_a_clear_makes_the_burn_say_cleared_and_drops_its_cache():
    session, runner, bus = make_session(boards=[1], verify_fire=False)
    burned(session, [{"slot": 1, "boards": {1: array(1)},
                      "delays": {1: table(20)}}])
    assert (1, 1) in runner._burn_cache
    session.clear(range(1, 19))
    assert wait_until(lambda: session.clear_record()["state"] == "cleared")
    # The slot's picture, its 0x1B config and its delay table are all gone
    # from the board, so nothing here may claim it still holds them.
    assert (1, 1) not in runner._burn_cache
    assert (1, 1) not in runner._cfg_done
    assert (1, 1) not in runner._delays_sent
    burn, complete = session.burn_record()
    assert burn["state"] == "cleared" and complete
    assert "reason" not in burn
    runner.stop()


def test_a_re_upload_after_a_clear_rewrites_every_slot():
    session, runner, bus = make_session(boards=[1], verify_fire=False)
    cues = [{"slot": 1, "boards": {1: array(1)}, "delays": {}},
            {"slot": 2, "boards": {1: array(2)}, "delays": {}}]
    burned(session, cues)
    n = len([f for f in bus.requested if f.cmd == SAVE])
    session.clear(range(1, 19))
    assert wait_until(lambda: session.clear_record()["state"] == "cleared")
    burned(session, cues)               # the very same show, again
    assert len([f for f in bus.requested if f.cmd == SAVE]) == n + 2
    # ...and the clear is history: a burn supersedes it outright.
    assert session.clear_record()["state"] == "none"
    assert session.burn_record()[0]["state"] == "burned"
    runner.stop()


def test_a_clear_interrupted_by_a_start_stops_and_says_so():
    session, runner, bus = make_session(SlowDeleteBus(), boards=[1],
                                        verify_fire=False)
    burned(session, [{"slot": 1, "boards": {1: array(1)}, "delays": {}}])
    session.clear(range(1, 19))
    assert wait_until(lambda: session.clear_record()["done"] > 0)
    session.cancel_clear("a new run started")
    assert wait_until(lambda: session.clear_record()["state"] == "partial")
    record = session.clear_record()
    assert 0 < record["done"] < record["total"] == 18
    assert record["reason"] == "a new run started"
    # Half a show's pictures is not something START may run: the burn
    # record says "cleared" with the reason the PC turns into "cleared
    # partially - Upload again".
    burn, complete = session.burn_record()
    assert burn["state"] == "cleared" and burn["reason"] == "cleared partially"
    assert wait_until(lambda: any("clear interrupted" in line
                                 for line in runner.recent(20)))
    runner.stop()


def test_a_clear_that_never_began_leaves_the_pictures_alone():
    # A long guard is what parks the worker: the clear stays QUEUED for
    # those seconds, so the cancel below really is "before it ever began"
    # rather than a race on how fast the worker woke up.
    session, runner, bus = make_session(boards=[1], verify_fire=False,
                                        guard_delay=5.0)
    burned(session, [{"slot": 1, "boards": {1: array(1)}, "delays": {}}])
    session.arm("q00", 1, dev_type=3)
    session.fire("q00", time.monotonic() + 0.02)
    assert wait_until(lambda: session.phase == FIRED)
    session.clear(range(1, 19))
    time.sleep(0.2)                     # the worker has had every chance
    assert session.clear_record()["state"] == "clearing"
    assert deletes(bus) == []           # ...and is waiting for the guard
    session.cancel_clear("a new run started")
    time.sleep(0.2)
    # Not one slot went, so nothing about the pictures has changed and
    # START passes its gate as it always did.
    assert session.clear_record()["state"] == "none"
    assert session.burn_record()[0]["state"] == "burned"
    assert deletes(bus) == []
    runner.stop()


def test_a_clear_the_worker_has_taken_is_past_taking_back():
    # The worker stops after the slot it is on, and THAT slot can land
    # after the cancel returns - so the pictures stop being something
    # START may run the moment the job was taken, not when the first
    # delete is recorded (50-250 ms of relay in between).
    held = threading.Event()
    reached = threading.Event()

    class Blocking(FakeBus):
        def request(self, frame, retries=3, timeout=None):
            if frame.cmd == DELETE:
                reached.set()
                held.wait(5)
            return super().request(frame, retries)

    session, runner, bus = make_session(Blocking(), boards=[1],
                                        verify_fire=False)
    burned(session, [{"slot": 1, "boards": {1: array(1)}, "delays": {}}])
    session.clear(range(1, 19))
    assert reached.wait(5), "the worker never reached the first delete"
    assert session.clear_record()["done"] == 0   # nothing recorded yet
    session.cancel_clear("a new run started")
    # ...and START is already refused, before that first 0x14 has landed.
    assert session.clear_record()["state"] == "partial"
    burn, _ = session.burn_record()
    assert burn["state"] == "cleared" and burn["reason"] == "cleared partially"
    held.set()
    assert wait_until(lambda: any("clear interrupted" in line
                                 for line in runner.recent(20)))
    runner.stop()


def test_slot_0_and_slot_19_can_never_be_cleared():
    session, runner, bus = make_session(boards=[1], verify_fire=False)
    for slot in (0, 19, 20):
        with pytest.raises(RemoteError) as exc:
            session.clear([slot])
        assert "not a show slot" in str(exc.value)
    assert deletes(bus) == []
    runner.stop()


def test_a_worker_stopped_mid_clear_reports_it():
    session, runner, bus = make_session(SlowDeleteBus(), boards=[1],
                                        verify_fire=False)
    burned(session, [{"slot": 1, "boards": {1: array(1)}, "delays": {}}])
    session.clear(range(1, 19))
    assert wait_until(lambda: session.clear_record()["done"] > 0)
    runner.stop()
    record = session.clear_record()
    assert record["state"] == "partial"
    assert record["reason"] == "interrupted: the port was taken"
    assert session.burn_record()[0]["state"] == "cleared"


def test_status_carries_the_clear_and_says_none_until_one_is_asked_for():
    session, runner, bus = make_session(boards=[1], verify_fire=False)
    assert session.status()["clear"] == {"state": "none", "done": 0,
                                        "total": 0, "failed": []}
    burned(session, [{"slot": 1, "boards": {1: array(1)}, "delays": {}}])
    session.clear(range(1, 19))
    assert wait_until(lambda: session.status()["clear"]["state"] == "cleared")
    assert session.status()["clear"]["total"] == 18
    runner.stop()


# ---- getting a bus that accepts frames and executes none back ----
# The 2026-09-28 evidence from radxa-07 (LOOK28), running main b372d41: a
# degraded master accepts every frame, executes none and answers nothing.
# Padding, a port reopen, a STOP and a probe sweep do NOT bring it back; a USB
# device reset does, in 0.3 s. And a write shortly after another reads fast
# even on the degraded master (61 ms at 0.5 s) - the false "bus recovered by
# padding (512 -> 61 ms)" the unit logged three times. DegradedMaster
# (tests/test_ui_runner.py) is that master; these tests pin the ladder.

def recovery_runner(bus, **kwargs):
    kwargs.setdefault("boards", [1, 2])
    kwargs.setdefault("recover_quiet", 0.5)
    kwargs.setdefault("recover_backoff", 0.3)
    kwargs.setdefault("verify_fire", False)
    kwargs.setdefault("proof_gap", 0.3)         # past DegradedMaster.quick_gap
    return make_runner(bus, **kwargs)


def degraded(monkeypatch, **fake):
    """A DegradedMaster, found by find_port() the way the real unit finds
    its master (no pinned port), so a node that comes back renamed is
    followed."""
    bus = DegradedMaster(**fake)
    monkeypatch.setattr("ui.runner.find_port", bus.find_port)
    return bus


def degraded_runner(bus, **kwargs):
    kwargs.setdefault("port", None)
    kwargs.setdefault("link_token", bus.token)
    kwargs.setdefault("usb_reset", bus.usb_reset)
    kwargs.setdefault("usb_reset_check", bus.reset_available)
    return recovery_runner(bus, **kwargs)


# The unit's own timings, for the tests that measure a budget: the degraded
# write 358 ms (61 ms right behind another), the reset 0.3 s, the node back
# 0.44 s after it, an open's 0.3 s settle (transport.Bus._open).
UNIT_TIMINGS = dict(reset_s=0.3, reenum_s=0.44, open_s=0.3)


def test_the_fake_is_the_master_the_unit_showed(monkeypatch):
    """What the fix rests on, pinned: slow after a pause, fast right behind
    another write (the reading that fooled the padding), silent to 0x02,
    and neither padding nor a reopen changes any of it."""
    bus = degraded(monkeypatch)
    runner = degraded_runner(bus)
    time.sleep(0.25)
    slow = runner._timed_stop(bus, 2)
    quick = runner._timed_stop(bus, 2)            # right behind it
    assert slow >= 300 and quick < 100, (slow, quick)
    assert runner._master_answers(bus, 2) is False
    bus.pad(16)
    bus.reopen(bus.port)
    time.sleep(0.25)
    assert runner._timed_stop(bus, 2) >= 300
    assert runner._master_answers(bus, 2) is False


def test_a_usb_reset_brings_the_master_back_and_is_proven(monkeypatch):
    bus = degraded(monkeypatch)
    runner = degraded_runner(bus)
    time.sleep(0.25)
    result = runner._recover_bus(bus, 2)
    assert result["recovered"] is True and result["by"] == "usb_reset"
    assert result["before_ms"] >= 300 and result["after_ms"] < 100
    assert bus.resets == ["/dev/ttyACM0"] and bus.closes == 1
    assert bus.reopened == ["/dev/ttyACM0"]
    assert runner._setup_owed is True             # a reset owes the sweep
    said = [l for l in runner.recent(20) if "bus recovered by usb reset" in l]
    assert said and "board 1 answers" in said[0] and "→" in said[0], said
    assert runner.bus_recovery["by"] == "usb_reset"


def test_the_false_recovery_of_2026_09_28_cannot_happen_again(monkeypatch):
    """A reset that does not take: the STOP right after the reopen would read
    fast (the padding's 61 ms), but the master is silent - so it is a
    failure, said as one, and the tile's mark stays up."""
    bus = degraded(monkeypatch, reset_cures=False)
    runner = degraded_runner(bus)
    time.sleep(0.25)
    result = runner._recover_bus(bus, 2)
    assert result["recovered"] is False and result["by"] is None
    assert any("bus recovery failed" in l and "board 1 silent" in l
               for l in runner.recent(20))
    assert runner.bus_stall and runner.bus_stall["ms"] >= 300


def test_a_master_that_answers_but_blocks_after_a_pause_is_not_recovered(
        monkeypatch):
    """The other half of the proof: an answer alone is not enough either -
    a STOP after PROOF_GAP_S of silence has to be fast too."""
    bus = degraded(monkeypatch, half_cure=True)
    runner = degraded_runner(bus)
    time.sleep(0.25)
    result = runner._recover_bus(bus, 2)
    assert result["recovered"] is False
    assert any("board 1 answers, but a stop after 0.3 s took" in l
               for l in runner.recent(20))


def test_a_reset_the_unit_cannot_do_says_why_and_keeps_the_port(monkeypatch):
    bus = degraded(monkeypatch, reset_ok=False)
    runner = degraded_runner(bus)
    time.sleep(0.25)
    result = runner._recover_bus(bus, 2)
    assert result["recovered"] is False and bus.resets == []
    assert bus.reopened == ["/dev/ttyACM0"]       # opened again as it was
    said = [l for l in runner.recent(20) if "bus recovery failed" in l]
    assert said and "usb reset: unsupported on this test unit" in said[0], said


def test_the_node_comes_back_renamed_and_refused_at_first(monkeypatch):
    """Run 1 on radxa-07: ttyACM1 in 0.44 s, and the first open refused EACCES
    because udev had not set the node's group yet."""
    bus = degraded(monkeypatch, back_as="/dev/ttyACM1", eacces=3,
                   reenum_s=0.1)
    runner = degraded_runner(bus)
    time.sleep(0.25)
    result = runner._recover_bus(bus, 2)
    assert result["recovered"] is True and result["by"] == "usb_reset"
    assert bus.port == "/dev/ttyACM1" and bus.reopened == ["/dev/ttyACM1"]
    assert any("opened after 3 refusals (not ready)" in l
               for l in runner.recent(20))


def test_a_healthy_master_is_already_clear(monkeypatch):
    bus = degraded(monkeypatch)
    bus.degraded = False
    runner = degraded_runner(bus)
    result = runner._recover_bus(bus, 2)
    assert result["recovered"] is True and result["by"] is None
    assert bus.resets == []
    assert any("bus is clear, nothing to recover" in l and "board 1 answers" in l
               for l in runner.recent(20))


def test_a_stale_stall_on_record_is_not_believed(monkeypatch):
    bus = degraded(monkeypatch)
    bus.degraded = False
    runner = degraded_runner(bus)
    runner.bus_stall = {"ms": 359.0, "frame": "stop", "at": time.time() - 90,
                        "count": 6}
    assert runner._recover_bus(bus, 2)["by"] is None and bus.resets == []


def test_a_recovery_takes_the_stall_mark_down(monkeypatch):
    bus = degraded(monkeypatch)
    runner = degraded_runner(bus)
    runner.bus_stall = {"ms": 359.0, "frame": "stop", "at": time.time(),
                        "count": 4}
    time.sleep(0.25)
    runner._recover_bus(bus, 2)
    # Five: the recovery's own measuring STOP was one more real stall; the
    # proof's STOP was fast and is not on the record.
    assert runner.bus_stall == {"ms": None, "frame": None, "at": None,
                                "count": 5}


def test_two_stalled_heartbeats_start_the_reset_on_their_own(monkeypatch):
    bus = degraded(monkeypatch)
    bus.degraded = False                          # healthy for the setup
    runner = degraded_runner(bus, remote_guard=0.25, guard_delay=0.05)
    session = RemoteSession(runner)
    session.arm("c1", 1)
    assert wait_until(lambda: any("panels online" in l
                                  for l in runner.recent(20)), timeout=10.0)
    bus.degraded = True                           # ...and then it degrades
    assert wait_until(lambda: runner.bus_recovery is not None, timeout=15.0)
    recovery = dict(runner.bus_recovery)
    runner.stop()
    assert recovery["by"] == "usb_reset" and len(bus.resets) == 1
    assert session.status()["bus_recovery"]["by"] == "usb_reset"


def test_one_stalled_heartbeat_is_not_enough():
    bus = RecoveringBus("padding")
    bus.stalled = False
    runner = recovery_runner(bus)
    runner.remote = RemoteSession(runner)
    runner._maybe_recover(bus, 2, 359.0)
    assert runner._stall_streak == 1 and runner.bus_recovery is None
    runner._maybe_recover(bus, 2, 12.0)          # a clean one breaks it
    assert runner._stall_streak == 0 and runner.bus_recovery is None
    runner._maybe_recover(bus, 2, 359.0)
    runner._maybe_recover(bus, 2, 359.0)         # ...two in a row do it
    assert runner.bus_recovery is not None


def test_a_cue_inside_the_quiet_window_holds_the_recovery_off():
    """A recovery resets the USB device. It stands aside for a cue and picks
    the unit up on a later heartbeat - it never makes a cue wait."""
    bus = RecoveringBus("padding")
    runner = recovery_runner(bus, recover_quiet=30.0)
    session = RemoteSession(runner)
    session.arm("c1", 1)
    session.fire("c1", time.monotonic() + 5.0)   # inside the window
    assert not runner._recover_quiet(time.monotonic())
    runner._stall_streak = 1
    runner._maybe_recover(bus, 2, 359.0)
    assert runner.bus_recovery is None
    session.cancel()
    runner.stop()


def test_a_show_being_played_holds_the_recovery_off_too():
    bus = RecoveringBus("padding")
    bus.stalled = False
    runner = recovery_runner(bus)
    session = RemoteSession(runner)
    runner.remote = session
    session.playing = lambda: True                # a run, or a HOLD
    assert not runner._recover_quiet(time.monotonic())
    runner._stall_streak = 1
    runner._maybe_recover(bus, 2, 359.0)
    assert runner.bus_recovery is None
    session.playing = lambda: False               # STOP, and it may run
    assert runner._recover_quiet(time.monotonic())


def test_the_recovery_backs_off_and_then_leaves_it_to_the_operator(monkeypatch):
    bus = degraded(monkeypatch, reset_cures=False)
    runner = degraded_runner(bus, recover_backoff=10.0, recover_attempts=3)
    runner.remote = RemoteSession(runner)

    def try_recovery():
        runner._stall_streak = 1
        runner._maybe_recover(bus, 2, 359.0)

    began = time.monotonic()
    try_recovery()
    assert runner._recover_tries == 1
    assert runner._recover_next >= began + 10.0 - 0.05
    runner._recover_next = time.monotonic() + 3600.0
    try_recovery()                       # inside the backoff: nothing
    assert runner._recover_tries == 1
    for _ in range(2):
        runner._recover_next = 0.0
        try_recovery()
    assert runner._recover_tries == 3 and len(bus.resets) == 3
    runner._recover_next = 0.0
    try_recovery()                       # the ceiling, and it says so once
    runner._recover_next = 0.0
    try_recovery()
    assert runner._recover_tries == 3 and len(bus.resets) == 3
    said = [line for line in runner.recent(120)
            if "leaving it to the operator" in line]
    assert len(said) == 1


def test_the_endpoint_answers_what_the_reset_took(monkeypatch):
    bus = degraded(monkeypatch)
    bus.degraded = False                          # healthy for the setup
    runner = degraded_runner(bus)
    session = RemoteSession(runner)
    # The PC owns this unit first - as it does before any Recover bus: a
    # unit on its own menu is refused, never taken over (review N2).
    session.arm("c1", 1)
    assert wait_until(lambda: any("panels online" in l
                                  for l in runner.recent(20)), timeout=10.0)
    time.sleep(0.3)
    bus.degraded = True
    agent = Agent(session, port=0, host="127.0.0.1", name="radxa-07")
    agent.start()
    try:
        code, answer = call(agent, "/bus/recover", {})
        code2, status = call(agent, "/status")
    finally:
        agent.stop()
        runner.stop()
    assert code == 200
    assert answer["recovered"] is True and answer["by"] == "usb_reset"
    assert answer["after_ms"] < 100 and len(bus.resets) == 1
    recovery = status["bus_recovery"]
    assert recovery["by"] == "usb_reset" and recovery["ago_s"] is not None
    assert status["resend_on_stall"] is False     # off by default


def test_the_endpoint_refuses_while_a_show_is_running():
    bus = RecoveringBus("padding")
    runner = recovery_runner(bus)
    session = RemoteSession(runner)
    session.playing = lambda: True
    agent = Agent(session, port=0, host="127.0.0.1", name="radxa-07")
    agent.start()
    try:
        code, answer = call(agent, "/bus/recover", {})
    finally:
        agent.stop()
        runner.stop()
    assert code == 409
    assert answer["error"] == "a show is running - stop it first"
    assert bus.reopened == []


def test_the_endpoint_refuses_a_cue_that_is_about_to_fire():
    bus = RecoveringBus("padding")
    runner = recovery_runner(bus, recover_quiet=30.0)
    session = RemoteSession(runner)
    session.arm("c1", 1)
    session.fire("c1", time.monotonic() + 4.0)
    agent = Agent(session, port=0, host="127.0.0.1", name="radxa-07")
    agent.start()
    try:
        code, answer = call(agent, "/bus/recover", {})
    finally:
        agent.stop()
        session.cancel()
        runner.stop()
    assert code == 409 and "a cue fires in" in answer["error"]
    assert bus.reopened == []


# ---- the fire-time re-send, after a USB reset (ON by default) ----

def fire_on_a_degraded_master(monkeypatch, lead=0.5, signal=True, **kwargs):
    """Set up healthy, then the master degrades, then a cue is fired: its own
    show frame is the first write after a pause, so it blocks like the
    unit's did (272-358 ms).

    `signal` is what _fire_resend_signal() answers: True "silent" by default
    here, so the reset-and-re-send machinery these tests pin is still
    exercised - the runner's own answer is None (no safe question at fire
    time, 2026-09-28 12:40), which re-sends nothing; `signal="real"` keeps
    it."""
    fake = {k: kwargs.pop(k) for k in list(kwargs)
            if k in ("reset_s", "reenum_s", "open_s", "reset_ok",
                     "reset_cures", "back_as", "eacces")}
    bus = degraded(monkeypatch, **fake)
    bus.degraded = False
    kwargs.setdefault("resend_on_stall", True)      # it is off by default
    runner = degraded_runner(bus, guard_delay=0.05, **kwargs)
    if signal != "real":
        monkeypatch.setattr(runner, "_fire_resend_signal",
                            lambda bus_, groups: signal)
    session = RemoteSession(runner)
    session.arm("c1", 6)
    assert wait_until(lambda: any("panels online" in l
                                  for l in runner.recent(20)), timeout=10.0)
    time.sleep(0.3)
    bus.degraded = True
    at = time.monotonic() + lead
    session.fire("c1", at)
    assert wait_until(lambda: session.phase == FIRED, timeout=10.0)
    runner.stop()
    return bus, runner, session, at


def test_the_re_send_is_off_by_default():
    """PM, after the review of 349dcdd: a stalled show frame is one of two
    states, and a re-send in the slow-but-working one paints twice."""
    assert make_runner(FakeBus()).resend_on_stall is False
    assert DemoRunner(open_bus=lambda port: FakeBus(), port="/dev/fake",
                      boards=[1], echo_log=False).resend_on_stall is False


def test_a_stalled_cue_is_reset_and_re_sent_exactly_once(monkeypatch):
    """With the unit's own timings: the picture goes about 2.1 s late -
    inside FIRE_RESEND_BUDGET_S - instead of never."""
    from ui.runner import FIRE_RESEND_BUDGET_S

    bus, runner, session, at = fire_on_a_degraded_master(monkeypatch,
                                                         **UNIT_TIMINGS)
    assert len(shows(bus)) == 2                   # the first, and ONE more
    # Nothing is ASKED on the wire at fire time: no 0x02 (never answered on
    # this firmware) and no unicast frame either.
    assert not any(f.cmd == 0x02 for f in bus.requested)
    # On the fire path the show frame is what follows the reset, and
    # nothing goes between (review M4 is for the idle and pre-cue paths).
    first, second = [i for i, f in enumerate(bus.sent) if f.cmd == SHOW][:2]
    assert bus.sent[first + 1:second] == []
    assert len(bus.resets) == 1
    record = runner.resend
    assert record["by"] == "usb_reset" and record["before_ms"] >= 200
    assert record["after_ms"] < 100
    assert record["late_s"] <= FIRE_RESEND_BUDGET_S, record
    said = [l for l in runner.recent(40) if "re-sent after usb reset" in l]
    assert said and "(stall " in said[0] and "s late)" in said[0], said
    # The heartbeat and the guard floor run from the frame that was re-sent.
    assert runner._last_show_at >= at + record["late_s"] - 0.05


def test_the_re_send_survives_udev_refusing_the_first_opens(monkeypatch):
    """radxa-07's Run 1: the node back as ttyACM1 in 0.44 s, and the first
    open refused EACCES because udev had not set its group yet. The budget
    is sized so this - the case the unit actually showed - still re-sends."""
    from ui.runner import FIRE_RESEND_BUDGET_S

    bus, runner, session, at = fire_on_a_degraded_master(
        monkeypatch, back_as="/dev/ttyACM1", eacces=2, **UNIT_TIMINGS)
    assert len(shows(bus)) == 2 and bus.port == "/dev/ttyACM1"
    assert runner.resend["by"] == "usb_reset"
    assert runner.resend["late_s"] <= FIRE_RESEND_BUDGET_S, runner.resend
    assert any("opened after 2 refusals" in l for l in runner.recent(40))


def test_with_the_flag_on_a_stall_is_said_and_nothing_is_re_sent(monkeypatch):
    """radxa-07, 2026-09-28 12:40: there is no safe second signal at fire
    time (0x02 is never answered; a unicast STOP there would read a
    repainting master as silent, and could cancel a delayed picture). So
    --resend-on-stall says the stall and sends nothing else at all."""
    bus, runner, session, at = fire_on_a_degraded_master(monkeypatch,
                                                         signal="real")
    assert len(shows(bus)) == 1 and bus.resets == [] and runner.resend is None
    assert any("cue c1 stalled" in l
               and "not re-sent (no safe question at fire time)" in l
               for l in runner.recent(40))
    assert not any(f.cmd == 0x02 for f in bus.requested)
    fired = [i for i, f in enumerate(bus.sent) if f.cmd == SHOW][0]
    assert all(f.dest == 0xFF for f in bus.sent[fired:])


def test_no_resend_on_stall_leaves_a_stalled_cue_alone(monkeypatch):
    bus, runner, session, at = fire_on_a_degraded_master(
        monkeypatch, resend_on_stall=False)
    assert len(shows(bus)) == 1 and bus.resets == []
    assert runner.resend is None
    assert any("bus stalled" in l for l in runner.recent(20))


def test_a_cue_that_went_out_cleanly_is_never_re_sent(monkeypatch):
    bus = degraded(monkeypatch)
    bus.degraded = False
    runner = degraded_runner(bus, guard_delay=0.05)
    session = RemoteSession(runner)
    session.arm("c1", 6)
    session.fire("c1", time.monotonic() + 0.3)
    assert wait_until(lambda: session.phase == FIRED, timeout=10.0)
    runner.stop()
    assert len(shows(bus)) == 1 and bus.resets == [] and runner.resend is None


def test_a_cue_whose_reset_cannot_be_done_is_not_re_sent(monkeypatch):
    bus, runner, session, at = fire_on_a_degraded_master(monkeypatch,
                                                         reset_ok=False)
    assert len(shows(bus)) == 1
    assert any("re-send failed (usb reset: unsupported on this test unit)" in l
               for l in runner.recent(40))


def test_a_port_back_too_late_is_not_re_sent(monkeypatch):
    """The node not back within FIRE_RESEND_BUDGET_S: the cue is reported as
    not re-sent, and there is never a second try at it."""
    bus, runner, session, at = fire_on_a_degraded_master(monkeypatch,
                                                         reenum_s=3.0)
    assert len(shows(bus)) == 1 and len(bus.resets) == 1
    said = [l for l in runner.recent(40) if "re-send failed" in l]
    assert said and "would not open" in said[0], said


class OneSlowShow(DegradedMaster):
    """A HEALTHY master whose next show frame blocks once - a board busy
    for a moment, or the LOOK23 17:29 state where every picture appeared,
    each about 0.36 s late. It answers 0x02 throughout."""

    def __init__(self, block_s=0.25, **kwargs):
        super().__init__(**kwargs)
        self.degraded = False
        self.next_show_block = 0.0
        self.block_s_once = block_s

    def send(self, frame):
        if frame.cmd == SHOW and self.next_show_block:
            wait, self.next_show_block = self.next_show_block, 0.0
            time.sleep(wait)
        super().send(frame)


def test_a_stall_on_a_master_that_answers_is_not_re_sent(monkeypatch):
    """Review of 349dcdd, H1 - the reviewer's t_fire case A: a healthy master
    and ONE show write that blocked 251 ms. With the re-send on, it still
    must not reset anything or send the frame twice: the master answers,
    so the frame is being executed late, not lost. (The signal is the
    stand-in "answers" here: the runner's own is None - see
    test_with_the_flag_on_a_stall_is_said_and_nothing_is_re_sent.)"""
    bus = OneSlowShow(**UNIT_TIMINGS)
    monkeypatch.setattr("ui.runner.find_port", bus.find_port)
    runner = degraded_runner(bus, guard_delay=0.05, resend_on_stall=True)
    monkeypatch.setattr(runner, "_fire_resend_signal",
                        lambda bus_, groups: False)
    session = RemoteSession(runner)
    session.arm("c1", 6)
    assert wait_until(lambda: any("panels online" in l
                                  for l in runner.recent(20)), timeout=10.0)
    bus.next_show_block = 0.251
    session.fire("c1", time.monotonic() + 0.3)
    assert wait_until(lambda: session.phase == FIRED, timeout=10.0)
    runner.stop()
    assert len(shows(bus)) == 1 and bus.resets == [] and runner.resend is None
    assert any("cue c1 stalled" in l and "master answers - not re-sent" in l
               for l in runner.recent(40))


def test_a_reset_that_hangs_at_fire_time_keeps_the_original_frame(monkeypatch):
    """Review M2 - t_fire case C: the reset hung 5 s, held the worker 5.25 s
    and reported the cue +5251 ms. Now the reset is given only what is left
    of FIRE_RESEND_BUDGET_S, the cue keeps its ORIGINAL frame's time, and
    the port is open again afterwards."""
    from ui.runner import FIRE_RESEND_BUDGET_S, OPEN_RETRY_S

    bus, runner, session, at = fire_on_a_degraded_master(
        monkeypatch, reset_s=5.0, reenum_s=0.44, open_s=0.3)
    assert len(shows(bus)) == 1 and bus.resets == []
    late_ms = session.status()["late_ms"]
    assert late_ms is not None and late_ms < 600, late_ms   # the first frame
    assert runner._last_show_at < at + 0.6                  # ...and its floor
    assert bus.reopened, "the port was left closed"
    assert runner._needs_reopen is False
    said = [l for l in runner.recent(40) if "re-send failed" in l]
    assert said and "no answer in" in said[0], said


def test_a_reset_that_hangs_before_a_cue_ends_by_the_hold(monkeypatch):
    """Review M2 - t_fire case B: a sudo that hung 4.5 s reopened the port at
    T-3.11. The reset is now given only until T - REMOTE_GUARD_HOLD_S, no
    frame goes into the hold, and the cue is on time."""
    bus, runner, session, at = armed(monkeypatch, reset_s=4.5)
    reopened_at = []
    reopen = bus.reopen

    def stamped(port=None):
        result = reopen(port)
        reopened_at.append(time.monotonic())
        return result
    bus.reopen = stamped
    assert wait_until(lambda: session.phase == FIRED, timeout=15.0)
    runner.stop()
    assert bus.resets == []                      # it never got to reset
    assert reopened_at and reopened_at[0] < at   # the port is open for the cue
    assert in_the_hold(bus, at) == []            # and not a frame in the hold
    # The master is still degraded - nothing was reset - so the cue's own
    # write blocks its ~0.36 s, and late_ms is taken after that write. That
    # block is all there is: the check itself put nothing in the way.
    late_ms = session.status()["late_ms"]
    assert late_ms is not None and late_ms < 359 + 150, late_ms
    assert any("usb reset → failed" in l and "no answer in" in l
               for l in runner.recent(30))


def test_a_port_an_abandoned_reset_could_not_reopen_goes_to_the_watcher(
        monkeypatch):
    """Review M2: never left closed. The node is back only after every
    window has passed, so the watcher is told, and opens it when it is."""
    bus = degraded(monkeypatch, reenum_s=0.8)
    runner = degraded_runner(bus, port_poll=0.02)
    runner.remote = RemoteSession(runner)
    now = time.monotonic()
    done, why = runner._usb_reset_reopen(bus, give_up_at=now + 0.2,
                                         reopen_by=now + 0.3)
    assert done is False and "left to the watcher" in why, why
    assert runner._needs_reopen is True and bus.reopened == []
    assert wait_until(lambda: runner._port_watch(bus, 2), timeout=3.0)
    assert runner._needs_reopen is False and bus.reopened == ["/dev/ttyACM0"]
    assert any("reopened after the reset" in l for l in runner.recent(20))


def test_a_stop_follows_every_reset_on_the_idle_path(monkeypatch):
    """Review M4: if a USB reset ever restarts the master's factory
    autoplay, a STOP straight after the reopen silences it - the measuring
    STOP, that one, and the proof's gapped STOP."""
    bus = degraded(monkeypatch)
    runner = degraded_runner(bus)
    time.sleep(0.25)
    assert runner._recover_bus(bus, 2)["by"] == "usb_reset"
    stops = [f for f in bus.sent if f.cmd == STOP and f.dest == 0xFF]
    assert len(stops) == 3, len(stops)


def test_a_stop_follows_a_reset_before_a_cue_too(monkeypatch):
    bus = degraded(monkeypatch)
    runner = degraded_runner(bus, precheck=20.0, remote_guard_hold=HOLD)
    time.sleep(0.25)
    runner._precheck(bus, 2, "q03", time.monotonic() + 4.0)
    assert runner.precheck["by"] == "usb_reset"
    stops = [f for f in bus.sent if f.cmd == STOP and f.dest == 0xFF]
    assert len(stops) == 2, len(stops)           # the measuring one, and M4's


def test_the_proof_is_cut_short_by_a_cue_so_start_is_never_late(monkeypatch):
    """Review L1: START right after Recover bus. The proof's 3 s of silence
    ends the moment a cue is armed, with the verdict so far."""
    import types

    bus = degraded(monkeypatch)
    runner = degraded_runner(bus, proof_gap=3.0)
    armed_cue = [None]
    runner.remote = types.SimpleNamespace(due=lambda: armed_cue[0],
                                          pending_job=lambda: False)
    time.sleep(0.25)
    timer = threading.Timer(0.8, lambda: armed_cue.__setitem__(
        0, ("c1", time.monotonic() + 10.0, 1, 3)))
    timer.start()
    began = time.monotonic()
    result = runner._recover_bus(bus, 2)
    took = time.monotonic() - began
    timer.cancel()
    assert result["recovered"] is True and result["by"] == "usb_reset"
    assert took < 2.5, took                      # not the full 3 s gap
    assert any("board 1 answers, proof cut short by a cue" in l
               for l in runner.recent(20))


def test_the_status_says_whether_a_usb_reset_is_possible(monkeypatch):
    """Review L2: asked once, when the worker first has the port."""
    bus = degraded(monkeypatch, reset_ok=False)
    bus.degraded = False
    runner = degraded_runner(bus, usb_reset_check=bus.reset_available)
    session = RemoteSession(runner)
    assert session.status()["usb_reset_ok"] is None        # not asked yet
    session.arm("c1", 1)
    assert wait_until(lambda: runner.usb_reset_ok is not None, timeout=5.0)
    status = session.status()
    runner.stop()
    assert status["usb_reset_ok"] is False
    assert any("no usb reset on this unit (no usbreset here)" in l
               for l in runner.recent(20))


# ---- a recovery attempt can never fail a session or lose a cue (N1) ----
# Review of 6a2d136 (review_T8/t_raise.py): a usb_reset that RAISED - from
# comports(), an undecodable usbreset output, anything - left the port closed
# and escaped into _run_remote: "ERROR bus boom", the session failed and the
# next cue never fired. One test per path, each with a reset that raises.

def raising_reset(port, timeout=None):
    raise RuntimeError("boom")


def test_a_reset_that_raises_on_the_idle_path_fails_nothing(monkeypatch):
    bus = degraded(monkeypatch)
    runner = degraded_runner(bus, usb_reset=raising_reset)
    time.sleep(0.25)
    result = runner._recover_bus(bus, 2)
    assert result["recovered"] is False
    assert bus.reopened == ["/dev/ttyACM0"]      # the port is open again
    assert any("bus recovery failed" in l and "usb reset raised: boom" in l
               for l in runner.recent(20))


def test_a_reset_that_raises_before_a_cue_loses_no_cue(monkeypatch):
    """The reviewer's t_raise, compressed: the master is silent at the
    pre-cue check, the reset raises - and the cue still fires on time, the
    session never fails."""
    bus, runner, session, at = armed(monkeypatch, usb_reset=raising_reset)
    assert wait_until(lambda: session.phase == FIRED, timeout=10.0)
    runner.stop()
    assert session.phase == FIRED
    late_ms = session.status()["late_ms"]
    assert late_ms is not None and late_ms < 359 + 150, late_ms
    assert any("usb reset raised: boom" in l for l in runner.recent(30))
    assert not [l for l in runner.recent(30) if "ERROR" in l]


def test_a_reset_that_raises_at_fire_time_keeps_the_cue(monkeypatch):
    bus, runner, session, at = fire_on_a_degraded_master(
        monkeypatch, usb_reset=raising_reset)
    assert session.phase == FIRED and len(shows(bus)) == 1
    assert any("re-send failed (usb reset raised: boom)" in l
               for l in runner.recent(40))
    assert not [l for l in runner.recent(40) if "ERROR" in l]


def test_find_port_raising_in_the_middle_of_a_reset_is_survived(monkeypatch):
    bus = DegradedMaster()

    def comports_failed():
        raise OSError("comports failed")
    monkeypatch.setattr("ui.runner.find_port", comports_failed)
    runner = degraded_runner(bus)
    time.sleep(0.25)
    done, why = runner._usb_reset_reopen(
        bus, give_up_at=time.monotonic() + 0.5)
    assert done is True and bus.reopened == ["/dev/ttyACM0"], why


def test_whatever_else_raises_in_a_check_never_fails_the_session(monkeypatch):
    """The outer guards: the pre-cue check and the idle ladder themselves
    raising, for any reason not foreseen above."""
    bus = RecoveringBus("padding", deaf_boards=False)
    bus.stalled = False
    runner = recovery_runner(bus, precheck=3.6, remote_guard_hold=HOLD)

    def kaboom(*args, **kwargs):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(runner, "_precheck", kaboom)
    monkeypatch.setattr(runner, "_recover_bus_steps", kaboom)
    session = RemoteSession(runner)
    session.prepare("c1", {1: array(1), 2: array(2)})
    assert wait_until(lambda: session.phase == READY, timeout=10.0)
    session.fire("c1", time.monotonic() + 4.0)
    assert wait_until(lambda: session.phase == FIRED, timeout=10.0)
    result = runner._recover_bus(bus, 2)
    runner.stop()
    assert session.phase == FIRED
    assert result["recovered"] is False
    said = runner.recent(200)
    assert any("precheck c1 raised: kaboom" in l for l in said)
    assert any("bus recovery raised: kaboom" in l for l in said)


# ---- Recover bus never takes over a unit on its own menu (N2) ----

def test_recover_bus_on_a_unit_playing_its_own_demo_is_refused(monkeypatch):
    """Review of 6a2d136, N2 (review_T8/t_press.py L): recover_bus() used to
    call start_remote() - the demo stopped, a REMOTE worker started, STOPs
    and per-board probes went out. Now it is refused, and nothing moves."""
    bus = FakeBus(deaf_boards=False)
    resets = []
    runner = make_runner(bus, boards=[1, 2, 3], verify_fire=False,
                         usb_reset=lambda p, timeout=None: (
                             resets.append(p), (False, "t"))[1])
    session = RemoteSession(runner)
    runner.start(BY_KEY["wave"])
    assert wait_until(lambda: runner.cycle >= 1, timeout=20.0)
    agent = Agent(session, port=0, host="127.0.0.1", name="radxa-02")
    agent.start()
    try:
        code, answer = call(agent, "/bus/recover", {})
    finally:
        agent.stop()
    still_the_demo = (runner.running and runner.pattern is not None
                      and runner.remote is None)
    runner.stop()
    assert code == 409
    assert answer["error"] == ("unit is on its own menu - nothing to "
                               "recover from here")
    assert still_the_demo and resets == []


def test_recover_bus_on_an_idle_unit_starts_no_worker():
    bus = FakeBus()
    runner = make_runner(bus)
    session = RemoteSession(runner)
    with pytest.raises(RemoteError, match="own menu"):
        session.recover_bus(timeout=1.0)
    assert not runner.running and runner.remote is None


# ---- the pre-cue check: a USB reset before the cue, on its own clock ----

HOLD = 0.3          # a compressed REMOTE_GUARD_HOLD_S for these tests


def armed(monkeypatch, lead=4.0, degrade=True, **kwargs):
    """Setup on a healthy master, then (optionally) it degrades, then a cue
    `lead` seconds out - so the setup's probes are not what is measured."""
    fake = {k: kwargs.pop(k) for k in list(kwargs)
            if k in ("reset_s", "reenum_s", "open_s", "reset_ok",
                     "reset_cures", "quick_gap")}
    bus = degraded(monkeypatch, **fake)
    bus.degraded = False
    kwargs.setdefault("remote_guard_hold", HOLD)
    kwargs.setdefault("precheck", 3.6)
    runner = degraded_runner(bus, **kwargs)
    session = RemoteSession(runner)
    session.prepare("c1", {1: array(1), 2: array(2)})
    assert wait_until(lambda: session.phase == READY, timeout=10.0)
    time.sleep(0.3)
    bus.degraded = degrade
    at = time.monotonic() + lead
    session.fire("c1", at)
    return bus, runner, session, at


def in_the_hold(bus, at, hold=HOLD):
    """Every frame other than the trigger itself that was handed to the
    port inside the last `hold` seconds before `at`."""
    out = []
    for frame, stamp in zip(bus.sent, bus.sent_at):
        if at - hold < stamp < at and frame.cmd != SHOW:
            out.append((round(stamp - at, 3), hex(frame.cmd)))
    return out


def test_the_precheck_before_a_healthy_cue_asks_the_master(monkeypatch):
    bus, runner, session, at = armed(monkeypatch, degrade=False)
    assert wait_until(lambda: session.phase == FIRED, timeout=10.0)
    runner.stop()
    assert runner.precheck["cue"] == "c1" and runner.precheck["by"] is None
    assert runner.precheck["master_answers"] is True
    assert any("precheck c1: bus ok" in l and "board 1 answers" in l
               for l in runner.recent(30))
    assert bus.resets == [] and in_the_hold(bus, at) == []
    assert runner.remote_guard_sent == 0


def test_a_degraded_master_before_a_cue_is_reset_and_the_cue_is_on_time(
        monkeypatch):
    bus, runner, session, at = armed(monkeypatch)
    assert wait_until(lambda: session.phase == FIRED, timeout=10.0)
    runner.stop()
    assert runner.precheck["by"] == "usb_reset" and len(bus.resets) == 1
    assert any("precheck c1: stalled" in l
               and "→ usb reset → ok (board 1 answers)" in l
               for l in runner.recent(30))
    late_ms = session.status()["late_ms"]
    assert late_ms is not None and late_ms < 250, late_ms
    assert in_the_hold(bus, at) == []
    assert len(shows(bus)) == 1                   # nothing left to re-send


def test_a_silent_master_is_caught_even_when_the_stop_reads_fast(monkeypatch):
    """The STOP right behind another write reads fast on a degraded master;
    the 0x02 is what gives it away."""
    bus, runner, session, at = armed(monkeypatch, quick_gap=100.0)
    assert wait_until(lambda: session.phase == FIRED, timeout=10.0)
    runner.stop()
    assert runner.precheck["by"] == "usb_reset"
    assert any("precheck c1: board 1 silent" in l and "usb reset" in l
               for l in runner.recent(30))


def test_a_precheck_reset_that_does_not_take_is_said_and_the_cue_fires(
        monkeypatch):
    bus, runner, session, at = armed(monkeypatch, reset_cures=False)
    assert wait_until(lambda: session.phase == FIRED, timeout=10.0)
    runner.stop()
    assert runner.precheck["by"] is None
    assert any("→ usb reset → no answer (board 1 silent)" in l
               for l in runner.recent(30))
    assert in_the_hold(bus, at) == []
    assert len(shows(bus)) >= 1                   # the picture still goes


def test_no_time_for_a_usb_reset_is_said_and_nothing_is_begun(monkeypatch):
    bus = degraded(monkeypatch)
    runner = degraded_runner(bus, precheck=20.0, remote_guard_hold=HOLD)
    time.sleep(0.25)
    # Room for the measuring STOP, none for USB_RESET_BUDGET_S behind it.
    runner._precheck(bus, 2, "q06", time.monotonic() + HOLD + 1.0)
    assert bus.resets == [] and bus.closes == 0
    assert any("no time for a usb reset" in l for l in runner.recent(20))


def test_a_cue_armed_inside_the_hold_gets_no_precheck_at_all():
    """Review F3: a cue armed 20 ms out had its precheck STOP at T-0.02 s -
    straight into the trigger. Not begun with less than the hold + one
    degraded write left, and nothing is logged about it."""
    bus = RecoveringBus("padding")
    bus.stalled = False
    runner = recovery_runner(bus)                # the REAL hold, 5 s
    session = RemoteSession(runner)
    session.prepare("c1", {1: array(1), 2: array(2)})
    assert wait_until(lambda: session.phase == READY, timeout=10.0)
    armed_at = time.monotonic()
    at = armed_at + 1.0
    session.fire("c1", at)
    assert wait_until(lambda: session.phase == FIRED, timeout=10.0)
    runner.stop()
    assert runner.precheck is None
    assert not [line for line in runner.recent(30) if "precheck" in line]
    assert in_the_hold(bus, at, hold=at - armed_at) == []


def test_the_precheck_waits_for_the_last_picture_to_finish():
    bus = RecoveringBus("padding", deaf_boards=False)
    bus.stalled = False
    runner = recovery_runner(bus, precheck=6.0)
    now = time.monotonic()
    runner._guard_floor = now + 30.0             # a picture still drawing
    runner._precheck(bus, 2, "c2", now + 5.6)
    assert runner.precheck is None and bus.sent == []
    runner._guard_floor = now - 1.0              # ...and now it is done
    runner._precheck(bus, 2, "c2", time.monotonic() + 5.6)
    assert runner.precheck is not None and runner.precheck["cue"] == "c2"
    before = dict(runner.precheck)
    runner._precheck(bus, 2, "c2", time.monotonic() + 5.5)
    assert runner.precheck == before             # once per cue


def test_a_precheck_block_below_the_threshold_is_not_put_on_the_record():
    bus = RecoveringBus("never", seconds=0.12, deaf_boards=False)
    runner = recovery_runner(bus, precheck=6.0)
    runner._precheck(bus, 2, "c1", time.monotonic() + 5.6)
    assert runner.precheck["before_ms"] >= 100
    assert runner.bus_stall is None


def test_precheck_zero_switches_the_check_off():
    bus = RecoveringBus("padding", seconds=0.25)
    runner = recovery_runner(bus, precheck=0)
    runner._precheck(bus, 2, "c1", time.monotonic() + 5.6)
    assert runner.precheck is None and bus.sent == []
    assert runner.precheck_s == 0


def test_the_budgets_hold_with_the_units_own_timings(monkeypatch):
    """Measured, not asserted from the arithmetic: with the unit's timings
    (358 ms writes, a 0.3 s reset, the node back 0.44 s later, a 0.3 s open)
    the pre-cue check begun at T-8.5 is done before T-5.0, and the idle
    recovery with the REAL 3 s proof gap is inside the agent's 15 s."""
    from ui.runner import PRECHECK_S, PROOF_GAP_S, REMOTE_GUARD_HOLD_S

    bus = degraded(monkeypatch, **UNIT_TIMINGS)
    runner = degraded_runner(bus, precheck=PRECHECK_S,
                             remote_guard_hold=REMOTE_GUARD_HOLD_S)
    time.sleep(0.25)
    began = time.monotonic()
    at = began + PRECHECK_S
    runner._precheck(bus, 2, "q03", at)
    took = time.monotonic() - began
    assert runner.precheck["by"] == "usb_reset"
    assert began + took < at - REMOTE_GUARD_HOLD_S, took       # before T-5.0

    bus2 = degraded(monkeypatch, **UNIT_TIMINGS)
    runner2 = degraded_runner(bus2, proof_gap=PROOF_GAP_S)
    time.sleep(0.25)
    began = time.monotonic()
    assert runner2._recover_bus(bus2, 2)["by"] == "usb_reset"
    assert time.monotonic() - began < 15.0


# ---- the probe sweep a fast reopen owes (review F5) ----

def test_a_re_enumeration_before_a_cue_puts_nothing_into_its_picture(monkeypatch):
    """The reviewer's scenario (review_T4/t_owed.py): the port re-enumerates
    at T-1.7 s while the cue is armed. The watcher reopens it - the port
    ONLY, since a frame there would be inside the hold - the cue fires on
    time, and the probe sweep that is now owed does NOT follow straight on
    behind the show frame: not one 0x17 or 0x1B between the trigger and
    that cue's guard floor. It runs after it."""
    bus = RecoveringBus("reopen", port="/dev/ttyACM0")
    bus.stalled = False
    names = ["/dev/ttyACM0"]
    monkeypatch.setattr("ui.runner.find_port", lambda: names[-1])
    stamps = []
    send, request = bus.send, bus.request

    def stamped_send(frame):
        send(frame)
        stamps.append((time.monotonic(), frame.cmd))

    def stamped_request(frame, retries=3, timeout=None):
        stamps.append((time.monotonic(), frame.cmd))
        return request(frame, retries=retries, timeout=timeout)
    bus.send, bus.request = stamped_send, stamped_request

    # owed_settle short: this is about ORDER against the floor. The 20 s
    # settle itself has a test of its own below.
    runner = recovery_runner(bus, port=None, link_token=bus.token,
                             port_poll=0.02, port_back_wait=1.0,
                             owed_settle=0.1)
    session = RemoteSession(runner)
    session.prepare("c1", {1: array(1), 2: array(2)}, span_s=0.5,
                    refresh_s=0.8)
    assert wait_until(lambda: session.phase == READY, timeout=10.0)
    at = time.monotonic() + 2.0
    session.fire("c1", at)
    time.sleep(0.3)
    names.append("/dev/ttyACM1")
    bus.unplug(back_as="/dev/ttyACM1")
    assert wait_until(lambda: session.phase == FIRED, timeout=5.0)
    fired = session.fired_at
    floor = runner._guard_floor
    assert floor is not None and floor > fired + 1.0
    # The owed sweep, after the floor: per-board probes of both boards.
    assert wait_until(lambda: any(t > floor and cmd == CFG
                                  for t, cmd in stamps), timeout=5.0)
    runner.stop()
    assert bus.reopened == ["/dev/ttyACM1"]
    assert abs(session.status()["late_ms"]) < 100
    between = [(round(t - fired, 3), hex(cmd)) for t, cmd in stamps
               if fired < t < floor - 0.02 and cmd in (STOP, CFG)]
    assert between == [], between
    # ...and nothing but the trigger in the hold before it either: the
    # reopen at T-1.7 s was the port alone.
    assert [cmd for t, cmd in stamps
            if at - 5.0 < t < at and cmd != SHOW and t > at - 1.8] == []
    assert any("port only" in line for line in runner.recent(40))


def _preset_after_a_recovery(monkeypatch, owed_settle):
    """The re-review's t_preset.py scenario, compressed: six boards, a
    recovery that ends "by usb_reset" (so the sweep is owed), then the preset
    armed ~0.45 s later, the way the Conductor sends it. Returns the stamps
    (time, cmd, dest) of every frame, the fire time and that cue's floor."""
    bus = degraded(monkeypatch)
    bus.degraded = False
    stamps = []
    send, request = bus.send, bus.request

    def stamped_send(frame):
        send(frame)
        stamps.append((time.monotonic(), frame.cmd, frame.dest))

    def stamped_request(frame, retries=3, timeout=None):
        stamps.append((time.monotonic(), frame.cmd, frame.dest))
        return request(frame, retries=retries, timeout=timeout)
    bus.send, bus.request = stamped_send, stamped_request
    boards = [1, 2, 3, 4, 5, 6]
    runner = degraded_runner(bus, boards=boards, owed_settle=owed_settle,
                             recover_quiet=0.5)
    session = RemoteSession(runner)
    session.prepare("c1", {b: array(b) for b in boards}, span_s=1.0,
                    refresh_s=1.0)
    assert wait_until(lambda: session.phase == READY, timeout=10.0)
    time.sleep(0.3)
    bus.degraded = True
    answer = session.recover_bus()
    assert answer["by"] == "usb_reset", answer
    time.sleep(0.15)                       # the Conductor posts the preset
    session.fire("c1", time.monotonic() + 0.3)
    assert wait_until(lambda: session.phase == FIRED, timeout=10.0)
    fired, floor = session.fired_at, runner._guard_floor
    assert floor is not None and floor >= fired + 1.9
    time.sleep(max(0.0, floor - time.monotonic()) + 0.3)
    return runner, stamps, fired, floor


def test_an_owed_sweep_waits_long_enough_for_the_preset_to_go_first(monkeypatch):
    """Re-review of e139a33, first end: a recovery is pressed right before
    ② Show preset, and the sweep it owed started at once - the preset then
    fired from inside it and it probed every board through the repaint.
    With OWED_SETTLE_S it has not even begun when the preset fires."""
    runner, stamps, fired, floor = _preset_after_a_recovery(monkeypatch, owed_settle=20.0)
    runner.stop()
    between = [(round(t - fired, 3), hex(c), d) for t, c, d in stamps
               if fired < t < floor - 0.02 and c != SHOW]
    assert between == [], between
    # ...and it has not run at all yet: still owed, inside its settle.
    assert runner._setup_owed is True
    assert not [l for l in runner.recent(40) if "stood aside" in l]


def test_an_owed_sweep_already_running_stands_down_for_the_cue(monkeypatch):
    """The other end: if the sweep IS running when a cue fires from inside
    it, it stops before its next probe - nothing but the show frame until
    that cue's floor - and stays owed, with the unit reading as before."""
    runner, stamps, fired, floor = _preset_after_a_recovery(monkeypatch, owed_settle=0.0)
    boards_after = list(runner.boards)
    runner.stop()
    between = [(round(t - fired, 3), hex(c), d) for t, c, d in stamps
               if fired < t < floor - 0.02 and c != SHOW]
    assert between == [], between
    assert any("probe sweep stood aside for the cue, still owed" in l
               for l in runner.recent(60))
    assert boards_after == [1, 2, 3, 4, 5, 6]     # nothing half-reset


def test_the_owed_sweep_waits_for_the_run_to_end_or_a_minute_of_quiet():
    bus = RecoveringBus("padding")
    bus.stalled = False
    runner = recovery_runner(bus, recover_quiet=60.0)
    session = RemoteSession(runner)
    now = time.monotonic()
    runner._guard_floor = now + 5.0
    assert runner._owed_setup_clear(session) is False     # picture drawing
    runner._guard_floor = now - 1.0
    session.playing = lambda: True
    assert runner._owed_setup_clear(session) is False     # a run in flight
    session.playing = lambda: False
    session.arm("c1", 1)
    session.fire("c1", time.monotonic() + 30.0)
    assert runner._owed_setup_clear(session) is False     # a cue in 30 s
    session.cancel()
    assert runner._owed_setup_clear(session) is True
    runner.stop()


# ---- noticing a re-enumeration at once ----

def test_a_re_enumeration_is_found_at_once_and_the_port_comes_back(monkeypatch):
    """Today the unit notices a lost port at its next WRITE - up to 20 s
    later - and takes ~5 s to come back. Watched, it is under a second."""
    bus = RecoveringBus("reopen", port="/dev/ttyACM0")
    bus.stalled = False
    names = ["/dev/ttyACM0"]
    monkeypatch.setattr("ui.runner.find_port", lambda: names[-1])
    runner = recovery_runner(bus, port=None, link_token=bus.token,
                             port_poll=0.02, port_back_wait=1.0)
    session = RemoteSession(runner)
    session.arm("c1", 1)                          # a worker on the port, idle
    assert wait_until(lambda: bus.sent, timeout=5.0)
    names.append("/dev/ttyACM1")                  # ttyACM0 -> ttyACM1
    bus.unplug(back_as="/dev/ttyACM1")
    assert wait_until(lambda: bus.reopened, timeout=5.0)
    runner.stop()
    assert bus.reopened == ["/dev/ttyACM1"]
    assert bus.port == "/dev/ttyACM1"
    line = [l for l in runner.recent(40) if "port lost" in l]
    assert line and "/dev/ttyACM1 back in" in line[0] and "bus ok" in line[0]


def test_a_port_that_does_not_come_back_is_left_to_the_ordinary_ladder():
    """A cable out is not a re-enumeration: nothing is gained by reopening a
    node that is not there, and the existing reopen/port_wait ladder is the
    right answer to it."""
    bus = RecoveringBus("reopen", port="/dev/ttyACM0")
    bus.stalled = False
    runner = recovery_runner(bus, port=None, link_token=bus.token,
                             port_poll=0.01, port_back_wait=0.15)
    bus.unplug(back_as="/dev/nothing")
    assert runner._port_watch(bus, 2) is False
    assert bus.reopened == []
    assert any("port gone for" in line for line in runner.recent(20))


class SlowReopenBus(RecoveringBus):
    """A real open settles 0.3 s (transport.Bus._open) - long enough for a
    verdict taken before it to be inside the hold by the time it acts."""

    def reopen(self, port=None):
        time.sleep(0.3)
        return super().reopen(port)


def _lost_at(monkeypatch, lead, bus_class=RecoveringBus):
    bus = bus_class("reopen", port="/dev/ttyACM0")
    bus.stalled = False
    monkeypatch.setattr("ui.runner.find_port", lambda: "/dev/ttyACM1")
    runner = recovery_runner(bus, port=None, link_token=bus.token,
                             port_poll=0.01)          # the REAL 5 s hold
    bus.unplug(back_as="/dev/ttyACM1")
    at = time.monotonic() + lead
    assert runner._port_watch(bus, 2, at=at) is True
    return bus, runner


def test_a_port_lost_just_outside_the_hold_comes_back_as_the_port_only(
        monkeypatch):
    """Re-review of e139a33: found at T-5.1 s the port may be reopened, but
    no frame of it can be handed over before T-5.0 - so none goes."""
    bus, runner = _lost_at(monkeypatch, lead=5.1)
    assert bus.reopened == ["/dev/ttyACM1"]
    assert bus.sent == [] and bus.requested == []
    assert any("port only (a cue is too near)" in line
               for line in runner.recent(20))


def _idle_loss(monkeypatch, floor_in):
    """An IDLE re-enumeration - no cue armed, the watcher's `at` is None -
    with the last picture's floor `floor_in` seconds away (negative: past)."""
    bus = RecoveringBus("reopen", port="/dev/ttyACM0")
    bus.stalled = False
    monkeypatch.setattr("ui.runner.find_port", lambda: "/dev/ttyACM1")
    runner = recovery_runner(bus, port=None, link_token=bus.token,
                             port_poll=0.01)
    runner.remote = RemoteSession(runner)          # nothing armed
    runner._guard_floor = time.monotonic() + floor_in
    bus.unplug(back_as="/dev/ttyACM1")
    assert runner._port_watch(bus, 2) is True      # at=None: the idle call
    return bus, runner


def test_an_idle_port_loss_inside_the_last_picture_sends_no_frame(monkeypatch):
    """Round-3 review HIGH: with at=None - the idle loop's call - both frames
    of the reopen went out UNCHECKED, into the repaint of the last picture
    (t_idle_floor.py: a 0x17 and a 0x1B at +1.5 s, the floor at +10 s).
    That is LOOK28 exactly: a re-enumeration 10-30 s after the last cue,
    which would leave the last look half-drawn."""
    bus, runner = _idle_loss(monkeypatch, floor_in=10.0)
    assert bus.reopened == ["/dev/ttyACM1"]
    assert bus.sent == [] and bus.requested == [], (bus.sent, bus.requested)
    assert any("port only (the last picture is still repainting)" in line
               for line in runner.recent(20))


def test_an_idle_port_loss_after_the_last_picture_is_reopened_and_measured(
        monkeypatch):
    bus, runner = _idle_loss(monkeypatch, floor_in=-1.0)
    assert [f.cmd for f in bus.sent][:1] == [STOP]          # frames went
    assert [f.dest for f in bus.requested] == [1]           # master config
    assert any("bus ok" in line for line in runner.recent(20))


def test_the_idle_worker_itself_keeps_frames_out_of_the_last_picture(
        monkeypatch):
    """The reviewer's t_idle_floor.py end to end: the worker's own idle loop
    finds the loss 1.5 s after the fire, well inside the floor."""
    bus = RecoveringBus("reopen", port="/dev/ttyACM0")
    bus.stalled = False
    names = ["/dev/ttyACM0"]
    monkeypatch.setattr("ui.runner.find_port", lambda: names[-1])
    runner = recovery_runner(bus, port=None, link_token=bus.token,
                             port_poll=0.02, port_back_wait=1.0, precheck=0)
    session = RemoteSession(runner)
    session.prepare("c1", {1: array(1), 2: array(2)}, span_s=4.0,
                    refresh_s=6.0)
    assert wait_until(lambda: session.phase == READY, timeout=10.0)
    session.fire("c1", time.monotonic() + 1.0)
    assert wait_until(lambda: session.phase == FIRED, timeout=5.0)
    floor = runner._guard_floor
    assert floor is not None and floor - time.monotonic() > 5.0
    mark_sent, mark_req = len(bus.sent), len(bus.requested)
    names.append("/dev/ttyACM1")
    bus.unplug(back_as="/dev/ttyACM1")
    assert wait_until(lambda: bus.reopened, timeout=5.0)
    time.sleep(0.3)
    inside = time.monotonic() < floor
    new_sent, new_req = bus.sent[mark_sent:], bus.requested[mark_req:]
    runner.stop()
    assert inside, "the check ran past the floor - lengthen the refresh"
    assert new_sent == [] and new_req == [], (new_sent, new_req)
    assert any("port only (the last picture is still repainting)" in line
               for line in runner.recent(40))


class FailingAfterOpenBus(RecoveringBus):
    """Reopens fine; the first frame on the new port raises."""

    def send(self, frame):
        if self.reopened:
            raise OSError(5, "Input/output error")
        super().send(frame)


def test_a_write_that_fails_after_the_reopen_is_said_not_swallowed(monkeypatch):
    """Round-3 review LOW-4: the reason frames did not go is always named -
    and a failed write is one of them, never passed over in silence."""
    bus, runner = _lost_at(monkeypatch, lead=30.0,
                           bus_class=FailingAfterOpenBus)
    said = [line for line in runner.recent(20) if "port lost" in line]
    assert said and "port only (write failed:" in said[0], said
    assert "Input/output error" in said[0]


def test_the_precheck_is_not_begun_inside_one_degraded_write_of_the_hold():
    """Round-3 review LOW-3: the measuring STOP is itself a write that may
    block DEGRADED_WRITE_S (0.4 s), so the bound is hold + 0.4 = 5.4 s -
    begun at T-5.25 it could block on to T-4.84, inside the hold."""
    bus = RecoveringBus("padding")
    bus.stalled = False
    runner = recovery_runner(bus)                  # the REAL 5 s hold
    runner._precheck(bus, 2, "c1", time.monotonic() + 5.39)
    assert runner.precheck is None and bus.sent == []
    runner._precheck(bus, 2, "c2", time.monotonic() + 5.41)
    assert runner.precheck is not None and runner.precheck["cue"] == "c2"


def test_a_start_up_sweep_never_stands_down_even_if_a_worker_left_the_flag():
    """Round-3 review LOW-1: `_setup_yields` could outlive the pass that set
    it - a worker stopped with it set let the NEXT worker's start-up sweep
    stand down. It is reset per worker, and set in exactly one place."""
    bus = RecoveringBus("padding")
    bus.stalled = False
    runner = recovery_runner(bus, boards=[1, 2, 3, 4])
    runner._setup_yields = True                    # left over by a worker
    session = RemoteSession(runner)
    session.arm("c1", 1)
    session.fire("c1", time.monotonic() + 0.15)    # fires inside the sweep
    assert wait_until(lambda: session.phase == FIRED, timeout=10.0)
    assert wait_until(lambda: any("panels online" in line
                                  for line in runner.recent(40)), timeout=10.0)
    runner.stop()
    assert not [l for l in runner.recent(60) if "stood aside" in l]
    source = (Path(__file__).resolve().parents[1] / "ui" / "runner.py"
              ).read_text(encoding="utf-8")
    assert source.count("self._setup_yields = True") == 1


def test_the_frames_are_decided_after_the_slow_open_not_before_it(monkeypatch):
    """Found at T-5.5 s the wire was still clear - but the open takes 0.3 s,
    and by then a STOP would land inside the hold. Decided after the open,
    frame by frame, nothing goes."""
    bus, runner = _lost_at(monkeypatch, lead=5.5, bus_class=SlowReopenBus)
    assert bus.reopened == ["/dev/ttyACM1"]
    assert bus.sent == [] and bus.requested == []


def test_a_port_lost_well_before_a_cue_is_reopened_and_measured(monkeypatch):
    bus, runner = _lost_at(monkeypatch, lead=30.0)
    assert [f.cmd for f in bus.sent][:1] == [STOP]        # frames went
    assert any("bus ok" in line for line in runner.recent(20))


def test_the_wait_for_the_port_never_runs_past_a_trigger(monkeypatch):
    bus = RecoveringBus("reopen", port="/dev/ttyACM0")
    bus.stalled = False
    monkeypatch.setattr("ui.runner.find_port", lambda: None)
    runner = recovery_runner(bus, port=None, link_token=bus.token,
                             port_poll=0.01, port_back_wait=3.0)
    bus.unplug(back_as="/dev/nothing")
    at = time.monotonic() + 0.4
    assert runner._port_watch(bus, 2, at=at) is False
    assert time.monotonic() < at + 0.05          # back before the trigger
    assert any("not waiting past it" in line for line in runner.recent(20))


def test_the_wait_for_the_port_lets_go_of_a_cancelled_cue(monkeypatch):
    bus = RecoveringBus("reopen", port="/dev/ttyACM0")
    bus.stalled = False
    monkeypatch.setattr("ui.runner.find_port", lambda: None)
    runner = recovery_runner(bus, port=None, link_token=bus.token,
                             port_poll=0.01, port_back_wait=3.0)
    bus.unplug(back_as="/dev/nothing")
    cancelled_at = time.monotonic() + 0.3
    began = time.monotonic()
    assert runner._port_watch(bus, 2, at=time.monotonic() + 10.0,
                              wanted=lambda: time.monotonic() < cancelled_at) \
        is False
    assert time.monotonic() - began < 0.3 + 0.2   # noticed within 200 ms


def test_the_watcher_stands_aside_for_the_probing_sweep_and_its_switch():
    bus = RecoveringBus("reopen", port="/dev/ttyACM0")
    bus.stalled = False
    runner = recovery_runner(bus, port=None, link_token=bus.token,
                             port_poll=0.01)
    bus.unplug(back_as="/dev/ttyACM0")            # comes straight back
    bus.gone, bus.back_as = "/dev/ttyACM0", "/dev/elsewhere"
    runner._probing = True
    assert runner._port_watch(bus, 2) is False and bus.reopened == []
    runner._probing = False
    off = recovery_runner(bus, port=None, link_token=bus.token,
                          port_poll=0.01, port_watch=False)
    assert off._port_watch(bus, 2) is False and bus.reopened == []


# ---- the recovery the operator asks for respects the picture (review F6) ----

def test_the_endpoint_refuses_while_the_last_picture_is_drawing():
    bus = RecoveringBus("padding")
    runner = recovery_runner(bus)
    session = RemoteSession(runner)
    runner._guard_floor = time.monotonic() + 12.3
    agent = Agent(session, port=0, host="127.0.0.1", name="radxa-07")
    agent.start()
    try:
        code, answer = call(agent, "/bus/recover", {})
    finally:
        agent.stop()
        runner.stop()
    assert code == 409
    assert answer["error"] == "a repaint is in progress - try again in 13 s"
    assert bus.padded == [] and bus.reopened == []


def test_a_gate_that_closes_before_the_worker_takes_the_job_still_refuses():
    bus = RecoveringBus("padding")
    bus.stalled = False
    runner = recovery_runner(bus)
    session = RemoteSession(runner)
    session.arm("c1", 1)                          # a worker on the port
    assert wait_until(lambda: bus.sent, timeout=5.0)
    # The request passed its gates a moment ago; now a picture has begun.
    runner._guard_floor = time.monotonic() + 30.0
    job = {"done": threading.Event(), "result": None}
    session._recover_job = job
    session.wake()
    assert job["done"].wait(5.0)
    runner.stop()
    assert job["result"]["error"].startswith("a repaint is in progress")
    assert bus.padded == [] and bus.reopened == []


def test_no_auto_recover_leaves_a_stalling_bus_to_the_operator():
    bus = RecoveringBus("padding")
    runner = recovery_runner(bus, auto_recover=False)
    runner.remote = RemoteSession(runner)
    runner._maybe_recover(bus, 2, 359.0)
    runner._maybe_recover(bus, 2, 359.0)
    assert runner.bus_recovery is None and bus.padded == []


def test_the_status_says_which_kill_switches_are_on():
    session, runner, bus = make_session()
    status = session.status()
    assert status["precheck_s"] == 8.5       # PM: both cures fit (SPEC 4.5)
    assert status["port_watch"] is True and status["auto_recover"] is True
    runner.stop()
    off = make_runner(FakeBus(), precheck=0, port_watch=False,
                      auto_recover=False)
    status = RemoteSession(off).status()
    assert status["precheck_s"] == 0
    assert status["port_watch"] is False and status["auto_recover"] is False


# ---- final gate on 393ddcd: ownership is the PC's worker, nothing else ----

def _owned_runner():
    bus = FakeBus(deaf_boards=False)
    resets = []
    runner = make_runner(bus, boards=[1, 2, 3], verify_fire=False,
                         usb_reset=lambda p, timeout=None: (
                             resets.append(p), (False, "t"))[1])
    return RemoteSession(runner), runner, bus, resets


def test_an_uploaded_unit_is_owned_and_may_be_recovered():
    """t_owned U: after an Upload (burn) the PC's REMOTE worker holds the
    port although the phase still reads `local` - owned, so Recover bus is
    offered and accepted. The phase was never the thing to ask."""
    session, runner, bus, resets = _owned_runner()
    session.burn([{"slot": 1, "boards": {1: array(1), 2: array(2),
                                         3: array(3)},
                   "delays": {}, "span_s": None}])
    assert wait_until(lambda: (session.status().get("burn") or {}).get(
        "state") not in (None, "burning"), timeout=30.0)
    try:
        assert runner.remote is session and runner.running
        assert session.owned() is True
        assert session.recover_refusal() is None
        assert session.status()["owned"] is True
    finally:
        runner.stop()


def test_a_unit_in_the_conductors_standby_is_not_owned():
    """t_owned S: the per-unit Standby is a one-shot pattern worker, not the
    PC's REMOTE worker - not owned, and the 409 says what to do."""
    session, runner, bus, resets = _owned_runner()
    session.standby()
    assert wait_until(lambda: session.phase == STANDBY, timeout=10.0)
    try:
        assert runner.remote is not session
        assert session.owned() is False
        assert session.status()["owned"] is False
        assert session.recover_refusal() == ("unit is in standby - Upload "
                                             "first, then recover")
        with pytest.raises(RemoteError, match="in standby"):
            session.recover_bus(timeout=1.0)
    finally:
        runner.stop()
    assert resets == []


def test_a_fresh_unit_says_it_is_not_owned():
    session, runner, bus = make_session()
    assert session.status()["owned"] is False
    assert session.recover_refusal() == ("unit is on its own menu - nothing "
                                         "to recover from here")


def test_a_recover_job_on_a_worker_that_exits_is_answered_at_once():
    """Final gate, LOW: the job used to wait out RECOVER_WAIT_S (15 s) for a
    worker thread that had already gone. Now the wait notices."""
    session, runner, bus = make_session()
    runner.remote = session
    runner._thread = threading.Thread(target=time.sleep, args=(0.4,),
                                      daemon=True)
    runner._thread.start()
    began = time.monotonic()
    with pytest.raises(RemoteError, match="unit's worker is not running"):
        session.recover_bus(timeout=15.0)
    assert time.monotonic() - began < 2.0
    assert session._recover_job is None
    # ...and one asked for once it has gone is refused before it is queued.
    with pytest.raises(RemoteError, match="unit's worker is not running"):
        session.recover_bus(timeout=15.0)
    assert session._recover_job is None


def _stop_fails_once(monkeypatch, runner):
    real = runner._send_timed
    failed = []

    def once(bus_, frame, what, **kwargs):
        if frame.cmd == STOP and not failed:
            failed.append(frame)
            raise OSError("write timeout")
        return real(bus_, frame, what, **kwargs)

    monkeypatch.setattr(runner, "_send_timed", once)
    return failed


def test_a_precheck_whose_stop_fails_is_not_bus_ok(monkeypatch):
    """Final gate, LOW: a STOP whose write raised can be fast, and was read
    as "bus ok". It is "write failed", and the reset path follows as for a
    silent master."""
    bus = degraded(monkeypatch)
    bus.degraded = False
    runner = degraded_runner(bus, precheck=20.0, remote_guard_hold=HOLD)
    failed = _stop_fails_once(monkeypatch, runner)
    runner._precheck(bus, 2, "q07", time.monotonic() + HOLD + 3.5)
    said = [l for l in runner.recent(20) if "precheck q07" in l]
    assert failed and said, runner.recent(20)
    assert "bus ok" not in said[0]
    assert "write failed: write timeout" in said[0]
    assert "→ usb reset" in said[0] and len(bus.resets) == 1
    assert runner.precheck["by"] == "usb_reset"


def test_a_failed_stop_is_not_clear_to_an_idle_recovery(monkeypatch):
    """The same for Recover bus: a failed measuring STOP is not "nothing
    was wrong" - the reset follows, and its proof decides."""
    bus = degraded(monkeypatch)
    bus.degraded = False
    runner = degraded_runner(bus)
    failed = _stop_fails_once(monkeypatch, runner)
    result = runner._recover_bus(bus, 2)
    assert failed and len(bus.resets) == 1
    assert result["by"] == "usb_reset" and result["recovered"] is True


def test_the_port_watch_survives_find_port_and_link_token_raising(
        monkeypatch):
    """Final gate, LOW: comports() or the node's stat can raise; the
    watcher answers "nothing to do" instead of failing the session."""
    bus = RecoveringBus("reopen", port="/dev/ttyACM0")
    bus.stalled = False

    def boom(*args):
        raise OSError("comports failed")

    monkeypatch.setattr("ui.runner.find_port", boom)
    runner = recovery_runner(bus, port=None, link_token=boom, port_poll=0.01,
                             port_back_wait=0.15)
    assert runner._port_watch(bus, 2) is False     # cannot tell: no guess
    assert bus.reopened == []
    # The node gone for real, and the search for it raising every time:
    # the ordinary ladder's answer, not an exception.
    runner._link_token = lambda port: None
    runner._next_port_poll = 0.0
    assert runner._port_watch(bus, 2) is False
    assert bus.reopened == []
    assert any("port gone for" in l for l in runner.recent(20))


# ---- radxa-07, 2026-09-28 12:40-12:46 (main 621669d), end to end ----

def _wire(bus, since, until):
    """Every frame handed to the port in [since, until), in order, as
    (seconds, "send"/"ask", cmd, dest)."""
    out = [(t, "send", f.cmd, f.dest) for f, t in zip(bus.sent, bus.sent_at)]
    out += [(t, "ask", f.cmd, f.dest)
            for f, t in zip(bus.requested, bus.requested_at)]
    return [(round(t - until, 3), how, hex(cmd), dest)
            for t, how, cmd, dest in sorted(out) if since <= t < until]


def test_a_healthy_master_passes_the_precheck_with_two_frames(monkeypatch):
    """12:40:28: `precheck q01: master silent (1 ms) → usb reset → master
    still silent` on a HEALTHY master - the 0x02 it asked is never answered
    on this firmware (the fake is now silent to 0x02 the same way). Asked a
    unicast STOP instead, a healthy master ACKs: the whole check is one
    broadcast STOP and one unicast STOP to board 1, and no reset."""
    bus, runner, session, at = armed(monkeypatch, degrade=False)
    assert wait_until(lambda: session.phase == FIRED, timeout=10.0)
    runner.stop()
    before = _wire(bus, at - 3.6 - 0.2, at - 0.01)
    assert [(how, cmd, dest) for _, how, cmd, dest in before] == [
        ("send", "0x17", 0xFF), ("ask", "0x17", 1)], before
    assert bus.resets == [] and runner.precheck["by"] is None
    assert not any(f.cmd == 0x02 for f in bus.requested)
    assert any("precheck c1: bus ok" in l and "board 1 answers" in l
               for l in runner.recent(30))
    assert len(shows(bus)) >= 1


def test_recover_bus_on_a_healthy_master_says_already_clear(monkeypatch):
    """The same mistake on the idle path: with 0x02 as the signal a healthy
    unit could never say "bus was already clear"."""
    bus = degraded(monkeypatch)
    bus.degraded = False
    runner = degraded_runner(bus)
    result = runner._recover_bus(bus, 2)
    assert result == {"recovered": True, "by": None,
                      "before_ms": result["before_ms"], "after_ms":
                      result["after_ms"]}
    assert bus.resets == []
    assert any("bus is clear, nothing to recover" in l
               for l in runner.recent(10))


def test_a_sweep_that_finds_nobody_resets_the_usb_and_sweeps_again(
        monkeypatch):
    """Fix B: a degraded master at start-up (or Upload, standby, an owed
    sweep) - the sweep finds no boards answering, the master's USB is reset
    and the sweep runs again, instead of the reopen loop that never ends."""
    bus = degraded(monkeypatch)                  # degraded from the start
    runner = degraded_runner(bus, probe_sweeps=1)
    session = RemoteSession(runner)
    session.prepare("c1", {1: array(1), 2: array(2)})
    assert wait_until(lambda: session.phase == READY, timeout=20.0)
    runner.stop()
    said = runner.recent(60)
    assert len(bus.resets) == 1
    assert any("no boards answering → usb reset → panels online: 2/2" in l
               for l in said), said
    assert not any("ERROR no boards answering" in l for l in said)
    assert session.status()["saved"] == [1, 2]


def test_the_setup_reset_is_bounded_and_then_the_old_loop_runs(monkeypatch):
    bus = degraded(monkeypatch, reset_cures=False)
    runner = degraded_runner(bus, probe_sweeps=1, recover_backoff=0.05)
    session = RemoteSession(runner)
    session.prepare("c1", {1: array(1), 2: array(2)})
    assert wait_until(lambda: any("3 usb resets already" in l
                                  for l in runner.recent(80)), timeout=40.0)
    time.sleep(1.0)
    runner.stop()
    said = runner.recent(200)
    assert len(bus.resets) == 3
    assert sum("no boards answering → usb reset → still no boards" in l
               for l in said) == 3
    assert any("ERROR no boards answering" in l for l in said)


def test_the_setup_reset_waits_for_the_last_picture_and_for_the_flag(
        monkeypatch):
    bus = degraded(monkeypatch)
    runner = degraded_runner(bus, auto_recover=False)
    runner.usb_reset_ok = True
    assert runner._setup_usb_reset(bus) is None       # --no-auto-recover
    runner = degraded_runner(bus)
    runner.usb_reset_ok = True
    runner._guard_floor = time.monotonic() + 30.0     # boards repainting
    assert runner._setup_usb_reset(bus) is None
    runner._guard_floor = None
    runner.usb_reset_ok = False                       # no reset means
    assert runner._setup_usb_reset(bus) is None
    assert bus.resets == []


def test_after_the_show_a_degraded_bus_is_recovered_before_the_owed_sweep(
        monkeypatch):
    """12:44:47-12:45:58: a reset earlier in the show left the probe sweep
    owed; the master degraded during the show; after END the owed sweep ran
    FIRST, found nothing and the unit looped on "no boards answering" until
    somebody ran usbreset by hand. Now the owed sweep is begun only on a bus
    whose timed STOP is fast and whose master ACKs; otherwise the recovery
    ladder runs first - reset, proof - and the sweep follows it, and the
    next preset paints."""
    bus = degraded(monkeypatch)
    bus.degraded = False
    runner = degraded_runner(bus, owed_settle=0.3, precheck=0)
    session = RemoteSession(runner)
    session.prepare("c1", {1: array(1), 2: array(2)})
    assert wait_until(lambda: session.phase == READY, timeout=10.0)
    session.fire("c1", time.monotonic() + 0.2)
    assert wait_until(lambda: session.phase == FIRED, timeout=10.0)
    time.sleep(0.5)
    mark = len(runner.recent(500))
    bus.degraded = True                          # ...during the show
    runner._owed_at = time.monotonic()           # the sweep a reset left owed
    runner._setup_owed = True
    assert wait_until(lambda: any(
        "panels online" in l for l in runner.recent(500)[mark:]),
        timeout=20.0)
    session.prepare("c2", {1: array(3), 2: array(4)})
    assert wait_until(lambda: session.phase == READY
                      and session.cue_id == "c2", timeout=10.0)
    session.fire("c2", time.monotonic() + 0.2)
    assert wait_until(lambda: session.phase == FIRED
                      and session.cue_id == "c2", timeout=10.0)
    runner.stop()
    said = runner.recent(500)[mark:]
    first = lambda text: next(i for i, l in enumerate(said) if text in l)
    assert first("owed probe sweep: bus") < first("bus recovered by usb reset")
    assert first("bus recovered by usb reset") < first("panels online")
    assert "→ recovery first" in said[first("owed probe sweep: bus")]
    assert not any("no boards answering" in l for l in said), said
    assert len(bus.resets) == 1
    assert len(shows(bus)) == 2 and session.status()["saved"] == [1, 2]


# ---- review of 9a8c045: M1 (who is asked) and L2 (one lost ACK) ----

class _SilentAt(DegradedMaster):
    """A healthy bus on which some addresses never answer: board 1 absent,
    or the USB board at another DIP address (review_T10/t_absent.py b1).
    `lose` drops that many answers of the next unicast STOPs to board 1 -
    a frame lost on a healthy bus."""

    def __init__(self, silent=(), wait=False, **kwargs):
        super().__init__(**kwargs)
        self.degraded = False
        self.silent = set(silent)
        self.lose = 0
        # `wait`: a silence costs what it costs on the unit - the whole read
        # window, plus the transport's 0.05 s poll overshoot.
        self.wait = wait

    def request(self, frame, retries=3, timeout=None):
        lost = (frame.dest == 1 and frame.cmd == STOP and self.lose > 0)
        if frame.dest in self.silent or lost:
            if lost:
                self.lose -= 1
            self.requested.append(frame)
            self.requested_at.append(time.monotonic())
            if self.wait:
                time.sleep(((0.5 if timeout is None else timeout) + 0.05)
                           * max(1, retries))
            return None
        return super().request(frame, retries=retries, timeout=timeout)


def test_a_garment_without_board_1_is_not_reset_before_its_cues(monkeypatch):
    """t_absent b1: boards 2 and 3 live, address 1 never answers. Asking
    address 1 read the healthy bus as "master silent" and reset its USB
    before every checked cue. The question goes to the lowest live board."""
    bus = _SilentAt(silent={1})
    monkeypatch.setattr("ui.runner.find_port", bus.find_port)
    runner = degraded_runner(bus, boards=[1, 2, 3], precheck=3.6,
                             remote_guard_hold=HOLD)
    session = RemoteSession(runner)
    for cue, colors in (("c1", (2, 3)), ("c2", (4, 5))):
        session.prepare(cue, {2: array(colors[0]), 3: array(colors[1])},
                        refresh_s=0.2)
        assert wait_until(lambda: session.phase == READY
                          and session.cue_id == cue, timeout=15.0)
        session.fire(cue, time.monotonic() + 4.0)
        assert wait_until(lambda: session.phase == FIRED
                          and session.cue_id == cue, timeout=15.0)
    runner.stop()
    said = runner.recent(100)
    assert bus.resets == []
    checks = [l for l in said if "precheck" in l]
    assert len(checks) == 2, said
    assert all("bus ok" in l and "board 2 answers" in l for l in checks)
    assert not any("silent" in l for l in said)


def test_a_listed_board_1_that_is_silent_is_not_no_boards_answering():
    """M1, the sweep's half: only "not one board answered" is the dead
    master that gets a USB reset (_setup_usb_reset())."""
    runner = make_runner(FakeBus(), boards=[1, 2, 3])
    runner.live = [2, 3]
    assert runner._sweep_found_nothing() is False
    assert runner._health_candidates() == [2, 3]
    runner.live = []
    assert runner._sweep_found_nothing() is True
    assert runner._health_candidates() == [1]    # nothing known: address 1


def test_the_health_candidates_are_sticky_then_lowest_then_highest():
    """Gate on f9efd43, MED-2: the board that last answered first, then the
    lowest live board, then the highest (the other harness segment) -
    distinct, and one live board is simply that board."""
    runner = make_runner(FakeBus(), boards=list(range(1, 23)))
    runner.live = list(range(1, 23))
    assert runner._health_candidates() == [1, 22]
    runner._health_sticky = 12
    assert runner._health_candidates() == [12, 1, 22]
    runner._health_sticky = 1
    assert runner._health_candidates() == [1, 22]
    runner.live = [5]                            # 1 is no longer live:
    assert runner._health_candidates() == [5]    # not preferred (LOW-2)
    runner._health_sticky = None
    assert runner._health_candidates() == [5]
    runner._health_sticky = 30                   # not on this list: ignored
    runner.live = []
    assert runner._health_candidates() == [1]


def test_one_lost_ack_behind_a_fast_stop_is_asked_again_not_reset(
        monkeypatch):
    """L2: one missed ACK behind a STOP under 50 ms is a lost frame on a
    healthy bus - asked once more; the USB reset only follows two misses.
    (One live board: the second ask goes to that same board.)"""
    bus = _SilentAt()
    monkeypatch.setattr("ui.runner.find_port", bus.find_port)
    runner = degraded_runner(bus, precheck=20.0, remote_guard_hold=HOLD)
    bus.lose = 1
    runner._precheck(bus, 2, "q08", time.monotonic() + HOLD + 3.5)
    assert bus.resets == [] and runner.precheck["by"] is None
    assert any("precheck q08: board 1 answers → bus ok" in l
               for l in runner.recent(10)), runner.recent(10)
    bus.lose = 2
    runner._precheck(bus, 2, "q09", time.monotonic() + HOLD + 3.5)
    said = [l for l in runner.recent(10) if "precheck q09" in l]
    assert said and "precheck q09: board 1 silent" in said[0], said
    assert "→ usb reset → ok (board 1 answers)" in said[0]
    assert len(bus.resets) == 1


def test_a_degraded_master_with_a_fast_stop_is_reset_before_the_hold(
        monkeypatch):
    """Gate on f9efd43, MED-1 (review_T11/t_degfast.py): a master that
    degrades mid-show but whose precheck STOP reads under 50 ms (every write
    blocks 30 ms). Two full asks left "no time for a usb reset" and the cue
    went into a degraded bus. Now: the second ask is short, and asked only
    if the reset still fits behind it - with the REAL PRECHECK_S and hold
    the reset is in and proven before T-5.0, and the cue is on time."""
    from ui.runner import PRECHECK_S, REMOTE_GUARD_HOLD_S

    bus = degraded(monkeypatch, quick_s=0.03, slow_s=0.03, reset_s=0.3,
                   reenum_s=0.44, open_s=0.3)
    bus.degraded = False
    runner = degraded_runner(bus, boards=[1, 2, 3], precheck=PRECHECK_S,
                             remote_guard_hold=REMOTE_GUARD_HOLD_S)
    session = RemoteSession(runner)
    session.prepare("c1", {1: array(1), 2: array(2), 3: array(3)},
                    refresh_s=0.2)
    assert wait_until(lambda: session.phase == READY, timeout=15.0)
    at = time.monotonic() + PRECHECK_S + 1.0
    bus.degraded = True
    reset_at = []
    real_reset = bus.usb_reset

    def timed_reset(port, timeout=None):
        reset_at.append(time.monotonic() - at)
        return real_reset(port, timeout=timeout)

    runner._usb_reset = timed_reset
    session.fire("c1", at)
    assert wait_until(lambda: session.phase == FIRED, timeout=20.0)
    runner.stop()
    said = [l for l in runner.recent(40) if "precheck c1" in l]
    assert said and "→ usb reset → ok (board" in said[0], said
    assert len(bus.resets) == 1 and bus.degraded is False
    assert reset_at and reset_at[0] <= -7.4 + 0.05, reset_at   # begun by T-7.4
    assert runner.precheck["by"] == "usb_reset"
    late_ms = session.status()["late_ms"]
    assert late_ms is not None and late_ms < 250, late_ms


def test_a_board_that_drops_mid_show_costs_no_reset(monkeypatch):
    """Gate on f9efd43, MED-2 (review_T11/t_drop.py): the lowest live board
    is unplugged mid-show (LOOK28's front-body 485 cable: boards 1-11 dead,
    the master healthy among 12-22). Nothing takes it off `live`, so every
    precheck asked the dead board twice and reset. Now the next candidate
    answers, the bus is ok, and that board is asked first from then on."""
    bus = _SilentAt()
    monkeypatch.setattr("ui.runner.find_port", bus.find_port)
    runner = degraded_runner(bus, boards=[1, 2, 3], precheck=3.6,
                             remote_guard_hold=HOLD)
    session = RemoteSession(runner)
    session.prepare("c1", {1: array(1), 2: array(2), 3: array(3)},
                    refresh_s=0.2)
    assert wait_until(lambda: session.phase == READY, timeout=15.0)
    bus.silent = {1}                             # unplugged, mid-show
    session.fire("c1", time.monotonic() + 4.0)
    assert wait_until(lambda: session.phase == FIRED, timeout=15.0)
    mark = len(bus.requested)
    session.arm("c2", 19)
    session.fire("c2", time.monotonic() + 4.0)
    assert wait_until(lambda: session.phase == FIRED
                      and session.cue_id == "c2", timeout=15.0)
    runner.stop()
    said = [l for l in runner.recent(60) if "precheck" in l]
    assert bus.resets == []
    assert "precheck c1: board 1 silent, board 3 answers → bus ok" in said[0]
    assert "precheck c2: bus ok" in said[1] and "board 3 answers" in said[1]
    # The sticky board is asked first: board 1 is not asked again.
    asked = [f.dest for f in bus.requested[mark:] if f.cmd == STOP]
    assert asked and asked[0] == 3 and 1 not in asked, asked
    # ...and the idle question goes the same way.
    assert runner._health_candidates()[0] == 3


# ---- final gate on 05cc86a: the limits (review_T12/t_limits.py) ----

class _AtTheLimits(DegradedMaster):
    """t_limits' degraded master, with the transport's own costs: the
    precheck's broadcast STOP reads 45 ms (under STALL_LOG_MS), every unicast
    write blocks 61 ms, and every silence overshoots its read window by
    0.05 s (transport.Bus.recv() polls with a 0.05 s timeout)."""

    def __init__(self, **kwargs):
        super().__init__(reset_s=0.0, reenum_s=0.05, open_s=0.0, **kwargs)
        self.degraded = True

    def send(self, frame):
        if self.degraded:
            time.sleep(0.045)
        FakeBus.send(self, frame)

    def request(self, frame, retries=3, timeout=None):
        if not self.degraded:
            return FakeBus.request(self, frame, retries=retries,
                                   timeout=timeout)
        time.sleep(0.061)
        self.requested.append(frame)
        self.requested_at.append(time.monotonic())
        time.sleep(((0.5 if timeout is None else timeout) + 0.05)
                   * max(1, retries))
        return None


def _precheck_at(monkeypatch, bus, lead, cue):
    from ui.runner import REMOTE_GUARD_HOLD_S

    monkeypatch.setattr("ui.runner.find_port", bus.find_port)
    runner = degraded_runner(bus, boards=[1, 2, 3], precheck=lead,
                             remote_guard_hold=REMOTE_GUARD_HOLD_S)
    runner.live = [1, 2, 3]
    runner._precheck(bus, 3, cue, time.monotonic() + lead)
    return runner, [l for l in runner.recent(20) if f"precheck {cue}" in l]


@pytest.mark.parametrize("lead", [8.355, 8.36, 8.365, 8.37])
def test_a_second_ask_that_just_fits_never_costs_the_reset(monkeypatch, lead):
    """LOW-1: HEALTH_SECOND_ASK_COST_S (0.3) had no room for the read
    overshoot, so at leads around 8.36 s the second ask just fit, overshot,
    and then "no time for a usb reset" - the degraded master went into the
    cue. At 0.35 the second ask is only taken with room for its overshoot
    too, and either way the reset follows."""
    bus = _AtTheLimits()
    runner, said = _precheck_at(monkeypatch, bus, lead, "q03")
    assert said and "→ usb reset → ok" in said[0], said
    assert "no time for a usb reset" not in said[0]
    assert len(bus.resets) == 1 and runner.precheck["by"] == "usb_reset"


def test_the_second_ask_costs_no_more_than_it_is_allowed(monkeypatch):
    """What HEALTH_SECOND_ASK_COST_S promises, measured on the same bus: a
    61 ms write, the 0.2 s window and its 0.05 s overshoot."""
    from ui.runner import (HEALTH_SECOND_ASK_COST_S, HEALTH_SECOND_ASK_S,
                           READ_OVERSHOOT_S, STALL_LOG_MS)

    assert HEALTH_SECOND_ASK_COST_S == pytest.approx(
        2 * STALL_LOG_MS / 1000 + HEALTH_SECOND_ASK_S + READ_OVERSHOOT_S)
    assert HEALTH_SECOND_ASK_COST_S == pytest.approx(0.35)
    bus = _AtTheLimits()
    monkeypatch.setattr("ui.runner.find_port", bus.find_port)
    runner = degraded_runner(bus)
    runner._health_trail = []
    began = time.monotonic()
    assert runner._ask_board(bus, 3, 2, HEALTH_SECOND_ASK_S) is False
    assert time.monotonic() - began <= HEALTH_SECOND_ASK_COST_S


def test_the_cure_check_asks_a_board_that_was_not_silent(monkeypatch):
    """LOW-2: a late precheck (no room for a second ask) on a healthy bus
    whose lowest board has dropped: the one miss gets the reset, and the
    check after it used to ask that same dead board - "no answer". It asks
    the first candidate not already silent in this check."""
    bus = _SilentAt(silent={1}, wait=True)
    runner, said = _precheck_at(monkeypatch, bus, 8.2, "q04")
    assert said, runner.recent(20)
    assert "board 1 silent, no time to ask another" in said[0], said
    assert "→ usb reset → ok (board 3 answers)" in said[0], said
    assert runner.precheck["by"] == "usb_reset"


def test_a_precheck_begun_too_late_to_ask_does_not_say_bus_ok():
    """LOW-4: the STOP alone is not "bus ok" - it reads fast right behind a
    write on a degraded master."""
    bus = RecoveringBus("padding", deaf_boards=False)
    bus.stalled = False
    runner = recovery_runner(bus, precheck=20.0, remote_guard_hold=HOLD)
    runner._precheck(bus, 2, "q03", time.monotonic() + HOLD + 0.45)
    said = [l for l in runner.recent(10) if "precheck q03" in l]
    assert said == [said[0]] and "bus ok" not in said[0], said
    assert said[0].endswith("precheck q03: stop 0 ms, no time to ask a "
                            "board"), said
    assert not any(f.cmd == STOP and f.dest != 0xFF for f in bus.requested)

