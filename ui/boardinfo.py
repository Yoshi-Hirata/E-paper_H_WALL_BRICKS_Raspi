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
list_ports / sysfs - nothing is sent to the board). FLASHED is this unit's
own flash record (ui/flashlog.py). FW is the FW VERSION screen's own
detection (ui/versions.py's BoardVersions - the very same instance, so
nothing new goes on the wire: PLAY_STOP to each configured address, then
0x29 and 0x25 to the lone USB board only). That detection needs the port
to itself, so the App stops the runner first, exactly as FW VERSION does -
and does not read FW at all while the PC is driving the unit
("FW (not read while the PC is driving)").
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

from epaper.transport import USB_BOARD_COMMON, find_port, usb_board_info

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


class BoardInfo:
    """State behind the BOARD INFO screen. `versions` is the App's own
    FW VERSION worker (ui/versions.py) - reused, not duplicated - or None,
    in which case the FW line only says it cannot be read here."""

    def __init__(self, versions=None, port: "str | None" = None,
                 locate=find_port, board_info=usb_board_info,
                 flash_log: Path = flashlog.DEFAULT_PATH):
        self.versions = versions
        self.port = port
        self._locate = locate
        self._board_info = board_info
        self.flash_log = Path(flash_log)
        self.usb: dict = {"serial": None, "family": None, "vid_pid": None,
                          "bcd": None}
        self.usb_port: "str | None" = None
        self.record: "dict | None" = None
        self.fw_blocked = False       # the PC was driving: FW not read
        self._fw_asked = False        # this screen started a FW read

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
        """Read everything again. SERIAL, TYPE and FLASHED never touch the
        wire; FW is read (through FW VERSION's scan) only if `read_fw` -
        the App says False while the PC drives the unit."""
        if self.busy:
            return
        try:
            port = self.port or self._locate()
        except Exception:               # noqa: BLE001 - no port is an answer
            port = None
        self.usb_port = port
        try:
            info = self._board_info(port) or {}
        except Exception:               # noqa: BLE001 - the helper never
            info = {}                   # raises; a fake might
        self.usb = {key: info.get(key) for key in
                    ("serial", "family", "vid_pid", "bcd")}
        try:
            self.record = flashlog.lookup(self.usb["serial"], self.flash_log)
        except Exception:               # noqa: BLE001 - a bad file is "none"
            self.record = None
        self.fw_blocked = not read_fw
        self._fw_asked = bool(read_fw and self.versions is not None)
        if self._fw_asked:
            self.versions.scan()

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

    def lines(self) -> "list[tuple[str, str, str]]":
        """[(key, value, tone)] top to bottom; tone is "" / "warn" / "err".
        A key of "" means the value is the whole line."""
        serial = self.usb["serial"]
        family = self.usb["family"]
        rows = [("SERIAL", serial or "none - no board on USB",
                 "" if serial else "err"),
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
        if not self.usb_port:
            return "no serial port - plug the board's USB in"
        return (f"USB {self.usb.get('vid_pid') or '-'}"
                f"  bcd {self.usb.get('bcd') or '-'}  {self.usb_port}")

    def key(self) -> tuple:
        """Everything the screen shows, for the App's redraw check."""
        versions = self.versions
        return (tuple(self.texts()), self.usb_line(),
                None if versions is None else (versions.phase, versions.status))
