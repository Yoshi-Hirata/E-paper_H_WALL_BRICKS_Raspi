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
# POST /bus/recover holds the connection for as long as the recovery takes:
# the unit bounds its own work at ~20 s (every limit at once; ~6 s typical)
# and its wait at 22 s (ui/remote.py's RECOVER_WAIT_S), so this has to
# outlive that and say "no answer" only when the unit really has stopped
# answering.
RECOVER_TIMEOUT_S = 25.0
# How often a unit is asked what demos it holds (GET /demo/list, from the
# poll loop). The store only changes when someone writes or deletes one -
# and those update the cache straight from the answer - so this is just
# the safety net for a demo written by another PC, or a unit that came
# back. Every tile shows the cached answer, so no tile costs a request.
DEMO_LIST_EVERY_S = 10.0
# How long past the show's own length a run has to be before the automatic
# clear is asked for (see Fleet._clear_after_end). The unit waits for its
# own guard floor - the last cue's refresh and sweep - before the first
# 0x14, so this only has to be past the END, not past the repaint; the
# slack is there so a clock a hair ahead cannot clear a show still on its
# last cue.
CLEAR_AFTER_END_S = 5.0
# ...and how long a STOP waits. STOP is also how a director aborts a show
# mid-way - the ordinary reason to press it - and a clear cannot be undone
# without a three-minute Upload, so the operator gets a window in which START,
# PRESET, RESUME, NEXT, a seek or an Upload takes it back (review, 2026-09-27:
# a mid-show abort used to cost the re-Upload, with a confirm dialog that said
# nothing about it). The natural END does not wait - it is over either way.
CLEAR_AFTER_STOP_S = 30.0
# An END clear also stands aside for this long after any T0 MOVE. A seek or a
# NEXT that lands at (or a hair before) the end would otherwise arm the
# irreversible clear on the spot, because the end was only ever read off the
# clock: fleet.seek() clamps to `duration` inclusive, so "go to the end to see
# the last look" deleted the show (review, 2026-09-27).
CLEAR_AFTER_MOVE_S = 30.0
# A T0 move landing this close to the end (or past it) is a JUMP to the end,
# never playing to it: no amount of waiting turns it into one, so the END
# clear never fires on that T0 at all. STOP is how the operator ends such a
# run, and STOP has its own window above.
END_REACH_MARGIN_S = 1.0
# EXHIBITION mode's Loop (2026-09-30): how often the loop thread looks at the
# run, and how long it waits before trying a restart again when the fleet
# refused one (a unit offline, its pictures gone) - the same refusal a ③ START
# press would get, said once in the corrections and retried until STOP.
LOOP_TICK_S = 0.25
LOOP_RETRY_S = 5.0
# ...and how long past its wait the loop keeps every unit waiting for one
# that is not ready (offline, still writing). After this it starts with the
# units that are ready and names the others (PM decision, 2026-09-30): one
# garment must never freeze the whole exhibition. The others are still
# polled and supervised, and rejoin at the next restart.
LOOP_WAIT_READY_S = 60.0
# The fleet-wide Wi-Fi switch (EXHIBITION mode): the side that must move
# first gets the short lead, the other side the long one, so the hotspot is
# up before its clients look for it and down only after they have left.
WIFI_SWITCH_SOON_S = 5.0
WIFI_SWITCH_RANGE_S = (3.0, 120.0)      # what the unit's /wifi/select takes
DEFAULT_HOTSPOT_UNIT = "radxa-05"
HOTSPOT_PROFILE = "AZ-Epaper"

# The PC's reference clock. Not time.monotonic(): on Windows that ticks
# every 15.6 ms (measured 2026-09-21: round trips of exactly 0, 15 or
# 31 ms), which is the whole error budget. perf_counter is monotonic
# too and resolves well under a microsecond everywhere.
pc_clock = time.perf_counter

# "This unit's status has no `burn` key at all" - an agent older than the
# pre-burn design - as opposed to a `burn` of None, which a current agent
# uses to say "nothing is written" (see Fleet._burn_problems).
_NO_BURN_KEY = object()
# How many board numbers a "not written" message names before it rounds
# the rest up as "+k more" (the page uses the same three).
_NAME_BOARDS = 3


def pictures_not_written(burn: dict) -> str:
    """"10 of 12 pictures not written on boards 1, 2, 3" for a burn that
    failed on live boards.

    The unit counts PAIRS - one picture per (board, slot) - and a wall
    of 12 boards x 18 cues loses 18 pictures when one board refuses, not
    "1 board". Saying "3 board(s) not written" made a whole garment's
    worth of missing pictures sound like a footnote (review round 2,
    2026-09-25).

    A whole garment that never answered is the unit's own sentence
    instead ("none of its 16 boards answered"): a feed switched off is
    an ordinary thing on a show day, and listing sixteen boards would
    bury it."""
    if burn.get("reason"):
        return str(burn["reason"])
    failed = [pair for pair in (burn.get("failed") or [])
              if isinstance(pair, (list, tuple)) and pair]
    boards = sorted({pair[0] for pair in failed})
    total = burn.get("total")
    count = len(failed) or len(boards)
    named = ", ".join(str(b) for b in boards[:_NAME_BOARDS])
    if len(boards) > _NAME_BOARDS:
        named += f" +{len(boards) - _NAME_BOARDS} more"
    return (f"{count} of {total if isinstance(total, int) else '?'} "
            f"pictures not written"
            + (f" on board{'' if len(boards) == 1 else 's'} {named}"
               if boards else ""))


def _too_old_for_clear(exc: Exception) -> bool:
    """Is this the 404 of an agent that has no /show/clear at all?

    UnitLink._exchange() turns a non-200 into a RuntimeError carrying the
    body's own "error" - which for any unknown path on the unit's agent is
    the literal "not found" (ui/agent.py) - or "HTTP 404" when the body
    said nothing. Either one means "too old", never a refusal this
    conductor should hold the other nine units up over.
    """
    text = str(exc).lower()
    return "not found" in text or "404" in text


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
                # NOT a lag: half of the best round trip, i.e. the WIDTH of
                # the error bar on this unit's clock ("+-3 ms"). The tile
                # calls it "clock accuracy" for that reason. How far the
                # unit's show clock actually is from this PC's is
                # `show_lag_ms`, added by Fleet._unit_snapshot() - the raw
                # `offset` cannot be it, since the two monotonic clocks
                # count from two different boots and their difference is
                # some arbitrary number of seconds.
                "sync_ms": None if best is None else round(best[0] * 500, 1),
                "samples": len(self._samples),
                "host": status.get("host"), "commit": status.get("commit"),
                "phase": status.get("phase"), "cue": status.get("cue"),
                "label": status.get("label"),
                "boards": len(status.get("boards", [])),
                "live": len(status.get("live", [])),
                # ...and WHICH ones are answering, not only how many. A
                # unit can carry two garments (a top and its skirt on one
                # Radxa); "11 of 12 answering" then says nothing about
                # which garment lost a board, and the NOW -> NEXT board
                # has a row for each of them to colour.
                "live_ids": list(status.get("live", [])),
                # The board list the unit is really working to, passed
                # through as it comes: the ids it counts as its own, the
                # ones it is still probing, where the list came from
                # ("show" / "explore" / "fixed") and the group_count its
                # frames carry. The tile compares this against the ids
                # the show gives that unit - 2026-09-26 went wrong
                # inside the unit's own bookkeeping while every picture
                # was written, so nothing on the page said a word.
                # Absent from an agent too old to report them.
                "board_ids": list(status.get("boards", [])),
                "absent": list(status.get("absent", [])),
                "boards_source": status.get("boards_source"),
                "group_count": status.get("group_count"),
                # How long the unit says it has been up (ui/agent.py), so
                # the tile can see a restart that happened after the
                # Upload - and so the NOW -> NEXT board's vitals can say
                # "restarted 3 min ago", which is the one thing that
                # explains a unit that lost its pictures mid-show.
                "uptime_s": status.get("uptime_s"),
                "saved": len(status.get("saved", [])),
                "failed": status.get("failed", []),
                "prepare_s": status.get("prepare_s"),
                "late_ms": status.get("late_ms"),
                # The unit's own landing check on the last cue it fired
                # (ui/remote.py's RemoteSession.verify): None from an
                # agent too old to run one, and nothing to show when the
                # broadcast simply landed - the tile only marks a cue
                # that had to be re-sent, or one that could not be
                # confirmed even then.
                "verify": status.get("verify"),
                # The unit's last blocked broadcast write, if any
                # (ui/runner.py's STALL_LOG_MS). A board that is
                # repainting stops taking USB, and a cue written into
                # that window is lost rather than late - this is the
                # only place that says so out loud.
                "bus_stall": status.get("bus_stall"),
                # What the unit's last bus recovery took, if any
                # (ui/runner.py's _recover_bus()): {"by", "before_ms",
                # "after_ms", "ago_s", "count"}. Missing altogether from
                # an agent too old to have the endpoint - which is also
                # the one that answers 404 to POST /bus/recover, so the
                # page's "Recover bus" says "too old" rather than
                # pretending it worked.
                "bus_recovery": status.get("bus_recovery"),
                # Whether the unit can reset its master's USB at all - the
                # one cure for a degraded bus (ui/runner.py's
                # _check_usb_reset()). False puts an amber "no usb reset on
                # this unit" on the tile; None (not asked yet, or an agent
                # too old to say) puts nothing.
                "usb_reset_ok": status.get("usb_reset_ok"),
                # The board on the unit's USB cable (its master), as the
                # unit read it off the USB descriptor: {"serial", "family"}
                # - family "324C" like most of the fleet, or "3930" /
                # "other", which the tile marks amber (two 3930 boards
                # behaved differently, 2026-09-28). None until the unit's
                # worker has had the port, or from an agent too old to say.
                "usb_board": status.get("usb_board"),
                # Whether the PC's worker holds the unit's port - the only
                # time Recover bus means anything (ui/remote.py's owned()).
                # Missing from an agent too old to say, which also has no
                # /bus/recover: the page offers it no button and its sweep
                # skips it.
                "owned": status.get("owned"),
                # Taking the show's pictures back out of slots 1-18 once
                # the show is over (ui/remote.py's clear()): {"state",
                # "done", "total", "failed"}, state "none" until one is
                # asked for. Missing altogether from an agent too old to
                # clear - which is also the one that answers 404 to
                # POST /show/clear (see Fleet._clear_units).
                "clear": status.get("clear"),
                # How many demos the unit says it holds, in its own poll
                # answer (None from an agent too old to count them). What
                # those demos ARE is the fleet's cached /demo/list, added
                # as `demos` by Fleet._unit_snapshot().
                "demo_count": status.get("demos"),
                # The unit's Wi-Fi as its agent reports it (EXHIBITION
                # mode, 2026-09-30): {"ssid", "ip", "signal", "mode",
                # "profile"} - mode "hotspot" on the unit that IS the
                # AZ-Epaper hotspot, where signal is null. Absent from an
                # agent too old to say; passed through as it comes.
                "wifi": status.get("wifi"),
                "unit_error": status.get("error"),
                "log": status.get("log", []),
                "show": status.get("show"),
            }


class Fleet:
    def __init__(self, units: "dict[str, str] | None" = None,
                 token: "str | None" = None, poll_s: float = POLL_S,
                 clock=pc_clock,
                 clear_after_stop_s: float = CLEAR_AFTER_STOP_S,
                 clear_after_move_s: float = CLEAR_AFTER_MOVE_S,
                 loop_settings=None, loop_tick_s: float = LOOP_TICK_S,
                 loop_retry_s: float = LOOP_RETRY_S):
        self._clock = clock
        self.poll_s = poll_s
        # EXHIBITION mode's Loop: `loop_settings()` answers (wait_s, lead_s)
        # while THE SHOW's Loop is on, None while it is off - the server
        # hands in the workspace's own show.json (loop_wait_s and the
        # countdown before START), so this class never reads a file. Asked
        # when a run reaches its end (to arm the wait) and again when the
        # wait is up (a Loop turned off meanwhile cancels it). See
        # _loop_tick().
        self.loop_settings = loop_settings
        self.loop_tick_s = loop_tick_s
        self.loop_retry_s = loop_retry_s
        # When the next run begins on this PC's clock, or None while no loop
        # restart is pending; the wait and lead it was armed with; how many
        # times the loop has restarted this show (the START press is 0); and
        # the last refusal the restart met, said once.
        self._loop_at: "float | None" = None
        self._loop_wait: float = 0.0
        self._loop_lead: float = 0.0
        self._loop_runs: int = 0
        self._loop_problem: "str | None" = None
        # When the wait first ran out (the LOOP_WAIT_READY_S clock starts
        # there), and the refusals already written down for this wait.
        self._loop_due: "float | None" = None
        self._loop_said: "set[str]" = set()
        # What THIS conductor's workspace compiles to, offered for adoption:
        # a conductor restarted (systemd, a power blip) knows nothing about
        # who holds what, and a unit that reports exactly one of these show
        # ids with its pictures burned is taken as holding it (see
        # offer_shows / _adopt_show) - so the Loop and START work again
        # without an Upload of every picture. Per unit, once per offer.
        self._offered: "dict[str, dict]" = {}
        self._offer_said: "set[str]" = set()
        # The two windows the clear after the show waits out: after a STOP
        # (the director's mid-show abort), and after any T0 move (so a seek
        # in the last seconds cannot delete the show). Knobs so the tests
        # can compress them; the constants are the real thing.
        self.clear_after_stop_s = clear_after_stop_s
        self.clear_after_move_s = clear_after_move_s
        self.links = {name: UnitLink(name, address, token, clock)
                      for name, address in (units or default_units()).items()}
        self._stop = threading.Event()
        self._threads: "list[threading.Thread]" = []
        self.last_fire: "dict | None" = None
        # The show: what each unit was given, and the run's T0 on the
        # PC's clock ({"t0", "state": running|holding, "held_at"}).
        self.shows: "dict[str, dict]" = {}
        # When this conductor last wrote a show to each unit (its own
        # clock). The unit's `uptime_s` beside it is what says the unit
        # has restarted since - which, until the show file's list is
        # handed over again, is the state radxa-04 spent a whole
        # rehearsal in. Empty after a conductor restart: nothing is
        # claimed then, the page simply shows no mark.
        self._uploaded_at: "dict[str, float]" = {}
        # What each unit said its T0 was at the instant this side last
        # moved T0, and for which move each unit has since been seen on
        # the new one. Both are `show_lag_ms`'s gate - see _t0_moved().
        self._t0_was: "dict[str, float | None]" = {}
        self._t0_seen: "dict[str, float]" = {}
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
        # Units already asked to clear their slots after THIS run, and the
        # ones whose agent is too old to know how ("unit too old for
        # clear"). Both are per run: forgotten by every START, and per
        # unit by an Upload that lands on it (the pictures are back, so
        # this show's end may ask again). Deliberately NOT forgotten by
        # STOP - the end of the run and the STOP behind it are the same
        # show ending, and the unit is asked once for the two of them.
        self._clear_told: "set[str]" = set()
        self._clear_too_old: "set[str]" = set()
        # A STOP's clear, waiting out CLEAR_AFTER_STOP_S: when it becomes
        # due (this PC's clock) and which units it is for. None when none
        # is armed. Anything that means "the show is not over after all" -
        # START, PRESET, RESUME, NEXT, a seek, an Upload - drops it.
        self._clear_at: "float | None" = None
        self._clear_armed: "list[str]" = []
        # Units currently left alone because they play their own demo -
        # said once per episode (added when the demo starts, dropped the
        # moment it is not running/holding any more), never every poll.
        self._demo_told: "set[str]" = set()
        # What each unit holds in its own menu: the last GET /demo/list
        # answer (a list), or None for "not known" (never asked yet, an
        # agent with no /demo/list, a unit that did not answer). Refreshed
        # from the poll loop every DEMO_LIST_EVERY_S and straight from the
        # answer of a write or a delete, so a tile never costs a request.
        self._demos: "dict[str, list | None]" = {}
        self._demos_at: "dict[str, float]" = {}
        self._demos_lock = threading.Lock()
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
        # The demo listings have a thread of their own: a unit's /status
        # poll is also its clock measurement and the supervision's
        # heartbeat, and must not wait behind an eMMC listing (measured
        # 2026-09-25: doing it inline stretched that unit's 2 s cadence to
        # 3.5 s every time it came round).
        demos = threading.Thread(target=self._demo_loop, daemon=True)
        demos.start()
        self._threads.append(demos)
        # The Loop has a thread of its own too: it must notice the end of a
        # run and fire the restart on time whether or not any unit is
        # answering polls (the exhibition's units come and go with their
        # garments), and _supervise() only runs for a unit that answered.
        looper = threading.Thread(target=self._loop_loop, daemon=True)
        looper.start()
        self._threads.append(looper)

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

    # ---- EXHIBITION mode's Loop ----
    # The Conductor runs headless on a unit at the exhibition, and the show
    # has to play itself all day: when THE SHOW's Loop is on and a run
    # reaches its end, the Conductor waits `loop_wait_s` and then starts
    # again exactly as a ③ START press would - the countdown included, so
    # the 0:00 cue puts the first look back. STOP (and any other move of the
    # run) cancels the pending restart; the loop arms again the next time a
    # run plays to its end.

    def _loop_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._loop_tick()
            except Exception as exc:        # noqa: BLE001 - never ends this
                self.corrections.append(f"{time.strftime('%H:%M:%S')} "
                                        f"loop: {exc}")
                del self.corrections[:-20]
            self._stop.wait(self.loop_tick_s)

    def _loop_tick(self) -> None:
        """One look at the run: arm the wait when a run has reached its
        end, fire the restart when the wait is up.

        "Reached its end" is the same clock reading everything else calls
        the end of a run - the position on this PC's clock is at or past
        the show's length (the page's ENDED, the music's stop). A run on
        HOLD is not over, and a STOP has no run at all: neither arms
        anything, and both cancel a wait already armed (_t0_moved).

        The restart goes through start_show() with the show's countdown
        as its lead and NEVER with `force` (a failed board is the
        operator's call, once, at the START press - a restart at 3 a.m.
        does not make it again). A refusal - a unit not answering, its
        pictures cleared - is the same answer a ③ START press would get:
        written to the corrections once per distinct reason and tried
        again every loop_retry_s. For LOOP_WAIT_READY_S past the wait,
        that is; after that the run starts with the units that ARE ready
        and names the others (`problem`, and once in the corrections) -
        they stay polled and supervised and rejoin at the next restart.
        STOP takes the loop back at any point, the restart's own window
        included (the run generation is checked under the lock inside
        start_show).
        """
        with self._run_lock:
            run = dict(self.run) if self.run else None
            at = self._loop_at
            gen = self._run_gen
        if run is None or run["state"] != "running":
            return
        now = self._clock()
        if at is None:
            duration = self.show_duration()
            if duration <= 0 or now - run["t0"] < duration:
                return
            settings = self.loop_settings() if self.loop_settings else None
            if not settings:
                return                      # Loop off: the run ends as ever
            wait_s, lead_s = settings
            with self._run_lock:
                if self._run_gen != gen or self._loop_at is not None:
                    return                  # moved meanwhile: read it again
                self._loop_at = now + float(wait_s)
                self._loop_due = None
                self._loop_wait, self._loop_lead = float(wait_s), float(lead_s)
                self._loop_problem = None
                self._loop_said = set()
            self._note(f"show ended - Loop: next run in {float(wait_s):.0f} s")
            return
        if now < at:
            return
        # Read again at the moment it matters: a Loop turned off during
        # the wait means no restart, and a countdown changed meanwhile is
        # the one this run counts down.
        settings = self.loop_settings() if self.loop_settings else None
        if not settings:
            with self._run_lock:
                self._loop_at = None
            self._note("Loop turned off - no next run")
            return
        _wait_s, lead_s = settings
        with self._run_lock:
            if self._loop_due is None:
                self._loop_due = now
            overdue = now - self._loop_due >= LOOP_WAIT_READY_S
        skip: "set[str]" = set()
        if overdue:
            # The 60 s of grace are up: whoever is not ready is left out
            # of this run, by name, and the rest go on.
            skip = set(self._not_ready(self._targets()))
        try:
            results = self.start_show(float(lead_s), 0.0, loop=True,
                                      skip=skip, expect_gen=gen)
        except ValueError as exc:
            reason = str(exc)
            with self._run_lock:
                if self._run_gen != gen:
                    return                  # STOP or a move beat us to it
                self._loop_at = now + self.loop_retry_s
                said = reason in self._loop_said
                self._loop_said.add(reason)
                self._loop_problem = reason
            if not said:
                self._note(f"Loop: cannot start again yet ({reason}) - "
                           f"trying every {self.loop_retry_s:.0f} s")
            return
        failed = sorted(name for name, r in results.items() if not r.get("ok"))
        left_out = sorted(skip)
        problem = None
        if left_out:
            problem = (f"started without {', '.join(left_out)} - not ready "
                       f"after {LOOP_WAIT_READY_S:.0f} s; rejoins at the next run")
        with self._run_lock:
            self._loop_problem = problem
        self._note(f"Loop: run {self._loop_runs} started"
                   + (f" without {', '.join(left_out)} (not ready)" if left_out else "")
                   + (f" ({', '.join(failed)} did not take it)" if failed else ""))

    def _note(self, text: str) -> None:
        """One line in the corrections, the way every other note is made."""
        self.corrections.append(f"{time.strftime('%H:%M:%S')} {text}")
        del self.corrections[:-20]

    def _not_ready(self, names) -> "list[str]":
        """The units of `names` a START would refuse on right now (offline,
        not holding the show, still writing, pictures cleared) - each
        message from _burn_problems starts with the unit's name."""
        problems = self._burn_problems(names, force=False)
        return [name for name in names
                if any(p.startswith(f"{name}:") for p in problems)]

    def loop_state(self) -> "dict | None":
        """What /api/fleet says about a pending loop restart: seconds until
        the next run (`next_in_s`), the wait and lead it was armed with,
        how many restarts this show has had, and the refusal the restart
        is waiting out, if any. None while no restart is pending."""
        with self._run_lock:
            if self.run is None:
                return None
            if self._loop_at is None:
                # Nothing pending - but a run started without somebody
                # still says so (the page's hint), until the next end.
                return ({"next_in_s": None, "wait_s": self._loop_wait,
                         "lead_s": self._loop_lead, "runs": self._loop_runs,
                         "problem": self._loop_problem}
                        if self._loop_problem else None)
            return {"next_in_s": max(0.0, round(self._loop_at - self._clock(), 2)),
                    "wait_s": self._loop_wait, "lead_s": self._loop_lead,
                    "runs": self._loop_runs, "problem": self._loop_problem}

    def run_is_over(self) -> bool:
        """A running run whose position is at or past the show's length -
        ENDED on the page, a Loop wait in progress or not. A START then is
        not a restart of a running show (no `force` asked for)."""
        with self._run_lock:
            run = dict(self.run) if self.run else None
        if not run or run["state"] != "running":
            return False
        duration = self.show_duration()
        return duration > 0 and self._clock() - run["t0"] >= duration

    # ---- what the units already hold, after a restart of this conductor ----

    def offer_shows(self, shows: "dict[str, dict]") -> None:
        """Offer this workspace's compiled shows ({unit: show}) for
        adoption: a unit that reports one of these ids with its pictures
        burned is taken as holding it (_adopt_show, from its next poll).
        Called once at startup, and with {} to withdraw the offer (an
        import: the workspace is somebody else's show now). A member with
        no show of its own (radxa-05, control only) is simply not in it."""
        self._offered = dict(shows)
        self._offer_said = set()

    def _adopt_show(self, link: UnitLink) -> None:
        """Take the offered show for this unit if the unit says it holds
        exactly that - the same id, pictures burned - and say so once;
        say once, too, when it holds something else (an Upload is needed)."""
        offered = self._offered.get(link.name)
        if offered is None or link.name in self.shows:
            return
        unit = (link.status or {}).get("show")
        if not isinstance(unit, dict) or unit.get("id") is None:
            return
        burn = unit.get("burn")
        burned = isinstance(burn, dict) and burn.get("state") == "burned"
        if unit.get("id") == offered["id"] and burned:
            self.shows[link.name] = offered
            self._note(f"{link.name}: holds this show already (adopted "
                       "after a restart of the conductor)")
            return
        if link.name not in self._offer_said:
            self._offer_said.add(link.name)
            why = ("another show" if unit.get("id") != offered["id"]
                   else f"pictures {(burn or {}).get('state') if isinstance(burn, dict) else 'not written'}")
            self._note(f"{link.name}: {why} - Upload before START")

    def run_snapshot(self) -> "tuple[dict | None, float]":
        """(a copy of the run, the show's length) under the run lock - what
        the Conductor host's own speaker follows (conductor/speaker.py),
        from its own thread, without the page's whole snapshot()."""
        with self._run_lock:
            run = dict(self.run) if self.run else None
        return run, self.show_duration()

    # ---- what each unit holds in its own menu ----

    def _demo_loop(self) -> None:
        """One thread for all of them: every unit that has answered a poll
        is asked what it holds every DEMO_LIST_EVERY_S. A unit that is
        slow to answer (or not answering at all) delays only the other
        units' listings, never anyone's /status."""
        while not self._stop.is_set():
            for link in list(self.links.values()):
                if self._stop.is_set():
                    break
                if not link.online:
                    continue
                try:
                    self._poll_demos(link)
                except Exception as exc:    # noqa: BLE001 - never ends this
                    # One unit answering something unexpected must not
                    # take the listings of the other nine with it (the
                    # poll loop guards its supervision the same way).
                    link.error = f"demo list: {exc}"
            self._stop.wait(1.0)

    def _poll_demos(self, link: UnitLink) -> None:
        """Every DEMO_LIST_EVERY_S: what demos does this unit hold?
        Timestamped BEFORE the request, and NOT reset by a failure, so a
        unit that refuses /demo/list (an older agent: a 404 every time) or
        times out is asked once every ten seconds - never on every pass
        (found in review). A failure is "not known" (None), not "none
        stored" - the page must not read an old agent's 404 as an empty
        menu. Its own short timeout: this is a listing, not a command."""
        now = self._clock()
        with self._demos_lock:
            if now - self._demos_at.get(link.name, -1e9) < DEMO_LIST_EVERY_S:
                return
            self._demos_at[link.name] = now
        try:
            demos = link.get("/demo/list", learn=False,
                             timeout=TIMEOUT_S).get("demos")
        except Exception:                   # noqa: BLE001 - offline is a state
            demos = None
        self.remember_demos(link.name, demos, retry_soon=False)

    def remember_demos(self, name: str, demos, retry_soon: bool = True) -> None:
        """The answer of a /demo/list, /demo/save or /demo/delete: what
        this unit holds, as of now. `None` is "not known".

        `retry_soon` (the write path) then asks again on the next pass
        instead of in ten seconds - a /demo/save whose answer did not
        carry the new menu leaves the tiles blank until it does. The poll
        path passes False: a unit that cannot answer at all must not be
        asked every second for the rest of the night."""
        with self._demos_lock:
            self._demos[name] = list(demos) if isinstance(demos, list) else None
            if demos is None and retry_soon:
                self._demos_at[name] = -1e9

    def demos_of(self, name: str) -> "list[dict] | None":
        """The unit's stored demos for the snapshot: one entry per demo,
        each with the `current` flag the page's chips are built from -
        True when the demo was written from the show THIS conductor last
        uploaded to that unit, False when it is older, None when there is
        nothing to compare against (nothing uploaded this session, or a
        demo from an agent that does not record which show it came from).
        Never "older" out of ignorance - see the same rule in server.py's
        /api/fleet/demos."""
        with self._demos_lock:
            demos = self._demos.get(name)
            demos = None if demos is None else list(demos)
        if demos is None:
            return None
        expected = (self.shows.get(name) or {}).get("id")
        out = []
        for demo in demos:
            if not isinstance(demo, dict):
                continue
            show_id = demo.get("show_id")
            out.append({"slug": demo.get("slug"), "name": demo.get("name"),
                        "cues": demo.get("cues"), "duration": demo.get("duration"),
                        "loop": bool(demo.get("loop")), "show_id": show_id,
                        "current": (None if expected is None or show_id is None
                                    else show_id == expected)})
        return out

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
            # ...and whether this run's END can still bring a clear. A HOLD
            # or a RESUME in the final second cannot be played past, so the
            # END path stands down for that T0 - and the operator has no way
            # to see why the pictures then stay unless the page says so.
            run["end_clear_off"] = self.end_clear_disabled()
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
        return {"units": [self._unit_snapshot(link, run)
                          for link in self.links.values()],
                "last_fire": self.last_fire, "run": run,
                # `boards`: the ids THIS show gives that unit
                # (conductor/showfile.py writes them into the show file),
                # which is what the tile holds the unit's own list up
                # against.
                "shows": {unit: {"id": show["id"], "cues": len(show["cues"]),
                                 "boards": sorted(show.get("boards") or [])}
                          for unit, show in self.shows.items()},
                "corrections": self.corrections[-5:],
                "start_at": start_at,
                "show_duration": duration if self.shows else None,
                # Seconds left of the window a STOP's clear is waiting out,
                # or null when none is armed - the page counts it down and
                # says what takes it back (CLEAR_AFTER_STOP_S).
                "clear_in_s": self.clear_armed_in_s(),
                # EXHIBITION mode's Loop: the restart that is pending, or
                # null - the page's "next run in m:ss" (loop_state()).
                "loop": self.loop_state(),
                "burn": {"burned": burned, "total": len(reporting)}}

    def _unit_snapshot(self, link, run: "dict | None" = None) -> dict:
        """The unit's tile, the demos it holds (the cache the poll loop
        keeps, so a tile costs no request - see demos_of()), plus the last
        thing it refused this conductor's supervision ("run refused: still
        writing 12/48").
        The corrections log scrolls and is shared by ten units; the tile
        is where the operator looks when THAT unit is the one holding
        the show up (review round 2, 2026-09-25)."""
        refused = self._refused.get(link.name)
        at = self._uploaded_at.get(link.name)
        return dict(link.snapshot(),
                    refused=None if refused is None
                    else f"{refused[0]} refused: {refused[1]}",
                    show_lag_ms=self._show_lag_ms(link, run),
                    # How long ago this conductor wrote the show to this
                    # unit, against which the unit's own uptime_s says
                    # whether it has restarted since. None when this
                    # conductor has not uploaded to it (a conductor
                    # restarted mid-evening knows nothing about it and
                    # the tile says nothing).
                    uploaded_ago_s=(None if at is None
                                    else round(self._clock() - at, 1)),
                    demos=self.demos_of(link.name))

    def _show_lag_ms(self, link, run: "dict | None") -> "float | None":
        """How far this unit's show clock is behind this PC's, in ms -
        the honest answer to "how late is that garment?".

        The unit runs the show off a T0 in its OWN monotonic clock; this
        PC's T0 translated into that clock is `run["t0"] + offset` (the
        same arithmetic _supervise() corrects a unit with). Whatever is
        left is the disagreement:

            lag = unit_t0 - (run_t0 + offset)

        A LATER T0 on the unit means it thinks the show started later
        than this PC does, so its position is that much smaller: positive
        = the unit is BEHIND this PC. Normally a millisecond or two, and
        never more than T0_TOLERANCE_S for long, since _supervise() puts
        a unit further out than that right.

        None when there is nothing to compare: no run, no clock
        measurement, an offline unit (its last status is however old the
        silence is, and a number from then is worse than no number), a
        unit not running this conductor's show (a demo, an old upload,
        one that has not answered), or one on hold - held units carry the
        position, not a T0.

        ...and None until the unit has actually TAKEN a T0 this side has
        moved. START, SEEK, RESUME and NEXT move T0 here on the instant,
        while the unit's side of the comparison is whatever its last poll
        said - so right after a 30 s seek the arithmetic is perfectly
        correct and perfectly useless ("-30000 ms"), and every row on the
        board goes red at the exact moment the operator is watching it.

        Waiting out a poll does NOT settle that, which is what the first
        version of this got wrong: the poll that follows the move is the
        one _supervise() computes its correction FROM, so the first
        status admitted by a clock alone can still be the pre-correction
        one. The gate is therefore the unit's own answer, not the time:
        it opens for this move once the unit reports a T0 that is either
        no longer the one it had when the move happened (_t0_moved()
        photographed it) or already within T0_TOLERANCE_S of where this
        PC wants it - and once open it stays open for that move.

        "Changed", not merely "close": a unit that took the new T0 and
        landed a third of a second out is exactly what this number
        exists to show, and a gate that only opened on agreement could
        never let a real lag through. "Close" is there for the other
        case, a run ADOPTED from the units, where their T0 is where the
        run came from and nothing about it will change.
        """
        if not run or run.get("state") != "running" or not link.online:
            return None
        offset = link.offset
        show = self.shows.get(link.name)
        unit = (link.status or {}).get("show")
        if offset is None or show is None or not isinstance(unit, dict):
            return None
        if unit.get("id") != show["id"] or unit.get("t0") is None:
            return None
        if unit.get("state") not in ("running", "ended"):
            return None
        lag = round((unit["t0"] - (run["t0"] + offset)) * 1000, 1)
        set_at = run.get("t0_set_at")
        if set_at is None or self._t0_seen.get(link.name) == set_at:
            return lag
        if (unit["t0"] == self._t0_was.get(link.name)
                and abs(lag) > T0_TOLERANCE_S * 1000):
            return None                     # still on the T0 it had before
        self._t0_seen[link.name] = set_at
        return lag

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

        ...or had its pictures CLEARED after the last show (always
        blocking, `force` and all: they were deliberately deleted and
        the only way back in is an Upload - 2026-09-27).

        Unit-side contract (ui/showplay.py, 2026-09-25): a new agent
        always sends a "burn" dict for a loaded show, state one of
        "burning" | "burned" | "failed" | "cancelled" | "none" |
        "cleared"; an old agent has no "burn" key at all."""
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
            elif state == "cleared":
                # Taken back out of the slots after the last show, on
                # purpose. Not a fault and not something `force` may wave
                # through: there is no picture on the boards to show.
                why = burn.get("reason")
                problems.append(
                    f"{name}: pictures were cleared after the last show"
                    f"{' (' + str(why) + ')' if why else ''} - Upload again")
            elif state == "failed":
                if force:
                    continue
                problems.append(f"{name}: {pictures_not_written(burn)}")
            elif state == "cancelled":
                # The unit says WHY it gave up (no boards answering, the
                # port taken, the bus busy); a plain STOP needs no reason.
                why = burn.get("reason")
                problems.append(
                    f"{name}: pictures not written "
                    f"(cancelled{': ' + str(why) if why else ''})"
                    " - Upload again")
            elif state == "none":
                problems.append(f"{name}: pictures not written since it "
                                "restarted - Upload again")
            elif state != "burned":
                problems.append(f"{name}: pictures not written ({state}) "
                                "- Upload again")
        return problems

    # ---- clearing the pictures after the show ----
    # After the show on 2026-09-27 the operator pressed STOP and unplugged
    # the Radxa from a garment whose boards were still on battery. About a
    # minute later the master board restarted the factory autoplay and
    # cycled slots 0-18 - it replayed the show on its own. Once the USB is
    # gone nothing can stop it, so when the operator asks for it (the
    # checkbox next to (3) START, show.json's `clear_after_show`) the
    # pictures come back OUT of the slots the moment the run is over.
    #
    # Nothing is repainted by it - the garment keeps the last look it was
    # shown for as long as it has power, which is the operator's own rule.

    def clear_wanted(self) -> bool:
        """Does the uploaded timeline ask for its pictures to be cleared
        when the show is over? (Any of the shows saying so is enough -
        one Upload writes them all from one timeline.)"""
        return any(bool(show.get("clear_after_show"))
                   for show in self.shows.values())

    def clear_pictures(self, only: "list[str] | None" = None
                       ) -> "dict[str, dict]":
        """The operator's own "Clear pictures now" (the WRITE TO UNITS
        dialog): delete slots 1-18 on the units of this timeline, or on
        `only` of them - exactly upload()'s own choice of targets.

        The caller refuses it while a show is running or holding
        ("stop the show first"); the units would refuse it anyway.
        """
        targets = self._targets()
        if only is not None:
            targets = [name for name in only if name in targets]
        return self._clear_units(targets, once=False)

    def _clear_units(self, names, once: bool = True) -> "dict[str, dict]":
        """POST /show/clear to each of `names` that is holding this
        conductor's show. A unit playing its own standalone demo is left
        alone, and so is one that holds some other show - its slots are
        not this show's to empty.

        `once` (the automatic path, on END and on STOP) does not ask a
        unit twice for the same run, so a supervision tick does not send
        it every three seconds; the manual button passes once=False.

        "Once" counts only an ask that SETTLED, though: a unit that was
        out of reach - offline, a timeout, the conductor's own connection
        dropped - is asked again on the next poll it answers. Marking it
        told before the post meant the comment above promised a retry that
        never came, and the one garment nobody could reach when the show
        ended was the one left holding the pictures (review, 2026-09-27).
        Units that refuse for a REASON (too old, holding another show,
        playing a demo) are settled: retrying cannot change the answer, so
        they are told once and the tile says why.

        An agent too old for the endpoint answers 404, which is NOT an
        error that may hold the other units up: that unit is remembered
        and its tile says "unit too old for clear - power the boards off
        before unplugging".
        """
        settled = set()

        def action(link):
            unit = (link.status or {}).get("show") or {}
            show = self.shows.get(link.name)
            if show is None:
                settled.add(link.name)
                raise RuntimeError("not holding this show")
            if self._playing_demo(link):
                settled.add(link.name)
                raise RuntimeError("playing a demo - press STOP first")
            if unit.get("id") != show["id"]:
                settled.add(link.name)
                raise RuntimeError("holding another show")
            try:
                link.post("/show/clear", {"show": show["id"]})
            except Exception as exc:        # noqa: BLE001 - see below
                if _too_old_for_clear(exc):
                    self._clear_too_old.add(link.name)
                    settled.add(link.name)
                    raise RuntimeError(
                        "unit too old for clear - power the boards off "
                        "before unplugging") from None
                # Anything else is the network, not an answer: leave this
                # unit untold so the next poll asks it again.
                raise
            self._clear_too_old.discard(link.name)
            settled.add(link.name)
            return {}

        wanted = [name for name in names
                  if not (once and name in self._clear_told)]
        if not wanted:
            return {}
        if once:
            # Stamped BEFORE the posts, like _post_or_refused(): a unit
            # that is not answering costs a connection timeout, and the
            # retry above belongs on supervision's own cadence
            # (SUPERVISE_EVERY_S) rather than on every poll.
            now = self._clock()
            for name in wanted:
                self._corrected[name] = now
        results = self._each(wanted, action)
        if once:
            self._clear_told.update(settled)
        for name, result in sorted(results.items()):
            if not result.get("ok"):
                retry = "" if name in settled else " - asking again next poll"
                self.corrections.append(
                    f"{time.strftime('%H:%M:%S')} {name}: clear refused: "
                    f"{result.get('error')}{retry}")
            else:
                self.corrections.append(f"{time.strftime('%H:%M:%S')} "
                                        f"{name}: clearing the pictures")
        del self.corrections[:-20]
        return results

    def show_duration(self) -> float:
        """The longest `duration` among the uploaded shows, 0.0 when none
        are uploaded (§2.1: the page then sees `show_duration: null`)."""
        return float(max((show.get("duration", 0.0)
                          for show in self.shows.values()), default=0.0))

    # ---- the show ----

    def upload(self, shows: "dict[str, dict]", force: bool = False,
               only: "list[str] | None" = None) -> "dict[str, dict]":
        """Write `shows` (unit -> compiled show) to the units.

        `only` writes to those units alone - the page's "Which LOOKs"
        choice, for checking one look without touching the rest of the
        fleet. The units left out are not posted to and keep the show
        they hold, so `self.shows` (what this conductor believes each
        unit holds) is MERGED rather than replaced: replacing it would
        have the conductor forget the earlier upload the other units are
        still running on, and the page's "uploaded n/n" would count them
        as holding a show nobody sent them.

        Merged, but only over the units this timeline still has: a unit
        whose look was taken out of the show is dropped from `self.shows`
        by a partial upload exactly as a full one drops it, or _targets()
        would go on driving it and START would post /show/run to a unit
        that is not in the show at all (review F3)."""
        self._cancel_armed_clear()      # the pictures are going back in
        targets = ([name for name in shows] if only is None
                   else [name for name in only if name in shows])
        if only is not None:
            self._refuse_mixed_duration(shows, targets)

        def action(link):
            excuse = self._demo_excuse(link)
            if excuse:
                raise RuntimeError(excuse)
            status = link.post("/show/load", shows[link.name])
            return {"show": (status.get("show") or {}).get("id")}
        results = self._each(targets, action)
        # Only the units that really took it: one that refused is still
        # holding whatever it held, and dating this upload on it would
        # have the tile call every later restart of it "since Upload".
        now = self._clock()
        for name in targets:
            if results.get(name, {}).get("ok"):
                self._uploaded_at[name] = now
        if only is None:
            self.shows = dict(shows)
        else:
            kept = {name: show for name, show in self.shows.items()
                    if name in shows}
            self.shows = {**kept, **{name: shows[name] for name in targets}}
        # A new show file is a new duration: a remembered position from
        # the old one may no longer even be inside it (found in review).
        with self._run_lock:
            self.start_at = 0.0
            # The pictures are back in the slots, so the clear that
            # emptied them is history: a unit written here may be asked to
            # clear again when this show ends.
            for name in targets:
                if results.get(name, {}).get("ok"):
                    self._clear_told.discard(name)
                    self._clear_too_old.discard(name)
            if force and self.run is not None:
                # The operator has just asked, under a running show, for
                # the pictures to be written again (the page's confirm) -
                # to rescue a unit that lost them. If that unit's re-burn
                # then fails on a live board, supervision must still put
                # it BACK INTO the show rather than refuse it and leave
                # it dark for the rest of the night (R4, review round 3).
                # Only for the units actually rescued, though: a one-unit
                # Upload during a run must not quietly wave every other
                # unit's failed boards through for the rest of the night
                # (review F5) - and a unit whose re-write never landed was
                # not rescued at all, so it keeps its own gate (N3).
                forced = set(self.run.get("forced") or ())
                forced.update(name for name in targets
                              if results.get(name, {}).get("ok"))
                self.run["forced"] = sorted(forced)
        return results

    def _refuse_mixed_duration(self, shows: "dict[str, dict]",
                               targets: "list[str]") -> None:
        """A one-LOOK upload whose show is a different LENGTH from the
        one the untouched units still hold is refused outright.

        Everything downstream reads the show's length as one number
        (show_duration() takes the longest of them), so a fleet split
        across two lengths would have SEEK and START accept a position
        that is past a unit's own end - and the unit that cannot reach it
        simply never fires again (review F2). A shorter/longer timeline
        is not a one-look fix; it is a full Upload."""
        mine = {round(float(shows[name].get("duration", 0.0)), 1)
                for name in targets}
        for name, show in self.shows.items():
            if name in targets or name not in shows:
                continue
            held = round(float(show.get("duration", 0.0)), 1)
            if mine and held not in mine:
                raise ValueError(
                    f"this timeline is {min(mine):g} s long but {name} still "
                    f"holds a {held:g} s one - Upload for All LOOKs")

    # ---- the standalone demo: a named copy of the show, in a unit's own
    # menu, that plays without this PC. Independent of the run this
    # conductor is driving - it touches neither self.shows nor self.run.

    def write_demo(self, name: str, loop: bool, shows: "dict[str, dict]",
                   only: "list[str] | None" = None) -> "dict[str, dict]":
        """Post each unit its own compiled show (`shows`, the same dict
        upload() sends via /show/load) to /demo/save under `name`, so the
        unit can play it from its own menu, on its own clock, without
        this PC. Only the units named in `shows` are written to - exactly
        upload()'s own targets - and `only` narrows that to one LOOK's
        units, the same subset upload() takes. The units left out keep
        the demo they already hold under that name (nothing is deleted:
        the unit only ever replaces a name it is written). `learn=False`:
        a write includes an eMMC save on the unit's side, and its own
        longer timeout - neither belongs anywhere near the clock-offset
        model."""
        targets = ([name_ for name_ in shows] if only is None
                   else [name_ for name_ in only if name_ in shows])

        def action(link):
            result = link.post("/demo/save", {"name": name, "loop": bool(loop),
                                               "show": shows[link.name]},
                               learn=False, timeout=DEMO_SAVE_TIMEOUT_S)
            # The unit answers with its whole menu: the tiles are right
            # before the next poll, without a second request.
            self.remember_demos(link.name, result.get("demos"))
            return {"slug": result.get("slug")}
        return self._each(targets, action)

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
            demos = link.get("/demo/list", learn=False,
                             timeout=TIMEOUT_S).get("demos") or []
            self.remember_demos(link.name, demos, retry_soon=False)
            return {"demos": demos}
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
            self.remember_demos(link.name, result.get("demos"))
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
            # A mid-show rescue Upload adds only the units it rescued
            # (`forced`), so the rest of the fleet keeps its own gate.
            force = bool(self.run.get("force"))
            forced = set(self.run.get("forced") or ())
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
                                    "force": force or link.name in forced})
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
        self._cancel_armed_clear()
        targets = self._targets()
        problems = self._burn_problems(targets, force=force)
        if problems:
            raise ValueError("; ".join(problems))
        body = {"force": bool(force)}
        return self._each(targets, lambda link: {
            "phase": link.post("/show/preset", body).get("phase")})

    def start_show(self, lead_s: float = DEFAULT_LEAD_S,
                   at: float = 0.0, force: bool = False,
                   loop: bool = False, skip=(),
                   expect_gen: "int | None" = None) -> "dict[str, dict]":
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
        live board that would not take the write.

        `loop` is the Loop's own restart (_loop_tick): the same START in
        every respect, counted on the run as `loops` so the page can say
        which run of the day this is. A START press starts the count over.
        `skip` names units left out of this run (the Loop's grace period
        ran out on them): not gated on, not sent to - supervision goes on
        polling them. `expect_gen` is the run generation the caller
        decided on: a STOP or a move since (a different generation, or
        `_stopped`) refuses the START under the lock, so the Loop can
        never start over the top of an operator's STOP."""
        # Before the gate: a STOP's clear that is still inside its window
        # has not deleted anything, so dropping it is what lets START run
        # the show again (the director's mid-show abort, taken back).
        if expect_gen is None:
            self._cancel_armed_clear()
        duration = self.show_duration()
        at = self._clamped(at, 0.0, duration)
        if not 0 <= at <= duration:
            raise ValueError(f"The show is {timeline.format_clock(0)} to "
                             f"{timeline.format_clock(duration)}.")
        targets = [name for name in self._targets() if name not in set(skip)]
        if not targets:
            raise ValueError("no unit is ready")
        # Every picture is meant to already be sitting in its slot: START
        # refuses while any unit is still writing them, offline, not yet
        # holding this show, or (unless `force`) failed to write some
        # boards (2026-09-24, the pre-burn design - see timeline.py).
        burning = self._burn_problems(targets, force=force)
        if burning:
            raise ValueError("; ".join(burning))
        with self._run_lock:
            if expect_gen is not None and (self._run_gen != expect_gen
                                           or self._stopped):
                raise ValueError("stopped or moved meanwhile")
            self._may_adopt, self._stopped = False, False
            self._loop_runs = self._loop_runs + 1 if loop else 0
            self.run = {"t0": self._clock() + lead_s - at, "state": "running",
                        "held_at": None, "force": bool(force),
                        # Mirrored onto the run so the page can see what
                        # THIS run will do when it is over, whatever the
                        # timeline is edited to meanwhile.
                        "clear_after_show": self.clear_wanted(),
                        # Which run of the Loop this is (0: the START press).
                        "loops": self._loop_runs}
            self.start_at = 0.0
            # A new run: nobody has been asked to clear anything yet, and
            # a unit that was too old last time may have been updated.
            self._clear_told, self._clear_too_old = set(), set()
            self._t0_moved()
        return self._send_run(targets)

    def _t0_moved(self) -> None:
        """One call for "the run just changed" - START, SEEK, HOLD,
        RESUME, NEXT, STOP and the adoption of a run found on the units.
        Must be made under _run_lock.

        It bumps the generation _supervise() checks before it posts
        anything (so a correction computed a moment ago is dropped rather
        than sent on top of the operator's move), and it stamps the run
        and photographs what every unit says its T0 is RIGHT NOW - before
        a single command has gone out. `_show_lag_ms` needs both: T0
        moves here on the instant, while what a unit reports is up to a
        poll old, so a 30 s seek would otherwise be reported as every
        unit being 30 s out of step and the whole board would go red.
        """
        self._run_gen += 1
        # Any move of the run is "the show is not simply over": a pending
        # Loop restart is dropped, and arms again the next time a run
        # reaches its end (a STOP has no run, so it never does).
        self._loop_at = None
        self._loop_problem = None
        if self.run is None:
            self._t0_was, self._t0_seen = {}, {}
            return
        now = self._clock()
        self.run["t0_set_at"] = now
        # ...and WHERE in the show this move landed. The clear after the
        # show reads it to tell "played to the end" from "jumped to the
        # end": a seek clamps to `duration` inclusive, so without this a
        # look at the last cue armed the irreversible clear on the spot
        # (review, 2026-09-27 - see _reached_end_by_playing()).
        held = self.run["state"] == "holding"
        mark = self.run["held_at"] if held and self.run["held_at"] else now
        self.run["landed_at_s"] = mark - self.run["t0"]
        # Rebound, never mutated in place: snapshot() reads these from
        # whichever thread is serving the page.
        self._t0_was = {name: ((link.status or {}).get("show") or {}).get("t0")
                        for name, link in self.links.items()}
        self._t0_seen = {}

    def seek(self, to_s: float, lead_s: float = DEFAULT_LEAD_S
             ) -> "tuple[str, dict[str, dict]]":
        """Move the show to `to_s` seconds from its start (§2.4):

            running :  t0 = clock + lead_s - to_s  -> sent to every unit
            holding :  t0 = held_at - to_s          -> nothing sent
            no run  :  start_at = to_s              -> nothing sent

        Returns (mode, per-unit results); `to_s` outside 0..show_duration()
        is a ValueError, the plain-English range the page shows."""
        self._cancel_armed_clear()
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
            self._t0_moved()
        if send:
            return mode, self._send_run(self._targets())
        return mode, {}

    def hold(self) -> "dict[str, dict]":
        self._cancel_armed_clear()
        with self._run_lock:
            if not self.run or self.run["state"] != "running":
                return {}
            self.run.update(state="holding", held_at=self._clock())
            self._t0_moved()
        return self.simple(self._targets(), "/show/hold")

    def resume(self) -> "dict[str, dict]":
        self._cancel_armed_clear()
        with self._run_lock:
            if not self.run or self.run["state"] != "holding":
                return {}
            self.run["t0"] += self._clock() - self.run["held_at"]
            self.run.update(state="running", held_at=None)
            self._t0_moved()
        return self._send_run(self._targets())

    def next_cue(self, lead_s: float = DEFAULT_LEAD_S) -> "dict[str, dict]":
        """Bring the earliest upcoming cue of any unit to `lead_s` from now
        by moving T0 earlier - for every unit alike, so they stay in step."""
        self._cancel_armed_clear()
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
            self._t0_moved()
        return self._send_run(self._targets())

    def stop_show(self) -> "dict[str, dict]":
        targets = self._targets()
        with self._run_lock:
            self._may_adopt, self._stopped = False, True
            self._stop_told = set()
            self._demo_told = set()     # the next demo episode is announced again
            # Whether the show that is ENDING asked for its pictures to
            # go, read before the run is thrown away: the run the operator
            # is stopping is the one whose slots these are, not whatever
            # the timeline has been edited to since. A STOP with no run at
            # all (the units are running something this conductor never
            # started) falls back to the timeline's own answer.
            clear = (bool(self.run.get("clear_after_show"))
                     if self.run and "clear_after_show" in self.run
                     else self.clear_wanted())
            self.run = None
            self.start_at = 0.0
            self._t0_moved()
        results = self.simple(targets, "/show/stop")
        if clear:
            # ARMED, not sent. STOP is also how a director aborts a show
            # half way through - the ordinary reason to press it - and a
            # clear cannot be undone without a three-minute Upload, so the
            # operator gets CLEAR_AFTER_STOP_S in which START, PRESET,
            # RESUME, NEXT, a seek or an Upload takes it back. The page's
            # own confirm says so before the click.
            #
            # (And after the STOP, never with it, whenever it does go: the
            # unit refuses a clear while its run is still running, and
            # those slots are what the next trigger would read from.)
            with self._run_lock:
                self._clear_at = self._clock() + self.clear_after_stop_s
                self._clear_armed = list(targets)
        return results

    def clear_armed_in_s(self) -> "float | None":
        """Seconds until an armed STOP clear goes out, or None when none is
        armed OR its window has already run out - the page's countdown, and
        only ever about a window still AHEAD.

        Never 0.0 for a window that has fired: the page shows this line in
        place of everything else on the panel, so a nought stuck here read
        "Pictures will be cleared on every unit in 0 s - press (3) START" for
        ever and hid the "(1) Upload writes them again" the operator needed
        (review, 2026-09-27). A unit still being retried keeps the clear
        armed in _clear_armed, which is not a countdown.
        """
        with self._run_lock:
            if self._clear_at is None:
                return None
            left = self._clear_at - self._clock()
            return left if left > 0 else None

    def end_clear_disabled(self) -> bool:
        """True when this run asked for a clear after the show but its END
        can no longer bring one - because the last T0 move landed at (or
        within END_REACH_MARGIN_S of) the end, so the show was JUMPED to
        rather than played to (see _reached_end_by_playing()).

        A HOLD or a RESUME in the final second is enough to do it, and the
        operator has no way to see why the pictures then stay: the page says
        so, and STOP is the way to clear them.
        """
        with self._run_lock:
            run = dict(self.run) if self.run else None
        if not run or not run.get("clear_after_show"):
            return False
        landed = run.get("landed_at_s")
        if landed is None:
            return True                 # nothing this side saw reach anything
        return float(landed) >= self.show_duration() - END_REACH_MARGIN_S

    def _cancel_armed_clear(self) -> None:
        """"The show is not over after all" - START, PRESET, RESUME, NEXT,
        a seek, an Upload. Called without _run_lock held."""
        with self._run_lock:
            armed, self._clear_at, self._clear_armed = self._clear_at, None, []
        if armed is not None:
            self.corrections.append(f"{time.strftime('%H:%M:%S')} "
                                    f"the clear after STOP was taken back")
            del self.corrections[:-20]

    def _fire_armed_clear(self, link: UnitLink) -> bool:
        """Has a STOP's window run out? Then ask THIS unit to clear - the
        same once-per-unit rule as the END path, so a unit that was out of
        reach is retried on the next poll it answers.

        True when there is nothing else to supervise about this unit.
        """
        with self._run_lock:
            if self._clear_at is None or self._clock() < self._clear_at:
                return False
            armed = list(self._clear_armed)
        if link.name not in armed:
            return False
        self._clear_units([link.name])
        with self._run_lock:
            # A unit that SETTLED leaves the armed list, and the window
            # itself closes once the list is empty. Without this the arming
            # outlived the clear for ever: clear_armed_in_s() went on
            # answering, /api/fleet's clear_in_s stayed put and the page's
            # countdown line masked the "(1) Upload writes them again" hint
            # the operator needed next (review, 2026-09-27).
            #
            # One that did NOT settle - offline, a timeout - stays armed on
            # purpose: that is the retry, and it is asked again on the next
            # poll it answers.
            self._clear_armed = [name for name in self._clear_armed
                                 if name not in self._clear_told]
            if not self._clear_armed:
                self._clear_at = None
        return True

    def _supervise(self, link: UnitLink) -> None:
        """After every poll: is this unit running what it should, on the
        T0 it should? If not, tell it - nobody has to notice first."""
        self._adopt_show(link)              # a restarted conductor: who holds what
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
            # A STOP armed a clear and its window has run out. Here, not on
            # a timer, because this already runs for every unit after every
            # poll - and a unit that was out of reach when the window
            # closed is asked again on the first poll it answers.
            self._fire_armed_clear(link)
            return
        show = self.shows.get(link.name)
        if show is None or link.offset is None:
            return
        if run["state"] == "running" and self._clear_after_end(link, run, show):
            # The show is over on the clock and asked for its pictures to
            # go. Done from here rather than from a timer, because this is
            # the one place that already runs for every unit after every
            # poll - and a unit that was out of reach at the end is asked
            # on the first poll it answers.
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
                         "force": bool(run.get("force"))
                                  or link.name in (run.get("forced") or ())},
                        "run"):
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

    def _clear_after_end(self, link: UnitLink, run: dict, show: dict) -> bool:
        """Ask this unit to clear its slots, if the run is over and asked
        for it. True when there is nothing else to supervise about it.

        "Over" is CLEAR_AFTER_END_S past the show's own length - the same
        kind of slack _supervise() already uses to stop correcting a
        finished show, and enough for the last cue's own repaint and the
        guard behind it (the unit waits for its guard floor itself before
        the first 0x14, so this need only be past the end, not past the
        repaint).

        A HOLD is not an end: a held show is one the operator means to
        resume, and the pictures have to stay where they are. Nor is a
        SEEK - it only moves T0, and the branch below reads the clock,
        not the seeking.

        Asked ONCE per unit and run, exactly like the missed STOP above:
        the slots are then empty (or emptying), so nothing a /show/run
        correction could say to that unit would be taken anyway. A unit
        that was out of reach at the end is the operator's own "Clear
        pictures now" button (the WRITE TO UNITS dialog), not a request
        this retries every two seconds into a connection timeout.
        """
        if not run.get("clear_after_show"):
            return False
        if link.name in self._clear_told:
            return True
        duration = float(show.get("duration", 0))
        if self._clock() - run["t0"] < duration + CLEAR_AFTER_END_S:
            return False
        # A Loop restart is pending: the show is not over, it is between
        # runs, and the pictures are what the next run triggers. With Loop
        # on, "Clear pictures after the show" happens on STOP (its own
        # window) and never at an end the Loop is about to play past.
        with self._run_lock:
            if self._loop_at is not None:
                return False
        if not self._reached_end_by_playing(run, duration):
            return False
        self._clear_units([link.name])
        return True

    def _reached_end_by_playing(self, run: dict, duration: float) -> bool:
        """Did this run get to the end by PLAYING there?

        The end used to be read off the clock alone, and `seek` clamps to
        `duration` inclusive - so "move to the end and see the last look"
        armed a clear that cannot be undone, and a NEXT onto the final cue
        did the same (review, 2026-09-27). Two rules, both about the last
        T0 MOVE (START, SEEK, RESUME, NEXT, an adoption):

        * it must have landed BEFORE the end (END_REACH_MARGIN_S of slack).
          A move that landed at or past the end is a jump to the end, and
          no amount of waiting makes it a performance - the END clear never
          fires on that T0. STOP is how the operator ends such a run, and
          STOP has its own window;
        * and the run must have been left alone for CLEAR_AFTER_MOVE_S
          since, so an operator still working the seek bar in the last
          half-minute of the show never has the pictures deleted under
          them. A show played through normally passes this long before its
          own end (the last move was the START).

        A run ADOPTED from the units is stamped like any other, so this
        conductor clears once it has watched that run play on past the end
        - but never one it adopted already past it, which it did not see
        reach anything. A run dict carrying neither key at all (nothing
        builds one today) is treated the same way: nothing is deleted on a
        guess.
        """
        landed, moved_at = run.get("landed_at_s"), run.get("t0_set_at")
        if landed is None or moved_at is None:
            return False
        if float(landed) >= duration - END_REACH_MARGIN_S:
            return False
        return self._clock() - float(moved_at) >= self.clear_after_move_s

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
                # "force": the show on the units is ALREADY running, so
                # it passed the burn gate when it was started - by this
                # conductor before it restarted, or by another PC. A
                # supervision /show/run posting force=False into that
                # would be refused by a unit whose burn merely failed on
                # a board, and the unit would sit out the show it is
                # already in (review round 2, 2026-09-25).
                self.run = {"t0": found[len(found) // 2], "state": "running",
                            "held_at": None, "adopted": True, "force": True,
                            # The units cannot say whether the show that is
                            # already running asked for its pictures to be
                            # cleared, so the timeline this conductor holds
                            # is the only answer there is. With nothing
                            # uploaded it is False, which is the safe way
                            # round: nothing is deleted on a guess.
                            "clear_after_show": self.clear_wanted()}
                # Whatever a SEEK remembered before this conductor came
                # up (or restarted) is not where THIS run began - the
                # units, not the page, decided that (found in review).
                self.start_at = 0.0
                self._t0_moved()

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

    def recover_bus(self, name: str) -> dict:
        """POST /bus/recover to ONE unit - the tile's "Recover bus".

        One unit, not the fleet: this is a button next to a mark on one
        tile, and the page's own pre-preset sweep calls it per unit in
        parallel so each answer lands on its own tile.

        `learn=False`: the unit holds the connection for as long as the
        recovery takes (seconds, by design), and feeding that round trip
        into the clock model would poison the offset with a "slow path"
        that has nothing to do with the network - the same reason a demo
        write skips it.
        """
        link = self.links.get(name)
        if link is None:
            raise KeyError(f"unknown unit {name}")
        return link.post("/bus/recover", {}, learn=False,
                         timeout=RECOVER_TIMEOUT_S)

    def simple(self, names, path: str) -> "dict[str, dict]":
        """cancel / standby / release."""
        return self._each(list(names), lambda link: {
            "phase": link.post(path, {}).get("phase")})

    def forget_shows(self) -> None:
        """This conductor no longer knows what the units hold - the
        workspace was just replaced by an import. START then says
        "Upload first" / "Upload again" (the server marks the units it
        used to know as holding an older upload) instead of running the
        units' old pictures under the new music."""
        with self._run_lock:
            self.shows = {}
            self.start_at = 0.0
        self.offer_shows({})

    def wifi_select(self, profile: str, after_s: float,
                    last: "list[str] | None" = None,
                    hotspot: str = DEFAULT_HOTSPOT_UNIT,
                    hotspot_profile: str = HOTSPOT_PROFILE) -> "dict[str, dict]":
        """POST /wifi/select {"profile", "after_s"} to every ONLINE unit -
        the page's "All units -> AZ-Epaper in 20 s" (EXHIBITION mode). The
        unit schedules the switch and answers {"scheduled": true,
        "after_s"}, or refuses (409) while a show runs, is held or is
        restored on its garment; a unit that is offline is reported as such
        rather than waited out.

        Two leads, so the two sides of the hotspot move in the right order
        (PM, 2026-09-30): towards the hotspot profile the HOTSPOT unit
        (fleet.json's "hotspot", radxa-05) gets the short lead and the
        clients the long one - the hotspot is up before they look for it;
        towards the router the clients get the short lead and the hotspot
        the long one - it goes down only after they have left. `after_s`
        is the long lead, clamped to what the unit takes.

        `last` names the units that are this Conductor's own host (reached
        over 127.0.0.1): they are told AFTER every other unit has answered
        whatever their lead, because switching the host's Wi-Fi takes the
        page's own connection with it.
        """
        low, high = WIFI_SWITCH_RANGE_S
        long_s = min(high, max(low, float(after_s)))
        soon_s = min(long_s, WIFI_SWITCH_SOON_S)
        to_hotspot = str(profile) == hotspot_profile
        leads = {name: (soon_s if (name == hotspot) == to_hotspot else long_s)
                 for name in self.links}

        def action(link):
            if not link.online:
                raise RuntimeError("offline")
            body = {"profile": str(profile), "after_s": leads[link.name]}
            answer = link.post("/wifi/select", body, learn=False)
            return {"scheduled": bool(answer.get("scheduled")),
                    "after_s": answer.get("after_s", leads[link.name])}

        own = [name for name in (last or ()) if name in self.links]
        first = [name for name in self.links if name not in own]
        results = self._each(first, action)
        if own:
            results.update(self._each(own, action))
        return results
