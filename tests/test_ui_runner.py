from __future__ import annotations

import errno
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

from epaper.protocol import Frame
from ui.patterns import BY_KEY
from ui.runner import NO_DELAY, DemoRunner


GET_VERSION = 0x02          # the landing check's read-only question


class FakeBus:
    """Records frames and ACKs everything (optionally failing on a cmd).

    The landing check (ui/runner.py's _verify_landing()) asks one board
    0x02 after a cue's broadcast and reads whether it answers at all, so
    this bus answers that question per board:

      "deaf"    silence - the board is repainting, which is what a board
                that TOOK the show broadcast does. The default, because
                that is the normal outcome on the real wall
      "busy"    ACK_BUSY: working, so the broadcast landed
      "idle"    ACK_FAIL 0x0A - what production firmware really answers
                to 0x02 (docs/SPECIFICATION.md 5.5). An answer means the
                board is listening, so it never got the show
      "version" ACK_SUCCESS with version bytes: the same "it answered",
                for the firmware the V1.0 spec described
    """

    def __init__(self, nak_on: int | None = None, ack_cmd: int = 0x80,
                 witness: "dict[int, str] | None" = None,
                 deaf_boards: bool = True):
        self.sent: list[Frame] = []
        self.requested: list[Frame] = []
        self.requested_at: "list[float]" = []   # when each request() began
        self.nak_on = nak_on
        self.ack_cmd = ack_cmd
        self.closed = False
        self.witness = dict(witness or {})
        self.witness_default = "deaf" if deaf_boards else "idle"
        # One late answer from a board nobody is asking any more - the
        # frame a deaf board queues and sends after its repaint.
        self.stray: "int | None" = None
        self.asked: "list[Frame]" = []      # only the 0x02 questions
        self.asked_at: "list[float]" = []
        self.asked_tries: "list[int]" = []  # ...and how many tries each got
        self.sent_at: "list[float]" = []    # when each send() went out
        # Called while a 0x02 is "in flight", for the tests that need
        # something to happen between the question and the re-send.
        self.on_ask = None

    @property
    def broadcasts(self) -> "list[Frame]":
        """Every "show slot N" that went to the whole bus."""
        return [f for f in self.sent if f.cmd == 0x1D and f.dest == 0xFF]

    def send(self, frame):
        self.sent.append(frame)
        self.sent_at.append(time.monotonic())

    def request(self, frame, retries=3, timeout=None):
        self.requested.append(frame)
        self.requested_at.append(time.monotonic())
        if frame.cmd == GET_VERSION:
            self.asked_tries.append(retries)
            return self._version_reply(frame)
        if self.nak_on is not None and frame.cmd == self.nak_on:
            return None
        # Real boards answer with DevType 0xFF (docs/SPECIFICATION.md 5.3).
        return Frame(dest=0x00, src=frame.dest, dev_type=0xFF,
                     cmd=self.ack_cmd)

    def _version_reply(self, frame):
        self.asked.append(frame)
        self.asked_at.append(time.monotonic())
        if self.on_ask is not None:
            self.on_ask()
        if self.stray is not None:
            # A late answer to an EARLIER question, from another board.
            src, self.stray = self.stray, None
            return Frame(dest=0x00, src=src, dev_type=0xFF, cmd=0x80)
        state = self.witness.get(frame.dest, self.witness_default)
        if isinstance(state, list):
            # A board that answers differently each time it is asked;
            # the last entry is what it keeps saying.
            state = state.pop(0) if len(state) > 1 else state[0]
        if state == "deaf":
            return None
        if state == "busy":
            return Frame(dest=0x00, src=frame.dest, dev_type=0xFF, cmd=0x82)
        if state == "version":
            return Frame(dest=0x00, src=frame.dest, dev_type=0xFF, cmd=0x80,
                         data=b"\x01\x04")
        return Frame(dest=0x00, src=frame.dest, dev_type=0xFF, cmd=0x81,
                     data=b"\x0a")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True


class RecoveringBus(FakeBus):
    """A bus whose every write BLOCKS until it is put right again.

    The 2026-09-28 LOOK28 state (ui/runner.py's RECOVERED_MS): the master
    accepts every frame, executes none, and every write takes ~360 ms.
    `cure` is what stops it - which is the whole question the recovery
    exists to answer, since the padding-alone test could not be run from
    the show PC:

      "padding"  the resync padding bytes (transport.RESYNC_PAD_BYTES)
      "reopen"   only the port closed and opened again
      "never"    nothing here does; the unit has to be restarted
    """

    def __init__(self, cure: str = "padding", seconds: float = 0.36,
                 port: str = "/dev/fake", **kwargs):
        super().__init__(**kwargs)
        self.cure, self.block_s = cure, seconds
        self.stalled = True
        self.padded: "list[int]" = []       # pad() calls, with their counts
        self.reopened: "list[str]" = []     # reopen() calls, with the port
        self.port = port

    def pad(self, count=8):
        self.padded.append(count)
        if self.cure == "padding":
            self.stalled = False

    def reopen(self, port=None):
        self.port = port or self.port
        self.reopened.append(self.port)
        if self.cure in ("padding", "reopen"):
            self.stalled = False
        return self.port

    def unplug(self, back_as: "str | None" = None):
        """A USB re-enumeration: the device node goes, and comes back under
        `back_as` (the kernel's ttyACM0 -> ttyACM1). Paired with
        `token()` below as the runner's link_token."""
        self.gone = self.port
        self.back_as = back_as or self.port

    def token(self, port: str):
        """What DemoRunner(link_token=...) reads: None for a node that is
        not there. A port that has been unplugged reads as gone until the
        one it comes back as is asked for."""
        if getattr(self, "gone", None) is None:
            return "up"
        return "up" if port == getattr(self, "back_as", None) else None

    def _block(self):
        if self.stalled:
            time.sleep(self.block_s)

    def send(self, frame):
        self._block()
        super().send(frame)

    def request(self, frame, retries=3, timeout=None):
        self._block()
        return super().request(frame, retries=retries, timeout=timeout)


class DegradedMaster(FakeBus):
    """The degraded master of 2026-09-28 as radxa-07 showed it, measured:

      * every write BLOCKS, and how long grows with the idle gap before it -
        `quick_s` (61 ms) within `quick_gap` of the previous write, `slow_s`
        (358 ms) after a longer pause. This is what made "bus recovered by
        padding (512 -> 61 ms)" a false reading;
      * no frame is executed and nobody answers - not the master's ACK to a
        unicast STOP, not a relayed board ("no boards answering" for 100 s);
      * padding and a port reopen change NOTHING. Only usb_reset() cures it.

    And as the REAL firmware answers when it is healthy (radxa-07, 2026-09-28
    12:40): every unicast STOP / config is ACKed, and a 0x02 over the bus is
    NEVER answered, healthy or not - so a health check built on 0x02 cannot
    pass this suite (it read a healthy master as "silent" on the unit). And,
    healthy, it misses the first unicast asked within 0.1 s of a broadcast
    (radxa-07 on dc846a1: every precheck's first ask, whichever board).

    The USB layer, for the runner's usb_reset= and link_token=: a reset
    takes `reset_s`, the node vanishes and is back `reenum_s` later - a new
    node, under `back_as` if given - and the first `eacces` opens after it
    are refused EACCES (udev not done). `reset_ok=False` is a unit that
    cannot reset at all; `reset_cures=False` a reset that does not take; and
    `half_cure=True` one after which the master answers 0x02 but writes
    after a pause still block - the second half of the proof's reason.
    """

    def __init__(self, *, quick_s=0.061, slow_s=0.358, quick_gap=0.2,
                 reset_s=0.0, reenum_s=0.05, open_s=0.0,
                 port="/dev/ttyACM0", back_as=None, eacces=0,
                 reset_ok=True, reset_cures=True, half_cure=False):
        super().__init__(deaf_boards=False)     # a healthy master ACKs
        self.quick_s, self.slow_s, self.quick_gap = quick_s, slow_s, quick_gap
        self.reset_s, self.reenum_s, self.open_s = reset_s, reenum_s, open_s
        self.port, self.back_as, self.eacces_after_reset = port, back_as, eacces
        self.reset_ok, self.reset_cures, self.half_cure = (
            reset_ok, reset_cures, half_cure)
        self.degraded = True
        self.slow_after_pause = False           # the half cure
        # radxa-07, dc846a1: a healthy master misses the FIRST unicast asked
        # within `relay_s` of a broadcast - it is still relaying it.
        self.relay_s = 0.1
        self.last_broadcast = float("-inf")
        self.relaying = False
        self.last_write = time.monotonic()
        self.gen = 0
        self.node_back_at = 0.0
        self.eacces_left = 0
        self.resets: "list[str]" = []
        self.reopened: "list[str]" = []
        self.padded: "list[int]" = []
        self.closes = 0

    # ---- the wire ----
    def _write(self):
        if self.degraded or self.slow_after_pause:
            gap = time.monotonic() - self.last_write
            time.sleep(self.quick_s if gap < self.quick_gap else self.slow_s)
        self.last_write = time.monotonic()

    def send(self, frame):
        self._write()
        if frame.dest == 0xFF:
            self.last_broadcast = time.monotonic()
            self.relaying = True
        super().send(frame)

    def request(self, frame, retries=3, timeout=None):
        self._write()
        relayed_over = (self.relaying and frame.dest != 0xFF
                        and time.monotonic() - self.last_broadcast
                        < self.relay_s)
        if frame.dest != 0xFF:
            self.relaying = False
        if self.degraded or frame.cmd == GET_VERSION or relayed_over:
            # Accepted, never answered - and a silence is only known once
            # the whole read window has passed, every try of it
            # (transport.Bus.request), so the caller pays for that too. A
            # 0x02 the same, healthy or not: the real firmware never
            # answers one over the bus.
            self.requested.append(frame)
            self.requested_at.append(time.monotonic())
            if frame.cmd == GET_VERSION:
                self.asked.append(frame)
                self.asked_at.append(time.monotonic())
            window = 0.5 if timeout is None else timeout
            time.sleep(window * max(1, retries))
            return None
        return super().request(frame, retries=retries, timeout=timeout)

    def pad(self, count=8):
        self.padded.append(count)               # cures nothing

    # ---- the port ----
    def close(self):
        self.closes += 1

    def reopen(self, port=None):
        # A refused open fails at os.open(), at once; only one that
        # succeeds pays transport.Bus._open's settle.
        port = port or self.port
        if self.eacces_left > 0:
            self.eacces_left -= 1
            raise PermissionError(errno.EACCES, "Permission denied")
        if port != self.port or time.monotonic() < self.node_back_at:
            raise FileNotFoundError(errno.ENOENT, "No such file or directory")
        time.sleep(self.open_s)
        self.reopened.append(port)              # cures nothing either
        return port

    def token(self, port):
        if port != self.port or time.monotonic() < self.node_back_at:
            return None
        return ("node", self.gen)

    def find_port(self):
        return self.port if self.token(self.port) is not None else None

    # ---- the USB layer ----
    def usb_reset(self, port, timeout=None):
        if not self.reset_ok:
            return False, "unsupported on this test unit"
        if timeout is not None and self.reset_s > timeout:
            # A sudo that hangs, killed at the caller's timeout: nothing
            # was reset, as far as anyone can tell.
            time.sleep(timeout)
            return False, f"sudo usbreset: no answer in {timeout:.1f} s"
        time.sleep(self.reset_s)
        self.resets.append(port)
        self.gen += 1
        self.node_back_at = time.monotonic() + self.reenum_s
        if self.back_as:
            self.port = self.back_as
        self.eacces_left = self.eacces_after_reset
        if self.reset_cures:
            self.degraded = False
            self.slow_after_pause = self.half_cure
        return True, "ioctl"

    def reset_available(self, port):
        return (True, "ioctl") if self.reset_ok else (False, "no usbreset here")


class AutoplayMaster(FakeBus):
    """The garment's master board as the week's incidents showed it (PM's
    analysis, 2026-09-28; docs/SPECIFICATION.md 4.6): it resumes its FACTORY
    AUTOPLAY `autoplay_after` seconds (85 s on the real board, scaled down by
    the test) after the LAST BROADCAST STOP it received, unless another one
    arrives first. Show frames (0x1D) do not reset that clock - nothing but
    a broadcast 0x17 does. Once it autoplays it stays that way (on the unit
    only a USB reset got it back): every write stalls `stall_s` and nothing
    is executed, and no unicast is answered.

    Records every broadcast on the wire with its time (`wire`: (t, cmd,
    slot)), and when the autoplay began (`autoplay_at`, None while it has
    not)."""

    def __init__(self, autoplay_after: float = 85.0, stall_s: float = 0.036):
        super().__init__()
        self.autoplay_after, self.stall_s = autoplay_after, stall_s
        self.last_stop = time.monotonic()
        self.autoplay_at: "float | None" = None
        self.wire: "list[tuple[float, int, int | None]]" = []
        self.ignored: "list[Frame]" = []

    def autoplaying(self) -> bool:
        if (self.autoplay_at is None
                and time.monotonic() - self.last_stop >= self.autoplay_after):
            self.autoplay_at = self.last_stop + self.autoplay_after
        return self.autoplay_at is not None

    def send(self, frame):
        stuck = self.autoplaying()
        now = time.monotonic()
        if frame.dest == 0xFF:
            slot = frame.data[0] if frame.cmd == 0x1D and frame.data else None
            self.wire.append((now, frame.cmd, slot))
        if stuck:
            time.sleep(self.stall_s)
            self.ignored.append(frame)
        elif frame.cmd == 0x17 and frame.dest == 0xFF:
            self.last_stop = now
        super().send(frame)

    def request(self, frame, retries=3, timeout=None):
        if self.autoplaying():
            time.sleep(self.stall_s)
            self.requested.append(frame)
            self.requested_at.append(time.monotonic())
            return None
        return super().request(frame, retries=retries, timeout=timeout)


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def make_runner(bus, **kwargs):
    kwargs.setdefault("interval", 0.05)
    kwargs.setdefault("boards", list(range(1, 21)))     # the test wall
    kwargs.setdefault("guard_delay", 0.0)
    kwargs.setdefault("port", "/dev/fake")
    kwargs.setdefault("echo_log", False)
    # Compress the retry timings; the real ones are sized to outwait a
    # 9.8 s repaint and would make these tests take minutes.
    kwargs.setdefault("command_attempts", 2)
    kwargs.setdefault("save_attempts", 1)
    kwargs.setdefault("retry_delays", (0.01,))
    kwargs.setdefault("busy_delay", 0.01)
    kwargs.setdefault("reopen_delay", 0.01)
    kwargs.setdefault("port_wait", 0.01)
    kwargs.setdefault("show_gap", 0.001)
    kwargs.setdefault("link_poll", 0.01)
    kwargs.setdefault("probe_sweep_delay", 0.01)
    kwargs.setdefault("reprobe_interval", 0.05)
    # The landing check waits a second on the real unit (the boards' deaf
    # window); here it waits long enough to be observable and no longer.
    kwargs.setdefault("verify_after", 0.05)
    kwargs.setdefault("verify_read", 0.01)
    # The check is opt-in on a real unit (2026-09-26: the current firmware
    # answers 0x02 mid-repaint, so it re-sent nearly every cue); the tests
    # of the check itself turn it on here, the default is pinned below.
    kwargs.setdefault("verify_fire", True)
    # The fake port has no device node, so standby would read every poll
    # as an unplug. Tests that care about that supply their own.
    kwargs.setdefault("link_token", lambda port: "up")
    # Never the real transport.usb_reset in a test: the suite runs on the
    # Radxas too, and a real master may be plugged in. Tests of the reset
    # supply a fake one (DegradedMaster.usb_reset).
    kwargs.setdefault("usb_reset",
                      lambda port, timeout=None: (False, "not in the tests"))
    # ...nor the real availability check (it would run `sudo -n true`). No
    # reset means, unless a test says so: a sweep that finds nobody would
    # otherwise reset the USB (_setup_usb_reset()) in every test of an
    # empty or silent wall. DegradedMaster's tests supply reset_available.
    kwargs.setdefault("usb_reset_check",
                      lambda port: (False, "not in the tests"))
    # ...nor the real USB descriptor read behind /status usb_board: the
    # same answer on every machine (a board with no serial), unless a test
    # of that field supplies its own.
    kwargs.setdefault("usb_board_info",
                      lambda port: {"serial": None, "family": None})
    return DemoRunner(open_bus=lambda port: bus, **kwargs)


def test_the_landing_check_is_off_unless_a_unit_is_started_with_it():
    """The 2026-09-26 rehearsal: board 1 answered the read-only query while
    it was repainting, so every landed cue read as lost and was sent
    again - a double repaint on nearly every cue, on both units. Until the
    boards' behaviour during a repaint is measured again the check must
    stay off unless the service is started with --verify-fire."""
    plain = DemoRunner(open_bus=lambda port: FakeBus(), port="/dev/fake",
                       boards=[1, 2], echo_log=False)
    assert plain.verify_fire is False
    assert make_runner(FakeBus()).verify_fire is True        # the tests opt in
    assert make_runner(FakeBus(), verify_fire=False).verify_fire is False


def test_runs_cycles_and_stops_cleanly():
    bus = FakeBus()
    runner = make_runner(bus)
    runner.start(BY_KEY["wave"])
    assert wait_until(lambda: runner.cycle >= 2)
    runner.stop()
    assert not runner.running
    assert bus.closed
    # Each cycle: per-board stop + save, then one broadcast show.
    assert any(f.cmd == 0x13 for f in bus.requested)   # save color
    assert any(f.cmd == 0x1D for f in bus.sent)        # show single


def test_first_action_silences_the_factory_autoplay():
    bus = FakeBus()
    runner = make_runner(bus)
    runner.start(BY_KEY["solid"])
    assert wait_until(lambda: runner.cycle >= 1)
    runner.stop()
    first = bus.sent[0]
    assert first.cmd == 0x17 and first.dest == 0xFF


def test_missing_ack_is_reported_but_not_fatal():
    bus = FakeBus(nak_on=0x13)          # color save never answers
    runner = make_runner(bus)
    runner.start(BY_KEY["wave"])
    assert wait_until(lambda: runner.error is not None)
    assert "gave up" in runner.error
    assert any("ERROR" in line for line in runner.recent(10))
    # Reporting is not stopping: see tests/test_ui_resilience.py for the
    # full fault matrix behind that rule.
    assert runner.running
    runner.stop()


def test_nak_response_is_reported_but_not_fatal():
    bus = FakeBus(ack_cmd=0x81)         # ACK_FAIL
    runner = make_runner(bus)
    runner.start(BY_KEY["wave"])
    assert wait_until(lambda: runner.error is not None)
    assert runner.running
    runner.stop()


def test_missing_port_is_reported_and_waited_out(monkeypatch):
    import ui.runner as runner_module

    monkeypatch.setattr(runner_module, "find_port", lambda: None)
    runner = make_runner(FakeBus(), port=None)
    runner.start(BY_KEY["wave"])
    assert wait_until(lambda: runner.error == "no serial port")
    # It waits for the port to appear rather than ending the demo; the
    # recovery path itself is covered in tests/test_ui_resilience.py.
    assert runner.running
    runner.stop()


def test_elapsed_starts_at_zero_and_resets_on_stop():
    bus = FakeBus()
    runner = make_runner(bus)
    assert runner.elapsed == 0.0
    runner.start(BY_KEY["solid"])
    assert wait_until(lambda: runner.elapsed > 0)
    runner.stop()
    assert runner.elapsed == 0.0


def test_starting_again_replaces_the_running_demo():
    bus = FakeBus()
    runner = make_runner(bus)
    runner.start(BY_KEY["wave"])
    assert wait_until(lambda: runner.cycle >= 1)
    runner.start(BY_KEY["random"])
    assert runner.pattern.key == "random"
    assert runner.cycle == 0
    runner.stop()


def test_guard_stop_is_sent_between_refreshes():
    bus = FakeBus()
    runner = make_runner(bus, interval=0.2, guard_delay=0.05)
    runner.start(BY_KEY["wave"])   # no per-pattern interval override
    assert wait_until(lambda: runner.cycle >= 2)
    runner.stop()
    broadcast_stops = [f for f in bus.sent if f.cmd == 0x17 and f.dest == 0xFF]
    assert len(broadcast_stops) >= 2   # startup silence + at least one guard


def test_pattern_interval_overrides_the_runner_default():
    bus = FakeBus()
    # Runner default 10 s, but the pattern asks for 0.05 s; if the override
    # were ignored the second cycle would never arrive in time.
    slow = make_runner(bus, interval=10.0)
    fast = BY_KEY["solid"].__class__(
        "fast", "FAST", "test", BY_KEY["solid"].build, interval=0.05)
    slow.start(fast)
    assert wait_until(lambda: slow.cycle >= 2, timeout=3.0)
    slow.stop()


def test_log_is_mirrored_to_stdout_for_journalctl(capsys):
    runner = DemoRunner(open_bus=lambda port: FakeBus(), port="/dev/fake", boards=list(range(1, 21)))
    runner.emit("cycle 1 shown")
    assert "cycle 1 shown" in capsys.readouterr().out


def test_log_keeps_newest_lines_only():
    runner = DemoRunner(open_bus=lambda port: FakeBus(), port="/dev/fake", boards=list(range(1, 21)),
                        echo_log=False)
    for i in range(500):
        runner.emit(f"line {i}")
    lines = runner.recent(3)
    assert len(lines) == 3
    assert lines[-1].endswith("line 499")


# ---- exploring the bus: 1..60, stopping past the last board ----

class Wall(FakeBus):
    """Only the given boards answer; the others are empty sockets."""

    def __init__(self, present):
        super().__init__()
        self.present = set(present)

    def request(self, frame, retries=3, timeout=None):
        self.requested.append(frame)
        if frame.dest != 0xFF and frame.dest not in self.present:
            return None
        return Frame(dest=0x00, src=frame.dest, dev_type=0xFF, cmd=0x80)


def test_without_a_list_the_runner_explores_and_stops_past_the_last_board():
    from ui.runner import EXPLORE_GAP

    bus = Wall(set(range(1, 22)) - {16})               # a 21-board garment, 16 dead
    runner = make_runner(bus, boards=None)
    runner.start(BY_KEY["solid"])
    assert wait_until(lambda: "panels online" in " ".join(runner.log), timeout=10)
    assert runner.live == [b for b in range(1, 22) if b != 16]
    assert runner.expected == 21 and runner.reported_boards == list(range(1, 22))
    assert any("panels online: 20/21" in line for line in runner.log)
    assert any("absent" in line and "16" in line for line in runner.log)
    probed = {f.dest for f in bus.requested if f.dest != 0xFF}
    assert max(probed) == 21 + EXPLORE_GAP               # not 60
    assert 16 in runner.absent and 22 in runner.absent   # both get reprobed
    runner.stop()


def test_exploring_searches_the_whole_range_until_a_board_answers():
    from ui.runner import EXPLORE_GAP, MAX_BOARD_ID

    # The low addresses dead (a power feed off), the rest alive: found.
    bus = Wall(set(range(12, 19)) | {21})
    runner = make_runner(bus, boards=None, probe_sweeps=1)
    runner.start(BY_KEY["solid"])
    assert wait_until(lambda: "panels online" in " ".join(runner.log), timeout=20)
    assert runner.live == [12, 13, 14, 15, 16, 17, 18, 21]
    assert runner.expected == 21
    assert any("panels online: 8/21" in line for line in runner.log)
    probed = {f.dest for f in bus.requested if f.dest != 0xFF}
    assert max(probed) == 21 + EXPLORE_GAP
    runner.stop()
    # An empty bus: the whole range once, then "no boards answering".
    bus = Wall(set())
    runner = make_runner(bus, boards=None, probe_sweeps=1)
    runner.start(BY_KEY["solid"])
    assert wait_until(lambda: runner.error == "no boards answering", timeout=20)
    probed = {f.dest for f in bus.requested if f.dest != 0xFF}
    assert max(probed) == MAX_BOARD_ID
    assert runner.expected == 0
    runner.stop()


def test_a_given_list_is_probed_as_given():
    bus = Wall({1, 2})
    runner = make_runner(bus, boards=[1, 2, 3])
    runner.start(BY_KEY["solid"])
    assert wait_until(lambda: "panels online" in " ".join(runner.log), timeout=10)
    assert runner.expected == 3 and any("panels online: 2/3" in l for l in runner.log)
    runner.stop()


# ---- the landing check: did the cue's broadcast reach the boards? ----
#
# radxa-04, LOOK26 rehearsal 2026-09-26: the last cue was recorded as
# fired (+166 ms) and the boards never changed - the unit's USB link had
# been dropping frames all evening ("usb1-port1: disabled by hub (EMI?)",
# ttyACM0 -> ttyACM1 mid-show), and a show broadcast is one unacknowledged
# frame. The runner now asks one board whether it is repainting and, only
# if it plainly is not, sends the frame once more. See ui/runner.py's
# VERIFY_AFTER_S for the deaf window this leans on.

SHOW, GUARD = 0x1D, 0x17


def cue_array(color: int = 3) -> bytes:
    return bytes([0xFE] + [color] * 60 + [0xFF, 0xFF, 0xFE])


def sweep_table(first_frames: int) -> bytes:
    """A delay table whose earliest socket starts `first_frames` frames
    (10 ms each) after the show broadcast."""
    return struct.pack(">64H",
                       *([NO_DELAY] + [first_frames] * 62 + [NO_DELAY]))


def fired_cue(bus, boards=(1,), delays=None, span_s=None, **kwargs):
    """Prepare a cue, fire it, and wait for the fire itself.

    The landing check is still running when this returns - that is what
    the callers are here to watch.
    """
    from ui.remote import FIRED, READY, RemoteSession

    runner = make_runner(bus, boards=list(boards), **kwargs)
    session = RemoteSession(runner)
    session.prepare("c1", {b: cue_array(b) for b in boards},
                    delays=delays, span_s=span_s)
    assert wait_until(lambda: session.phase == READY)
    session.fire("c1", time.monotonic() + 0.05)
    assert wait_until(lambda: session.phase == FIRED)
    return session, runner


def logged(runner, text: str) -> bool:
    return any(text in line for line in runner.log)


def test_a_silent_board_means_the_broadcast_landed():
    # Silence is the GOOD answer: the board is deaf because it is
    # repainting, which is what taking the broadcast looks like.
    bus = FakeBus()                                   # every board deaf
    session, runner = fired_cue(bus)
    assert wait_until(lambda: session.verify is not None)
    assert session.verify == {"cue": "c1", "landed": "deaf",
                              "resent": False, "witness": 1}
    assert len(bus.broadcasts) == 1
    assert [f.cmd for f in bus.asked] == [GET_VERSION]
    assert logged(runner, "cue c1 landed (@01 deaf, checked +")
    runner.stop()


def test_a_busy_board_also_means_the_broadcast_landed():
    bus = FakeBus(witness={1: "busy"})
    session, runner = fired_cue(bus)
    assert wait_until(lambda: session.verify is not None)
    assert session.verify["landed"] == "busy"
    assert session.verify["resent"] is False
    assert len(bus.broadcasts) == 1
    assert logged(runner, "cue c1 landed (@01 busy, checked +")
    runner.stop()


def test_a_board_still_listening_gets_the_show_frame_again():
    # "idle" is ACK_FAIL 0x0A, what production firmware answers to 0x02
    # (docs/SPECIFICATION.md 5.5): an answer at all means the board is
    # not repainting, so it never got the show.
    bus = FakeBus(witness={1: ["idle", "deaf"]})
    session, runner = fired_cue(bus)
    assert wait_until(lambda: session.verify is not None)
    assert session.verify == {"cue": "c1", "landed": "deaf",
                              "resent": True, "witness": 1}
    assert len(bus.broadcasts) == 2
    assert [f.data[0] for f in bus.broadcasts] == [19, 19]      # same slot
    assert logged(runner, "cue c1 not applied at @01 (checked +")
    assert logged(runner, "cue c1 landed (@01 deaf, checked +")
    runner.stop()


def test_the_version_answer_of_the_v1_0_spec_reads_the_same_way():
    # A firmware that actually answers 0x02 with a version is just as
    # much "awake and listening" as one that refuses it.
    bus = FakeBus(witness={1: ["version", "busy"]})
    session, runner = fired_cue(bus)
    assert wait_until(lambda: session.verify is not None)
    assert session.verify["resent"] is True
    assert len(bus.broadcasts) == 2
    runner.stop()


def test_a_cue_is_never_broadcast_more_than_twice():
    # The 2026-08-14 measurement is the rule here: every extra copy is
    # another full repaint. Two is the cap, whatever the board says.
    bus = FakeBus(witness={1: "idle"})                # never repaints
    session, runner = fired_cue(bus)
    assert wait_until(lambda: session.verify is not None)
    assert session.verify == {"cue": "c1", "landed": "idle-after-resend",
                              "resent": True, "witness": 1}
    assert len(bus.broadcasts) == 2
    assert len(bus.asked) == 2
    assert logged(runner, "cue c1 re-send unconfirmed @01")
    runner.stop()
    assert len(bus.broadcasts) == 2                   # and nothing after


def test_a_re_sent_cue_still_reports_the_first_send_as_its_fire_time():
    bus = FakeBus(witness={1: ["idle", "deaf"]})
    session, runner = fired_cue(bus)
    assert wait_until(lambda: session.verify is not None)
    first = bus.sent_at[bus.sent.index(bus.broadcasts[0])]
    assert abs(session.fired_at - first) < 0.01       # not the re-send
    assert session.status()["late_ms"] < 50
    assert session.status()["verify"]["resent"] is True
    runner.stop()


def test_the_guard_stop_waits_for_the_re_sent_picture_not_the_first_one():
    # The re-sent frame is when the picture actually starts drawing, so
    # the guard 0x17 has to move with it - timed from the first send it
    # would land inside the re-sent cue's own sweep, which is the one
    # thing _guard_for() exists to prevent.
    bus = FakeBus(witness={1: ["idle", "deaf"]})
    session, runner = fired_cue(bus, guard_delay=0.4, verify_after=0.2)
    assert wait_until(lambda: session.verify is not None)
    shows = [i for i, f in enumerate(bus.sent)
             if f.cmd == SHOW and f.dest == 0xFF]
    assert len(shows) == 2
    first, last = bus.sent_at[shows[0]], bus.sent_at[shows[1]]
    assert last - first >= 0.2                      # the re-send's offset
    assert wait_until(lambda: any(f.cmd == GUARD and f.dest == 0xFF
                                  for f in bus.sent[shows[1]:]), timeout=3)
    guard = next(i for i in range(shows[1], len(bus.sent))
                 if bus.sent[i].cmd == GUARD and bus.sent[i].dest == 0xFF)
    # Measured from the LAST broadcast...
    assert bus.sent_at[guard] - last >= 0.4 - 0.02
    # ...which is the first send plus the whole re-send offset.
    assert bus.sent_at[guard] - first >= 0.4 + (last - first) - 0.02
    runner.stop()


def test_the_any_policy_asks_the_board_whose_sweep_starts_first():
    # Board 1 starts 0.30 s into the sweep, board 2 at 0.10 s: board 2 is
    # the one surely repainting when the question goes out, and the
    # question waits for ITS start, not for the broadcast. Only on a wall
    # where a relayed 0x02 has been shown to be harmless (SPECIFICATION
    # 5.7) - hence not the default.
    bus = FakeBus()
    session, runner = fired_cue(bus, boards=(1, 2), verify_witness="any",
                                delays={1: sweep_table(30),
                                        2: sweep_table(10)},
                                span_s=0.3)
    assert wait_until(lambda: session.verify is not None)
    assert [f.dest for f in bus.asked] == [2]
    fired = bus.sent_at[bus.sent.index(bus.broadcasts[0])]
    assert 0.10 + 0.05 <= bus.asked_at[0] - fired < 0.30
    assert session.verify["witness"] == 2
    runner.stop()


def test_the_witness_is_the_usb_board_even_when_another_starts_earlier():
    # The default policy. Board 2 starts the sweep and board 1 only
    # 0.30 s later, but board 1 is the one on the USB cable: a query to
    # board 2 has to be relayed by board 1, and a relayed query is not
    # known to be safe (0x29 wedges that CDC until a power cycle -
    # docs/SPECIFICATION.md 5.7). So the check waits for board 1's own
    # start instead, however much later that is.
    bus = FakeBus()
    session, runner = fired_cue(bus, boards=(1, 2),
                                delays={1: sweep_table(30),
                                        2: sweep_table(0)},
                                span_s=0.3)
    assert wait_until(lambda: session.verify is not None)
    assert [f.dest for f in bus.asked] == [1]
    fired = bus.sent_at[bus.sent.index(bus.broadcasts[0])]
    assert bus.asked_at[0] - fired >= 0.30 + 0.05
    assert session.verify["witness"] == 1
    # ...and the log says how long after the cue the answer was read, so
    # a witness that only starts late is not mistaken for a slow check.
    assert logged(runner, "cue c1 landed (@01 deaf, checked +0.")
    runner.stop()


def test_the_question_is_never_asked_before_the_witness_is_deaf():
    # THE invariant behind the whole check: a board that has not started
    # repainting answers, an answer means "re-send", and a re-send of a
    # frame that did land repaints the whole wall twice (2026-08-14). So
    # the question may never go out before the witness's own sweep start
    # plus the full deaf window - here 0.50 s + 0.20 s.
    bus = FakeBus()
    session, runner = fired_cue(bus, boards=(1, 2), verify_after=0.2,
                                delays={1: sweep_table(50),
                                        2: sweep_table(0)},
                                span_s=0.5)
    assert wait_until(lambda: session.verify is not None, timeout=3)
    fired = bus.sent_at[bus.sent.index(bus.broadcasts[0])]
    assert bus.asked_at[0] - fired >= 0.50 + 0.2
    assert len(bus.broadcasts) == 1                   # nothing re-sent
    assert not logged(runner, "early")
    runner.stop()


def test_the_check_is_skipped_when_the_usb_board_is_absent():
    # Nobody else may be asked under the default policy: the boards that
    # are there are all behind the relay of a board that is not.
    bus = FakeBus(witness={2: "idle", 3: "idle"})
    session, runner = fired_cue(bus, boards=(2, 3))
    assert wait_until(lambda: session.verify is not None)
    assert session.verify == {"cue": "c1", "landed": "skipped",
                              "resent": False, "witness": None}
    assert bus.asked == [] and len(bus.broadcasts) == 1
    assert logged(runner, "cue c1 verify skipped: usb board absent")
    runner.stop()


def test_a_cue_with_no_sweep_asks_a_live_board_straight_away():
    bus = FakeBus()
    session, runner = fired_cue(bus, boards=(1, 2))   # cleared tables
    assert wait_until(lambda: session.verify is not None)
    assert [f.dest for f in bus.asked] == [1]
    fired = bus.sent_at[bus.sent.index(bus.broadcasts[0])]
    assert bus.asked_at[0] - fired < 0.30
    assert session.verify["landed"] == "deaf"
    runner.stop()


def test_a_swept_cue_whose_tables_are_unknown_is_not_checked():
    # A show burned before this unit restarted: the pictures are in the
    # slots, but nothing here knows when each board starts drawing, so
    # there is no honest instant to ask at - and asking too early would
    # cost the wall a second repaint. Said in the log rather than guessed.
    from ui.remote import FIRED, RemoteSession

    bus = FakeBus(witness={1: "idle"})
    runner = make_runner(bus, boards=[1])
    session = RemoteSession(runner)
    session.arm("c1", 4, span_s=3.0)                  # burned elsewhere
    assert wait_until(lambda: runner.live == [1])     # the probing is done
    session.fire("c1", time.monotonic() + 0.05)
    assert wait_until(lambda: session.phase == FIRED)
    assert wait_until(lambda: session.verify is not None)
    assert session.verify["landed"] == "skipped"
    assert bus.asked == [] and len(bus.broadcasts) == 1
    assert logged(runner, "cue c1 verify skipped: no sweep table known "
                          "for slot 4")
    runner.stop()


def test_the_check_gives_the_port_to_a_cue_that_comes_due():
    from ui.remote import FIRED, RemoteSession

    bus = FakeBus()
    runner = make_runner(bus, boards=[1], verify_after=0.6)
    session = RemoteSession(runner)
    session.arm("c1", 2)
    assert wait_until(lambda: runner.live == [1])
    session.fire("c1", time.monotonic() + 0.05)
    assert wait_until(lambda: session.phase == FIRED)
    # ...and a second cue lands inside the check's window.
    at = time.monotonic() + 0.15
    session.arm("c2", 3)
    session.fire("c2", at)
    # c1's window was given up - recorded as "skipped", which says
    # nothing either way and shows nothing on the tile...
    assert wait_until(lambda: (session.verify or {}).get("cue") == "c1")
    assert session.verify["landed"] == "skipped"
    assert logged(runner, "cue c1 verify skipped: cue c2 is due")
    # ...and c2, which was not held up, replaces it with its own verdict.
    assert wait_until(lambda: (session.verify or {}).get("cue") == "c2",
                      timeout=3)
    assert session.verify["landed"] == "deaf"
    assert abs(session.fired_at - at) < 0.05
    assert [f.data[0] for f in bus.broadcasts] == [2, 3]
    runner.stop()


def test_a_stop_during_the_check_ends_it_at_once():
    from ui.remote import FIRED, RemoteSession

    bus = FakeBus(witness={1: "idle"})
    runner = make_runner(bus, boards=[1], verify_after=5.0)
    session = RemoteSession(runner)
    session.arm("c1", 2)
    assert wait_until(lambda: runner.live == [1])
    session.fire("c1", time.monotonic() + 0.05)
    assert wait_until(lambda: session.phase == FIRED)
    began = time.monotonic()
    runner.stop()
    assert time.monotonic() - began < 2.0             # not the 5 s window
    assert bus.asked == [] and len(bus.broadcasts) == 1
    assert logged(runner, "cue c1 verify skipped: stopped")


def test_a_late_reply_from_another_board_is_not_taken_as_an_answer():
    # A board that was deaf when an earlier question arrived answers it
    # after its repaint; that frame lands in this question's read window.
    # Reading it as "board 1 is idle" would re-send the show for nothing.
    bus = FakeBus(witness={1: "idle"})
    bus.stray = 20                                    # the first ask gets it
    session, runner = fired_cue(bus)
    assert wait_until(lambda: session.verify is not None)
    assert session.verify["landed"] == "deaf"
    assert len(bus.broadcasts) == 1
    assert logged(runner, "cue c1 stray reply from 0x14 ignored")
    runner.stop()


def test_the_check_can_be_switched_off():
    bus = FakeBus(witness={1: "idle"})
    session, runner = fired_cue(bus, verify_fire=False)
    time.sleep(0.2)
    assert bus.asked == [] and len(bus.broadcasts) == 1
    assert session.verify is None
    runner.stop()


def test_the_default_deaf_window_clears_the_measured_onset():
    # The 2026-08-14 run has the slower of the two boards still answering
    # at +1.2 s. A default below that would ask a board that has taken
    # the frame but not yet gone deaf - read as "idle", re-sent, two
    # repaints. Lower it only with a fresh measurement per board type.
    from ui.runner import VERIFY_AFTER_S, VERIFY_READ_S, VERIFY_TRIES

    assert VERIFY_AFTER_S >= 1.2 + 0.3
    assert VERIFY_READ_S * VERIFY_TRIES <= 0.7     # the whole ask window


def test_the_witness_is_asked_twice_before_silence_is_believed():
    # The link this check exists for drops frames outbound too: a
    # question that never arrived looks exactly like a board deaf from
    # repainting, and that reading reports a LOST cue as landed.
    bus = FakeBus()
    session, runner = fired_cue(bus)
    assert wait_until(lambda: session.verify is not None)
    assert bus.asked_tries == [2]
    runner.stop()


class _Armed:
    """A session holding one armed cue, for the repair arithmetic."""

    def __init__(self, at, span_s=None, refresh_s=None):
        self.span_s, self.refresh_s, self._at = span_s, refresh_s, at

    def due(self):
        return ("cB", self._at, 3, 3)


def test_the_repair_goes_out_when_the_next_cue_loses_at_most_two_seconds():
    # The rule is a lateness budget, not "one repaint clear". A board
    # queues what arrives mid-repaint and runs it after, so a repair
    # still drawing when the next trigger comes does not repaint this
    # slot twice - it makes the NEXT cue that late. Two seconds of that
    # is worth the right picture.
    #
    # Cues nine seconds apart, 7 s refresh: a 1 s sweep found lost at
    # T+2.5 finishes at T+10.5, which is 1.5 s into the next cue - it
    # goes out. A 7 s sweep is only found lost at T+8.5 and would finish
    # at T+22.5, 11.5 s late - it does not.
    runner = make_runner(FakeBus(), boards=[1])
    now = time.monotonic()
    quick = _Armed(now + 6.5)                    # T+9, seen from T+2.5
    slow = _Armed(now + 0.5)                     # T+9, seen from T+8.5
    assert runner._repair_blocked(quick, 1.0, 7.0) is None
    assert "cue cB due in 0.5 s" in runner._repair_blocked(slow, 7.0, 7.0)
    # ...and a cue whose trigger is already past always wins the port.
    assert runner._repair_blocked(_Armed(now - 0.1), 0.0, 7.0) == \
        "cue cB is due"


def test_a_loss_that_is_not_repaired_is_still_reported_red():
    # The one thing that must never happen: the unit KNOWS the cue did
    # not land, decides against repairing it because the next cue is too
    # close, and the tile shows nothing at all.
    from ui.remote import RemoteSession

    bus = FakeBus(witness={1: "idle"})
    runner = make_runner(bus, boards=[1])
    session = RemoteSession(runner)
    # Armed inside the question's own window, close enough that a repair
    # (7 s of flat refresh) would eat it.
    bus.on_ask = lambda: (session.arm("c2", 3),
                          session.fire("c2", time.monotonic() + 4.5))
    session.arm("c1", 2)
    assert wait_until(lambda: runner.live == [1])
    session.fire("c1", time.monotonic() + 0.05)
    assert wait_until(lambda: len(bus.asked) == 1)
    bus.on_ask = None
    assert wait_until(lambda: session.verify is not None)
    assert session.verify == {"cue": "c1", "landed": "idle-not-repaired",
                              "resent": False, "witness": 1}
    assert logged(runner, "cue c1 not applied at @01 (checked +")
    assert logged(runner, "not re-sent: cue c2 due in 4.")
    assert len(bus.broadcasts) == 1              # c1 only, never repaired
    runner.stop()


def test_a_cue_far_enough_off_does_not_cancel_the_re_send():
    # ...and the other way round: a cue armed a minute out is not a
    # reason to leave the garment on the wrong picture until then.
    from ui.remote import RemoteSession

    bus = FakeBus(witness={1: ["idle", "deaf"]})
    runner = make_runner(bus, boards=[1])
    session = RemoteSession(runner)
    bus.on_ask = lambda: (session.arm("c2", 3),
                          session.fire("c2", time.monotonic() + 60))
    session.arm("c1", 2)
    assert wait_until(lambda: runner.live == [1])
    session.fire("c1", time.monotonic() + 0.05)
    assert wait_until(lambda: len(bus.asked) == 1)
    bus.on_ask = None
    assert wait_until(lambda: len(bus.broadcasts) == 2)
    assert [f.data[0] for f in bus.broadcasts] == [2, 2]   # c1 repaired
    assert logged(runner, "cue c1 not applied at @01")
    runner.stop()


def test_the_question_is_not_begun_right_before_the_next_trigger():
    # The ask holds the port for verify_read x VERIFY_TRIES; started
    # just before a trigger it would make that cue late by the whole of
    # it. Not started at all, then.
    from ui.remote import RemoteSession

    bus = FakeBus()
    runner = make_runner(bus, boards=[1], verify_after=0.5, verify_read=0.3)
    session = RemoteSession(runner)
    session.arm("c1", 2)
    assert wait_until(lambda: runner.live == [1])
    at = time.monotonic() + 0.05
    session.fire("c1", at)
    assert wait_until(lambda: len(bus.broadcasts) == 1)
    nxt = at + 0.5 + 0.4                 # 0.4 s after the question's instant
    session.arm("c2", 3)
    session.fire("c2", nxt)
    assert wait_until(lambda: len(bus.broadcasts) == 2, timeout=3)
    assert bus.asked == []               # never begun
    assert logged(runner, "cue c1 verify skipped: cue c2 is due")
    assert abs(session.fired_at - nxt) < 0.05      # ...and c2 was on time
    runner.stop()


def test_a_stop_while_the_question_is_in_flight_stops_the_re_send():
    # KEY2 / /show/stop during the 0.6 s the question takes. Nothing may
    # go on the glass after that.
    from ui.remote import RemoteSession

    bus = FakeBus(witness={1: "idle"})
    runner = make_runner(bus, boards=[1])
    session = RemoteSession(runner)
    # The stop flag is what the worker sees first; runner.stop() itself
    # cannot be called from inside the worker's own thread.
    bus.on_ask = lambda: runner._stop.set()
    session.arm("c1", 2)
    assert wait_until(lambda: runner.live == [1])
    session.fire("c1", time.monotonic() + 0.05)
    assert wait_until(lambda: not runner.running)
    assert len(bus.broadcasts) == 1
    # The loss was found before the stop, so it is still reported as one -
    # the garment is wrong whether or not anyone is still driving it.
    assert logged(runner, "cue c1 not applied at @01 (checked +")
    assert logged(runner, "not re-sent: stopped")
    assert session.verify["landed"] == "idle-not-repaired"
    runner.stop()


def test_a_sweep_start_beyond_the_cues_own_span_is_not_waited_for():
    # The table is 16-bit frames: one corrupt entry reads as 655 s, and
    # the check would sit on the port for eleven minutes with no reprobe
    # and no guard STOP behind it.
    bus = FakeBus(witness={1: "idle"})
    session, runner = fired_cue(bus, boards=(1,), span_s=3.0,
                                delays={1: sweep_table(65534)})
    assert wait_until(lambda: session.verify is not None)
    assert session.verify["landed"] == "skipped"
    assert bus.asked == [] and len(bus.broadcasts) == 1
    assert logged(runner, "cue c1 verify skipped: sweep start 655.3 s "
                          "too late to check")
    runner.stop()


def test_a_sweep_start_beyond_the_ceiling_is_not_waited_for_either():
    # No span given (an older conductor's /prepare): the ceiling is
    # VERIFY_MAX_DELAY_S rather than the cue's own word for it.
    from ui.runner import VERIFY_MAX_DELAY_S

    bus = FakeBus(witness={1: "idle"})
    session, runner = fired_cue(bus, boards=(1,),
                                delays={1: sweep_table(4000)})   # 40 s
    assert wait_until(lambda: session.verify is not None)
    assert VERIFY_MAX_DELAY_S < 40.0
    assert session.verify["landed"] == "skipped"
    assert logged(runner, "cue c1 verify skipped: sweep start 40.0 s")
    runner.stop()


def test_a_healed_cue_is_not_checked_a_second_time():
    # ui/showplay.py re-arms the SAME cue id to heal a board that joined
    # late. That cue was already confirmed on the glass, and the healing
    # broadcast is for a board that was not even there to be asked
    # about: checking again would only spend port time and risk a
    # re-send nothing needs. So a heal adds exactly one broadcast.
    from ui.remote import FIRED

    bus = FakeBus()
    session, runner = fired_cue(bus)
    assert wait_until(lambda: session.verify is not None)
    assert session.verify["landed"] == "deaf"
    session.arm("c1", 19)                             # the heal: same cue id
    assert session.verify["landed"] == "deaf"         # ...keeps the verdict
    session.fire("c1", time.monotonic() + 0.05)
    assert wait_until(lambda: len(bus.broadcasts) == 2)
    assert wait_until(lambda: logged(runner, "cue c1 landed already, "
                                             "not checked again"))
    time.sleep(0.2)
    assert len(bus.asked) == 1 and len(bus.broadcasts) == 2
    runner.stop()


def test_a_new_fire_of_the_same_cue_id_is_always_checked():
    # A one-cue show stopped and started again fires the very same cue
    # id, and a looping demo comes round to its ids for ever. Those are
    # new fires onto boards nobody is vouching for any more - not heals -
    # so the "landed already" shortcut must not swallow them. What tells
    # them apart is the session's glass generation, not the cue id.
    bus = FakeBus()
    session, runner = fired_cue(bus)
    assert wait_until(lambda: session.verify is not None)
    assert len(bus.asked) == 1
    # The generation moves on (what run(), stop() and release() do),
    # while the verdict itself is still on the tile...
    session.glass_gen += 1
    session.arm("c1", 19)
    session.fire("c1", time.monotonic() + 0.05)
    assert wait_until(lambda: len(bus.broadcasts) == 2)
    assert wait_until(lambda: len(bus.asked) == 2)     # ...and it is checked
    assert not logged(runner, "landed already")
    runner.stop()


def test_a_verdict_from_before_a_release_is_forgotten():
    bus = FakeBus()
    session, runner = fired_cue(bus)
    assert wait_until(lambda: session.verify is not None)
    session.release()
    assert session.verify is None and session.status()["verify"] is None
    runner.stop()


def test_the_pattern_loop_is_left_alone():
    # Only a show's cues are checked: the demo loop repaints every
    # interval anyway, and a question after every cycle would be noise.
    bus = FakeBus(witness={1: "idle"})
    runner = make_runner(bus, boards=[1, 2])
    runner.start(BY_KEY["wave"])
    assert wait_until(lambda: runner.cycle >= 2)
    runner.stop()
    assert bus.asked == []


# ---- a show's board list is THE list: nothing outside it is touched ----
#
# radxa-04 (tops, 16 boards), 2026-09-26 rehearsals: it came up in
# STANDBY, its discovery logged "board 17-22 absent, skipping" (the
# explore looks EXPLORE_GAP past the last live board) - and those six
# stayed in `absent` after the show's own list of 1-16 arrived. The
# reprobe then spent ~15 s of every minute on six serial timeouts to
# sockets the show knew were empty, and the cues whose trigger fell
# inside one went out +492 / +246 / +73 ms late. radxa-05, which started
# straight into REMOTE with the job's list, had an empty `absent`, no
# probing and fires +1 ms.

SHOW_BOARDS = list(range(1, 17))        # the tops garment


def explored_unit(bus, **kwargs):
    """A unit that came up on its own and explored the bus, as radxa-04
    did before the show PC ever spoke to it."""
    kwargs.setdefault("verify_fire", False)
    runner = make_runner(bus, boards=None, **kwargs)
    runner.start(BY_KEY["solid"])
    assert wait_until(lambda: "panels online" in " ".join(runner.log),
                      timeout=20)
    runner.stop()
    return runner


def show_cue(session, runner, boards=SHOW_BOARDS, cue_id="c1"):
    """The show PC's job, which carries the garment's board list."""
    from ui.remote import READY

    session.prepare(cue_id, {b: cue_array(b) for b in boards})
    assert wait_until(lambda: session.phase == READY, timeout=20)


def fire_once(session, cue_id: str, slot: int = 2, ahead: float = 0.2):
    """Arm a cue already burned into `slot` and fire it; how late it was."""
    from ui.remote import FIRED

    at = time.monotonic() + ahead
    session.arm(cue_id, slot)
    session.fire(cue_id, at)
    assert wait_until(lambda: session.phase == FIRED, timeout=10)
    return session.fired_at - at


def test_a_shows_board_list_drops_the_boards_outside_it():
    from ui.remote import RemoteSession

    bus = Wall(set(SHOW_BOARDS))
    runner = explored_unit(bus)
    assert runner.absent == set(range(17, 23))       # six empty sockets
    assert logged(runner, "board 17-22 absent, skipping")

    session = RemoteSession(runner)
    show_cue(session, runner)
    assert runner.boards == SHOW_BOARDS and runner.explore is False
    assert runner.absent == set() and runner.live == SHOW_BOARDS
    assert logged(runner,
                  "boards 1-16 from the show (17-22 dropped, not probed)")

    # ...and nothing reaches a dropped socket for the rest of the show:
    # several reprobe intervals (0.05 s here, 60 s on the unit) of it,
    # with cues fired in the middle.
    bus.sent.clear()
    bus.requested.clear()
    late = [fire_once(session, "c2"), fire_once(session, "c3")]
    time.sleep(4 * runner.reprobe_interval)
    touched = {f.dest for f in bus.sent + bus.requested}
    assert touched <= set(SHOW_BOARDS) | {0xFF}
    assert all(0 <= slip < 0.05 for slip in late)    # +1 ms on radxa-05
    session.release()


def test_a_listed_board_that_was_silent_still_joins_the_show():
    # The other half of the rule: a board powered on late is on the
    # show's own list, so it keeps its chance to come back.
    from ui.remote import RemoteSession

    bus = Wall(set(SHOW_BOARDS) - {5})
    runner = explored_unit(bus)
    assert runner.absent == {5} | set(range(17, 23))

    session = RemoteSession(runner)
    show_cue(session, runner)
    assert runner.absent == {5}                      # the list's own, only
    assert logged(runner,
                  "boards 1-16 from the show (17-22 dropped, not probed)")
    bus.present.add(5)                               # its feed came back on
    assert wait_until(lambda: 5 in runner.live, timeout=10)
    assert runner.absent == set() and logged(runner, "board 5 joined")
    session.release()


def test_a_board_outside_the_shows_list_is_ignored_even_if_it_answers():
    from ui.remote import RemoteSession

    bus = Wall(set(SHOW_BOARDS))
    runner = explored_unit(bus)
    session = RemoteSession(runner)
    show_cue(session, runner)

    bus.present.add(17)              # a board on a socket the show omits
    bus.sent.clear()
    bus.requested.clear()
    time.sleep(4 * runner.reprobe_interval)
    assert 17 not in runner.boards and 17 not in runner.live
    assert 17 not in runner.absent
    assert not [f for f in bus.sent + bus.requested if f.dest == 17]
    assert runner.expected == 16 and runner.reported_boards == SHOW_BOARDS
    session.release()


def test_releasing_the_show_gives_the_unit_its_own_discovery_back():
    from ui.remote import RemoteSession

    bus = Wall(set(SHOW_BOARDS))
    runner = explored_unit(bus)
    session = RemoteSession(runner)
    show_cue(session, runner)
    assert runner.explore is False and runner.boards == SHOW_BOARDS

    session.release()                       # KEY2 / the PC hands it back
    seen = sum("panels online" in line for line in runner.log)
    bus.requested.clear()
    runner.start(BY_KEY["solid"])           # the unit's own menu again
    assert wait_until(lambda: sum("panels online" in line
                                  for line in runner.log) > seen, timeout=20)
    assert logged(runner, "the show's board list is released, exploring again")
    assert runner.explore is True
    assert runner.absent == set(range(17, 23))       # discovery as before
    assert max(f.dest for f in bus.requested if f.dest != 0xFF) == 22
    runner.stop()


def test_an_explicit_boards_list_is_kept_across_a_show():
    # A unit started with --boards was never exploring; the show's list
    # rules while the show runs, and nothing turns discovery on after it.
    from ui.remote import RemoteSession

    bus = Wall({1, 2, 3})
    runner = make_runner(bus, boards=[1, 2, 3], verify_fire=False)
    session = RemoteSession(runner)
    show_cue(session, runner, boards=[1, 2], cue_id="c1")
    assert runner.boards == [1, 2] and runner.explore is False
    session.release()
    runner.start(BY_KEY["solid"])
    assert wait_until(lambda: "panels online" in " ".join(runner.log),
                      timeout=10)
    assert runner.explore is False           # never explored, never will
    runner.stop()


def test_one_port_session_never_changes_the_group_count_under_it():
    # The group count is a header byte on every frame, and the boards
    # are configured with it (0x1B). An exploring setup TRIMS the list
    # as it probes (1..60 down to 1..22), so a group count that followed
    # the list would leave the boards configured in the first sweep
    # disagreeing with every frame sent after them - and a board that
    # joined at a later reprobe configured differently from its
    # neighbours (review, 2026-09-27). Only a list that is REPLACED - a
    # show's - moves it, and that comes with a fresh setup.
    bus = Wall(set(SHOW_BOARDS))
    runner = make_runner(bus, boards=None, verify_fire=False)
    runner.start(BY_KEY["solid"])
    assert wait_until(lambda: runner.cycle >= 2, timeout=20)
    assert runner.boards == list(range(1, 23))        # trimmed while probing
    runner.stop()
    assert {f.group_count for f in bus.sent + bus.requested} == {60}
    assert {f.group_count for f in bus.requested if f.cmd == 0x1B} == {60}


def test_the_group_count_follows_the_shows_board_list():
    from ui.remote import RemoteSession

    bus = Wall(set(SHOW_BOARDS))
    runner = explored_unit(bus)
    session = RemoteSession(runner)
    show_cue(session, runner)

    bus.sent.clear()
    bus.requested.clear()
    fire_once(session, "c2")
    counted = [f for f in bus.sent + bus.requested
               if f.cmd in (0x1D, 0x17)]                 # show single, stop
    assert counted and all(f.group_count == 16 for f in counted)

    # A list with a gap counts by its highest address, not its length.
    show_cue(session, runner, boards=[1, 2, 20], cue_id="c3")
    assert runner.boards == [1, 2, 20]
    bus.sent.clear()
    bus.requested.clear()
    fire_once(session, "c4")
    counted = [f for f in bus.sent + bus.requested if f.cmd in (0x1D, 0x17)]
    assert counted and all(f.group_count == 20 for f in counted)
    session.release()
