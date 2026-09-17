"""Firmware update mode: flash the bundled OTA image from the LCD HAT.

The images live in the repo (FW/FW_<yymmdd>/*.bin), so a `git pull`
on the appliance is the whole "copy the firmware over" step, and the
newest folder is what the menu offers. The flashing itself is
host/ota.py, unchanged; this module runs it in a worker thread and turns
its log into screen state, the way DemoRunner does for the demos.

The update is point-to-point over the USB-attached board. On entering
the confirm screen every configured address is asked its OTA state
(0x29); if exactly one answers - the USB board's own DIP setting, with
all DIP off reading as 1 - it becomes the target, and UP/DOWN remain
as an override. Several answers mean the 485 cable is still plugged in
and the relayed boards are answering too, so the screen says so rather
than guessing. KEY1 flashes. Nothing
interrupts a transfer once started - a half-written image is harmless
(the board stages it and only commits on 0x28) but a button that could
abort it mid-way invites exactly the retry-into-a-stall failure that
host/ota.py exists to avoid.

After 0x28 the board resets without signalling a USB disconnect, and on
the Radxa it then sometimes fails to re-enumerate (dmesg "error -71",
both boards 2026-09-11). Unbinding and rebinding the xhci host
controller fixed it every time, so that is the fallback here when the
board stays silent after the reboot; it needs passwordless sudo, which
radxa/setup.sh assumes anyway.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

import serial

import ota
from epaper.transport import Bus, find_port

from .config import LOG_HISTORY

REPO_ROOT = Path(__file__).resolve().parents[1]
FIRMWARE_DIR = REPO_ROOT / "FW"
XHCI_DRIVER = Path("/sys/bus/platform/drivers/xhci-hcd")

# Phases of the update screen.
IDLE = "idle"              # confirm screen: pick the board, KEY1 flashes
FLASHING = "flashing"      # 0x26 / 0x27 chunks / 0x28 in flight
VERIFYING = "verifying"    # waiting for the board to reboot and answer 0x29
DONE = "done"
FAILED = "failed"

VERIFY_WAIT_S = 25.0       # host/ota.py's default, per reboot attempt


@dataclass(frozen=True)
class MenuEntry:
    """The menu row. Duck-types Pattern for the menu renderer only; the
    App never hands it to the runner."""

    key: str
    label: str
    detail: str


def find_firmware(directory: Path = FIRMWARE_DIR) -> Path | None:
    """Newest OTA image in the repo: the .bin in the last FW_<yymmdd> folder.

    Folders are date-named, so the last one in sort order is the newest.
    The vendor's file names are kept as delivered (OTA_16c.bin in
    FW_260903, MCB_e16_2029.09.17.bin in FW_260917), so any .bin counts;
    the .hex beside it is for SWD burning and is never offered. Should a
    folder ever hold several .bin files, the last in sort order wins.
    """
    candidates = sorted(directory.glob("FW_*/*.bin"))
    return candidates[-1] if candidates else None


def usb_rebind(log, driver: Path = XHCI_DRIVER, settle_s: float = 3.0) -> bool:
    """Re-enumerate USB by unbinding and rebinding the xhci controller(s).

    The recovery from docs/STATUS.md for a board that reset after 0x28
    and never came back (dmesg "unable to enumerate USB device"). Every
    xhci-hcd platform device under the driver is cycled - on the Cubie
    A7Z there is one data port, so there is nothing else to disturb.
    """
    if not driver.is_dir():
        log("usb rebind: no xhci-hcd platform driver here", error=True)
        return False
    devices = sorted(p.name for p in driver.iterdir()
                     if p.name.startswith("xhci-hcd"))
    if not devices:
        log("usb rebind: no xhci-hcd device to cycle", error=True)
        return False
    for device in devices:
        for action in ("unbind", "bind"):
            try:
                result = subprocess.run(
                    ["sudo", "-n", "tee", str(driver / action)],
                    input=device.encode(), capture_output=True, timeout=20)
            except (OSError, subprocess.SubprocessError) as exc:
                log(f"usb rebind: {action} {device}: {exc}", error=True)
                return False
            if result.returncode != 0:
                reason = result.stderr.decode(errors="replace").strip()
                log(f"usb rebind: {action} {device} failed: "
                    f"{reason or result.returncode}", error=True)
                return False
            log(f"usb rebind: {action} {device}")
            time.sleep(settle_s)
    return True


class FirmwareUpdater:
    """State behind the update screen; the work runs in a thread.

    `open_bus`, `flash`, `verify`, `rebind` and `locate` are injectable
    so the whole flow is testable without a board.
    """

    def __init__(self, firmware: Path | None = None,
                 boards: list[int] | None = None, port: str | None = None,
                 open_bus=None, flash=ota.flash, verify=ota.verify,
                 rebind=usb_rebind, locate=find_port,
                 verify_wait: float = VERIFY_WAIT_S, echo_log: bool = True):
        self.firmware = Path(firmware) if firmware else None
        self.boards = list(boards) if boards else list(range(1, 21))
        self.port = port
        self._open_bus = open_bus or (lambda p: Bus(p, verbose=False))
        self._flash = flash
        self._verify = verify
        self._rebind = rebind
        self._locate = locate
        self.verify_wait = verify_wait
        self._echo_log = echo_log

        self.addr = self.boards[0]
        self.phase = IDLE
        self.board_state = ""       # last 0x29 answer for self.addr
        self.error: str | None = None
        self.done = 0               # bytes transferred
        self.size = self._image_size()
        self.log: deque[str] = deque(maxlen=LOG_HISTORY)

        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._probe_thread: threading.Thread | None = None
        # Pending background query: ("scan", None) or ("probe", addr).
        self._probe_pending: tuple[str, int | None] | None = None
        self.found: dict[int, str] = {}   # last scan: addr -> state line

    # ---- facts for the screen ----

    @property
    def menu_entry(self) -> MenuEntry:
        return MenuEntry("update", "UPDATE FW", self.firmware_label)

    @property
    def firmware_label(self) -> str:
        if self.firmware is None:
            return "no firmware image in FW/"
        return f"{self.firmware.parent.name}/{self.firmware.name}"

    @property
    def busy(self) -> bool:
        """True while a flash is in flight - nothing may interrupt it."""
        return self.phase in (FLASHING, VERIFYING)

    @property
    def finished(self) -> bool:
        return self.phase in (DONE, FAILED)

    @property
    def progress(self) -> float:
        return self.done / self.size if self.size else 0.0

    def recent(self, count: int) -> list[str]:
        with self._lock:
            return list(self.log)[-count:]

    def emit(self, message: str, error: bool = False) -> None:
        """Log sink for host/ota.py and this module.

        The LCD keeps one line per message (ota's failure texts run to
        several lines of advice); the full text goes to stdout, which
        is the journal under systemd.
        """
        first = message.strip().splitlines()[0] if message.strip() else ""
        if error and not first.startswith("ERROR"):
            first = f"ERROR {first}"
        stamp = time.strftime("%H:%M:%S")
        with self._lock:
            self.log.append(f"{stamp} {first}")
        if self._echo_log:
            print(f"{stamp} {message.rstrip()}", flush=True)

    def _image_size(self) -> int:
        try:
            return self.firmware.stat().st_size if self.firmware else 0
        except OSError:
            return 0

    # ---- board choice + state query ----

    @property
    def bus_shared(self) -> bool:
        """True when the last scan saw several boards: the 485 cable is
        in, so no 0x29 may go out (a relayed one wedges the USB board)."""
        return len(self.found) > 1

    def select(self, step: int) -> None:
        """Move the target address through the configured boards."""
        if self.busy:
            return
        index = self.boards.index(self.addr) if self.addr in self.boards else 0
        self.addr = self.boards[(index + step) % len(self.boards)]
        if self.bus_shared:
            self.board_state = "485 connected: unplug it, KEY1 rescans"
            return
        self.probe()

    def reset(self) -> None:
        """Back to the confirm screen (after DONE/FAILED, or on entry)."""
        if self.busy:
            return
        self.phase = IDLE
        self.error = None
        self.done = 0
        self.size = self._image_size()
        self.board_state = ""

    def probe(self) -> None:
        """Ask the selected board its OTA state (0x29), in the background.

        Repeated presses collapse into one query of whatever address is
        current when the worker gets to it; a stale answer for a board
        no longer selected is dropped.
        """
        self._enqueue(("probe", self.addr))

    def scan(self) -> None:
        """Find the target by asking every configured address (0x29).

        The USB-attached board answers at its own DIP address; with the
        485 cable unplugged nothing else does, so one answer picks the
        target without the operator reading DIP switches. UP/DOWN after
        the scan override it with a plain probe.
        """
        self._enqueue(("scan", None))

    def _enqueue(self, job: tuple[str, int | None]) -> None:
        if self.busy:
            return
        with self._lock:
            self._probe_pending = job
            if self._probe_thread is not None and self._probe_thread.is_alive():
                return
            self._probe_thread = threading.Thread(target=self._probe_loop,
                                                  daemon=True)
            self._probe_thread.start()

    def _probe_loop(self) -> None:
        while True:
            with self._lock:
                job, self._probe_pending = self._probe_pending, None
                if job is None:
                    self._probe_thread = None
                    return
            kind, addr = job
            if kind == "scan":
                self.board_state = "scanning..."
                self._scan()
                continue
            self.board_state = "checking..."
            state = self._with_bus(lambda bus: ota.describe_state(
                ota.query_state(bus, addr, quiet=True,
                                log=lambda *a, **k: None)))
            if self.addr == addr and not self.busy:
                self.board_state = state

    def _scan(self) -> None:
        result = self._with_bus(lambda bus: ota.scan(bus, self.boards))
        if self.busy or self._probe_pending is not None:
            return                      # superseded by a newer request
        if isinstance(result, str):     # no port / wedged / exception
            self.found = {}
            self.board_state = result
            return
        self.found = {addr: ota.describe_found(ack)
                      for addr, ack in result.items()}
        chosen, reason = ota.choose_target(result)
        if chosen is not None:
            self.addr = chosen
            self.board_state = f"{self.found[chosen]} (auto)"
        else:
            self.board_state = reason
        self.emit(reason)

    def _with_bus(self, action):
        """Run `action(bus)` on a freshly opened port; a string on failure."""
        port = self.port or self._locate()
        if not port:
            return "no serial port"
        try:
            with self._open_bus(port) as bus:
                return action(bus)
        except serial.SerialTimeoutException:
            return "port wedged (write timeout)"
        except Exception as exc:
            return f"ERROR {exc}"

    # ---- the update itself ----

    def start(self) -> None:
        if self.busy:
            return
        if self.firmware is None:
            self.phase = FAILED
            self.error = "no firmware image"
            self.emit("no firmware image in FW/", error=True)
            return
        if self.bus_shared:
            # KEY1 on the result screen rescans, which is the way back
            # once the cable is out.
            self.phase = FAILED
            self.error = "unplug the 485 cable first"
            self.emit("485 connected: OTA is point-to-point, unplug it "
                      "and press KEY1 to rescan", error=True)
            return
        with self._lock:
            # A probe still in flight must not overwrite the flashing
            # state with a stale answer; it checks `busy` before writing.
            self._probe_pending = None
        self.phase = FLASHING
        self.error = None
        self.done = 0
        self.emit(f"update board {self.addr:02d} <- {self.firmware_label}")
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def join(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _progress(self, done: int, size: int) -> None:
        self.done, self.size = done, size

    def _run(self) -> None:
        addr = self.addr
        try:
            image = self.firmware.read_bytes()
            if not 0 < len(image) <= ota.MAX_IMAGE_SIZE:
                raise RuntimeError(f"image size {len(image)} out of range")
            self.size = len(image)
            port = self.port or self._locate()
            if not port:
                raise RuntimeError("no serial port")
            self.emit(f"port {port}")
            with self._open_bus(port) as bus:
                ok = self._flash(bus, addr, image, log=self.emit,
                                 progress=self._progress)
            if not ok:
                raise RuntimeError("flash failed")
            self.phase = VERIFYING
            ok = self._verify(addr, self.port, wait_s=self.verify_wait,
                              log=self.emit, open_bus=self._open_bus,
                              locate=self._locate)
            if not ok and self._rebind is not None:
                self.emit("board silent after reboot, cycling USB host")
                if self._rebind(self.emit):
                    ok = self._verify(addr, self.port,
                                      wait_s=self.verify_wait, log=self.emit,
                                      open_bus=self._open_bus,
                                      locate=self._locate)
            if not ok:
                raise RuntimeError("board did not come back; replug USB")
            self.board_state = "updated"
            self.phase = DONE
            self.emit(f"board {addr:02d} updated")
        except serial.SerialTimeoutException:
            self.error = "port wedged: replug USB / power-cycle board"
            self.emit(ota.WEDGE_MSG, error=True)
            self.phase = FAILED
        except Exception as exc:          # noqa: BLE001 - shown, not raised
            self.error = str(exc)
            self.emit(f"{exc}", error=True)
            self.phase = FAILED
