"""Append the run counters to disk so a power cut cannot erase them.

Written for battery endurance runs, which by definition end with the
power disappearing - there is no shutdown hook that could save anything
at that moment, so the numbers have to be on disk already.

Why not just read the journal afterwards: journald on this Pi is
configured Storage=volatile (deliberately, to spare the SD card), so its
entire history lives in RAM and dies with the power. This writes only
the handful of values that matter, once a minute, with fsync - a few
dozen bytes per minute, negligible next to a full journal.

Elapsed time comes from /proc/uptime, never the wall clock: the Pi has
no RTC, and a real run here showed the clock jumping about 12 hours
forward once NTP synced after boot, which would have made timestamp
arithmetic off by 3x.

Runs independently of the UI service - it only reads the journal - so it
can be started and stopped mid-test without disturbing a run.

    python3 raspi/runlog.py            # every 60 s into ~/runlog/
"""

import os
import re
import subprocess
import time
from pathlib import Path

OUT_DIR = Path.home() / "runlog"
CSV = OUT_DIR / "epaper-run.csv"
LATEST = OUT_DIR / "latest.txt"
UNIT = "epaper-ui"
PERIOD_S = 60
CYCLE_RE = re.compile(r"cycle (\d+)(?: (\w+))? shown")


def run(cmd) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=20).stdout.strip()
    except Exception:
        return ""


def latest_cycle() -> tuple[str, str]:
    """Newest 'cycle N PATTERN shown' line the service has emitted."""
    out = run(["journalctl", "-u", UNIT, "-n", "80", "--no-pager"])
    cycle = pattern = ""
    for line in out.splitlines():
        found = CYCLE_RE.search(line)
        if found:
            cycle, pattern = found.group(1), found.group(2) or ""
    return cycle, pattern


def sample() -> dict[str, str]:
    cycle, pattern = latest_cycle()
    uptime = float(Path("/proc/uptime").read_text().split()[0])
    return {
        "wall": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "uptime_s": f"{uptime:.0f}",
        "uptime_hms": time.strftime("%H:%M:%S", time.gmtime(uptime)),
        "cycle": cycle,
        "pattern": pattern,
        # Bit 0 set means the 5V rail sagged: on a battery run that is the
        # difference between "the pack ran out" and "the pack cut off".
        "throttled": run(["vcgencmd", "get_throttled"]).replace("throttled=", ""),
        "temp_c": run(["vcgencmd", "measure_temp"]).replace("temp=", "").rstrip("'C"),
        "core_v": run(["vcgencmd", "measure_volts"]).replace("volt=", "").rstrip("V"),
        "service": run(["systemctl", "is-active", UNIT]),
    }


def append(row: dict[str, str]) -> None:
    write_header = not CSV.exists()
    with CSV.open("a") as handle:
        if write_header:
            handle.write(",".join(row) + "\n")
        handle.write(",".join(row.values()) + "\n")
        handle.flush()
        os.fsync(handle.fileno())     # the whole point: survive a hard cut
    # Single-line snapshot, replaced atomically so it is never half written.
    tmp = LATEST.with_suffix(".tmp")
    tmp.write_text("\n".join(f"{k}: {v}" for k, v in row.items()) + "\n")
    os.replace(tmp, LATEST)


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    while True:
        try:
            append(sample())
        except Exception as exc:      # never let one bad sample end the run
            print(f"runlog error: {exc}", flush=True)
        time.sleep(PERIOD_S)


if __name__ == "__main__":
    main()
