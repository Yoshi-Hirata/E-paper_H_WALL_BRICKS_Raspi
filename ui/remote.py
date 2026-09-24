"""Remote cues: the show PC loads a design, then fires it on the clock.

A garment changes in two steps, because only the last one can be timed:

  prepare   every board gets its 64-byte array saved to the slot - about
            0.2 s a board over the 9600 bps relay, retried like any demo
            cycle, reported board by board. Nothing changes on the glass.
  fire      one broadcast show, sent at an agreed instant of THIS unit's
            monotonic clock. The PC measures each unit's clock offset
            (ui/agent.py serves the clock), so ten units given "their"
            instant fire together. One frame, never repeated (see
            runner.SHOW_REPEATS).

The session is the mailbox between the HTTP agent's threads and the
runner's worker, which owns the serial port: the agent posts prepare /
fire / cancel, the worker (DemoRunner._run_remote) takes them and
reports back, and status() is what the PC and the LCD read.

The monotonic clock is used because the wall clock can step - timesyncd
is active on the units whenever they see the internet - and a step in
the middle of a show would move every cue.
"""

from __future__ import annotations

import threading
import time

IDLE = "idle"              # remote, nothing loaded
PREPARING = "preparing"    # saving the arrays to the boards
READY = "ready"            # saved; waiting for a fire time
ARMED = "armed"            # saved and a fire time is set
FIRED = "fired"            # the show went out
FAILED = "failed"          # nothing could be saved
STANDBY = "standby"        # asked for the white standby
LOCAL = "local"            # the unit is on its own menu

ARRAY_LEN = 64
TABLE_LEN = 128            # a delay table: 64 sockets x uint16, big-endian
DEV_NUMBER_BRAND = 0x03    # the layout every UI pattern sends (ui/patterns.py)
# A cue this close to its own fire time (ui/runner.py's _fire_at() busy-waits
# the last FIRE_SPIN_S = 0.02 s of it, then the broadcast send and the
# session.fired() call after it cost a few more ms) must not be displaced by
# a new prepare(): fired() matches on cue_id, and a prepare() landing in
# that window would move cue_id on before fired() ever gets to record it -
# the fire happens on the wire, but the session, and everything reading it
# (ShowPlayer._tally(), /status), never finds out. Found in the timing
# review, 2026-09-24, alongside the same race in ShowPlayer._plan()'s own
# scheduling (see its owned_unfired_elsewhere) - this is the general,
# session-level version of the same guard, covering every other caller too
# (an operator's own /prepare+/fire, not just a ShowPlayer-run show).
FIRE_IMMINENT_S = 0.05


class RemoteError(ValueError):
    """A request the unit cannot take; the agent answers 4xx with it."""


class RemoteSession:
    def __init__(self, runner, clock=time.monotonic, busy=None):
        self.runner = runner
        self._clock = clock
        # Set by the App: true while an OTA, a scan or a reboot owns the
        # unit - a cue must not pull the port from under a flash.
        self.busy = busy or (lambda: False)

        self.on_release = None         # set by the show player's owner
        self.active = False            # the unit is under remote control
        self.phase = LOCAL
        self.cue_id: str | None = None
        self.label = ""
        self.error: str | None = None
        self.saved: list[int] = []
        self.failed: list[int] = []
        self.fire_at: float | None = None
        self.fired_at: float | None = None
        self.prepare_s: float | None = None
        self._job: dict | None = None
        self._lock = threading.Lock()
        self._wake = threading.Event()

    # ---- called by the agent ----

    def prepare(self, cue_id: str, boards: "dict[int, bytes]",
                dev_type: int = DEV_NUMBER_BRAND, label: str = "",
                delays: "dict[int, bytes] | None" = None) -> None:
        """`delays`: per board, the 128-byte table (64 sockets x uint16,
        big-endian, 10 ms frames) of per-socket start delays that makes
        the change sweep the garment (written before the colours;
        boards without one keep what they have)."""
        if self.busy():
            raise RemoteError("unit is busy (firmware update, scan or reboot)")
        if not boards:
            raise RemoteError("no boards in the cue")
        for address, array in boards.items():
            if not 1 <= address <= 0xFE:
                raise RemoteError(f"board address {address} out of range")
            if len(array) != ARRAY_LEN:
                raise RemoteError(f"board {address}: array must be "
                                  f"{ARRAY_LEN} bytes, got {len(array)}")
        delays = {int(a): bytes(t) for a, t in (delays or {}).items()}
        for address, table in delays.items():
            if len(table) != TABLE_LEN:
                raise RemoteError(f"board {address}: delay table must be "
                                  f"{TABLE_LEN} bytes (64 sockets x uint16), "
                                  f"got {len(table)}")
        with self._lock:
            if (self.phase == ARMED and self.fire_at is not None
                    and self.fire_at - self._clock() <= FIRE_IMMINENT_S):
                # About to fire, or already sent and not yet tallied - see
                # FIRE_IMMINENT_S. Refused rather than silently dropped:
                # the caller (ShowPlayer never hits this - its own
                # scheduling already waits, see _plan()'s
                # owned_unfired_elsewhere - so in practice this is an
                # operator's own /prepare arriving a beat too soon) gets a
                # 409 and tries again a moment later, once fired() has run.
                raise RemoteError(f"cue {self.cue_id} is about to fire - "
                                  f"try again in a moment")
            self.active = True
            self.phase = PREPARING
            self.cue_id, self.label = str(cue_id), label
            self.error = None
            self.saved, self.failed = [], []
            self.fire_at = self.fired_at = self.prepare_s = None
            self._job = {"cue_id": self.cue_id, "boards": dict(boards),
                         "dev_type": dev_type, "delays": delays}
        if not self.runner.remote and self.runner.start_remote(self) is False:
            self.failed_with("bus busy: the previous worker has not finished")
        self._wake.set()

    def fire(self, cue_id: str, at: float) -> None:
        with self._lock:
            if str(cue_id) != self.cue_id:
                raise RemoteError(f"cue {cue_id} is not the one loaded "
                                  f"({self.cue_id})")
            if self.phase not in (PREPARING, READY, ARMED):
                raise RemoteError(f"cue {cue_id} cannot fire: {self.phase}")
            self.fire_at = float(at)
            if self.phase == READY:
                self.phase = ARMED
        self._wake.set()

    def cancel(self) -> None:
        """Forget the fire time; what is saved on the boards stays."""
        with self._lock:
            self.fire_at = None
            if self.phase == ARMED:
                self.phase = READY
        self._wake.set()

    def standby(self) -> None:
        if self.busy():
            raise RemoteError("unit is busy (firmware update, scan or reboot)")
        with self._lock:
            self.active = True
            self.phase = STANDBY
            self.cue_id, self.label, self.error = None, "", None
            self.fire_at = self.fired_at = None
            self._job = None
        self.runner.standby()

    def release(self) -> None:
        """Back to the unit's own menu (KEY2 on the REMOTE screen)."""
        if self.on_release is not None:
            self.on_release()           # a running show ends with it
        with self._lock:
            self.active = False
            self.phase = LOCAL
            self.fire_at = None
            self._job = None
        self.runner.stop()

    # ---- called by the runner's worker ----

    def wake(self) -> None:
        self._wake.set()

    def wait(self, timeout: float) -> None:
        self._wake.wait(max(0.0, timeout))
        self._wake.clear()

    def take_job(self) -> "dict | None":
        with self._lock:
            job, self._job = self._job, None
            return job

    def prepared(self, cue_id: str, saved, failed, seconds: float) -> None:
        with self._lock:
            if cue_id != self.cue_id or self._job is not None:
                return                  # a newer cue arrived meanwhile
            self.saved, self.failed = sorted(saved), sorted(failed)
            self.prepare_s = seconds
            if not saved:
                self.phase = FAILED
                self.error = "no board took the design"
            else:
                self.phase = ARMED if self.fire_at is not None else READY

    def due(self) -> "tuple[str, float] | None":
        """(cue, fire time) once the boards are loaded and a time is set."""
        with self._lock:
            if self.phase == ARMED and self.fire_at is not None:
                return self.cue_id, self.fire_at
            return None

    def fired(self, cue_id: str, at: float) -> None:
        with self._lock:
            if cue_id != self.cue_id:
                return
            self.fired_at = at
            self.phase = FIRED

    def failed_with(self, message: str) -> None:
        with self._lock:
            if self.phase in (PREPARING, READY, ARMED):
                self.phase = FAILED
            self.error = message

    # ---- what the PC and the LCD read ----

    def status(self) -> dict:
        runner = self.runner
        with self._lock:
            late_ms = (None if self.fired_at is None or self.fire_at is None
                       else round((self.fired_at - self.fire_at) * 1000, 1))
            return {
                "active": self.active, "phase": self.phase,
                "cue": self.cue_id, "label": self.label,
                "error": self.error or runner.error,
                "saved": list(self.saved), "failed": list(self.failed),
                "prepare_s": self.prepare_s,
                "fire_at": self.fire_at, "fired_at": self.fired_at,
                "late_ms": late_ms,
                "boards": runner.reported_boards, "live": list(runner.live),
                "no_sweep": sorted(runner.no_sweep),
                "standby_ready": bool(runner.standby_ready),
            }
