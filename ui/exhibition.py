"""EXHIBITION: run the show from the LCD of the unit that IS the Conductor.

An exhibition without the venue's router or a PC runs off radxa-05: it
is the AZ-Epaper hotspot (ui/wifi.py) and it runs the Conductor itself
(`python3 -m conductor serve --host 0.0.0.0 --port 8765`, a systemd
service). The other units join the hotspot and the Conductor drives
them exactly as the PC would have - but nobody has a browser to press
START in. The EXHIBITION menu row is that button, on the HAT.

Everything here is a client of the Conductor's own HTTP API on
127.0.0.1:8765, and the Conductor stays the one that owns the show: the
row never fires a cue itself, never talks to the boards, and KEY2 back
to the menu leaves a run running. What it calls:

    GET  /api/fleet           the poll: `run` (null when idle, else the
                              run with `state` running/holding and `now`
                              seconds into the show - negative during
                              the countdown), `show_duration`, `units`
                              (each with `online`), `shows` (what was
                              uploaded), plus `loop` {"on", "wait_s",
                              "next_in_s"} and `speaker` {"available",
                              "error"} (an older Conductor has neither:
                              the screen then reads `loop ?` / `speaker ?`)
    GET  /api/show/export     once per opening of the screen: the
                              timeline's name (the workspace folder), its
                              cue count and duration - the Conductor has
                              exactly one timeline, so there is nothing to
                              choose between; UP/DOWN only read a verdict
                              away
    POST /api/fleet/start {}  START, with the show's own countdown (the
                              Conductor picks the lead when the body
                              names none). A 200 with a `note` is a soft
                              refusal ("Nothing uploaded yet - Upload
                              first.", "The show is already running.")
                              and is shown verbatim, as is a 400's error
    POST /api/fleet/stop  {}  STOP - the run and its countdown
    POST /api/loop {"on": b}  the loop flag; answers the loop object
    POST /api/speaker/volume {"delta": -5 | +5}
                              joystick LEFT / RIGHT, plain presses;
                              answers {"volume", "applied", "error"} and
                              the speaker line follows (`speaker ok ·
                              vol 70% (bluez)`). One request in flight
                              at a time: presses meanwhile add up and go
                              out as one delta once it has answered. A
                              Conductor whose speaker object has no
                              `volume` shows `vol ?`, and LEFT / RIGHT
                              only say so

The row is only useful where a Conductor answers, and the question is
asked by a daemon thread (every POLL_OPEN_S while the screen is open,
POLL_IDLE_S otherwise, one PROBE_TIMEOUT_S request), never on the HAT
loop: the screen and the menu label read a cache, like WIFI's. A unit
without a local Conductor shows the row as `EXHIBITION  (no conductor)`
and the screen says only that. Every command runs on a worker thread
with COMMAND_TIMEOUT_S; the screen reads `sending…` and then the
Conductor's answer or the first line of its error. One injectable HTTP
function carries all of it, so the tests run against a fake Conductor
and never open a socket.
"""

from __future__ import annotations

import json
import math
import threading
import time
import urllib.error
import urllib.request
from collections import deque

from .config import LOG_HISTORY

IDLE = "idle"                # nothing in flight; hold KEY1 = START / STOP
SENDING = "sending"          # a command is on its way to the Conductor
DONE = "done"                # the Conductor answered
FAILED = "failed"            # refused, or no answer within the timeout

CONDUCTOR_URL = "http://127.0.0.1:8765"
FLEET_PATH = "/api/fleet"
SHOW_PATH = "/api/show/export"
START_PATH = "/api/fleet/start"
STOP_PATH = "/api/fleet/stop"
LOOP_PATH = "/api/loop"
VOLUME_PATH = "/api/speaker/volume"
VOLUME_STEP = 5              # joystick LEFT / RIGHT, per press
VOLUME_UNSUPPORTED = "volume: not supported by this conductor"

PROBE_TIMEOUT_S = 1.0        # /api/fleet must answer within this
COMMAND_TIMEOUT_S = 3.0      # START / STOP / loop, and the show's name
POLL_OPEN_S = 5.0            # while the EXHIBITION screen is open
POLL_IDLE_S = 30.0           # otherwise: only the menu label needs it

NO_CONDUCTOR = "no conductor"
MENU_LABEL = "EXHIBITION"
MENU_LABEL_NONE = "EXHIBITION  (no conductor)"


def http_json(method: str, url: str, body, timeout: float):
    """(status code, decoded JSON - or the text when it is not JSON).

    An HTTP error status is an answer, not an exception (the Conductor
    puts its reason in the body); a refused connection or a timeout
    raises, and the caller turns that into its first line.
    """
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"} if data is not None else {}
    request = urllib.request.Request(url, data=data, method=method,
                                     headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            code, raw = response.status, response.read()
    except urllib.error.HTTPError as exc:
        code, raw = exc.code, exc.read()
    try:
        return code, json.loads(raw or b"null")
    except ValueError:
        return code, raw.decode("utf-8", "replace")


def _first_line(text) -> str:
    for line in str(text).splitlines():
        if line.strip():
            return line.strip()
    return ""


def _why(exc: BaseException) -> str:
    """An exception as the screen's one line. urllib wraps the socket's
    own error (`<urlopen error [Errno 111] Connection refused>`): the
    inner reason is what the operator can read - `Connection refused`,
    `timed out`."""
    reason = getattr(exc, "reason", None)
    if isinstance(reason, BaseException):
        return _why(reason)
    if reason:
        return _first_line(str(reason))
    text = getattr(exc, "strerror", None) or str(exc)
    return _first_line(text) or exc.__class__.__name__


def format_clock(seconds: float) -> str:
    """m:ss (the Conductor's page format), never negative."""
    total = max(0, int(seconds))
    return f"{total // 60}:{total % 60:02d}"


class ExhibitionRow:
    """The menu row: its label follows the cache (EXHIBITION, or
    `EXHIBITION  (no conductor)`), which a frozen MenuEntry could not."""

    key = "exhibition"

    def __init__(self, exhibition: "Exhibition"):
        self._exhibition = exhibition

    @property
    def label(self) -> str:
        return MENU_LABEL if self._exhibition.available else MENU_LABEL_NONE

    @property
    def detail(self) -> str:
        if self._exhibition.available:
            return "START / STOP the show from here"
        return "needs the Conductor on this unit"


class Exhibition:
    """State behind the EXHIBITION screen.

    `http` is injectable: (method, url, body | None, timeout) -> (code,
    payload), like http_json above, so the flow is testable against a
    fake Conductor.
    """

    def __init__(self, http=http_json, base: str = CONDUCTOR_URL,
                 poll_open_s: float = POLL_OPEN_S,
                 poll_idle_s: float = POLL_IDLE_S,
                 probe_timeout: float = PROBE_TIMEOUT_S,
                 command_timeout: float = COMMAND_TIMEOUT_S,
                 clock=time.monotonic, echo_log: bool = True):
        self._http = http
        self.base = base.rstrip("/")
        self.poll_open_s = poll_open_s
        self.poll_idle_s = poll_idle_s
        self.probe_timeout = probe_timeout
        self.command_timeout = command_timeout
        self._clock = clock
        self._echo_log = echo_log

        # The cache: what the reader last saw of the Conductor.
        self.available: "bool | None" = None      # None: never asked yet
        self.fleet: "dict | None" = None
        self.fleet_at = 0.0                       # clock() of that fleet
        self.fleet_error: "str | None" = None
        self.show: "dict | None" = None           # {"name", "cues", "duration"}
        self._want_show = False

        self.phase = IDLE
        self.command: "str | None" = None         # start / stop / loop
        self.note = ""                            # the line under the state
        self.is_open = False
        self.log: deque = deque(maxlen=LOG_HISTORY)
        self._lock = threading.Lock()             # cache, log
        self._poll_lock = threading.Lock()        # one poll at a time
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._reader: "threading.Thread | None" = None
        self._thread: "threading.Thread | None" = None
        # Volume: one request in flight, presses meanwhile summed up.
        self._volume_inflight = False
        self._volume_wanted = 0
        self._volume_thread: "threading.Thread | None" = None

    # ---- facts for the screen ----

    @property
    def menu_entry(self) -> ExhibitionRow:
        return ExhibitionRow(self)

    @property
    def busy(self) -> bool:
        """True while a command is on its way. KEY2 still leaves the
        screen then; the command completes on its own."""
        return self.phase == SENDING

    @property
    def active(self) -> bool:
        """A run (or its countdown, or the loop's wait for the next one)
        exists on the Conductor: hold KEY1 is STOP rather than START."""
        fleet = self.fleet
        if not fleet:
            return False
        return fleet.get("run") is not None or self.next_in_s() is not None

    @property
    def waiting(self) -> bool:
        """LOOP is between runs: the Conductor will start the next one by
        itself, and STOP is what cancels that."""
        return self.next_in_s() is not None

    def next_in_s(self) -> "float | None":
        """Seconds until the loop's next run, moved on by the time since
        the poll - or None when the loop is not waiting. Read whatever
        `run` is: the Conductor keeps the ENDED run in place during the
        wait (review of 951e0b7, MED-1)."""
        loop = (self.fleet or {}).get("loop")
        if not isinstance(loop, dict) or not loop.get("on"):
            return None
        left = loop.get("next_in_s")
        if not isinstance(left, (int, float)) or isinstance(left, bool):
            return None
        return max(0.0, float(left) - (self._clock() - self.fleet_at))

    def recent(self, count: int) -> "list[str]":
        with self._lock:
            return list(self.log)[-count:]

    def emit(self, message: str, error: bool = False) -> None:
        first = _first_line(message)
        if error and not first.startswith("ERROR"):
            first = f"ERROR {first}"
        stamp = time.strftime("%H:%M:%S")
        with self._lock:
            self.log.append(f"{stamp} {first}")
        if self._echo_log:
            print(f"{stamp} {message.rstrip()}", flush=True)

    def _duration(self) -> "float | None":
        fleet = self.fleet or {}
        duration = fleet.get("show_duration")
        if duration is None and self.show:
            duration = self.show.get("duration")
        try:
            return None if duration is None else float(duration)
        except (TypeError, ValueError):
            return None

    def _run_now(self, run: dict) -> "float | None":
        """Seconds into the show right now: the Conductor's `now` moved
        on by the time since that poll (a held run stands still)."""
        now = run.get("now")
        if not isinstance(now, (int, float)):
            return None
        if run.get("state") == "holding":
            return float(now)
        return float(now) + max(0.0, self._clock() - self.fleet_at)

    def show_lines(self) -> "tuple[str, str]":
        """The show: its name, then `N cues · m:ss` (and whether it is on
        the units at all)."""
        if not self.available:
            return "", ""
        show = self.show
        fleet = self.fleet or {}
        name = (show or {}).get("name") or "show"
        parts = []
        cues = (show or {}).get("cues")
        if cues is not None:
            parts.append(f"{cues} cue{'s' if cues != 1 else ''}")
        duration = self._duration()
        if duration is not None:
            parts.append(format_clock(duration))
        if not fleet.get("shows"):
            parts.append("not uploaded")
        return name, " · ".join(parts)

    def run_text(self) -> str:
        """idle / countdown -0:11 / 0:00 / 10:54 running / hold ... /
        ended ... / next run in 0:25 (the loop's wait)."""
        fleet = self.fleet
        if not fleet:
            return ""
        left = self.next_in_s()
        if left is not None:
            return f"next run in {format_clock(math.ceil(left))}"
        run = fleet.get("run")
        duration = self._duration()
        if run is None:
            return "idle"
        now = self._run_now(run)
        if now is None:
            return str(run.get("state") or "running")
        if now < 0:
            return f"countdown -{format_clock(math.ceil(-now))}"
        total = "" if duration is None else f" / {format_clock(duration)}"
        if run.get("state") == "holding":
            return f"hold {format_clock(now)}{total}"
        if duration is not None and now >= duration:
            return f"ended {format_clock(duration)}{total}"
        return f"{format_clock(now)}{total} running"

    def fleet_text(self) -> str:
        fleet = self.fleet
        if not fleet:
            return ""
        units = fleet.get("units") or []
        online = sum(1 for unit in units if isinstance(unit, dict)
                     and unit.get("online"))
        return f"units {online}/{len(units)} online"

    def loop_text(self) -> str:
        loop = (self.fleet or {}).get("loop")
        if not isinstance(loop, dict) or "on" not in loop:
            return "loop ?"
        return "LOOP on" if loop.get("on") else "LOOP off"

    def loop_on(self) -> "bool | None":
        loop = (self.fleet or {}).get("loop")
        if not isinstance(loop, dict) or "on" not in loop:
            return None
        return bool(loop.get("on"))

    def speaker_text(self) -> str:
        speaker = (self.fleet or {}).get("speaker")
        if not isinstance(speaker, dict) or "available" not in speaker:
            return "speaker ?"
        if speaker.get("available"):
            volume = speaker.get("volume")
            if not isinstance(volume, (int, float)) or isinstance(volume, bool):
                return "speaker ok · vol ?"
            text = f"speaker ok · vol {int(volume)}%"
            if speaker.get("applied"):
                text += f" ({speaker['applied']})"
            return text
        error = _first_line(speaker.get("error") or "")
        return f"no speaker - {error}" if error else "no speaker"

    def speaker_available(self) -> bool:
        speaker = (self.fleet or {}).get("speaker")
        return isinstance(speaker, dict) and bool(speaker.get("available"))

    def volume_supported(self) -> bool:
        """The Conductor's speaker object carries `volume` (Coder Z's
        /api/speaker/volume exists there)."""
        speaker = (self.fleet or {}).get("speaker")
        return isinstance(speaker, dict) and "volume" in speaker

    def status_text(self) -> str:
        """The line under the state: what a command is doing or did."""
        if self.phase == SENDING:
            return "sending…"
        if self.phase in (DONE, FAILED):
            return self.note
        if self.available is False and self.fleet_error:
            return f"{NO_CONDUCTOR}: {self.fleet_error}"
        return ""

    def key(self) -> tuple:
        """Everything the screen shows, for the App's redraw check - the
        run clock to the second, so a running show repaints once a
        second and an idle one not at all."""
        return (self.available, self.phase, self.note, self.run_text(),
                self.fleet_text(), self.loop_text(), self.speaker_text(),
                self.show_lines(), self.active)

    # ---- the screen opening and closing ----

    def open(self) -> None:
        """The screen was opened: poll now and every poll_open_s, and
        read the show's name once."""
        self.reset()
        self.is_open = True
        self._want_show = True
        self.refresh()

    def close(self) -> None:
        """KEY2: back to the slow poll. A command in flight completes;
        a run on the Conductor is none of this screen's business."""
        self.is_open = False
        if not self.busy:
            self.reset()

    def reset(self) -> None:
        """A verdict was read (UP/DOWN, or the screen re-opened)."""
        if self.busy:
            return
        self.phase = IDLE
        self.command = None
        self.note = ""

    # ---- the reader ----

    def start_reader(self) -> None:
        """The daemon that fills the cache (main wires it)."""
        if self._reader is not None:
            return
        self._reader = threading.Thread(target=self._loop, daemon=True,
                                        name="exhibition-reader")
        self._reader.start()

    def refresh(self) -> None:
        if self._reader is None:
            self.start_reader()         # its first poll is right away
        else:
            self._wake.set()

    def shutdown(self) -> None:
        self._stop.set()
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.poll()
            self._wake.wait(self.poll_open_s if self.is_open
                            else self.poll_idle_s)
            self._wake.clear()

    def _get(self, path: str, timeout: float):
        code, payload = self._http("GET", self.base + path, None, timeout)
        if code != 200:
            raise RuntimeError(self._reason(code, payload))
        if not isinstance(payload, dict):
            raise RuntimeError(f"{path}: not a JSON object")
        return payload

    @staticmethod
    def _reason(code: int, payload) -> str:
        if isinstance(payload, dict) and payload.get("error"):
            return _first_line(payload["error"])
        text = _first_line(payload) if isinstance(payload, str) else ""
        return text or f"HTTP {code}"

    def poll(self) -> None:
        """One refresh of the cache, on the caller's thread (the
        reader's, normally; a test's). Never raises."""
        with self._poll_lock:
            try:
                fleet = self._get(FLEET_PATH, self.probe_timeout)
            except Exception as exc:        # noqa: BLE001 - shown, not raised
                reason = _why(exc)
                with self._lock:
                    was = self.available
                    self.available = False
                    self.fleet = None
                    self.fleet_error = reason
                if was is not False:
                    self.emit(f"exhibition: {NO_CONDUCTOR} ({reason})")
                return
            with self._lock:
                was = self.available
                self.available = True
                self.fleet = fleet
                self.fleet_at = self._clock()
                self.fleet_error = None
                want_show = self._want_show
            if was is not True:
                self.emit(f"exhibition: conductor at {self.base}")
            if want_show:
                self._read_show()

    def _read_show(self) -> None:
        try:
            export = self._get(SHOW_PATH, self.command_timeout)
        except Exception as exc:            # noqa: BLE001 - the name is a nicety
            self.emit(f"exhibition: show name unread ({_why(exc)})")
            return
        cues = export.get("cues")
        with self._lock:
            self.show = {"name": str(export.get("workspace") or "show"),
                         "cues": len(cues) if isinstance(cues, list) else None,
                         "duration": export.get("duration")}
            self._want_show = False

    # ---- the commands ----

    def start(self) -> None:
        """START: the Conductor's own countdown, then the show."""
        self._send("start", START_PATH, {})

    def stop(self) -> None:
        self._send("stop", STOP_PATH, {})

    def toggle_loop(self) -> None:
        """LOOP on <-> off (an unknown flag is turned on)."""
        self._send("loop", LOOP_PATH, {"on": not self.loop_on()})

    def adjust_volume(self, delta: int) -> None:
        """Joystick LEFT / RIGHT: the speaker's volume by `delta`. Not a
        SENDING command - the keys stay live, and presses that land
        while a request is out add up into the next one (one request
        in flight, ever)."""
        if not self.available or not self.speaker_available():
            return
        if not self.volume_supported():
            if not self.busy:
                self.phase = DONE
                self.note = VOLUME_UNSUPPORTED
            return
        with self._lock:
            if self._volume_inflight:
                self._volume_wanted += delta
                return
            self._volume_inflight = True
        try:
            self._volume_thread = threading.Thread(
                target=self._volume_worker, args=(delta,), daemon=True,
                name="exhibition-volume")
            self._volume_thread.start()
        except Exception as exc:            # noqa: BLE001 - never stuck in flight
            with self._lock:
                self._volume_inflight = False
                self._volume_wanted = 0
            self._volume_verdict(f"ERROR could not start: {_first_line(str(exc))}",
                                 error=True)

    def join_volume(self, timeout: "float | None" = None) -> None:
        thread = self._volume_thread
        if thread is not None:
            thread.join(timeout)

    def _volume_worker(self, delta: int) -> None:
        try:
            while True:
                self._volume_once(delta)
                with self._lock:
                    delta, self._volume_wanted = self._volume_wanted, 0
                    if delta == 0:
                        self._volume_inflight = False
                        return
        except BaseException:
            with self._lock:
                self._volume_inflight = False
                self._volume_wanted = 0
            raise

    def _volume_once(self, delta: int) -> None:
        body = {"delta": int(delta)}
        self.emit(f"exhibition: volume -> {VOLUME_PATH} {json.dumps(body)}")
        try:
            code, payload = self._http("POST", self.base + VOLUME_PATH, body,
                                       self.command_timeout)
        except Exception as exc:            # noqa: BLE001 - a verdict, not a crash
            self._volume_verdict(f"ERROR volume: {_why(exc)}", error=True)
            return
        if code >= 400 or not isinstance(payload, dict):
            self._volume_verdict(f"ERROR volume: {self._reason(code, payload)}",
                                 error=True)
            return
        with self._lock:
            if self.fleet is not None:
                speaker = dict(self.fleet.get("speaker") or {})
                for key in ("volume", "applied"):
                    if key in payload:
                        speaker[key] = payload[key]
                self.fleet = dict(self.fleet, speaker=speaker)
        if payload.get("error"):
            self._volume_verdict(f"ERROR volume: {_first_line(payload['error'])}",
                                 error=True)
            return
        volume = payload.get("volume")
        self._volume_verdict(f"vol {int(volume)}%" if isinstance(volume, (int, float))
                             else "volume set")

    def _volume_verdict(self, note: str, error: bool = False) -> None:
        # The speaker line is what really shows the result; the note
        # only when no START/STOP/LOOP verdict is in flight or waiting.
        self.emit(note, error=error)
        if self.busy:
            return
        self.note = note
        self.phase = FAILED if error else DONE

    def _send(self, command: str, path: str, body: dict) -> None:
        if self.busy:
            return
        self.phase = SENDING
        self.command = command
        self.note = ""
        try:
            self._thread = threading.Thread(target=self._command,
                                            args=(command, path, body),
                                            daemon=True, name="exhibition-cmd")
            self._thread.start()
        except Exception as exc:            # noqa: BLE001 - never stuck SENDING
            self.note = f"ERROR could not start: {_first_line(str(exc))}"
            self.emit(self.note, error=True)
            self.phase = FAILED

    def join(self, timeout: "float | None" = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _command(self, command: str, path: str, body: dict) -> None:
        self.emit(f"exhibition: {command} -> {path} {json.dumps(body)}")
        try:
            code, payload = self._http("POST", self.base + path, body,
                                       self.command_timeout)
        except Exception as exc:            # noqa: BLE001 - a verdict, not a crash
            self._verdict(FAILED, f"ERROR {_why(exc)}")
            return
        if code >= 400:
            # The Conductor's refusal, verbatim (its first line).
            self._verdict(FAILED, f"ERROR {self._reason(code, payload)}")
            return
        self._verdict(DONE, self._describe(command, payload))

    def _verdict(self, phase: str, note: str) -> None:
        # The state is read again BEFORE the verdict shows, so the
        # screen that says DONE already shows the countdown (or, after
        # a failure, that the Conductor is gone).
        self.poll()
        self.note = note
        self.emit(note, error=phase == FAILED)
        self.phase = phase

    def _describe(self, command: str, payload) -> str:
        if not isinstance(payload, dict):
            return f"{command.upper()} sent"
        if command == "loop":
            # Answered with the loop object: the screen flips at once,
            # without waiting for the next poll.
            if "on" in payload:
                with self._lock:
                    if self.fleet is not None:
                        self.fleet = dict(self.fleet, loop=payload)
                return f"LOOP {'on' if payload.get('on') else 'off'}"
            return "LOOP sent"
        if payload.get("note"):
            # A soft refusal ("Nothing uploaded yet - Upload first.") or
            # a remark ("Started from 1:30.") - the Conductor's words.
            return _first_line(payload["note"])
        units = payload.get("units")
        if not isinstance(units, dict):
            return f"{command.upper()} sent"
        ok = sum(1 for result in units.values()
                 if isinstance(result, dict) and result.get("ok"))
        text = f"{command.upper()} · {ok}/{len(units)} units"
        if command == "start" and isinstance(payload.get("lead_s"), (int, float)):
            text = f"START in {payload['lead_s']:g} s · {ok}/{len(units)} units"
        failed = [(name, result) for name, result in units.items()
                  if not (isinstance(result, dict) and result.get("ok"))]
        if failed:
            name, result = failed[0]
            reason = (result.get("error") if isinstance(result, dict)
                      else None) or "refused"
            text += f" - {name}: {_first_line(reason)}"
        return text
