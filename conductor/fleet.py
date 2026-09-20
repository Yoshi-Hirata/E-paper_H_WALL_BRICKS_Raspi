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
"""

from __future__ import annotations

import http.client
import json
import socket
import threading
import time
from collections import deque

DEFAULT_AGENT_PORT = 8787
POLL_S = 2.0
TIMEOUT_S = 1.5
SAMPLES = 8                # polls the best round trip is chosen from
STALE_S = 6.0              # no answer this long: shown as offline
JUMP_S = 0.5               # an offset moving more than this = a reboot
DEFAULT_LEAD_S = 3.0
T0_TOLERANCE_S = 0.05      # a unit's T0 further off than this is corrected
SUPERVISE_EVERY_S = 3.0    # at most one correction per unit in this time

# The PC's reference clock. Not time.monotonic(): on Windows that ticks
# every 15.6 ms (measured 2026-09-21: round trips of exactly 0, 15 or
# 31 ms), which is the whole error budget. perf_counter is monotonic
# too and resolves well under a microsecond everywhere.
pc_clock = time.perf_counter


def default_units() -> "dict[str, str]":
    """radxa-NN -> 192.168.50.(100+NN), the addresses firstboot.sh gives."""
    return {f"radxa-{n:02d}": f"192.168.50.{100 + n}:{DEFAULT_AGENT_PORT}"
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

    def post(self, path: str, body: dict) -> dict:
        """A command, on a connection of its own (any thread may call)."""
        conn = self._connect(self._timeout * 2)
        try:
            payload, sent, received = self._exchange(conn, "POST", path, body)
        finally:
            conn.close()
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
        self._run_lock = threading.Lock()
        # A run is adopted from the units only by a conductor that has
        # just come up and been told nothing yet; after an operator's
        # STOP, a unit still running is a unit that missed it.
        self._may_adopt = True
        self._stopped = False
        self._corrected: "dict[str, float]" = {}
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
        if run:
            mark = run["held_at"] if run["state"] == "holding" else self._clock()
            run["now"] = round(mark - run["t0"], 2)
        return {"units": [link.snapshot() for link in self.links.values()],
                "last_fire": self.last_fire, "run": run,
                "shows": {unit: {"id": show["id"], "cues": len(show["cues"])}
                          for unit, show in self.shows.items()},
                "corrections": self.corrections[-5:]}

    # ---- the show ----

    def upload(self, shows: "dict[str, dict]") -> "dict[str, dict]":
        def action(link):
            status = link.post("/show/load", shows[link.name])
            return {"show": (status.get("show") or {}).get("id")}
        results = self._each(list(shows), action)
        self.shows = dict(shows)
        return results

    def _send_run(self, names) -> "dict[str, dict]":
        with self._run_lock:
            t0 = self.run["t0"]
        # The polls already in flight still show the old T0; give this
        # one time to land before the supervision second-guesses it.
        for name in names:
            self._corrected[name] = self._clock()

        def action(link):
            offset = link.offset
            if offset is None:
                raise RuntimeError("clock not measured yet")
            show = self.shows.get(link.name)
            link.post("/show/run", {"t0": t0 + offset,
                                    "show": show["id"] if show else None})
            return {}
        return self._each(list(names), action)

    def _targets(self) -> "list[str]":
        return list(self.shows) or [
            name for name, link in self.links.items()
            if (link.status or {}).get("show")]

    def start_show(self, lead_s: float = DEFAULT_LEAD_S) -> "dict[str, dict]":
        with self._run_lock:
            self._may_adopt, self._stopped = False, False
            self.run = {"t0": self._clock() + lead_s, "state": "running",
                        "held_at": None}
        return self._send_run(self._targets())

    def hold(self) -> "dict[str, dict]":
        with self._run_lock:
            if not self.run or self.run["state"] != "running":
                return {}
            self.run.update(state="holding", held_at=self._clock())
        return self.simple(self._targets(), "/show/hold")

    def resume(self) -> "dict[str, dict]":
        with self._run_lock:
            if not self.run or self.run["state"] != "holding":
                return {}
            self.run["t0"] += self._clock() - self.run["held_at"]
            self.run.update(state="running", held_at=None)
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
        return self._send_run(self._targets())

    def stop_show(self) -> "dict[str, dict]":
        targets = self._targets()
        with self._run_lock:
            self._may_adopt, self._stopped = False, True
            self.run = None
        return self.simple(targets, "/show/stop")

    def _supervise(self, link: UnitLink) -> None:
        """After every poll: is this unit running what it should, on the
        T0 it should? If not, tell it - nobody has to notice first."""
        with self._run_lock:
            run = dict(self.run) if self.run else None
            stopped = self._stopped
        now = self._clock()
        if now - self._corrected.get(link.name, -1e9) < SUPERVISE_EVERY_S:
            return
        unit = (link.status or {}).get("show") or {}
        if run is None:
            # The operator stopped the show; a unit that was out of reach
            # then and still runs it has to be told now.
            if stopped and unit.get("state") in ("running", "holding"):
                link.post("/show/stop", {})
                self._corrected[link.name] = now
                self.corrections.append(f"{time.strftime('%H:%M:%S')} "
                                        f"{link.name}: stopped (missed STOP)")
                del self.corrections[:-20]
            return
        show = self.shows.get(link.name)
        if show is None or link.offset is None:
            return
        why = None
        if unit.get("id") != show["id"]:
            why = "show reloaded"
            link.post("/show/load", show)
        if run["state"] == "holding":
            if why or unit.get("state") == "running":
                link.post("/show/hold", {})
                why = why or "put on hold"
        else:
            expected = run["t0"] + link.offset
            # "ended" is a unit that ran the show to its last cue on this
            # T0 - done, not stopped (seen on radxa-01, 2026-09-21: the
            # finished unit was restarted every few seconds).
            if (why or unit.get("state") not in ("running", "ended")
                    or unit.get("t0") is None
                    or abs(unit["t0"] - expected) > T0_TOLERANCE_S):
                if now - run["t0"] > float(show.get("duration", 0)) + 30:
                    return                  # the show is over; leave it be
                link.post("/show/run", {"t0": expected, "show": show["id"]})
                why = why or ("T0 corrected" if unit.get("t0") is not None
                              else "started late")
        if why:
            self._corrected[link.name] = now
            self.corrections.append(
                f"{time.strftime('%H:%M:%S')} {link.name}: {why}")
            del self.corrections[:-20]

    def _adopt(self) -> None:
        """A conductor restarted mid-show finds the run on the units."""
        with self._run_lock:
            if self.run is not None or not self._may_adopt:
                return
            found = []
            for link in self.links.values():
                unit = (link.status or {}).get("show") or {}
                if (link.online and unit.get("state") == "running"
                        and unit.get("synced") and unit.get("t0") is not None
                        and link.offset is not None):
                    found.append(unit["t0"] - link.offset)
            if found:
                found.sort()
                self._may_adopt = False
                self.run = {"t0": found[len(found) // 2], "state": "running",
                            "held_at": None, "adopted": True}

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
