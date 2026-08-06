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
        self.paused = False
        self.starts = []
        self.stops = 0
        self.pauses = 0

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

    def stop(self, timeout=5.0):
        self.running = False
        self.paused = False
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


def test_key1_while_running_pauses_then_resumes():
    # Pause, not stop: the demo keeps its cycle count and timer.
    # Full coverage of this lives in tests/test_ui_pause.py.
    app, runner = make_app()
    app.handle("key1")
    app.handle("key1")
    assert runner.paused
    assert runner.stops == 0
    app.handle("key1")
    assert not runner.paused
    assert runner.starts == [PATTERNS[0].key]     # never restarted


def test_key2_returns_to_menu_and_stops_the_demo():
    app, runner = make_app()
    app.handle("key1")
    app.handle("key2")
    assert app.screen is Screen.MENU
    assert runner.stops == 1


def test_key3_blanks_the_screen_without_stopping_the_demo():
    # On an appliance whose only interface is this screen, no button may
    # leave it dark and unrecoverable - KEY3 sleeps, it does not quit.
    app, runner = make_app()
    app.handle("key1")
    app.handle("key3")
    assert app.blanked
    assert app.display.asleep
    assert not app.quit
    assert runner.running          # the panels keep cycling


def test_any_press_wakes_the_screen_and_is_consumed():
    app, runner = make_app()
    app.handle("key3")
    before = list(runner.starts)   # copy: the runner keeps appending to its own
    app.handle("key1")             # blind press: wakes only
    assert not app.blanked
    assert not app.display.asleep
    assert runner.starts == before
    app.handle("key1")             # now it acts
    assert len(runner.starts) == len(before) + 1


def test_blanked_screen_is_not_repainted():
    app, _ = make_app()
    app.handle("key1")
    app.tick(wait=0)
    app.handle("key3")
    before = app.display.frames
    app.tick(wait=0)
    app.tick(wait=0)
    assert app.display.frames == before


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


def test_run_loop_processes_events_and_stops_the_demo_on_exit():
    app, runner = make_app(["down", "key1"])
    app.run(max_ticks=5)
    assert runner.starts == [PATTERNS[1].key]
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
