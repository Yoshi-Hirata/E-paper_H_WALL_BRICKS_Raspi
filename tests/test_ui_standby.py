"""Standby: the state the panels sit in when nothing is playing.

The boards ship running a factory autoplay that starts by itself at
power-on, so "idle" is not a blank wall unless something makes it one.
As soon as the host has the link, it stops that autoplay and paints
every sector white, once, and leaves it there.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

from epaper.pattern import COLOR_NAMES, VALID_TRIANGLES
from ui.app import App, Screen
from ui.display import NullDisplay
from ui.inputs import ScriptedInput
from ui.patterns import BY_KEY, STANDBY
from ui.runner import DemoRunner
from tests.test_ui_runner import FakeBus, make_runner, wait_until

WHITE = COLOR_NAMES["white"]


def make_app(runner):
    return App(NullDisplay(), ScriptedInput([]), runner,
               port_label="/dev/fake")


def test_standby_pattern_is_every_valid_triangle_white():
    frame = STANDBY(0, [0x01, 0x02])
    assert set(frame) == {0x01, 0x02}
    for triangles in frame.values():
        assert set(triangles) == set(VALID_TRIANGLES)
        assert set(triangles.values()) == {WHITE}


def test_standby_is_not_offered_in_the_menu():
    # It is a state, not a choice - and picking it would loop a 9.8 s
    # repaint of white on white forever.
    assert "standby" not in BY_KEY


def test_standby_stops_playback_saves_white_and_finishes():
    bus = FakeBus()
    runner = make_runner(bus)
    runner.standby()
    assert wait_until(lambda: runner.standby_ready)
    assert wait_until(lambda: not runner.running)   # one-shot, not a loop

    # Silence first, so the autoplay is not still repainting over us.
    assert bus.sent[0].cmd == 0x17 and bus.sent[0].dest == 0xFF
    assert any(f.cmd == 0x13 for f in bus.requested)          # save color
    assert any(f.cmd == 0x1D for f in bus.sent)                # show single
    assert runner.cycle == 1
    assert runner.error is None


def test_standby_stops_playback_again_after_the_repaint():
    # Showing a slot leaves the board playing; without a second stop it
    # would run on into the factory autoplay once the repaint finished.
    bus = FakeBus()
    runner = make_runner(bus, guard_delay=0.01)
    runner.standby()
    assert wait_until(lambda: runner.standby_ready)
    show_at = max(i for i, f in enumerate(bus.sent) if f.cmd == 0x1D)
    assert any(f.cmd == 0x17 and f.dest == 0xFF
               for f in bus.sent[show_at + 1:])


def test_standby_retries_a_board_that_is_mid_repaint():
    # A board ignores everything for the 9.8 s its e-paper redraws, and
    # at power-on it is doing exactly that. Deaf is not dead.
    class DeafAtFirst(FakeBus):
        """Ignores the first few requests, then behaves normally."""

        deaf_for = 3

        def request(self, frame, retries=3):
            if self.deaf_for > 0:
                self.deaf_for -= 1
                self.requested.append(frame)
                return None
            return super().request(frame, retries)

    bus = DeafAtFirst()
    runner = make_runner(bus, command_attempts=8, retry_delays=(0.01,))
    runner.standby()
    assert wait_until(lambda: runner.standby_ready, timeout=10.0)


def test_app_enters_standby_and_reports_it_on_the_menu():
    bus = FakeBus()
    runner = make_runner(bus)
    app = make_app(runner)
    assert app._standby_status() == ""      # nothing claimed before we ask
    app.enter_standby()
    assert wait_until(lambda: runner.standby_ready)
    assert app.screen is Screen.MENU        # standby is not a demo
    assert "standby" in app._standby_status()
    app.draw()                              # the menu renders with it


def test_starting_a_demo_drops_the_standby_status():
    bus = FakeBus()
    runner = make_runner(bus)
    app = make_app(runner)
    app.enter_standby()
    assert wait_until(lambda: runner.standby_ready)
    app.select("solid")
    app.handle("key1")
    assert app.screen is Screen.RUNNING
    assert app._standby_status() == ""
    app.runner.stop()


def test_standby_failure_is_shown_but_does_not_block_the_ui():
    bus = FakeBus(nak_on=0x13)              # the colour save never answers
    runner = make_runner(bus)
    app = make_app(runner)
    app.enter_standby()
    assert wait_until(lambda: runner.error is not None, timeout=10.0)
    assert app._standby_status().startswith("ERROR")
    # Still a usable menu: the operator can start a demo regardless.
    app.select("solid")
    app.handle("key1")
    assert app.screen is Screen.RUNNING
    app.runner.stop()


def test_default_runner_reaches_standby_without_arguments():
    runner = DemoRunner(open_bus=lambda port: FakeBus(), port="/dev/fake",
                        echo_log=False, guard_delay=0.0, show_gap=0.001)
    runner.standby()
    assert wait_until(lambda: runner.standby_ready, timeout=30.0)
    runner.stop()


def test_missing_port_is_logged_once_not_every_retry(monkeypatch):
    # Standby waits for the port from boot, so on a host with no panels
    # a per-retry line would fill the journal for as long as it is up.
    import ui.runner as runner_module

    monkeypatch.setattr(runner_module, "find_port", lambda: None)
    runner = make_runner(FakeBus(), port=None)
    runner.standby()
    assert wait_until(lambda: runner.error == "no serial port")
    time.sleep(0.2)                              # several port_wait rounds
    runner.stop()
    waiting = [l for l in runner.recent(50) if "no serial port" in l]
    assert len(waiting) == 1
