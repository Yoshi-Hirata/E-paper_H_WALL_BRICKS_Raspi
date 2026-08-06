"""Keep the run counters on disk so a power cut cannot erase them.

Written for battery endurance runs, which by definition end with the
power vanishing - there is no shutdown hook that could save anything at
that moment, so the numbers have to be on disk already.

Why not just read the journal afterwards: journald on this Pi is
configured Storage=volatile (deliberately, to spare the SD card), so its
whole history lives in RAM and dies with the power.

Everything written here is **bounded** - the files are rewritten in
place, never appended without limit, so storage use is constant no
matter how long the run lasts (a few kB in total):

    latest.txt   the current cycle and uptime, plus context
    errors.log   the most recent errors (rewritten only when one arrives)
    hourly.log   one line per hour, last 48 - shows when things drifted

Elapsed time comes from /proc/uptime, never the wall clock: the Pi has
no RTC, and a real run here showed the clock jumping about 12 hours
forward once NTP synced after boot, which would have made timestamp
arithmetic off by 3x.

Runs independently of the UI service - it only reads the journal - so it
can be started and stopped mid-test without disturbing a run.
"""

import os
import re
import subprocess
import time
from collections import deque
from pathlib import Path

OUT_DIR = Path.home() / "runlog"
LATEST = OUT_DIR / "latest.txt"
ERRORS = OUT_DIR / "errors.log"
HOURLY = OUT_DIR / "hourly.log"
UNIT = "epaper-ui"

PERIOD_S = 60
HOURLY_EVERY = 60          # samples between hourly lines
KEEP_ERRORS = 30
KEEP_HOURLY = 48

CYCLE_RE = re.compile(r"cycle (\d+)(?: (\w+))? shown")
ERROR_RE = re.compile(r"(ERROR|Traceback|no ACK|NAK)", re.IGNORECASE)


def run(cmd) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=20).stdout.strip()
    except Exception:
        return ""


def write_atomic(path: Path, text: str) -> None:
    """Replace the file in one step so it is never half written."""
    tmp = path.with_suffix(".tmp")
    with tmp.open("w") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def format_uptime(seconds: float) -> str:
    total = int(seconds)
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}d {hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


class Watcher:
    def __init__(self):
        OUT_DIR.mkdir(exist_ok=True)
        self.errors: deque[str] = deque(maxlen=KEEP_ERRORS)
        self.hourly: deque[str] = deque(maxlen=KEEP_HOURLY)
        # Reload so a restart of this watcher does not duplicate or lose
        # what it already recorded.
        for path, target in ((ERRORS, self.errors), (HOURLY, self.hourly)):
            if path.exists():
                target.extend(path.read_text().splitlines()[-target.maxlen:])
        self.seen = set(self.errors)
        self.max_temp = 0.0
        self.samples = 0

    def journal(self, lines: int) -> list[str]:
        return run(["journalctl", "-u", UNIT, "-n", str(lines),
                    "--no-pager"]).splitlines()

    def sample(self) -> dict[str, str]:
        lines = self.journal(200)
        cycle = pattern = ""
        for line in lines:
            found = CYCLE_RE.search(line)
            if found:
                cycle, pattern = found.group(1), found.group(2) or ""

        fresh = [ln for ln in lines
                 if ERROR_RE.search(ln) and ln not in self.seen]
        for line in fresh:
            self.seen.add(line)
            self.errors.append(line)
        if fresh:
            write_atomic(ERRORS, "\n".join(self.errors) + "\n")

        uptime = float(Path("/proc/uptime").read_text().split()[0])
        temp = run(["vcgencmd", "measure_temp"]).replace("temp=", "").rstrip("'C")
        try:
            self.max_temp = max(self.max_temp, float(temp))
        except ValueError:
            pass

        return {
            "wall": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "uptime": format_uptime(uptime),
            "uptime_s": f"{uptime:.0f}",
            "cycle": cycle or "-",
            "pattern": pattern or "-",
            # Bits 0-3 are live, 16-19 latch "has happened since boot", so
            # a non-zero value here tells a drained pack (the 5V rail sags
            # first) from a pack that simply switched itself off.
            "throttled": run(["vcgencmd", "get_throttled"]).replace("throttled=", ""),
            "temp_c": temp,
            "temp_max_c": f"{self.max_temp:.1f}",
            "core_v": run(["vcgencmd", "measure_volts"]).replace("volt=", "").rstrip("V"),
            "service": run(["systemctl", "is-active", UNIT]),
            "errors": str(len(self.seen)),
            "last_error": self.errors[-1] if self.errors else "-",
        }

    def step(self) -> None:
        row = self.sample()
        write_atomic(LATEST,
                     "\n".join(f"{k}: {v}" for k, v in row.items()) + "\n")
        self.samples += 1
        if self.samples % HOURLY_EVERY == 1:
            self.hourly.append(
                f"{row['wall']} up={row['uptime']} cycle={row['cycle']} "
                f"temp={row['temp_c']} max={row['temp_max_c']} "
                f"throttled={row['throttled']} errors={row['errors']}")
            write_atomic(HOURLY, "\n".join(self.hourly) + "\n")


def main() -> None:
    watcher = Watcher()
    while True:
        try:
            watcher.step()
        except Exception as exc:      # never let one bad sample end the run
            print(f"runlog error: {exc}", flush=True)
        time.sleep(PERIOD_S)


if __name__ == "__main__":
    main()
