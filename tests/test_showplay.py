"""ui/showplay.py: the unit runs its show file from a T0, alone.

Real time, compressed: a "refresh" of 0.3 s, cues under a second apart,
the runner on the FakeBus. What is checked is what goes out on the bus
and when - the saves before, the single show at T0 + sent.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ui.remote import RemoteError
from ui.showplay import ENDED, HOLDING, LOADED, RUNNING, STOPPED, ShowPlayer
from tests.test_ui_remote import SAVE, SHOW, make_session, wait_until

REFRESH = 0.3


def array(color: int) -> str:
    return bytes([0xFE] + [color] * 60 + [0xFF, 0xFF, 0xFE]).hex()


NOTHING = bytes([0xFE] + [0xFF] * 62 + [0xFE]).hex()


def make_show(sents=(-REFRESH, 0.8, 1.7), duration=2.2):
    """Preset all 1s; then all 2s; then a partial cue touching board 1 only."""
    colors = [1, 2, 3]
    cues = []
    for n, sent in enumerate(sents):
        partial = n == 2
        cues.append({
            "id": f"q{n:02d}", "at": max(0.0, sent + REFRESH), "sent": sent,
            "label": f"Look22 P{n + 1:02d}",
            "boards": {"1": array(colors[n]),
                       "2": NOTHING if partial else array(colors[n])},
            "state": {"1": array(colors[n]),
                      "2": array(2) if partial else array(colors[n])}})
    return {"id": "abc1234567", "name": "test", "unit": "radxa-03",
            "dev_type": 3, "refresh_s": REFRESH, "duration": duration,
            "boards": [1, 2], "cues": cues}


@pytest.fixture
def rig(tmp_path):
    session, runner, bus = make_session()
    player = ShowPlayer(session, store=tmp_path, save_s=0.01, margin_s=0.15,
                        grace_s=0.3, tick_s=0.02, setup_s=0.5,
                        setup_board_s=0.0)
    yield player, session, runner, bus, tmp_path
    player.close()
    runner.stop()


def events(bus):
    """The bus as a story: ("save", board, first colour) / ("show",)."""
    story = []
    for frame in bus.log:
        if frame.cmd == SAVE:
            story.append(("save", frame.dest, frame.data[3]))
        elif frame.cmd == SHOW:
            story.append(("show",))
    return story


@pytest.fixture(autouse=True)
def ordered_bus(monkeypatch):
    """FakeBus keeps sends and requests apart; here their order matters."""
    from tests import test_ui_runner

    original_init = test_ui_runner.FakeBus.__init__
    original_send = test_ui_runner.FakeBus.send
    original_request = test_ui_runner.FakeBus.request

    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.log, self.times = [], []

    def send(self, frame):
        self.log.append(frame)
        self.times.append(time.monotonic())
        return original_send(self, frame)

    def request(self, frame, retries=3):
        self.log.append(frame)
        self.times.append(time.monotonic())
        return original_request(self, frame, retries)

    monkeypatch.setattr(test_ui_runner.FakeBus, "__init__", init)
    monkeypatch.setattr(test_ui_runner.FakeBus, "send", send)
    monkeypatch.setattr(test_ui_runner.FakeBus, "request", request)


def show_times(bus):
    return [t for f, t in zip(bus.log, bus.times) if f.cmd == SHOW]


# ---- a plain run ----

def test_preset_then_every_cue_on_its_instant(rig):
    player, session, runner, bus, _ = rig
    player.load(make_show())
    assert player.state == LOADED
    player.preset()
    assert wait_until(lambda: player.applied == "q00")
    assert events(bus) == [("save", 1, 1), ("save", 2, 1), ("show",)]

    t0 = time.monotonic() + 0.5
    player.run(t0, "abc1234567")
    assert wait_until(lambda: player.state == ENDED, timeout=6)
    story = events(bus)
    # The preset is not repainted at START; the partial cue still writes
    # board 2 - with "refresh nothing" - because the show is a broadcast.
    assert story == [("save", 1, 1), ("save", 2, 1), ("show",),
                     ("save", 1, 2), ("save", 2, 2), ("show",),
                     ("save", 1, 3), ("save", 2, 0xFF), ("show",)]
    fired = show_times(bus)
    assert 0 <= fired[1] - (t0 + 0.8) < 0.05
    assert 0 <= fired[2] - (t0 + 1.7) < 0.05
    assert player.status()["applied"] == "q02"


def test_start_without_a_preset_puts_it_up_first(rig):
    player, session, runner, bus, _ = rig
    player.load(make_show())
    player.run(time.monotonic() + 0.6)      # room for the preset's refresh
    assert wait_until(lambda: player.applied == "q00")
    assert events(bus)[:3] == [("save", 1, 1), ("save", 2, 1), ("show",)]
    assert wait_until(lambda: player.state == ENDED, timeout=6)
    assert len(show_times(bus)) == 3


# ---- the operator moves T0 ----

def test_hold_disarms_and_resume_fires_at_the_moved_time(rig):
    player, session, runner, bus, _ = rig
    player.load(make_show())
    player.preset()
    assert wait_until(lambda: player.applied == "q00")
    t0 = time.monotonic() + 0.2
    player.run(t0)
    assert wait_until(lambda: session.phase == "armed")     # q01 is loaded
    player.hold()
    assert player.state == HOLDING and session.phase == "ready"
    time.sleep(1.0)                                         # past t0 + 0.8
    assert len(show_times(bus)) == 1                        # nothing fired
    t0_moved = t0 + 1.2                                     # held for 1.2 s
    player.run(t0_moved)
    assert wait_until(lambda: player.applied == "q01", timeout=4)
    assert 0 <= show_times(bus)[1] - (t0_moved + 0.8) < 0.05
    # Not written twice: the arrays were already on the boards.
    assert events(bus).count(("save", 1, 2)) == 1


def test_next_is_t0_moved_earlier(rig):
    player, session, runner, bus, _ = rig
    player.load(make_show(sents=(-REFRESH, 5.0, 9.0), duration=10))
    player.preset()
    assert wait_until(lambda: player.applied == "q00")
    t0 = time.monotonic() + 0.1
    player.run(t0)
    time.sleep(0.3)
    lead = 0.4
    t0_next = time.monotonic() + lead - 5.0                 # q01 due in 0.4 s
    player.run(t0_next)
    assert wait_until(lambda: player.applied == "q01", timeout=3)
    assert 0 <= show_times(bus)[1] - (t0_next + 5.0) < 0.05


def test_joining_mid_show_sends_the_whole_picture(rig):
    player, session, runner, bus, _ = rig
    # Cue q02 (partial) is already past: what must show is 3s on board 1
    # over 2s on board 2 - the state, not the change.
    player.load(make_show(sents=(-REFRESH, 0.5, 1.0), duration=30))
    player.run(time.monotonic() - 3.0)
    assert wait_until(lambda: player.applied == "q02")
    assert events(bus) == [("save", 1, 3), ("save", 2, 2), ("show",)]


def test_a_late_joiner_with_no_room_waits_and_then_sends_the_state(rig):
    player, session, runner, bus, _ = rig
    show = make_show(sents=(-REFRESH, 0.5, 1.2), duration=30)
    player.load(show)
    # Now = 0.6: q01 is past and unshown, q02 is 0.6 s away - no room for
    # a catch-up refresh (0.3) plus two loads, so q02 goes out whole.
    t0 = time.monotonic() - 0.6
    player.run(t0)
    assert wait_until(lambda: player.applied == "q02", timeout=4)
    assert events(bus) == [("save", 1, 3), ("save", 2, 2), ("show",)]
    assert 0 <= show_times(bus)[0] - (t0 + 1.2) < 0.05


def test_stop_ends_the_run_and_keeps_the_garment(rig):
    player, session, runner, bus, _ = rig
    player.load(make_show())
    player.run(time.monotonic() + 0.6)
    assert wait_until(lambda: player.applied == "q00")
    player.stop()
    assert player.state == STOPPED
    time.sleep(1.2)
    assert len(show_times(bus)) == 1
    assert player.status()["t0"] is None


# ---- a restart in the middle of the show ----

def test_a_restarted_unit_rejoins_from_disk(rig):
    player, session, runner, bus, store = rig
    player.load(make_show(sents=(-REFRESH, 0.4, 5.0), duration=30))
    t0 = time.monotonic() - 1.0
    player.run(t0)
    assert wait_until(lambda: player.applied == "q01")
    player.close()                                          # "power cut"

    session2, runner2, bus2 = make_session()
    reborn = ShowPlayer(session2, store=store, save_s=0.01, margin_s=0.15,
                        grace_s=0.3, tick_s=0.02, setup_s=0.5,
                        setup_board_s=0.0)
    try:
        reborn.restore()
        assert reborn.state == RUNNING and not reborn.synced
        assert abs(reborn.t0 - t0) < 0.05                   # via the wall clock
        assert reborn.status()["note"] == "restored after restart"
        # It does not know what the boards show, so the whole picture goes
        # out - after the grace period that lets the PC correct T0 first.
        assert wait_until(lambda: reborn.applied == "q01")
        assert events(bus2) == [("save", 1, 2), ("save", 2, 2), ("show",)]
        assert wait_until(lambda: reborn.applied == "q02", timeout=8)
        assert 0 <= show_times(bus2)[1] - (t0 + 5.0) < 0.05
    finally:
        reborn.close()
        runner2.stop()


def test_a_show_long_over_is_not_resumed(rig):
    player, session, runner, bus, store = rig
    player.load(make_show(duration=2))
    player.run(time.monotonic() - 500)
    player.close()
    session2, runner2, _ = make_session()
    reborn = ShowPlayer(session2, store=store, tick_s=0.02)
    try:
        reborn.restore()
        assert reborn.state == LOADED and reborn.t0 is None
    finally:
        reborn.close()
        runner2.stop()


def test_a_stopped_show_stays_stopped_after_a_restart(rig):
    player, session, runner, bus, store = rig
    player.load(make_show(duration=60))
    player.run(time.monotonic() + 5)
    player.stop()
    run = json.loads((store / "show-run.json").read_text(encoding="utf-8"))
    assert run["state"] == STOPPED and run["t0_wall"] is None
    session2, runner2, _ = make_session()
    reborn = ShowPlayer(session2, store=store, tick_s=0.02)
    try:
        reborn.restore()
        assert reborn.state == LOADED
    finally:
        reborn.close()
        runner2.stop()


# ---- refusals ----

def test_refusals(rig):
    player, session, runner, bus, _ = rig
    with pytest.raises(RemoteError):
        player.run(time.monotonic())                        # nothing loaded
    with pytest.raises(RemoteError):
        player.preset()
    with pytest.raises(RemoteError):
        player.load({"id": "x", "cues": []})
    with pytest.raises(RemoteError):
        player.load({"id": "x", "cues": [{"id": "q00"}]})
    player.load(make_show())
    with pytest.raises(RemoteError):
        player.run(time.monotonic(), "another-show")
    session.busy = lambda: True
    with pytest.raises(RemoteError):
        player.run(time.monotonic())
    assert bus.log == []
