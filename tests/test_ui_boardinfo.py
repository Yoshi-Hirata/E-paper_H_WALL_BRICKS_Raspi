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


class FakeCache:
    """The runner's side of it: usb_board() / note_usb_board()."""

    def __init__(self, board=None):
        self.board = board
        self.noted = []

    def usb_board(self):
        return None if self.board is None else dict(self.board)

    def note_usb_board(self, info):
        self.noted.append(dict(info))


def make_info(tmp_path, usb=ODD, bus=None, record=None, versions=True,
              cache=None, board_info=None):
    log = tmp_path / "flash.json"
    if record:
        flashlog.record(usb["serial"], 1, record, 100, 0x1234, path=log,
                        when=AT_1800)
    seen = []

    def read(port):
        seen.append(port)
        return dict(usb)
    v = (make_versions(tmp_path, bus=bus or alone(1), flash_log=log,
                       serial_of=lambda port: usb["serial"])
         if versions else None)
    info = BoardInfo(v, locate=lambda: "/dev/ttyACM0",
                     board_info=board_info or read, flash_log=log,
                     cache=cache)
    return info, seen


def settle(info):
    """The screen's own USB read and FW VERSION's scan have both landed."""
    return wait_until(lambda: info.usb_state != "reading" and not info.busy
                      and (info.versions is None
                           or info.versions.phase != "scanning"))


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
    assert settle(info)
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
    assert settle(info)
    assert info.texts() == ["SERIAL 48E8854C324C", "TYPE 324C",
                            "FW V1.1 16-color", "no flash record here"]
    assert [tone for _, _, tone in info.lines()][1] == ""


def test_no_board_on_usb_says_so(tmp_path):
    info, _ = make_info(tmp_path, usb=NONE, bus=FakeOtaBus(answers=set()))
    info.read(read_fw=True)
    assert settle(info)
    assert info.texts() == ["SERIAL none - no board on USB", "TYPE -",
                            "FW no board answers", "no flash record here"]


def test_with_the_485_in_fw_is_not_guessed(tmp_path):
    info, _ = make_info(tmp_path, bus=wall(tmp_path))
    info.read(read_fw=True)
    assert settle(info)
    assert info.texts()[2] == "FW 485 in: unplug it to read"


def test_type_text_for_every_family():
    assert type_text("324C") == "324C"
    assert type_text("3930") == "3930 (differs from most: 324C)"
    assert type_text("other") == "other (differs from most: 324C)"
    assert type_text(None) == "-"


# ---- the menu row and KEY handling ----

def test_board_info_is_the_last_menu_row(tmp_path):
    # Review of 59fbded, LOW-4: GIT PULL and REBOOT keep their rows.
    from ui.puller import RepoPuller
    from ui.rebooter import Rebooter

    info, _ = make_info(tmp_path)
    app = App(NullDisplay(), ScriptedInput(()), FakeRunner(),
              versions=info.versions, boardinfo=info, puller=RepoPuller(),
              rebooter=Rebooter())
    keys = [p.key for p in app.patterns]
    assert keys[-3:] == ["pull", "reboot", "boardinfo"], keys
    assert app.patterns[-1].label == "BOARD INFO"


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


def test_while_the_pc_drives_nothing_is_read_at_all(tmp_path):
    for remote in (FakeRemote(active=True), FakeRemote(owned=True)):
        bus = alone(1)
        cache = FakeCache({"serial": "5CF26F473930", "family": "3930"})
        info, seen = make_info(tmp_path, bus=bus, cache=cache)
        app, runner = make_app(info, remote=remote)
        app.screen = Screen.MENU
        open_board_info(app)
        assert app.screen in (Screen.BOARDINFO, Screen.REMOTE)
        assert runner.stops == 0
        assert info.versions.phase != "scanning"
        assert bus.sent == []
        time.sleep(0.1)
        assert seen == []                     # not even the USB descriptor
        # The runner's cached value is what is shown.
        assert info.texts()[:2] == ["SERIAL 5CF26F473930",
                                    "TYPE 3930 (differs from most: 324C)"]
        assert info.texts()[2] == f"FW {FW_NOT_WHILE_PC}"
        assert info.texts()[2] == "FW (not read while the PC is driving)"
    info, _ = make_info(tmp_path, cache=FakeCache(None))
    info.read(read_fw=False)
    assert info.texts()[0] == "SERIAL not read yet"


def test_a_hung_descriptor_read_never_holds_the_lcd(tmp_path):
    """Review of 59fbded, MED-1: a descriptor read waits behind a USB reset;
    the LCD loop (which pets the watchdog) must not wait with it."""
    import threading

    release = threading.Event()
    calls = []

    def hung(port):
        calls.append(port)
        release.wait(10)
        return dict(ODD)
    info, _ = make_info(tmp_path, board_info=hung, versions=False)
    info.read_timeout = 0.2
    started = time.monotonic()
    info.read(read_fw=True)
    assert time.monotonic() - started < 0.1        # the caller never waits
    assert info.texts()[0] == "SERIAL reading..."
    assert wait_until(lambda: info.usb_state == "busy")
    assert info.texts()[0] == "SERIAL USB busy - KEY1 to read again"
    info.read(read_fw=True)                        # still hung: not re-read
    assert wait_until(lambda: info.usb_state == "busy")
    assert len(calls) == 1
    release.set()
    time.sleep(0.05)
    info.read(read_fw=True)
    assert wait_until(lambda: info.usb_state == "read")
    assert info.texts()[0] == "SERIAL 5CF26F473930"


def test_the_screens_read_fills_the_runners_cache(tmp_path):
    cache = FakeCache()
    info, _ = make_info(tmp_path, cache=cache)
    info.read(read_fw=True)
    assert settle(info)
    assert cache.noted and cache.noted[-1]["serial"] == "5CF26F473930"


def test_key1_reads_again(tmp_path):
    info, seen = make_info(tmp_path)
    app, _ = make_app(info)
    open_board_info(app)
    assert settle(info)
    app.handle("key1")
    assert settle(info)
    assert len(seen) == 2


# ---- the screen ----

def test_the_screen_renders_and_every_line_fits(tmp_path):
    info, _ = make_info(tmp_path, record="FW_260923/x.bin")
    info.read(read_fw=True)
    assert settle(info)
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
    assert settle(info)
    assert app._display_key() != first
    app.draw()


# ---- /status usb_board (review of 59fbded, MED-1) ----

def _reader_session(monkeypatch, board_info):
    from tests.test_ui_remote import make_session
    from ui import runner as runner_mod

    monkeypatch.setattr(runner_mod, "USB_BOARD_POLL_S", 0.02)
    session, runner, _ = make_session(usb_board_info=board_info)
    return session, runner


def test_status_only_ever_returns_the_cache(monkeypatch):
    reads = []

    def board_info(port):
        reads.append(port)
        return dict(ODD)
    session, runner = _reader_session(monkeypatch, board_info)
    runner._saw_port("/dev/ttyACM0")           # an open, but no reader yet
    for _ in range(5):
        assert session.status()["usb_board"] is None
    assert reads == []                         # /status read nothing
    runner._start_usb_board_reader()           # what the worker's open does
    assert wait_until(lambda: session.status()["usb_board"] is not None)
    assert session.status()["usb_board"] == {"serial": "5CF26F473930",
                                             "family": "3930"}
    time.sleep(0.1)
    assert reads == ["/dev/ttyACM0"]           # once per open, then cached
    runner._stop.set()


def test_status_answers_at_once_while_a_read_hangs_and_a_reopen_refreshes(
        monkeypatch):
    import threading

    release = threading.Event()
    reads = []

    def board_info(port):
        reads.append(port)
        if len(reads) > 1:
            release.wait(10)                   # hung behind a USB reset
            return dict(USUAL)
        return dict(ODD)
    session, runner = _reader_session(monkeypatch, board_info)
    runner._saw_port("/dev/ttyACM0")
    runner._start_usb_board_reader()
    assert wait_until(lambda: session.status()["usb_board"] is not None)
    runner._saw_port("/dev/ttyACM1")           # a reopen, renamed node
    assert wait_until(lambda: len(reads) == 2)  # the reader is stuck in it
    started = time.monotonic()
    status = session.status()
    assert time.monotonic() - started < 0.1, "status waited for the read"
    assert status["usb_board"]["serial"] == "5CF26F473930"   # the cache
    time.sleep(0.2)
    assert len(reads) == 2                     # no second read piled on
    release.set()                              # the reset is over
    assert wait_until(lambda: session.status()["usb_board"]["serial"]
                      == "48E8854C324C")       # the reopen refreshed it
    assert reads[-1] == "/dev/ttyACM1"
    runner._stop.set()


def test_nothing_is_read_while_a_show_plays_a_cue_is_armed_or_a_reset_runs(
        monkeypatch):
    reads = []

    def board_info(port):
        reads.append(port)
        return dict(ODD)
    session, runner = _reader_session(monkeypatch, board_info)
    runner.remote = session                    # as once the PC's worker runs
    for busy, clear in (
            (lambda: setattr(session, "playing", lambda: True),
             lambda: setattr(session, "playing", lambda: False)),
            (lambda: setattr(session, "due", lambda: ("c1", 1.0, 1, 3)),
             lambda: setattr(session, "due", lambda: None)),
            (lambda: setattr(runner, "_recovering", True),
             lambda: setattr(runner, "_recovering", False)),
            (lambda: setattr(runner, "_firing", True),
             lambda: setattr(runner, "_firing", False))):
        busy()
        runner._saw_port("/dev/ttyACM0")
        runner._start_usb_board_reader()
        time.sleep(0.15)
        assert reads == [], reads
        clear()
        assert wait_until(lambda: len(reads) == 1)
        reads.clear()
    runner._stop.set()


def test_a_stop_then_a_start_always_leaves_the_new_worker_a_reader(
        monkeypatch):
    """Re-review of 62293fb, LOW-c: the old reader must end and the new
    worker must get its own, however quickly the two follow each other."""
    reads = []

    def board_info(port):
        reads.append(port)
        return dict(ODD)
    session, runner = _reader_session(monkeypatch, board_info)
    runner._saw_port("/dev/ttyACM0")
    runner._start_usb_board_reader()             # worker 1's open
    assert wait_until(lambda: len(reads) == 1)
    old = runner._usb_board_thread
    runner._stop.set()                           # stop ...
    runner._stop.clear()                         # ... and start at once
    runner._saw_port("/dev/ttyACM1")             # worker 2's open
    runner._start_usb_board_reader()
    new = runner._usb_board_thread
    assert new is not old
    assert wait_until(lambda: not old.is_alive())    # the old one ended
    assert new.is_alive()                            # the new one did not
    assert wait_until(lambda: reads[-1:] == ["/dev/ttyACM1"])
    runner._saw_port("/dev/ttyACM2")             # a reopen under worker 2
    assert wait_until(lambda: reads[-1:] == ["/dev/ttyACM2"])
    runner._stop.set()
    assert wait_until(lambda: not new.is_alive())


def test_board_info_carries_on_when_no_thread_can_start(tmp_path, monkeypatch):
    """Re-review of 62293fb, LOW-b: nothing may reach App.handle."""
    import ui.boardinfo as boardinfo_mod

    class NoThread:
        def __init__(self, *a, **k):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")
    info, _ = make_info(tmp_path, versions=False)
    app, _ = make_app(info)
    import threading
    from types import SimpleNamespace

    # Only this module's view of `threading`, never the real module's.
    monkeypatch.setattr(boardinfo_mod, "threading",
                        SimpleNamespace(Thread=NoThread, Lock=threading.Lock))
    open_board_info(app)                         # must not raise
    assert app.screen is Screen.BOARDINFO
    assert info.texts()[0] == "SERIAL USB busy - KEY1 to read again"


def test_the_reader_never_raises(monkeypatch):
    def boom(port):
        raise OSError("sysfs went away")
    session, runner = _reader_session(monkeypatch, boom)
    runner.note_usb_board(dict(ODD))           # read once before
    runner._saw_port("/dev/ttyACM0")
    runner._start_usb_board_reader()
    time.sleep(0.15)
    # A read that fails leaves the cache as it was, and the reader lives on.
    assert session.status()["usb_board"]["serial"] == "5CF26F473930"
    assert runner._usb_board_thread.is_alive()
    runner._usb_board_info = lambda port: dict(USUAL)
    assert wait_until(lambda: session.status()["usb_board"]["serial"]
                      == "48E8854C324C")
    runner._stop.set()


def test_a_worker_opening_the_port_starts_the_reader(monkeypatch):
    from tests.test_ui_remote import array as picture

    session, runner = _reader_session(
        monkeypatch, lambda port: {"serial": port and "48E8854C324C",
                                   "family": "324C"})
    session.prepare("c1", {1: picture(3)}, label="x")
    assert wait_until(lambda: session.status()["usb_board"] is not None)
    assert session.status()["usb_board"] == {"serial": "48E8854C324C",
                                             "family": "324C"}
    runner.stop()


def test_the_screens_read_goes_into_the_status_cache(monkeypatch):
    session, runner = _reader_session(monkeypatch, lambda port: dict(NONE))
    runner.note_usb_board(dict(ODD))
    assert session.status()["usb_board"] == {"serial": "5CF26F473930",
                                             "family": "3930"}


def test_the_bounded_read_gives_up_and_never_piles_up():
    import threading

    from epaper.transport import usb_board_info_bounded

    release, guard, calls = threading.Event(), threading.Lock(), []

    def hung(port):
        calls.append(port)
        release.wait(10)
        return {"serial": "x"}
    started = time.monotonic()
    assert usb_board_info_bounded("/dev/a", 0.1, read=hung, guard=guard) is None
    assert time.monotonic() - started < 0.5
    assert usb_board_info_bounded("/dev/a", 0.1, read=hung, guard=guard) is None
    assert calls == ["/dev/a"]                 # in flight: not started again
    release.set()
    assert wait_until(lambda: not guard.locked())
    assert usb_board_info_bounded("/dev/a", 1.0, read=lambda p: {"serial": p},
                                  guard=guard) == {"serial": "/dev/a"}
    assert usb_board_info_bounded(
        "/dev/a", 1.0, read=lambda p: 1 / 0, guard=guard) is None


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


def test_with_the_485_in_the_usb_board_is_not_shown_as_the_target(tmp_path):
    """Review of 59fbded, LOW-3: the serial is the board on the USB cable,
    the flash target comes from the scan - with several boards answering it
    is named as the USB board and nothing more."""
    image = tmp_path / "fw.bin"
    image.write_bytes(b"\x01" * 64)
    updater, _ = make_updater(image, bus=wall(tmp_path), boards=[1, 2, 7, 20],
                              board_info=lambda port: dict(ODD))
    app = App(NullDisplay(), ScriptedInput(()), FakeRunner(),
              port_label="/dev/fake", updater=updater)
    app.select("update")
    app.handle("key1")
    assert wait_until(lambda: updater.bus_shared)
    assert render.usb_board_line(updater.usb_board, shared=True) == (
        "USB BOARD 5CF2…3930", render.DIM)
    shared = app.frame()
    alone_line = render.update_screen(
        updater.firmware_label, updater.size, updater.addr, updater.phase,
        updater.board_state, 0, updater.recent(6), usb_board=updater.usb_board)
    assert shared.tobytes() != alone_line.tobytes()
    assert ("update" in [p.key for p in app.patterns])


def test_the_update_screens_read_fills_the_status_cache(tmp_path):
    image = tmp_path / "fw.bin"
    image.write_bytes(b"\x01" * 64)
    cache = FakeCache()
    updater, _ = make_updater(image, bus=alone(1),
                              board_info=lambda port: dict(ODD))
    updater.usb_board_sink = cache.note_usb_board
    updater.scan()
    assert wait_until(lambda: cache.noted)
    assert cache.noted[-1]["serial"] == "5CF26F473930"
