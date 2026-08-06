"""KEY1: start, pause, resume - and hold to reset.

Pause has to keep both the cycle count and the timer, otherwise it is
just a stop; reset has to clear both, otherwise it is just a resume.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

from epaper.protocol import Frame
from ui.app import App, Screen
from ui.display import NullDisplay
from ui.inputs import ScriptedInput
from ui.patterns import BY_KEY, PATTERNS
from ui.runner import DemoRunner


class FakeBus:
    def __init__(self):
        self.sent, self.requested = [], []

    def send(self, frame):
        self.sent.append(frame)

    def request(self, frame, retries=3):
        self.requested.append(frame)
        return Frame(dest=0, src=frame.dest, dev_type=0xFF, cmd=0x80)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def make_runner(bus):
    return DemoRunner(open_bus=lambda port: bus, port="/dev/fake",
                      interval=0.05, guard_delay=0.0, echo_log=False,
                      show_gap=0.001)


# ---- runner level ----

def test_pause_freezes_the_timer_and_keeps_the_cycle_count():
    runner = make_runner(FakeBus())
    runner.start(BY_KEY["wave"])
    assert wait_until(lambda: runner.cycle >= 2)
    runner.pause()
    cycle_at_pause, elapsed_at_pause = runner.cycle, runner.elapsed
    time.sleep(0.3)
    assert runner.cycle == cycle_at_pause          # no new cycles
    assert runner.elapsed == elapsed_at_pause      # timer frozen
    assert runner.paused and runner.running        # thread still alive
    runner.stop()


def test_resume_carries_on_from_where_it_paused():
    runner = make_runner(FakeBus())
    runner.start(BY_KEY["wave"])
    assert wait_until(lambda: runner.cycle >= 2)
    runner.pause()
    resumed_from = runner.cycle
    elapsed_at_pause = runner.elapsed
    time.sleep(0.2)
    runner.resume()
    assert wait_until(lambda: runner.cycle > resumed_from)
    assert not runner.paused
    assert runner.elapsed >= elapsed_at_pause      # continues, not restarts
    # The paused stretch must not be counted into the elapsed time.
    assert runner.elapsed < elapsed_at_pause + 0.2
    runner.stop()


def test_restart_resets_both_counters():
    runner = make_runner(FakeBus())
    runner.start(BY_KEY["wave"])
    assert wait_until(lambda: runner.cycle >= 2)
    runner.start(BY_KEY["wave"])                   # reset
    assert runner.cycle == 0
    assert runner.elapsed < 0.5
    runner.stop()


def test_stopping_a_paused_runner_works():
    runner = make_runner(FakeBus())
    runner.start(BY_KEY["wave"])
    assert wait_until(lambda: runner.cycle >= 1)
    runner.pause()
    runner.stop()
    assert not runner.running
    assert not runner.paused


# ---- app level ----

class FakeRunner:
    def __init__(self):
        self.pattern = None
        self.cycle = 0
        self.elapsed = 0.0
        self.error = None
        self.running = False
        self.paused = False
        self.starts, self.pauses, self.resumes, self.stops = [], 0, 0, 0

    def start(self, pattern):
        self.pattern = pattern
        self.running = True
        self.paused = False
        self.starts.append(pattern.key)

    def pause(self):
        self.paused = True
        self.pauses += 1

    def resume(self):
        self.paused = False
        self.resumes += 1

    def stop(self, timeout=5.0):
        self.running = False
        self.paused = False
        self.stops += 1

    def recent(self, count):
        return []


def make_app():
    runner = FakeRunner()
    return App(NullDisplay(), ScriptedInput(), runner, blank_after=0), runner


def test_key1_cycles_start_pause_resume():
    app, runner = make_app()
    app.handle("key1")                 # start
    assert app.screen is Screen.RUNNING
    assert runner.starts == [PATTERNS[0].key]
    app.handle("key1")                 # pause
    assert runner.pauses == 1 and runner.paused
    app.handle("key1")                 # resume
    assert runner.resumes == 1 and not runner.paused
    assert runner.starts == [PATTERNS[0].key]      # never restarted


def test_key1_hold_resets_from_a_running_demo():
    app, runner = make_app()
    app.handle("key1")
    runner.cycle = 42
    app.handle("key1_hold")
    assert len(runner.starts) == 2     # a fresh start, i.e. from zero
    assert not runner.paused


def test_key1_hold_resets_from_a_paused_demo():
    app, runner = make_app()
    app.handle("key1")
    app.handle("key1")                 # paused
    app.handle("key1_hold")
    assert not runner.paused
    assert len(runner.starts) == 2


def test_key1_hold_starts_from_the_menu_too():
    app, runner = make_app()
    app.handle("key1_hold")
    assert app.screen is Screen.RUNNING
    assert runner.starts == [PATTERNS[0].key]


def test_hold_while_blanked_only_wakes():
    app, runner = make_app()
    app.handle("key1")
    app.blanked = True
    app.display.sleep()
    app.handle("key1_hold")            # blind hold in the dark
    assert not app.blanked
    assert len(runner.starts) == 1     # nothing was reset
