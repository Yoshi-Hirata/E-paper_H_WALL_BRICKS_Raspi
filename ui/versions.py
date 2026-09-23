"""Firmware version check: the FW VERSION menu row.

The protocol has no working "what version are you" command (0x02 from
the V1.0 spec answers ACK_FAIL 0x0A on V1.1 firmware too, every data
variant tried 2026-09-17), so the OTA state query (0x29) is the
fingerprint: V1.0 firmware rejects it, V1.1 answers it. The size/CRC in
that answer would name the bundled image (ota.identify), but the boards
report 0 after their post-OTA reset, so in practice the answer is only
"V1.0" or "V1.1". V1.4 (FW_260923) answers 0x29 like V1.1; what tells
them apart is the pipeline family it added, so the lone USB board is
also asked 0x25 (ota.pipeline_supported): "V1.4 16-color" when it
answers, "V1.1" when it refuses.

0x29 may only go to the USB-attached board: relayed over the 485 it is
never answered and wedges the USB board's CDC (ota.scan explains). So
the scan is a relay-safe presence sweep first, and 0x29 only when one
board answers - with the 485 cable in, the screen lists who is on the
bus and says to unplug it to identify them one at a time.

What the lone USB board was last flashed with comes from this unit's
own flash record (ui/flashlog.py), keyed by the USB serial number, so
the row reads e.g. "V1.1, flashed FW_260917 09-17 17:19".

Like UPDATE FW this stops the runner first: the scan needs the port to
itself. KEY2 returns to the menu without repainting; the panels keep
what they showed, and the operator picks STANDBY or a demo when ready.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from pathlib import Path

import serial

import ota
from epaper.protocol import ACK_SUCCESS
from epaper.transport import Bus, find_port, port_serial

from . import flashlog
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
                 v14=ota.pipeline_supported,
                 firmware_dir: Path = FIRMWARE_DIR, echo_log: bool = True,
                 serial_of=port_serial,
                 flash_log: Path = flashlog.DEFAULT_PATH):
        self.boards = list(boards) if boards else list(range(1, 21))
        self._serial_of = serial_of
        self.flash_log = Path(flash_log)
        self.usb_serial: str | None = None
        self.port = port
        self._open_bus = open_bus or (lambda p: Bus(p, verbose=False))
        self._locate = locate
        self._scan = scan
        self._v14 = v14
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

    ON_BUS = "on 485 bus - unplug 485 to identify"

    def _progress(self, addr: int, ack) -> None:
        """Presence sweep progress: list who answers, identify later."""
        self.asked += 1
        if ack is not None:
            self.rows.append((addr, "answers..."))
        self.status = f"scanning {self.asked}/{len(self.boards)}..."

    def _label(self, ack, v14: bool | None = None) -> str:
        if ack is None:
            return self.ON_BUS
        label = ota.identify(ack, self.catalog)
        if label.startswith("V1.1") and "build unknown" in label:
            # The board cannot say which build; 0x25 tells V1.4 from
            # V1.1, and the flash record names the file.
            record = flashlog.lookup(self.usb_serial, self.flash_log)
            family = ("V1.4 16-color (FW_260923+)" if v14 else
                      "V1.1 16-color" if v14 is False else
                      "V1.1/V1.4 16-color (0x25 unanswered)")
            label = f"{family}, {flashlog.describe(record)}"
            if v14 and record and "FW_2609" in str(record) and "FW_260923" not in str(record):
                label += " - record older than the board"
        return label

    def _run(self) -> None:
        port = self.port or self._locate()
        if not port:
            self.status = "no serial port"
            self.emit("no serial port: plug a board into USB", error=True)
            self.phase = DONE
            return
        try:
            self.usb_serial = self._serial_of(port)
            with self._open_bus(port) as bus:
                found = self._scan(bus, self.boards, progress=self._progress)
                v14 = {}
                if len(found) == 1:             # alone on USB: may be asked
                    (addr, ack), = found.items()
                    if ack is not None and ack.cmd == ACK_SUCCESS:
                        v14[addr] = self._v14(bus, addr, max(self.boards),
                                              log=self.emit)
            self.rows = [(addr, self._label(ack, v14.get(addr)))
                         for addr, ack in found.items()]
            for addr, label in self.rows:
                self.emit(f"board {addr:02d}: {label}")
            answering = len(found)
            if not answering:
                self.status = "no board answers"
            elif answering == 1:
                self.status = (f"USB {self.usb_serial}" if self.usb_serial
                               else f"1/{len(self.boards)} board answers")
            else:
                self.status = (f"{answering}/{len(self.boards)} on bus: "
                               "unplug 485 to identify")
        except serial.SerialTimeoutException:
            self.status = "port wedged (write timeout)"
            self.emit(ota.WEDGE_MSG, error=True)
        except Exception as exc:          # noqa: BLE001 - shown, not raised
            self.status = f"ERROR {exc}"
            self.emit(str(exc), error=True)
        self.phase = DONE
