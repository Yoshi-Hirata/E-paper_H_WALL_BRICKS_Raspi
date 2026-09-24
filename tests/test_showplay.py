"""ui/showplay.py: the unit runs its show file from a T0, alone.

Pre-burn (docs/MERIS_REPLY_3SLOT.pdf, 2026-09-24): load() burns every cue
into its own slot up front (RemoteSession.burn()); RUNNING is triggers
only - a broadcast "show slot N", never another colour write. Real time,
compressed: a "refresh" of 0.3 s, cues under a second apart, the runner
on the FakeBus.
"""

from __future__ import annotations

import json
import struct
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ui.remote import ARMED, READY, RemoteError
from ui.showplay import ENDED, HOLDING, LOADED, RUNNING, STOPPED, ShowPlayer
from tests.test_ui_remote import SAVE, SHOW, make_session, wait_until

REFRESH = 0.3


def array(color: int) -> str:
    return bytes([0xFE] + [color] * 60 + [0xFF, 0xFF, 0xFE]).hex()


NOTHING = bytes([0xFE] + [0xFF] * 62 + [0xFE]).hex()


def make_show(sents=(-REFRESH, 0.8, 1.7), duration=2.2):
    """Preset all 1s (slot 1); then all 2s (slot 2); then a partial cue
    touching board 1 only (slot 3) - "partial" only changes what the
    `boards` diff carries; `state` (what gets burned) is always whole.
    Cues use slots 1..18 (19 is the manual/demo one-shot slot, 0 is the
    standby white - the contract as of 2026-09-24)."""
    colors = [1, 2, 3]
    cues = []
    for n, sent in enumerate(sents):
        partial = n == 2
        color = colors[n % 3]
        cues.append({
            "id": f"q{n:02d}", "at": max(0.0, sent + REFRESH), "sent": sent,
            "label": f"Look22 P{n + 1:02d}", "slot": (n % 18) + 1,
            "boards": {"1": array(color),
                       "2": NOTHING if partial else array(color)},
            "state": {"1": array(color),
                      "2": array(2) if partial else array(color)}})
    return {"id": "abc1234567", "name": "test", "unit": "radxa-03",
            "dev_type": 3, "refresh_s": REFRESH, "duration": duration,
            "boards": [1, 2], "slot_capacity": 20, "cues": cues}


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
    """The bus as a story: ("save", board, first colour) / ("show", slot)."""
    story = []
    for frame in bus.log:
        if frame.cmd == SAVE:
            story.append(("save", frame.dest, frame.data[3]))
        elif frame.cmd == SHOW:
            story.append(("show", frame.data[0]))
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


def wait_burned(player, timeout=5.0):
    return wait_until(lambda: (player.status() or {}).get("burn", {})
                      .get("state") == "burned", timeout)


def wait_burn_settled(player, timeout=5.0):
    return wait_until(lambda: (player.status() or {}).get("burn", {})
                      .get("state") in ("burned", "failed"), timeout)


# ---- pre-burn: load() writes every picture up front ----

def test_load_burns_every_cue_before_anything_runs(rig):
    player, session, runner, bus, _ = rig
    player.load(make_show())
    assert wait_burned(player)
    saves = [e for e in events(bus) if e[0] == "save"]
    assert len(saves) == 6                       # 3 cues x 2 boards
    assert [e[2] for e in saves] == [1, 1, 2, 2, 3, 2]
    status = player.status()
    assert status["burn"] == {"done": 6, "total": 6, "failed": [],
                              "state": "burned"}


def test_a_second_load_of_the_same_show_burns_nothing_new(rig):
    player, session, runner, bus, _ = rig
    show = make_show()
    player.load(show)
    assert wait_burned(player)
    n = len([e for e in events(bus) if e[0] == "save"])
    player.load(show)                            # identical content
    assert wait_burned(player)
    assert player.status()["burn"]["done"] == 6
    assert len([e for e in events(bus) if e[0] == "save"]) == n


# ---- a plain run: triggers only ----

def test_preset_then_every_cue_fires_its_own_slot_on_time(rig):
    player, session, runner, bus, _ = rig
    player.load(make_show())
    assert wait_burned(player)
    burned = [e for e in events(bus) if e[0] == "save"]

    player.preset()
    assert wait_until(lambda: player.applied == "q00")
    assert [e for e in events(bus) if e[0] == "show"] == [("show", 1)]
    assert [e for e in events(bus) if e[0] == "save"] == burned   # nothing new

    t0 = time.monotonic() + 0.5
    player.run(t0, "abc1234567")
    assert wait_until(lambda: player.state == ENDED, timeout=6)
    shown = [e for e in events(bus) if e[0] == "show"]
    assert shown == [("show", 1), ("show", 2), ("show", 3)]
    assert [e for e in events(bus) if e[0] == "save"] == burned   # never rewritten
    fired = show_times(bus)
    assert 0 <= fired[1] - (t0 + 0.8) < 0.05
    assert 0 <= fired[2] - (t0 + 1.7) < 0.05
    assert player.status()["applied"] == "q02"


def test_start_without_a_preset_puts_it_up_first(rig):
    player, session, runner, bus, _ = rig
    player.load(make_show())
    assert wait_burned(player)
    player.run(time.monotonic() + 0.2)
    assert wait_until(lambda: player.applied == "q00")
    assert wait_until(lambda: player.state == ENDED, timeout=6)
    assert len(show_times(bus)) == 3


def test_no_colour_writes_happen_while_running(rig):
    """Every 0x13 in the whole story comes from the burn, before the
    first trigger - RUNNING sends only 0x1D."""
    player, session, runner, bus, _ = rig
    player.load(make_show())
    assert wait_burned(player)
    player.run(time.monotonic() + 0.2)
    assert wait_until(lambda: player.state == ENDED, timeout=6)
    story = events(bus)
    first_show = next(i for i, e in enumerate(story) if e[0] == "show")
    assert all(e[0] == "save" for e in story[:first_show])
    assert all(e[0] == "show" for e in story[first_show:])


# ---- run() is gated on the burn ----

def test_run_is_refused_while_still_burning(rig):
    player, session, runner, bus, _ = rig
    player.load(make_show())
    with pytest.raises(RemoteError, match="still writing the pictures"):
        player.run(time.monotonic() + 0.2)
    assert wait_burned(player)
    player.run(time.monotonic() + 0.2)           # fine once burned
    assert wait_until(lambda: player.state == ENDED, timeout=6)


def test_preset_is_also_refused_while_still_burning(rig):
    player, session, runner, bus, _ = rig
    player.load(make_show())
    with pytest.raises(RemoteError, match="still writing the pictures"):
        player.preset()
    assert wait_burned(player)
    player.preset()
    assert wait_until(lambda: player.applied == "q00")


class RefusesSaveBus:
    """Answers everything except 0x13 (colour save) for one board, which
    it always NAKs - a board that is live (answers stop/cfg) but for
    some reason keeps refusing the write, unlike PickyBus's silence
    (indistinguishable from "simply not there")."""

    def __init__(self, board: int):
        from tests.test_ui_runner import FakeBus

        self._bus = FakeBus()
        self.board = board
        self.requested = self._bus.requested
        self.sent = self._bus.sent

    def request(self, frame, retries=3):
        if frame.cmd == SAVE and frame.dest == self.board:
            self.requested.append(frame)
            return None
        return self._bus.request(frame, retries)

    def send(self, frame):
        return self._bus.send(frame)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._bus.closed = True


def test_run_is_refused_while_a_live_board_never_took_the_burn(rig):
    session, runner, bus = make_session(RefusesSaveBus(2))
    player = ShowPlayer(session, store=None, save_s=0.01, margin_s=0.1,
                        grace_s=0.1, tick_s=0.02)
    try:
        player.load(make_show())
        assert wait_until(lambda: (player.status() or {}).get("burn", {})
                          .get("state") == "failed")
        assert 2 not in runner.absent               # live, just refusing
        with pytest.raises(RemoteError, match="did not take the burn"):
            player.run(time.monotonic() + 0.2)
    finally:
        player.close()
        runner.stop()


def test_run_proceeds_when_the_only_burn_failures_are_absent_boards(rig):
    from tests.test_ui_remote import PickyBus

    session, runner, bus = make_session(PickyBus({2}))
    player = ShowPlayer(session, store=None, save_s=0.01, margin_s=0.1,
                        grace_s=0.1, tick_s=0.02)
    try:
        show = make_show()
        player.load(show)
        assert wait_until(lambda: (player.status() or {}).get("burn", {})
                          .get("state") == "failed")
        assert 2 in runner.absent                # dropped, not merely failed
        player.run(time.monotonic() + 0.2)        # not refused
        assert wait_until(lambda: player.state == ENDED, timeout=6)
    finally:
        player.close()
        runner.stop()


# ---- the operator moves T0 ----

def test_hold_disarms_and_resume_fires_at_the_moved_time(rig):
    player, session, runner, bus, _ = rig
    player.load(make_show())
    assert wait_burned(player)
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


def test_next_is_t0_moved_earlier(rig):
    player, session, runner, bus, _ = rig
    player.load(make_show(sents=(-REFRESH, 5.0, 9.0), duration=10))
    assert wait_burned(player)
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


def test_a_forward_jump_over_an_armed_cue_disarms_it_instead_of_firing_it(rig):
    # SEEK/NEXT can jump clean past a cue that was already armed for the
    # old T0. Left alone it would fire as scheduled - the wrong picture,
    # since the new T0 says a later cue (q02) is the one due now.
    player, session, runner, bus, _ = rig
    player.load(make_show(sents=(-REFRESH, 0.8, 1.7), duration=30))
    assert wait_burned(player)
    player.preset()
    assert wait_until(lambda: player.applied == "q00")
    t0 = time.monotonic() + 0.2
    player.run(t0)
    assert wait_until(lambda: session.phase == "armed")     # q01 loaded, not yet due
    assert session.fire_at is not None
    # Forward past BOTH q01 and q02: current is q02, and without the fix
    # the stale, still-armed q01 would fire first regardless.
    player.run(time.monotonic() - 2.0)
    assert session.fire_at is None                          # disarmed at once
    assert wait_until(lambda: player.applied == "q02", timeout=4)
    # q01 was never actually shown: just the preset, then q02 directly.
    assert len(show_times(bus)) == 2


def test_a_backward_seek_re_times_an_armed_cue_instead_of_disarming_it(rig):
    # The mirror case: a backward seek lands units inside the show, never
    # skipping a cue - the armed cue keeps its identity, only its fire
    # time moves (_send()'s own re-timing, not the new disarm above).
    player, session, runner, bus, _ = rig
    player.load(make_show(sents=(-REFRESH, 5.0, 9.0), duration=30))
    assert wait_burned(player)
    player.preset()
    assert wait_until(lambda: player.applied == "q00")
    player.run(time.monotonic() + 0.1)
    t0_close = time.monotonic() + 0.4 - 5.0                 # q01 due in 0.4 s: armed
    player.run(t0_close)
    assert wait_until(lambda: session.phase == "armed" and session.cue_id.endswith("q01"))
    # Backward (T0 LATER): q01 is 2.0 s away again, not skipped - just re-timed.
    t0_back = time.monotonic() - 3.0
    player.run(t0_back)
    assert wait_until(lambda: session.phase == "armed"
                      and abs((session.fire_at or 0) - (t0_back + 5.0)) < 0.1)


def test_joining_mid_show_arms_the_current_cues_own_slot(rig):
    player, session, runner, bus, _ = rig
    player.load(make_show(sents=(-REFRESH, 0.5, 1.0), duration=30))
    assert wait_burned(player)
    player.run(time.monotonic() - 3.0)                       # past every cue
    assert wait_until(lambda: player.applied == "q02")
    assert [e for e in events(bus) if e[0] == "show"] == [("show", 3)]


def test_stop_ends_the_run_and_keeps_the_garment(rig):
    player, session, runner, bus, _ = rig
    player.load(make_show())
    assert wait_burned(player)
    player.run(time.monotonic() + 0.6)
    assert wait_until(lambda: player.applied == "q00")
    player.stop()
    assert player.state == STOPPED
    time.sleep(1.2)
    assert len(show_times(bus)) == 1
    assert player.status()["t0"] is None


def test_stop_cancels_a_burn_still_in_progress(rig):
    player, session, runner, bus, _ = rig
    show = make_show(sents=tuple(n * 0.01 for n in range(19)))
    player.load(show)
    player.stop()
    time.sleep(0.2)
    status = session.burn_status()
    assert status is None or status["state"] != "burning"


# ---- a restart in the middle of the show ----

def test_a_restarted_unit_rejoins_from_disk(rig):
    player, session, runner, bus, store = rig
    player.load(make_show(sents=(-REFRESH, 0.4, 5.0), duration=30))
    assert wait_burned(player)
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
        # restore() never re-burns - the boards' flash already holds every
        # picture from before the restart; only the trigger is re-armed.
        assert wait_until(lambda: reborn.applied == "q01")
        assert [e for e in events(bus2) if e[0] == "show"] == [("show", 2)]
        assert wait_until(lambda: reborn.applied == "q02", timeout=8)
        assert 0 <= show_times(bus2)[1] - (t0 + 5.0) < 0.05
    finally:
        reborn.close()
        runner2.stop()


def test_a_show_long_over_is_not_resumed(rig):
    player, session, runner, bus, store = rig
    player.load(make_show(duration=2))
    assert wait_burned(player)
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
    assert wait_burned(player)
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


# ---- healing: a board that missed a trigger outright ----

def test_a_board_that_joins_late_gets_the_current_cues_slot_re_armed(rig):
    # Healing only happens while RUNNING (_plan() is a no-op otherwise),
    # and only once there is nothing else to keep the session's one
    # fire-time slot busy - a single-cue show (nxt is always None) is
    # the clean case: nothing else ever competes for it.
    from tests.test_ui_remote import PickyBus

    bus = PickyBus({2})                             # board 2 is off from the start
    session, runner, bus = make_session(bus)
    player = ShowPlayer(session, store=None, save_s=0.01, margin_s=0.1,
                        grace_s=0.1, tick_s=0.02, retry_s=0.2)
    try:
        player.load(make_show(sents=(-REFRESH,), duration=60))
        assert wait_burn_settled(player)             # "failed" (board 2 absent)
        player.run(time.monotonic() + 0.2)
        assert wait_until(lambda: player.applied == "q00")
        n0 = len([e for e in events(bus) if e[0] == "show"])
        bus.silent = set()                          # ...and comes back
        runner._next_reprobe = 0.0
        assert wait_until(lambda: len([e for e in events(bus) if e[0] == "show"])
                          > n0, timeout=4)
        # The re-arm is the same slot as `applied` (q00's, slot 1) - no
        # rewrite, no new cue.
        assert player.applied == "q00"
        shown = [e for e in events(bus) if e[0] == "show"]
        assert shown[-1] == ("show", 1)
    finally:
        player.close()
        runner.stop()


def test_a_board_that_stays_dead_does_not_re_arm_for_ever(rig):
    from tests.test_ui_remote import PickyBus

    session, runner, bus = make_session(PickyBus({2}))
    player = ShowPlayer(session, store=None, save_s=0.01, margin_s=0.1,
                        grace_s=0.1, tick_s=0.02, retry_s=0.2)
    try:
        player.load(make_show(sents=(-REFRESH, 30.0), duration=60))
        assert wait_until(lambda: (player.status() or {}).get("burn", {})
                          .get("state") == "failed")
        assert 2 in runner.absent
        player.run(time.monotonic() + 0.2)
        assert wait_until(lambda: player.applied == "q00")
        time.sleep(1.5)
        # Board 2 was never live, so _check_dirty() never sees it "join
        # late" (it is simply absent) - nothing extra is armed for it.
        shows_ = [e for e in events(bus) if e[0] == "show"]
        assert len(shows_) == 1
    finally:
        player.close()
        runner.stop()


# ---- refusals ----

def test_show_files_missing_what_the_player_needs_are_refused(rig):
    player, *_ = rig
    good = make_show()
    for broken in ({k: v for k, v in good.items() if k != "refresh_s"},
                   {k: v for k, v in good.items() if k != "duration"},
                   dict(good, id=""), dict(good, cues=["q00"]), []):
        with pytest.raises(RemoteError):
            player.load(broken)
    assert player.show is None


def test_a_show_file_without_slots_is_refused(rig):
    player, *_ = rig
    good = make_show()
    no_slots = dict(good, cues=[{k: v for k, v in cue.items() if k != "slot"}
                                for cue in good["cues"]])
    with pytest.raises(RemoteError, match="no slots"):
        player.load(no_slots)
    assert player.show is None
    player.load(good)                     # a show WITH slots is fine
    assert player.show is not None


def test_a_show_with_the_wrong_delay_unit_ms_is_refused(rig):
    player, *_ = rig
    good = make_show()
    with pytest.raises(RemoteError):
        player.load(dict(good, delay_unit_ms=20))       # this unit's frame is 10 ms
    player.load(dict(good, delay_unit_ms=10))            # matches: fine


def test_a_restored_t0_in_the_future_is_not_trusted(rig):
    player, session, runner, bus, store = rig
    player.load(make_show(duration=60))
    assert wait_burned(player)
    player.run(time.monotonic() + 5)
    player.close()
    session2, runner2, _ = make_session()
    # The wall clock came up an hour behind (no RTC): T0 looks an hour away.
    reborn = ShowPlayer(session2, store=store, tick_s=0.02,
                        wall=lambda: time.time() - 3600)
    try:
        reborn.restore()
        assert reborn.state == LOADED and reborn.t0 is None
        assert "future" in reborn.status()["note"]
    finally:
        reborn.close()
        runner2.stop()


def test_the_show_file_is_written_once_and_never_half(rig):
    player, session, runner, bus, store = rig
    player.load(make_show(duration=60))
    assert wait_burned(player)
    stamp = (store / "show.json").stat().st_mtime_ns
    player.run(time.monotonic() + 5)
    player.hold()
    player.stop()
    assert (store / "show.json").stat().st_mtime_ns == stamp
    assert not list(store.glob("*.tmp"))


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
    assert wait_burned(player)
    marker = len(bus.log)
    with pytest.raises(RemoteError):
        player.run(time.monotonic(), "another-show")
    session.busy = lambda: True
    with pytest.raises(RemoteError):
        player.run(time.monotonic())
    assert len(bus.log) == marker          # neither refusal touched the bus


def test_status_answers_while_the_first_cue_takes_the_port(tmp_path):
    # arm() (via preset()) can wait seconds for the previous worker; the
    # PC's poll and the LCD read status() meanwhile.
    from tests.test_ui_remote import make_session as _make_session

    session, runner, bus = _make_session()
    player = ShowPlayer(session, store=tmp_path, save_s=0.01, margin_s=0.15,
                        grace_s=0.3, tick_s=0.02, setup_s=0.5,
                        setup_board_s=0.0)
    started = runner.start_remote

    def slow_start(sess):
        time.sleep(1.0)
        return started(sess)
    runner.start_remote = slow_start
    try:
        player.load(make_show(duration=30))
        assert wait_burned(player)
        import threading
        worker = threading.Thread(target=player.preset)
        worker.start()
        time.sleep(0.2)
        began = time.monotonic()
        assert player.status()["state"] == LOADED
        assert time.monotonic() - began < 0.3
        worker.join()
    finally:
        player.close()
        runner.stop()


# ---- found in the second review pass ----

def test_a_send_decided_before_hold_does_not_arm_the_cue_after_it(rig):
    # _send() runs without the lock; a HOLD can land between the decision
    # and the arming. arm() may already have set a cue - the time must
    # not be set.
    player, session, runner, bus, _ = rig
    player.load(make_show(sents=(-REFRESH, 5.0, 9.0), duration=30))
    assert wait_burned(player)
    player.run(time.monotonic() - 1.0)
    assert wait_until(lambda: player.applied == "q00")
    show, cue = player.show, player.show["cues"][1]
    stale = player._epoch
    player.hold()                                   # ...the operator was faster
    player._send(show, cue, time.monotonic() + 0.3, stale)
    assert wait_until(lambda: session.phase == "ready")
    assert session.fire_at is None
    time.sleep(0.6)
    assert len(show_times(bus)) == 1                # only the preset ever fired


def test_the_same_goes_for_stop_and_for_a_new_show(rig):
    player, session, runner, bus, _ = rig
    player.load(make_show(duration=30))
    assert wait_burned(player)
    show, cue = player.show, player.show["cues"][1]
    for command in (player.stop, lambda: player.load(make_show(duration=31))):
        stale = player._epoch
        command()
        player._send(show, cue, time.monotonic() + 0.2, stale)
        time.sleep(0.5)
        assert session.fire_at is None
    assert show_times(bus) == []


def test_the_show_ends_on_the_clock_even_if_the_last_cue_never_lands(tmp_path):
    from tests.test_ui_remote import PickyBus

    session, runner, bus = make_session(PickyBus({1, 2}))
    player = ShowPlayer(session, store=tmp_path, save_s=0.01, margin_s=0.15,
                        grace_s=0.3, tick_s=0.02, retry_s=0.2)
    try:
        import ui.showplay as showplay

        player.load(make_show(sents=(-REFRESH, 0.3, 0.6), duration=1))
        assert wait_burn_settled(player)
        old, showplay.END_SLACK_S = showplay.END_SLACK_S, 1.0
        try:
            player.run(time.monotonic() - 0.2)
            assert wait_until(lambda: player.state == ENDED, timeout=8)
        finally:
            showplay.END_SLACK_S = old
        assert player.applied is None and show_times(bus) == []
        assert session.fire_at is None              # nothing left armed
    finally:
        player.close()
        runner.stop()


def test_starting_again_from_the_top_runs_every_cue_again(rig):
    player, session, runner, bus, _ = rig
    player.load(make_show())
    assert wait_burned(player)
    player.preset()
    assert wait_until(lambda: player.applied == "q00")
    player.run(time.monotonic() + 0.3)
    assert wait_until(lambda: player.state == ENDED, timeout=6)
    first_run = len(show_times(bus))
    assert first_run == 3
    # START again (the page asks, the server sends force): same show, new T0.
    t0 = time.monotonic() + 0.6
    player.run(t0)
    assert player.state == RUNNING
    assert wait_until(lambda: player.state == ENDED, timeout=8)
    assert len(show_times(bus)) == first_run + 3
    assert player.applied == "q02"
    assert 0 <= show_times(bus)[-2] - (t0 + 0.8) < 0.05


def test_restarting_a_one_cue_show_refires_it(rig):
    # _run_no_of() (embedded in the session key alongside the cue id) is
    # what tells two runs of a one-cue show apart, since the only cue is
    # both the first and last of every run.
    player, session, runner, bus, _ = rig
    show = make_show(sents=(-REFRESH,), duration=0.3)
    player.load(show)
    assert wait_burned(player)
    player.run(time.monotonic() + 0.3)
    assert wait_until(lambda: player.state == ENDED, timeout=4)
    first_run = len(show_times(bus))
    assert first_run == 1
    player.load(show)
    assert wait_burned(player)
    player.run(time.monotonic() + 0.3)          # from the top again
    assert wait_until(lambda: player.state == ENDED, timeout=4)
    assert len(show_times(bus)) == first_run + 1     # refired, not stuck
    assert player.applied == "q00"


def test_preset_survives_a_run_no_bump_and_is_not_repainted_at_start(rig):
    # PRESET, then START: run() bumps _run_no even though nothing but the
    # T0 changed, so the preset's own FIRED session key (from preset(),
    # an earlier run number) must still read as "already applied".
    player, session, runner, bus, _ = rig
    player.load(make_show())
    assert wait_burned(player)
    player.preset()
    assert wait_until(lambda: player.applied == "q00")
    preset_run_no = player._run_no
    t0 = time.monotonic() + 0.5
    player.run(t0, "abc1234567")
    assert player._run_no != preset_run_no          # the bump happened
    assert wait_until(lambda: player.state == ENDED, timeout=6)
    assert [e for e in events(bus) if e[0] == "show"][:1] == [("show", 1)]
    assert len([e for e in events(bus) if e[0] == "show"]) == 3


def test_branch_one_does_not_preempt_an_armed_cue_still_waiting_to_fire(rig):
    # An owned cue with a fire_at set carries a promise to fire at that
    # instant; arm()-ing any OTHER cue in its place would displace it
    # outright, and the runner's own session.fired() for the displaced
    # cue would then find a different cue_id and drop the tally with no
    # error - the cue is simply never shown. Reproduced directly against
    # _plan() rather than by racing real clocks.
    player, session, runner, bus, _ = rig
    show = make_show(sents=(-REFRESH, 1.0, 1.15))    # q01/q02 0.15 s apart
    player.load(show)
    key = player._key(show, show["cues"][1])   # q01, as _send() would key it
    with player._lock:
        player.state, player.t0 = RUNNING, time.monotonic() - 1.0
        player.applied, player.dirty = "q00", False
        session.active, session.phase, session.cue_id = True, ARMED, key
        session.fire_at = time.monotonic() + 10        # nowhere near due
        _, action = player._plan()

        # q01's session entry must be untouched, and q02 not sent in its
        # place - it fires later (at 1.15), or at once if that has
        # passed by the time q01 finally does.
        assert session.cue_id == key and session.phase == ARMED
    assert action is None or action[1]["id"] != "q02"


# ---- 36 boards x 10 cues: the burn-time estimate ----

def test_the_36_board_10_cue_burn_time_estimate(rig):
    """No test can literally run a 36-board serial bus fast, but the
    estimate the STATUS note and the conductor's operator warning rely
    on is anchored here to the same constant ShowPlayer uses: each
    board write costs SAVE_S_PER_BOARD (docs: ~0.22-0.25 s measured),
    and a burn is boards x cues writes, back to back."""
    from ui.showplay import SAVE_S_PER_BOARD

    boards, cues = 36, 10
    estimate_s = boards * cues * SAVE_S_PER_BOARD
    assert 70 <= estimate_s <= 100          # ~90 s at 0.25 s/board
