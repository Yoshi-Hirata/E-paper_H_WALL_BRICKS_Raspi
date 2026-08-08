"""Fault injection: the demo must never be left stopped.

This is the regression net for show operation. Every test here asserts
the same underlying property - whatever the bus does, the runner keeps
running and keeps trying - because the failure that matters is a frozen
panel waiting for a human.
"""

from __future__ import annotations

import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

import pytest
from serial import SerialException

from epaper.protocol import Frame
from ui.patterns import BY_KEY
from ui.runner import DemoRunner

CMD_STOP, CMD_SAVE, CMD_SHOW, CMD_SLOT = 0x17, 0x13, 0x1D, 0x1B


class FaultBus:
    """Fake transport with injectable faults.

    fail_first  - first N request() calls answer nothing
    fail_cmds   - these commands always go unanswered
    nak_cmds    - these answer with a failure code
    busy_first  - first N calls answer ACK_BUSY
    raise_at    - raise SerialException on the Nth call (simulates unplug)
    fail_rate   - random unanswered fraction (needs `rng`)
    """

    def __init__(self, fail_first=0, fail_cmds=(), nak_cmds=(), busy_first=0,
                 raise_at=None, fail_rate=0.0, rng=None, fail_cmd_times=None):
        self.fail_first = fail_first
        self.fail_cmds = set(fail_cmds)
        # {cmd: how many times it fails before it starts working}
        self.fail_cmd_times = dict(fail_cmd_times or {})
        self.nak_cmds = set(nak_cmds)
        self.busy_first = busy_first
        self.raise_at = raise_at
        self.fail_rate = fail_rate
        self.rng = rng or random.Random(0)
        self.sent: list[Frame] = []
        self.requested: list[Frame] = []
        self.calls = 0
        self.closed = False

    def _maybe_raise(self):
        self.calls += 1
        if self.raise_at is not None and self.calls == self.raise_at:
            raise SerialException("device disconnected")

    def send(self, frame):
        self._maybe_raise()
        self.sent.append(frame)

    def request(self, frame, retries=3):
        self._maybe_raise()
        self.requested.append(frame)
        if self.fail_first > 0:
            self.fail_first -= 1
            return None
        if self.busy_first > 0:
            self.busy_first -= 1
            return Frame(dest=0, src=frame.dest, dev_type=0xFF, cmd=0x82)
        if frame.cmd in self.fail_cmds:
            return None
        if self.fail_cmd_times.get(frame.cmd, 0) > 0:
            self.fail_cmd_times[frame.cmd] -= 1
            return None
        if self.fail_rate and self.rng.random() < self.fail_rate:
            return None
        cmd = 0x81 if frame.cmd in self.nak_cmds else 0x80
        return Frame(dest=0, src=frame.dest, dev_type=0xFF, cmd=cmd)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True


def wait_until(predicate, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def make_runner(bus_or_factory, **kwargs):
    """Runner with retry delays compressed so tests stay fast."""
    kwargs.setdefault("interval", 0.05)
    kwargs.setdefault("guard_delay", 0.0)
    kwargs.setdefault("port", "/dev/fake")
    kwargs.setdefault("echo_log", False)
    kwargs.setdefault("retry_delays", (0.01, 0.02))
    kwargs.setdefault("busy_delay", 0.01)
    kwargs.setdefault("reopen_delay", 0.01)
    kwargs.setdefault("port_wait", 0.01)
    kwargs.setdefault("show_gap", 0.001)
    factory = (bus_or_factory if callable(bus_or_factory)
               else (lambda port: bus_or_factory))
    return DemoRunner(open_bus=factory, **kwargs)


# ---- the core property ----

def test_transient_failures_do_not_stop_the_demo():
    bus = FaultBus(fail_first=4)          # first few commands go unanswered
    runner = make_runner(bus)
    runner.start(BY_KEY["wave"])
    assert wait_until(lambda: runner.cycle >= 2)
    assert runner.running
    runner.stop()


def test_permanent_failure_keeps_retrying_instead_of_giving_up():
    bus = FaultBus(fail_cmds=[CMD_STOP])  # nothing will ever succeed
    runner = make_runner(bus, command_attempts=2)
    runner.start(BY_KEY["wave"])
    assert wait_until(lambda: runner.error is not None)
    # The demo must still be alive and still trying after failing.
    before = len(bus.requested)
    assert wait_until(lambda: len(bus.requested) > before)
    assert runner.running
    runner.stop()


def test_serial_exception_reopens_the_bus():
    opened = []

    def factory(port):
        bus = FaultBus(raise_at=6 if not opened else None)
        opened.append(bus)
        return bus

    runner = make_runner(factory)
    runner.start(BY_KEY["wave"])
    assert wait_until(lambda: len(opened) >= 2)   # reopened after the unplug
    assert wait_until(lambda: runner.cycle >= 1)
    assert runner.running
    runner.stop()


def test_missing_port_waits_instead_of_raising(monkeypatch):
    import ui.runner as runner_module

    monkeypatch.setattr(runner_module, "find_port", lambda: None)
    runner = make_runner(FaultBus(), port=None)
    runner.start(BY_KEY["wave"])
    assert wait_until(lambda: runner.error is not None)
    assert runner.running                          # still waiting, not dead
    runner.stop()


def test_port_appearing_later_is_picked_up(monkeypatch):
    import ui.runner as runner_module

    state = {"port": None}
    monkeypatch.setattr(runner_module, "find_port", lambda: state["port"])
    runner = make_runner(FaultBus(), port=None)
    runner.start(BY_KEY["wave"])
    assert wait_until(lambda: runner.error is not None)
    state["port"] = "/dev/ttyACM0"                 # cable plugged back in
    assert wait_until(lambda: runner.cycle >= 1)
    runner.stop()


def test_chaos_run_completes_many_cycles():
    bus = FaultBus(fail_rate=0.2, rng=random.Random(7))
    runner = make_runner(bus)
    # "wave" sets no interval of its own, so the compressed test interval
    # applies (SOLID would pace itself at its real 15 s).
    runner.start(BY_KEY["wave"])
    assert wait_until(lambda: runner.cycle >= 20, timeout=20)
    assert runner.running
    runner.stop()


# ---- retry mechanics ----

def test_busy_response_is_retried():
    bus = FaultBus(busy_first=3)
    runner = make_runner(bus)
    runner.start(BY_KEY["wave"])
    assert wait_until(lambda: runner.cycle >= 1)
    runner.stop()


def test_nak_is_retried_not_fatal():
    bus = FaultBus(nak_cmds=[CMD_SAVE])
    runner = make_runner(bus, save_attempts=2)
    runner.start(BY_KEY["wave"])
    assert wait_until(lambda: runner.error is not None)
    assert runner.running
    runner.stop()


def test_colour_save_is_retried_less_than_other_commands():
    # Every save retry burns a flash write, so it must not retry as hard.
    runner = make_runner(FaultBus(), command_attempts=8, save_attempts=3)
    assert runner.save_attempts < runner.command_attempts


def test_show_is_sent_repeatedly_because_it_is_unacknowledged():
    bus = FaultBus()
    runner = make_runner(bus)
    runner.start(BY_KEY["wave"])
    assert wait_until(lambda: runner.cycle >= 1)
    runner.stop()
    shows = [f for f in bus.sent if f.cmd == CMD_SHOW]
    assert len(shows) >= 3          # broadcast show has no ACK to check
    assert all(f.dest == 0xFF for f in shows)


def test_failed_cycle_does_not_advance_the_pattern():
    # The image that failed should be retried, not skipped past.
    bus = FaultBus(fail_cmds=[CMD_SAVE])
    runner = make_runner(bus, command_attempts=2, save_attempts=1)
    runner.start(BY_KEY["solid"])
    assert wait_until(lambda: runner.error is not None)
    assert runner.cycle == 0
    runner.stop()


def test_recovered_cycle_reinitialises_the_slot():
    # A board may have rebooted while it was unreachable, so the slot
    # configuration has to be sent again before trusting it.
    bus = FaultBus(fail_cmd_times={CMD_SAVE: 2})
    runner = make_runner(bus, command_attempts=2, save_attempts=1)
    runner.start(BY_KEY["wave"])
    assert wait_until(lambda: runner.cycle >= 1, timeout=20)
    runner.stop()
    slot_cfgs = [f for f in bus.requested if f.cmd == CMD_SLOT]
    assert len(slot_cfgs) > len(runner.boards)   # re-sent after the failures
    assert runner.failures >= 1


@pytest.mark.parametrize("fault", [
    {"fail_first": 5},
    {"fail_cmds": [CMD_STOP]},
    {"nak_cmds": [CMD_SAVE]},
    {"busy_first": 4},
    {"fail_rate": 0.5},
])
def test_runner_is_never_left_stopped(fault):
    runner = make_runner(FaultBus(rng=random.Random(1), **fault),
                         command_attempts=2, save_attempts=1)
    runner.start(BY_KEY["wave"])
    for _ in range(20):
        time.sleep(0.05)
        assert runner.running, f"runner died with {fault}"
    runner.stop()
    assert not runner.running
