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

from epaper.commands import (TEST_SLOT, clear_pipeline, save_color,
                             save_pipeline, show_single, slot_config, stop)
from epaper.protocol import ACK_INVALID_CMD, ACK_SUCCESS, DEV_NUMBER_BRAND

NO_DELAY = 0xFF            # in a show file's table: no delay for this socket
FRAMES_PER_UNIT = 10       # the table's 0.1 s in the board's 10 ms frames
from epaper.transport import Bus, find_port

from .config import LOG_HISTORY
from .patterns import DEFAULT_PALETTE, Pattern

# A garment carries up to 60 boards, addressed 1..n by rank. Without a
# list from the show PC the runner explores: it probes upwards from 1 and
# stops once EXPLORE_GAP addresses in a row past the last board that
# answered stay silent - so a 21-board garment costs ~27 probes, not 60,
# and a dead board in the middle (16 of 21) does not end the search.
# Until the first board answers it keeps going to 60: the dead ones may
# be the low addresses (2026-09-22: 1-11 and 19-20 off, 12-18 and 21 on).
MAX_BOARD_ID = 60
DEFAULT_BOARDS = list(range(1, MAX_BOARD_ID + 1))
EXPLORE_GAP = 6
ACK_SUCCESS, ACK_BUSY = 0x80, 0x82
# The show broadcast goes out exactly once. It is unacknowledged, and it
# is tempting to repeat it as insurance - but a board does NOT discard
# commands that arrive during its repaint: they queue in its receive
# buffer and run afterwards, so every extra copy is another full 16 s
# repaint. Measured on the production boards 2026-08-14: three copies
# made board 1 repaint twice (34 s) and board 20 finish at 35 s, while a
# single copy had both boards repainting in step (deaf 0.7-17.1 s and
# 1.2-17.3 s) - the whole "lag between boards" was this. A genuinely
# lost frame costs one cycle and the next one repairs it.
SHOW_REPEATS = 1
SHOW_GAP_S = 0.15
LINK_POLL_S = 2.0         # how often standby checks the panel link
LINK_GUARD_S = 60.0       # how often standby re-suppresses the autoplay
PROBE_SWEEPS = 3          # setup passes over the board list
PROBE_SWEEP_DELAY_S = 3.0 # between passes, so a repaint can finish meanwhile
REPROBE_INTERVAL_S = 60.0 # how often absent boards get another chance
FIRE_SPIN_S = 0.02        # the last stretch before a timed show is polled
# How long a new start waits for a worker that did not end within stop()'s
# timeout. One request into a wedged CDC can block for ~7.5 s (three
# tries of 2 s write timeout + 0.5 s read), and a save retries that.
OLD_WORKER_PATIENCE_S = 12.0


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
        self.explore = not boards            # no list given: find them
        self.boards = list(boards) if boards else list(DEFAULT_BOARDS)
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
        # Sweeps (docs/FW_REQUEST_SEGMENT_DELAY.md): the delay table each
        # board holds, so an unchanged one is not written again, and the
        # boards whose firmware answered "no such command".
        self._delays_sent: dict[int, bytes] = {}
        self.no_sweep: set[int] = set()

        self.log: deque[str] = deque(maxlen=LOG_HISTORY)
        self.pattern: Pattern | None = None
        # The session feeding cues while the show PC drives this unit
        # (ui/remote.py); None whenever the unit runs its own patterns.
        self.remote = None
        self._remote_dev_type = DEV_NUMBER_BRAND
        self.caption: str | None = None   # what the current cycle shows
        self._once = False
        self.standby_ready = False
        self.cycle = 0
        self.failures = 0          # cycles abandoned since the demo started
        self.started_at: float | None = None
        self.error: str | None = None
        self._thread: threading.Thread | None = None
        # A worker that outlived stop()'s join. While it lives the stop
        # flag stays set and nothing new is started: two workers on one
        # bus means two broadcast shows for one cue.
        self._lingering: threading.Thread | None = None
        self._control = threading.RLock()   # start / start_remote / stop
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

    def _old_worker_gone(self) -> bool:
        """True once no earlier worker can touch the bus any more."""
        old = self._lingering
        if old is not None and old.is_alive():
            old.join(timeout=OLD_WORKER_PATIENCE_S)
        if old is not None and old.is_alive():
            self.error = "bus busy: the previous worker has not finished"
            self.emit("ERROR previous worker still on the bus, not starting")
            return False
        self._lingering = None
        return True

    def start_remote(self, session) -> bool:
        """Hand the port to the show PC's cues (ui/remote.py).

        Same worker thread, different loop: instead of drawing a pattern
        every interval it saves what the session hands it and sends the
        show at the instant the session names. False if the bus is still
        held by a worker that would not stop.
        """
        with self._control:
            return self._start_remote(session)

    def _start_remote(self, session) -> bool:
        if self.running:
            self.stop()
        if not self._old_worker_gone():
            return False
        self.pattern = None
        self.remote = session
        self.cycle = 0
        self.caption = None
        self.failures = 0
        self.error = None
        self._needs_cfg = set()
        self._next_reprobe = 0.0
        self._stop.clear()
        self._pause.clear()
        self._elapsed_base = 0.0
        self.standby_ready = False
        self.started_at = time.monotonic()
        self.emit("start REMOTE")
        self._thread = threading.Thread(target=self._run_remote,
                                        args=(session,), daemon=True)
        self._thread.start()
        return True

    def start(self, pattern: Pattern, once: bool = False) -> bool:
        with self._control:
            return self._start(pattern, once)

    def _start(self, pattern: Pattern, once: bool = False) -> bool:
        if self.running:
            self.stop()
        self.pattern = pattern          # what the screen names, either way
        if not self._old_worker_gone():
            return False
        self.remote = None
        self._once = once
        self.pattern = pattern
        self.cycle = 0
        self.caption = None
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
        return True

    def stop(self, timeout: float = 5.0) -> None:
        with self._control:
            self._stop_locked(timeout)

    def _stop_locked(self, timeout: float) -> None:
        self._stop.set()
        self._pause.clear()          # let a paused worker notice the stop
        if self.remote is not None:
            self.remote.wake()       # ...and one waiting for a cue
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=timeout)
            if thread.is_alive():
                # Still inside a bus request. It will see the stop flag
                # when that returns - as long as nobody clears the flag,
                # which is what _old_worker_gone() guards.
                self._lingering = thread
        self.remote = None
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
        deaf for ~16 s while its e-paper redraws (production boards;
        9.8 s on the first generation), and that is a normal event, not
        a fault.

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

    def _active_dev_type(self) -> int:
        """Wire format of the pattern drawing this cycle (see Pattern)."""
        if self.pattern is None:
            return self._remote_dev_type        # a cue from the show PC
        active, _ = self.pattern.resolve(self.cycle)
        return active.dev_type

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
                                              group_count=groups,
                                              dev_type=self._active_dev_type()),
                             f"cfg @{board:02d}")

    def _drop(self, board: int) -> None:
        """Stop bothering an unreachable board until a reprobe finds it."""
        if board in self.live:
            self.live.remove(board)
        self.absent.add(board)
        self._needs_cfg.discard(board)
        self._delays_sent.pop(board, None)      # may come back rebooted
        self.emit(f"board {board} dropped, will reprobe")

    @property
    def expected(self) -> int:
        """How many boards the wall is taken to have: the highest that
        ever answered when exploring, the length of the list otherwise."""
        if self.explore:
            return max(self.live) if self.live else 0
        return len(self.boards)

    @property
    def reported_boards(self) -> "list[int]":
        """The board list as told to the LCD and the show PC: when
        exploring, 1 up to the highest board that answers."""
        if self.explore:
            return [b for b in self.boards if b <= self.expected]
        return list(self.boards)

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
        if self.explore:                    # look a little past the newcomer
            horizon = min(MAX_BOARD_ID, max(self.live) + EXPLORE_GAP)
            beyond = [b for b in DEFAULT_BOARDS if b > max(self.boards)
                      and b <= horizon]
            self.boards += beyond
            self.absent |= set(beyond)
        self.emit(f"board {self._fmt_boards(joined)} joined "
                  f"({len(self.live)}/{self.expected})")
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
        if self.explore:
            self.boards = list(DEFAULT_BOARDS)      # the search starts over
        known_absent = {b for b in self.boards if b in self.absent}
        pending = list(self.boards)
        found: list[int] = []
        for sweep in range(self.probe_sweeps):
            if not pending:
                break
            if sweep and not self._sleep(self.probe_sweep_delay):
                return False
            still = []
            # Until a board answers the whole range is searched: the
            # low addresses can be the ones that are dead (a power feed
            # off, a cable out) while the rest of the garment is fine.
            reach = MAX_BOARD_ID if self.explore and sweep == 0 else None
            for board in pending:
                if reach is not None and board > reach:
                    break                       # nothing for a while: the end
                if self._probe(bus, board, groups):
                    found.append(board)
                    if reach is not None:
                        reach = board + EXPLORE_GAP
                else:
                    still.append(board)
            if reach is not None:
                # Only what lies within reach is worth the extra sweeps
                # (a board mid-repaint); the rest is the empty end of the bus.
                self.boards = [b for b in self.boards
                               if b <= min(reach, MAX_BOARD_ID)]
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
        self.emit(f"panels online: {len(self.live)}/{self.expected}")
        return True

    def _cycle(self, bus, groups: int, rng: random.Random) -> bool:
        self._reprobe(bus, groups)
        # A playlist hands back whichever pattern owns this cycle; a plain
        # pattern hands back itself.
        active, local_cycle = self.pattern.resolve(self.cycle)
        self.caption = active.caption(local_cycle) if active.caption else None
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
                    bus, slot_config(board, self.slot, group_count=groups,
                                     dev_type=active.dev_type),
                    f"cfg @{board:02d}"):
                self._drop(board)
                skipped = True
                continue
            self._needs_cfg.discard(board)
            arr = active.array(frame[board])
            if not self._request(bus, save_color(board, self.slot, arr, groups,
                                                 dev_type=active.dev_type),
                                 f"save @{board:02d}", self.save_attempts):
                # Answering but not taking data - it may have rebooted, so
                # it needs its slot configured again before the next try.
                self._needs_cfg.add(board)
                skipped = True
                continue
            updated += 1
        if updated == 0:
            return False

        # One broadcast show keeps the panels in step. Never repeat it as
        # insurance: boards queue commands received mid-repaint and play
        # them back afterwards, so each extra copy is another full repaint
        # (see SHOW_REPEATS).
        for _ in range(self.show_repeats):
            bus.send(show_single(0xFF, self.slot, groups,
                                 dev_type=active.dev_type))
            time.sleep(self.show_gap)

        self.cycle += 1
        if skipped:
            self.failures += 1
        label = "" if active is self.pattern else f" {active.label}"
        note = f" {self.caption}" if self.caption else ""
        self.emit(f"cycle {self.cycle}{label}{note} shown "
                  f"({updated}/{self.expected})")
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

    # ---- remote cues (ui/remote.py) ----

    def _save_cue(self, bus, groups: int, job: dict) -> "tuple[list, list]":
        """Write one cue's arrays to the boards; (saved, failed).

        The per-board sequence is _cycle's - stop, slot config when the
        board may have rebooted, save - without the show: that goes out
        later, on the clock.
        """
        dev_type = job["dev_type"]
        saved, failed = [], []
        for board in sorted(job["boards"]):
            if self._stop.is_set():
                break
            if board not in self.live:
                failed.append(board)            # absent; the reprobe looks
                continue
            if not self._request(bus, stop(board, groups), f"stop @{board:02d}"):
                self._drop(board)
                failed.append(board)
                continue
            if board in self._needs_cfg and not self._request(
                    bus, slot_config(board, self.slot, group_count=groups,
                                     dev_type=dev_type), f"cfg @{board:02d}"):
                self._drop(board)
                failed.append(board)
                continue
            self._needs_cfg.discard(board)
            table = (job.get("delays") or {}).get(board)
            if table is not None and not self._save_delays(bus, groups, board,
                                                           table, dev_type):
                self._needs_cfg.add(board)
                failed.append(board)
                continue
            if not self._request(bus, save_color(board, self.slot,
                                                 job["boards"][board], groups,
                                                 dev_type=dev_type),
                                 f"save @{board:02d}", self.save_attempts):
                self._needs_cfg.add(board)
                failed.append(board)
                continue
            saved.append(board)
        return saved, failed

    def _save_delays(self, bus, groups: int, board: int, table: bytes,
                     dev_type: int) -> bool:
        """Give a board the sweep's delay table unless it already holds
        it. The show file says tenths of a second per socket (0xFF: no
        delay given); the board takes frames of 10 ms as V1.4's 0x1F,
        and a table with no delays at all is 0x25 - forget the sweep.
        A board whose firmware does not know the commands is remembered
        and left alone: the cue still goes out, in socket order."""
        if board in self.no_sweep or self._delays_sent.get(board) == table:
            return True
        if all(b == NO_DELAY for b in table):
            frames = [clear_pipeline(board, self.slot, groups, dev_type=dev_type)]
            label = f"sweep off @{board:02d}"
        else:
            frames = list(save_pipeline(
                board, self.slot,
                [0 if b == NO_DELAY else b * FRAMES_PER_UNIT for b in table],
                groups, dev_type=dev_type))
            label = f"sweep @{board:02d}"
        for frame in frames:
            ack = bus.request(frame)
            if (ack is not None and ack.src == board
                    and ack.cmd == ACK_INVALID_CMD):
                self.no_sweep.add(board)
                self.emit(f"board {board}: firmware without sweeps "
                          f"(0x{frame.cmd:02X} refused) - update it to V1.4")
                return True
            if not (ack is not None and ack.src == board
                    and ack.cmd == ACK_SUCCESS
                    or self._request(bus, frame, label, self.save_attempts)):
                return False
        self._delays_sent[board] = table
        return True

    def _fire_at(self, bus, groups: int, session, cue_id: str,
                 at: float) -> bool:
        """Send the one broadcast show at monotonic time `at`.

        Sleeps most of the way and polls the last FIRE_SPIN_S, so the
        frame leaves within a millisecond or two of the instant; a time
        already past (a late command) fires at once and the lateness is
        what the session reports.
        """
        while True:
            remaining = at - time.monotonic()
            if remaining <= 0:
                break
            if self._stop.is_set() or session.due() != (cue_id, at):
                return False                    # stopped, cancelled or moved
            if remaining > FIRE_SPIN_S:
                self._stop.wait(min(remaining - FIRE_SPIN_S, 0.05))
            else:
                time.sleep(0.0005)
        bus.send(show_single(0xFF, self.slot, groups,
                             dev_type=self._remote_dev_type))
        sent_at = time.monotonic()
        session.fired(cue_id, sent_at)
        self.cycle += 1
        self.emit(f"cue {cue_id} fired {(sent_at - at) * 1000:+.0f} ms")
        return True

    def _run_remote(self, session) -> None:
        guard_due = None
        while not self._stop.is_set():
            port = self.port or find_port()
            if not port:
                if self.error != "no serial port":
                    self.emit("no serial port, waiting")
                self.error = "no serial port"
                session.failed_with("no serial port")
                if not self._sleep(self.port_wait):
                    break
                continue
            try:
                with self._open_bus(port) as bus:
                    self.emit(f"port {port}")
                    needs_setup = True
                    groups = max(len(self.boards), max(self.boards))
                    while not self._stop.is_set():
                        job = session.take_job()
                        if job is not None:
                            guard_due = None    # the save's stops cover it
                            self._remote_dev_type = job["dev_type"]
                            wanted = sorted(job["boards"])
                            if wanted != sorted(self.boards):
                                # Another garment, another board list - but
                                # what is already known about a socket stays
                                # known: a board the standby sweep found
                                # empty gets one probe here, not three
                                # (29 s for 15 empty sockets on the bench,
                                # 2026-09-21).
                                self.absent = {b for b in wanted
                                               if b in self.absent}
                                self.live = [b for b in self.live
                                             if b in wanted]
                                self.boards = wanted
                                self.explore = False    # the show PC knows
                                needs_setup = True
                            groups = max(len(self.boards), max(self.boards))
                            began = time.monotonic()
                            if needs_setup and not self._setup(bus, groups):
                                session.failed_with(self.error or "setup failed")
                                break           # reopen the bus and retry
                            needs_setup = False
                            self.error = None
                            saved, failed = self._save_cue(bus, groups, job)
                            took = time.monotonic() - began
                            session.prepared(job["cue_id"], saved, failed, took)
                            self.emit(f"cue {job['cue_id']} saved "
                                      f"{len(saved)}/{len(wanted)} in {took:.1f} s")
                            continue
                        due = session.due()
                        if due is not None:
                            if self._fire_at(bus, groups, session, *due):
                                guard_due = time.monotonic() + self.guard_delay
                            continue
                        now = time.monotonic()
                        if guard_due is not None and now >= guard_due:
                            # As after every demo cycle: a shown slot runs
                            # on into the factory autoplay unless stopped.
                            guard_due = None
                            bus.send(stop(0xFF, groups))
                        if not needs_setup and self._reprobe(bus, groups):
                            pass                # joined boards take the next cue
                        wait = self.link_poll
                        if guard_due is not None:
                            wait = min(wait, max(0.0, guard_due - now))
                        session.wait(wait)
            except Exception as exc:        # unplugged, permissions, ...
                self.error = str(exc)
                self.emit(f"ERROR bus {exc}")
                session.failed_with(str(exc))
            if not self._stop.is_set() and not self._sleep(self.reopen_delay):
                break
        self.emit("stopped")

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
