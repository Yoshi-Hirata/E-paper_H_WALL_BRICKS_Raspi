"""Repo update mode: `git pull` from the LCD HAT, then restart the UI.

Ten appliances run the same checkout, and until now bringing one up to
date meant SSHing in. The GIT PULL menu row runs `git pull --ff-only`
in the checkout the service runs from and, when the commit moved,
offers a restart so the new code - and any newly bundled firmware
image - is what runs. The restart is simply exiting: epaper-ui.service
is Restart=always, so systemd brings the UI back after RestartSec
without any sudo.

Fast-forward only: a unit's checkout is never edited in place, so
anything else is a fault worth reading on the screen, not merging
blindly. git runs with GIT_TERMINAL_PROMPT=0 so a credential prompt
fails instead of hanging the worker forever, and with a timeout so a
dead network fails too.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from .config import LOG_HISTORY
from .updater import MenuEntry

REPO_ROOT = Path(__file__).resolve().parents[1]

IDLE = "idle"            # confirm screen: KEY1 pulls
PULLING = "pulling"      # git pull in flight
DONE = "done"            # pulled (see `changed`) - KEY1 restarts if it moved
FAILED = "failed"

PULL_TIMEOUT_S = 180.0


@dataclass(frozen=True)
class Head:
    """What the checkout is at: short hash and commit subject."""

    commit: str
    subject: str = ""

    @property
    def label(self) -> str:
        return f"{self.commit} {self.subject}".strip()


def run_git(repo: Path, args: list[str], timeout: float) -> tuple[int, str]:
    """`git -C repo args...`; (exit code, stdout+stderr). Never prompts."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    try:
        result = subprocess.run(["git", "-C", str(repo), *args],
                                capture_output=True, text=True,
                                timeout=timeout, env=env)
    except FileNotFoundError:
        return 127, "git not installed"
    output = (result.stdout + result.stderr).strip()
    return result.returncode, output


class RepoPuller:
    """State behind the GIT PULL screen; the pull runs in a thread.

    `run` is injectable (args, timeout) -> (code, output) so the flow is
    testable without a repo or a network.
    """

    def __init__(self, repo: Path = REPO_ROOT, run=None,
                 timeout: float = PULL_TIMEOUT_S, echo_log: bool = True):
        self.repo = Path(repo)
        self._run = run or (lambda args, t: run_git(self.repo, args, t))
        self.timeout = timeout
        self._echo_log = echo_log

        self.phase = IDLE
        self.error: str | None = None
        self.before: Head = self.head()
        self.after: Head | None = None
        self.log: deque[str] = deque(maxlen=LOG_HISTORY)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    # ---- facts for the screen ----

    @property
    def menu_entry(self) -> MenuEntry:
        return MenuEntry("pull", "GIT PULL", f"repo at {self.before.label}")

    @property
    def busy(self) -> bool:
        return self.phase == PULLING

    @property
    def finished(self) -> bool:
        return self.phase in (DONE, FAILED)

    @property
    def changed(self) -> bool:
        """True once a pull moved HEAD - a restart is what applies it."""
        return (self.phase == DONE and self.after is not None
                and self.after.commit != self.before.commit)

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

    # ---- git ----

    def head(self) -> Head:
        code, commit = self._run(["rev-parse", "--short", "HEAD"], 10.0)
        if code != 0 or not commit:
            return Head("?", commit.splitlines()[0] if commit else "no git")
        _, subject = self._run(["log", "-1", "--format=%s"], 10.0)
        return Head(commit.strip(), subject.strip())

    def branch(self) -> str:
        code, name = self._run(["rev-parse", "--abbrev-ref", "HEAD"], 10.0)
        return name.strip() if code == 0 and name.strip() else "HEAD"

    # ---- the pull itself ----

    def reset(self) -> None:
        """Back to the confirm screen, re-reading where the checkout is."""
        if self.busy:
            return
        self.phase = IDLE
        self.error = None
        self.after = None
        self.before = self.head()

    def start(self) -> None:
        if self.busy:
            return
        self.phase = PULLING
        self.error = None
        self.after = None
        self._thread = threading.Thread(target=self._pull, daemon=True)
        self._thread.start()

    def join(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _pull(self) -> None:
        try:
            self.before = self.head()
            if self.before.commit == "?":
                raise RuntimeError(f"not a git checkout: {self.before.subject}")
            self.emit(f"git pull --ff-only ({self.branch()}) at {self.before.commit}")
            try:
                code, output = self._run(["pull", "--ff-only"], self.timeout)
            except subprocess.TimeoutExpired:
                raise RuntimeError(f"git pull timed out after {self.timeout:.0f} s")
            for line in output.splitlines():
                if line.strip():
                    self.emit(line.rstrip(), error=(code != 0 and
                                                    line.startswith(("fatal", "error"))))
            if code != 0:
                raise RuntimeError(f"git pull failed (exit {code})")
            self.after = self.head()
            self.phase = DONE
            if self.after.commit == self.before.commit:
                self.emit(f"up to date at {self.after.commit}")
            else:
                self.emit(f"{self.before.commit} -> {self.after.commit}: "
                          "restart to apply")
        except Exception as exc:          # noqa: BLE001 - shown, not raised
            self.error = str(exc)
            self.emit(str(exc), error=True)
            self.phase = FAILED
