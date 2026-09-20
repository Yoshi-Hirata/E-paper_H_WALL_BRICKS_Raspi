"""The unit runs the show by itself: a show file, a start time, no PC needed.

The PC compiles the timeline into one file per unit (conductor/showfile.py)
and loads it before the start. From then on the only thing that ever has
to arrive is T0 - "second 0 of the show, in your monotonic clock". The
player turns every cue into the session's two steps on its own: prepare
in time for the boards to be written, fire at T0 + sent. The Wi-Fi may
drop for the rest of the show.

Everything the operator does to a running show is a new T0:

    HOLD     stop scheduling (a cue already armed is disarmed)
    RESUME   run again with T0 moved later by the time held
    NEXT     run with T0 moved earlier, so the next cue is due now

so the player has one rule to follow, not four: look at the clock, work
out which cue should be on the garment and which comes next, and do what
is missing. That same rule is the recovery. After a reboot, a late
start or a jump, "what should be showing" is not what was last sent, so
the cue's `state` (the whole picture) goes out instead of its `boards`
(the change) - one refresh and the garment is right, whatever it missed.

Across a reboot the monotonic clock starts over, so T0 is also kept as
wall-clock time on disk. That is only good to within the reboot itself
(no RTC battery), which is why the restored player waits a few seconds
before acting: the PC, which watches every unit's T0, will have sent the
exact one by then if it can reach the unit at all.
"""

from __future__ import annotations

import json
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
CATCH_UP_LEAD_S = 0.3

LOADED, RUNNING, HOLDING, STOPPED, ENDED = (
    "loaded", "running", "holding", "stopped", "ended")


class ShowPlayer:
    def __init__(self, session, store: "Path | None" = STORE,
                 clock=time.monotonic, wall=time.time,
                 save_s: float = SAVE_S_PER_BOARD,
                 margin_s: float = PREP_MARGIN_S,
                 grace_s: float = RESTORE_GRACE_S, tick_s: float = 0.2,
                 setup_s: float = SETUP_S,
                 setup_board_s: float = SETUP_S_PER_BOARD):
        self.session = session
        self.store = Path(store) if store else None
        self._clock, self._wall = clock, wall
        self.save_s, self.margin_s = save_s, margin_s
        self.grace_s, self.tick_s = grace_s, tick_s
        self.setup_s, self.setup_board_s = setup_s, setup_board_s

        self.show: "dict | None" = None
        self.state = STOPPED
        self.t0: "float | None" = None
        self.synced = False            # T0 came from the PC, not from disk
        self.applied: "str | None" = None      # cue id on the garment now
        self.note = ""
        self._not_before = 0.0
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._quit = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    # ---- commands (from the agent) ----

    def load(self, show: dict) -> None:
        cues = show.get("cues")
        if not isinstance(cues, list) or not cues:
            raise RemoteError("the show has no cues")
        for cue in cues:
            for key in ("id", "sent", "boards", "state"):
                if key not in cue:
                    raise RemoteError(f"cue without {key}")
        with self._lock:
            self._disarm()
            self.show = show
            self.state, self.t0, self.synced = LOADED, None, False
            self.applied, self.note = None, ""
            self._persist()
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
            self.t0, self.synced = float(t0), True
            self.state, self.note = RUNNING, ""
            self._not_before = 0.0
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
            first = self.show["cues"][0]
            self.applied = None
            self._send(first, True, self._clock() + self._lead(first)
                       + CATCH_UP_LEAD_S)
        self._wake.set()

    def hold(self) -> None:
        with self._lock:
            if self.state == RUNNING:
                self.state = HOLDING
                self._disarm()
                self._persist()
        self._wake.set()

    def stop(self) -> None:
        with self._lock:
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

    def _persist(self) -> None:
        if self.store is None:
            return
        try:
            self.store.mkdir(parents=True, exist_ok=True)
            if self.show is not None:
                (self.store / "show.json").write_text(json.dumps(self.show),
                                                      encoding="utf-8")
            run = {"show": self.show["id"] if self.show else None,
                   "state": self.state, "applied": self.applied,
                   # T0 as wall time: what survives a reboot.
                   "t0_wall": (None if self.t0 is None else
                               self._wall() + (self.t0 - self._clock()))}
            (self.store / "show-run.json").write_text(json.dumps(run),
                                                      encoding="utf-8")
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
        except (OSError, ValueError):
            return
        with self._lock:
            self.show = show
            self.state = LOADED
            if run.get("show") != show.get("id"):
                return
            t0_wall = run.get("t0_wall")
            if run.get("state") != RUNNING or t0_wall is None:
                return
            t0 = self._clock() + (t0_wall - self._wall())
            if self._clock() - t0 > float(show.get("duration", 0)) + 60:
                return                  # that show is long over
            self.t0, self.synced = t0, False
            self.state = RUNNING
            self.applied = None         # unknown after a restart: send state
            self.note = "restored after restart"
            self._not_before = self._clock() + self.grace_s
        self._wake.set()

    # ---- the one rule ----

    def _lead(self, cue: dict, after_another: bool = False) -> float:
        boards = len(cue["boards"])
        lead = boards * self.save_s + self.margin_s
        if self.session.runner.remote is None and not after_another:
            lead += self.setup_s + boards * self.setup_board_s   # setup to pay
        return lead

    def _disarm(self) -> None:
        session = self.session
        if session.phase in (ARMED,) and self._owns(session.cue_id):
            session.cancel()

    def _owns(self, cue_id) -> bool:
        return bool(self.show) and str(cue_id or "").startswith(
            self.show["id"] + ":")

    def _cue_key(self, cue: dict, whole: bool) -> str:
        return f"{self.show['id']}:{cue['id']}" + ("+" if whole else "")

    def _arrays(self, cue: dict, whole: bool) -> "dict[int, bytes]":
        source = cue["state"] if whole else cue["boards"]
        return {int(a): bytes.fromhex(h) for a, h in source.items()}

    def _send(self, cue: dict, whole: bool, fire_at: float) -> None:
        """Make the session hold this cue, timed for `fire_at`."""
        session = self.session
        key = self._cue_key(cue, whole)
        if session.cue_id != key or session.phase == FAILED:
            session.prepare(key, self._arrays(cue, whole),
                            int(self.show.get("dev_type", 3)),
                            cue.get("label", ""))
            session.fire(key, fire_at)
        elif session.phase in (PREPARING, READY) or (
                session.phase == ARMED and session.fire_at is not None
                and abs(session.fire_at - fire_at) > 0.001):
            session.fire(key, fire_at)      # (re)timed: HOLD/RESUME, NEXT

    def _step(self) -> float:
        """Do what is missing; seconds until it is worth looking again."""
        with self._lock:
            session = self.session
            # What the session last fired is what is on the garment -
            # noted whether or not the show runs (the preset comes first).
            if session.phase == FIRED and self._owns(session.cue_id):
                self.applied = session.cue_id.split(":", 1)[1].rstrip("+")
            if self.state != RUNNING or self.show is None or self.t0 is None:
                return self.tick_s
            now_mono = self._clock()
            if now_mono < self._not_before:
                return min(self.tick_s, self._not_before - now_mono)
            show = self.show
            cues = show["cues"]
            now = now_mono - self.t0

            past = [c for c in cues if c["sent"] <= now]
            ahead = [c for c in cues if c["sent"] > now]
            current = past[-1] if past else None
            nxt = ahead[0] if ahead else None
            in_flight = (self._owns(session.cue_id)
                         and session.phase in (PREPARING, READY, ARMED))

            try:
                # 1. The next cue, once it is time to write the boards.
                if nxt is not None and nxt["sent"] - now <= self._lead(nxt):
                    before = cues[cues.index(nxt) - 1]["id"] \
                        if cues.index(nxt) > 0 else None
                    whole = self.applied != before
                    self._send(nxt, whole, self.t0 + nxt["sent"])
                # 2. Not showing what it should, and room before the next
                #    cue needs the bus: put the whole picture up now.
                elif (current is not None and self.applied != current["id"]
                      and not in_flight):
                    room = (nxt["sent"] - now) if nxt else float("inf")
                    need = (self._lead(current) + float(show["refresh_s"])
                            + (self._lead(nxt, after_another=True)
                               if nxt else 0.0))
                    if room > need:
                        self._send(current, True,
                                   now_mono + self._lead(current)
                                   + CATCH_UP_LEAD_S)
            except RemoteError as exc:
                self.note = str(exc)
                return 1.0

            if (nxt is None and current is not None
                    and self.applied == current["id"]
                    and now > float(show["duration"])):
                self.state = ENDED
                self._persist()
            return self.tick_s

    def _loop(self) -> None:
        while not self._quit.is_set():
            try:
                wait = self._step()
            except Exception as exc:        # noqa: BLE001 - never die mid-show
                self.note = f"player error: {exc}"
                wait = 1.0
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
                   "applied": self.applied, "note": self.note,
                   "now": None, "next": None,
                   "duration": show.get("duration")}
            if self.t0 is not None and self.state in (RUNNING, ENDED):
                now = self._clock() - self.t0
                out["now"] = round(now, 2)
                ahead = [c for c in show["cues"] if c["sent"] > now]
                if ahead:
                    out["next"] = {"id": ahead[0]["id"],
                                   "label": ahead[0].get("label", ""),
                                   "in_s": round(ahead[0]["sent"] - now, 1)}
            return out
