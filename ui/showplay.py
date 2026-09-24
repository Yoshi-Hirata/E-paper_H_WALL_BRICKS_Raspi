"""The unit runs the show by itself: a show file, a start time, no PC needed.

The PC compiles the timeline into one file per unit (conductor/showfile.py)
and loads it before the start. Pre-burn (docs/MERIS_REPLY_3SLOT.pdf,
2026-09-24): every cue's picture already went into its own slot (1..19)
the moment the show was load()ed - see RemoteSession.burn() - so from
then on the only thing that ever has to arrive is T0 - "second 0 of the
show, in your monotonic clock". The player turns every cue into a single
timed trigger on its own: arm a broadcast "show slot N" for T0 + sent,
fire it. Nothing is written here - a colour save, a delay table and a
slot's own config all persist across a power cycle, so a write only
ever happens again at the next /show/load. The Wi-Fi may drop for the
rest of the show.

Everything the operator does to a running show is a new T0:

    HOLD     stop scheduling (a cue armed is disarmed)
    RESUME   run again with T0 moved later by the time held
    NEXT     run with T0 moved earlier, so the next cue is due now

so the player has one rule to follow, not four: look at the clock, work
out which cue should be on the garment and which comes next, and do what
is missing. That same rule is the recovery. After a reboot, a late
start or a jump, "what should be showing" is just re-armed - the trigger
alone puts the garment right, since the picture was already there.

The same goes for a board that missed a trigger outright (it was not
powered up yet, say): a cue counts as on the garment once it fired, but
if a board that was not live then turns up later, the garment is
`dirty` until its slot is re-triggered - no rewrite, just the same
broadcast again (once per cue, so a board that is simply dead does not
make this repeat for ever).

Across a reboot the monotonic clock starts over, so T0 is also kept as
wall-clock time on disk. That is only good to within the reboot itself
(no RTC battery), which is why the restored player waits a few seconds
before acting: the PC, which watches every unit's T0, will have sent the
exact one by then if it can reach the unit at all. A restored T0 that
lies in the future cannot be right - nothing starts a show more than a
minute ahead - and is not run on.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from .remote import ARMED, DEFAULT_SLOT, FAILED, FIRED, PREPARING, READY, RemoteError

STORE = Path.home() / ".epaper"
# A show's own cues take 1..18: 19 (DEFAULT_SLOT) stays the manual
# Prepare / demo one-shot slot, and 0 is the standby white (contract
# update, 2026-09-24 - boards hold slot_capacity == 20 slots, 0..19).
SHOW_SLOT_MIN, SHOW_SLOT_MAX = 1, DEFAULT_SLOT - 1
# Pre-burn (docs/MERIS_REPLY_3SLOT.pdf, 2026-09-24): every cue's picture is
# written into its own slot at /show/load time (RemoteSession.burn()), so
# RUNNING a show is triggers only - a broadcast "show slot N", nothing to
# write. SAVE_S_PER_BOARD/PREP_MARGIN_S/SETUP_S/SETUP_S_PER_BOARD stay as
# constructor knobs (existing callers pass them) but no longer drive
# _plan()'s timing, which needs no lead time for a trigger.
SAVE_S_PER_BOARD = 0.25    # a little over the measured 0.22 s
PREP_MARGIN_S = 2.0
# The first cue after the runner takes the port for remote work also
# pays for its setup: the broadcast stop, its settle time and one probe
# of every board. Normally the preset pays that long before START; a
# unit rejoining mid-show pays it on its first cue.
SETUP_S, SETUP_S_PER_BOARD = 1.0, 0.15
RESTORE_GRACE_S = 6.0      # let the PC correct a restored T0 first
RESTORE_AHEAD_S = 90.0     # a restored T0 further ahead than this is junk
CATCH_UP_LEAD_S = 0.3
RETRY_AFTER_FAILED_S = 3.0
END_SLACK_S = 30.0

LOADED, RUNNING, HOLDING, STOPPED, ENDED = (
    "loaded", "running", "holding", "stopped", "ended")
DELAY_UNIT_MS = 10        # conductor/showfile.py's DELAY_UNIT_MS (10 ms frames)


def validate_show(show: dict) -> None:
    """What `ShowPlayer.load()` needs a show file to have - also used by
    ui/demos.py so a bad show is refused at /demo/save, not at KEY1."""
    if not isinstance(show, dict) or not show.get("id"):
        raise RemoteError("the show has no id")
    cues = show.get("cues")
    if not isinstance(cues, list) or not cues:
        raise RemoteError("the show has no cues")
    try:
        float(show["refresh_s"]), float(show["duration"])
    except (KeyError, TypeError, ValueError):
        raise RemoteError("the show needs refresh_s and duration")
    unit_ms = show.get("delay_unit_ms")
    if unit_ms is not None and unit_ms != DELAY_UNIT_MS:
        raise RemoteError(f"this unit's delay tables are "
                          f"{DELAY_UNIT_MS} ms frames; the show says "
                          f"{unit_ms!r}")
    for cue in cues:
        if not isinstance(cue, dict):
            raise RemoteError("a cue must be an object")
        for key in ("id", "sent", "boards", "state"):
            if key not in cue:
                raise RemoteError(f"cue without {key}")
    # The pre-burn redesign (2026-09-24): a cue with no slot cannot be
    # burned, and this unit no longer knows how to play a show any other
    # way - conductor/showfile.py always assigns one now. "slot_capacity"
    # (boards hold slots 0..19) is how a burned show file identifies
    # itself; an old file carries neither and gets the same refusal.
    # Cues use 1..SHOW_SLOT_MAX only: SHOW_SLOT_MAX+1 (19) stays the
    # manual Prepare / demo one-shot slot, so a show's own burn can never
    # be overwritten by one, and slot 0 is the standby white.
    if "slot_capacity" not in show or any("slot" not in cue for cue in cues):
        raise RemoteError("show file has no slots - update the conductor")
    for cue in cues:
        try:
            slot = int(cue["slot"])
        except (TypeError, ValueError):
            raise RemoteError(f"cue {cue.get('id', '?')}: bad slot "
                              f"{cue['slot']!r}")
        if not SHOW_SLOT_MIN <= slot <= SHOW_SLOT_MAX:
            raise RemoteError(f"cue {cue.get('id', '?')}: slot {slot} must "
                              f"be {SHOW_SLOT_MIN}-{SHOW_SLOT_MAX} "
                              f"({DEFAULT_SLOT} is the manual slot, 0 is "
                              f"the standby)")


class ShowPlayer:
    def __init__(self, session, store: "Path | None" = STORE,
                 clock=time.monotonic, wall=time.time,
                 save_s: float = SAVE_S_PER_BOARD,
                 margin_s: float = PREP_MARGIN_S,
                 grace_s: float = RESTORE_GRACE_S, tick_s: float = 0.2,
                 setup_s: float = SETUP_S,
                 setup_board_s: float = SETUP_S_PER_BOARD,
                 retry_s: float = RETRY_AFTER_FAILED_S):
        self.session = session
        self.store = Path(store) if store else None
        self._clock, self._wall = clock, wall
        self.save_s, self.margin_s = save_s, margin_s
        self.grace_s, self.tick_s = grace_s, tick_s
        self.setup_s, self.setup_board_s = setup_s, setup_board_s
        self.retry_s = retry_s

        self.show: "dict | None" = None
        self.state = STOPPED
        self.t0: "float | None" = None
        self.synced = False            # T0 came from the PC, not from disk
        # A show loaded with load(show, demo=True) - ui/demos.py's stored
        # standalone shows, played from the unit's own menu. Only changes
        # what restore() does after a reboot (see there); everything else
        # about running one is identical to a PC-driven show.
        self.is_demo = False
        self.demo_name = ""            # the name it was written under
        self.applied: "str | None" = None      # cue id on the garment now
        self.dirty = False             # some board does not show `applied`
        self.note = ""
        # Boards presumed to show `applied` - every LIVE board at the
        # moment its trigger last went out (a broadcast has no per-board
        # ACK to check instead). One that was not live then and joins
        # later is what makes `dirty` true (see _check_dirty()).
        self._ever_ok: "set[int]" = set()
        self._counted: "str | None" = None     # session key already tallied
        self._retry_at = 0.0
        self._not_before = 0.0
        # Bumped by every command. _send() runs without the lock (it may
        # wait for the port), so what _plan() decided can be overtaken by
        # a HOLD, a STOP or a new show; the epoch is how it notices, and
        # a cue is only ever armed under the lock in an unchanged epoch.
        self._epoch = 0
        self._run_no = 0               # part of the session key, see run()
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._quit = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    # ---- commands (from the agent) ----

    def load(self, show: dict, demo: bool = False, name: str = "") -> None:
        validate_show(show)
        with self._lock:
            self._epoch += 1
            # A fresh run_no here too, not just in run()'s own "start from
            # the top" branch: the burn this triggers below always takes
            # real time (even a fast one leaves a window between load()
            # returning and the operator's next /show/run), and the
            # background loop keeps calling _tally() the whole time - a
            # FIRED session key left over from a PREVIOUS run of the same
            # show can share this one's cue ids, and without a bump here
            # its run_no would still match self._run_no, resurrecting
            # `applied` before this run ever sends anything of its own
            # (found reproducing "load(); wait for the burn; run()" back
            # to back on a fast bus, 2026-09-24).
            self._run_no += 1
            self._disarm()
            self.show = show
            self.is_demo = bool(demo)
            # The name it was written under (ui/demos.py), not show["name"]
            # (the look's own name from the timeline) - the PC's Units
            # tile labels a unit "demo: <name>" from /status.show.
            self.demo_name = str(name) if demo else ""
            self.state, self.t0, self.synced = LOADED, None, False
            self._forget_garment()
            self.note = ""
            self._persist(with_show=True)
        # Burn every cue into its own slot now, ahead of the show itself -
        # RUNNING sends only a trigger per cue, never a write (see the
        # module docstring and ui/remote.py's RemoteSession.burn()). Runs
        # on the runner's worker; status()["burn"] is the progress.
        self.session.burn(self._burn_items(show), int(show.get("dev_type", 3)))
        self._wake.set()

    @staticmethod
    def _burn_items(show: dict) -> "list[dict]":
        """One item per cue, in cue order (preset first): the FULL
        picture (`state`, not the diff `boards`) - a slot is self
        contained, so a board left out of a cue's own change must still
        show the right colour once that slot is triggered."""
        return [{"slot": int(cue["slot"]),
                "boards": {int(a): bytes.fromhex(h)
                          for a, h in cue["state"].items()},
                "delays": {int(a): bytes.fromhex(h)
                          for a, h in (cue.get("delays") or {}).items()}}
               for cue in show["cues"]]

    def run(self, t0: float, show_id: "str | None" = None) -> None:
        with self._lock:
            if self.show is None:
                raise RemoteError("no show loaded")
            if show_id is not None and show_id != self.show["id"]:
                raise RemoteError(f"loaded show is {self.show['id']}, "
                                  f"not {show_id}")
            if self.session.busy():
                raise RemoteError("unit is busy (firmware update, scan "
                                  "or reboot)")
            self._burn_gate()
            self._epoch += 1
            self.t0, self.synced = float(t0), True
            self.state, self.note = RUNNING, ""
            self._not_before = 0.0
            if self._clock() < self.t0:
                # T0 ahead of now is a start from the top (RESUME and NEXT
                # land inside the show). A new run gets new session keys,
                # so nothing left in the session from the last run can
                # pass for this run's cue, and the garment is only taken
                # as known if it is cleanly showing the preset.
                self._run_no += 1
                first = self.show["cues"][0]["id"]
                if self.applied != first or self.dirty:
                    self._forget_garment()
            # A forward jump (a SEEK past due, or a big NEXT) can leave a
            # cue that was already loading/armed for the OLD T0 behind
            # the new one - its `sent` is now in the past. Left alone it
            # would just fire as scheduled (or the moment it is ready),
            # showing that skipped cue while the true current one waits
            # behind it (_plan()'s branch 2 is blocked by `owned_unfired`
            # until this fires). Disarming it clears fire_at, which is
            # exactly what lets that branch repaint the picture this new
            # T0 actually wants (owned_unfired requires a fire_at).
            session = self.session
            if (self._owns(session.cue_id)
                    and session.phase in (PREPARING, READY, ARMED)):
                cue_id = self._parse(session.cue_id)
                cue = next((c for c in self.show["cues"]
                           if c["id"] == cue_id), None)
                if cue is not None and cue["sent"] <= self._clock() - self.t0:
                    self._disarm()
            self._persist()
        self._wake.set()

    def preset(self) -> None:
        """Put the first cue's picture up now, before the start.

        Through the player rather than as a loose cue, so that it knows
        the preset is on the garment and START does not repaint it.
        """
        with self._lock:
            if self.show is None:
                raise RemoteError("no show loaded")
            if self.state == RUNNING:
                raise RemoteError("the show is running")
            if self.session.busy():
                raise RemoteError("unit is busy (firmware update, scan "
                                  "or reboot)")
            self._burn_gate()
            self._epoch += 1
            self._run_no += 1
            show, first = self.show, self.show["cues"][0]
            self._forget_garment()
            # No write to budget for any more - the picture is already
            # burned into its slot; a small margin only covers arm()
            # possibly waiting for the port (start_remote()).
            fire_at = self._clock() + CATCH_UP_LEAD_S
            epoch = self._epoch
        self._send(show, first, fire_at, epoch)   # may take the port
        self._wake.set()

    def _burn_gate(self) -> None:
        """Refuse to run/preset while the show is not safely burned -
        called with the lock held."""
        burn = self.session.burn_status()
        if burn is not None and burn["state"] == "burning":
            raise RemoteError(f"still writing the pictures: "
                              f"{burn['done']}/{burn['total']}")
        if burn is not None and burn["state"] == "failed":
            # A board that was simply absent (dropped) never gets a
            # picture anyway - accepted as a known gap. One that is live
            # but still refused the write is a real problem the operator
            # has to fix (reload) before the show can start.
            absent = set(self.session.runner.absent)
            stuck = sorted({b for b, s in burn["failed"] if b not in absent})
            if stuck:
                names = ",".join(str(b) for b in stuck)
                raise RemoteError(f"board {names} did not take the burn - "
                                  f"reload the show")

    def hold(self) -> None:
        with self._lock:
            self._epoch += 1
            if self.state == RUNNING:
                self.state = HOLDING
                self._disarm()
                self._persist()
        self._wake.set()

    def stop(self) -> None:
        with self._lock:
            self._epoch += 1
            if self.state in (RUNNING, HOLDING):
                self._disarm()
            if self.show is not None:
                self.state = STOPPED
            self.t0 = None
            self._persist()
        self.session.cancel_burn()   # give up on a burn still in flight
        self._wake.set()

    def close(self) -> None:
        self._quit.set()
        self._wake.set()
        self._thread.join(timeout=2)

    @property
    def running(self) -> bool:
        return self.state == RUNNING

    # ---- disk ----

    def _write(self, name: str, payload) -> None:
        """Whole or not at all: the power cut restore() exists for must
        not find half a file."""
        target = self.store / name
        scratch = self.store / (name + ".tmp")
        scratch.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(scratch, target)

    def _persist(self, with_show: bool = False) -> None:
        if self.store is None:
            return
        try:
            self.store.mkdir(parents=True, exist_ok=True)
            if with_show and self.show is not None:
                self._write("show.json", self.show)     # only when it changes
            self._write("show-run.json", {
                "show": self.show["id"] if self.show else None,
                "state": self.state, "applied": self.applied,
                "demo": self.is_demo, "demo_name": self.demo_name,
                # T0 as wall time: what survives a reboot.
                "t0_wall": (None if self.t0 is None else
                            self._wall() + (self.t0 - self._clock()))})
        except OSError as exc:
            self.note = f"cannot save the show: {exc}"

    def restore(self) -> None:
        """At start-up: pick the show up again if it was running."""
        if self.store is None:
            return
        try:
            show = json.loads((self.store / "show.json")
                              .read_text(encoding="utf-8"))
            run = json.loads((self.store / "show-run.json")
                             .read_text(encoding="utf-8"))
            duration = float(show["duration"])
            float(show["refresh_s"]), show["cues"][0]["sent"]
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            return
        with self._lock:
            self.show = show
            self.state = LOADED
            self.is_demo = bool(run.get("demo"))
            self.demo_name = run.get("demo_name", "") if self.is_demo else ""
            if run.get("show") != show.get("id"):
                return
            if self.is_demo:
                # A demo is simpler and safer left alone: it restarts only
                # when the operator presses KEY1 again, never on its own
                # after a power cut (a PC-driven show still resumes below).
                # Cleared to a plain LOADED show (not re-marked as a demo)
                # so a *second* reboot, mid-SHOW this time, does not take
                # this same branch again - a demo's show id is a content
                # digest, so without this the PC would never see reason to
                # reload it and the show would simply sit un-restored.
                self.is_demo, self.demo_name = False, ""
                self._persist()
                return
            t0_wall = run.get("t0_wall")
            if run.get("state") != RUNNING or t0_wall is None:
                return
            t0 = self._clock() + (t0_wall - self._wall())
            if self._clock() - t0 > duration + 60:
                return                  # that show is long over
            if t0 - self._clock() > RESTORE_AHEAD_S:
                # No RTC: the wall clock came up behind. Running on this T0
                # would sit out the show waiting for a start that is past.
                self.note = "restored T0 is in the future - waiting for the PC"
                return
            self.t0, self.synced = t0, False
            self.state = RUNNING
            self._forget_garment()      # unknown after a restart: send state
            self.note = "restored after restart"
            self._not_before = self._clock() + self.grace_s
        self._wake.set()

    # ---- the one rule ----

    def _forget_garment(self) -> None:
        self.applied, self.dirty = None, False
        self._ever_ok = set()
        self._counted = None

    def _lead(self, cue: "dict | None" = None,
              after_another: bool = False) -> float:
        """Kept for ui/app.py's KEY1 handler (_start_demo_show() /
        _loop_demo_show() add this to their own margin before their own
        run()): the pre-burn redesign needs no per-cue write-time
        estimate here any more, since nothing is written while RUNNING -
        this is just arm()'s own small margin (see preset()). NOTE: a
        freshly load()ed show still needs its burn to finish before
        run() succeeds (RemoteError "still writing the pictures") -
        ui/app.py's KEY1 flow does not yet wait for that (2026-09-24
        pre-burn redesign left as a follow-up); this margin alone is not
        enough for anything but a trivially small demo."""
        return CATCH_UP_LEAD_S

    def _disarm(self) -> None:
        """Take the fire time off whatever this show has in the session -
        loading as much as armed: a cue still arming already carries its
        time and would fire the moment it is ready."""
        session = self.session
        if (self._owns(session.cue_id)
                and session.phase in (PREPARING, READY, ARMED)):
            session.cancel()

    def _owns(self, cue_id) -> bool:
        return bool(self.show) and str(cue_id or "").startswith(
            self.show["id"] + ":")

    def _key(self, show: dict, cue: dict) -> str:
        return f"{show['id']}:{self._run_no}:{cue['id']}"

    @staticmethod
    def _parse(key: str) -> str:
        """session key -> cue id."""
        return key.rsplit(":", 1)[1]

    def _run_no_of(self, key: str) -> "int | None":
        """The run number a session key was made under, by stripping the
        known "<show id>:" prefix rather than counting colons - a show
        id is never expected to contain one, but this way nothing breaks
        if it ever does."""
        if not self.show:
            return None
        prefix = self.show["id"] + ":"
        if not key.startswith(prefix):
            return None
        try:
            return int(key[len(prefix):].split(":", 1)[0])
        except ValueError:
            return None

    def _send(self, show: dict, cue: dict, fire_at: float, epoch: int,
              heal: bool = False) -> None:
        """Make the session hold this cue, timed for `fire_at`.

        Called WITHOUT the player's lock: arm() may take the serial port
        (start_remote(), which can wait seconds for a previous worker),
        and /status (the PC's poll, the LCD) must keep answering
        meanwhile. Nothing is written - the picture is already burned
        into the cue's slot - but giving the cue its time still happens
        under the lock, in the epoch it was decided in, since a HOLD or
        a STOP or a new show may have overtaken the decision meanwhile.

        `heal=True` is a re-arm of a cue that already fired (its own
        key would otherwise read as "already loaded, leave it alone") -
        arm() is called again regardless, which resets it to READY so
        the fire below always follows.
        """
        session = self.session
        key = self._key(show, cue)
        if heal or session.cue_id != key or session.phase == FAILED:
            session.arm(key, int(cue["slot"]),
                       int(show.get("dev_type", 3)), cue.get("label", ""))
        with self._lock:
            if epoch != self._epoch:
                if session.cue_id == key:
                    session.cancel()        # decided before the command
                return
            if session.cue_id == key and (
                    session.phase in (PREPARING, READY) or (
                        session.phase == ARMED and session.fire_at is not None
                        and abs(session.fire_at - fire_at) > 0.001)):
                session.fire(key, fire_at)  # timed, or (re)timed: T0 moved

    def _tally(self) -> None:
        """What the session last fired is what is on the garment - noted
        whether or not the show runs (the preset comes first). A
        broadcast trigger has no per-board ACK, so every LIVE board at
        the moment it went out is presumed to now show it - see
        _check_dirty() for the only way that presumption gets revised."""
        session = self.session
        key = session.cue_id
        if (session.phase != FIRED or not self._owns(key)
                or key == self._counted
                # A FIRED cue left over from a run that has since been
                # restarted (run() bumps _run_no every time T0 starts a
                # fresh top) must not be mistaken for this run's - most
                # visibly on a one-cue show, where the last cue of the
                # old run and the first of the new one are the same id
                # and _forget_garment() has just cleared `applied` and
                # `_counted`, so nothing else here tells them apart.
                or self._run_no_of(key) != self._run_no):
            return
        self._counted = key
        self.applied = self._parse(key)
        self._ever_ok = set(session.runner.live)
        self.dirty = False
        if self.note.startswith("board "):
            self.note = ""

    def _check_dirty(self) -> None:
        """A board that was not live when the applied cue's trigger last
        went out cannot be trusted to be showing it - it missed the
        broadcast outright (there is no queue to catch up from) and
        needs it resent; no rewrite, the picture is already in its
        flash (see the heal branch in _plan())."""
        if self.applied is None:
            return
        joined = set(self.session.runner.live) - self._ever_ok
        if joined:
            self._ever_ok |= joined
            self.dirty = True
            self.note = (f"board {','.join(str(b) for b in sorted(joined))} "
                        f"joined late: re-arming {self.applied}")

    def _plan(self) -> "tuple[float, tuple | None]":
        """(seconds until it is worth looking again, what to send now)."""
        with self._lock:
            self._tally()
            if self.state != RUNNING or self.show is None or self.t0 is None:
                return self.tick_s, None
            now_mono = self._clock()
            if now_mono < self._not_before:
                return min(self.tick_s, self._not_before - now_mono), None
            self._check_dirty()
            show, session = self.show, self.session
            cues = show["cues"]
            now = now_mono - self.t0

            past = [c for c in cues if c["sent"] <= now]
            ahead = [c for c in cues if c["sent"] > now]
            current = past[-1] if past else None
            nxt = ahead[0] if ahead else None
            # An owned cue with a fire_at set (ARMED, or still PREPARING
            # but already given one by _send()) carries a promise to fire
            # at that instant; arm()-ing any OTHER cue would displace it
            # outright (its cue_id, its fire_at), and the runner's own
            # session.fired() for the displaced cue would then find a
            # different cue_id and drop the tally with no error - the
            # cue is simply never shown (found in the timing review,
            # 2026-09-24, back when a cue's own write time could overrun
            # this same window; a trigger is instant, but a STOPPED or
            # HELD show, or the port still being taken by arm()'s own
            # start_remote(), can still leave one owned-and-promised for
            # a moment). A cue that is merely READY, or PREPARING with no
            # fire_at yet, has no such promise and is fine to preempt;
            # requiring fire_at is what tells the two apart.
            owned = self._parse(session.cue_id) if self._owns(
                session.cue_id) else None
            owned_unfired = (owned is not None
                             and session.phase in (PREPARING, READY, ARMED)
                             and session.fire_at is not None)
            duration = float(show["duration"])
            if nxt is None and now > duration + END_SLACK_S:
                # Over on the clock - even if the last cue's trigger never
                # reached a dead board and would be retried for ever.
                self._disarm()
                self.state = ENDED
                self._persist()
                return self.tick_s, None
            if (session.phase == FAILED and self._owns(session.cue_id)
                    and now_mono < self._retry_at):
                return self.tick_s, None        # not a tight re-arm loop

            action = None
            heal = False
            # `self.applied` (from _tally(), reading the session's FIRED
            # state) and `current` (from THIS tick's own clock read) are
            # two independent clocks - right on a cue boundary they can
            # disagree by a hair: the runner's _fire_at() may have just
            # fired `nxt` on its own slightly-later read of the clock,
            # so applied is already `nxt`'s id while this tick's `now`
            # still classifies `current` as the cue before it. Comparing
            # by each cue's own `sent` (order in the show), not id
            # equality, is what tells "genuinely behind" apart from
            # "already there, just a hair off" - id equality alone would
            # otherwise re-arm the cue that just fired one instant late,
            # right after its own successor already went out (found
            # chasing a one-cue-show-run-twice test firing 5 shows for a
            # 3-cue show instead of 3, 2026-09-24).
            applied_cue = next((c for c in cues if c["id"] == self.applied),
                               None)
            behind = (current is not None
                     and (applied_cue is None
                          or applied_cue["sent"] < current["sent"]))
            # 1. The garment must show `current` right now - either it
            #    never has (a start, a jump, a restart) or it did and a
            #    board joined late since (dirty). A trigger is instant,
            #    so this fires at `current`'s own instant - already past
            #    by definition of `current`, so it goes out at once and
            #    the delay from `now_mono` is what late_ms then reports.
            #    A heal reuses the very same key session.arm() would
            #    otherwise leave alone as "already loaded". Blocked only
            #    by a DIFFERENT owned unfired cue - re-deciding this same
            #    one's fire_at is how a HOLD/RESUME or a backward seek
            #    re-times a cue that is already armed for it, and unlike
            #    the old live-write design this fire_at is always the
            #    fixed t0 + current["sent"], never a moving "now + a
            #    lead", so redeciding it every tick cannot livelock.
            if (current is not None
                    and not (owned_unfired and owned != current["id"])
                    and (behind or self.dirty)):
                heal = self.applied == current["id"]
                self.dirty = False
                action = (show, current, self.t0 + current["sent"])
            # 2. Already showing the right thing, nothing due yet: arm
            #    the NEXT cue ahead of its own instant, so the worker's
            #    busy-wait (ui/runner.py's _fire_at, not this tick
            #    loop's own granularity) is what decides exactly when
            #    it fires. Blocked by a DIFFERENT owned cue still
            #    carrying a promise to fire - arm()-ing over it would
            #    displace it outright and the runner's own
            #    session.fired() for the displaced cue would then drop
            #    the tally with no error, the cue simply never shown
            #    (found in the timing review, 2026-09-24, back when a
            #    cue's own write could overrun this same window; nothing
            #    is written any more, but a STOPPED/HELD show, or arm()'s
            #    own start_remote() still taking the port, can still
            #    leave one owned for a moment). Re-deciding THIS SAME
            #    cue's fire_at is fine (same reasoning as branch 1's own
            #    fixed target). A cue that is merely READY, or PREPARING
            #    with no fire_at yet, has no promise at all and is fine
            #    to preempt outright; requiring fire_at is what tells
            #    the two apart.
            elif (nxt is not None
                    and not (owned_unfired and owned != nxt["id"])
                    and not (self.applied == nxt["id"] and not self.dirty)):
                action = (show, nxt, self.t0 + nxt["sent"])

            if (nxt is None and action is None and not owned_unfired
                    and current is not None and self.applied == current["id"]
                    and now > duration):
                self.state = ENDED
                self._persist()
            if action is not None and session.phase == FAILED:
                self._retry_at = now_mono + self.retry_s
            if action is None:
                return self.tick_s, None
            return self.tick_s, action + (self._epoch, heal)

    def _loop(self) -> None:
        while not self._quit.is_set():
            wait = 1.0
            try:
                wait, action = self._plan()
                if action is not None:
                    self._send(*action)
            except RemoteError as exc:
                # A refused prepare (the session holds a cue about to
                # fire, or the unit is busy): look again next tick, not a
                # second later - a second is most of a cue's lead.
                self.note = str(exc)
                wait = self.tick_s
            except Exception as exc:        # noqa: BLE001 - never die mid-show
                self.note = f"player error: {exc}"
            self._wake.wait(wait)
            self._wake.clear()

    # ---- what the PC and the LCD read ----

    def status(self) -> "dict | None":
        with self._lock:
            if self.show is None:
                return None
            show = self.show
            out = {"id": show["id"], "name": show.get("name", ""),
                   "cues": len(show["cues"]), "state": self.state,
                   "t0": self.t0, "synced": self.synced,
                   "applied": self.applied, "dirty": self.dirty,
                   "note": self.note, "now": None, "next": None,
                   "duration": show.get("duration"),
                   # ui/demos.py's standalone shows: the conductor's own
                   # supervise()/_adopt() should leave one of these alone
                   # rather than mistake it for its own show; the Units
                   # tile labels a unit "demo: <name>" from demo_name.
                   "demo": self.is_demo, "demo_name": self.demo_name,
                   # "writing pictures n/N" (ui/remote.py's burn()) - what
                   # the PC gates START on and the LCD/Units tile show.
                   "burn": self.session.burn_status()}
            if self.t0 is not None and self.state in (RUNNING, ENDED):
                now = self._clock() - self.t0
                out["now"] = round(now, 2)
                ahead = [c for c in show["cues"] if c["sent"] > now]
                if ahead:
                    out["next"] = {"id": ahead[0]["id"],
                                   "label": ahead[0].get("label", ""),
                                   "in_s": round(ahead[0]["sent"] - now, 1)}
            return out
