import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

from epaper.protocol import Frame
from ui.patterns import BY_KEY
from ui.runner import DemoRunner


class FakeBus:
    """Records frames and ACKs everything (optionally failing on a cmd)."""

    def __init__(self, nak_on: int | None = None, ack_cmd: int = 0x80):
        self.sent: list[Frame] = []
        self.requested: list[Frame] = []
        self.nak_on = nak_on
        self.ack_cmd = ack_cmd
        self.closed = False

    def send(self, frame):
        self.sent.append(frame)

    def request(self, frame, retries=3):
        self.requested.append(frame)
        if self.nak_on is not None and frame.cmd == self.nak_on:
            return None
        # Real boards answer with DevType 0xFF (docs/SPECIFICATION.md 5.3).
        return Frame(dest=0x00, src=frame.dest, dev_type=0xFF,
                     cmd=self.ack_cmd)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def make_runner(bus, **kwargs):
    kwargs.setdefault("interval", 0.05)
    kwargs.setdefault("guard_delay", 0.0)
    kwargs.setdefault("port", "/dev/fake")
    return DemoRunner(open_bus=lambda port: bus, **kwargs)


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


def test_missing_ack_aborts_and_is_reported():
    bus = FakeBus(nak_on=0x13)          # color save never answers
    runner = make_runner(bus)
    runner.start(BY_KEY["wave"])
    assert wait_until(lambda: runner.error is not None)
    assert "no ACK" in runner.error
    assert any("ERROR" in line for line in runner.recent(10))
    assert wait_until(lambda: not runner.running)


def test_nak_response_aborts_too():
    bus = FakeBus(ack_cmd=0x81)         # ACK_FAIL
    runner = make_runner(bus)
    runner.start(BY_KEY["wave"])
    assert wait_until(lambda: runner.error is not None)
    assert "NAK" in runner.error


def test_missing_port_is_reported_not_raised(monkeypatch):
    import ui.runner as runner_module

    monkeypatch.setattr(runner_module, "find_port", lambda: None)
    runner = DemoRunner(open_bus=lambda port: FakeBus(), port=None)
    runner.pattern = BY_KEY["wave"]
    runner._run()
    assert runner.error == "no serial port"


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
    runner.start(BY_KEY["solid"])
    assert wait_until(lambda: runner.cycle >= 2)
    runner.stop()
    broadcast_stops = [f for f in bus.sent if f.cmd == 0x17 and f.dest == 0xFF]
    assert len(broadcast_stops) >= 2   # startup silence + at least one guard


def test_log_keeps_newest_lines_only():
    runner = DemoRunner(open_bus=lambda port: FakeBus(), port="/dev/fake")
    for i in range(500):
        runner.emit(f"line {i}")
    lines = runner.recent(3)
    assert len(lines) == 3
    assert lines[-1].endswith("line 499")
