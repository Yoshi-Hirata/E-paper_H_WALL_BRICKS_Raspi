"""The unit runs the show by itself: a show file, a start time, no PC needed.

The PC compiles the timeline into one file per unit (conductor/showfile.py)
and loads it before the start. From then on the only thing that ever has
to arrive is T0 - "second 0 of the show, in your monotonic clock". The
player turns every cue into the session's two steps on its own: prepare
in time for the boards to be written, fire at T0 + sent. The Wi-Fi may
drop for the rest of the show.

Everything the operator does to a running show is a new T0:

    HOLD     stop scheduling (a cue loading or armed is disarmed)
    RESUME   run again with T0 moved later by the time held
    NEXT     run with T0 moved earlier, so the next cue is due now

so the player has one rule to follow, not four: look at the clock, work
out which cue should be on the garment and which comes next, and do what
is missing. That same rule is the recovery. After a reboot, a late
start or a jump, "what should be showing" is not what was last sent, so
the cue's `state` (the whole picture) goes out instead of its `boards`
(the change) - one refresh and the garment is right, whatever it missed.

The same goes for a board that missed a change. A cue counts as on the
garment once it fired, but if a board that had a picture did not take
this one - or a board turned up that has only ever been given a change -
the garment is `dirty`: the whole picture goes out at the next chance
(once per cue, so a board that is simply dead does not make the garment
repaint for ever), and every cue is sent whole until it is clean again.

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

from .remote import ARMED, FAILED, FIRED, PREPARING, READY, RemoteError

STORE = Path.home() / ".epaper"
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
        self._ever_ok: "set[int]" = set()      # boards that hold a picture
        self._healed_for: "str | None" = None  # one whole repaint per cue
        self._latched: "tuple[str, bool] | None" = None    # (cue id, whole)
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
        self._wake.set()

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
                self._healed_for = self._latched = None
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
                cue_id, _ = self._parse(session.cue_id)
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
            self._epoch += 1
            self._run_no += 1
            show, first = self.show, self.show["cues"][0]
            self._forget_garment()
            fire_at = self._clock() + self._lead(first) + CATCH_UP_LEAD_S
            epoch = self._epoch
        self._send(show, first, True, fire_at, epoch)   # may take the port
        self._wake.set()

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
        self._healed_for = self._latched = self._counted = None

    def _lead(self, cue: dict, after_another: bool = False) -> float:
        # A cue with delay tables writes each board twice.
        boards = len(cue["boards"]) + len(cue.get("delays") or {})
        lead = boards * self.save_s + self.margin_s
        if self.session.runner.remote is None and not after_another:
            lead += self.setup_s + boards * self.setup_board_s   # setup to pay
        return lead

    def _disarm(self) -> None:
        """Take the fire time off whatever this show has in the session -
        loading as much as armed: a cue still writing its boards already
        carries its time and would fire the moment it is ready."""
        session = self.session
        if (self._owns(session.cue_id)
                and session.phase in (PREPARING, READY, ARMED)):
            session.cancel()

    def _owns(self, cue_id) -> bool:
        return bool(self.show) and str(cue_id or "").startswith(
            self.show["id"] + ":")

    def _key(self, show: dict, cue: dict, whole: bool) -> str:
        return (f"{show['id']}:{self._run_no}:{cue['id']}"
                + ("+" if whole else ""))

    @staticmethod
    def _parse(key: str) -> "tuple[str, bool]":
        """session key -> (cue id, was it the whole picture)."""
        cue_id = key.rsplit(":", 1)[1]
        return (cue_id[:-1], True) if cue_id.endswith("+") else (cue_id, False)

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

    def _send(self, show: dict, cue: dict, whole: bool, fire_at: float,
              epoch: int) -> None:
        """Make the session hold this cue, timed for `fire_at`.

        Called WITHOUT the player's lock: the first prepare takes the
        serial port, which can wait seconds for the previous worker, and
        /status (the PC's poll, the LCD) must keep answering meanwhile.
        Writing the boards changes nothing on the glass, so that part may
        be overtaken by a HOLD or a STOP; giving the cue its time may
        not, and happens under the lock, in the epoch it was decided in.
        """
        session = self.session
        key = self._key(show, cue, whole)
        if session.cue_id != key or session.phase == FAILED:
            source = cue["state"] if whole else cue["boards"]
            label = cue.get("label", "") + (" [whole]" if whole
                                            and cue is not show["cues"][0]
                                            else "")
            session.prepare(key, {int(a): bytes.fromhex(h)
                                  for a, h in source.items()},
                            int(show.get("dev_type", 3)), label,
                            {int(a): bytes.fromhex(h)
                             for a, h in (cue.get("delays") or {}).items()})
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
        whether or not the show runs (the preset comes first)."""
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
        cue_id, whole = self._parse(key)
        saved, failed = set(session.saved), set(session.failed)
        # A board that held a picture and missed this cue is wrong now; so
        # is a board that turned up and has only been given a change.
        missed = failed & self._ever_ok
        half = set() if whole else saved - self._ever_ok
        self._ever_ok |= saved
        self.applied = cue_id
        self.dirty = bool(missed or half)
        if self.dirty:
            boards = ",".join(str(b) for b in sorted(missed | half))
            self.note = f"board {boards} missed {cue_id}: whole picture next"
        elif self.note.startswith("board "):
            self.note = ""

    def _plan(self) -> "tuple[float, tuple | None]":
        """(seconds until it is worth looking again, what to send now)."""
        with self._lock:
            self._tally()
            if self.state != RUNNING or self.show is None or self.t0 is None:
                return self.tick_s, None
            now_mono = self._clock()
            if now_mono < self._not_before:
                return min(self.tick_s, self._not_before - now_mono), None
            show, session = self.show, self.session
            cues = show["cues"]
            now = now_mono - self.t0

            past = [c for c in cues if c["sent"] <= now]
            ahead = [c for c in cues if c["sent"] > now]
            current = past[-1] if past else None
            nxt = ahead[0] if ahead else None
            # An owned cue with a fire_at set (ARMED, or still PREPARING
            # but already given one by _send() - see below) carries a
            # promise to fire at that instant; session.prepare() for any
            # OTHER cue would displace it outright (its cue_id, its
            # fire_at), and the runner's own session.fired() for the
            # displaced cue would then find a different cue_id and drop
            # the tally with no error - the cue is simply never shown.
            # A cue that is merely READY, or PREPARING with no fire_at
            # yet (freshly prepared, or disarmed on purpose - run()'s own
            # forward-jump handling cancel()s a stale armed cue this same
            # way), has no such promise and is fine to preempt; requiring
            # fire_at is what tells the two apart.
            #
            # Every branch below waits for a genuinely promised cue
            # instead: it fires at its own time once this clears, or at
            # once if that time has already passed by then (found in the
            # timing review, 2026-09-24). Two ways this opens: branch 1 -
            # the interval between cues shorter than the next one's own
            # _lead() (32 boards 8 s apart on real hardware, or a short
            # lead right after a NEXT/SEEK) sends the next cue before the
            # current one has fired; branch 2 - a save slow enough to
            # overrun its own _lead() estimate can leave a cue neither
            # "current" nor "next" by the time it finally resolves (both
            # have already moved past it), which a plain current/next
            # membership check does not see.
            owned = self._parse(session.cue_id)[0] if self._owns(
                session.cue_id) else None
            owned_unfired = (owned is not None
                             and session.phase in (PREPARING, READY, ARMED)
                             and session.fire_at is not None)
            duration = float(show["duration"])
            if nxt is None and now > duration + END_SLACK_S:
                # Over on the clock - even if the last cue never made it
                # onto a dead board and would be retried for ever.
                self._disarm()
                self.state = ENDED
                self._persist()
                return self.tick_s, None
            if (session.phase == FAILED and self._owns(session.cue_id)
                    and now_mono < self._retry_at):
                return self.tick_s, None        # not a tight re-prepare loop

            action = None
            # 1. The next cue, once it is time to write the boards - unless
            #    it is on the garment already (the preset, shown before a
            #    START whose T0 is still ahead).
            if (nxt is not None and nxt["sent"] - now <= self._lead(nxt)
                    and not (self.applied == nxt["id"] and not self.dirty)
                    and not (owned_unfired and owned != nxt["id"])):
                index = cues.index(nxt)
                before = cues[index - 1]["id"] if index else None
                if self._latched is None or self._latched[0] != nxt["id"]:
                    # Decided once per cue: flipping between the change
                    # and the whole picture mid-load would write every
                    # board twice and lose the record of the fire.
                    # The first cue is laid over nothing: its change IS
                    # the whole picture, and is recorded as such.
                    self._latched = (nxt["id"], index == 0 or self.dirty
                                     or self.applied != before)
                action = (show, nxt, self._latched[1], self.t0 + nxt["sent"])
            # 2. Not showing what it should, and room before the next cue
            #    needs the bus: put the whole picture up now.
            #
            # Blocked by ANY owned unfired cue, even this same `current`
            # once armed - unlike branch 1, this send's fire_at is
            # "now_mono + a lead", not a fixed instant, so re-deciding it
            # every tick while ARMED would keep moving its own fire time
            # forward and it would never actually fire (a livelock found
            # while testing the owned_unfired guard itself).
            elif (current is not None and not owned_unfired
                    and (self.applied != current["id"]
                        or (self.dirty and self._healed_for != current["id"]))):
                room = (nxt["sent"] - now) if nxt else float("inf")
                # The unit compiled this moment's own refresh (the
                # slowest of the cues sharing it, conductor/showfile.py)
                # - prefer it over the show's default when it is there.
                refresh = float(current.get("refresh_s", show["refresh_s"]))
                need = (self._lead(current) + refresh
                        + float(current.get("span") or 0.0)
                        + (self._lead(nxt, after_another=True) if nxt else 0.0))
                if room > need:
                    if self.applied == current["id"]:
                        self._healed_for = current["id"]    # once per cue
                    action = (show, current, True,
                              now_mono + self._lead(current) + CATCH_UP_LEAD_S)

            if (nxt is None and action is None and not owned_unfired
                    and current is not None and self.applied == current["id"]
                    and now > duration):
                self.state = ENDED
                self._persist()
            if action is not None and session.phase == FAILED:
                self._retry_at = now_mono + self.retry_s
            return self.tick_s, (action + (self._epoch,) if action else None)

    def _loop(self) -> None:
        while not self._quit.is_set():
            wait = 1.0
            try:
                wait, action = self._plan()
                if action is not None:
                    self._send(*action)
            except RemoteError as exc:
                self.note = str(exc)
                wait = 1.0
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
                   "demo": self.is_demo, "demo_name": self.demo_name}
            if self.t0 is not None and self.state in (RUNNING, ENDED):
                now = self._clock() - self.t0
                out["now"] = round(now, 2)
                ahead = [c for c in show["cues"] if c["sent"] > now]
                if ahead:
                    out["next"] = {"id": ahead[0]["id"],
                                   "label": ahead[0].get("label", ""),
                                   "in_s": round(ahead[0]["sent"] - now, 1)}
            return out
