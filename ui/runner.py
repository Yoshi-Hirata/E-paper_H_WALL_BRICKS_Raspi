"""Background demo runner: drives the panels and reports progress.

Built for show operation, where the failure that matters is a frozen
panel waiting for a human. Nothing here is fatal - see docs/RELIABILITY.md
for the reasoning. The layers, outermost last:

  command  exponential backoff, long enough to outwait a repaint (9.8 s
           measured) and a busy board
  board    a board that does not answer is skipped, not fatal: the wall
           is built for 20 boards but must run with whatever subset is
           powered. Absent boards are re-probed periodically and join
           the show when they appear
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

DEFAULT_BOARDS = list(range(1, 21))   # the production wall: board IDs 1..20
ACK_SUCCESS, ACK_BUSY = 0x80, 0x82
SHOW_REPEATS = 3          # the show frame is broadcast, so never acknowledged
SHOW_GAP_S = 0.15
LINK_POLL_S = 2.0         # how often standby checks the panel link
LINK_GUARD_S = 60.0       # how often standby re-suppresses the autoplay
PROBE_SWEEPS = 3          # setup passes over the board list
PROBE_SWEEP_DELAY_S = 3.0 # between passes, so a repaint can finish meanwhile
REPROBE_INTERVAL_S = 60.0 # how often absent boards get another chance


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
                 link_token=device_token,
                 probe_sweeps: int = PROBE_SWEEPS,
                 probe_sweep_delay: float = PROBE_SWEEP_DELAY_S,
                 reprobe_interval: float = REPROBE_INTERVAL_S):
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
        self.probe_sweeps = probe_sweeps
        self.probe_sweep_delay = probe_sweep_delay
        self.reprobe_interval = reprobe_interval

        # Which of self.boards are actually answering. `live` keeps the
        # bus order of self.boards; `absent` boards cost one quick probe
        # per reprobe interval and nothing else.
        self.live: list[int] = []
        self.absent: set[int] = set()
        self._needs_cfg: set[int] = set()
        self._next_reprobe = 0.0

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
        # live/absent survive across starts on purpose: the wall does not
        # change because a different pattern was picked, and re-sweeping
        # eighteen empty sockets would hold the first frame for half a
        # minute every time KEY1 is pressed.
        self._needs_cfg = set()
        self._next_reprobe = 0.0
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

    def _request(self, bus, frame, label: str, attempts: int | None = None,
                 quiet: bool = False, bus_retries: int | None = None) -> bool:
        """Send until acknowledged, backing off between tries.

        The backoff has to be able to outwait a full repaint: a board is
        deaf for 9.8 s while its e-paper redraws, and that is a normal
        event, not a fault.

        `quiet` is for probes of boards that may simply not be there:
        no per-attempt log lines and no self.error, because a socket
        that is empty on purpose is bookkeeping, not a fault.
        """
        attempts = attempts or self.command_attempts
        busy_seen = 0
        for attempt in range(1, attempts + 1):
            if bus_retries is None:
                ack = bus.request(frame)
            else:
                ack = bus.request(frame, retries=bus_retries)
            if ack is not None and ack.src != frame.dest:
                # Board 20's relayed ACKs can arrive many seconds late,
                # landing in the receive window of whatever was asked
                # next - seen on hardware vouching for an empty socket.
                # An answer from the wrong board is no answer.
                ack = None
            if ack is not None and ack.cmd == ACK_SUCCESS:
                if attempt > 1 and not quiet:
                    self.emit(f"{label} ok after {attempt} tries")
                return True
            if ack is not None and ack.cmd == ACK_BUSY:
                busy_seen += 1
                if busy_seen > self.busy_attempts:
                    break
                if not self._sleep(self.busy_delay):
                    return False
                continue
            if attempt == 1 and not quiet:
                reason = "no ACK" if ack is None else f"NAK 0x{ack.cmd:02X}"
                self.emit(f"{label} {reason}, retrying")
            if attempt < attempts and not self._sleep(self._backoff(attempt)):
                return False
        if not quiet:
            self.error = f"{label}: gave up after {attempts}"
            self.emit(f"ERROR {label} gave up")
        return False

    # ---- board bookkeeping ----

    @staticmethod
    def _fmt_boards(boards) -> str:
        """Compress a board list for the LCD log: [2..19] -> "2-19"."""
        boards = sorted(boards)
        runs, start, prev = [], boards[0], boards[0]
        for b in boards[1:] + [None]:
            if b is not None and b == prev + 1:
                prev = b
                continue
            runs.append(str(start) if start == prev else f"{start}-{prev}")
            start = prev = b
        return ",".join(runs)

    def _probe(self, bus, board: int, groups: int) -> bool:
        """One quick chance for a board to answer: silence it, set the slot.

        A single short request, because during discovery most probes hit
        sockets that are empty on purpose - the full backoff ladder is
        reserved for boards that are known to be there.
        """
        if not self._request(bus, stop(board, groups), f"probe @{board:02d}",
                             attempts=1, quiet=True, bus_retries=1):
            return False
        return self._request(bus, slot_config(board, self.slot,
                                              group_count=groups),
                             f"cfg @{board:02d}")

    def _drop(self, board: int) -> None:
        """Stop bothering an unreachable board until a reprobe finds it."""
        if board in self.live:
            self.live.remove(board)
        self.absent.add(board)
        self._needs_cfg.discard(board)
        self.emit(f"board {board} dropped, will reprobe")

    def _reprobe(self, bus, groups: int) -> bool:
        """Give absent boards a quick chance to join; True if any did.

        This is how a board powered on after the show started still gets
        into it: one short probe per board per interval, so eighteen
        empty sockets cost about nine seconds a minute and a board that
        appears is drawing within a cycle.
        """
        if not self.absent or time.monotonic() < self._next_reprobe:
            return False
        self._next_reprobe = time.monotonic() + self.reprobe_interval
        joined = [board for board in sorted(self.absent)
                  if not self._stop.is_set()
                  and self._probe(bus, board, groups)]
        if not joined:
            return False
        self.absent -= set(joined)
        keep = set(self.live) | set(joined)
        self.live = [b for b in self.boards if b in keep]
        self._needs_cfg -= set(joined)      # _probe just configured them
        self.emit(f"board {self._fmt_boards(joined)} joined "
                  f"({len(self.live)}/{len(self.boards)})")
        return True

    # ---- one cycle ----

    def _setup(self, bus, groups: int) -> bool:
        """Silence playback and (re)configure every board that answers.

        A board that stays silent is skipped, not fatal: the wall is
        built for 20 boards but runs with whatever subset is powered.
        The probing sweeps the list a few times, because a present board
        is deaf for the 9.8 s of a repaint and the factory autoplay is
        repainting at power-on - one pass would misread it as absent.
        Stragglers go to `absent`, where the periodic reprobe picks
        them up if they ever appear.
        """
        bus.send(stop(0xFF, groups))
        time.sleep(0.3)
        known_absent = {b for b in self.boards if b in self.absent}
        pending = list(self.boards)
        found: list[int] = []
        for sweep in range(self.probe_sweeps):
            if not pending:
                break
            if sweep and not self._sleep(self.probe_sweep_delay):
                return False
            still = []
            for board in pending:
                if self._probe(bus, board, groups):
                    found.append(board)
                else:
                    still.append(board)
            # The extra sweeps exist to catch a fitted board that is deaf
            # mid-repaint. A board already known absent was not fitted a
            # moment ago, so it gets one pass here and the periodic
            # reprobe later - not three sweeps holding up the first frame.
            pending = [b for b in still if b not in known_absent]
        self.absent = set(self.boards) - set(found)
        self.live = [b for b in self.boards if b in set(found)]
        self._needs_cfg = set()
        self._next_reprobe = time.monotonic() + self.reprobe_interval
        if not self.live:
            self.error = "no boards answering"
            self.emit("ERROR no boards answering")
            return False
        if pending:
            self.emit(f"board {self._fmt_boards(pending)} absent, skipping")
        self.emit(f"panels online: {len(self.live)}/{len(self.boards)}")
        return True

    def _cycle(self, bus, groups: int, rng: random.Random) -> bool:
        self._reprobe(bus, groups)
        # A playlist hands back whichever pattern owns this cycle; a plain
        # pattern hands back itself.
        active, local_cycle = self.pattern.resolve(self.cycle)
        frame = active(local_cycle, self.boards, self.palette, rng)
        updated = 0
        skipped = False
        for board in list(self.live):
            if self._stop.is_set():
                return False
            if not self._request(bus, stop(board, groups), f"stop @{board:02d}"):
                # Unreachable after the full ladder: out of the loop, so
                # one dead board cannot freeze the other nineteen.
                self._drop(board)
                skipped = True
                continue
            if board in self._needs_cfg and not self._request(
                    bus, slot_config(board, self.slot, group_count=groups),
                    f"cfg @{board:02d}"):
                self._drop(board)
                skipped = True
                continue
            self._needs_cfg.discard(board)
            arr = build_hexagon_array(frame[board])
            if not self._request(bus, save_color(board, self.slot, arr, groups),
                                 f"save @{board:02d}", self.save_attempts):
                # Answering but not taking data - it may have rebooted, so
                # it needs its slot configured again before the next try.
                self._needs_cfg.add(board)
                skipped = True
                continue
            updated += 1
        if updated == 0:
            return False

        # Broadcast keeps the panels in step but is unacknowledged, so a
        # dropped frame would silently leave the old image up. Repeating is
        # harmless: a board ignores commands while it is already repainting.
        for _ in range(self.show_repeats):
            bus.send(show_single(0xFF, self.slot, groups))
            time.sleep(self.show_gap)

        self.cycle += 1
        if skipped:
            self.failures += 1
        label = "" if active is self.pattern else f" {active.label}"
        self.emit(f"cycle {self.cycle}{label} shown "
                  f"({updated}/{len(self.boards)})")
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
        for board in list(self.live):
            self._request(bus, stop(board, groups), f"stop @{board:02d}")

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

        Absent boards are reprobed on the same clock: one powered on
        during standby comes up playing its factory demo, so finding it
        is treated like a link change and the white gets repainted.
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
            if self._reprobe(bus, groups):
                self.standby_ready = False
                self.emit("new board joined, will re-blank")
                return True
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
