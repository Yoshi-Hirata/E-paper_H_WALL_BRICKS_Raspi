"""The show's music on the Conductor host itself (EXHIBITION mode).

At the exhibition the Conductor runs headless on a unit (radxa-05) with a
USB speaker and no browser, so the page's own <audio> player - which is
what plays the music on the show PC - has nobody to play it. This module
does that job on the host: `python -m conductor serve --speaker` drives
one `mpg123 -R` (its remote-control mode: commands on stdin, `@P` /
`@E` replies on stdout) from a thread of its own, following the same run
the fleet is driving, on the same reference clock (fleet.pc_clock).

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

The unpause latency. Between writing "P" on the pipe and the first sample
leaving the speaker there is the pipe, mpg123's command loop and the
ALSA buffer. The command half is MEASURED once at load: with the volume
at 0 the track is unpaused and paused LATENCY_SAMPLES times and the
round trip from "P" to mpg123's "@P 2" is timed; the median, bounded to
LATENCY_MAX_S, is what the unpause is sent early by. The output half
(the ALSA buffer past the reply) cannot be measured from here, so it is a
documented allowance, DEVICE_LATENCY_S, that `--speaker-lead-ms` trims by
ear. After a JUMP the same latency is added to the position aimed at.

Nothing here ever blocks an HTTP thread: the server only reads status(),
which takes a lock no command ever waits under. A host with no mpg123 (or
one that dies) is a status, not a failure - the run proceeds silently, the
page says so (/api/fleet's `speaker`), and the speaker tries again every
RETRY_S in case somebody installs it. The process factory is injectable so
the tests drive a fake mpg123.
"""

from __future__ import annotations

import statistics
import subprocess
import threading
import time
from pathlib import Path

from .fleet import pc_clock

MPG123 = "mpg123"
TICK_S = 0.05              # how often the run is looked at between events
LOAD_TIMEOUT_S = 5.0       # mpg123 has this long to report a loaded track
REPLY_TIMEOUT_S = 2.0      # ...and to answer any other command
LATENCY_SAMPLES = 3
LATENCY_MAX_S = 0.5        # a round trip longer than this is not latency
DEVICE_LATENCY_S = 0.05    # the ALSA buffer past mpg123's reply (allowance)
RETRY_S = 30.0             # mpg123 missing or dead: try again this often
TRACK_CHECK_S = 1.0        # how often the music file itself is looked at

PAUSED, PLAYING = 1, 2     # mpg123's "@P n" (0 is stopped / nothing loaded)


def default_factory(argv):
    """The real thing: mpg123 on two pipes, stderr dropped (its banner)."""
    return subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)


class Speaker:
    """One mpg123 following the fleet's run. `track()` answers the path of
    the show's music (or None); `run()` answers (run dict or None, the
    show's length) - Fleet.run_snapshot()."""

    def __init__(self, track, run, clock=pc_clock, factory=None,
                 binary: str = MPG123, extra_lead_s: float = DEVICE_LATENCY_S,
                 tick_s: float = TICK_S, retry_s: float = RETRY_S,
                 track_check_s: float = TRACK_CHECK_S):
        self._track = track
        self._run = run
        self._clock = clock
        self._factory = factory or default_factory
        self._binary = binary
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
        self._pstate: "int | None" = None       # the last @P n
        self._error_line: "str | None" = None   # the last @E
        self._next_try = 0.0
        # What is known about the track and the run being followed.
        self._loaded: "tuple[str, int] | None" = None    # (path, mtime_ns)
        self._latency = 0.0
        self._playing = False
        self._key = None                  # (t0, state, held_at) last acted on
        self._due: "float | None" = None  # when to unpause, this PC's clock
        self._track_checked = -1e9
        self.state = "idle"    # idle | loaded | armed | playing | paused | ended
        self.error: "str | None" = None
        self.log: "list[str]" = []

    # ---- lifecycle ----

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
        self._quit()

    def status(self) -> dict:
        """What /api/fleet reports as `speaker` (any thread may call)."""
        with self._lock:
            loaded = self._loaded
            return {"available": self._proc is not None, "error": self.error,
                    "state": self.state,
                    "track": Path(loaded[0]).name if loaded else None,
                    "latency_ms": round(self._latency * 1000, 1),
                    "playing": self._playing, "log": list(self.log[-5:])}

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
            self._command("P", want=PLAYING)      # unpause: the show's 0:00
            self._set_playing(True)
            self._set_state("playing")
        elif self._playing and run is not None and duration > 0 \
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
            if self._playing:
                self._command("P", want=PAUSED)
                self._set_playing(False)
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
        if not self._playing:
            self._command("P", want=PLAYING)
            self._set_playing(True)
        self._set_state("playing")

    def _pause_at_zero(self) -> None:
        if self._playing:
            self._command("P", want=PAUSED)
            self._set_playing(False)
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
        self._set_playing(False)
        self._key, self._due = None, None
        self._set_state("loaded")
        self._say(f"loaded {path.name}, unpause latency "
                  f"{self._latency * 1000:.0f} ms")
        return True

    def _spawn(self, now: float) -> bool:
        try:
            proc = self._factory([self._binary, "-R", "--quiet"])
        except OSError as exc:
            self._fail(f"{self._binary} not found ({exc.__class__.__name__}) "
                       "- apt install mpg123")
            self._next_try = now + self._retry_s
            return False
        with self._reply:
            self._pstate, self._error_line = None, None
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
                            self._pstate = int(line[3:].split()[0])
                        except ValueError:
                            pass
                    elif line.startswith("@E"):
                        self._error_line = line[2:].strip() or "error"
                    self._reply.notify_all()
        except (OSError, ValueError):
            pass

    def _load(self, path: Path) -> None:
        """LOADPAUSED, then the latency measurement (see the module doc)."""
        self._set_playing(False)
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

    def _command(self, text: str, want=None, timeout: float = REPLY_TIMEOUT_S,
                 what: str = "") -> "int | None":
        """One line to mpg123. With `want` (an @P state, or a tuple of
        acceptable ones) wait for it and return the state reached; an @E
        or no answer in `timeout` is a RuntimeError."""
        proc = self._proc
        if proc is None:
            return None
        wanted = (want,) if isinstance(want, int) else want
        with self._reply:
            if wanted is not None:
                self._pstate, self._error_line = None, None
            try:
                proc.stdin.write((text + "\n").encode("utf-8"))
                proc.stdin.flush()
            except (OSError, ValueError) as exc:
                raise RuntimeError(f"mpg123 pipe: {exc}")
            if wanted is None:
                return None
            deadline = time.monotonic() + timeout
            while self._pstate not in wanted and self._error_line is None:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise RuntimeError(f"mpg123 {what or text}: no answer "
                                       f"in {timeout:.0f} s")
                self._reply.wait(left)
            if self._pstate not in wanted:
                raise RuntimeError(f"mpg123 {what or text}: {self._error_line}")
            return self._pstate

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

    def _set_playing(self, playing: bool) -> None:
        with self._lock:
            self._playing = playing

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
