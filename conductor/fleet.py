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
                self._samples.clear()           # the unit restarted
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
            link.poll()
            self._stop.wait(self.poll_s)

    def snapshot(self) -> dict:
        return {"units": [link.snapshot() for link in self.links.values()],
                "last_fire": self.last_fire}

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
