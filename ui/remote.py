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
                    (`failed` = [[board, slot], ...] not written, the
                    whole list walked - with a "reason" when the worker
                    can say more than the list does, e.g. "none of its
                    16 boards answered" for a garment with no power).
                    A burn that never got that far is "cancelled" with
                    a "reason" - cancel_burn() (STOP, no reason
                    needed), burn_cancelled() (the worker taken off the
                    port) or failed_with() (no port, a busy bus) - never
                    "failed", whose empty list would have the PC offer
                    a force over "0 board(s) not written" (review round
                    2, 2026-09-25). ShowPlayer pairs this with the show
                    it belongs to and adds "none" (nothing burned for
                    the loaded show since the unit started).

  a clear           the other end of a burn: clear() takes the show's
                    pictures back OUT of slots 1-18 once the show is
                    over (ui/showplay.py's `clear_after_show`), so the
                    garment's factory autoplay has nothing of the show
                    left to cycle through after the Radxa is unplugged.
                    Acked 0x14 per (board, slot), never 0x15; status()
                    ["clear"] is the progress and a clear that deleted
                    anything at all turns the burn record into
                    "cleared" - the START and PRESET gates then ask for
                    an Upload. NOTHING is repainted by it: the garment
                    keeps the last look it was shown for as long as it
                    has power.

The monotonic clock is used for fire times because the wall clock can
step - timesyncd is active on the units whenever they see the internet
- and a step in the middle of a show would move every cue.
"""

from __future__ import annotations

import math
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
# How long POST /bus/recover waits for the worker's answer. The recovery
# itself is bounded at about 11 s (ui/runner.py's _recover_bus()); this is
# that with room for the worker to reach the job, and a 409 saying so
# rather than an HTTP client left hanging.
RECOVER_WAIT_S = 15.0


class RemoteError(ValueError):
    """A request the unit cannot take; the agent answers 4xx with it."""


def _seconds(value) -> "float | None":
    """A finite, non-negative number of seconds, or None for "not said".
    Junk (a string, a dict, NaN, an infinity, a negative) reads as not
    said rather than raising: these are advisory - the guard STOP's
    timing, nothing the picture depends on - and a cue must never be
    refused over one. Infinity is not merely junk here: a guard that is
    never owed is the factory autoplay back on the wall, so it has to
    fall back to the flat delay like anything else unusable."""
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(seconds) or seconds < 0.0:
        return None
    return seconds


def _with_age(stall: "dict | None") -> "dict | None":
    """A copy of one of the runner's timestamped records (bus_stall,
    bus_recovery, resend) with `ago_s` filled in, or None.

    A copy, because the worker thread owns the original and goes on
    updating it while the PC reads this one. The age is worked out on
    the unit's own clock: the PC's differs, and this is one subtraction
    between two readings of the same one.
    """
    if not stall:
        return None
    at = stall.get("at")
    ago = None if not at else round(max(0.0, time.time() - float(at)), 1)
    return dict(stall, ago_s=ago)


class RemoteSession:
    def __init__(self, runner, clock=time.monotonic, busy=None):
        self.runner = runner
        self._clock = clock
        # Set by the App: true while an OTA, a scan or a reboot owns the
        # unit - a cue must not pull the port from under a flash.
        self.busy = busy or (lambda: False)
        # True while a show (or a stored demo) is being played or held -
        # set by ShowPlayer.__init__(), which is the only thing that
        # knows. A bus recovery reopens the port, so it never runs
        # inside a run however far off the next cue looks
        # (ui/runner.py's _recover_quiet()); with no player at all
        # nothing is ever being played, which is the safe reading.
        self.playing = lambda: False

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
        # How long the loaded cue takes to finish on the glass: its
        # refresh, plus the seconds its sweep spreads the scales over.
        # None means "this caller did not say" - an older conductor's
        # /prepare body, or a show file from before cues carried a span
        # - and the runner then falls back to its flat guard delay (see
        # ui/runner.py's _guard_for()).
        self.span_s: float | None = None
        self.refresh_s: float | None = None
        # What the unit's own landing check made of the last cue it
        # CHECKED (ui/runner.py's _verify_landing()): None until one has
        # been, then {"cue", "landed", "resent", "witness"} -
        #   landed "deaf" / "busy"  the witness board was repainting, so
        #                           the broadcast was taken; nothing to see
        #   landed "idle-after-resend"  even the one re-send could not be
        #                           confirmed - the garment may be showing
        #                           the previous picture
        #   landed "idle-not-repaired"  the broadcast was NOT taken and
        #                           the next cue was too close to send it
        #                           again (ui/runner.py's REPAIR_LATE_S) -
        #                           a known loss, shown red like the above
        #   landed "skipped"        not checked (no board, no table, or a
        #                           cue came due) - says nothing either way
        # `resent` is true when a second broadcast went out at all, which
        # is the operator's cue that the link dropped a frame.
        #
        # It names its own cue and OUTLIVES that cue: a show arms the
        # next cue within a tick of the last one applying, so a record
        # dropped or cleared on that would be a verdict nobody ever saw
        # (it takes a second or more to reach one). Replaced by the next
        # check, and forgotten by forget_verify() (below) - a new design,
        # a run started or stopped, standby, release.
        self.verify: dict | None = None
        # Which GENERATION of the glass the verdict belongs to, and the
        # generation a cue firing now belongs to. Everything that makes
        # what is on the boards stop being vouched for bumps glass_gen,
        # and the runner only skips re-checking a cue when the cue id AND
        # the generation both match - so ui/showplay.py's heal (the same
        # cue id, same generation) is skipped, while a new fire of the
        # same cue id after a STOP/START, or a looping demo coming round
        # to the same cue again, is checked like anything else (review
        # round 3). A heal's arm() deliberately does NOT bump it.
        self.glass_gen = 0
        self.verify_gen: int | None = None
        self._job: dict | None = None
        # A garment's board list waiting for the worker, with no job to
        # go with it - see set_boards().
        self._boards: "list[int] | None" = None

        # A show's own burn (see the module docstring): None means
        # "nothing has ever been burned this session".
        self.burn_state: str | None = None
        self.burn_done = 0
        self.burn_total = 0
        self.burn_failed: "list[tuple[int, int]]" = []
        # Why a burn ended as it did ("no serial port", "bus busy: ...",
        # "interrupted: ...", or - on a FAILED burn - "none of its 16
        # boards answered"), carried into the burn dict as "reason" so
        # the PC's tile can say it rather than only "cancelled" (review
        # round 2, 2026-09-25).
        self.burn_reason: str | None = None
        # True once the worker walked the whole list (burn_finished()):
        # a "failed" without it is a burn the bus gave up on part way
        # (failed_with(): no port, bus busy, an exception) whose
        # `failed` list says nothing about what was never reached.
        self.burn_complete = False
        self._burn_job: dict | None = None
        self._burn_epoch = 0

        # The clear after a show (see the module docstring). None means
        # "no clear has been asked for since the unit started", which is
        # every ordinary evening; the states are
        #   "clearing"  the worker is deleting the slots
        #   "cleared"   every slot of every live board is empty
        #   "failed"    some slots could not be deleted (`clear_failed`)
        #   "partial"   interrupted - a START, or the worker taken off
        #               the port - so some slots are empty and some are
        #               not. Either way the burn record reads "cleared"
        #               the moment one slot has really gone.
        self.clear_state: str | None = None
        self.clear_done = 0
        self.clear_total = 0
        self.clear_deleted = 0         # slots really deleted, for the gate
        self.clear_failed: "list[tuple[int, int]]" = []
        self.clear_reason: str | None = None
        self._clear_job: dict | None = None
        self._clear_epoch = 0
        # Which slots the clear in hand is about, so a second ask for the
        # SAME ones is a no-op rather than a restart - see clear().
        self._clear_slots: "list[int] | None" = None
        # POST /bus/recover, waiting for the worker (see recover_bus()).
        self._recover_job: "dict | None" = None

        self._lock = threading.Lock()
        self._wake = threading.Event()

    # ---- called by the agent: a manual cue (always slot 19) ----

    def prepare(self, cue_id: str, boards: "dict[int, bytes]",
                dev_type: int = DEV_NUMBER_BRAND, label: str = "",
                delays: "dict[int, bytes] | None" = None,
                slot: int = DEFAULT_SLOT,
                span_s: "float | None" = None,
                refresh_s: "float | None" = None) -> None:
        """`delays`: per board, the 128-byte table (64 sockets x uint16,
        big-endian, 10 ms frames) of per-socket start delays that makes
        the change sweep the garment (written before the colours; a
        board without one gets its slot's sweep cleared, once - see
        ui/runner.py's _save_one()).

        `span_s` / `refresh_s`: how long this cue needs to finish once
        it fires - the sweep's span and the refresh the panels take.
        Only the guard STOP uses them (ui/runner.py's _guard_for()), and
        a body that leaves them out keeps the old flat guard.

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
        span_s, refresh_s = _seconds(span_s), _seconds(refresh_s)
        with self._lock:
            self._refuse_if_imminent_locked(cue_id)
            self.active = True
            self.phase = PREPARING
            self.cue_id, self.label = cue_id, label
            self.error = None
            self.saved, self.failed = [], []
            self.fire_at = self.fired_at = self.prepare_s = None
            # Always forgotten here, even for the same cue id: a fresh
            # /prepare is the operator sending the design again and
            # wanting to know whether THAT one landed. (arm() is the
            # other way round - see its heal note.)
            self._forget_verify_locked()
            self.slot, self.dev_type = slot, dev_type
            self.span_s, self.refresh_s = span_s, refresh_s
            self._job = {"cue_id": cue_id, "boards": dict(boards),
                         "dev_type": dev_type, "delays": delays, "slot": slot,
                         "span_s": span_s}
        if not self.runner.remote and self.runner.start_remote(self) is False:
            self.failed_with("bus busy: the previous worker has not finished")
        self._wake.set()

    def arm(self, cue_id: str, slot: int, dev_type: int = DEV_NUMBER_BRAND,
            label: str = "", span_s: "float | None" = None,
            refresh_s: "float | None" = None) -> None:
        """A cue already burned into `slot`: nothing to write, just a
        fire time to keep - used by ui/showplay.py while RUNNING. Skips
        PREPARING outright (there is no board write for the worker to
        do or report on).

        `span_s` / `refresh_s` are the cue's own, from the show file, and
        only the guard STOP reads them (see prepare()).

        The landing check's verdict is NOT cleared here, whichever cue is
        armed: it names the cue it is about and takes a second or more to
        arrive, while a running show arms the next cue within a tick of
        the last one applying - clearing it here would throw away every
        verdict a show ever reaches, and blink a red "not applied" off
        the operator's tile the moment the next cue is armed. The next
        check replaces it (ui/runner.py's _verify_landing(), which also
        reads it: a heal of a cue already confirmed is not re-checked)."""
        if self.busy():
            raise RemoteError("unit is busy (firmware update, scan or reboot)")
        cue_id, slot = str(cue_id), int(slot)
        span_s, refresh_s = _seconds(span_s), _seconds(refresh_s)
        with self._lock:
            self._refuse_if_imminent_locked(cue_id)
            self.active = True
            self.phase = READY
            self.cue_id, self.label = cue_id, label
            self.error = None
            self.saved, self.failed = [], []
            self.fire_at = self.fired_at = self.prepare_s = None
            self.slot, self.dev_type = slot, dev_type
            self.span_s, self.refresh_s = span_s, refresh_s
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
            self.span_s = self.refresh_s = None
            self._forget_verify_locked()
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
            self.span_s = self.refresh_s = None
            # The unit is its own again: the last cue's verdict is not
            # about anything anyone is driving (review round 3 - it used
            # to be left standing here despite what the commit said).
            self._forget_verify_locked()
            self._job = None
            # ...and the show's board list with it.
            self._boards = None
        self.runner.stop()
        # The unit is its own again from this moment, not from whenever
        # somebody next presses KEY1: a bench unit released to the MENU
        # used to go on reporting the show's 16 boards for ever (review,
        # 2026-09-27). After stop(), so the worker is not holding the
        # list it is being relieved of.
        self.runner.take_own_boards()

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
            self.burn_reason = None
            self.burn_complete = False
            self._burn_job = {"cues": cues, "dev_type": dev_type, "epoch": epoch}
            # A burn supersedes any clear outright: a queued one must not
            # delete the pictures this is writing, and a finished one is
            # no longer what the slots hold - a re-Upload is exactly how
            # the operator undoes a clear, so nothing of it may survive
            # into the new burn's state.
            self._clear_epoch += 1
            self._clear_job = None
            self._clear_slots = None
            self.clear_state = self.clear_reason = None
            self.clear_done = self.clear_total = self.clear_deleted = 0
            self.clear_failed = []
        if not self.runner.remote and self.runner.start_remote(self) is False:
            # Not "failed": nothing was even attempted, so the failed
            # list would be empty and the PC would offer a force over
            # "0 board(s) not written" that this unit refuses anyway -
            # failed_with() cancels it with the reason (review round 2).
            self.failed_with("bus busy: the previous worker has not finished")
            return
        self._wake.set()

    def cancel_burn(self, reason: "str | None" = None) -> None:
        """Give up on a burn in progress; what is already written stays
        written (and the cache still knows it). The state becomes
        "cancelled" - never None, which would read as "nothing to worry
        about" to ShowPlayer's gate and let a half-written show START
        (review finding F1, 2026-09-25) - and never "failed", which the
        PC would offer to force past. A burn already burned/failed is
        left as it is: STOP on a running show must not unsay it.

        `reason` (None for the operator's own STOP, which needs none) is
        what the PC's tile shows after "cancelled:"."""
        with self._lock:
            self._burn_epoch += 1
            self._burn_job = None
            if self.burn_state == "burning":
                self.burn_state = "cancelled"
                self.burn_reason = reason
        self._wake.set()

    def burn_cancelled(self, epoch: int, reason: str) -> None:
        """The worker gave up on the burn it was working through (the
        port taken, a shutdown): "cancelled" with its reason, not
        "failed" - nothing says what was never reached, so there is
        nothing for the PC to force past (review round 2, 2026-09-25)."""
        with self._lock:
            if epoch != self._burn_epoch:
                return              # superseded: the newer state stands
            self.burn_state = "cancelled"
            self.burn_reason = reason
            self.burn_complete = False
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
            if self.burn_state == "cancelled":
                # The worker IS writing (an unsuperseded epoch is the
                # proof - cancel_burn() bumps it), so a "cancelled" from
                # a moment when the port was missing is out of date.
                self.burn_state, self.burn_reason = "burning", None

    def burn_finished(self, epoch: int, failed,
                      reason: "str | None" = None) -> None:
        """The worker walked the whole list: `failed` is every (board,
        slot) not written - refused by the board, or absent. A burn that
        did NOT get that far ends at burn_cancelled() instead, so a
        "failed" always names every pair it is about.

        `reason` is set only where the worker can say something the list
        of pairs does not - "none of its 16 boards answered" for a
        garment that is simply not powered (ui/runner.py's
        _all_absent())."""
        with self._lock:
            if epoch != self._burn_epoch:
                return
            self.burn_failed = list(failed)
            self.burn_done = self.burn_total
            self.burn_complete = True
            self.burn_state = "failed" if failed else "burned"
            self.burn_reason = reason if failed else None

    def burn_status(self) -> "dict | None":
        return self.burn_record()[0]

    def burn_record(self) -> "tuple[dict | None, bool]":
        """(burn_status(), burn_complete) read in one go - ShowPlayer's
        gate must not see a "failed" from one instant and the flag from
        another."""
        with self._lock:
            if self.burn_state is None:
                return None, False
            return self._burn_dict_locked(), self.burn_complete

    # ---- called by the player: taking the pictures back out (0x14) ----

    def clear(self, slots) -> None:
        """Queue a clear of `slots` (1-18) on every board of the garment.

        The worker waits for the last cue's guard before the first 0x14 -
        a delete landing inside a repaint is the one thing this must not
        do - and then deletes one (board, slot) at a time, acked, with
        the same retry ladder as a save (ui/runner.py's _run_clear()).

        Nothing is REPAINTED: no 0x1D, no standby, slot 0 is not shown.
        The garment keeps the last look it was given for as long as it
        has power; what is taken away is only what the factory autoplay
        would find in the slots once the Radxa is gone. 0x15 (全消去) is
        forbidden and never used (docs/SPECIFICATION.md 2.4).
        """
        if self.busy():
            raise RemoteError("unit is busy (firmware update, scan or reboot)")
        slots = sorted({int(s) for s in slots})
        if not slots:
            raise RemoteError("no slots to clear")
        for slot in slots:
            if not 1 <= slot <= 18:
                raise RemoteError(f"slot {slot} is not a show slot (1-18): "
                                  f"0 is the standby white and 19 the "
                                  f"manual slot, and neither is cleared")
        with self._lock:
            if self.clear_state == "clearing" and self._clear_slots == slots:
                # The same clear is already queued or on the bus. Asking
                # twice happens by design - the unit's own STOP queues one
                # and the conductor's POST /show/clear lands a few ms
                # later - and bumping the epoch here would abort that walk
                # part way and start all 288 pairs again, with every slot
                # the first walk already emptied answering NAK and reading
                # as a failure (review, 2026-09-27).
                return
            self._clear_epoch += 1
            epoch = self._clear_epoch
            self.clear_state = "clearing"
            self.clear_done = self.clear_total = self.clear_deleted = 0
            self.clear_failed = []
            self.clear_reason = None
            self._clear_slots = list(slots)
            self._clear_job = {"slots": slots, "epoch": epoch}
        if not self.runner.remote and self.runner.start_remote(self) is False:
            self.clear_cancelled(epoch, "bus busy: the previous worker has "
                                        "not finished")
            return
        self._wake.set()

    def cancel_clear(self, reason: "str | None" = None) -> None:
        """Give up on a clear - a START taking the pictures back.

        The line is whether the WORKER HAS THE JOB, not whether a slot
        has been recorded as deleted yet:

        * still queued - the job is taken off the queue here and not one
          0x14 ever went out, so the slots are exactly as they were: the
          state goes back to None and START passes its gate as it always
          did;
        * already on the bus - the worker stops after the slot it is on,
          and that slot can land AFTER this returns. So the pictures stop
          being something START may run the moment the job was taken,
          whether or not a delete has been recorded: the state is
          "partial" and the burn record "cleared" ("cleared partially" as
          its reason). Deciding this on `clear_deleted` instead left a
          window of one 0x14 - 50-250 ms on the relay - in which START
          was waved through and a slot then went anyway, so the garment's
          first look was missing on one board.
        """
        with self._lock:
            self._clear_epoch += 1
            queued, self._clear_job = self._clear_job, None
            if self.clear_state != "clearing":
                return              # finished, failed, or never asked for
            if queued is not None:
                self.clear_state = self.clear_reason = None
                self._clear_slots = None
                self.clear_done = self.clear_total = self.clear_deleted = 0
                self.clear_failed = []
            else:
                self.clear_state = "partial"
                self.clear_reason = reason or "interrupted"
                self._mark_cleared_locked()
        self._wake.set()

    def clear_current(self, epoch: int) -> bool:
        """False once a cancel_clear() or a newer clear() has superseded
        the one the worker is still working through."""
        with self._lock:
            return self._clear_epoch == epoch

    def clear_pending(self) -> bool:
        """True while a clear is queued and has not been taken yet - the
        worker shortens its idle wait for it, so a clear held back only
        by the guard floor starts the moment that passes."""
        with self._lock:
            return self._clear_job is not None

    def take_clear_job(self) -> "dict | None":
        with self._lock:
            job, self._clear_job = self._clear_job, None
            return job

    # ---- POST /bus/recover ----

    def recover_bus(self, timeout: float = RECOVER_WAIT_S) -> dict:
        """Get the bus working again, and say what it took.

        The worker thread owns the port, so this queues the job, wakes
        it and waits for the answer - the same shape as a clear, with a
        reply instead of a progress record. Answers {"recovered", "by"
        ("padding" | "reopen" | null), "before_ms", "after_ms"}; `by`
        null with recovered true is "there was nothing wrong", which is
        the honest answer to a button pressed on a healthy unit.

        Refused (409) while anything else could want the bus - see
        recover_refusal(), which the worker asks AGAIN when it takes the
        job: the gate can close in the moment between the two (a START
        arriving, a picture beginning), and it is the later answer that
        counts.
        """
        refusal = self.recover_refusal()
        if refusal:
            raise RemoteError(refusal)
        done = threading.Event()
        job = {"done": done, "result": None}
        with self._lock:
            self._recover_job = job
        # Nobody on the port at all (the unit on its own menu): the same
        # move a clear makes - the worker that does this work is started,
        # and it picks the job up as soon as it has the bus.
        if not self.runner.remote and self.runner.start_remote(self) is False:
            with self._lock:
                self._recover_job = None
            raise RemoteError("bus busy: the previous worker has not finished")
        self._wake.set()
        if not done.wait(max(0.0, timeout)):
            with self._lock:
                if self._recover_job is job:
                    self._recover_job = None
            raise RemoteError("the worker did not answer in time")
        result = dict(job["result"] or {})
        if result.get("error"):
            # The worker found a gate closed when it came to the job.
            raise RemoteError(result["error"])
        return result

    def recover_refusal(self) -> "str | None":
        """Why a bus recovery may not run now, or None when it may.

        A recovery reopens the port and sends frames, so it stands aside
        for everything a cue or a picture could be hurt by (review F6):

        * an OTA, a scan or a reboot owning the unit;
        * a show being played or held;
        * a cue armed within the runner's recover_quiet (60 s);
        * the last cue's picture still drawing - the runner's
          `_guard_floor`, its refresh plus its span: a STOP into a sweep
          leaves the change half-drawn, and a master busy repainting reads
          as a stall and would be "recovered" by a reopen it never needed.

        The runner's numbers are read off the runner rather than imported:
        this module imports nothing of ours (see ui/runner.py's import of
        READY), and a test that compresses them compresses these gates.
        """
        if self.busy():
            return "unit is busy (firmware update, scan or reboot)"
        if self.playing():
            return "a show is running - stop it first"
        with self._lock:
            at = self.fire_at if self.phase == ARMED else None
        if at is not None:
            left = at - self._clock()
            if left < self.runner.recover_quiet:
                return (f"a cue fires in {max(0.0, left):.0f} s - "
                        f"stop the show first")
        floor = getattr(self.runner, "_guard_floor", None)
        if floor is not None:
            left = floor - time.monotonic()
            if left > 0:
                return (f"a repaint is in progress - try again in "
                        f"{math.ceil(left)} s")
        return None

    def take_recover_job(self) -> "dict | None":
        with self._lock:
            job, self._recover_job = self._recover_job, None
            return job

    def recovered(self, job: dict, result: dict) -> None:
        """The worker's answer to one recover_bus(); wakes the waiter."""
        job["result"] = dict(result)
        job["done"].set()

    def clear_started(self, epoch: int, total: int) -> None:
        """How many (board, slot) pairs this clear is about - the worker
        knows the garment's list, the caller does not."""
        with self._lock:
            if epoch != self._clear_epoch:
                return
            self.clear_total = int(total)

    def clear_progress(self, epoch: int, done: int, failed) -> None:
        with self._lock:
            if epoch != self._clear_epoch:
                return
            self.clear_done = done
            self.clear_failed = list(failed)
            self.clear_deleted = done - len(self.clear_failed)
            if self.clear_deleted:
                # The FIRST slot that really goes is what makes the show
                # unrunnable: said here rather than at the end, so a
                # START landing in the middle of a clear is refused on
                # "cleared" instead of running a garment whose pictures
                # are half gone (the operator's rule, 2026-09-27).
                self._mark_cleared_locked()

    def clear_finished(self, epoch: int, failed) -> None:
        """The worker walked the whole list: `failed` is every (board,
        slot) it could not delete - refused by the board, or absent."""
        with self._lock:
            if epoch != self._clear_epoch:
                return
            self.clear_failed = list(failed)
            self.clear_done = self.clear_total
            self.clear_deleted = self.clear_done - len(self.clear_failed)
            self.clear_state = "failed" if failed else "cleared"
            self.clear_reason = None
            self._mark_cleared_locked()

    def clear_cancelled(self, epoch: int, reason: str) -> None:
        """The worker gave up on the clear it was working through, or
        never got to start it (the port taken, a shutdown, no bus at
        all). A cancel_clear() has already written its own verdict, so a
        superseded epoch is a no-op here."""
        with self._lock:
            if epoch != self._clear_epoch:
                return              # superseded: the newer state stands
            # Nothing is left for a worker to pick up: the state below is
            # the last word on this clear.
            self._clear_job = None
            self.clear_state = "partial"
            self.clear_reason = reason
            if self.clear_deleted:
                self._mark_cleared_locked()
        self._wake.set()

    def _mark_cleared_locked(self) -> None:
        """The pictures are out of the slots, so the burn record says so:
        both gates (ui/showplay.py's _burn_gate(), the PC's own) then
        refuse with "Upload again", and a restart reads the same thing
        back off show-burn.json. A PARTIAL clear carries its own reason,
        which is what the PC's tile turns into "cleared partially"."""
        self.burn_state = "cleared"
        self.burn_complete = True
        self.burn_done = self.burn_total
        self.burn_failed = []
        self.burn_reason = ("cleared partially"
                           if self.clear_state == "partial" else None)

    def clear_record(self) -> dict:
        """What the PC and the LCD read. Always a dict (state "none"
        until one is asked for), unlike `burn`: an agent too old to
        clear has no "clear" key at all, and the conductor tells that
        apart by the 404 its POST /show/clear gets."""
        with self._lock:
            return self._clear_dict_locked()

    def _clear_dict_locked(self) -> dict:
        out = {"state": self.clear_state or "none",
               "done": self.clear_done, "total": self.clear_total,
               "failed": [list(pair) for pair in self.clear_failed]}
        if self.clear_reason:
            out["reason"] = self.clear_reason
        return out

    def _burn_dict_locked(self) -> dict:
        """The burn as the PC and the LCD read it. "reason" is only
        there when there is one (a cancelled burn, or a clear that was
        interrupted part way), so a plain burned / failed / burning /
        cleared dict keeps the exact shape it always had."""
        burn = {"done": self.burn_done, "total": self.burn_total,
                "failed": [list(bs) for bs in self.burn_failed],
                "state": self.burn_state}
        if self.burn_reason:
            burn["reason"] = self.burn_reason
        return burn

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

    def set_boards(self, boards) -> None:
        """The garment's board list, with nothing to write.

        A show file carries the list its unit was built for
        (conductor/showfile.py's `boards`), and ui/showplay.py hands it
        over whenever a show becomes this unit's current one. Normally a
        job brings the same list along, but a show that is RESUMED after
        a restart has no job at all - ShowPlayer.restore() never
        re-burns, so nothing but this tells the runner which sockets
        exist. radxa-04, 2026-09-26: it came back mid-show, kept the
        standby discovery's 17-22 in `absent` and probed those six empty
        sockets every minute for the rest of the show.

        Queued, not applied here: the worker picks it up the way it
        picks up a job (ui/runner.py's _run_remote()), so the list
        arrives between two cues and never inside one. A unit that is
        not on the show PC's port yet (restore() at start-up, before the
        first arm()) applies it when the worker starts."""
        wanted = sorted({int(b) for b in boards})
        if not wanted:
            return                      # a show with no boards says nothing
        with self._lock:
            self._boards = wanted
        # Deliberately no wake(): there is nothing to hurry for (the
        # worker reads this at the top of every pass, and the cue that
        # matters wakes it itself), and a wake left standing here would
        # be spent on the ARM_GRACE_S wait that lets an overdue cue's
        # fire time arrive before the probing sweep - a restored show
        # would go back to painting the wall after the sweep instead of
        # before it (ui/runner.py's _run_remote()).

    def take_boards(self) -> "list[int] | None":
        with self._lock:
            boards, self._boards = self._boards, None
            return boards

    def pending_job(self) -> bool:
        """True while a prepare or a burn is waiting for the worker.

        The worker's idle-time autoplay guard (ui/runner.py's
        REMOTE_GUARD_S heartbeat) stands aside for one: the writes are
        about to start and the heartbeat is only worth sending when
        nothing else is going to touch the bus anyway. A queued board
        list (set_boards()) is not one of these - it puts nothing on the
        bus of its own, and the probing sweep it causes opens with a
        stop anyway.

        A queued CLEAR is one of these: its 0x14s are about to take the
        bus, and it is itself waiting for the guard floor - a heartbeat
        sent into that window buys nothing and only delays it.
        """
        with self._lock:
            return (self._job is not None or self._burn_job is not None
                    or self._clear_job is not None)

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

    def forget_verify(self) -> None:
        """Whatever the landing check knew, nobody is vouching for the
        glass any more: a run started or stopped, the unit released, a
        new design prepared. The verdict goes (a red marker from the last
        run must not hang over this one) and the generation moves on, so
        no cue firing from here can be mistaken for one already
        confirmed."""
        with self._lock:
            self._forget_verify_locked()

    def _forget_verify_locked(self) -> None:
        self.verify = None
        self.verify_gen = None
        self.glass_gen += 1

    def verified(self, cue_id: str, landed: str, resent: bool = False,
                 witness: "int | None" = None,
                 gen: "int | None" = None) -> None:
        """What the landing check found (see `self.verify`).

        Recorded whatever the session is holding by now: the verdict
        takes a second or more to reach, by which time a running show
        has armed the next cue, and matching on cue_id the way fired()
        does would drop every verdict a show ever produces. The record
        says which cue it is about instead.

        `fired_at` and `late_ms` are deliberately left alone: they are
        about the cue's FIRST broadcast, and a re-send repairs that cue
        rather than making a new, later one - "how late was the cue" must
        not improve or worsen because the link needed a second copy.

        The guard STOP is the opposite case and is NOT taken from here:
        the picture really does start again at the re-send, so
        ui/runner.py's _guard_after_fire() measures it from the LAST
        broadcast. A guard timed from the first send would land a
        re-sent cue's 0x17 inside its own sweep."""
        with self._lock:
            self.verify = {"cue": cue_id, "landed": landed,
                           "resent": bool(resent), "witness": witness}
            # The generation the CUE fired in, not whatever it is now: a
            # verdict cannot vouch for a glass that has since been let go.
            self.verify_gen = gen

    def failed_with(self, message: str) -> None:
        with self._lock:
            if self.phase in (PREPARING, READY, ARMED):
                self.phase = FAILED
            self.error = message
            if self.burn_state == "burning":
                # An unfinished burn is "cancelled", never "failed": no
                # port, a busy bus or no board answering leaves nothing
                # written and an EMPTY failed list, which the PC used to
                # read as "0 board(s) not written" and offer a force this
                # unit refuses anyway (review round 2, 2026-09-25). The
                # message stays as the reason.
                self.burn_state = "cancelled"
                self.burn_reason = message

    # ---- what the PC and the LCD read ----

    def status(self) -> dict:
        runner = self.runner
        with self._lock:
            late_ms = (None if self.fired_at is None or self.fire_at is None
                       else round((self.fired_at - self.fire_at) * 1000, 1))
            burn = None
            if self.burn_state is not None:
                burn = self._burn_dict_locked()
            return {
                "active": self.active, "phase": self.phase,
                "cue": self.cue_id, "label": self.label,
                "error": self.error or runner.error,
                "saved": list(self.saved), "failed": list(self.failed),
                "prepare_s": self.prepare_s,
                "fire_at": self.fire_at, "fired_at": self.fired_at,
                "late_ms": late_ms,
                "verify": dict(self.verify) if self.verify else None,
                "boards": runner.reported_boards, "live": list(runner.live),
                # Which board list the unit is actually working to, and
                # what it still probes. The 2026-09-26 failure was
                # invisible from the Conductor - the pictures WERE
                # written; what was wrong was the unit's own idea of
                # which sockets exist (radxa-04 kept probing 17-22 all
                # show). `absent` is part of the list in force, so the
                # PC compares `boards` + `absent` against the show's own
                # board ids; `group_count` is the number the frames
                # carry (ui/runner.py's _group_count()).
                "boards_source": runner.boards_source,
                "absent": sorted(runner.absent_snapshot()),
                "group_count": runner.group_count,
                "no_sweep": sorted(runner.no_sweep),
                "standby_ready": bool(runner.standby_ready),
                # How many idle autoplay guards this worker has sent
                # (ui/runner.py's REMOTE_GUARD_S). Diagnosis only: a
                # garment losing cues with this stuck at 0 means the
                # heartbeat never got a clear window.
                "remote_guard_sent": runner.remote_guard_sent,
                # The last broadcast write that BLOCKED, and how many
                # have (ui/runner.py's STALL_LOG_MS): {"ms", "frame",
                # "at" (wall clock), "ago_s", "count"}. None while every
                # write has been immediate, which is the normal answer,
                # and `ms` back to None once a later cue went out
                # cleanly. The age is worked out HERE, on the unit's own
                # clock - the PC's differs, and this is one subtraction
                # between two readings of the same one.
                "bus_stall": _with_age(runner.bus_stall),
                # The last time a bus that was accepting frames and
                # executing none was got working again (ui/runner.py's
                # _recover_bus()): {"by" ("padding" | "reopen" | null),
                # "before_ms", "after_ms", "at" (wall clock), "ago_s",
                # "count"}. None until one has been run in this worker.
                "bus_recovery": _with_age(runner.bus_recovery),
                # The opt-in fire-time re-send (--resend-on-stall), and
                # whether this unit has it on at all: a cue whose own
                # broadcast blocked, sent again once. None until one has.
                "resend_on_stall": bool(runner.resend_on_stall),
                "resend": _with_age(runner.resend),
                # The health check two seconds before the last cue
                # (ui/runner.py's PRECHECK_S): {"cue", "before_ms", "by"
                # ("padding" | "reopen" | null), "after_ms", "at",
                # "ago_s"}. `before_ms` of 1-2 ms with `by` null is the
                # normal answer - the port was fine and nothing was done.
                "precheck": _with_age(runner.precheck),
                # The three kill switches as the unit is actually running
                # them (ui/main.py's --precheck SECONDS, --no-port-watch,
                # --no-auto-recover), so the PC can see which are on.
                "precheck_s": runner.precheck_s,
                "port_watch": bool(runner.port_watch),
                "auto_recover": bool(runner.auto_recover),
                "burn": burn,
                # Taking the pictures back out of slots 1-18 after the
                # show (see the module docstring): {"state", "done",
                # "total", "failed"}, state "none" until one is asked
                # for. Never null, so the PC can tell "this agent does
                # not clear at all" (no key) from "nothing to clear yet".
                "clear": self._clear_dict_locked(),
            }
