import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ui.app import App, Screen
from ui.config import HEIGHT, WIDTH
from ui.display import NullDisplay
from ui.inputs import ScriptedInput
from ui.patterns import PATTERNS


class FakeRunner:
    """Stands in for DemoRunner: records calls, no threads or serial."""

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


def make_app(events=()):
    runner = FakeRunner()
    app = App(NullDisplay(), ScriptedInput(events), runner,
              port_label="/dev/ttyACM0")
    return app, runner


def test_joystick_moves_selection_and_wraps():
    app, _ = make_app()
    app.handle("down")
    assert app.selected == 1
    app.handle("up")
    app.handle("up")
    assert app.selected == len(PATTERNS) - 1   # wrapped past the top
    app.handle("right")
    assert app.selected == 0


def test_key1_starts_the_selected_pattern():
    app, runner = make_app()
    app.handle("down")
    app.handle("key1")
    assert app.screen is Screen.RUNNING
    assert runner.starts == [PATTERNS[1].key]


def test_key1_while_running_stops_then_restarts():
    app, runner = make_app()
    app.handle("key1")
    app.handle("key1")
    assert runner.stops == 1
    assert not runner.running
    app.handle("key1")            # pressing again restarts the same demo
    assert runner.running
    assert runner.starts == [PATTERNS[0].key, PATTERNS[0].key]


def test_key2_returns_to_menu_and_stops_the_demo():
    app, runner = make_app()
    app.handle("key1")
    app.handle("key2")
    assert app.screen is Screen.MENU
    assert runner.stops == 1


def test_key3_quits_from_either_screen():
    app, _ = make_app()
    app.handle("key3")
    assert app.quit
    app2, _ = make_app()
    app2.handle("key1")
    app2.handle("key3")
    assert app2.quit


def test_menu_ignores_selection_keys_while_running():
    app, _ = make_app()
    app.handle("key1")
    app.handle("down")
    assert app.selected == 0      # joystick must not re-select mid-demo


def test_frames_are_lcd_sized_on_both_screens():
    app, _ = make_app()
    assert app.frame().size == (WIDTH, HEIGHT)
    app.handle("key1")
    assert app.frame().size == (WIDTH, HEIGHT)


def test_run_loop_processes_events_and_stops_on_quit():
    app, runner = make_app(["down", "key1", "key3"])
    app.run(max_ticks=20)
    assert runner.starts == [PATTERNS[1].key]
    assert app.quit
    assert runner.stops >= 1      # run() always stops the demo on exit


def test_idle_ticks_do_not_repaint_an_unchanged_screen():
    # Packing a frame costs ~125 ms on the target Pi, so identical
    # repaints would burn most of the CPU for nothing.
    app, _ = make_app()
    app.handle("key1")
    app.tick(wait=0)                       # initial paint of the run screen
    before = app.display.frames
    app.tick(wait=0)
    app.tick(wait=0)
    assert app.display.frames == before


def test_running_screen_repaints_when_the_timer_advances():
    app, runner = make_app()
    app.handle("key1")
    app.tick(wait=0)
    before = app.display.frames
    runner.elapsed = 1.0                   # one second later
    app.tick(wait=0)
    assert app.display.frames == before + 1


def test_running_screen_repaints_on_a_new_cycle():
    app, runner = make_app()
    app.handle("key1")
    app.tick(wait=0)
    before = app.display.frames
    runner.cycle = 1
    app.tick(wait=0)
    assert app.display.frames == before + 1
