"""Firmware version check: the FW VERSION menu row.

The protocol has no "what version are you" command, so the OTA state
query (0x29) is the fingerprint, exactly as host/ota.py's scan uses it
to find the update target: V1.0 firmware rejects it, V1.1 answers with
the size and CRC of the image it was last given, and those name the
bundled FW_<yymmdd> folder when they match (ota.identify). Every
configured address is asked, so with the 485 cable in this is a
one-screen inventory of the whole wall.

Like UPDATE FW this stops the runner first: the scan needs the port to
itself, and standby on the way out hands it back and repaints white.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from pathlib import Path

import serial

import ota
from epaper.transport import Bus, find_port

from .config import LOG_HISTORY
from .updater import FIRMWARE_DIR, MenuEntry

IDLE = "idle"              # nothing scanned yet (only before the first scan)
SCANNING = "scanning"
DONE = "done"


class BoardVersions:
    """State behind the FW VERSION screen; the scan runs in a thread.

    `open_bus`, `locate` and `scan` are injectable so the flow is
    testable without a board.
    """

    def __init__(self, boards: list[int] | None = None,
                 port: str | None = None, open_bus=None, locate=find_port,
                 scan=ota.scan, catalog: dict | None = None,
                 firmware_dir: Path = FIRMWARE_DIR, echo_log: bool = True):
        self.boards = list(boards) if boards else list(range(1, 21))
        self.port = port
        self._open_bus = open_bus or (lambda p: Bus(p, verbose=False))
        self._locate = locate
        self._scan = scan
        self.catalog = (catalog if catalog is not None
                        else ota.bundled_images(firmware_dir))
        self._echo_log = echo_log

        self.phase = IDLE
        self.status = ""                 # one line under the header
        self.rows: list[tuple[int, str]] = []   # (addr, label), answers only
        self.asked = 0                   # addresses queried so far
        self.offset = 0                  # first row shown (UP/DOWN scroll)
        self.log: deque[str] = deque(maxlen=LOG_HISTORY)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    # ---- facts for the screen ----

    @property
    def menu_entry(self) -> MenuEntry:
        span = f"{self.boards[0]:02d}-{self.boards[-1]:02d}"
        return MenuEntry("versions", "FW VERSION",
                         f"ask boards {span} what they run")

    @property
    def busy(self) -> bool:
        return self.phase == SCANNING

    @property
    def bundled(self) -> str:
        """The newest bundled image's folder, for the screen's footer."""
        names = sorted(set(self.catalog.values()))
        return names[-1] if names else "none"

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

    # ---- scrolling ----

    def scroll(self, step: int, visible: int) -> None:
        if self.busy:
            return
        top = max(0, len(self.rows) - visible)
        self.offset = max(0, min(self.offset + step, top))

    # ---- the scan ----

    def scan(self) -> None:
        if self.busy:
            return
        self.phase = SCANNING
        self.rows = []
        self.asked = 0
        self.offset = 0
        self.status = "scanning..."
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def join(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _progress(self, addr: int, ack) -> None:
        self.asked += 1
        if ack is not None:
            label = ota.identify(ack, self.catalog)
            self.rows.append((addr, label))
            self.emit(f"board {addr:02d}: {label}")
        self.status = f"scanning {self.asked}/{len(self.boards)}..."

    def _run(self) -> None:
        port = self.port or self._locate()
        if not port:
            self.status = "no serial port"
            self.emit("no serial port: plug a board into USB", error=True)
            self.phase = DONE
            return
        try:
            with self._open_bus(port) as bus:
                found = self._scan(bus, self.boards, progress=self._progress)
            answering = len(found)
            self.status = (f"{answering}/{len(self.boards)} boards answer"
                           if answering else "no board answers 0x29")
        except serial.SerialTimeoutException:
            self.status = "port wedged (write timeout)"
            self.emit(ota.WEDGE_MSG, error=True)
        except Exception as exc:          # noqa: BLE001 - shown, not raised
            self.status = f"ERROR {exc}"
            self.emit(str(exc), error=True)
        self.phase = DONE
