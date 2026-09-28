"""BOARD INFO (ui/boardinfo.py) and the SERIAL/TYPE line on UPDATE FW.

The board on the USB cable is the only one a unit can name: its USB serial
(the STM32 unique ID) and the serial's last four characters, its TYPE -
most of the fleet reads 324C, two boards read 3930 and behaved differently
(2026-09-28). The serial comes from a fake usb_board_info here; FW comes
from FW VERSION's own scan over the fake OTA bus the version tests use.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

import ota
from ui import flashlog, render
from ui.app import App, Screen
from ui.boardinfo import BoardInfo, FW_NOT_WHILE_PC, type_text
from ui.config import HEIGHT, WIDTH
from ui.display import NullDisplay
from ui.inputs import ScriptedInput
from ui.versions import DONE
from tests.test_ui_app import FakeRunner
from tests.test_ui_runner import wait_until
from tests.test_ui_update import FakeOtaBus, make_updater
from tests.test_ui_versions import alone, make_versions, wall

ODD = {"serial": "5CF26F473930", "family": "3930", "vid_pid": "0483:5740",
       "bcd": "0200"}
USUAL = {"serial": "48E8854C324C", "family": "324C", "vid_pid": "0483:5740",
         "bcd": "0200"}
NONE = {"serial": None, "family": None, "vid_pid": None, "bcd": None}
AT_1800 = time.mktime((2026, 9, 28, 18, 0, 0, 0, 0, -1))


def make_info(tmp_path, usb=ODD, bus=None, record=None, versions=True):
    log = tmp_path / "flash.json"
    if record:
        flashlog.record(usb["serial"], 1, record, 100, 0x1234, path=log,
                        when=AT_1800)
    seen = []

    def board_info(port):
        seen.append(port)
        return dict(usb)
    v = (make_versions(tmp_path, bus=bus or alone(1), flash_log=log,
                       serial_of=lambda port: usb["serial"])
         if versions else None)
    info = BoardInfo(v, locate=lambda: "/dev/ttyACM0", board_info=board_info,
                     flash_log=log)
    return info, seen


class FakeRemote:
    """Just what App reads off a RemoteSession to know who drives."""

    def __init__(self, active=False, owned=False):
        self.active = active
        self._owned = owned
        self.busy = None
        self.released = 0

    def owned(self):
        return self._owned

    def release(self):
        self.released += 1
        self.active = False


def make_app(info, remote=None):
    runner = FakeRunner()
    app = App(NullDisplay(), ScriptedInput(()), runner, port_label="/dev/ttyACM0",
              versions=info.versions, boardinfo=info, remote=remote,
              host="radxa-01")
    return app, runner


def open_board_info(app):
    app.select("boardinfo")
    app.handle("key1")


# ---- the four lines ----

def test_a_3930_board_reads_as_different_from_most(tmp_path):
    info, seen = make_info(tmp_path, record="FW_260923/fw2029.09.23/MCB_16_0923.bin")
    info.read(read_fw=True)
    assert wait_until(lambda: info.versions.phase == DONE)
    assert seen == ["/dev/ttyACM0"]
    assert info.texts() == ["SERIAL 5CF26F473930",
                            "TYPE 3930 (differs from most: 324C)",
                            "FW V1.4 16-color (FW_260923+)",
                            "FLASHED FW_260923 09-28 18:00 here"]
    tones = [tone for _, _, tone in info.lines()]
    assert tones[1] == "warn"                       # amber on the LCD
    assert info.usb_line() == "USB 0483:5740  bcd 0200  /dev/ttyACM0"


def test_a_324c_v1_1_board_without_a_record(tmp_path):
    info, _ = make_info(tmp_path, usb=USUAL, bus=alone(1, v14=False))
    info.read(read_fw=True)
    assert wait_until(lambda: info.versions.phase == DONE)
    assert info.texts() == ["SERIAL 48E8854C324C", "TYPE 324C",
                            "FW V1.1 16-color", "no flash record here"]
    assert [tone for _, _, tone in info.lines()][1] == ""


def test_no_board_on_usb_says_so(tmp_path):
    info, _ = make_info(tmp_path, usb=NONE, bus=FakeOtaBus(answers=set()))
    info.read(read_fw=True)
    assert wait_until(lambda: info.versions.phase == DONE)
    assert info.texts() == ["SERIAL none - no board on USB", "TYPE -",
                            "FW no board answers", "no flash record here"]


def test_with_the_485_in_fw_is_not_guessed(tmp_path):
    info, _ = make_info(tmp_path, bus=wall(tmp_path))
    info.read(read_fw=True)
    assert wait_until(lambda: info.versions.phase == DONE)
    assert info.texts()[2] == "FW 485 in: unplug it to read"


def test_type_text_for_every_family():
    assert type_text("324C") == "324C"
    assert type_text("3930") == "3930 (differs from most: 324C)"
    assert type_text("other") == "other (differs from most: 324C)"
    assert type_text(None) == "-"


# ---- the menu row and KEY handling ----

def test_board_info_is_a_menu_row_after_fw_version(tmp_path):
    info, _ = make_info(tmp_path)
    app, _ = make_app(info)
    keys = [p.key for p in app.patterns]
    assert keys.index("boardinfo") == keys.index("versions") + 1
    assert app.patterns[keys.index("boardinfo")].label == "BOARD INFO"


def test_key1_opens_it_reads_fw_with_fw_versions_own_scan_and_key2_leaves(tmp_path):
    bus = alone(1)
    info, _ = make_info(tmp_path, bus=bus)
    app, runner = make_app(info)
    open_board_info(app)
    assert app.screen is Screen.BOARDINFO
    assert runner.stops == 1                  # the port, as FW VERSION does
    assert wait_until(lambda: not info.busy)
    # Nothing new on the wire: PLAY_STOP to each address, 0x29 and 0x25 to
    # the lone USB board - exactly FW VERSION's scan.
    assert {f.cmd for f in bus.sent} <= {0x17, ota.CMD_OTA_QUERY, 0x25}
    assert {f.dest for f in bus.sent if f.cmd != 0x17} == {1}
    app.draw()
    assert "FW V1.4 16-color (FW_260923+)" in info.texts()
    app.handle("key2")
    assert app.screen is Screen.MENU
    assert runner.stops == 1


def test_keys_wait_while_the_fw_read_holds_the_port(tmp_path):
    info, _ = make_info(tmp_path)
    app, _ = make_app(info)
    info.versions.phase = "scanning"          # as if mid-scan
    info._fw_asked = True
    app.screen = Screen.BOARDINFO
    app.handle("key2")
    assert app.screen is Screen.BOARDINFO
    info.versions.phase = DONE
    app.handle("key2")
    assert app.screen is Screen.MENU


def test_while_the_pc_drives_nothing_touches_the_port(tmp_path):
    for remote in (FakeRemote(active=True), FakeRemote(owned=True)):
        bus = alone(1)
        info, seen = make_info(tmp_path, bus=bus)
        app, runner = make_app(info, remote=remote)
        app.screen = Screen.MENU
        open_board_info(app)
        assert app.screen in (Screen.BOARDINFO, Screen.REMOTE)
        assert runner.stops == 0
        assert info.versions.phase != "scanning"
        assert bus.sent == []
        assert seen == ["/dev/ttyACM0"]         # the serial still reads
        assert info.texts()[:2] == ["SERIAL 5CF26F473930",
                                    "TYPE 3930 (differs from most: 324C)"]
        assert info.texts()[2] == f"FW {FW_NOT_WHILE_PC}"
        assert info.texts()[2] == "FW (not read while the PC is driving)"


def test_key1_reads_again(tmp_path):
    info, seen = make_info(tmp_path)
    app, _ = make_app(info)
    open_board_info(app)
    assert wait_until(lambda: not info.busy)
    app.handle("key1")
    assert wait_until(lambda: not info.busy)
    assert len(seen) == 2


# ---- the screen ----

def test_the_screen_renders_and_every_line_fits(tmp_path):
    info, _ = make_info(tmp_path, record="FW_260923/x.bin")
    info.read(read_fw=True)
    assert wait_until(lambda: info.versions.phase == DONE)
    image = render.boardinfo_screen(info.lines(), False,
                                    usb_line=info.usb_line(), host="radxa-01")
    assert image.size == (WIDTH, HEIGHT)
    # Each value wraps into its column - read in full, never cut off.
    for key, value, _ in info.lines():
        width = WIDTH - 16 - (62 if key else 0)
        for font in (render.FONT_M, render.FONT_S):
            for line in render._wrap(value, font, width):
                assert font.getlength(line) <= width, (value, line)
    usual, _ = make_info(tmp_path, usb=USUAL)
    usual.read(read_fw=False)
    other = render.boardinfo_screen(usual.lines(), False, host="radxa-01")
    assert other.tobytes() != image.tobytes()
    reading = render.boardinfo_screen(info.lines(), True, host="radxa-01")
    assert reading.tobytes() != image.tobytes()


def test_the_app_draws_board_info_and_redraws_when_fw_lands(tmp_path):
    info, _ = make_info(tmp_path)
    app, _ = make_app(info)
    open_board_info(app)
    app.draw()
    first = app._display_key()
    assert wait_until(lambda: not info.busy)
    assert app._display_key() != first
    app.draw()


# ---- /status usb_board ----

def test_status_usb_board_is_read_after_each_open_and_cached(monkeypatch):
    from tests.test_ui_remote import make_session
    from ui import runner as runner_mod

    reads = []

    def board_info(port):
        reads.append(port)
        return dict(ODD)
    session, runner, _ = make_session(usb_board_info=board_info)
    assert session.status()["usb_board"] is None      # no worker has had it
    assert reads == []
    runner._saw_port("/dev/ttyACM0")                  # what a worker's open does
    assert session.status()["usb_board"] == {"serial": "5CF26F473930",
                                             "family": "3930"}
    session.status()
    session.status()
    assert reads == ["/dev/ttyACM0"]                  # cached, not re-read
    runner._saw_port("/dev/ttyACM1")                  # another open
    assert session.status()["usb_board"]["serial"] == "5CF26F473930"
    assert reads == ["/dev/ttyACM0", "/dev/ttyACM1"]
    monkeypatch.setattr(runner_mod, "USB_BOARD_RECHECK_S", 0.0)
    session.status()                                  # stale: read again
    assert len(reads) == 3


def test_status_usb_board_never_raises():
    from tests.test_ui_remote import make_session

    def boom(port):
        raise OSError("sysfs went away")
    session, runner, _ = make_session(usb_board_info=boom)
    runner._saw_port("/dev/ttyACM0")
    assert session.status()["usb_board"] == {"serial": None, "family": None}


def test_a_worker_opening_the_port_is_what_marks_it():
    from tests.test_ui_remote import make_session
    from tests.test_ui_remote import array as picture

    session, runner, _ = make_session(
        usb_board_info=lambda port: {"serial": port and "48E8854C324C",
                                     "family": "324C"})
    session.prepare("c1", {1: picture(3)}, label="x")
    assert wait_until(lambda: session.status()["usb_board"] is not None)
    assert session.status()["usb_board"] == {"serial": "48E8854C324C",
                                             "family": "324C"}
    runner.stop()


# ---- UPDATE FW shows SERIAL and TYPE before KEY1 ----

def test_the_update_screen_names_the_usb_board_before_a_write(tmp_path):
    image = tmp_path / "fw.bin"
    image.write_bytes(b"\x01" * 64)
    updater, calls = make_updater(image, bus=alone(1),
                                  board_info=lambda port: dict(ODD))
    app = App(NullDisplay(), ScriptedInput(()), FakeRunner(),
              port_label="/dev/fake", updater=updater)
    app.select("update")
    app.handle("key1")                       # opens the confirm screen
    assert app.screen is Screen.UPDATE
    assert wait_until(lambda: updater.usb_board is not None
                      and updater.board_state.endswith("(auto)"))
    assert calls["flash"] == []              # nothing written yet
    assert updater.usb_board["serial"] == "5CF26F473930"
    text, tint = render.usb_board_line(updater.usb_board)
    assert text == "SERIAL 5CF26F473930  TYPE 3930" and tint == render.WARN
    assert render.usb_board_line(USUAL) == ("SERIAL 48E8854C324C  TYPE 324C",
                                            render.DIM)
    assert render.usb_board_line(NONE)[0] == "SERIAL none - no USB serial"
    with_line = app.frame()
    without = render.update_screen(updater.firmware_label, updater.size,
                                   updater.addr, updater.phase,
                                   updater.board_state, 0,
                                   updater.recent(6), host=None)
    assert with_line.tobytes() != without.tobytes()
    for text in ("SERIAL 5CF26F473930  TYPE 3930",
                 "SERIAL 48E8854C324C  TYPE 324C"):
        assert render.FONT_S.getlength(text) <= WIDTH - 16
