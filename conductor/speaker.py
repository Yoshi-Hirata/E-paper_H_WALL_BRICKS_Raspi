"""The show's music on the Conductor host itself (EXHIBITION mode).

At the exhibition the Conductor runs headless on a unit (radxa-05) with a
USB speaker and no browser, so the page's own <audio> player - which is
what plays the music on the show PC - has nobody to play it. This module
does that job on the host: `python -m conductor serve --speaker` drives
one `mpg123 -R --keep-open` (its remote-control mode: commands on stdin,
`@P` / `@E` replies on stdout; --keep-open so the process outlives the end
of a track) from a thread of its own, following the same run the fleet is
driving, on the same reference clock (fleet.pc_clock).

    load    LOADPAUSED <file>   the track sits paused at 0:00 - "LOAD then
                                PAUSE immediately", in mpg123's own single
                                command for exactly that, so not a frame is
                                heard at load time (a build that answers a
                                LOADPAUSED with "@P 2" is paused with a P)
    T0      P                   unpause - sent LATENCY early (below)
    HOLD    P                   pause where it is
    RESUME / SEEK / NEXT / a START from a mark
            JUMP <pos>s, then P if paused
    STOP / the show's end
            P (pause), JUMP 0s  - the track stays loaded for the next run

`P` TOGGLES, so it is only ever sent when mpg123's own play state - the
last "@P n" it printed, kept as the truth and never guessed - differs from
the state wanted; a reply that arrives late is matched to its command by a
sequence count, never mistaken for the answer to the next one.

The end of the file (a track shorter than the show, §4.4). Measured on
radxa-05 (mpg123 1.26.4, `-R --keep-open`, 2026-09-30): at EOF mpg123
prints an UNSOLICITED "@P 1" (paused) - not "@P 0" - after a last frame
line "@F <n> 0 ..." with 0 frames left. Sitting there, a `P` answers
"@P 2" and at once "@P 1" again (nothing to play), and a `JUMP 0s` does
NOT make it playable again: only a second LOADPAUSED does. So a "@P 1"
this side did not ask for, while the state was playing, is the end of the
file (as is a "@P 0", for a build that says so): the track is dropped and
loaded again with LP for the NEXT run, never jumped, and THIS run stays
silent - the `_eof` guard keeps the run key, so nothing sends a P every
tick against a file that has run out. A P of ours excuses only the FIRST
wanted line after it: when the track ends in the same instant as the
show-end pause the burst is "@P 1" (EOF), "@P 2", "@P 1" (our P against
the ended file), and the last line is the EOF nobody asked for.

The unpause latency. Between writing "P" on the pipe and the first sample
leaving the speaker there is the pipe, mpg123's command loop and the
ALSA buffer. The command half is MEASURED once at load: with the volume
at 0 the track is unpaused and paused LATENCY_SAMPLES times and the
round trip from "P" to mpg123's "@P 2" is timed. Three samples and the
median, because the first command after a load is regularly slow (the
decoder is still filling) and one such outlier must not set the lead for
the whole day; bounded to LATENCY_MAX_S. The output half (the ALSA buffer
past the reply) cannot be measured from here: DEVICE_LATENCY_S is a GUESS
of it, to be measured once with a click track against the panels and set
with `--speaker-lead-ms`. After a JUMP the same latency is added to the
position aimed at.

Nothing here ever blocks an HTTP thread: the server only reads status(),
which takes a lock no command ever waits under. A host with no mpg123 (or
one that dies) is a status, not a failure - the run proceeds silently, the
page says so (/api/fleet's `speaker`), and the speaker tries again every
RETRY_S in case somebody installs it. The process factory is injectable so
the tests drive a fake mpg123.

The Bluetooth speaker's CONNECTION (--speaker-output pulse, the Bose over
A2DP). Nothing used to watch it: a Bose that dropped (power, distance, a
phone grabbing it) left the show running in silence with nothing on the
page or the LCD to say so. A third thread of its own (never the music
thread, never an HTTP thread) now reads `bluetoothctl info <MAC>` and
`pactl list short sinks` every CONNECTION_CHECK_S (CONNECTION_CHECK_RUN_S
while a run is active) and publishes the device and one word for the
state - connected | disconnected | connecting | pairing | no_device. The
MAC is fleet.json's "speaker_mac" or, when that is absent, the first
`bluetoothctl paired-devices` entry whose info carries `UUID: Audio
Sink`. Disconnected, it runs `bluetoothctl connect` RECONNECT_FIRST_S
later, then every RECONNECT_EVERY_S, backing off to RECONNECT_BACKOFF_S
after RECONNECT_BACKOFF_AFTER refusals in a row (bluetoothd's "Permission
denied (13)" - the speaker is attached to another source or has dropped
our pairing; bluetoothctl itself only says `org.bluez.Error.Failed`, so a
speaker that is simply off reads the same here). Connected again: the
bluez sink is made the default and every sink-input (mpg123's stream,
which PulseAudio's module-rescue-streams had moved to the fallback sink
when the bluez one vanished) is moved back onto it, the volume thread is
poked so the AVRCP volume lands on the new transport, and one line says
`Bose reconnected after N s`. The page's Connect button asks for an
attempt now; Re-pair runs the whole cure in ONE interactive bluetoothctl
session (remove, scan on, wait for the classic address - it appears only
while the speaker is in pairing mode -, pair, trust, connect, quit; BlueZ
forgets an unpaired scan result ~30 s after it was seen, hence the one
session), with its progress in `pairing`. Both run on the connection
thread; an HTTP thread only queues the request.
"""

from __future__ import annotations

import re
import statistics
import subprocess
import threading
import time
from pathlib import Path

from .fleet import pc_clock

MPG123 = "mpg123"
# The host's loudness (EXHIBITION mode, measured on radxa-05 with the Bose
# SoundLink Flex, 2026-09-30): the speaker's own +/- buttons do nothing for
# us - no AVRCP notification, no key event, the speaker defers to the
# source - so loudness is entirely the source's AVRCP absolute volume, the
# bluez MediaTransport1 `Volume` (0-127; 47 was quiet, 90 comfortable, 127
# loud), set with busctl as user radxa. Its object path's fdN changes on
# every reconnect, so it is looked up each time. A plain USB speaker is the
# PulseAudio default sink's own volume instead. Stored per host in
# fleet.json ("speaker_volume", 0-100, VOLUME_DEFAULT), applied at startup,
# whenever the sink or transport (re)appears, and on every change.
VOLUME_DEFAULT = 70
VOLUME_CHECK_S = 5.0
VOLUME_APPLY_TIMEOUT_S = 1.5    # how long a POST waits for the thread to apply
_TRANSPORT_RE = r"/org/bluez/hci[0-9]+/dev_{mac}/sep[0-9]+/fd[0-9]+"
# The Bluetooth speaker's connection (see the module doc).
CONNECTION_CHECK_S = 10.0       # bluetoothctl info + pactl list, idle
CONNECTION_CHECK_RUN_S = 2.0    # ...while a run is active
RECONNECT_FIRST_S = 5.0         # a drop is given this long to come back by itself
RECONNECT_EVERY_S = 30.0
RECONNECT_BACKOFF_S = 120.0
RECONNECT_BACKOFF_AFTER = 3     # refusals in a row before the long interval
CONNECT_TIMEOUT_S = 20.0        # bluetoothctl connect has this long to answer
PAIR_SCAN_S = 60.0              # the speaker has this long to show up in a scan
PAIR_POLL_S = 2.0               # how often the scan is looked at
PAIR_WAIT_S = 15.0              # `pair` has this long (measured ~8 s on the Bose)
PAIR_CONNECT_WAIT_S = 15.0
PAIRING_SHOWN_S = 60.0          # a finished re-pair stays in `pairing` this long
CONNECTION_STATES = ("connected", "disconnected", "connecting", "pairing", "no_device")
# bluetoothctl's words for a connect the speaker (or bluetoothd) refused -
# "Permission denied (13)" is bluetoothd's own reason, seen in its journal;
# bluetoothctl itself answers org.bluez.Error.Failed for it.
_REFUSED_MARKS = ("Permission denied", "org.bluez.Error.Failed",
                  "org.bluez.Error.NotAvailable")
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][A-Z0-9]|\r")
_PROMPT_RE = re.compile(r"^(?:\[[^\]\n]*\]#\s*)+", re.M)   # "[bluetooth]# ", "[Bose Flex]# "
_MAC_RE = re.compile(r"^(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")


def plain_lines(text: str) -> "list[str]":
    """bluetoothctl's output without its colours and prompts, one line at
    a time, blanks dropped."""
    lines = []
    for raw in _ANSI_RE.sub("", text).splitlines():
        line = _PROMPT_RE.sub("", raw).strip()
        if line:
            lines.append(line)
    return lines


def default_runner(argv, timeout: float = 5.0):
    """Run a tool, answer (exit code, stdout); a missing tool is exit 127."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return 127, ""
    except subprocess.SubprocessError as exc:
        return 1, str(exc)
    return done.returncode, done.stdout


def default_session_factory(argv):
    """An interactive bluetoothctl on two pipes (the re-pair flow)."""
    return subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)


def clean_mac(value) -> "str | None":
    """A Bluetooth address as bluetoothctl prints it (upper case, colons),
    or None for anything that is not one."""
    if not isinstance(value, str):
        return None
    text = value.strip().replace("-", ":")
    return text.upper() if _MAC_RE.match(text) else None


def bluez_sink_name(mac: str) -> str:
    """PulseAudio's name for the A2DP sink of a device."""
    return f"bluez_sink.{mac.replace(':', '_')}.a2dp_sink"


class _BtSession:
    """One interactive bluetoothctl: commands on stdin, its lines (ANSI
    stripped) collected by a reader thread, waited for by substring."""

    def __init__(self, proc):
        self.proc = proc
        self.lines: "list[str]" = []
        self.closed = False
        self._cond = threading.Condition()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        try:
            for raw in iter(self.proc.stdout.readline, b""):
                for line in plain_lines(raw.decode("utf-8", "replace")):
                    with self._cond:
                        self.lines.append(line)
                        del self.lines[:-200]
                        self._cond.notify_all()
        except (OSError, ValueError):
            pass
        with self._cond:
            self.closed = True
            self._cond.notify_all()

    def send(self, text: str) -> None:
        try:
            self.proc.stdin.write((text + "\n").encode("utf-8"))
            self.proc.stdin.flush()
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"bluetoothctl pipe: {exc}")

    def mark(self) -> int:
        with self._cond:
            return len(self.lines)

    def wait_for(self, marks, timeout: float, since: int = 0,
                 stop: "threading.Event | None" = None) -> "str | None":
        """The first line after `since` containing one of `marks`, or None
        after `timeout` (or once `stop` is set / the process is gone)."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                for line in self.lines[since:]:
                    if any(mark in line for mark in marks):
                        return line
                since = len(self.lines)
                left = deadline - time.monotonic()
                if left <= 0 or self.closed or (stop is not None and stop.is_set()):
                    return None
                self._cond.wait(min(left, 0.25))

    def close(self) -> None:
        try:
            self.send("quit")
        except RuntimeError:
            pass
        try:
            self.proc.wait(timeout=2)
        except Exception:                   # noqa: BLE001 - then it is killed
            try:
                self.proc.terminate()
            except OSError:
                pass
TICK_S = 0.05              # how often the run is looked at between events
LOAD_TIMEOUT_S = 5.0       # mpg123 has this long to report a loaded track
REPLY_TIMEOUT_S = 2.0      # ...and to answer any other command
LATENCY_SAMPLES = 3
LATENCY_MAX_S = 0.5        # a round trip longer than this is not latency
DEVICE_LATENCY_S = 0.05    # the ALSA buffer past mpg123's reply (a guess)
RETRY_S = 30.0             # mpg123 missing or dead: try again this often
TRACK_CHECK_S = 1.0        # how often the music file itself is looked at

STOPPED, PAUSED, PLAYING = 0, 1, 2      # mpg123's "@P n"


def default_factory(argv):
    """The real thing: mpg123 on two pipes, stderr dropped (its banner)."""
    return subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)


class Speaker:
    """One mpg123 following the fleet's run. `track()` answers the path of
    the show's music (or None); `run()` answers (run dict or None, the
    show's length) - Fleet.run_snapshot(). `output` names mpg123's -o
    module ("alsa" under systemd, where there is no PulseAudio session)."""

    def __init__(self, track, run, clock=pc_clock, factory=None,
                 binary: str = MPG123, extra_lead_s: float = DEVICE_LATENCY_S,
                 output: "str | None" = None,
                 tick_s: float = TICK_S, retry_s: float = RETRY_S,
                 track_check_s: float = TRACK_CHECK_S,
                 runner=None, volume: int = VOLUME_DEFAULT, save_volume=None,
                 volume_check_s: float = VOLUME_CHECK_S,
                 speaker_mac=None, bluetooth: "bool | None" = None,
                 session_factory=None,
                 connection_check_s: float = CONNECTION_CHECK_S,
                 connection_check_run_s: float = CONNECTION_CHECK_RUN_S,
                 reconnect_first_s: float = RECONNECT_FIRST_S,
                 reconnect_every_s: float = RECONNECT_EVERY_S,
                 reconnect_backoff_s: float = RECONNECT_BACKOFF_S,
                 connect_timeout_s: float = CONNECT_TIMEOUT_S,
                 pair_scan_s: float = PAIR_SCAN_S, pair_poll_s: float = PAIR_POLL_S,
                 pair_wait_s: float = PAIR_WAIT_S,
                 pair_connect_wait_s: float = PAIR_CONNECT_WAIT_S,
                 pairing_shown_s: float = PAIRING_SHOWN_S):
        self._track = track
        self._run = run
        self._clock = clock
        self._factory = factory or default_factory
        self._binary = binary
        self._output = output
        # The Bluetooth speaker's connection (see the module doc): watched
        # and reconnected only where the sound goes through PulseAudio
        # (`bluetooth` None = "when output is pulse"; a USB speaker on alsa
        # has no connection to watch). `speaker_mac` is fleet.json's, or
        # None to find the paired Audio Sink. `session_factory(argv)` opens
        # the interactive bluetoothctl the re-pair flow drives.
        self._bluetooth = (output == "pulse") if bluetooth is None else bool(bluetooth)
        self._mac_configured = clean_mac(speaker_mac)
        self._session_factory = session_factory or default_session_factory
        self._conn_check_s = connection_check_s
        self._conn_check_run_s = connection_check_run_s
        self._reconnect_first_s = reconnect_first_s
        self._reconnect_every_s = reconnect_every_s
        self._reconnect_backoff_s = reconnect_backoff_s
        self._connect_timeout_s = connect_timeout_s
        self._pair_scan_s = pair_scan_s
        self._pair_poll_s = pair_poll_s
        self._pair_wait_s = pair_wait_s
        self._pair_connect_wait_s = pair_connect_wait_s
        self._pairing_shown_s = pairing_shown_s
        self._pairing_ended: "float | None" = None  # this clock, when done / failed
        self._conn_thread: "threading.Thread | None" = None
        self._conn_wake = threading.Event()
        self._conn_mac: "str | None" = self._mac_configured
        self._mac_hint: "str | None" = None     # the last MAC a re-pair was asked for
        self._conn_due = -1e9               # the next check, this clock
        self._conn_requests: "list[tuple]" = []    # ("connect",) / ("pair", mac)
        self._device: "dict | None" = None
        self._connection = "no_device"
        self._reconnect_at: "float | None" = None   # the next auto connect
        self._reconnect_attempts = 0
        self._reconnect_error: "str | None" = None
        self._refused = 0                   # refusals in a row (the backoff)
        self._disconnected_since: "float | None" = None
        self._routed_sink: "str | None" = None      # the bluez sink last routed to
        self._pairing: "dict | None" = None
        self._conn_said: "str | None" = None
        # The host's loudness (see VOLUME_DEFAULT): `runner(argv)` runs
        # pactl / busctl, `save_volume(v)` persists a change (fleet.json).
        self._runner = runner or default_runner
        self._save_volume = save_volume
        self._volume = self._clamp_volume(volume)
        self._volume_check_s = volume_check_s
        self._volume_checked = -1e9
        self._volume_dirty = True           # apply at startup
        self._volume_key = None             # (sink, transport path) last applied to
        self._applied: "str | None" = None  # "bluez" | "pulse" | None
        self.volume_error: "str | None" = None
        self._volume_gen = 0                # bumped by every apply attempt
        self._volume_seq = 0                # bumped by every set_volume()
        self._volume_applied_seq = 0        # the last seq a tick applied whole
        self._volume_done = threading.Condition()
        self._volume_wake = threading.Event()
        self._volume_thread: "threading.Thread | None" = None
        self._volume_said_key = None        # the sink the log last named
        self._extra_lead = max(0.0, float(extra_lead_s))
        self._tick_s = tick_s
        self._retry_s = retry_s
        self._track_check_s = track_check_s
        self._stop = threading.Event()
        self._thread: "threading.Thread | None" = None
        # Two locks on purpose: `_lock` guards what status() reads and is
        # never held while waiting for mpg123; `_reply` is the reader
        # thread's condition, which a command waits under.
        self._lock = threading.Lock()
        self._reply = threading.Condition()
        self._proc = None
        # mpg123's play state as IT last said it ("@P n"), and how many @P
        # lines have arrived - a command waits for a line newer than the
        # count it saw when it was sent. Never reset to a guess.
        self._pstate = STOPPED
        self._pseq = 0
        self._error_line: "str | None" = None
        self._error_seq = 0
        # Whether a command of ours is waiting for its @P right now (so the
        # reader can tell an answer from a line mpg123 volunteered), and the
        # reader's verdict "the file ran out" (see the module doc).
        self._awaiting = 0
        self._await_want = None
        self._answered = False            # the in-flight command got its line
        self._phist: "list[tuple[int, int]]" = []    # the last few (seq, state)
        self._eof_seen = False
        self._frames_left: "int | None" = None
        self._next_try = 0.0
        # What is known about the track and the run being followed.
        self._loaded: "tuple[str, int] | None" = None    # (path, mtime_ns)
        self._latency = 0.0
        self._key = None                  # (t0, state, held_at) last acted on
        self._due: "float | None" = None  # when to unpause, this PC's clock
        self._eof = False                 # the track ran out under this run
        self._track_checked = -1e9
        self.state = "idle"    # idle | loaded | armed | playing | paused | ended
        self.error: "str | None" = None
        self.log: "list[str]" = []

    # ---- lifecycle ----

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self._volume_thread = threading.Thread(target=self._volume_loop, daemon=True)
        self._volume_thread.start()
        if self._bluetooth:
            self._conn_thread = threading.Thread(target=self._connection_loop, daemon=True)
            self._conn_thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._volume_wake.set()
        self._conn_wake.set()
        for thread in (self._thread, self._volume_thread, self._conn_thread):
            if thread is not None:
                thread.join(timeout=3)
        self._quit()

    def status(self) -> dict:
        """What /api/fleet reports as `speaker` (any thread may call).
        The Bluetooth half is the API contract the page and the unit LCD
        read: `bluetooth` (true only when the sound goes through
        PulseAudio AND a Bluetooth speaker is known - fleet.json's
        speaker_mac, a paired Audio Sink, or a bluez default sink; false
        for a wired speaker, so the LCD shows no LOST banner for a device
        that was never there), `device` {mac, name, paired, trusted,
        connected, sink_present, last_connected_at (epoch seconds),
        last_error} or null, `connection` (CONNECTION_STATES),
        `reconnect` {attempts, next_in_s, last_error}, `pairing` {phase,
        note, started_at} or null (null again PAIRING_SHOWN_S after it
        ended)."""
        now = self._clock()
        with self._lock:
            loaded = self._loaded
            bluetooth = self._bluetooth and (
                self._conn_mac is not None or self._device is not None
                or bool(self._volume_key and str(self._volume_key[0]).startswith("bluez_sink.")))
            next_in = None
            if (bluetooth and self._connection == "disconnected"
                    and self._reconnect_at is not None):
                next_in = round(max(0.0, self._reconnect_at - now), 1)
            return {"available": self._proc is not None, "error": self.error,
                    "state": self.state,
                    "track": Path(loaded[0]).name if loaded else None,
                    "latency_ms": round(self._latency * 1000, 1),
                    "playing": self._pstate == PLAYING, "log": list(self.log[-5:]),
                    "volume": self._volume, "applied": self._applied,
                    "volume_error": self.volume_error,
                    "bluetooth": bluetooth,
                    "device": dict(self._device) if self._device else None,
                    "connection": self._connection,
                    "reconnect": {"attempts": self._reconnect_attempts,
                                  "next_in_s": next_in,
                                  "last_error": self._reconnect_error},
                    "pairing": dict(self._pairing) if self._pairing else None}

    @property
    def playing(self) -> bool:
        with self._reply:
            return self._pstate == PLAYING

    # ---- the host's loudness ----

    @staticmethod
    def _clamp_volume(value) -> int:
        try:
            return max(0, min(100, int(round(float(value)))))
        except (TypeError, ValueError, OverflowError):     # junk, NaN, inf
            return VOLUME_DEFAULT

    def set_volume(self, value) -> dict:
        """A new loudness (0-100): stored, persisted, and applied by the
        volume thread at once - this waits for THAT apply (the one made
        for this value's sequence number, bounded by VOLUME_APPLY_TIMEOUT_S,
        never on a subprocess itself) and answers {"volume", "applied",
        "error"}. A save that cannot parse fleet.json raises (the caller's
        500): the file is left as it is."""
        volume = self._clamp_volume(value)
        with self._volume_done:
            self._volume_seq += 1
            seq = self._volume_seq
            with self._lock:
                self._volume = volume
            self._volume_dirty = True
        self._volume_wake.set()
        if self._save_volume is not None:
            try:
                self._save_volume(volume)
            except OSError as exc:
                self._fail(f"could not save the volume: {exc}")
        with self._volume_done:
            self._volume_done.wait_for(lambda: self._volume_applied_seq >= seq,
                                       VOLUME_APPLY_TIMEOUT_S)
        status = self.status()
        return {"volume": status["volume"], "applied": status["applied"],
                "error": status["volume_error"]}

    def _volume_loop(self) -> None:
        """The volume's OWN thread: pactl / busctl (up to four tools, each
        with a 5 s timeout) must never sit between the music thread and
        the 0:00 unpause or a JUMP."""
        while not self._stop.is_set():
            try:
                self._volume_tick(self._clock())
            except Exception as exc:        # noqa: BLE001 - said, never fatal
                self._fail(f"volume: {exc.__class__.__name__}: {exc}")
            self._volume_wake.wait(min(self._volume_check_s, 0.25))
            self._volume_wake.clear()

    def _volume_tick(self, now: float) -> None:
        """Every VOLUME_CHECK_S - and at once after a change: where the
        sound goes right now, and the volume applied there if that changed
        or the volume did. Cheap - one `pactl info`, plus one `busctl tree`
        for a Bluetooth sink. A failure is retried on the SAME interval,
        never in a hot loop (PulseAudio is down for a while every boot).
        The volume is read with its sequence number: a change that lands
        while this applies is applied again by the next tick, never
        mistaken for done."""
        with self._volume_done:
            dirty = self._volume_dirty
            seq = self._volume_seq
            with self._lock:
                volume = self._volume
        if not dirty and now - self._volume_checked < self._volume_check_s:
            return
        self._volume_checked = now
        applied, key, error = None, None, None
        try:
            sink = self._default_sink()
            if sink is None:
                error = "no PulseAudio default sink"
            elif sink.startswith("bluez_sink."):
                mac = sink.split(".")[1]
                path = self._transport_path(mac)
                key = (sink, path)
                if path is None:
                    error = f"bluez transport for {mac} not found (speaker connected?)"
                elif key != self._volume_key or dirty:
                    # The AVRCP absolute volume, 0-127, and the pulse sink
                    # pinned at 100 % so nothing scales it a second time.
                    code, out = self._runner(["busctl", "--system", "set-property",
                                              "org.bluez", path,
                                              "org.bluez.MediaTransport1", "Volume",
                                              "q", str(int(round(volume * 127 / 100)))])
                    if code != 0:
                        raise RuntimeError(f"busctl set-property: exit {code} {out.strip()}")
                    code, out = self._runner(["pactl", "set-sink-volume", sink, "100%"])
                    if code != 0:
                        raise RuntimeError(f"pactl set-sink-volume: exit {code} {out.strip()}")
                    applied = "bluez"
                else:
                    applied = self._applied
            else:
                key = (sink, None)
                if key != self._volume_key or dirty:
                    code, out = self._runner(["pactl", "set-sink-volume", "@DEFAULT_SINK@",
                                              f"{volume}%"])
                    if code != 0:
                        raise RuntimeError(f"pactl set-sink-volume: exit {code} {out.strip()}")
                    applied = "pulse"
                else:
                    applied = self._applied
        except (RuntimeError, OSError, IndexError) as exc:
            error = str(exc)
        with self._volume_done:
            with self._lock:
                self._applied = applied
                said = self.volume_error == error
                self.volume_error = error
            # A failure: the key is forgotten, so the next INTERVAL applies
            # again - dirty stays off, or this would spin on pactl.
            self._volume_key = key if error is None else None
            if self._volume_seq == seq:
                # Nothing changed under us: this value is done (or failed
                # and will be retried). A change that landed meanwhile
                # keeps dirty, and the next tick applies the new value.
                self._volume_dirty = False
                self._volume_applied_seq = seq
            self._volume_gen += 1
            self._volume_done.notify_all()
        if error and not said:
            self._say(f"volume: {error}")
        elif not error and key != self._volume_said_key:
            self._volume_said_key = key
            self._say(f"volume {volume} applied via {applied} ({key[0]})")

    def _default_sink(self) -> "str | None":
        code, out = self._runner(["pactl", "info"])
        if code != 0:
            raise RuntimeError(f"pactl info: exit {code} {out.strip()[:80]}")
        for line in out.splitlines():
            if line.startswith("Default Sink:"):
                sink = line.partition(":")[2].strip()
                return sink or None
        return None

    def _transport_path(self, mac: str) -> "str | None":
        code, out = self._runner(["busctl", "--system", "tree", "org.bluez"])
        if code != 0:
            raise RuntimeError(f"busctl tree: exit {code} {out.strip()[:80]}")
        found = re.findall(_TRANSPORT_RE.format(mac=re.escape(mac)), out)
        return found[-1] if found else None

    # ---- the Bluetooth speaker's connection ----

    def request_connect(self) -> "tuple[int, dict]":
        """POST /api/speaker/connect: a connect attempt now, on the
        connection thread. Answers (HTTP status, body) - 409 while a
        re-pair runs, 400 with nothing to connect to."""
        with self._lock:
            if not self._bluetooth:
                return 400, self._answer(False, "no Bluetooth speaker on this Conductor "
                                                "(serve --speaker-output pulse)")
            if self._connection == "pairing":
                return 409, self._answer(False, "re-pairing is in progress - wait for it")
            if self._conn_mac is None:
                return 400, self._answer(False, "no paired Bluetooth speaker known - "
                                                "Re-pair (with the MAC), or fleet.json "
                                                "\"speaker_mac\"")
            if self._connection != "connecting":
                self._connection = "connecting"
                self._conn_requests.append(("connect",))
            answer = self._answer(True, None)
        self._conn_wake.set()
        return 200, answer

    def request_pair(self, mac=None, force: bool = False) -> "tuple[int, dict]":
        """POST /api/speaker/pair: the whole cure (remove, scan, pair,
        trust, connect) on the connection thread. 409 while one runs, 409
        during a run unless `force` (pairing drops the audio), 400 with no
        MAC (none given, none known)."""
        if mac is not None and clean_mac(mac) is None:
            return 400, self._answer(False, f"mac: not a Bluetooth address ({mac!r})")
        run, _ = self._run()
        with self._lock:
            if not self._bluetooth:
                return 400, self._answer(False, "no Bluetooth speaker on this Conductor "
                                                "(serve --speaker-output pulse)")
            if self._connection == "pairing" or any(r[0] == "pair" for r in self._conn_requests):
                return 409, self._answer(False, "re-pairing is already in progress")
            if run is not None and not force:
                return 409, self._answer(False, "show running - pairing drops the audio; "
                                                "send {\"force\": true} to pair anyway")
            target = clean_mac(mac) or self._conn_mac or self._mac_hint
            if target is None:
                return 400, self._answer(False, "no Bluetooth speaker known - give its "
                                                "MAC (bluetoothctl devices while it is "
                                                "in pairing mode)")
            self._mac_hint = target
            self._connection = "pairing"
            self._pairing = {"phase": "scanning", "note": "starting bluetoothctl",
                             "started_at": time.time()}
            self._pairing_ended = None
            self._conn_requests.append(("pair", target))
            answer = self._answer(True, None)
        self._conn_wake.set()
        return 200, answer

    def _answer(self, ok: bool, error: "str | None") -> dict:
        # Under self._lock (the caller's).
        return {"ok": ok, "connection": self._connection, "error": error}

    def _connection_loop(self) -> None:
        """The connection's OWN thread: bluetoothctl / pactl (seconds each,
        a re-pair a minute) never sit between the music thread and the
        0:00 unpause, and never under an HTTP request."""
        while not self._stop.is_set():
            try:
                wait = self._connection_tick(self._clock())
            except Exception as exc:        # noqa: BLE001 - said, never fatal
                self._connection_fail(f"{exc.__class__.__name__}: {exc}")
                wait = self._conn_check_s
            self._conn_wake.wait(max(0.001, min(wait, 0.25)))
            self._conn_wake.clear()

    def _connection_tick(self, now: float) -> float:
        """A queued request first (Connect / Re-pair), then the check when
        it is due, then the automatic connect when it is due. Returns how
        long the loop may sleep."""
        with self._lock:
            request = self._conn_requests.pop(0) if self._conn_requests else None
            if (self._pairing is not None and self._pairing_ended is not None
                    and now - self._pairing_ended >= self._pairing_shown_s):
                self._pairing, self._pairing_ended = None, None     # old news
        if request is not None:
            if request[0] == "pair":
                self._pair(request[1])
            else:
                self._connect(now, manual=True)
            self._conn_due = now
            return 0.0
        if now >= self._conn_due:
            run, _ = self._run()
            self._refresh(now)
            interval = self._conn_check_run_s if run is not None else self._conn_check_s
            with self._lock:
                connected_no_sink = (self._connection == "connected" and self._device
                                     and not self._device["sink_present"])
            if connected_no_sink:
                # PulseAudio takes a moment to make the sink: look again soon.
                interval = min(interval, self._conn_check_run_s)
            self._conn_due = now + interval
        with self._lock:
            due = self._reconnect_at if self._connection == "disconnected" else None
        if due is not None and now >= due:
            self._connect(now, manual=False)
            self._conn_due = now                # the check that follows says connected
            return 0.0
        waits = [self._conn_due - now] + ([due - now] if due is not None else [])
        return max(0.0, min(waits))

    def _refresh(self, now: float) -> None:
        """One look: the device (bluetoothctl info) and PulseAudio's sinks,
        published as `device` and `connection`; a drop arms the automatic
        connect, a return routes the sound back (see _route)."""
        mac = self._conn_mac
        if mac is None:
            mac = self._discover()
            if mac is None:
                # Nothing paired. The address a re-pair was last asked for
                # (and failed) is still shown, so Re-pair needs no retyping.
                with self._lock:
                    self._conn_mac = None
                    hint = self._mac_hint
                if hint is None:
                    with self._lock:
                        self._device = None
                        self._set_connection("no_device")
                        self._conn_said = None
                    return
                mac = hint
            else:
                with self._lock:
                    self._conn_mac = mac
        info = self._info(mac)
        sinks = self._sinks()
        sink = bluez_sink_name(mac)
        present = sink in sinks
        with self._lock:
            self._conn_said = None          # the tools answer again: a later failure is news
        if info is None:
            # Configured, but BlueZ does not know it: not paired (any more).
            with self._lock:
                previous = self._device or {}
                self._device = {"mac": mac, "name": previous.get("name"),
                                "paired": False, "trusted": False, "connected": False,
                                "sink_present": present,
                                "last_connected_at": previous.get("last_connected_at"),
                                "last_error": f"bluetoothctl does not know {mac} - "
                                              "not paired: Re-pair"}
                self._routed_sink = None
                if self._mac_configured is None:
                    self._conn_mac = None         # discovered: look again
                self._set_connection("no_device")
            return
        connected = info["connected"]
        said = None
        with self._lock:
            previous = self._device or {}
            self._device = {"mac": mac, "name": info["name"] or previous.get("name"),
                            "paired": info["paired"], "trusted": info["trusted"],
                            "connected": connected, "sink_present": present,
                            "last_connected_at": (time.time() if connected
                                                  else previous.get("last_connected_at")),
                            "last_error": None}
            # No attempt is ever in flight here: connects and re-pairs run
            # on THIS thread, before a check - so what bluetoothctl says
            # now is the word, "connecting" / "pairing" included.
            if connected:
                if self._disconnected_since is not None:
                    # Came back by itself (Trusted) or by our connect.
                    said = (f"{info['name'] or 'speaker'} reconnected after "
                            f"{now - self._disconnected_since:.0f} s")
                self._disconnected_since = None
                self._reconnect_at = None
                self._reconnect_attempts = 0
                self._refused = 0
                self._set_connection("connected")
                route = present and self._routed_sink != sink
            else:
                if self._disconnected_since is None:
                    self._disconnected_since = now
                    self._reconnect_attempts = 0
                    self._refused = 0
                    said = f"{info['name'] or mac} disconnected"
                if self._reconnect_at is None:
                    self._reconnect_at = now + self._reconnect_first_s
                self._routed_sink = None
                self._set_connection("disconnected")
                route = False
        if said:
            self._say(said)
        if route:
            self._route(sink, sinks)

    def _discover(self) -> "str | None":
        """No "speaker_mac": the first paired device that is an A2DP sink."""
        code, out = self._runner(["bluetoothctl", "paired-devices"])
        if code != 0:
            raise RuntimeError(f"bluetoothctl paired-devices: exit {code} "
                               f"{out.strip()[:80]}")
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == "Device" and clean_mac(parts[1]):
                mac = clean_mac(parts[1])
                info = self._info(mac)
                if info is not None and info["audio_sink"]:
                    self._say(f"Bluetooth speaker {info['name'] or mac} ({mac}) found "
                              "among the paired devices")
                    return mac
        return None

    def _info(self, mac: str) -> "dict | None":
        """`bluetoothctl info <MAC>` read into a dict; None when BlueZ does
        not know the device ("Device <MAC> not available")."""
        code, out = self._runner(["bluetoothctl", "info", mac])
        if "not available" in out:
            return None
        if code != 0:
            raise RuntimeError(f"bluetoothctl info: exit {code} {out.strip()[:80]}")
        found = {"name": None, "paired": False, "trusted": False, "connected": False,
                 "audio_sink": False}
        for raw in out.splitlines():
            line = raw.strip()
            key, _, value = line.partition(":")
            key, value = key.strip(), value.strip()
            if key == "Name":
                found["name"] = value or None
            elif key in ("Paired", "Trusted", "Connected"):
                found[key.lower()] = value == "yes"
            elif key == "UUID" and value.startswith("Audio Sink"):
                found["audio_sink"] = True
        return found

    def _sinks(self) -> "dict[str, str]":
        """`pactl list short sinks`: {name: index}. PulseAudio down is an
        empty dict - the connection is still bluetoothctl's word."""
        code, out = self._runner(["pactl", "list", "short", "sinks"])
        if code != 0:
            return {}
        sinks = {}
        for line in out.splitlines():
            parts = line.split("\t") if "\t" in line else line.split()
            if len(parts) >= 2:
                sinks[parts[1]] = parts[0]
        return sinks

    def _route(self, sink: str, sinks: "dict[str, str]") -> None:
        """The bluez sink is back: make it the default, move every stream
        onto it (module-rescue-streams had moved mpg123's to the fallback
        sink when it vanished - on this headless host mpg123 is the only
        stream there is), and wake the volume thread to apply the AVRCP
        volume to the new transport. A failure is said, and tried again
        on the next check (the routed sink is only remembered on success)."""
        try:
            code, out = self._runner(["pactl", "info"])
            if code != 0:
                raise RuntimeError(f"pactl info: exit {code} {out.strip()[:80]}")
            default = None
            for line in out.splitlines():
                if line.startswith("Default Sink:"):
                    default = line.partition(":")[2].strip()
            if default != sink:
                code, out = self._runner(["pactl", "set-default-sink", sink])
                if code != 0:
                    raise RuntimeError(f"pactl set-default-sink: exit {code} {out.strip()[:80]}")
            code, out = self._runner(["pactl", "list", "short", "sink-inputs"])
            if code != 0:
                raise RuntimeError(f"pactl list sink-inputs: exit {code} {out.strip()[:80]}")
            moved = 0
            for line in out.splitlines():
                parts = line.split("\t") if "\t" in line else line.split()
                if len(parts) >= 2 and parts[1] != sinks.get(sink):
                    code, out = self._runner(["pactl", "move-sink-input", parts[0], sink])
                    if code != 0:
                        raise RuntimeError(f"pactl move-sink-input {parts[0]}: exit {code} "
                                           f"{out.strip()[:80]}")
                    moved += 1
        except (RuntimeError, OSError) as exc:
            with self._lock:
                if self._device:
                    self._device["last_error"] = f"route to {sink}: {exc}"
            self._say(f"could not route the sound to {sink}: {exc}")
            return
        with self._lock:
            self._routed_sink = sink
        # The AVRCP volume goes to the transport's new fdN: now, not in
        # VOLUME_CHECK_S. (_volume_done on its own - the volume thread takes
        # it before _lock, never the other way round.)
        with self._volume_done:
            self._volume_dirty = True
        self._volume_wake.set()
        self._say(f"sound routed to {sink}" + (f", {moved} stream(s) moved" if moved else ""))

    def _connect(self, now: float, manual: bool) -> None:
        """One `bluetoothctl connect`. Success: the next check finds it
        connected and routes the sound. Failure: said, counted, and the
        next automatic attempt set - RECONNECT_EVERY_S, or the backoff
        after RECONNECT_BACKOFF_AFTER refusals in a row."""
        with self._lock:
            mac = self._conn_mac
            if mac is None:
                self._set_connection("no_device")
                return
            self._set_connection("connecting")
        code, out = self._runner(["bluetoothctl", "connect", mac],
                                 timeout=self._connect_timeout_s)
        lines = plain_lines(out)
        ok = code == 0 and any("Connection successful" in line for line in lines)
        error = None
        if not ok:
            error = lines[-1] if lines else f"bluetoothctl connect: exit {code}"
            if code == 127:
                error = "bluetoothctl not found"
        with self._lock:
            self._reconnect_attempts += 1
            self._reconnect_error = error
            if ok:
                self._refused = 0
                self._reconnect_at = None
                # `connected` is published by the check that follows at
                # once (and it routes the sound); until then "connecting".
                return
            refused = any(mark in error for mark in _REFUSED_MARKS)
            self._refused = self._refused + 1 if refused else 0
            wait = (self._reconnect_backoff_s
                    if self._refused >= RECONNECT_BACKOFF_AFTER
                    else self._reconnect_every_s)
            self._reconnect_at = now + wait
            self._set_connection("disconnected")
            if self._disconnected_since is None:
                self._disconnected_since = now
        why = ""
        if any(mark in error for mark in _REFUSED_MARKS):
            why = " (the speaker is off, out of range, on another phone, or has dropped this pairing)"
        self._say(f"connect {mac}{' (manual)' if manual else ''} failed: {error}{why}"
                  f" - next try in {wait:.0f} s")

    def _pair(self, mac: str) -> None:
        """The cure for a speaker that refuses us: remove, scan on, wait
        for the classic address (pairing mode only), pair, trust, connect
        - in ONE bluetoothctl session (see the module doc). Progress in
        `pairing`; the connection word is "pairing" throughout."""
        started = time.monotonic()

        def phase(name, note):
            ended = name in ("done", "failed")
            with self._lock:
                self._pairing = {"phase": name, "note": note,
                                 "started_at": self._pairing["started_at"]
                                 if self._pairing else time.time()}
                self._pairing_ended = self._clock() if ended else None
                if not ended:
                    self._set_connection("pairing")
            self._say(f"re-pair {mac}: {name} - {note}")

        session = None
        try:
            phase("scanning", "forgetting the old pairing, scanning - put the speaker "
                              "in pairing mode")
            session = _BtSession(self._session_factory(["bluetoothctl"]))
            session.send(f"remove {mac}")
            session.wait_for(("removed", "not available"), 3.0, stop=self._stop)
            since = session.mark()
            session.send("scan on")
            deadline = time.monotonic() + self._pair_scan_s
            seen = False
            while time.monotonic() < deadline and not self._stop.is_set():
                # "[NEW] Device <MAC> <name>" in the session (a "[DEL]" from
                # the remove above also names it - hence NEW / CHG only),
                # or the address in `bluetoothctl devices`.
                if session.wait_for((f"[NEW] Device {mac}", f"[CHG] Device {mac}"),
                                    self._pair_poll_s, since=since, stop=self._stop):
                    seen = True
                    break
                code, out = self._runner(["bluetoothctl", "devices"])
                if code == 0 and f"Device {mac}" in _ANSI_RE.sub("", out):
                    seen = True
                    break
            if not seen:
                raise RuntimeError(f"{mac} did not appear in {self._pair_scan_s:.0f} s of "
                                   "scanning - is the speaker in pairing mode (and every "
                                   "phone's Bluetooth off)?")
            phase("pairing", f"{mac} found - pairing")
            since = session.mark()
            session.send(f"pair {mac}")
            line = session.wait_for(("Pairing successful", "Failed to pair",
                                     "not available", "AuthenticationFailed",
                                     "AlreadyExists"),
                                    self._pair_wait_s, since=since, stop=self._stop)
            if line is None:
                raise RuntimeError(f"pair: no answer in {self._pair_wait_s:.0f} s")
            if "Pairing successful" not in line and "AlreadyExists" not in line:
                raise RuntimeError(f"pair: {line}")
            since = session.mark()
            session.send(f"trust {mac}")
            session.wait_for(("trust succeeded", "not available"), 3.0, since=since,
                             stop=self._stop)
            phase("connecting", "paired and trusted - connecting")
            since = session.mark()
            session.send(f"connect {mac}")
            line = session.wait_for(("Connection successful", "Failed to connect",
                                     "not available"),
                                    self._pair_connect_wait_s, since=since, stop=self._stop)
            if line is None:
                raise RuntimeError(f"connect: no answer in {self._pair_connect_wait_s:.0f} s")
            if "Connection successful" not in line:
                raise RuntimeError(f"connect: {line}")
            session.send("scan off")
        except (RuntimeError, OSError) as exc:
            with self._lock:
                self._connection = "disconnected"
                self._reconnect_at = self._clock() + self._reconnect_every_s
                self._reconnect_error = str(exc)
                self._disconnected_since = (self._disconnected_since
                                            if self._disconnected_since is not None
                                            else self._clock())
            phase("failed", str(exc))
            return
        finally:
            if session is not None:
                session.close()
        with self._lock:
            self._conn_mac = mac
            self._connection = "connecting"      # the check that follows says connected
            self._reconnect_at = None
            self._reconnect_attempts = 0
            self._refused = 0
            self._reconnect_error = None
            self._routed_sink = None
        phase("done", f"paired, trusted and connected in {time.monotonic() - started:.0f} s")

    def _set_connection(self, state: str) -> None:
        # Under self._lock (the caller's).
        self._connection = state

    def _connection_fail(self, message: str) -> None:
        """The thread's tool failed (bluetoothctl raising, say): said once,
        kept in the device's last_error, retried on the interval."""
        with self._lock:
            if self._device is not None:
                self._device["last_error"] = message
            if self._connection in ("connecting", "pairing"):
                self._connection = "disconnected" if self._device else "no_device"
                self._pairing = None if self._pairing is None else dict(
                    self._pairing, phase="failed", note=message)
            said = self._conn_said == message
            self._conn_said = message
        self._conn_due = self._clock() + self._conn_check_s
        if not said:
            self._say(f"connection: {message}")

    # ---- the thread ----

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                wait = self._tick()
            except Exception as exc:        # noqa: BLE001 - said, never fatal
                self._fail(f"{exc.__class__.__name__}: {exc}")
                wait = self._tick_s
            self._stop.wait(max(0.001, min(self._tick_s, wait)))

    def _tick(self) -> float:
        """One look at the run. Returns how long the loop may sleep."""
        now = self._clock()
        if not self._ensure_track(now):
            return self._tick_s
        run, duration = self._run()
        key = None if run is None else (run["t0"], run["state"], run.get("held_at"))
        if key != self._key:
            self._key = key
            self._due = None
            self._follow(run, duration, now)
        if self._due is not None:
            left = self._due - now
            if left > 0:
                return left
            self._due = None
            self._play(True)                       # the show's 0:00
            self._set_state("playing")
        elif self.playing and run is not None and duration > 0 \
                and now - run["t0"] >= duration:
            # The show is over; the track may be longer (§4.4). Silence,
            # and back to the top for the next run.
            self._pause_at_zero()
            self._set_state("ended")
        return self._tick_s

    def _follow(self, run, duration: float, now: float) -> None:
        """The run just changed: put the track where it belongs."""
        if run is None:
            self._pause_at_zero()
            self._set_state("loaded")
            return
        if run["state"] != "running":               # HOLD keeps the position
            self._play(False)
            self._set_state("paused")
            return
        pos = now - run["t0"]
        if duration > 0 and pos >= duration:
            self._pause_at_zero()
            self._set_state("ended")
            return
        if pos + self._latency < 0:
            # The countdown before 0:00: the track waits at the top and
            # the unpause goes out LATENCY early.
            self._pause_at_zero()
            self._due = run["t0"] - self._latency
            self._set_state("armed")
            return
        # A START from a mark, a SEEK, a RESUME, a NEXT: land at the
        # position the show will be at once the sound is out.
        self._command(f"J {max(0.0, pos + self._latency):.3f}s")
        self._play(True)
        self._set_state("playing")

    def _play(self, on: bool) -> None:
        """Bring mpg123 to playing / paused - P only when it is not there
        already (P toggles, and a doubled one would undo itself)."""
        want = PLAYING if on else PAUSED
        with self._reply:
            state = self._pstate
        if state == want:
            return
        if state == STOPPED:
            return                          # nothing loaded: _ensure_track's job
        self._command("P", want=want)

    def _pause_at_zero(self) -> None:
        self._play(False)
        self._command("J 0s")

    # ---- the process and the track ----

    def _ensure_track(self, now: float) -> bool:
        """The music file, loaded and measured. False when there is nothing
        to play with (no mpg123, no track) - said once, retried later."""
        if self._proc is not None and self._proc.poll() is not None:
            code = self._proc.poll()
            with self._lock:
                self._proc, self._loaded = None, None
            self._fail(f"mpg123 exited (code {code})")
            self._next_try = now + self._retry_s
        with self._reply:
            ended = self._eof_seen
            self._eof_seen = False
        if ended and self._loaded is not None and self._proc is not None:
            # The track ran out (shorter than the show): the unsolicited
            # "@P 1" - or a "@P 0" - the reader saw. Loaded again below,
            # from the top, so the NEXT run has music - this run keeps its
            # silence (the run key stays, so nothing follows it back into
            # a file that cannot be played on without a reload).
            with self._lock:
                self._loaded = None
            self._eof = True
            self._say("track ended before the show did - loading it again")
        if now - self._track_checked < self._track_check_s:
            return self._loaded is not None
        self._track_checked = now
        path = self._track()
        if path is None:
            if self._loaded is not None:
                self._quit()
            self._set_state("idle")
            return False
        path = Path(path)
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            return self._loaded is not None
        if self._loaded == (str(path), mtime):
            return True
        if now < self._next_try:
            return False
        if self._proc is None and not self._spawn(now):
            return False
        try:
            self._load(path)
        except RuntimeError as exc:
            with self._lock:
                self._loaded = None
            self._fail(str(exc))
            self._next_try = now + self._retry_s
            return False
        with self._lock:
            self._loaded = (str(path), mtime)
            self.error = None
        self._due = None
        if self._eof:
            self._eof = False
            self._set_state("ended")        # the run goes on without music
        else:
            self._key = None                # a fresh track follows the run
            self._set_state("loaded")
        self._say(f"loaded {path.name}, unpause latency "
                  f"{self._latency * 1000:.0f} ms")
        return True

    def _spawn(self, now: float) -> bool:
        argv = [self._binary, "-R", "--keep-open", "--quiet"]
        if self._output:
            argv += ["-o", self._output]
        try:
            proc = self._factory(argv)
        except OSError as exc:
            self._fail(f"{self._binary} not found ({exc.__class__.__name__}) "
                       "- apt install mpg123")
            self._next_try = now + self._retry_s
            return False
        with self._reply:
            self._pstate, self._error_line = STOPPED, None
            self._phist, self._eof_seen = [], False
        with self._lock:
            self._proc = proc
            self.error = None
        threading.Thread(target=self._read, args=(proc,), daemon=True).start()
        self._command("SILENCE")            # no @F line per frame, please
        return True

    def _read(self, proc) -> None:
        """mpg123's stdout, line by line, into _pstate / _error_line."""
        try:
            for raw in iter(proc.stdout.readline, b""):
                line = raw.decode("utf-8", "replace").strip()
                with self._reply:
                    if line.startswith("@P "):
                        try:
                            state = int(line[3:].split()[0])
                        except ValueError:
                            continue
                        # A "paused" nobody asked for, while it was playing:
                        # the file ran out (mpg123 1.26.4 says it this way;
                        # "@P 0" is the same news from a build that says so).
                        # "Nobody asked" = no P of ours that wanted a pause
                        # is in flight AND still unanswered: a P excuses
                        # only the FIRST wanted line after it. So a P that
                        # wanted PLAYING and got "@P 2" then "@P 1" is the
                        # EOF case, and so is the burst where the track
                        # ends in the same instant as our show-end pause -
                        # "@P 1" (EOF), then "@P 2" / "@P 1" from our P:
                        # the first pause is excused, the last one is not.
                        asked_pause = (self._awaiting and not self._answered
                                       and PAUSED in (self._await_want or ()))
                        if state == PAUSED and self._pstate == PLAYING and not asked_pause:
                            self._eof_seen = True
                        elif state == STOPPED and self._pstate != STOPPED:
                            self._eof_seen = True
                        if self._awaiting and state in (self._await_want or ()):
                            self._answered = True
                        self._pstate = state
                        self._pseq += 1
                        self._phist.append((self._pseq, state))
                        del self._phist[:-8]
                    elif line.startswith("@F "):
                        # "@F <frame> <frames left> <s> <s left>": frames
                        # left 0 confirms the end of the file (only seen
                        # without SILENCE; kept as the confirmation it is).
                        try:
                            self._frames_left = int(line.split()[2])
                        except (IndexError, ValueError):
                            pass
                        if self._frames_left == 0 and self._pstate == PLAYING \
                                and self._awaiting == 0:
                            self._eof_seen = True
                    elif line.startswith("@E"):
                        self._error_line = line[2:].strip() or "error"
                        self._error_seq += 1
                    self._reply.notify_all()
        except (OSError, ValueError):
            pass

    def _load(self, path: Path) -> None:
        """LOADPAUSED, then the latency measurement (see the module doc)."""
        with self._reply:
            self._eof_seen = False          # a fresh file: nothing has run out
            self._frames_left = None
        state = self._command(f"LP {path}", want=(PAUSED, PLAYING),
                              timeout=LOAD_TIMEOUT_S,
                              what=f"could not load {path.name}")
        if state == PLAYING:                # a build that plays on LOADPAUSED
            self._command("P", want=PAUSED, what="pause after load")
        self._command("V 0")
        samples = []
        for _ in range(LATENCY_SAMPLES):
            sent = self._clock()
            self._command("P", want=PLAYING, what="unpause")
            samples.append(self._clock() - sent)
            self._command("P", want=PAUSED, what="pause")
        self._command("V 100")
        self._command("J 0s")
        measured = min(LATENCY_MAX_S, max(0.0, statistics.median(samples)))
        with self._lock:
            self._latency = measured + self._extra_lead
        with self._reply:
            self._eof_seen = False          # the P/P toggles above are not an EOF

    def _command(self, text: str, want=None, timeout: float = REPLY_TIMEOUT_S,
                 what: str = "") -> "int | None":
        """One line to mpg123. With `want` (an @P state, or a tuple of
        acceptable ones) wait for an @P line NEWER than the ones already
        seen and return the state it reached; an @E newer than that or no
        answer in `timeout` is a RuntimeError."""
        proc = self._proc
        if proc is None:
            return None
        wanted = (want,) if isinstance(want, int) else want
        with self._reply:
            seen, errors = self._pseq, self._error_seq
            if wanted is not None:
                self._awaiting += 1
                self._await_want = wanted
                self._answered = False
            try:
                proc.stdin.write((text + "\n").encode("utf-8"))
                proc.stdin.flush()
            except (OSError, ValueError) as exc:
                if wanted is not None:
                    self._awaiting -= 1
                raise RuntimeError(f"mpg123 pipe: {exc}")
            if wanted is None:
                return None
            deadline = time.monotonic() + timeout
            try:
                while True:
                    if self._error_seq != errors:
                        raise RuntimeError(f"mpg123 {what or text}: {self._error_line}")
                    # Any @P newer than the ones seen at send time that is
                    # a wanted state answers - even when a later line has
                    # already moved the state on ("@P 2" then "@P 1" at the
                    # end of the file), which the reader reads as EOF.
                    for seq, state in self._phist:
                        if seq > seen and state in wanted:
                            return state
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise RuntimeError(f"mpg123 {what or text}: no answer "
                                           f"in {timeout:.0f} s")
                    self._reply.wait(left)
            finally:
                self._awaiting -= 1
                if self._awaiting == 0:
                    self._await_want = None

    def _quit(self) -> None:
        with self._lock:
            proc, self._proc, self._loaded = self._proc, None, None
        if proc is None:
            return
        try:
            proc.stdin.write(b"Q\n")
            proc.stdin.flush()
        except (OSError, ValueError):
            pass
        try:
            proc.wait(timeout=1)
        except Exception:                   # noqa: BLE001 - then it is killed
            try:
                proc.terminate()
            except OSError:
                pass

    # ---- what the operator reads ----

    def _set_state(self, state: str) -> None:
        with self._lock:
            self.state = state

    def _fail(self, message: str) -> None:
        with self._lock:
            said = self.error == message
            self.error = message
        if not said:
            self._say(message)

    def _say(self, message: str) -> None:
        with self._lock:
            self.log.append(f"{time.strftime('%H:%M:%S')} {message}")
            del self.log[:-20]
        print(f"speaker: {message}", flush=True)
