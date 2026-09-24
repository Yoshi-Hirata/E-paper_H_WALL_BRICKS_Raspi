"""Remote cues: the show PC loads a design, then fires it on the clock.

Two independent things share this mailbox between the HTTP agent's
threads and the runner's worker, which owns the serial port:

  a manual cue      /prepare + /fire (the Designs tab, and a demo row's
                    one-shot preview): every board gets its 64-byte
                    array saved to a slot - about 0.2 s a board over
                    the 9600 bps relay - then one broadcast show, sent
                    at an agreed instant of THIS unit's monotonic
                    clock. Always slot 19 (DEFAULT_SLOT); see arm()
                    below for why a show's own cues never take this
                    path.

  a burn            ui/showplay.py's ShowPlayer.load() writes every
                    cue of a show into its OWN slot (1..18) up front,
                    once, well before the show runs - see burn() and
                    docs/MERIS_REPLY_3SLOT.pdf: a colour save (0x13), a
                    delay table (0x1F) and a slot's config (0x1B) all
                    persist across a power cycle, so this only ever
                    needs doing again when the show file itself
                    changes. Once burned, RUNNING a show is triggers
                    only: arm(cue_id, slot, ...) sets a fire time with
                    NOTHING to write - the picture is already on the
                    glass's own board, waiting in its slot - and the
                    worker's _fire_at() sends one broadcast "show slot
                    N" at the cue's instant. status()["burn"] is what
                    the PC and the LCD read to show "writing pictures
                    n/N" before a show can be started. Its "state":
                    "burning" -> "burned" (all written) or "failed"
                    (`failed` = [[board, slot], ...] not written - a
                    burn the worker had to abandon reports what was
                    left as failed too, so nothing sticks at
                    "burning"); cancel_burn() turns a burn in progress
                    into "cancelled". ShowPlayer pairs this with the
                    show it belongs to and adds "none" (nothing burned
                    for the loaded show since the unit started).

The monotonic clock is used for fire times because the wall clock can
step - timesyncd is active on the units whenever they see the internet
- and a step in the middle of a show would move every cue.
"""

from __future__ import annotations

import threading
import time

IDLE = "idle"              # remote, nothing loaded
PREPARING = "preparing"    # saving the arrays to the boards
READY = "ready"            # saved (or nothing to save); waiting for a fire time
ARMED = "armed"            # ready and a fire time is set
FIRED = "fired"            # the show went out
FAILED = "failed"          # nothing could be saved
STANDBY = "standby"        # asked for the white standby
LOCAL = "local"            # the unit is on its own menu

ARRAY_LEN = 64
TABLE_LEN = 128            # a delay table: 64 sockets x uint16, big-endian
DEV_NUMBER_BRAND = 0x03    # the layout every UI pattern sends (ui/patterns.py)
# host/epaper/commands.py's TEST_SLOT: slot 0 is the standby white, a show's
# cues take 1..18 (conductor/showfile.py), and this is what a manual
# /prepare (the Designs tab) or a demo row's one-shot preview still uses -
# it is never part of a show's own rotation (docs/MERIS_REPLY_3SLOT.pdf,
# the pre-burn redesign, 2026-09-24).
DEFAULT_SLOT = 19
# A cue this close to its own fire time (ui/runner.py's _fire_at() busy-waits
# the last FIRE_SPIN_S = 0.02 s of it, then the broadcast send and the
# session.fired() call after it cost a few more ms) must not be displaced by
# a new prepare()/arm(): fired() matches on cue_id, and one landing in that
# window would move cue_id on before fired() ever gets to record it - the
# fire happens on the wire, but the session, and everything reading it
# (ShowPlayer._tally(), /status), never finds out. Found in the timing
# review, 2026-09-24, alongside the same race in ShowPlayer._plan()'s own
# scheduling - this is the general, session-level version of the same
# guard, covering every other caller too (an operator's own /prepare+/fire,
# not just a ShowPlayer-run show).
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
        self.slot = DEFAULT_SLOT
        self.dev_type = DEV_NUMBER_BRAND
        self._job: dict | None = None

        # A show's own burn (see the module docstring): None means
        # "nothing has ever been burned this session".
        self.burn_state: str | None = None
        self.burn_done = 0
        self.burn_total = 0
        self.burn_failed: "list[tuple[int, int]]" = []
        # True once the worker walked the whole list (burn_finished()):
        # a "failed" without it is a burn the bus gave up on part way
        # (failed_with(): no port, bus busy, an exception) whose
        # `failed` list says nothing about what was never reached.
        self.burn_complete = False
        self._burn_job: dict | None = None
        self._burn_epoch = 0

        self._lock = threading.Lock()
        self._wake = threading.Event()

    # ---- called by the agent: a manual cue (always slot 19) ----

    def prepare(self, cue_id: str, boards: "dict[int, bytes]",
                dev_type: int = DEV_NUMBER_BRAND, label: str = "",
                delays: "dict[int, bytes] | None" = None,
                slot: int = DEFAULT_SLOT) -> None:
        """`delays`: per board, the 128-byte table (64 sockets x uint16,
        big-endian, 10 ms frames) of per-socket start delays that makes
        the change sweep the garment (written before the colours;
        boards without one keep what they have).

        Writing here (rather than through a burn) makes the slot's
        content unknown to the burn cache - see ui/runner.py's
        `_forget_burned()`, called for every board this saves."""
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
        cue_id, slot = str(cue_id), int(slot)
        with self._lock:
            self._refuse_if_imminent_locked(cue_id)
            self.active = True
            self.phase = PREPARING
            self.cue_id, self.label = cue_id, label
            self.error = None
            self.saved, self.failed = [], []
            self.fire_at = self.fired_at = self.prepare_s = None
            self.slot, self.dev_type = slot, dev_type
            self._job = {"cue_id": cue_id, "boards": dict(boards),
                         "dev_type": dev_type, "delays": delays, "slot": slot}
        if not self.runner.remote and self.runner.start_remote(self) is False:
            self.failed_with("bus busy: the previous worker has not finished")
        self._wake.set()

    def arm(self, cue_id: str, slot: int, dev_type: int = DEV_NUMBER_BRAND,
            label: str = "") -> None:
        """A cue already burned into `slot`: nothing to write, just a
        fire time to keep - used by ui/showplay.py while RUNNING. Skips
        PREPARING outright (there is no board write for the worker to
        do or report on)."""
        if self.busy():
            raise RemoteError("unit is busy (firmware update, scan or reboot)")
        cue_id, slot = str(cue_id), int(slot)
        with self._lock:
            self._refuse_if_imminent_locked(cue_id)
            self.active = True
            self.phase = READY
            self.cue_id, self.label = cue_id, label
            self.error = None
            self.saved, self.failed = [], []
            self.fire_at = self.fired_at = self.prepare_s = None
            self.slot, self.dev_type = slot, dev_type
            self._job = None
        if not self.runner.remote and self.runner.start_remote(self) is False:
            self.failed_with("bus busy: the previous worker has not finished")
        self._wake.set()

    def _refuse_if_imminent_locked(self, new_cue_id: str) -> None:
        if (new_cue_id != self.cue_id and self.phase == ARMED
                and self.fire_at is not None
                and self.fire_at - self._clock() <= FIRE_IMMINENT_S):
            # About to fire, or already sent and not yet tallied - see
            # FIRE_IMMINENT_S. Refused rather than silently dropped: the
            # caller (ShowPlayer never hits this - its own scheduling
            # already waits - so in practice this is an operator's own
            # /prepare arriving a beat too soon) gets a 409 and tries
            # again a moment later, once fired() has run.
            raise RemoteError(f"cue {self.cue_id} is about to fire - "
                              f"try again in a moment")

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

    # ---- called by the agent: burning a show's cues into their slots ----

    def burn(self, cues: "list[dict]", dev_type: int = DEV_NUMBER_BRAND) -> None:
        """Queue a burn: `cues` is [{"slot", "boards": {addr: bytes64},
        "delays": {addr: bytes128}}, ...], written in that order. Runs
        on the runner's worker, which owns the port; a later burn() or
        cancel_burn() (ShowPlayer.stop()) makes an in-flight one give up
        early - see ui/runner.py's _run_burn()."""
        if self.busy():
            raise RemoteError("unit is busy (firmware update, scan or reboot)")
        total = sum(len(c["boards"]) for c in cues)
        with self._lock:
            self._burn_epoch += 1
            epoch = self._burn_epoch
            self.burn_state = "burning"
            self.burn_done, self.burn_total, self.burn_failed = 0, total, []
            self.burn_complete = False
            self._burn_job = {"cues": cues, "dev_type": dev_type, "epoch": epoch}
        if not self.runner.remote and self.runner.start_remote(self) is False:
            with self._lock:
                self.burn_state = "failed"
            self.failed_with("bus busy: the previous worker has not finished")
            return
        self._wake.set()

    def cancel_burn(self) -> None:
        """Give up on a burn in progress; what is already written stays
        written (and the cache still knows it). The state becomes
        "cancelled" - never None, which would read as "nothing to worry
        about" to ShowPlayer's gate and let a half-written show START
        (review finding F1, 2026-09-25). A burn already burned/failed is
        left as it is: STOP on a running show must not unsay it."""
        with self._lock:
            self._burn_epoch += 1
            self._burn_job = None
            if self.burn_state == "burning":
                self.burn_state = "cancelled"
        self._wake.set()

    def burn_current(self, epoch: int) -> bool:
        """False once a newer burn() or a cancel_burn() has superseded
        the one the worker is (still) working through."""
        with self._lock:
            return self._burn_epoch == epoch

    def take_burn_job(self) -> "dict | None":
        with self._lock:
            job, self._burn_job = self._burn_job, None
            return job

    def burn_progress(self, epoch: int, done: int, failed) -> None:
        with self._lock:
            if epoch != self._burn_epoch:
                return
            self.burn_done = done
            self.burn_failed = list(failed)

    def burn_finished(self, epoch: int, failed) -> None:
        """The worker is done with this burn: `failed` is every (board,
        slot) not written - refused by the board, absent, or never
        reached because the worker was taken off the port mid-burn
        (ui/runner.py's _run_burn() reports the rest that way, so the
        state never sticks at "burning")."""
        with self._lock:
            if epoch != self._burn_epoch:
                return
            self.burn_failed = list(failed)
            self.burn_done = self.burn_total
            self.burn_complete = True
            self.burn_state = "failed" if failed else "burned"

    def burn_status(self) -> "dict | None":
        return self.burn_record()[0]

    def burn_record(self) -> "tuple[dict | None, bool]":
        """(burn_status(), burn_complete) read in one go - ShowPlayer's
        gate must not see a "failed" from one instant and the flag from
        another."""
        with self._lock:
            if self.burn_state is None:
                return None, False
            return ({"done": self.burn_done, "total": self.burn_total,
                     "failed": [list(bs) for bs in self.burn_failed],
                     "state": self.burn_state}, self.burn_complete)

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

    def due(self) -> "tuple[str, float, int, int] | None":
        """(cue, fire time, slot, dev_type) once a time is set."""
        with self._lock:
            if self.phase == ARMED and self.fire_at is not None:
                return self.cue_id, self.fire_at, self.slot, self.dev_type
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
            if self.burn_state == "burning":
                self.burn_state = "failed"

    # ---- what the PC and the LCD read ----

    def status(self) -> dict:
        runner = self.runner
        with self._lock:
            late_ms = (None if self.fired_at is None or self.fire_at is None
                       else round((self.fired_at - self.fire_at) * 1000, 1))
            burn = None
            if self.burn_state is not None:
                burn = {"done": self.burn_done, "total": self.burn_total,
                       "failed": [list(bs) for bs in self.burn_failed],
                       "state": self.burn_state}
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
                "burn": burn,
            }
