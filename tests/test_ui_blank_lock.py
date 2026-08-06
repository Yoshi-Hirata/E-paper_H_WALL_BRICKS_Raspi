"""Backlight auto-blank and the show lock.

Both exist so the device can be left alone: the screen should not burn
power all night, and a knock during a show must not stop the demo.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ui.app import App, Screen
from ui.config import UNLOCK_SEQUENCE
from ui.display import NullDisplay
from ui.inputs import ScriptedInput
from ui.patterns import PATTERNS


class FakeRunner:
    def __init__(self):
        self.pattern = None
        self.cycle = 0
        self.elapsed = 0.0
        self.error = None
        self.running = False
        self.starts = []
        self.stops = 0

    def start(self, pattern):
        self.pattern = pattern
        self.running = True
        self.starts.append(pattern.key)

    def stop(self, timeout=5.0):
        self.running = False
        self.stops += 1

    def recent(self, count):
        return ["09:00:00 start"]


class Clock:
    """Hand-cranked clock so idle timeouts are exact, not slept through."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def make_app(events=(), **kwargs):
    clock = Clock()
    runner = FakeRunner()
    app = App(NullDisplay(), ScriptedInput(events), runner,
              port_label="/dev/ttyACM0", clock=clock, **kwargs)
    return app, runner, clock


# ---- auto-blank ----

def test_screen_blanks_after_the_idle_timeout():
    app, _, clock = make_app(blank_after=10.0)
    app.tick(wait=0)
    assert not app.blanked
    clock.advance(9.9)
    app.tick(wait=0)
    assert not app.blanked          # not yet
    clock.advance(0.2)
    app.tick(wait=0)
    assert app.blanked
    assert app.display.asleep


def test_input_postpones_blanking():
    app, _, clock = make_app(blank_after=10.0)
    clock.advance(9.0)
    app.handle("down")              # a press resets the idle timer
    clock.advance(9.0)
    app.tick(wait=0)
    assert not app.blanked


def test_waking_press_changes_nothing_else():
    # The whole point: in a dark room you press something to see the
    # screen, and that must not start, stop or re-select anything.
    app, runner, clock = make_app(blank_after=10.0)
    app.handle("key1")              # demo running, menu -> running
    before = (app.screen, app.selected, list(runner.starts), runner.stops)
    clock.advance(11)
    app.tick(wait=0)
    assert app.blanked

    app.handle("key1")              # blind press: wakes only
    assert not app.blanked
    assert not app.display.asleep
    assert (app.screen, app.selected, list(runner.starts), runner.stops) == before


def test_every_button_wakes_the_screen():
    for event in ("up", "down", "left", "right", "press", "key1", "key2", "key3"):
        app, _, clock = make_app(blank_after=10.0)
        clock.advance(11)
        app.tick(wait=0)
        assert app.blanked
        app.handle(event)
        assert not app.blanked, f"{event} did not wake the screen"


def test_blanking_can_be_disabled():
    app, _, clock = make_app(blank_after=0)
    clock.advance(3600)
    app.tick(wait=0)
    assert not app.blanked


def test_blanking_is_off_unless_asked_for():
    # The default has to stay off: a dark screen reads as a dead device,
    # and an operator glancing at the panel state should not have to
    # touch anything. Battery runs opt in with --blank-after.
    from ui.config import BLANK_AFTER_S

    assert BLANK_AFTER_S == 0
    app, _, clock = make_app()          # no blank_after given
    clock.advance(3600)
    app.tick(wait=0)
    assert not app.blanked
    # KEY3 still blanks on demand.
    app.handle("key3")
    assert app.blanked


def test_demo_keeps_running_while_blanked():
    app, runner, clock = make_app(blank_after=10.0)
    app.handle("key1")
    clock.advance(11)
    app.tick(wait=0)
    assert app.blanked
    assert runner.running           # only the backlight sleeps


# ---- show lock ----

def test_locked_buttons_do_nothing():
    app, runner, _ = make_app(locked=True)
    for event in ("down", "key1", "press", "key2"):
        app.handle(event)
    assert app.selected == 0
    assert runner.starts == []
    assert app.screen is Screen.MENU


def test_unlock_sequence_frees_the_buttons():
    app, runner, _ = make_app(locked=True)
    for event in UNLOCK_SEQUENCE:
        app.handle(event)
    assert not app.locked
    app.handle("key1")
    assert runner.starts == [PATTERNS[0].key]


def test_wrong_sequence_does_not_unlock():
    app, _, _ = make_app(locked=True)
    for event in ("key2", "key1", "key2"):
        app.handle(event)
    assert app.locked


def test_unlock_sequence_expires():
    app, _, clock = make_app(locked=True)
    app.handle(UNLOCK_SEQUENCE[0])
    clock.advance(10)               # too slow
    for event in UNLOCK_SEQUENCE[1:]:
        app.handle(event)
    assert app.locked


def test_lock_returns_on_its_own():
    # An operator who unlocks mid-show must not have to remember to re-arm it.
    app, _, clock = make_app(locked=True, relock_after=60.0, blank_after=0)
    for event in UNLOCK_SEQUENCE:
        app.handle(event)
    assert not app.locked
    clock.advance(61)
    app.tick(wait=0)
    assert app.locked


def test_a_device_that_started_unlocked_never_locks_itself():
    app, _, clock = make_app(locked=False, relock_after=60.0, blank_after=0)
    clock.advance(600)
    app.tick(wait=0)
    assert not app.locked


def test_locked_screen_still_wakes():
    # Seeing the status must not require unlocking first.
    app, _, clock = make_app(locked=True, blank_after=10.0)
    clock.advance(11)
    app.tick(wait=0)
    assert app.blanked
    app.handle("key1")
    assert not app.blanked
    assert app.locked               # ...but it stays locked
