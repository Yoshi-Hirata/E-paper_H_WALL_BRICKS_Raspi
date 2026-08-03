"""Background demo runner: drives the panels and reports progress.

Mirrors the sequence proven by host/wave_demo.py (broadcast stop at
start, per-board stop -> save color with ACK check, broadcast show, guard
stop mid-interval), but runs in a thread so the UI stays responsive and
can show a live log and elapsed timer.
"""

import random
import sys
import threading
import time
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

from epaper.commands import TEST_SLOT, save_color, show_single, slot_config, stop
from epaper.pattern import build_hexagon_array
from epaper.transport import Bus, find_port

from .config import LOG_HISTORY
from .patterns import DEFAULT_PALETTE, Pattern

DEFAULT_BOARDS = [0x01, 0x02]


class DemoRunner:
    """Runs one pattern in a worker thread until stopped.

    `open_bus` is injectable so tests can drive a fake transport.
    """

    def __init__(self, boards: list[int] | None = None,
                 interval: float = 60.0, guard_delay: float = 12.0,
                 slot: int = TEST_SLOT, port: str | None = None,
                 palette: list[int] | None = None,
                 open_bus=None, seed: int | None = None):
        self.boards = boards or list(DEFAULT_BOARDS)
        self.interval = interval
        self.guard_delay = guard_delay
        self.slot = slot
        self.port = port
        self.palette = palette or list(DEFAULT_PALETTE)
        self._open_bus = open_bus or (lambda p: Bus(p, verbose=False))
        self._seed = seed

        self.log: deque[str] = deque(maxlen=LOG_HISTORY)
        self.pattern: Pattern | None = None
        self.cycle = 0
        self.started_at: float | None = None
        self.error: str | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    # ---- state ----

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def elapsed(self) -> float:
        return 0.0 if self.started_at is None else time.monotonic() - self.started_at

    def recent(self, count: int) -> list[str]:
        with self._lock:
            return list(self.log)[-count:]

    def emit(self, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        with self._lock:
            self.log.append(f"{stamp} {message}")

    # ---- control ----

    def start(self, pattern: Pattern) -> None:
        if self.running:
            self.stop()
        self.pattern = pattern
        self.cycle = 0
        self.error = None
        self._stop.clear()
        self.started_at = time.monotonic()
        self.emit(f"start {pattern.label}")
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=timeout)
        self.started_at = None

    # ---- worker ----

    def _sleep(self, seconds: float) -> bool:
        """Interruptible sleep; False if a stop was requested."""
        return not self._stop.wait(seconds)

    def _run(self) -> None:
        port = self.port or find_port()
        if not port:
            self.error = "no serial port"
            self.emit("ERROR no serial port")
            return
        rng = random.Random(self._seed)
        try:
            with self._open_bus(port) as bus:
                self.emit(f"port {port}")
                groups = max(len(self.boards), max(self.boards))
                # Silence the factory autoplay before touching the slots.
                bus.send(stop(0xFF, groups))
                time.sleep(0.3)
                for board in self.boards:
                    if not self._request(bus, stop(board, groups),
                                         f"stop @{board:02X}"):
                        return
                    if not self._request(
                            bus, slot_config(board, self.slot, group_count=groups),
                            f"cfg @{board:02X}"):
                        return

                while not self._stop.is_set():
                    frame = self.pattern(self.cycle, self.boards,
                                         self.palette, rng)
                    for board in self.boards:
                        if not self._request(bus, stop(board, groups),
                                             f"stop @{board:02X}"):
                            return
                        arr = build_hexagon_array(frame[board])
                        if not self._request(
                                bus, save_color(board, self.slot, arr, groups),
                                f"save @{board:02X}"):
                            return
                    bus.send(show_single(0xFF, self.slot, groups))
                    self.cycle += 1
                    self.emit(f"cycle {self.cycle} shown")

                    if 0 < self.guard_delay < self.interval:
                        if not self._sleep(self.guard_delay):
                            break
                        bus.send(stop(0xFF, groups))
                        if not self._sleep(self.interval - self.guard_delay):
                            break
                    elif not self._sleep(self.interval):
                        break
        except Exception as exc:  # serial unplugged, permission, ...
            self.error = str(exc)
            self.emit(f"ERROR {exc}")
        finally:
            self.emit("stopped")

    def _request(self, bus, frame, label: str) -> bool:
        ack = bus.request(frame)
        if ack is None:
            self.error = f"{label}: no ACK"
            self.emit(f"ERROR {label} no ACK")
            return False
        if ack.cmd != 0x80:
            self.error = f"{label}: NAK 0x{ack.cmd:02X}"
            self.emit(f"ERROR {label} NAK 0x{ack.cmd:02X}")
            return False
        return True
