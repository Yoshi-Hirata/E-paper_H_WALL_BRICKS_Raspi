"""ui/showplay.py: the unit runs its show file from a T0, alone.

Real time, compressed: a "refresh" of 0.3 s, cues under a second apart,
the runner on the FakeBus. What is checked is what goes out on the bus
and when - the saves before, the single show at T0 + sent.
"""

from __future__ import annotations

import json
import struct
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


def test_a_forward_jump_over_an_armed_cue_disarms_it_instead_of_firing_it(rig):
    # SEEK/NEXT can jump clean past a cue that was already armed for the
    # old T0. Left alone it would fire as scheduled - the wrong picture,
    # since the new T0 says a later cue (q02) is the one due now.
    player, session, runner, bus, _ = rig
    player.load(make_show(sents=(-REFRESH, 0.8, 1.7), duration=30))
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
    # The mirror case (§2.4: a backward seek lands units inside the show,
    # never skipping a cue): the armed cue keeps its identity, only its
    # fire time moves - _send()'s own re-timing, not the new disarm above.
    player, session, runner, bus, _ = rig
    player.load(make_show(sents=(-REFRESH, 5.0, 9.0), duration=30))
    player.preset()
    assert wait_until(lambda: player.applied == "q00")
    player.run(time.monotonic() + 0.1)
    t0_close = time.monotonic() + 0.4 - 5.0                 # q01 due in 0.4 s: armed
    player.run(t0_close)
    assert wait_until(lambda: session.phase == "armed"
                      and session.cue_id.endswith(("q01", "q01+")))
    # Backward (T0 LATER): q01 is 2.0 s away again, not skipped - just re-timed.
    t0_back = time.monotonic() - 3.0
    player.run(t0_back)
    assert wait_until(lambda: session.phase == "armed"
                      and abs((session.fire_at or 0) - (t0_back + 5.0)) < 0.1)


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


# ---- found in review (2026-09-21) ----

class SlowBus:
    """A FakeBus whose saves take a while, so a cue can be caught loading."""

    def __new__(cls, delay):
        from tests.test_ui_runner import FakeBus

        bus = FakeBus()
        request = bus.request

        def slow(frame, retries=3):
            if frame.cmd == SAVE:
                time.sleep(delay)
            return request(frame, retries)
        bus.request = slow
        return bus


def make_rig(tmp_path, bus):
    session, runner, bus = make_session(bus)
    player = ShowPlayer(session, store=tmp_path, save_s=0.01, margin_s=0.15,
                        grace_s=0.3, tick_s=0.02, setup_s=0.5,
                        setup_board_s=0.0, retry_s=0.2)
    return player, session, runner, bus


def test_hold_while_the_boards_are_still_loading_does_not_fire(tmp_path):
    # The cue already carries its fire time while it loads; HOLD has to
    # take it off, or the show goes out in the middle of the hold.
    player, session, runner, bus = make_rig(tmp_path, SlowBus(0.4))
    try:
        player.load(make_show(sents=(-REFRESH, 5.0, 9.0), duration=30))
        player.run(time.monotonic() - 4.5)          # q01 is due in 0.5 s
        assert wait_until(lambda: session.phase == "preparing"
                          and session.cue_id.endswith("q01+"))
        player.hold()
        assert session.fire_at is None
        time.sleep(1.5)                             # past the instant
        assert [f for f in bus.sent if f.cmd == SHOW] == []
        assert session.phase == "ready"
    finally:
        player.close()
        runner.stop()


def test_a_board_that_misses_a_change_gets_the_whole_picture(tmp_path):
    from tests.test_ui_remote import PickyBus

    bus = PickyBus(set())
    player, session, runner, bus = make_rig(tmp_path, bus)
    sent_arrays = []
    request = bus.request

    def spy(frame, retries=3):
        if frame.cmd == SAVE:
            sent_arrays.append((frame.dest, frame.data[3], frame.data[2 + 60]))
        return request(frame, retries)
    bus.request = spy
    try:
        player.load(make_show(sents=(-REFRESH, 0.8, 4.0), duration=30))
        player.preset()
        assert wait_until(lambda: player.applied == "q00")
        bus.silent = {2}                            # board 2 drops out...
        player.run(time.monotonic() + 0.2)
        assert wait_until(lambda: player.applied == "q01")
        assert player.dirty and "board 2 missed q01" in player.status()["note"]
        mark = len(sent_arrays)
        bus.silent = set()                          # ...and comes back
        runner._next_reprobe = 0.0
        # Healed at the next chance: by a whole repaint of q01 if the
        # reprobe finds the board first, else by q02 going out whole
        # instead of as a change. Either way board 2 is written the full
        # picture (its colour is 2 in both) and the garment ends clean.
        assert wait_until(lambda: player.applied == "q02" and not player.dirty,
                          timeout=8)
        assert (2, 2, 2) in sent_arrays[mark:]
        assert player.status()["note"] == ""
    finally:
        player.close()
        runner.stop()


def test_a_board_that_stays_dead_does_not_repaint_the_garment_for_ever(tmp_path):
    from tests.test_ui_remote import PickyBus

    player, session, runner, bus = make_rig(tmp_path, PickyBus(set()))
    try:
        player.load(make_show(sents=(-REFRESH, 0.6, 30.0), duration=60))
        player.preset()
        assert wait_until(lambda: player.applied == "q00")
        bus.silent = {2}
        player.run(time.monotonic() + 0.2)
        assert wait_until(lambda: player.applied == "q01")
        time.sleep(3.0)
        shows = [f for f in bus.sent if f.cmd == SHOW]
        assert len(shows) == 3          # preset, q01, ONE whole repaint
        assert player.dirty             # still said, on /status and the LCD
    finally:
        player.close()
        runner.stop()


def test_show_files_missing_what_the_player_needs_are_refused(rig):
    player, *_ = rig
    good = make_show()
    for broken in ({k: v for k, v in good.items() if k != "refresh_s"},
                   {k: v for k, v in good.items() if k != "duration"},
                   dict(good, id=""), dict(good, cues=["q00"]), []):
        with pytest.raises(RemoteError):
            player.load(broken)
    assert player.show is None


def test_a_show_with_the_wrong_delay_unit_ms_is_refused(rig):
    player, *_ = rig
    good = make_show()
    with pytest.raises(RemoteError):
        player.load(dict(good, delay_unit_ms=20))       # this unit's frame is 10 ms
    player.load(dict(good, delay_unit_ms=10))            # matches: fine
    assert player.show is not None


def test_a_restored_t0_in_the_future_is_not_trusted(rig):
    player, session, runner, bus, store = rig
    player.load(make_show(duration=60))
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
    stamp = (store / "show.json").stat().st_mtime_ns
    player.run(time.monotonic() + 5)
    player.hold()
    player.stop()
    assert (store / "show.json").stat().st_mtime_ns == stamp
    assert not list(store.glob("*.tmp"))


def test_status_answers_while_the_first_cue_takes_the_port(tmp_path):
    # start_remote() can wait seconds for the previous worker; the PC's
    # poll and the LCD read status() meanwhile.
    player, session, runner, bus = make_rig(tmp_path, SlowBus(0.0))
    started = runner.start_remote

    def slow_start(sess):
        time.sleep(1.0)
        return started(sess)
    runner.start_remote = slow_start
    try:
        player.load(make_show(duration=30))
        player.preset_thread = None
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
    # and the arming. The boards may be written - the time must not be set.
    player, session, runner, bus, _ = rig
    player.load(make_show(sents=(-REFRESH, 5.0, 9.0), duration=30))
    player.run(time.monotonic() - 1.0)
    assert wait_until(lambda: player.applied == "q00")
    show, cue = player.show, player.show["cues"][1]
    stale = player._epoch
    player.hold()                                   # ...the operator was faster
    player._send(show, cue, False, time.monotonic() + 0.3, stale)
    assert wait_until(lambda: session.phase == "ready")
    assert session.fire_at is None
    time.sleep(0.6)
    assert len(show_times(bus)) == 1                # only the preset ever fired


def test_the_same_goes_for_stop_and_for_a_new_show(rig):
    player, session, runner, bus, _ = rig
    player.load(make_show(duration=30))
    show, cue = player.show, player.show["cues"][1]
    for command in (player.stop, lambda: player.load(make_show(duration=31))):
        stale = player._epoch
        command()
        player._send(show, cue, True, time.monotonic() + 0.2, stale)
        time.sleep(0.5)
        assert session.fire_at is None
    assert show_times(bus) == []


def test_the_show_ends_on_the_clock_even_if_the_last_cue_never_lands(tmp_path):
    from tests.test_ui_remote import PickyBus

    player, session, runner, bus = make_rig(tmp_path, PickyBus({1, 2}))
    try:
        import ui.showplay as showplay

        player.load(make_show(sents=(-REFRESH, 0.3, 0.6), duration=1))
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
    # The garment showed q02, not the preset: all three go out again, the
    # preset as a whole picture, and nothing is mistaken for already sent.
    assert len(show_times(bus)) == first_run + 3
    assert player.applied == "q02"
    assert 0 <= show_times(bus)[-2] - (t0 + 0.8) < 0.05


def test_a_cue_with_its_own_refresh_time_spaces_the_next_write_by_it(rig):
    player, session, runner, bus, _ = rig
    current = make_show(sents=(-REFRESH, 0.0, 1.0))["cues"][1]
    nxt = make_show(sents=(-REFRESH, 0.0, 1.0))["cues"][2]

    def need_for(refresh):
        return (player._lead(current) + refresh
                + player._lead(nxt, after_another=True))

    need_default = need_for(REFRESH)            # the show's own refresh
    override = REFRESH / 3                      # this cue's own, much faster
    need_own = need_for(override)
    assert need_own < need_default              # the override must matter
    room = (need_default + need_own) / 2        # enough for its own, not the show's

    def plan_with(refresh_s):
        show = make_show(sents=(-REFRESH, 0.0, room + 0.05), duration=1000)
        if refresh_s is not None:
            show["cues"][1]["refresh_s"] = refresh_s
        player.load(show)
        with player._lock:
            player.state = RUNNING
            player.t0 = time.monotonic() - 0.05
            player.applied = show["cues"][1]["id"]
            player.dirty = True                 # needs a whole repaint
        return player._plan()[1]

    assert plan_with(None) is None              # the show's default is too slow
    assert plan_with(override) is not None      # its own, smaller one fits


def test_a_cue_with_delay_tables_hands_them_to_the_session(rig):
    player, session, runner, bus, _ = rig
    show = make_show(duration=30)
    NO_DELAY = 0xFFFF
    swept = struct.pack(">64H", *([NO_DELAY] + [70] * 62 + [NO_DELAY]))
    for cue in show["cues"]:
        cue["delays"] = {"1": swept.hex(), "2": swept.hex()}
        cue["span"] = 0.7
    player.load(show)
    player.preset()
    assert wait_until(lambda: player.applied == "q00")
    delays = [f for f in bus.requested if f.cmd == 0x1F]
    assert [f.dest for f in delays] == [1, 1, 2, 2]        # low + high per board
    assert delays[0].data[2:] == bytes([0]) + bytes([70] * 62) + bytes([0])
    # Its lead counts the tables: each board is written twice.
    assert player._lead(show["cues"][1]) > player._lead(
        dict(show["cues"][1], delays={}))


# ---- the director's 1 s gap (conductor/timeline.py min_interval/validate) ----

def test_the_write_is_issued_right_after_the_previous_fire(tmp_path):
    """ui/showplay.py's _plan() branch 1 starts writing the next cue's
    boards the moment it is due, whatever the previous cue is still
    doing - it does not wait for the previous picture to complete first.

    This only checks that: the write for q02 is issued after q01 fires,
    and q02 still fires exactly on time (refresh + the director's 1 s
    gap after q01's send). It deliberately does NOT assert anything
    about q01's own refresh completing, nor claim the write overlaps a
    live repaint - docs/STATUS.md (2026-09-24 fix round) is explicit
    that whether a save actually executes during one has not been
    confirmed on real hardware (a unit-side fix for pre-empting an
    unfired cue is being done separately); the window here is widened to
    q02's own send so this cannot flake on a loaded machine.
    """
    session, runner, bus = make_session()
    player = ShowPlayer(session, store=tmp_path, save_s=0.5, margin_s=0.1,
                        grace_s=0.3, tick_s=0.02, setup_s=0.5,
                        setup_board_s=0.0)
    try:
        gap = 1.0                                    # the director's minimum
        show = make_show(sents=(-REFRESH, 0.0, REFRESH + gap), duration=5.0)
        player.load(show)
        player.preset()
        assert wait_until(lambda: player.applied == "q00")

        t0 = time.monotonic() + 0.1
        player.run(t0)
        assert wait_until(lambda: player.applied == "q01")
        fire_q01 = show_times(bus)[1]

        assert wait_until(lambda: player.applied == "q02", timeout=4)
        fire_q02 = show_times(bus)[2]

        saves = [(frame.dest, frame.data[3], t)
                for frame, t in zip(bus.log, bus.times) if frame.cmd == SAVE]
        prepare_q02 = next(t for dest, colour, t in saves
                           if dest == 1 and colour == 3)

        # The write for q02 is issued after q01 actually fired...
        assert prepare_q02 > fire_q01
        # ...and, safely, before q02's own send (not some tighter window
        # relative to q01's refresh completing, which is not confirmed).
        assert prepare_q02 < fire_q02
        # And still fires q02 on time: refresh + the 1 s gap after q01.
        assert 0 <= fire_q02 - (t0 + REFRESH + gap) < 0.05
    finally:
        player.close()
        runner.stop()
