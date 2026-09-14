"""UI state machine: menu -> running, driven by HAT events.

Controls (Waveshare 1.3inch LCD HAT):
  joystick up/down/left/right - choose a demo pattern
  KEY1                        - start; then pause; then resume
  KEY1 held ~1 s              - reset: start again from zero
  KEY2                        - back to the menu (stops a running demo)
  KEY3                        - blank the screen now

The last menu row, UPDATE FW, is a mode rather than a demo: it stops
the runner to free the port, lets UP/DOWN pick the USB-attached board's
address, and KEY1 flashes the bundled image (ui/updater.py). While the
transfer runs every button is ignored - there is no safe "abort" of an
OTA in flight - and afterwards KEY2 returns to the menu via the boot
standby, which silences the factory autoplay the rebooted board wakes
up playing.

The screen also blanks itself after BLANK_AFTER_S without input. Any
press wakes it and does nothing else - waking must never move the state
machine, or a blind press in a dark room could stop a running show.

No button quits. This runs as a service on a headless appliance, so a
button that ended the process would leave the screen dark until someone
SSHed in.

With `locked` set the buttons do nothing at all, except that they still
wake the screen; the UNLOCK_SEQUENCE frees them temporarily and the lock
returns by itself after RELOCK_AFTER_S of quiet.
"""

from __future__ import annotations

import time
from enum import Enum

from . import notify, render
from .config import (BLANK_AFTER_S, FRAME_INTERVAL_S, LOG_LINES,
                     RELOCK_AFTER_S, UNLOCK_SEQUENCE, UNLOCK_WINDOW_S,
                     WATCHDOG_PERIOD_S)
from .patterns import PATTERNS
from .runner import DemoRunner


class Screen(Enum):
    MENU = "menu"
    RUNNING = "running"
    UPDATE = "update"


class App:
    def __init__(self, display, inputs, runner: DemoRunner | None = None,
                 patterns=None, port_label: str | None = None,
                 locked: bool = False, blank_after: float = BLANK_AFTER_S,
                 relock_after: float = RELOCK_AFTER_S,
                 clock=time.monotonic, updater=None):
        self.display = display
        self.inputs = inputs
        self.runner = runner or DemoRunner()
        self.patterns = list(patterns or PATTERNS)
        # The update mode is a menu row only when an updater is wired
        # in, so a headless or test App keeps the plain demo menu.
        self.updater = updater
        if updater is not None:
            self.patterns.append(updater.menu_entry)
        self.selected = 0
        self.screen = Screen.MENU
        self.port_label = port_label
        self.quit = False
        self.blanked = False
        self.locked = locked
        self.blank_after = blank_after
        self.relock_after = relock_after
        self._clock = clock
        # Only a device that started locked re-locks itself; unlocking an
        # unlocked device would be a surprise.
        self._locks_itself = locked
        self._unlock_progress: list[str] = []
        self._unlock_started = 0.0
        self._last_input = clock()
        self._last_pet = 0.0
        self._dirty = True
        self._drawn_key = None
        self._standby = False

    # ---- state transitions ----

    def select(self, key: str) -> None:
        """Move the menu cursor to a pattern by key."""
        for index, pattern in enumerate(self.patterns):
            if pattern.key == key:
                self.selected = index
                self._dirty = True
                return
        raise KeyError(f"unknown pattern: {key}")

    def _try_unlock(self, event: str) -> None:
        now = self._clock()
        if self._unlock_progress and now - self._unlock_started > UNLOCK_WINDOW_S:
            self._unlock_progress.clear()
        if not self._unlock_progress:
            self._unlock_started = now
        expected = UNLOCK_SEQUENCE[len(self._unlock_progress)]
        if event == expected:
            self._unlock_progress.append(event)
            if len(self._unlock_progress) == len(UNLOCK_SEQUENCE):
                self._unlock_progress.clear()
                self.locked = False
                self._dirty = True
        else:
            self._unlock_progress.clear()

    def handle(self, event: str) -> None:
        now = self._clock()
        if self.blanked:
            # Any press wakes the screen and is consumed doing so, so a
            # blind press cannot also change what is running.
            self.blanked = False
            self.display.wake()
            self._dirty = True
            self._last_input = now
            return
        self._last_input = now

        if self.locked:
            self._try_unlock(event)
            return

        if event == "key3":
            self._blank()
            return

        if self.screen is Screen.UPDATE:
            self._handle_update(event)
            return

        if event == "key1_hold":
            # Reset: back to cycle 0 with the timer at zero, wherever we
            # were. Distinct from pause, which keeps both.
            self._restart()
            return

        if self.screen is Screen.MENU:
            if event in ("up", "left"):
                self.selected = (self.selected - 1) % len(self.patterns)
                self._dirty = True
            elif event in ("down", "right"):
                self.selected = (self.selected + 1) % len(self.patterns)
                self._dirty = True
            elif event in ("key1", "press"):
                self._restart()
        else:  # RUNNING
            if event in ("key1", "press"):
                # Pause and resume keep the cycle count and the timer; the
                # panels hold their image while paused.
                if self.runner.paused:
                    self.runner.resume()
                elif self.runner.running:
                    self.runner.pause()
                else:
                    self._restart()
                self._dirty = True
            elif event == "key2":
                self.runner.stop()
                self.screen = Screen.MENU
                self._dirty = True

    def _handle_update(self, event: str) -> None:
        updater = self.updater
        if updater.busy:
            return                  # nothing interrupts a flash in flight
        if updater.finished:
            if event in ("key1", "press"):
                updater.reset()     # back to the confirm screen
                updater.probe()
            elif event == "key2":
                self._leave_update()
            self._dirty = True
            return
        if event in ("up", "left"):
            updater.select(-1)
        elif event in ("down", "right"):
            updater.select(+1)
        elif event in ("key1", "press"):
            updater.start()
        elif event == "key2":
            self._leave_update()
        self._dirty = True

    def _enter_update(self) -> None:
        # The runner owns the serial port (standby keeps it open to
        # watch the link), and the OTA needs it to itself.
        self.runner.stop()
        self._standby = False
        self.updater.reset()
        self.updater.probe()
        self.screen = Screen.UPDATE
        self._dirty = True

    def _leave_update(self) -> None:
        # A freshly flashed board reboots into its factory autoplay;
        # the boot standby is exactly what silences that.
        self.screen = Screen.MENU
        self.enter_standby()

    def _restart(self) -> None:
        if self.patterns[self.selected].key == "update":
            self._enter_update()
            return
        if self.patterns[self.selected].key == "standby":
            # The top menu entry is not a looping demo. One shot of the
            # boot standby - every sector white, every board probed -
            # with the outcome reported on the menu.
            self.enter_standby()
            self.screen = Screen.MENU
            return
        self._standby = False
        self.runner.start(self.patterns[self.selected])
        self.screen = Screen.RUNNING
        self._dirty = True

    def _blank(self) -> None:
        if not self.blanked:
            self.blanked = True
            self.display.sleep()

    # ---- drawing ----

    def enter_standby(self) -> None:
        """Silence the factory autoplay and white out the panels.

        Called once the link is up, before anyone touches a button, so
        the installation waits on white instead of on whatever vendor
        demo frame happened to be mid-play.
        """
        self._standby = True
        self.runner.standby()
        self._dirty = True

    def _standby_status(self) -> str:
        """Menu subtitle while the panels are being blanked, else ''.

        Doubles as the link-check report: standby probes every
        configured board, so the count of boards answering - and any
        error - is the state of the wall.
        """
        if not self._standby:
            return ""
        if self.runner.error:
            return f"ERROR {self.runner.error}"
        boards = f"{len(self.runner.live)}/{len(self.runner.boards)}"
        if self.runner.standby_ready:
            return f"standby: white, boards {boards} OK"
        if self.runner.running:
            return f"standby: blanking + check {boards}..."
        return ""

    def frame(self):
        if self.screen is Screen.MENU:
            return render.menu_screen(self.patterns, self.selected,
                                      self.port_label, locked=self.locked,
                                      status=self._standby_status())
        if self.screen is Screen.UPDATE:
            updater = self.updater
            return render.update_screen(
                updater.firmware_label, updater.size, updater.addr,
                updater.phase, updater.board_state, updater.done,
                updater.recent(LOG_LINES), error=updater.error,
                locked=self.locked)
        pattern = self.runner.pattern
        return render.running_screen(
            pattern.label if pattern else "-",
            self.runner.elapsed,
            self.runner.cycle,
            self.runner.recent(LOG_LINES),
            error=self.runner.error,
            caption=self.runner.caption,
            stopping=not self.runner.running and self.runner.error is None,
            paused=self.runner.paused,
            locked=self.locked,
        )

    def draw(self) -> None:
        if self.blanked:
            return
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
            # The menu shows the standby progress, so it has to repaint
            # when that changes.
            if self.screen is Screen.UPDATE:
                updater = self.updater
                return ("update", updater.phase, updater.addr,
                        updater.board_state, updater.done, updater.size,
                        tuple(updater.recent(LOG_LINES)), updater.error,
                        self.locked)
            return ("menu", self._standby_status())
        return (int(self.runner.elapsed), self.runner.cycle,
                self.runner.caption,
                tuple(self.runner.recent(LOG_LINES)),
                self.runner.error, self.runner.running, self.runner.paused)

    # ---- main loop ----

    def _idle_tasks(self) -> None:
        now = self._clock()
        idle = now - self._last_input
        if not self.blanked and 0 < self.blank_after <= idle:
            self._blank()
        if (self._locks_itself and not self.locked
                and 0 < self.relock_after <= idle):
            self.locked = True
            self._unlock_progress.clear()
            self._dirty = True
        if now - self._last_pet >= WATCHDOG_PERIOD_S:
            self._last_pet = now
            notify.alive()

    def tick(self, wait: float = FRAME_INTERVAL_S) -> None:
        """Drain pending events, then redraw if anything visible changed."""
        event = self.inputs.get(timeout=wait)
        while event is not None:
            self.handle(event)
            if self.quit:
                return
            event = self.inputs.get()
        self._idle_tasks()
        if self.blanked:
            return
        if self._dirty or self._display_key() != self._drawn_key:
            self.draw()

    def run(self, max_ticks: int | None = None) -> None:
        self.draw()
        notify.ready()
        ticks = 0
        try:
            while not self.quit and (max_ticks is None or ticks < max_ticks):
                self.tick()
                ticks += 1
        finally:
            self.runner.stop()
            self.display.wake()
            self.display.show(render.message_screen("stopped", "service ended"))
            time.sleep(0.2)
