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
import struct
import sys
import threading
import time
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

from epaper.commands import (TEST_SLOT, clear_pipeline, save_color,
                             save_pipeline, show_single, slot_config, stop)
from epaper.protocol import ACK_INVALID_CMD, ACK_SUCCESS, DEV_NUMBER_BRAND

NO_DELAY = 0xFFFF          # in a show file's table: no delay for this socket
# "No sweep anywhere" as a table: what a cue that carries no delay table
# for a (board, slot) means (review finding F5, 2026-09-25) - the slot's
# pipeline is cleared (0x25) once and remembered in _delays_sent, so a
# re-upload that took every sweep off the timeline does not leave the
# previous upload's tables in the boards.
NO_TABLE = struct.pack(">64H", *([NO_DELAY] * 64))
from epaper.transport import Bus, find_port

from .config import LOG_HISTORY
from .patterns import DEFAULT_PALETTE, Pattern
from .remote import READY          # ui/remote.py imports nothing of ours

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
ARM_GRACE_S = 0.25        # how long a fresh worker waits for a cue's fire time
# A probe of a board that is not there costs a serial timeout - about
# 1.5 s, and up to 2.5 s across the setup's sweeps (radxa-01,
# 2026-09-25). A cue's trigger falling inside one is a late cue on
# stage (a unit that restarted mid-show fired its next cue 4 s late,
# behind the start-up probe of six absent boards), so no probe is begun
# while a trigger is due within this, and the probing sweep waits the
# trigger out and sends it first instead.
PROBE_HOLD_S = 3.0
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
        self.slot = slot           # the LOCAL pattern loop's working slot
        self._pattern_slot = slot  # ...restored by _start() after standby
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
        # Per (board, slot): slot 0x1B config already sent, and the
        # delay table (docs/FW_REQUEST_SEGMENT_DELAY.md) already there -
        # so an unchanged one is not written again. A board's reboot
        # wipes neither in flash (docs/MERIS_REPLY_3SLOT.pdf: 0x13/0x1F/
        # 0x1B persist across power cycles), but a board that dropped
        # out and reappeared is re-verified with a write anyway rather
        # than trusted blind - see _forget_board().
        self._cfg_done: set[tuple[int, int]] = set()
        self._delays_sent: dict[tuple[int, int], bytes] = {}
        # Per (board, slot): the (array, table) last burned there, so a
        # re-Upload of an unchanged show writes nothing (ui/showplay.py's
        # ShowPlayer.load() -> RemoteSession.burn() -> _run_burn()).
        self._burn_cache: dict[tuple[int, int], tuple] = {}
        self._next_reprobe = 0.0
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
        # A trigger sent from inside the probing (see PROBE_HOLD_S) owes
        # the same guard stop as any other; _run_remote() picks this up.
        self._guard_owed: "float | None" = None
        self._firing = False       # inside _fire_at(): never re-enter it
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
        worker finishes, leaving the panels holding white - always into
        slot 0, since that is what the master autoplays at power-up
        (docs/MERIS_REPLY_3SLOT.pdf): whatever comes back from an
        unplug/replug shows the same white, not a stale look.
        """
        from .patterns import STANDBY

        self.start(STANDBY, once=True, slot=0)

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
        self._cfg_done = set()
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

    def start(self, pattern: Pattern, once: bool = False,
              slot: "int | None" = None) -> bool:
        with self._control:
            return self._start(pattern, once, slot)

    def _start(self, pattern: Pattern, once: bool = False,
               slot: "int | None" = None) -> bool:
        if self.running:
            self.stop()
        self.pattern = pattern          # what the screen names, either way
        if not self._old_worker_gone():
            return False
        self.remote = None
        self._once = once
        self.pattern = pattern
        # standby() asks for slot 0 (docs/MERIS_REPLY_3SLOT.pdf: the
        # master's power-up autoplay is slot 0, so that is what "white
        # and quiet" has to mean); every other pattern uses the runner's
        # own configured slot, same as always.
        self.slot = self._pattern_slot if slot is None else slot
        self.cycle = 0
        self.caption = None
        self.failures = 0
        self.error = None
        # live/absent survive across starts on purpose: the wall does not
        # change because a different pattern was picked, and re-sweeping
        # eighteen empty sockets would hold the first frame for half a
        # minute every time KEY1 is pressed.
        self._cfg_done = set()
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

    def _trigger_due_soon(self) -> bool:
        """True when a show cue's trigger is due within the cost of one
        probe (PROBE_HOLD_S) - the probing must not start another one."""
        session = self.remote
        if session is None:
            return False
        due = session.due()
        return due is not None and due[1] - time.monotonic() <= PROBE_HOLD_S

    def _fire_before_probing(self, bus, groups: int) -> None:
        """Called between probes: a trigger due within one probe's cost
        is waited out precisely and sent first (_fire_at() spins the
        last 20 ms), and the probing goes on straight after. Nothing has
        to be probed or configured for a broadcast "show slot N" - the
        picture is already in the slot."""
        if self._firing or not self._trigger_due_soon():
            return
        due = self.remote.due()
        if due is not None and self._fire_at(bus, groups, self.remote, *due):
            self._guard_owed = time.monotonic() + self.guard_delay

    def _probe(self, bus, board: int, groups: int) -> bool:
        """One quick chance for a board to answer: silence it, set the slot.

        A single short request, because during discovery most probes hit
        sockets that are empty on purpose - the full backoff ladder is
        reserved for boards that are known to be there.
        """
        if not self._request(bus, stop(board, groups), f"probe @{board:02d}",
                             attempts=1, quiet=True, bus_retries=1):
            return False
        ok = self._request(bus, slot_config(board, self.slot,
                                            group_count=groups,
                                            dev_type=self._active_dev_type()),
                           f"cfg @{board:02d}")
        if ok:
            self._cfg_done.add((board, self.slot))
        return ok

    def _forget_board(self, board: int) -> None:
        """A board that failed to take data may have rebooted - which,
        even though 0x13/0x1F/0x1B all persist across a power cycle
        (docs/MERIS_REPLY_3SLOT.pdf), is reason enough to re-verify every
        one of its slots with a write rather than trust the caches."""
        self._cfg_done = {k for k in self._cfg_done if k[0] != board}
        self._delays_sent = {k: v for k, v in self._delays_sent.items()
                             if k[0] != board}
        self._burn_cache = {k: v for k, v in self._burn_cache.items()
                            if k[0] != board}

    def _forget_burned(self, board: int, slot: int) -> None:
        """A manual /prepare (always slot 19) just wrote over what a
        burn might have put there - the cache no longer knows what the
        board holds, so the next burn must not skip it."""
        self._burn_cache.pop((board, slot), None)

    def absent_snapshot(self) -> "set[int]":
        """A copy of `absent` for other threads (ui/showplay.py's burn
        gate, ui/app.py's DEMO screen): the worker changes the set in
        place, and iterating a live set from another thread can raise
        "set changed size during iteration" mid-show (review F10)."""
        with self._lock:
            return set(self.absent)

    def _drop(self, board: int) -> None:
        """Stop bothering an unreachable board until a reprobe finds it."""
        if board in self.live:
            self.live.remove(board)
        with self._lock:
            self.absent.add(board)
        self._forget_board(board)
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
        # Not while a cue is nearly due: a pass over six absent boards
        # is 15 s, and _fire_at() calls this from inside its own wait.
        # What is left unprobed stays absent for the next interval.
        joined = [board for board in sorted(self.absent)
                  if not self._trigger_due_soon()
                  and not self._stop.is_set()
                  and self._probe(bus, board, groups)]
        if not joined:
            return False
        with self._lock:
            self.absent -= set(joined)
        keep = set(self.live) | set(joined)
        self.live = [b for b in self.boards if b in keep]
        if self.explore:                    # look a little past the newcomer
            horizon = min(MAX_BOARD_ID, max(self.live) + EXPLORE_GAP)
            beyond = [b for b in DEFAULT_BOARDS if b > max(self.boards)
                      and b <= horizon]
            self.boards += beyond
            with self._lock:
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
        # Cheap to redo, so always re-verified on a bus reopen - unlike
        # _burn_cache, which is deliberately NOT cleared here: a burn is
        # expensive (docs/MERIS_REPLY_3SLOT.pdf says the write itself
        # persists across a power cycle), so only a board that actually
        # dropped out (_drop(), below) loses its place in that cache.
        self._cfg_done = set()
        self._delays_sent = {}
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
                self._fire_before_probing(bus, groups)
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
            if (board, self.slot) not in self._cfg_done and not self._request(
                    bus, slot_config(board, self.slot, group_count=groups,
                                     dev_type=active.dev_type),
                    f"cfg @{board:02d}"):
                self._drop(board)
                skipped = True
                continue
            self._cfg_done.add((board, self.slot))
            arr = active.array(frame[board])
            if not self._request(bus, save_color(board, self.slot, arr, groups,
                                                 dev_type=active.dev_type),
                                 f"save @{board:02d}", self.save_attempts):
                # Answering but not taking data - it may have rebooted, so
                # every one of its slots needs re-verifying before the
                # next try (_forget_board()).
                self._forget_board(board)
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

    # ---- remote cues and burns (ui/remote.py) ----

    def _save_one(self, bus, groups: int, slot: int, dev_type: int,
                 board: int, array: bytes,
                 table: "bytes | None" = None) -> bool:
        """Write one board's picture and its delay table into `slot`. No
        per-board stop first: 0x13 is pure storage and never needs the
        board silenced (docs/MERIS_REPLY_3SLOT.pdf) - unlike _cycle()'s
        live pattern loop, which still silences before it draws.

        No `table` means "no sweep": the slot's pipeline is cleared
        (NO_TABLE -> 0x25) unless _save_delays() remembers it already
        is, so a slot never keeps a sweep from a previous show or a
        previous manual cue (review finding F5, 2026-09-25)."""
        if board not in self.live:
            return False
        if table is None:
            table = NO_TABLE
        if (board, slot) not in self._cfg_done and not self._request(
                bus, slot_config(board, slot, group_count=groups,
                                 dev_type=dev_type), f"cfg @{board:02d}"):
            self._drop(board)
            return False
        self._cfg_done.add((board, slot))
        if not self._save_delays(bus, groups, board, table, dev_type, slot):
            self._forget_board(board)
            return False
        if not self._request(bus, save_color(board, slot, array, groups,
                                             dev_type=dev_type),
                             f"save @{board:02d}", self.save_attempts):
            self._forget_board(board)
            return False
        return True

    def _save_cue(self, bus, groups: int, job: dict) -> "tuple[list, list]":
        """Write one manual cue's arrays to its slot; (saved, failed) -
        the Designs tab and a demo row's one-shot preview (always slot
        19, DEFAULT_SLOT)."""
        dev_type, slot = job["dev_type"], job.get("slot", TEST_SLOT)
        delays = job.get("delays") or {}
        saved, failed = [], []
        for board in sorted(job["boards"]):
            if self._stop.is_set():
                break
            if self._save_one(bus, groups, slot, dev_type, board,
                              job["boards"][board], delays.get(board)):
                saved.append(board)
                self._forget_burned(board, slot)   # the burn cache is stale now
            else:
                failed.append(board)
        return saved, failed

    def _save_delays(self, bus, groups: int, board: int, table: bytes,
                     dev_type: int, slot: "int | None" = None) -> bool:
        """Give a board the sweep's delay table unless it already holds
        it (in this `slot`). The show file's table is 64 sockets of
        uint16, big-endian, already in the board's own unit - 10 ms
        frames (NO_DELAY: no delay given); the board takes it as V1.4's
        0x1F, and a table with no delays at all is 0x25 - forget the
        sweep. A board whose firmware does not know the commands is
        remembered and left alone: the picture still goes out, in
        socket order."""
        slot = self.slot if slot is None else slot
        if board in self.no_sweep or self._delays_sent.get((board, slot)) == table:
            return True
        values = struct.unpack(">64H", table)
        if all(v == NO_DELAY for v in values):
            frames = [clear_pipeline(board, slot, groups, dev_type=dev_type)]
            label = f"sweep off @{board:02d}"
        else:
            frames = list(save_pipeline(
                board, slot,
                [0 if v == NO_DELAY else v for v in values],
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
        self._delays_sent[(board, slot)] = table
        # Said once per table, so the operator can see on the unit's
        # log that the sweep really reached the board (a cached table
        # is not sent again and not announced again).
        if all(v == NO_DELAY for v in values):
            self.emit(f"board {board}: sweep cleared")
        else:
            timed = [v for v in values if v != NO_DELAY]
            self.emit(f"board {board}: sweep table saved, {len(timed)} sockets,"
                      f" last starts +{max(timed) * 10 / 1000:.2f} s")
        return True

    def _run_burn(self, bus, groups: int, session, burn_job: dict,
                  began: "float | None" = None) -> None:
        """Write every cue of a show into its own slot, in order,
        skipping whatever the cache already knows is there
        (ui/showplay.py's ShowPlayer.load()). Interruptible: a newer
        burn() or a cancel_burn() bumps the session's burn epoch, and
        this notices between boards, not just between cues, so
        /show/stop or a fresh /show/load never waits for the whole
        thing to finish - an interrupted burn ends "cancelled" with its
        reason, never "failed" (review round 2, 2026-09-25).

        `began` is when the worker took the job, so the time this
        reports is the one the operator waited - the probing before the
        first write is most of it on a wall with absent boards (the
        caller's _setup(), 22 s for 14 of them on radxa-01)."""
        epoch, dev_type = burn_job["epoch"], burn_job["dev_type"]
        done, failed = 0, []
        began = time.monotonic() if began is None else began
        pairs = [(cue, board) for cue in burn_job["cues"]
                 for board in sorted(cue["boards"])]
        self._probe_burn_boards(bus, groups, burn_job)
        probe_s = time.monotonic() - began
        for n, (cue, board) in enumerate(pairs):
            slot = cue["slot"]
            if self._stop.is_set() or not session.burn_current(epoch):
                # Taken off the port (KEY1 on a pattern, KEY2, a shutdown)
                # or superseded (a newer burn(), a cancel_burn()). The
                # burn is CANCELLED with its reason, not "failed": what
                # was never reached is not a board that refused a write,
                # and a "failed" would have the PC offer a force this
                # unit refuses (review finding F4 + round 2, 2026-09-25).
                # For a superseded burn this is a no-op (its epoch has
                # moved on) and the newer state stands.
                left = len(pairs) - n
                why = "the port was taken" if self._stop.is_set() else "stopped"
                session.burn_cancelled(epoch, f"interrupted: {why}")
                self.emit(f"burn interrupted: {done - len(failed)}/{len(pairs)}"
                          f" boards written, {left} left")
                return
            array = cue["boards"][board]
            # No table in the cue = "no sweep" (NO_TABLE, cleared once):
            # the same key whether the show file says so or says nothing.
            table = (cue.get("delays") or {}).get(board)
            if table is None:
                table = NO_TABLE
            if board not in self.live:
                failed.append((board, slot))
            elif self._burn_cache.get((board, slot)) == (array, table):
                pass                     # unchanged: nothing to write
            elif self._save_one(bus, groups, slot, dev_type, board,
                                array, table):
                self._burn_cache[(board, slot)] = (array, table)
            else:
                failed.append((board, slot))
            done += 1
            session.burn_progress(epoch, done, failed)
        session.burn_finished(epoch, failed, self._all_absent(pairs, failed))
        # Everything in one line, because the unit's log on the tile is
        # six entries deep and the "absent ... skipped" line above can
        # have scrolled off by the time a long burn ends.
        absent = [b for b in self.boards if b in self.absent]
        self.emit(f"burn done: {done - len(failed)}/{done} in "
                  f"{time.monotonic() - began:.1f} s (probe {probe_s:.1f} s, "
                  f"{len(self.live)} live boards, {len(absent)} absent"
                  + (f": {self._fmt_boards(absent)})" if absent else ")"))

    def _all_absent(self, pairs, failed) -> "str | None":
        """"none of its 16 boards answered", when that is what this burn
        found - the show's whole garment is dark (its feed is off, or it
        is not plugged in). The PC words that case its own way and lets
        the operator start the other units past it, which is why the
        unit says it rather than leaving the PC to guess from a list of
        pairs that happens to be complete: a wall whose boards all
        answered and all refused the write looks the same in `failed`
        and is NOT this."""
        if not failed or len(failed) != len(pairs):
            return None
        boards = sorted({board for board, _ in failed})
        if not all(board in self.absent for board in boards):
            return None
        return f"none of its {len(boards)} boards answered"

    def _probe_burn_boards(self, bus, groups: int, burn_job: dict) -> None:
        """Give every board this burn names, and that nothing is known
        about yet, one short probe before the first slot is written.

        Measured on radxa-01 (2026-09-25): a board that is not there
        costs about 1.5 s of serial timeout. Without this pass those
        timeouts landed inside the first slot's writes (14 unknown
        boards of a 16-board garment = 22 s on the first slot, 0.3 s on
        each of the next), so the PC's "writing 0/64" sat still for 22 s
        and then raced - and nothing said why. Paid once here instead,
        with the absent boards named in the log; a board that appears
        later is picked up by the periodic reprobe as always.
        """
        wanted = sorted({b for cue in burn_job["cues"]
                         for b in cue["boards"]})
        unknown = [b for b in wanted
                   if b not in self.live and b not in self.absent]
        for board in unknown:
            if self._stop.is_set():
                return
            if self._probe(bus, board, groups):
                if board not in self.live:
                    self.live = [b for b in self.boards if b in
                                 set(self.live) | {board}]
            else:
                with self._lock:
                    self.absent.add(board)
        gone = [b for b in wanted if b in self.absent]
        if gone:
            self.emit(f"{len(gone)} board{'' if len(gone) == 1 else 's'} "
                      f"absent ({self._fmt_boards(gone)}) - skipped")

    def _fire_at(self, bus, groups: int, session, cue_id: str, at: float,
                 slot: int, dev_type: int) -> bool:
        """Send the one broadcast "show slot" at monotonic time `at`.

        Sleeps most of the way and polls the last FIRE_SPIN_S, so the
        frame leaves within a millisecond or two of the instant; a time
        already past (a late command) fires at once and the lateness is
        what the session reports. Nothing is written here - the picture
        is already burned into `slot`.

        A show's cues can now be far apart (nothing to write ahead of
        time any more, so ui/showplay.py arms the next one the moment
        the current one applies, however far off its own instant is) -
        this call is where the runner would otherwise sit for that whole
        stretch, so it keeps reprobing (cheap: _reprobe() only touches
        the bus once every reprobe_interval) rather than only doing so
        between cues.
        """
        self._firing = True
        try:
            while True:
                remaining = at - time.monotonic()
                if remaining <= 0:
                    break
                if (self._stop.is_set()
                        or session.due() != (cue_id, at, slot, dev_type)):
                    return False                # stopped, cancelled or moved
                if remaining > FIRE_SPIN_S:
                    self._reprobe(bus, groups)
                    self._stop.wait(min(remaining - FIRE_SPIN_S, 0.05))
                else:
                    time.sleep(0.0005)
        finally:
            self._firing = False
        bus.send(show_single(0xFF, slot, groups, dev_type=dev_type))
        sent_at = time.monotonic()
        session.fired(cue_id, sent_at)
        self.cycle += 1
        self.emit(f"cue {cue_id} fired slot {slot} "
                  f"{(sent_at - at) * 1000:+.0f} ms")
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
                        # A job's own board list is applied BEFORE setup
                        # runs, so the very first prepare() (still holding
                        # the runner's construction-time board list) does
                        # not pay for a setup sweep of the wrong list and
                        # then a second one right after adjusting it.
                        job = session.take_job()
                        if job is not None:
                            guard_due = None    # the save's stops cover it
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
                                groups = max(len(self.boards), max(self.boards))
                                needs_setup = True
                            if needs_setup:
                                if not self._setup(bus, groups):
                                    session.failed_with(self.error
                                                        or "setup failed")
                                    break
                                needs_setup = False
                                self.error = None
                            began = time.monotonic()
                            saved, failed = self._save_cue(bus, groups, job)
                            took = time.monotonic() - began
                            session.prepared(job["cue_id"], saved, failed, took)
                            self.emit(f"cue {job['cue_id']} saved "
                                      f"{len(saved)}/{len(wanted)} in {took:.1f} s")
                            continue

                        # A burn's own board list (every board any of its
                        # cues names) is applied the same way, and for the
                        # same reason: the runner may still be holding its
                        # construction-time list (or a previous show's).
                        burn_job = session.take_burn_job()
                        if burn_job is not None:
                            # The clock the operator is watching starts
                            # HERE, not at the first write: the setup
                            # sweep below is what a wall with absent
                            # boards spends most of an Upload on.
                            burn_began = time.monotonic()
                            guard_due = None
                            wanted = sorted({b for cue in burn_job["cues"]
                                            for b in cue["boards"]})
                            if wanted and wanted != sorted(self.boards):
                                self.absent = {b for b in wanted
                                               if b in self.absent}
                                self.live = [b for b in self.live
                                             if b in wanted]
                                self.boards = wanted
                                self.explore = False
                                groups = max(len(self.boards), max(self.boards))
                                needs_setup = True
                            if needs_setup:
                                if not self._setup(bus, groups):
                                    if self.error != "no boards answering":
                                        # The setup itself was cut short
                                        # (a stop, a dead port): nothing
                                        # was tried and nothing says what
                                        # would have been written, so the
                                        # burn is CANCELLED with the
                                        # reason - never a "failed" over
                                        # pairs nobody reached, which the
                                        # PC would offer to force past
                                        # (review round 2, 2026-09-25).
                                        session.burn_cancelled(
                                            burn_job["epoch"],
                                            self.error or "setup failed")
                                        session.failed_with(self.error
                                                            or "setup failed")
                                        break
                                    # Every board of this show was
                                    # probed and none answered - a
                                    # garment whose feed is off, or one
                                    # that is not plugged in. That is a
                                    # KNOWN GAP, not an unknown one: the
                                    # burn below walks its whole list and
                                    # puts every pair down as absent, so
                                    # the operator can start the other
                                    # nine units past it (the conductor
                                    # asks first). needs_setup stays on,
                                    # so a feed switched on later is
                                    # found by the next pass.
                                    self.emit("no boards answering: the "
                                              "burn writes nothing")
                                else:
                                    needs_setup = False
                                    self.error = None
                            self._run_burn(bus, groups, session, burn_job,
                                           began=burn_began)
                            continue

                        if (needs_setup and session.phase == READY
                                and session.due() is None):
                            # ui/showplay.py's _send() arms the cue and
                            # sets its time in two steps (arm() may have
                            # to take the port first - which is what
                            # started this worker), so a moment's
                            # patience here is what lets the branch
                            # below see the time at all.
                            session.wait(ARM_GRACE_S)
                        late = session.due()
                        if (needs_setup and late is not None
                                and late[1] <= time.monotonic()):
                            # A unit that restarted in the middle of the
                            # show: its cue's trigger is already overdue,
                            # and a broadcast "show slot N" needs no
                            # board probed or configured (the picture is
                            # burned into the slot and the 0x1B with it).
                            # So it goes out BEFORE the probing sweep -
                            # 16 s of it with 6 absent boards on the real
                            # unit (2026-09-25), which on stage is the
                            # garment sitting on the wrong picture.
                            if self._fire_at(bus, groups, session, *late):
                                guard_due = time.monotonic() + self.guard_delay
                        if needs_setup:
                            # The one broadcast 0x17 for this port session
                            # (docs/MERIS_REPLY_3SLOT.pdf: one is enough
                            # after power-up) - _setup()'s own first move.
                            # Only reached with no job or burn pending: a
                            # due fire needs it just as much, but there is
                            # no board list of its own to adjust first.
                            if not self._setup(bus, groups):
                                session.failed_with(self.error
                                                    or "setup failed")
                                break           # reopen the bus and retry
                            needs_setup = False
                            self.error = None

                        due = session.due()
                        if due is not None:
                            if self._fire_at(bus, groups, session, *due):
                                guard_due = time.monotonic() + self.guard_delay
                            continue
                        now = time.monotonic()
                        if self._guard_owed is not None:
                            guard_due = (self._guard_owed if guard_due is None
                                         else min(guard_due, self._guard_owed))
                            self._guard_owed = None
                        if guard_due is not None and now >= guard_due:
                            # As after every demo cycle: a shown slot runs
                            # on into the factory autoplay unless stopped.
                            guard_due = None
                            bus.send(stop(0xFF, groups))
                        if self._reprobe(bus, groups):
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
        pending = session.take_burn_job()
        if pending is not None:
            # Queued after the last take_burn_job() and never started:
            # nothing of it is written, nothing was even tried, and
            # nobody else would ever say so - "cancelled", not a
            # "failed" the PC would offer to force past (review round 2).
            session.burn_cancelled(pending["epoch"],
                                   "the worker was stopped first")
            self.emit("burn never started: the worker was stopped first")
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
