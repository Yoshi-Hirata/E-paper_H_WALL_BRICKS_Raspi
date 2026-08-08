"""Background demo runner: drives the panels and reports progress.

Built for show operation, where the failure that matters is a frozen
panel waiting for a human. Nothing here is fatal - see docs/RELIABILITY.md
for the reasoning. The layers, outermost last:

  command  exponential backoff, long enough to outwait a repaint (9.8 s
           measured) and a busy board
  cycle    give up on at most one cycle, re-initialise, try the next one
  bus      close and reopen the port - via find_port, since a USB
           re-enumeration can rename the device - and wait for it to come
           back if it is gone
  thread   the worker only ever exits when asked to stop

The command sequence itself mirrors host/wave_demo.py, which is the one
verified against the real boards.

Standby (all-white idle) uses the same machinery, but instead of looping
it paints once and then watches the link, repainting whenever the boards
come back from a power cycle running their factory demo.
"""

from __future__ import annotations

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
ACK_SUCCESS, ACK_BUSY = 0x80, 0x82
SHOW_REPEATS = 3          # the show frame is broadcast, so never acknowledged
SHOW_GAP_S = 0.15
LINK_POLL_S = 2.0         # how often standby checks the panel link
LINK_GUARD_S = 60.0       # how often standby re-suppresses the autoplay


def device_token(port: str):
    """Identity of the panel's USB device node, or None if it is gone.

    Unplugging the cable removes the node, and board 0x01 comes back from
    the replug running its factory demo - the board cannot tell us, so
    this is how it is noticed. Presence alone is not enough: a quick
    replug can come and go between polls. A re-enumeration creates a new
    devtmpfs node, so the inode and its creation time change even when
    the name does not.

    Names that are not POSIX device paths (COM3) have no node to look at;
    they report a constant, and a failed command has to speak instead.
    """
    if not port.startswith("/dev/"):
        return "opaque"
    try:
        info = Path(port).stat()
    except OSError:
        return None
    return (info.st_ino, info.st_ctime_ns)


class DemoRunner:
    """Runs one pattern in a worker thread until stopped.

    `open_bus` is injectable so tests can drive a fake transport; the
    retry timings are parameters so they can be compressed in tests.
    """

    def __init__(self, boards: list[int] | None = None,
                 interval: float = 60.0, guard_delay: float = 12.0,
                 slot: int = TEST_SLOT, port: str | None = None,
                 palette: list[int] | None = None,
                 open_bus=None, seed: int | None = None,
                 echo_log: bool = True,
                 command_attempts: int = 8, save_attempts: int = 3,
                 retry_delays: tuple[float, ...] = (1, 2, 4, 8, 12),
                 busy_delay: float = 1.0, busy_attempts: int = 5,
                 reopen_after_failures: int = 3, reopen_delay: float = 2.0,
                 port_wait: float = 10.0,
                 show_repeats: int = SHOW_REPEATS,
                 show_gap: float = SHOW_GAP_S,
                 link_poll: float = LINK_POLL_S,
                 link_guard: float = LINK_GUARD_S,
                 link_token=device_token):
        self.boards = boards or list(DEFAULT_BOARDS)
        self.interval = interval
        self.guard_delay = guard_delay
        self.slot = slot
        self.port = port
        self.palette = palette or list(DEFAULT_PALETTE)
        self._open_bus = open_bus or (lambda p: Bus(p, verbose=False))
        self._seed = seed
        # Mirror the on-screen log to stdout so the same progress shows up
        # in `journalctl -u epaper-ui` (the LCD is the only other view).
        self._echo_log = echo_log

        self.command_attempts = command_attempts
        # Each save retry writes the boards' flash, so it retries less.
        self.save_attempts = save_attempts
        self.retry_delays = retry_delays
        self.busy_delay = busy_delay
        self.busy_attempts = busy_attempts
        self.reopen_after_failures = reopen_after_failures
        self.reopen_delay = reopen_delay
        self.port_wait = port_wait
        self.show_repeats = show_repeats
        self.show_gap = show_gap
        self.link_poll = link_poll
        self.link_guard = link_guard
        self._link_token = link_token

        self.log: deque[str] = deque(maxlen=LOG_HISTORY)
        self.pattern: Pattern | None = None
        self._once = False
        self.standby_ready = False
        self.cycle = 0
        self.failures = 0          # cycles abandoned since the demo started
        self.started_at: float | None = None
        self.error: str | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._elapsed_base = 0.0
        self._lock = threading.Lock()

    # ---- state ----

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def paused(self) -> bool:
        return self._pause.is_set()

    @property
    def elapsed(self) -> float:
        """Time spent demoing, with paused stretches not counted."""
        if self.started_at is None:
            return self._elapsed_base
        return self._elapsed_base + time.monotonic() - self.started_at

    def recent(self, count: int) -> list[str]:
        with self._lock:
            return list(self.log)[-count:]

    def emit(self, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        line = f"{stamp} {message}"
        with self._lock:
            self.log.append(line)
        if self._echo_log:
            print(line, flush=True)

    # ---- control ----

    def standby(self) -> None:
        """Silence the factory autoplay and leave every panel white.

        Runs as soon as the link is up, so the installation always starts
        from a known blank state instead of whatever vendor demo frame
        happened to be on the glass. One-shot: it paints once and the
        worker finishes, leaving the panels holding white.
        """
        from .patterns import STANDBY

        self.start(STANDBY, once=True)

    def start(self, pattern: Pattern, once: bool = False) -> None:
        if self.running:
            self.stop()
        self._once = once
        self.pattern = pattern
        self.cycle = 0
        self.failures = 0
        self.error = None
        self._stop.clear()
        self._pause.clear()
        self._elapsed_base = 0.0
        self.standby_ready = False
        self.started_at = time.monotonic()
        self.emit(f"start {pattern.label}")
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._pause.clear()          # let a paused worker notice the stop
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=timeout)
        self.started_at = None
        self._elapsed_base = 0.0

    def pause(self) -> None:
        """Hold between cycles, keeping the timer and the pattern position.

        The panels simply keep whatever they are showing - nothing is
        playing on them, so there is nothing to stop.
        """
        if not self.running or self.paused:
            return
        self._elapsed_base = self.elapsed
        self.started_at = None       # freeze the timer
        self._pause.set()
        self.emit("paused")

    def resume(self) -> None:
        if not self.paused:
            return
        self.started_at = time.monotonic()
        self._pause.clear()
        self.emit("resumed")

    # ---- retry helpers ----

    def _hold_while_paused(self) -> bool:
        """Block until resumed; False if a stop was requested meanwhile."""
        while self._pause.is_set():
            if self._stop.wait(0.05):
                return False
        return True

    def _sleep(self, seconds: float) -> bool:
        """Interruptible sleep; False if a stop was requested.

        A pause is honoured before and after the wait, so the demo halts
        between cycles rather than mid-command.
        """
        if not self._hold_while_paused():
            return False
        if self._stop.wait(seconds):
            return False
        return self._hold_while_paused()

    def _backoff(self, attempt: int) -> float:
        return self.retry_delays[min(attempt, len(self.retry_delays)) - 1]

    def _request(self, bus, frame, label: str, attempts: int | None = None) -> bool:
        """Send until acknowledged, backing off between tries.

        The backoff has to be able to outwait a full repaint: a board is
        deaf for 9.8 s while its e-paper redraws, and that is a normal
        event, not a fault.
        """
        attempts = attempts or self.command_attempts
        busy_seen = 0
        for attempt in range(1, attempts + 1):
            ack = bus.request(frame)
            if ack is not None and ack.cmd == ACK_SUCCESS:
                if attempt > 1:
                    self.emit(f"{label} ok after {attempt} tries")
                return True
            if ack is not None and ack.cmd == ACK_BUSY:
                busy_seen += 1
                if busy_seen > self.busy_attempts:
                    break
                if not self._sleep(self.busy_delay):
                    return False
                continue
            if attempt == 1:
                reason = "no ACK" if ack is None else f"NAK 0x{ack.cmd:02X}"
                self.emit(f"{label} {reason}, retrying")
            if attempt < attempts and not self._sleep(self._backoff(attempt)):
                return False
        self.error = f"{label}: gave up after {attempts}"
        self.emit(f"ERROR {label} gave up")
        return False

    # ---- one cycle ----

    def _setup(self, bus, groups: int) -> bool:
        """Silence any playback and (re)configure the slot on each board."""
        bus.send(stop(0xFF, groups))
        time.sleep(0.3)
        for board in self.boards:
            if not self._request(bus, stop(board, groups), f"stop @{board:02X}"):
                return False
            if not self._request(bus, slot_config(board, self.slot,
                                                  group_count=groups),
                                 f"cfg @{board:02X}"):
                return False
        return True

    def _cycle(self, bus, groups: int, rng: random.Random) -> bool:
        # A playlist hands back whichever pattern owns this cycle; a plain
        # pattern hands back itself.
        active, local_cycle = self.pattern.resolve(self.cycle)
        frame = active(local_cycle, self.boards, self.palette, rng)
        for board in self.boards:
            if not self._request(bus, stop(board, groups), f"stop @{board:02X}"):
                return False
            arr = build_hexagon_array(frame[board])
            if not self._request(bus, save_color(board, self.slot, arr, groups),
                                 f"save @{board:02X}", self.save_attempts):
                return False

        # Broadcast keeps both panels in step but is unacknowledged, so a
        # dropped frame would silently leave the old image up. Repeating is
        # harmless: a board ignores commands while it is already repainting.
        for _ in range(self.show_repeats):
            bus.send(show_single(0xFF, self.slot, groups))
            time.sleep(self.show_gap)

        self.cycle += 1
        label = "" if active is self.pattern else f" {active.label}"
        self.emit(f"cycle {self.cycle}{label} shown")
        return True

    def _settle(self, bus, groups: int) -> None:
        """Let the repaint finish, then stop playback and leave it there.

        Only for the one-shot standby paint. Showing a slot starts the
        board playing, and once its 9.8 s repaint is done it would run on
        into the factory autoplay - which is exactly the thing standby
        exists to silence. The looping demo gets the same treatment from
        _wait_next after every cycle.
        """
        if not self._sleep(self.guard_delay):
            return
        bus.send(stop(0xFF, groups))
        for board in self.boards:
            self._request(bus, stop(board, groups), f"stop @{board:02X}")

    def _watch_link(self, bus, groups: int, port: str) -> bool:
        """Sit on white until the panel link changes. False to stop.

        Standby is not a one-shot job: a board that loses power - an
        unplugged USB cable is enough - comes back playing the factory
        demo, and nothing on the board says so. Watching the device node
        is the cue to paint white again once it returns. The caller
        reopens the bus, since a replug can rename the port.

        A broadcast stop goes out every link_guard seconds as well. It
        costs one 8-byte frame and it is the backstop for a reboot this
        cannot see - it silences an autoplay that started unnoticed,
        which is what actually ruins the look of the wall.
        """
        token = self._link_token(port)
        next_guard = time.monotonic() + self.link_guard
        while not self._stop.is_set():
            if not self._sleep(self.link_poll):
                return False
            if self._link_token(port) != token:
                self.standby_ready = False
                self.emit("panel link changed, will re-blank")
                return True
            if self.link_guard > 0 and time.monotonic() >= next_guard:
                next_guard = time.monotonic() + self.link_guard
                bus.send(stop(0xFF, groups))
        return False

    def _wait_next(self, bus, groups: int) -> bool:
        active, _ = self.pattern.resolve(max(self.cycle - 1, 0))
        interval = active.interval or self.interval
        if 0 < self.guard_delay < interval:
            if not self._sleep(self.guard_delay):
                return False
            bus.send(stop(0xFF, groups))    # suppress the factory autoplay
            return self._sleep(interval - self.guard_delay)
        return self._sleep(interval)

    # ---- worker ----

    def _run(self) -> None:
        rng = random.Random(self._seed)
        groups = max(len(self.boards), max(self.boards))
        while not self._stop.is_set():
            port = self.port or find_port()
            if not port:
                # Say it once. Standby waits for the port from boot, so a
                # host with no panels attached would otherwise write this
                # line every port_wait seconds for as long as it is up.
                if self.error != "no serial port":
                    self.emit("no serial port, waiting")
                self.error = "no serial port"
                if not self._sleep(self.port_wait):
                    break
                continue
            try:
                with self._open_bus(port) as bus:
                    self.emit(f"port {port}")
                    consecutive = 0
                    needs_setup = True
                    while not self._stop.is_set():
                        if not self._hold_while_paused():
                            break
                        if needs_setup and not self._setup(bus, groups):
                            consecutive += 1
                        elif self._cycle(bus, groups, rng):
                            consecutive = 0
                            needs_setup = False
                            self.error = None
                            if self._once:
                                self._settle(bus, groups)
                                self.standby_ready = True
                                self.emit("standby ready")
                                if not self._watch_link(bus, groups, port):
                                    return          # asked to stop
                                break               # link changed: repaint
                            if not self._wait_next(bus, groups):
                                break
                            continue
                        else:
                            consecutive += 1
                        # This cycle is lost; the panels keep the previous
                        # image. Re-initialise next time in case a board
                        # rebooted while it was unreachable.
                        self.failures += 1
                        needs_setup = True
                        if consecutive >= self.reopen_after_failures:
                            self.emit("reopening the bus")
                            break
                        if not self._sleep(self.reopen_delay):
                            break
            except Exception as exc:        # unplugged, permissions, ...
                self.error = str(exc)
                self.emit(f"ERROR bus {exc}")
            if not self._stop.is_set() and not self._sleep(self.reopen_delay):
                break
        self.emit("stopped")
