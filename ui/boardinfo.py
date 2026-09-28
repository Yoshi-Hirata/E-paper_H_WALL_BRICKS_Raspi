"""BOARD INFO: which board is on this unit's USB cable (2026-09-28).

The boards carry no printed serial or type. The one thing that tells them
apart is the STM32's USB serial string (its unique ID), and only the
board whose USB is plugged into the unit can be read - the ones behind
the 485 have no way to say (this firmware does not answer 0x02). Most of
the fleet's masters read ...324C; two read ...3930, and one of those two
showed wrong colours even in a solid-colour demo after FW_260923 went in
without error. So this screen names the board on the cable:

    SERIAL   5CF26F473930
    TYPE     3930 (differs from most: 324C)
    FW       V1.4 16-color (FW_260923+)
    FLASHED  FW_260923 09-28 18:00 here     (or "no flash record here")

SERIAL and TYPE come from the USB descriptor (transport.usb_board_info:
list_ports / sysfs - nothing is sent to the board), read on a thread of
their own and bounded (transport.usb_board_info_bounded): a descriptor
read waits behind a USB reset, and the LCD loop - which pets the watchdog
- must never wait with it. While the PC drives the unit nothing is read
at all: the runner's cached value is shown (DemoRunner.usb_board()).
FLASHED is this unit's own flash record (ui/flashlog.py). FW is the FW
VERSION screen's own detection (ui/versions.py's BoardVersions - the very
same instance, so nothing new goes on the wire: PLAY_STOP to each
configured address, then 0x29 and 0x25 to the lone USB board only). That
detection needs the port to itself, so the App stops the runner first,
exactly as FW VERSION does - and does not read FW at all while the PC is
driving the unit ("FW (not read while the PC is driving)").
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

from epaper.transport import (USB_BOARD_COMMON, USB_BOARD_READ_S, find_port,
                              usb_board_info, usb_board_info_bounded)

from . import flashlog
from .updater import MenuEntry

FW_NOT_WHILE_PC = "(not read while the PC is driving)"


def type_text(family: "str | None") -> str:
    """'324C', '3930 (differs from most: 324C)', or '-' without a serial."""
    if not family:
        return "-"
    if family == USB_BOARD_COMMON:
        return family
    return f"{family} (differs from most: {USB_BOARD_COMMON})"


def flashed_text(record: "dict | None") -> str:
    """'FLASHED FW_260923 09-28 18:00 here' or 'no flash record here'."""
    said = flashlog.describe(record)          # "flashed FW_... mm-dd HH:MM"
    if not record:
        return said
    return f"FLASHED{said[len('flashed'):]} here"


_BLANK = {"serial": None, "family": None, "vid_pid": None, "bcd": None}


class BoardInfo:
    """State behind the BOARD INFO screen. `versions` is the App's own
    FW VERSION worker (ui/versions.py) - reused, not duplicated - or None,
    in which case the FW line only says it cannot be read here. `cache` is
    the runner (its usb_board() / note_usb_board()), or None."""

    def __init__(self, versions=None, port: "str | None" = None,
                 locate=find_port, board_info=usb_board_info,
                 flash_log: Path = flashlog.DEFAULT_PATH, cache=None,
                 read_timeout: float = USB_BOARD_READ_S):
        self.versions = versions
        self.port = port
        self._locate = locate
        self._board_info = board_info
        self.flash_log = Path(flash_log)
        self.cache = cache
        self.read_timeout = read_timeout
        self.usb: dict = dict(_BLANK)
        self.usb_port: "str | None" = None
        self.usb_state = "none"       # reading / read / busy / cached / none
        self.record: "dict | None" = None
        self.fw_blocked = False       # the PC was driving: FW not read
        self._fw_asked = False        # this screen started a FW read
        self._guard = threading.Lock()   # one descriptor read in flight
        self._epoch = 0

    @property
    def menu_entry(self) -> MenuEntry:
        return MenuEntry("boardinfo", "BOARD INFO",
                         "serial + type of the board on USB")

    @property
    def busy(self) -> bool:
        """True while the FW read holds the port (KEY2 waits for it)."""
        return bool(self._fw_asked and self.versions is not None
                    and self.versions.busy)

    def read(self, read_fw: bool) -> None:
        """Read everything again - never blocking the caller (the LCD loop).

        SERIAL / TYPE / FLASHED: with `read_fw` (the unit is its own), a
        bounded descriptor read on a thread of its own, whose answer also
        fills the runner's cache; without it (the PC drives the unit) the
        runner's cached value, nothing read. FW is read (through FW
        VERSION's scan) only with `read_fw`."""
        if self.busy:
            return
        self._epoch += 1
        self.fw_blocked = not read_fw
        if read_fw:
            self.usb_state = "reading"
            self.usb, self.usb_port, self.record = dict(_BLANK), None, None
            epoch = self._epoch
            threading.Thread(target=self._read_usb, args=(epoch,),
                             daemon=True, name="boardinfo-usb").start()
        else:
            self._show_cached()
        self._fw_asked = bool(read_fw and self.versions is not None)
        if self._fw_asked:
            self.versions.scan()

    def _show_cached(self) -> None:
        cached = None
        try:
            cached = self.cache.usb_board() if self.cache is not None else None
        except Exception:               # noqa: BLE001 - none is an answer
            cached = None
        self.usb = dict(_BLANK, **(cached or {}))
        self.usb_port = None
        self.usb_state = "cached" if cached else "none"
        self._lookup_record()

    def _lookup_record(self) -> None:
        try:
            self.record = flashlog.lookup(self.usb["serial"], self.flash_log)
        except Exception:               # noqa: BLE001 - a bad file is "none"
            self.record = None

    def _read_usb(self, epoch: int) -> None:
        """The screen's own read, on its own thread, never raising."""
        def work(_):
            port = self.port or self._locate()
            return port, (self._board_info(port) or {})
        try:
            got = usb_board_info_bounded(None, self.read_timeout, read=work,
                                         guard=self._guard)
        except Exception:               # noqa: BLE001 - shown as busy
            got = None
        if epoch != self._epoch:
            return                      # superseded by a newer read()
        if got is None:
            self.usb_state = "busy"     # hung or still in flight
            return
        port, info = got
        self.usb = {key: info.get(key) for key in _BLANK}
        self.usb_port = port
        self._lookup_record()
        self.usb_state = "read"
        if self.cache is not None and port:
            try:
                self.cache.note_usb_board(self.usb)
            except Exception:           # noqa: BLE001 - the cache is optional
                pass

    # ---- the four lines ----

    def fw_text(self) -> str:
        if self.fw_blocked:
            return FW_NOT_WHILE_PC
        versions = self.versions
        if versions is None:
            return "(not readable on this unit)"
        if not self._fw_asked:
            return "-"
        if versions.busy:
            return "reading..."
        if versions.usb_fw:
            return versions.usb_fw
        if len(versions.rows) > 1:
            return "485 in: unplug it to read"
        return versions.status or "no board answers"

    def _serial_row(self) -> "tuple[str, str]":
        serial = self.usb["serial"]
        if self.usb_state == "reading":
            return "reading...", ""
        if self.usb_state == "busy":
            return "USB busy - KEY1 to read again", "err"
        if serial:
            return serial, ""
        if self.fw_blocked:
            return "not read yet", ""
        return "none - no board on USB", "err"

    def lines(self) -> "list[tuple[str, str, str]]":
        """[(key, value, tone)] top to bottom; tone is "" / "warn" / "err".
        A key of "" means the value is the whole line."""
        family = self.usb["family"]
        serial, tone = self._serial_row()
        rows = [("SERIAL", serial, tone),
                ("TYPE", type_text(family),
                 "warn" if family and family != USB_BOARD_COMMON else "")]
        fw = self.fw_text()
        rows.append(("FW", fw, "err" if fw.startswith(("ERROR", "no ", "port "))
                     else ""))
        if self.record:
            rows.append(("FLASHED", flashed_text(self.record)[len("FLASHED "):],
                         ""))
        else:
            rows.append(("", flashed_text(None), ""))
        return rows

    def texts(self) -> "list[str]":
        """The lines as the operator reads them: 'SERIAL 5CF26F473930'."""
        return [f"{key} {value}" if key else value
                for key, value, _ in self.lines()]

    def usb_line(self) -> str:
        """Small print: what the serial was read from."""
        if self.usb_state == "cached":
            return "USB serial as read before the PC took over"
        if self.usb_state in ("reading", "busy") or self.fw_blocked:
            return ""
        if not self.usb_port:
            return "no serial port - plug the board's USB in"
        return (f"USB {self.usb.get('vid_pid') or '-'}"
                f"  bcd {self.usb.get('bcd') or '-'}  {self.usb_port}")

    def key(self) -> tuple:
        """Everything the screen shows, for the App's redraw check."""
        versions = self.versions
        return (tuple(self.texts()), self.usb_line(),
                None if versions is None else (versions.phase, versions.status))
