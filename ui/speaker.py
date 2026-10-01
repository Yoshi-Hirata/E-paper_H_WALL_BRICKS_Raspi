"""SPEAKER: see, connect and re-pair the Bluetooth speaker from the LCD.

radxa-05 plays the show's music through a Bose over Bluetooth, driven
by the Conductor it runs itself (ui/exhibition.py). When the link drops
in the middle of an exhibition - the Bose went to sleep, somebody's
phone grabbed it, bluez lost the sink - the only fix used to be a phone
or a laptop on the hotspot. The SPEAKER menu row (right after
EXHIBITION) is that fix on the HAT: what the Conductor sees of the
speaker, a held KEY1 to connect, a held KEY3 (twice) to pair again.

Everything shown is a view over the EXHIBITION row's cache - the same
`GET /api/fleet` poll, never a second poller: the Conductor's `speaker`
object carries, besides `available / state / track / volume / applied /
error`, the Bluetooth side (Coder AC's Conductor):

    device      {mac, name, paired, trusted, connected, sink_present,
                 last_connected_at, last_error} | null
    connection  "connected" | "disconnected" | "connecting" | "pairing"
                | "no_device"
    reconnect   {attempts, next_in_s, last_error}
    pairing     {phase: scanning | pairing | connecting | done | failed,
                 note, started_at} | null

    bluetooth   false when the output is not Bluetooth at all
                (--speaker-output pulse with a wired sink): then there
                is no link to lose or to pair - the screen says `wired /
                not Bluetooth` and offers the volume only

and the two commands this screen sends, both answering {"ok",
"connection", "error"}:

    POST /api/speaker/connect {}     KEY1 held. Never refused for a run
                                     (connecting is harmless); 409 only
                                     while a pairing is in progress, no
                                     force. Answers "connecting" - the
                                     poll shows the outcome, so the
                                     verdict is amber `connecting…`,
                                     never a green DONE
    POST /api/speaker/pair    {}     KEY3 held twice within
                                     PAIR_CONFIRM_S. 409 "show running -
                                     …" during a run, which {"force":
                                     true} overrides (the next hold); 409
                                     "re-pairing is already in progress"
                                     has no force. Every refusal is shown
                                     verbatim

The row appears once the Conductor here has reported a `speaker` key
and then stays, its label following the cache: `SPEAKER  (no
conductor)` while nothing answers, `SPEAKER  (no speaker)` on a
Conductor started without --speaker (speaker null; the screen only says
so). An older Conductor whose speaker object has no `connection`
degrades: the screen reads `speaker ?` and the holds say `not supported
by this conductor`. While the screen is open the reader polls every 2 s
(a pairing's progress - scanning… / pairing… / connecting… - comes from
the poll), and while a request or a pairing is in flight only KEY2 is
heard. Every request runs on a worker thread through the Exhibition's
one injectable HTTP function, so the tests run against a fake Conductor
and the HAT loop never waits on a socket.
"""

from __future__ import annotations

import json
import math
import threading
import time
from datetime import datetime, timezone

from .exhibition import (VOLUME_UNSUPPORTED, Exhibition, _first_line,
                         _percent, _why)

IDLE = "idle"                # nothing in flight
BUSY = "busy"                # a request is on its way to the Conductor
DONE = "done"                # the Conductor answered
FAILED = "failed"            # refused, or no answer within the timeout

CONNECT_PATH = "/api/speaker/connect"
PAIR_PATH = "/api/speaker/pair"
PAIR_CONFIRM_S = 15.0        # the second KEY3 hold must land within this
PAIRING_STALE_S = 300.0      # a `pairing` older than this is not in progress

MENU_LABEL = "SPEAKER"
MENU_LABEL_NONE = "SPEAKER  (no speaker)"
MENU_LABEL_GONE = "SPEAKER  (no conductor)"
NOT_SUPPORTED = "not supported by this conductor"
NOT_BLUETOOTH = "wired speaker - nothing to connect"
PAIR_CONFIRM = "hold KEY3 again = pair"
PAIR_FORCE = "show running - hold KEY3 again to pair anyway"
CONNECTING = "connecting…"
INSTRUCTION = ("put the Bose in pairing mode (hold its Bluetooth button) "
               "and switch off phones' Bluetooth")
MUSIC_LOST = "MUSIC LOST"
NO_SINK = "connected, no sound output"   # connection "no_sink": paired and
                                         # connected, but no PulseAudio sink
WIRED = "wired / not Bluetooth"
PAIRING_PHASES = ("scanning", "pairing", "connecting")

# Where the screen is, in one word (render.speaker_screen's `mode`).
MODE_CHECKING = "checking"   # the reader has not answered yet
MODE_MISSING = "missing"     # no Conductor on this unit
MODE_NONE = "none"           # the Conductor runs without --speaker
MODE_OLD = "old"             # a Conductor from before the Bluetooth keys
MODE_WIRED = "wired"         # the output is not Bluetooth: volume only
MODE_OK = "ok"


class SpeakerRow:
    """The menu row: `SPEAKER`, `SPEAKER  (no speaker)` on a Conductor
    without --speaker, `SPEAKER  (no conductor)` while none answers.
    The App adds it once the Conductor has reported a speaker key and
    keeps it (App._refresh_speaker_row)."""

    key = "speaker"

    def __init__(self, speaker: "Speaker"):
        self._speaker = speaker

    @property
    def label(self) -> str:
        if not self._speaker.present:
            return MENU_LABEL_GONE
        return MENU_LABEL if self._speaker.configured else MENU_LABEL_NONE

    @property
    def detail(self) -> str:
        if not self._speaker.present:
            return "needs the Conductor on this unit"
        if not self._speaker.configured:
            return "the Conductor runs without --speaker"
        if not self._speaker.bluetooth:
            return "wired speaker: volume only"
        return "connect / re-pair the Bluetooth speaker"


def _ago(seconds: float) -> str:
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    return f"{int(seconds // 86400)} d ago"


def _epoch(value) -> "float | None":
    """`last_connected_at` as epoch seconds: a number, or an ISO 8601
    string (with or without a zone - a naive one is read as UTC); None
    for anything else."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            when = datetime.fromisoformat(text)
        except ValueError:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return when.timestamp()
    return None


def format_error(text) -> str:
    """The speaker's last_error as one line the operator can act on:
    bluez's `Permission denied` means another device holds the Bose."""
    first = _first_line(text or "")
    if not first:
        return ""
    if "Permission denied" in first:
        return "Permission denied - another phone?"
    return first


class Speaker:
    """State behind the SPEAKER screen: a view over the Exhibition's
    cache plus this screen's own command (connect / pair) state.

    `clock` is the monotonic clock the confirm windows and the reconnect
    countdown run on - the Exhibition's own unless given, since
    `fleet_at` is written in that one; `wall` is time.time, for
    `connected 12 min ago`."""

    def __init__(self, exhibition: Exhibition, clock=None, wall=time.time):
        self._ex = exhibition
        self._clock = clock or exhibition._clock
        self._wall = wall
        self.phase = IDLE
        self.command: "str | None" = None         # connect / pair
        self.note = ""                            # the line under the state
        self.is_open = False
        # The Conductor here has reported a speaker key at least once:
        # the row exists from then on (its label follows the cache).
        self._seen = False
        self._lock = threading.Lock()
        self._thread: "threading.Thread | None" = None
        # KEY3 twice: the first hold opens this window, the second pairs.
        self._confirm_until = 0.0
        # A pair 409 (run active): the next KEY3 hold sends force.
        self._force_until = 0.0

    # ---- facts for the row and the screen ----

    @property
    def menu_entry(self) -> SpeakerRow:
        return SpeakerRow(self)

    def _speaker(self) -> "dict | None":
        speaker = (self._ex.fleet or {}).get("speaker")
        return speaker if isinstance(speaker, dict) else None

    @property
    def present(self) -> bool:
        """A Conductor answers here and its fleet carries the `speaker`
        key (null or not)."""
        fleet = self._ex.fleet
        if bool(self._ex.available) and isinstance(fleet, dict) \
                and "speaker" in fleet:
            self._seen = True
            return True
        return False

    @property
    def seen(self) -> bool:
        """The row belongs on the menu: a speaker key was reported at
        least once (and stays, whatever the cache says since)."""
        return self._seen or self.present

    @property
    def configured(self) -> bool:
        """The Conductor runs with --speaker (speaker is an object)."""
        return self.present and self._speaker() is not None

    @property
    def supported(self) -> bool:
        """The speaker object carries the Bluetooth keys (Coder AC's
        Conductor): connect / pair exist there."""
        speaker = self._speaker()
        return speaker is not None and "connection" in speaker

    @property
    def bluetooth(self) -> bool:
        """The output is Bluetooth (speaker.bluetooth is not false): a
        link to lose, a device to connect and pair. Wired (pulse with a
        wired sink): the screen is the volume alone."""
        speaker = self._speaker()
        return speaker is not None and speaker.get("bluetooth") is not False

    def mode(self) -> str:
        if self._ex.available is None:
            return MODE_CHECKING
        if not self.present:
            return MODE_MISSING
        if not self.configured:
            return MODE_NONE
        if not self.bluetooth:
            return MODE_WIRED
        if not self.supported:
            return MODE_OLD
        return MODE_OK

    def connection(self) -> "str | None":
        """The `connection` word, None when absent; a value that is not
        even a string is reported as "?" (not None, so it counts as
        lost - never as "not asked")."""
        speaker = self._speaker() or {}
        value = speaker.get("connection")
        if value is None:
            return None
        return value if isinstance(value, str) else "?"

    def device(self) -> "dict | None":
        device = (self._speaker() or {}).get("device")
        return device if isinstance(device, dict) else None

    def pairing(self) -> "dict | None":
        pairing = (self._speaker() or {}).get("pairing")
        return pairing if isinstance(pairing, dict) else None

    @property
    def pairing_active(self) -> bool:
        """The Conductor is scanning / pairing / connecting right now
        (read from the poll): the keys stay shut until it is done. A
        `pairing` whose started_at is older than PAIRING_STALE_S is a
        leftover, not a pairing - the keys must not stay shut on it
        (review of 73c8fdc, LOW-4)."""
        pairing = self.pairing()
        if (pairing is None or not self.bluetooth
                or pairing.get("phase") not in PAIRING_PHASES):
            return False
        started = _epoch(pairing.get("started_at"))
        return started is None or self._wall() - started < PAIRING_STALE_S

    @property
    def busy(self) -> bool:
        """A request is in flight, or a pairing is: only KEY2 is heard."""
        return self.phase == BUSY or self.pairing_active

    @property
    def confirming(self) -> bool:
        """The first KEY3 hold landed; the second pairs."""
        return self._confirm_until > self._clock()

    @property
    def forcing(self) -> bool:
        """The next KEY3 hold sends {"force": true} (a run-409 came)."""
        return self._force_until > self._clock()

    def reconnect_in_s(self) -> "float | None":
        """Seconds until the Conductor's next reconnect attempt, moved
        on by the time since the poll - None when none is pending."""
        reconnect = (self._speaker() or {}).get("reconnect")
        if not isinstance(reconnect, dict):
            return None
        left = reconnect.get("next_in_s")
        if isinstance(left, bool) or not isinstance(left, (int, float)):
            return None
        return max(0.0, float(left) - (self._clock() - self._ex.fleet_at))

    def status_word(self) -> str:
        """The header's word: READY / BUSY / DONE / FAILED."""
        if self.phase == BUSY or self.pairing_active:
            return "BUSY"
        return {DONE: "DONE", FAILED: "FAILED"}.get(self.phase, "READY")

    def device_text(self) -> str:
        """Line 1: the speaker's name, or that none is paired."""
        if not self.bluetooth:
            return WIRED
        if not self.supported:
            return "speaker ?"
        device = self.device()
        if device is None:
            return "no speaker paired"
        name = device.get("name") or device.get("mac") or "speaker"
        return _first_line(name) or "speaker"

    def state_text(self) -> str:
        """Line 2: `connected · vol 70%` / `NOT CONNECTED` / `connecting…`
        / `pairing: scanning…` (the render tints by the words)."""
        if not self.bluetooth:
            volume = _percent((self._speaker() or {}).get("volume"))
            return "vol ?" if volume is None else f"vol {volume}%"
        if not self.supported:
            return self._ex.speaker_text()
        connection = self.connection()
        if self.pairing_active:
            return f"pairing: {self.pairing()['phase']}…"
        if connection == "connected":
            volume = _percent((self._speaker() or {}).get("volume"))
            return "connected · vol ?" if volume is None \
                else f"connected · vol {volume}%"
        if connection == "connecting":
            return "connecting…"
        if connection == "pairing":
            return "pairing…"
        if connection == "no_sink":
            return NO_SINK
        if connection is None:
            return "?"
        # disconnected, no_device - and any value this UI has never
        # heard of: the music is not playing, say so (the banner agrees).
        return "NOT CONNECTED"

    def detail_text(self) -> "tuple[str, str]":
        """The small line under the state and its tone ("", "warn",
        "err"): a failed pairing's note, the reconnect countdown, or the
        last error (`Permission denied - another phone?`)."""
        if not self.supported or not self.bluetooth:
            return "", ""
        speaker = self._speaker() or {}
        pairing = self.pairing()
        if pairing is not None and pairing.get("phase") == "failed":
            note = format_error(pairing.get("note"))
            return (f"pairing failed - {note}" if note else "pairing failed"), "err"
        if self.pairing_active:
            note = _first_line(pairing.get("note") or "")
            return note, ""
        connection = self.connection()
        if connection != "connected":
            left = self.reconnect_in_s()
            reconnect = speaker.get("reconnect")
            if left is not None:
                attempts = (reconnect or {}).get("attempts")
                tries = ""
                if isinstance(attempts, int) and not isinstance(attempts, bool) \
                        and attempts > 0:
                    tries = f" ({attempts} {'try' if attempts == 1 else 'tries'})"
                return f"reconnect in {math.ceil(left)} s{tries}", "warn"
            error = ""
            device = self.device()
            if device is not None:
                error = format_error(device.get("last_error"))
            if not error and isinstance(reconnect, dict):
                error = format_error(reconnect.get("last_error"))
            if not error:
                error = format_error(speaker.get("error"))
            if error:
                return error, "err"
        return "", ""

    def seen_text(self) -> str:
        """Line 3: `connected 12 min ago` from device.last_connected_at."""
        device = self.device()
        if device is None or not self.supported or not self.bluetooth:
            return ""
        when = _epoch(device.get("last_connected_at"))
        if when is None:
            return "never connected"
        return f"connected {_ago(max(0.0, self._wall() - when))}"

    def banner(self) -> str:
        """`MUSIC LOST` while a run is on and the speaker is not
        connected - the one thing to read from across the room."""
        fleet = self._ex.fleet or {}
        if not self.supported or not self.bluetooth or fleet.get("run") is None:
            return ""
        return MUSIC_LOST if self.connection() != "connected" else ""

    def show_instruction(self) -> bool:
        """The pairing instruction belongs on the screen from the first
        KEY3 hold until the pairing is over."""
        if not self.supported or not self.bluetooth:
            return False
        return (self.confirming or self.forcing or self.pairing_active
                or (self.phase == BUSY and self.command == "pair"))

    def status_text(self) -> str:
        """The verdict line. The "hold KEY3 again" prompts go with their
        15 s window (review of 73c8fdc, LOW-5); a volume error (the
        Exhibition's own verdict, LEFT/RIGHT are its request) shows here
        too, since this screen has the keys (LOW-8)."""
        if self.phase == BUSY:
            return "sending…"
        if self.mode() == MODE_MISSING and self._ex.fleet_error:
            return f"no conductor: {self._ex.fleet_error}"
        note = self.note
        if note == PAIR_CONFIRM and not self.confirming:
            note = ""
        elif note == PAIR_FORCE and not self.forcing:
            note = ""
        if not note and self._ex.phase != "sending":
            volume = self._ex.note
            if volume == VOLUME_UNSUPPORTED or volume.startswith("ERROR volume"):
                note = volume
        return note

    def volume_keys(self) -> bool:
        return (self.configured and self._ex.speaker_available()
                and self._ex.volume_supported())

    def key(self) -> tuple:
        """Everything the screen shows, for the App's redraw check."""
        return (self.mode(), self.status_word(), self.status_text(),
                self._ex.fleet_error, self.device_text(), self.state_text(),
                self.detail_text(), self.seen_text(), self.banner(),
                self.show_instruction(), self.volume_keys(), self.busy)

    # ---- the screen opening and closing ----

    def open(self) -> None:
        """The screen was opened: the fast poll, now and every 2 s."""
        self.reset()
        self.is_open = True
        self._ex.speaker_open = True
        self._ex.refresh()

    def close(self) -> None:
        """KEY2: back to whatever poll the EXHIBITION row wants. A
        request in flight completes on its own."""
        self.is_open = False
        self._ex.speaker_open = False
        if self.phase != BUSY:
            self.reset()

    def reset(self) -> None:
        """A verdict was read (UP/DOWN, or the screen re-opened): the
        note goes, and so do the confirm and force windows - and the
        volume verdict, which is the Exhibition's (status_text shows
        its errors here)."""
        with self._lock:
            if self.phase == BUSY:
                return
            self.phase = IDLE
            self.command = None
            self.note = ""
            self._confirm_until = 0.0
            self._force_until = 0.0
        self._ex.reset()

    # ---- the commands ----

    def connect(self) -> None:
        """KEY1 held: connect the paired speaker. The Conductor never
        refuses it for a run; it answers "connecting" and the poll
        shows the outcome."""
        if not self._ready("connect"):
            return
        with self._lock:
            self._confirm_until = 0.0
            self._force_until = 0.0
        self._send("connect", CONNECT_PATH, {})

    def pair(self) -> None:
        """KEY3 held: the first hold shows the instruction and asks for
        a second within PAIR_CONFIRM_S; the second sends the pair; after
        a run-409 the next one sends {"force": true}."""
        if not self._ready("pair"):
            return
        now = self._clock()
        with self._lock:
            if self.phase == BUSY:
                return
            if self._force_until > now:
                body = {"force": True}
            elif self._confirm_until > now:
                body = {}
            else:
                self._confirm_until = now + PAIR_CONFIRM_S
                self._force_until = 0.0
                self.phase = IDLE
                self.command = None
                self.note = PAIR_CONFIRM
                self._ex.emit("speaker: pair - confirm asked")
                return
        self._send("pair", PAIR_PATH, body)

    def _ready(self, command: str) -> bool:
        if not self.configured or self.busy:
            return False
        if not self.bluetooth or not self.supported:
            with self._lock:
                if self.phase != BUSY:
                    self.phase = DONE
                    self.note = (NOT_BLUETOOTH if not self.bluetooth
                                 else f"{command}: {NOT_SUPPORTED}")
            return False
        return True

    def _send(self, command: str, path: str, body: dict) -> None:
        with self._lock:
            if self.phase == BUSY:
                return
            self.phase = BUSY
            self.command = command
            self.note = ""
            self._confirm_until = 0.0
            self._force_until = 0.0
        try:
            self._thread = threading.Thread(target=self._command,
                                            args=(command, path, body),
                                            daemon=True, name="speaker-cmd")
            self._thread.start()
        except Exception as exc:            # noqa: BLE001 - never stuck BUSY
            self._verdict(FAILED, f"ERROR could not start: {_first_line(str(exc))}",
                          poll=False)

    def join(self, timeout: "float | None" = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _command(self, command: str, path: str, body: dict) -> None:
        # Whatever happens on this thread ends in a verdict: the phase
        # must never stick at BUSY with the keys shut (review of 73c8fdc,
        # LOW-7).
        try:
            self._request(command, path, body)
        except Exception as exc:            # noqa: BLE001 - the keys come back
            self._verdict(FAILED, f"ERROR {_first_line(str(exc)) or exc.__class__.__name__}",
                          poll=False)

    def _request(self, command: str, path: str, body: dict) -> None:
        ex = self._ex
        ex.emit(f"speaker: {command} -> {path} {json.dumps(body)}")
        try:
            code, payload = ex._http("POST", ex.base + path, body,
                                     ex.command_timeout)
        except Exception as exc:            # noqa: BLE001 - a verdict, not a crash
            self._verdict(FAILED, f"ERROR {_why(exc)}")
            return
        if code == 409:
            reason = ex._reason(code, payload)
            if command == "pair" and self._run_refusal(reason):
                # A run is on: the Conductor wants to be told twice. The
                # next hold within the window sends {"force": true}.
                self._verdict(IDLE, PAIR_FORCE, force=True)
                return
            # Any other 409 (a pairing already in progress - for connect,
            # the only one there is): the Conductor's words, no force.
            self._verdict(FAILED, f"ERROR {reason}")
            return
        if code >= 400:
            self._verdict(FAILED, f"ERROR {ex._reason(code, payload)}")
            return
        if not isinstance(payload, dict):
            self._verdict(DONE, f"{command} sent")
            return
        connection = payload.get("connection")
        if isinstance(connection, str):
            # The screen flips at once, without waiting for the poll.
            with ex._lock:
                if ex.fleet is not None and isinstance(ex.fleet.get("speaker"), dict):
                    speaker = dict(ex.fleet["speaker"], connection=connection)
                    ex.fleet = dict(ex.fleet, speaker=speaker)
        if payload.get("error") and not payload.get("ok"):
            self._verdict(FAILED, f"ERROR {_first_line(payload['error'])}")
            return
        if not payload.get("ok"):
            self._verdict(FAILED, f"ERROR {command} refused")
            return
        if connection == "connected":
            self._verdict(DONE, "connected" if command == "connect"
                          else "paired · connected")
        elif connection == "connecting":
            # Async: the poll decides. Amber `connecting…`, never a
            # green DONE (review of 73c8fdc, MED-1).
            self._verdict(IDLE, CONNECTING)
        elif connection == "pairing":
            self._verdict(DONE, "pairing started")
        else:
            self._verdict(DONE, f"{command} sent" if not connection
                          else str(connection))

    def _run_refusal(self, reason: str) -> bool:
        """Is this pair-409 the "show running" one (which force
        overrides), as opposed to "re-pairing is already in progress"
        (which nothing does)? By the Conductor's words; and if they say
        neither, by whether the cache has a run."""
        text = reason.lower()
        if text.startswith("show running") or "run is on" in text:
            return True
        if "progress" in text or "pairing" in text:
            return False
        return (self._ex.fleet or {}).get("run") is not None

    def _verdict(self, phase: str, note: str, force: bool = False,
                 poll: bool = True) -> None:
        # The cache is read again BEFORE the verdict shows, so the DONE
        # screen already carries the new connection (or the pairing's
        # first phase) - and whatever that read does, the phase is
        # written (LOW-7).
        try:
            if poll:
                try:
                    self._ex.poll()         # "never raises" - but the
                except Exception as exc:    # noqa: BLE001 - verdict stands
                    self._ex.emit(f"speaker: re-read failed ({_why(exc)})",
                                  error=True)
            self._ex.emit(f"speaker: {note}", error=phase == FAILED)
        finally:
            with self._lock:
                self.note = note
                self.phase = phase
                if force:
                    self._force_until = self._clock() + PAIR_CONFIRM_S
