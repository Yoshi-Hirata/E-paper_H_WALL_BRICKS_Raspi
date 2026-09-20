"""Reboot mode: restart the whole unit from the LCD HAT.

Ten appliances sit behind a wall with nothing but this screen, and a
wedged USB CDC port or a stuck network stack (docs/STATUS.md 2.x) used
to mean a power cycle by hand. The REBOOT menu row asks the OS to
restart instead: `sudo -n systemctl reboot`, the same passwordless sudo
the USB rebind already relies on (radxa/README.md, setup step 3).

A reboot is not something a stray press should do, so the row only opens
a confirm screen and the reboot itself needs KEY1 *held* (ui/app.py).

The runner is deliberately left alone: if the command is refused the
demo carries on undisturbed, and if it goes through the shutdown ends
everything anyway. After the boot the UI comes up like any other start
- standby (all panels white) unless the unit runs with --pattern or
--no-standby.
"""

from __future__ import annotations

import subprocess
import threading
import time
from collections import deque

from .config import LOG_HISTORY
from .updater import MenuEntry

IDLE = "idle"              # confirm screen: hold KEY1 reboots
REBOOTING = "rebooting"    # command accepted; the OS is going down
FAILED = "failed"          # sudo or systemd refused

REBOOT_COMMAND = ["sudo", "-n", "systemctl", "reboot"]
REBOOT_TIMEOUT_S = 20.0


def run_command(args: list[str], timeout: float) -> tuple[int, str]:
    """Run `args`; (exit code, stdout+stderr). Never waits on a prompt."""
    try:
        result = subprocess.run(args, capture_output=True, text=True,
                                timeout=timeout, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        return 127, f"{args[0]} not installed"
    return result.returncode, (result.stdout + result.stderr).strip()


class Rebooter:
    """State behind the REBOOT screen; the command runs in a thread.

    `run` is injectable (args, timeout) -> (code, output) so the flow is
    testable without rebooting the machine the tests run on.
    """

    def __init__(self, run=run_command, command: list[str] | None = None,
                 timeout: float = REBOOT_TIMEOUT_S, echo_log: bool = True):
        self._run = run
        self.command = list(command or REBOOT_COMMAND)
        self.timeout = timeout
        self._echo_log = echo_log

        self.phase = IDLE
        self.error: str | None = None
        self.log: deque[str] = deque(maxlen=LOG_HISTORY)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    # ---- facts for the screen ----

    @property
    def menu_entry(self) -> MenuEntry:
        return MenuEntry("reboot", "REBOOT", "restart this unit (~1 min)")

    @property
    def busy(self) -> bool:
        """True from the moment the command is issued: a reboot that was
        accepted is never taken back, and a refused one is settled in
        well under a second."""
        return self.phase == REBOOTING

    def recent(self, count: int) -> list[str]:
        with self._lock:
            return list(self.log)[-count:]

    def emit(self, message: str, error: bool = False) -> None:
        first = message.strip().splitlines()[0] if message.strip() else ""
        if error and not first.startswith("ERROR"):
            first = f"ERROR {first}"
        stamp = time.strftime("%H:%M:%S")
        with self._lock:
            self.log.append(f"{stamp} {first}")
        if self._echo_log:
            print(f"{stamp} {message.rstrip()}", flush=True)

    # ---- the reboot itself ----

    def reset(self) -> None:
        """Back to the confirm screen after a refusal."""
        if self.busy:
            return
        self.phase = IDLE
        self.error = None

    def start(self) -> None:
        if self.busy:
            return
        self.phase = REBOOTING
        self.error = None
        self._thread = threading.Thread(target=self._reboot, daemon=True)
        self._thread.start()

    def join(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _reboot(self) -> None:
        try:
            self.emit(f"reboot: {' '.join(self.command)}")
            try:
                code, output = self._run(self.command, self.timeout)
            except subprocess.TimeoutExpired:
                raise RuntimeError(
                    f"reboot command timed out after {self.timeout:.0f} s")
            if code != 0:
                for line in output.splitlines():
                    if line.strip():
                        self.emit(line.rstrip(), error=True)
                raise RuntimeError(f"reboot refused (exit {code})")
            # Accepted. Stay in REBOOTING: the next thing that happens
            # is the OS killing this process.
            self.emit("reboot requested - going down")
        except Exception as exc:          # noqa: BLE001 - shown, not raised
            self.error = str(exc)
            self.emit(str(exc), error=True)
            self.phase = FAILED
