"""UI state machine: menu -> running, driven by HAT events.

Controls (Waveshare 1.3inch LCD HAT):
  joystick up/down/left/right - choose a demo pattern
  KEY1                        - start the selected demo / stop it again
  KEY2                        - back to the menu (stops a running demo)
  KEY3                        - quit
"""

import time
from enum import Enum

from . import render
from .config import FRAME_INTERVAL_S, LOG_LINES
from .patterns import PATTERNS
from .runner import DemoRunner


class Screen(Enum):
    MENU = "menu"
    RUNNING = "running"


class App:
    def __init__(self, display, inputs, runner: DemoRunner | None = None,
                 patterns=None, port_label: str | None = None):
        self.display = display
        self.inputs = inputs
        self.runner = runner or DemoRunner()
        self.patterns = list(patterns or PATTERNS)
        self.selected = 0
        self.screen = Screen.MENU
        self.port_label = port_label
        self.quit = False
        self._dirty = True
        self._drawn_key = None

    # ---- state transitions ----

    def handle(self, event: str) -> None:
        if event == "key3":
            self.quit = True
            self._dirty = True
            return

        if self.screen is Screen.MENU:
            if event in ("up", "left"):
                self.selected = (self.selected - 1) % len(self.patterns)
                self._dirty = True
            elif event in ("down", "right"):
                self.selected = (self.selected + 1) % len(self.patterns)
                self._dirty = True
            elif event in ("key1", "press"):
                self.runner.start(self.patterns[self.selected])
                self.screen = Screen.RUNNING
                self._dirty = True
        else:  # RUNNING
            if event in ("key1", "press"):
                # Toggle: stop a live demo, restart it once stopped.
                if self.runner.running:
                    self.runner.stop()
                else:
                    self.runner.start(self.patterns[self.selected])
                self._dirty = True
            elif event == "key2":
                self.runner.stop()
                self.screen = Screen.MENU
                self._dirty = True

    # ---- drawing ----

    def frame(self):
        if self.screen is Screen.MENU:
            return render.menu_screen(self.patterns, self.selected,
                                      self.port_label)
        pattern = self.runner.pattern
        return render.running_screen(
            pattern.label if pattern else "-",
            self.runner.elapsed,
            self.runner.cycle,
            self.runner.recent(LOG_LINES),
            error=self.runner.error,
            stopping=not self.runner.running and self.runner.error is None,
        )

    def draw(self) -> None:
        self.display.show(self.frame())
        self._dirty = False
        self._drawn_key = self._display_key()

    def _display_key(self):
        """Everything the running screen actually shows.

        Packing a frame costs ~125 ms on a Pi Zero 2 W, so redrawing on
        every input poll would eat most of the CPU to paint identical
        pixels - the timer only has one-second resolution.
        """
        if self.screen is not Screen.RUNNING:
            return None
        return (int(self.runner.elapsed), self.runner.cycle,
                tuple(self.runner.recent(LOG_LINES)),
                self.runner.error, self.runner.running)

    # ---- main loop ----

    def tick(self, wait: float = FRAME_INTERVAL_S) -> None:
        """Drain pending events, then redraw if anything visible changed."""
        event = self.inputs.get(timeout=wait)
        while event is not None:
            self.handle(event)
            if self.quit:
                return
            event = self.inputs.get()
        if self._dirty or self._display_key() != self._drawn_key:
            self.draw()

    def run(self, max_ticks: int | None = None) -> None:
        self.draw()
        ticks = 0
        try:
            while not self.quit and (max_ticks is None or ticks < max_ticks):
                self.tick()
                ticks += 1
        finally:
            self.runner.stop()
            self.display.show(render.message_screen("stopped", "KEY3 pressed"))
            time.sleep(0.2)
