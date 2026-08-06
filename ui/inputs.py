"""Input backends: the HAT's buttons, or a keyboard/script stand-in.

All backends expose the same event names as the HAT controls:
up, down, left, right, press, key1, key2, key3.
"""

import queue
import sys
import threading

from .config import BUTTON_PINS, EVENTS, KEY1_HOLD_S


class InputSource:
    """Interface: non-blocking `get()` returning one event name or None."""

    def get(self, timeout: float = 0.0) -> str | None:
        raise NotImplementedError  # pragma: no cover - interface

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class QueueInput(InputSource):
    """Base for backends that push events from another thread."""

    def __init__(self):
        self._queue: queue.Queue[str] = queue.Queue()

    def post(self, event: str) -> None:
        if event not in EVENTS:
            raise ValueError(f"unknown event: {event}")
        self._queue.put(event)

    def get(self, timeout: float = 0.0) -> str | None:
        try:
            if timeout <= 0:
                return self._queue.get_nowait()
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None


class GpioInput(QueueInput):
    """Waveshare LCD HAT joystick + KEY1..3 (active low, pull-up).

    KEY1 also reports a hold. The hold fires while the button is still
    down and marks the press as consumed, so releasing afterwards does
    not also send the short event - one press, one meaning.
    """

    def __init__(self, bounce_s: float = 0.05, hold_s: float = KEY1_HOLD_S):
        super().__init__()
        from gpiozero import Button

        self._buttons = []
        self._key1_was_held = False
        for event, pin in BUTTON_PINS.items():
            if event == "key1":
                button = Button(pin, pull_up=True, bounce_time=bounce_s,
                                hold_time=hold_s)
                button.when_pressed = self._key1_pressed
                button.when_held = self._key1_held
                button.when_released = self._key1_released
            else:
                button = Button(pin, pull_up=True, bounce_time=bounce_s)
                button.when_pressed = (lambda e=event: self.post(e))
            self._buttons.append(button)

    def _key1_pressed(self) -> None:
        self._key1_was_held = False

    def _key1_held(self) -> None:
        self._key1_was_held = True
        self.post("key1_hold")

    def _key1_released(self) -> None:
        if not self._key1_was_held:
            self.post("key1")

    def close(self) -> None:
        for button in self._buttons:
            button.close()
        self._buttons.clear()


class KeyboardInput(QueueInput):
    """Line-based stdin fallback so the UI is usable over plain SSH.

    Type a key and press Enter: w/s/a/d move, Enter alone = press,
    1/2/3 = KEY1..3, q = quit (posts key3).
    """

    KEYMAP = {
        "w": "up", "k": "up",
        "s": "down", "j": "down",
        "a": "left", "h": "left",
        "d": "right", "l": "right",
        "": "press", "p": "press",
        "1": "key1", "2": "key2", "3": "key3", "q": "key3",
        "!": "key1_hold",          # shift-1: the KEY1 hold (reset)
    }

    def __init__(self, stream=None):
        super().__init__()
        self._stream = stream or sys.stdin
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def _read_loop(self) -> None:
        while not self._stop.is_set():
            try:
                line = self._stream.readline()
            except (ValueError, OSError):
                return
            if line == "":            # EOF (e.g. running under systemd)
                return
            event = self.KEYMAP.get(line.strip().lower())
            if event:
                self.post(event)

    def close(self) -> None:
        self._stop.set()


class ScriptedInput(QueueInput):
    """Pre-seeded event list for tests and demos."""

    def __init__(self, events=()):
        super().__init__()
        for event in events:
            self.post(event)


def make_input(kind: str = "auto") -> InputSource:
    """kind: auto | gpio | keyboard | none.

    "auto" uses the HAT buttons when gpiozero can claim them and falls
    back to the keyboard, so the same command works with or without the
    HAT fitted.
    """
    if kind == "gpio":
        return GpioInput()
    if kind == "keyboard":
        return KeyboardInput()
    if kind == "none":
        return QueueInput()
    if kind != "auto":
        raise ValueError(f"unknown input kind: {kind}")
    try:
        return GpioInput()
    except Exception:
        return KeyboardInput()
