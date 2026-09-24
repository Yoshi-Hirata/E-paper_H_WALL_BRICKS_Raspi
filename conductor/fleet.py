"""The ten units as the show PC sees them: who answers, whose clock is
where, and the two-step cue sent to all of them at once.

Every unit runs an agent (ui/agent.py). This side polls each one over a
kept-alive HTTP connection, and every poll is also a clock measurement:

    offset = unit_monotonic - (t_sent + t_received) / 2

- the NTP idea, with the PC's own monotonic clock as the reference. The
round trip bounds the error (half of it, if the path were as lopsided as
it can be), so of the last few polls the one with the shortest round
trip is believed. On the show's WLAN that is 2-10 ms of round trip, a
few milliseconds of error - nothing against a repaint of seconds.

"Fire together" is then: pick one instant T on the PC's clock a little
ahead, and give each unit T + its offset, in its own clock. Whatever
the network does to the delivery of that message no longer matters, as
long as it arrives before T; a unit that gets it late fires at once and
reports by how much.

Running the show is the same idea once more. Every unit holds its whole
show file and keeps its own time (ui/showplay.py); all it needs from
here is T0, second 0 of the show, in its own clock. HOLD, RESUME and
NEXT only move T0. And because each poll reports the T0 a unit is
running on, this side can see a unit that is wrong - restarted, missed
the START, holds an old show - and put it right without being asked:
the supervision in _supervise().

Monotonic clocks restart with the machine. A unit that rebooted shows
up as an offset that jumped by more than any drift could explain, and
its measurements start over.

SEEK moves the show's position by hand, the way NEXT moves it - the
same T0 arithmetic, just to a position the operator chose instead of
the next cue.
"""

from __future__ import annotations

import http.client
import json
import socket
import threading
import time
from collections import deque

from . import timeline

DEFAULT_AGENT_PORT = 8787
POLL_S = 2.0
TIMEOUT_S = 1.5
SAMPLES = 8                # polls the best round trip is chosen from
STALE_S = 6.0              # no answer this long: shown as offline
JUMP_S = 0.5               # an offset moving more than this = a reboot
DEFAULT_LEAD_S = 3.0
T0_TOLERANCE_S = 0.05      # a unit's T0 further off than this is corrected
SUPERVISE_EVERY_S = 3.0    # at most one correction per unit in this time
DEMO_SAVE_TIMEOUT_S = TIMEOUT_S * 4    # /demo/save includes an eMMC write

# The PC's reference clock. Not time.monotonic(): on Windows that ticks
# every 15.6 ms (measured 2026-09-21: round trips of exactly 0, 15 or
# 31 ms), which is the whole error budget. perf_counter is monotonic
# too and resolves well under a microsecond everywhere.
pc_clock = time.perf_counter

# "This unit's status has no `burn` key at all" - an agent older than the
# pre-burn design - as opposed to a `burn` of None, which a current agent
# uses to say "nothing is written" (see Fleet._burn_problems).
_NO_BURN_KEY = object()


def default_units() -> "dict[str, str]":
    """radxa-NN -> 192.168.51.(100+NN), the addresses firstboot.sh gives."""
    return {f"radxa-{n:02d}": f"192.168.51.{100 + n}:{DEFAULT_AGENT_PORT}"
            for n in range(1, 11)}


class UnitLink:
    """One unit: its connection, its last status, its clock offset."""

    def __init__(self, name: str, address: str, token: "str | None" = None,
                 clock=pc_clock, timeout: float = TIMEOUT_S):
        self.name = name
        self.address = address
        host, _, port = address.partition(":")
        self._host, self._port = host, int(port or DEFAULT_AGENT_PORT)
        self._token = token
        self._clock = clock
        self._timeout = timeout
        self._poll_conn: "http.client.HTTPConnection | None" = None
        self._lock = threading.Lock()
        self._samples: "deque[tuple[float, float]]" = deque(maxlen=SAMPLES)
        self._suspect: "tuple[float, float] | None" = None

        self.status: "dict | None" = None
        self.last_seen: "float | None" = None
        self.error: "str | None" = None
        self.rtt: "float | None" = None

    # ---- HTTP ----

    def _connect(self, timeout: float) -> http.client.HTTPConnection:
        conn = http.client.HTTPConnection(self._host, self._port,
                                          timeout=timeout)
        conn.connect()
        # Small requests must leave at once: a held-back segment is a
        # lopsided round trip, and that is clock error (see ui/agent.py).
        conn.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return conn

    def _exchange(self, conn, method: str, path: str, body=None):
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["X-Show-Token"] = self._token
        data = json.dumps(body).encode() if body is not None else None
        sent = self._clock()
        conn.request(method, path, body=data, headers=headers)
        response = conn.getresponse()
        raw = response.read()
        received = self._clock()
        payload = json.loads(raw or b"{}")
        if response.status != 200:
            raise RuntimeError(payload.get("error") or f"HTTP {response.status}")
        return payload, sent, received

    def _learn(self, payload: dict, sent: float, received: float) -> None:
        """Every answer carries the unit's clock: one more measurement."""
        clock = payload.get("clock") or payload
        if "mono" not in clock:
            return
        rtt = received - sent
        offset = clock["mono"] - (sent + received) / 2
        with self._lock:
            best = self._best()
            if best is not None and abs(offset - best[1]) > JUMP_S:
                # A restarted unit - or one stalled packet behind a stage.
                # Only two in a row that agree with each other are a
                # restart; a lone outlier is dropped, the history kept.
                suspect, self._suspect = self._suspect, (rtt, offset)
                if suspect is None or abs(offset - suspect[1]) > JUMP_S:
                    self.rtt, self.last_seen, self.error = rtt, received, None
                    if "phase" in payload:
                        self.status = payload
                    return
                self._samples.clear()
                self._samples.append(suspect)
            self._suspect = None
            self._samples.append((rtt, offset))
            self.rtt = rtt
            if "phase" in payload:
                self.status = payload
            self.last_seen = received
            self.error = None

    def poll(self) -> bool:
        try:
            if self._poll_conn is None:
                self._poll_conn = self._connect(self._timeout)
            payload, sent, received = self._exchange(self._poll_conn, "GET",
                                                     "/status")
        except Exception as exc:            # noqa: BLE001 - offline is a state
            if self._poll_conn is not None:
                self._poll_conn.close()
            self._poll_conn = None
            with self._lock:
                self.error = str(exc) or exc.__class__.__name__
            return False
        self._learn(payload, sent, received)
        return True

    def post(self, path: str, body: dict, learn: bool = True,
             timeout: "float | None" = None) -> dict:
        """A command, on a connection of its own (any thread may call).
        `learn=False` skips feeding this round trip into the clock model:
        a reply whose timing has nothing to do with the network (writing
        a demo to eMMC, say) would poison the offset with a false "slow
        path". `timeout` overrides the usual 2x poll timeout for a
        command known to run long."""
        conn = self._connect(timeout if timeout is not None
                             else self._timeout * 2)
        try:
            payload, sent, received = self._exchange(conn, "POST", path, body)
        finally:
            conn.close()
        if learn:
            self._learn(payload, sent, received)
        return payload

    def get(self, path: str, learn: bool = True,
            timeout: "float | None" = None) -> dict:
        """A read, on a connection of its own - the same shape as post(),
        for a GET that is not the polled /status (e.g. /demo/list)."""
        conn = self._connect(timeout if timeout is not None
                             else self._timeout * 2)
        try:
            payload, sent, received = self._exchange(conn, "GET", path)
        finally:
            conn.close()
        if learn:
            self._learn(payload, sent, received)
        return payload

    # ---- what is known ----

    def _best(self) -> "tuple[float, float] | None":
        return min(self._samples) if self._samples else None

    @property
    def offset(self) -> "float | None":
        with self._lock:
            best = self._best()
            return None if best is None else best[1]

    @property
    def online(self) -> bool:
        return (self.last_seen is not None
                and self._clock() - self.last_seen < STALE_S)

    def snapshot(self) -> dict:
        with self._lock:
            best = self._best()
            status = self.status or {}
            return {
                "name": self.name, "address": self.address,
                "online": self.online, "error": self.error,
                "rtt_ms": None if self.rtt is None else round(self.rtt * 1000, 1),
                "sync_ms": None if best is None else round(best[0] * 500, 1),
                "samples": len(self._samples),
                "host": status.get("host"), "commit": status.get("commit"),
                "phase": status.get("phase"), "cue": status.get("cue"),
                "label": status.get("label"),
                "boards": len(status.get("boards", [])),
                "live": len(status.get("live", [])),
                "saved": len(status.get("saved", [])),
                "failed": status.get("failed", []),
                "prepare_s": status.get("prepare_s"),
                "late_ms": status.get("late_ms"),
                "demos": status.get("demos"),
                "unit_error": status.get("error"),
                "log": status.get("log", []),
                "show": status.get("show"),
            }


class Fleet:
    def __init__(self, units: "dict[str, str] | None" = None,
                 token: "str | None" = None, poll_s: float = POLL_S,
                 clock=pc_clock):
        self._clock = clock
        self.poll_s = poll_s
        self.links = {name: UnitLink(name, address, token, clock)
                      for name, address in (units or default_units()).items()}
        self._stop = threading.Event()
        self._threads: "list[threading.Thread]" = []
        self.last_fire: "dict | None" = None
        # The show: what each unit was given, and the run's T0 on the
        # PC's clock ({"t0", "state": running|holding, "held_at"}).
        self.shows: "dict[str, dict]" = {}
        self.run: "dict | None" = None
        # Where START begins when nothing says otherwise - moved by SEEK
        # while there is no run, reset by every START and STOP.
        self.start_at: float = 0.0
        self._run_lock = threading.Lock()
        # Bumped under _run_lock by every write to `run` (start, seek,
        # resume, next_cue, hold, stop, adopt). _supervise() reads it
        # alongside its copy of `run` and checks again, under the lock,
        # right before it posts a T0 that copy decided on - an operator's
        # command that landed in between makes that copy stale, and
        # supervision must not send it anyway (found in review, see §2.4).
        self._run_gen = 0
        # A run is adopted from the units only by a conductor that has
        # just come up and been told nothing yet; after an operator's
        # STOP, a unit still running is a unit that missed it.
        self._may_adopt = True
        self._stopped = False
        self._stop_told: "set[str]" = set()    # told once; not a tug of war
        # Units currently left alone because they play their own demo -
        # said once per episode (added when the demo starts, dropped the
        # moment it is not running/holding any more), never every poll.
        self._demo_told: "set[str]" = set()
        self._corrected: "dict[str, float]" = {}
        # Per unit, the last supervision command it refused and why -
        # so a standing refusal is written down once, not every retry.
        self._refused: "dict[str, tuple[str, str]]" = {}
        self.corrections: "list[str]" = []

    # ---- polling ----

    def start(self) -> None:
        for link in self.links.values():
            thread = threading.Thread(target=self._poll_loop, args=(link,),
                                      daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=3)

    def _poll_loop(self, link: UnitLink) -> None:
        while not self._stop.is_set():
            if link.poll():
                try:
                    self._supervise(link)
                except Exception as exc:    # noqa: BLE001 - next poll retries
                    link.error = f"supervise: {exc}"
            self._stop.wait(self.poll_s)

    def snapshot(self) -> dict:
        self._adopt()
        with self._run_lock:
            run = dict(self.run) if self.run else None
            # Meaningful only with no run: once one exists (live, or
            # adopted from the units) a leftover position is nobody's
            # business - showing it would invite "Back to 0:00" to seek a
            # LIVE show back to the top (found in review).
            start_at = self.start_at if run is None else 0.0
        if run:
            mark = run["held_at"] if run["state"] == "holding" else self._clock()
            run["now"] = round(mark - run["t0"], 2)
        duration = self.show_duration()
        # The summary line is purely informational (unlike the strict,
        # id-matched `_burn()` that gates START below): any unit currently
        # saying anything about a burn at all counts towards `total`,
        # whichever show it is about - so a unit still catching up to a
        # brand new upload is not silently left out of the denominator,
        # and neither is one whose pictures are NOT written (burn null,
        # "cancelled", "none"): those count against `burned`.
        reporting = [self._raw_burn(name) for name in self.shows]
        reporting = [b for b in reporting if b is not _NO_BURN_KEY]
        burned = sum(1 for b in reporting
                     if isinstance(b, dict) and b.get("state") == "burned")
        return {"units": [link.snapshot() for link in self.links.values()],
                "last_fire": self.last_fire, "run": run,
                "shows": {unit: {"id": show["id"], "cues": len(show["cues"])}
                          for unit, show in self.shows.items()},
                "corrections": self.corrections[-5:],
                "start_at": start_at,
                "show_duration": duration if self.shows else None,
                "burn": {"burned": burned, "total": len(reporting)}}

    def _raw_burn(self, name: str):
        """Whatever this unit currently reports as `status.show.burn`,
        regardless of which show it is about - for the informational
        summary in snapshot() only; START gating uses the stricter,
        id-matched `_burn()` below. `_NO_BURN_KEY` when the unit says
        nothing about burning at all (an older agent, or no show)."""
        link = self.links.get(name)
        if link is None:
            return _NO_BURN_KEY
        show = (link.status or {}).get("show")
        show = show if isinstance(show, dict) else {}
        return show.get("burn") if "burn" in show else _NO_BURN_KEY

    def _burn(self, name: str) -> "tuple[dict, object] | None":
        """(the unit's own `status.show`, its `.burn`) for a unit that is
        online AND currently holds the show THIS conductor uploaded under
        `name` - `None` for anything else (offline, never polled, or
        simply holding some other show), so a stale or foreign burn
        report is never mistaken for this show's own (found in review:
        an offline unit's last-known "burned", or a unit still showing a
        PREVIOUS upload's "burned", must never wave START through).

        `.burn` is then one of three things `_burn_problems` tells apart:
        `_NO_BURN_KEY` (the unit's status has no "burn" key at all - an
        older agent that says nothing about burning, which start_show()
        must not hold up), a dict with a "state", or `None`/junk (a NEW
        agent that DOES report burns saying "nothing is written" - a
        blocker, see the review's F1)."""
        link = self.links.get(name)
        if link is None or not link.online:
            return None
        show = (link.status or {}).get("show")
        show = show if isinstance(show, dict) else {}
        expected = (self.shows.get(name) or {}).get("id")
        if expected is None or show.get("id") != expected:
            return None
        return show, (show.get("burn") if "burn" in show else _NO_BURN_KEY)

    def _burn_problems(self, names, force: bool = False) -> "list[str]":
        """One message per unit (of `names`) START/PRESET must wait on:
        offline or not yet holding this show (always blocking), still
        burning or playing its own demo while burning (always blocking),
        pictures not written at all - a burn cancelled by STOP, lost to a
        unit restart, never started, or a state this conductor does not
        know (always blocking: Upload again), or failed to burn some
        boards (blocking unless `force` - the operator may choose to go
        on anyway, missing boards and all; the unit itself still refuses
        a `force` over a LIVE board that would not take the write).

        Unit-side contract (ui/showplay.py, 2026-09-25): a new agent
        always sends a "burn" dict for a loaded show, state one of
        "burning" | "burned" | "failed" | "cancelled" | "none"; an old
        agent has no "burn" key at all."""
        problems = []
        for name in names:
            found = self._burn(name)
            if found is None:
                link = self.links.get(name)
                if link is None or not link.online:
                    problems.append(f"{name}: not answering")
                else:
                    problems.append(f"{name}: has not taken this show yet")
                continue
            show, burn = found
            if burn is _NO_BURN_KEY:
                continue                    # older agent: not held up
            if not isinstance(burn, dict):
                problems.append(f"{name}: pictures not written - Upload again")
                continue
            state = burn.get("state")
            done, total = burn.get("done", 0), burn.get("total", "?")
            if state == "burning":
                if show.get("demo"):
                    problems.append(f"{name}: writing its demo pictures "
                                    f"({done}/{total}) - wait or STOP it")
                else:
                    problems.append(f"{name}: still writing {done}/{total}")
            elif state == "failed":
                if force:
                    continue
                failed = burn.get("failed") or []
                boards = sorted({pair[0] for pair in failed
                                if isinstance(pair, (list, tuple)) and pair})
                problems.append(
                    f"{name}: {len(boards) or len(failed)} board(s) not "
                    f"written" + (f" ({', '.join(map(str, boards))})"
                                 if boards else ""))
            elif state == "cancelled":
                problems.append(f"{name}: pictures not written (cancelled) "
                                "- Upload again")
            elif state == "none":
                problems.append(f"{name}: pictures not written since it "
                                "restarted - Upload again")
            elif state != "burned":
                problems.append(f"{name}: pictures not written ({state}) "
                                "- Upload again")
        return problems

    def show_duration(self) -> float:
        """The longest `duration` among the uploaded shows, 0.0 when none
        are uploaded (§2.1: the page then sees `show_duration: null`)."""
        return float(max((show.get("duration", 0.0)
                          for show in self.shows.values()), default=0.0))

    # ---- the show ----

    def upload(self, shows: "dict[str, dict]") -> "dict[str, dict]":
        def action(link):
            excuse = self._demo_excuse(link)
            if excuse:
                raise RuntimeError(excuse)
            status = link.post("/show/load", shows[link.name])
            return {"show": (status.get("show") or {}).get("id")}
        results = self._each(list(shows), action)
        self.shows = dict(shows)
        # A new show file is a new duration: a remembered position from
        # the old one may no longer even be inside it (found in review).
        with self._run_lock:
            self.start_at = 0.0
        return results

    # ---- the standalone demo: a named copy of the show, in a unit's own
    # menu, that plays without this PC. Independent of the run this
    # conductor is driving - it touches neither self.shows nor self.run.

    def write_demo(self, name: str, loop: bool, shows: "dict[str, dict]"
                   ) -> "dict[str, dict]":
        """Post each unit its own compiled show (`shows`, the same dict
        upload() sends via /show/load) to /demo/save under `name`, so the
        unit can play it from its own menu, on its own clock, without
        this PC. Only the units named in `shows` are written to - exactly
        upload()'s own targets. `learn=False`: a write includes an eMMC
        save on the unit's side, and its own longer timeout - neither
        belongs anywhere near the clock-offset model."""
        def action(link):
            result = link.post("/demo/save", {"name": name, "loop": bool(loop),
                                               "show": shows[link.name]},
                               learn=False, timeout=DEMO_SAVE_TIMEOUT_S)
            return {"slug": result.get("slug")}
        return self._each(list(shows), action)

    def list_demos(self) -> "dict[str, dict]":
        """Per unit: {"ok": True, "demos": [...]} from a GET /demo/list,
        or {"ok": False, "error": "offline"} for an unreachable one, or
        {"ok": False, "error": <the unit's own message>} for one that
        answered but refused (e.g. an older agent with no /demo/list at
        all) - every configured unit, not just those with a show
        uploaded (a demo written earlier outlives this conductor's own
        upload). The server tells the two failure kinds apart by the
        exact "offline" text."""
        def action(link):
            if not link.online:
                raise RuntimeError("offline")
            return {"demos": link.get("/demo/list").get("demos") or []}
        return self._each(list(self.links), action)

    def delete_demo(self, slug: str) -> "dict[str, dict]":
        """POST /demo/delete on every configured unit that is online -
        the same "offline" sentinel and skip as list_demos(), instead of
        waiting out a real unit's connection timeout for a delete that
        could not land anyway."""
        def action(link):
            if not link.online:
                raise RuntimeError("offline")
            result = link.post("/demo/delete", {"slug": slug}, learn=False)
            return {"demos": result.get("demos")}
        return self._each(list(self.links), action)

    @staticmethod
    def _burning_demo(unit: dict) -> bool:
        """A unit still writing the pictures of its own standalone demo
        (KEY1 on a demo row burns first, then runs - state "loaded" with
        `demo` set while the burn is in flight)."""
        burn = unit.get("burn")
        return (bool(unit.get("demo")) and unit.get("state") == "loaded"
                and isinstance(burn, dict) and burn.get("state") == "burning")

    @classmethod
    def _playing_demo(cls, link: "UnitLink") -> bool:
        """True only while this unit is actually running or holding its
        own standalone demo - or still writing that demo's pictures, the
        step right before it runs (found in review: a /show/load landing
        then would cancel the burn under the operator's feet) - not
        merely one it once played and has since stopped, ended, or gone
        back to its menu."""
        unit = (link.status or {}).get("show") or {}
        if not unit.get("demo"):
            return False
        return (unit.get("state") in ("running", "holding")
                or cls._burning_demo(unit))

    @classmethod
    def _demo_excuse(cls, link: "UnitLink") -> "str | None":
        """Why a command must leave this unit alone right now, in the
        operator's words - or None when the unit is not busy with its
        own demo."""
        if not cls._playing_demo(link):
            return None
        unit = (link.status or {}).get("show") or {}
        if cls._burning_demo(unit):
            burn = unit["burn"]
            return (f"writing its demo pictures ({burn.get('done', 0)}/"
                    f"{burn.get('total', '?')}) - wait or STOP it")
        return "playing a demo - press STOP first"

    def _send_run(self, names) -> "dict[str, dict]":
        with self._run_lock:
            # Re-checked, not just read: seek()/start_show() release the
            # lock before this runs, so a STOP or HOLD may have already
            # landed - posting the T0 they decided on would be exactly
            # the stale command supervision exists to correct, only sent
            # by the conductor itself (found in review).
            if not self.run or self.run["state"] != "running":
                return {}
            t0 = self.run["t0"]
            # START's `force` belongs to the whole run: the unit's own
            # gate must wave the same failed boards through for every
            # SEEK/RESUME/NEXT of this run too, not just the first /show/run.
            force = bool(self.run.get("force"))
        # The polls already in flight still show the old T0; give this
        # one time to land before the supervision second-guesses it.
        for name in names:
            self._corrected[name] = self._clock()

        def action(link):
            excuse = self._demo_excuse(link)
            if excuse:
                # The unit itself would refuse /show/run while its own
                # demo runs - said here too, so START/SEEK/RESUME/NEXT
                # never even try, and the operator sees exactly why.
                raise RuntimeError(excuse)
            offset = link.offset
            if offset is None:
                raise RuntimeError("clock not measured yet")
            show = self.shows.get(link.name)
            link.post("/show/run", {"t0": t0 + offset,
                                    "show": show["id"] if show else None,
                                    "force": force})
            return {}
        return self._each(list(names), action)

    def _targets(self) -> "list[str]":
        return list(self.shows) or [
            name for name, link in self.links.items()
            if (link.status or {}).get("show")]

    @staticmethod
    def _clamped(value: float, low: float, high: float) -> float:
        """`value` rounded to 0.1 s (§2.2/§2.3), then snapped to `low`/
        `high` when the rounding alone would put it just outside - a
        719.96 s show must not refuse a seek to its own end because
        719.96 rounds to 720.0 (found in review)."""
        value = round(float(value), 1)
        if value > high and value - high <= 0.1:
            value = high
        elif value < low and low - value <= 0.1:
            value = low
        return value

    def preset(self, force: bool = False) -> "dict[str, dict]":
        """Show the 0:00 look on every unit (`/show/preset`). Like START
        this waits for the burn: a unit still writing its pictures would
        refuse the preset anyway ("still writing the pictures: n/N"), so
        the operator gets the same per-unit list here instead of one
        error per tile. `force` is START's: it waves through a unit that
        FAILED to burn some boards (a board that is simply absent is the
        common case - the unit tolerates that too) and is posted to the
        unit as {"force": true}; never a unit still burning, offline, on
        another show, or with nothing written (cancelled/none)."""
        targets = self._targets()
        problems = self._burn_problems(targets, force=force)
        if problems:
            raise ValueError("; ".join(problems))
        body = {"force": bool(force)}
        return self._each(targets, lambda link: {
            "phase": link.post("/show/preset", body).get("phase")})

    def start_show(self, lead_s: float = DEFAULT_LEAD_S,
                   at: float = 0.0, force: bool = False) -> "dict[str, dict]":
        """Begin the show `lead_s` from now, `at` seconds into it (0.0 for
        the top). The caller resolves `at` itself - normally
        `fleet.start_at`, where a SEEK made before the show started (or
        nothing) left it - and this uses exactly the value it is given,
        never `self.start_at` again: the note a caller builds from `at`
        and the T0 this actually runs on can then never disagree (found
        in review). Out of range is the same ValueError seek() raises. A
        successful START always forgets `self.start_at` (§2.3).

        `force` (also what re-starts an already-running show, see
        server.py) additionally waves through a unit that failed to burn
        some boards - never one still burning, offline, holding some
        other show, or with nothing written: see `_burn_problems`. It is
        kept on the run (`run["force"]`) and posted with every /show/run
        of this run (_send_run, _supervise), so the unit's own gate waves
        the same boards through; the unit still refuses a force over a
        live board that would not take the write."""
        duration = self.show_duration()
        at = self._clamped(at, 0.0, duration)
        if not 0 <= at <= duration:
            raise ValueError(f"The show is {timeline.format_clock(0)} to "
                             f"{timeline.format_clock(duration)}.")
        # Every picture is meant to already be sitting in its slot: START
        # refuses while any unit is still writing them, offline, not yet
        # holding this show, or (unless `force`) failed to write some
        # boards (2026-09-24, the pre-burn design - see timeline.py).
        burning = self._burn_problems(self._targets(), force=force)
        if burning:
            raise ValueError("; ".join(burning))
        with self._run_lock:
            self._may_adopt, self._stopped = False, False
            self.run = {"t0": self._clock() + lead_s - at, "state": "running",
                        "held_at": None, "force": bool(force)}
            self.start_at = 0.0
            self._run_gen += 1
        return self._send_run(self._targets())

    def seek(self, to_s: float, lead_s: float = DEFAULT_LEAD_S
             ) -> "tuple[str, dict[str, dict]]":
        """Move the show to `to_s` seconds from its start (§2.4):

            running :  t0 = clock + lead_s - to_s  -> sent to every unit
            holding :  t0 = held_at - to_s          -> nothing sent
            no run  :  start_at = to_s              -> nothing sent

        Returns (mode, per-unit results); `to_s` outside 0..show_duration()
        is a ValueError, the plain-English range the page shows."""
        duration = self.show_duration()
        to_s = self._clamped(to_s, 0.0, duration)
        if not 0 <= to_s <= duration:
            raise ValueError(f"The show is {timeline.format_clock(0)} to "
                             f"{timeline.format_clock(duration)}.")
        send = False
        with self._run_lock:
            if self.run is None:
                self.start_at = to_s
                mode = "start_at"
            elif self.run["state"] == "holding":
                self.run["t0"] = self.run["held_at"] - to_s
                mode = "holding"
            else:
                self.run["t0"] = self._clock() + lead_s - to_s
                mode = "running"
                send = True
            self._run_gen += 1
        if send:
            return mode, self._send_run(self._targets())
        return mode, {}

    def hold(self) -> "dict[str, dict]":
        with self._run_lock:
            if not self.run or self.run["state"] != "running":
                return {}
            self.run.update(state="holding", held_at=self._clock())
            self._run_gen += 1
        return self.simple(self._targets(), "/show/hold")

    def resume(self) -> "dict[str, dict]":
        with self._run_lock:
            if not self.run or self.run["state"] != "holding":
                return {}
            self.run["t0"] += self._clock() - self.run["held_at"]
            self.run.update(state="running", held_at=None)
            self._run_gen += 1
        return self._send_run(self._targets())

    def next_cue(self, lead_s: float = DEFAULT_LEAD_S) -> "dict[str, dict]":
        """Bring the earliest upcoming cue of any unit to `lead_s` from now
        by moving T0 earlier - for every unit alike, so they stay in step."""
        with self._run_lock:
            if not self.run:
                return {}
            mark = (self.run["held_at"] if self.run["state"] == "holding"
                    else self._clock())
            now = mark - self.run["t0"]
            ahead = [cue["sent"] - now for show in self.shows.values()
                     for cue in show["cues"] if cue["sent"] > now]
            if not ahead:
                for link in self.links.values():
                    nxt = ((link.status or {}).get("show") or {}).get("next")
                    if nxt:
                        ahead.append(nxt["in_s"])
            if not ahead or min(ahead) <= lead_s:
                return {}
            self.run["t0"] -= min(ahead) - lead_s
            if self.run["state"] == "holding":  # NEXT also lets go of a hold
                self.run["t0"] += self._clock() - self.run["held_at"]
                self.run.update(state="running", held_at=None)
            self._run_gen += 1
        return self._send_run(self._targets())

    def stop_show(self) -> "dict[str, dict]":
        targets = self._targets()
        with self._run_lock:
            self._may_adopt, self._stopped = False, True
            self._stop_told = set()
            self._demo_told = set()     # the next demo episode is announced again
            self.run = None
            self.start_at = 0.0
            self._run_gen += 1
        return self.simple(targets, "/show/stop")

    def _supervise(self, link: UnitLink) -> None:
        """After every poll: is this unit running what it should, on the
        T0 it should? If not, tell it - nobody has to notice first."""
        with self._run_lock:
            run = dict(self.run) if self.run else None
            stopped = self._stopped
            gen = self._run_gen
        now = self._clock()
        if now - self._corrected.get(link.name, -1e9) < SUPERVISE_EVERY_S:
            return
        unit = (link.status or {}).get("show") or {}
        if run is None:
            # The operator stopped the show; a unit that was out of reach
            # then and still runs it has to be told now.
            if (stopped and unit.get("state") in ("running", "holding")
                    and link.name not in self._stop_told
                    and not self._playing_demo(link)):
                # A demo that started AFTER the STOP is not a missed STOP:
                # someone pressed KEY1 on the unit on purpose (found in the
                # final review - the first demo after every STOP used to
                # be killed within a poll, the second one survived).
                # Once per unit and STOP: a unit that missed it. One that
                # runs again after that was started by someone, on purpose.
                self._stop_told.add(link.name)
                link.post("/show/stop", {})
                self._corrected[link.name] = now
                self.corrections.append(f"{time.strftime('%H:%M:%S')} "
                                        f"{link.name}: stopped (missed STOP)")
                del self.corrections[:-20]
            return
        show = self.shows.get(link.name)
        if show is None or link.offset is None:
            return
        if self._playing_demo(link):
            # Playing its own standalone demo (the same player the fleet's
            # show would run on) - leave it alone. The unit itself refuses
            # /show/load and /show/run while a demo runs, so nothing here
            # would land anyway; forcing it would only fight an operator
            # who chose to play the demo on purpose. STOP still ends it
            # (the "missed STOP" branch above, unaffected by this return) -
            # said once per episode, not every poll, and forgotten the
            # moment the demo is no longer running/holding, so the next
            # one is announced too.
            if link.name not in self._demo_told:
                self._demo_told.add(link.name)
                doing = ("writing its demo pictures"
                         if self._burning_demo(unit) else "playing a demo")
                self.corrections.append(
                    f"{time.strftime('%H:%M:%S')} {link.name}: "
                    f"{doing}, left alone")
                del self.corrections[:-20]
            return
        self._demo_told.discard(link.name)
        why = None
        if unit.get("id") != show["id"]:
            why = "show reloaded"
            if not self._post_or_refused(link, now, "/show/load", show,
                                         "reload"):
                return
        elif (run["state"] != "holding"
              and (unit.get("burn") or {}).get("state") == "burning"):
            # Still writing its pictures after a reload (pre-burn): a
            # /show/run now would only be refused ("still writing the
            # pictures: n/N"). The tile's Pictures row shows the progress;
            # the run goes out on the first poll after the burn settles.
            return
        if run["state"] == "holding":
            if why or unit.get("state") == "running":
                # `run` was copied outside the lock, and `link.post` above
                # may have taken a moment: re-check that no SEEK/RESUME/
                # STOP landed since, or this would post the T0 or hold
                # decision they have already overtaken (found in review).
                with self._run_lock:
                    if self._run_gen != gen:
                        return
                link.post("/show/hold", {})
                why = why or "put on hold"
        else:
            expected = run["t0"] + link.offset
            # "ended" is a unit that ran the show to its last cue on this
            # T0 - done, not stopped (seen on radxa-01, 2026-09-21: the
            # finished unit was restarted every few seconds).
            # `synced` false is a unit running on the T0 it restored from
            # disk: even when that is close enough, say so, so it knows
            # (and shows) that the PC has confirmed it.
            if why == "show reloaded":
                # The load just started the unit's burn; running it is
                # the next poll's job, once the pictures are written
                # (the branch above waits for that). Said now, so the
                # operator sees why this unit is a few seconds behind.
                why = "show reloaded, writing its pictures"
            elif (unit.get("state") not in ("running", "ended")
                    or unit.get("t0") is None
                    or unit.get("synced") is False
                    or abs(unit["t0"] - expected) > T0_TOLERANCE_S):
                if now - run["t0"] > float(show.get("duration", 0)) + 30:
                    return                  # the show is over; leave it be
                with self._run_lock:
                    if self._run_gen != gen:
                        return              # stale: an operator beat us to it
                if not self._post_or_refused(
                        link, now, "/show/run",
                        {"t0": expected, "show": show["id"],
                         "force": bool(run.get("force"))}, "run"):
                    return
                why = why or ("started late" if unit.get("t0") is None
                              else "T0 confirmed after its restart"
                              if abs(unit["t0"] - expected) <= T0_TOLERANCE_S
                              else "T0 corrected")
        if why:
            self._corrected[link.name] = now
            self.corrections.append(
                f"{time.strftime('%H:%M:%S')} {link.name}: {why}")
            del self.corrections[:-20]

    def _post_or_refused(self, link: UnitLink, now: float, path: str,
                         body: dict, what: str) -> bool:
        """A supervision command to one unit. The unit is marked
        corrected BEFORE the post, so a refusal (409 - "board 7 did not
        take the burn", "unit is busy") is not retried on the very next
        poll but after SUPERVISE_EVERY_S like any other correction
        (found in review: a failed-burn unit was hammered every poll).
        A refusal is written to the corrections once per reason - not
        once per retry - as "<unit>: <what> refused: <reason>"; the
        entry is forgotten when a post to that unit lands."""
        self._corrected[link.name] = now
        try:
            link.post(path, body)
        except Exception as exc:            # noqa: BLE001 - said, not raised
            reason = str(exc) or exc.__class__.__name__
            if self._refused.get(link.name) != (what, reason):
                self._refused[link.name] = (what, reason)
                self.corrections.append(f"{time.strftime('%H:%M:%S')} "
                                        f"{link.name}: {what} refused: {reason}")
                del self.corrections[:-20]
            return False
        self._refused.pop(link.name, None)
        return True

    def _adopt(self) -> None:
        """A conductor restarted mid-show finds the run on the units."""
        with self._run_lock:
            if self.run is not None or not self._may_adopt:
                return
            found = []
            for link in self.links.values():
                unit = (link.status or {}).get("show") or {}
                # A unit playing its own standalone demo is not running
                # the fleet's show, even though its state also reads
                # "running" - adopting it would have this conductor try
                # to drive (and "correct") a demo nobody asked it to.
                if (link.online and unit.get("state") == "running"
                        and not self._playing_demo(link)
                        and unit.get("synced") and unit.get("t0") is not None
                        and link.offset is not None):
                    found.append(unit["t0"] - link.offset)
            if found:
                found.sort()
                self._may_adopt = False
                self.run = {"t0": found[len(found) // 2], "state": "running",
                            "held_at": None, "adopted": True}
                # Whatever a SEEK remembered before this conductor came
                # up (or restarted) is not where THIS run began - the
                # units, not the page, decided that (found in review).
                self.start_at = 0.0
                self._run_gen += 1

    # ---- commands, to many units at once ----

    def _each(self, names, action) -> "dict[str, dict]":
        """Run `action(link)` for every unit in parallel; never raises."""
        results: "dict[str, dict]" = {}

        def run(name):
            link = self.links.get(name)
            if link is None:
                results[name] = {"ok": False, "error": "unknown unit"}
                return
            try:
                results[name] = {"ok": True, **(action(link) or {})}
            except Exception as exc:        # noqa: BLE001 - reported per unit
                results[name] = {"ok": False,
                                 "error": str(exc) or exc.__class__.__name__}

        threads = [threading.Thread(target=run, args=(name,)) for name in names]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return results

    def prepare(self, payloads: "dict[str, dict]") -> "dict[str, dict]":
        """{unit: {"cue", "label", "dev_type", "boards"}} -> per-unit result."""
        def action(link):
            status = link.post("/prepare", payloads[link.name])
            return {"phase": status.get("phase")}
        return self._each(list(payloads), action)

    def fire(self, cues: "dict[str, str]", lead_s: float = DEFAULT_LEAD_S
             ) -> "dict[str, dict]":
        """{unit: cue id} -> all of them at one instant, `lead_s` from now."""
        instant = self._clock() + lead_s

        def action(link):
            offset = link.offset
            if offset is None:
                raise RuntimeError("clock not measured yet")
            link.post("/fire", {"cue": cues[link.name], "at": instant + offset})
            return {"at_unit": instant + offset}

        results = self._each(list(cues), action)
        self.last_fire = {"lead_s": lead_s, "units": sorted(cues),
                          "wall": time.time() + lead_s}
        return results

    def simple(self, names, path: str) -> "dict[str, dict]":
        """cancel / standby / release."""
        return self._each(list(names), lambda link: {
            "phase": link.post(path, {}).get("phase")})
