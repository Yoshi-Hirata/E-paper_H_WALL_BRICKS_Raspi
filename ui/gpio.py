"""Portable GPIO on the Linux character device, via python-periphery.

gpiozero cannot serve here: its lgpio pin factory assumes it is running
on a Raspberry Pi, so it is unusable on any other board. The character
device is the common ground - python-periphery talks to it directly, is
pure Python, and needs nothing compiled.

Two things this must get right, both learned the hard way on the Pi:
the buttons are active low with a pull-up, and KEY1 needs to tell a hold
from a press, which means watching releases and not just presses.
"""

from __future__ import annotations

import select
import threading
import time

from .boards import Line


def _open(line: Line, direction: str, **kwargs):
    from periphery import GPIO

    try:
        return GPIO(line.device, line.line, direction, **kwargs)
    except TypeError:
        # Older python-periphery, or a kernel without the v2 ioctl, will
        # not accept bias/edge. Drop them rather than fail: an external
        # pull-up still works, it is just not configured by us.
        kwargs.pop("bias", None)
        return GPIO(line.device, line.line, direction, **kwargs)


class OutputLine:
    """A driven output (the LCD's DC, RST and backlight lines)."""

    def __init__(self, line: Line, initial: bool = False):
        self._gpio = _open(line, "out")
        self.write(initial)

    def write(self, value: bool) -> None:
        self._gpio.write(bool(value))

    def on(self) -> None:
        self.write(True)

    def off(self) -> None:
        self.write(False)

    def close(self) -> None:
        try:
            self._gpio.close()
        except Exception:
            pass


class ButtonWatcher:
    """Watch several active-low buttons and report presses on a callback.

    One thread selects across every line's event fd. `hold_events` names
    buttons that should also report `<name>_hold` once held past
    `hold_s`; for those the short press is reported on release and is
    suppressed when the hold already fired, so one press means one thing.
    """

    def __init__(self, lines: "dict[str, Line]", on_event,
                 hold_events: "dict[str, float]" | None = None,
                 bounce_s: float = 0.05):
        self._on_event = on_event
        self._hold = dict(hold_events or {})
        self._bounce_s = bounce_s
        self._gpios: "dict[str, object]" = {}
        self._last_edge: "dict[str, float]" = {}
        self._pressed_at: "dict[str, float]" = {}
        self._hold_fired: "dict[str, bool]" = {}
        for name, line in lines.items():
            self._gpios[name] = _open(line, "in", edge="both", bias="pull_up")
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    # Buttons pull the line low, so "pressed" is a zero reading.
    @staticmethod
    def _is_pressed(gpio) -> bool:
        return not gpio.read()

    def _loop(self) -> None:
        by_fd = {gpio.fd: name for name, gpio in self._gpios.items()}
        while not self._stop.is_set():
            # Short timeout so a pending hold is noticed while the button
            # is still down - no edge arrives to wake us for that.
            ready, _, _ = select.select(list(by_fd), [], [], 0.05)
            now = time.monotonic()
            for fd in ready:
                name = by_fd[fd]
                gpio = self._gpios[name]
                try:
                    gpio.read_event()
                except Exception:
                    continue
                if now - self._last_edge.get(name, 0.0) < self._bounce_s:
                    continue
                self._last_edge[name] = now
                self._edge(name, self._is_pressed(gpio), now)
            self._check_holds(now)

    def _edge(self, name: str, pressed: bool, now: float) -> None:
        if name not in self._hold:
            if pressed:
                self._on_event(name)
            return
        if pressed:
            self._pressed_at[name] = now
            self._hold_fired[name] = False
        else:
            # Released: report the short press only if the hold did not
            # already claim this one.
            if self._pressed_at.pop(name, None) is not None \
                    and not self._hold_fired.get(name):
                self._on_event(name)
            self._hold_fired[name] = False

    def _check_holds(self, now: float) -> None:
        for name, threshold in self._hold.items():
            started = self._pressed_at.get(name)
            if started is None or self._hold_fired.get(name):
                continue
            if now - started >= threshold:
                self._hold_fired[name] = True
                self._on_event(f"{name}_hold")

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)
        for gpio in self._gpios.values():
            try:
                gpio.close()
            except Exception:
                pass
        self._gpios.clear()
