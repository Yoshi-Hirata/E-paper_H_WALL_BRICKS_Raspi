"""Firmware update mode: the UPDATE FW menu row and ui/updater.py.

The flashing itself is host/ota.py; here it is driven through fakes so
the flow - stop the runner, pick a board, flash, wait for the reboot,
fall back to a USB rebind, return via standby - is covered without a
board on the bench.
"""

from __future__ import annotations

import struct
import sys
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

import serial

import ota
from epaper.protocol import ACK_FAIL, ACK_INVALID_CMD, ACK_SUCCESS, Frame
from ui import render, updater as updater_mod
from ui.app import App, Screen
from ui.config import HEIGHT, WIDTH
from ui.display import NullDisplay
from ui.inputs import ScriptedInput
from ui.patterns import PATTERNS
from ui.updater import DONE, FAILED, FLASHING, IDLE, FirmwareUpdater
from tests.test_ui_app import FakeRunner
from tests.test_ui_runner import wait_until


# ---- fakes ----

class FakeOtaBus:
    """Answers the OTA commands the way a V1.1 board does.

    Every frame gets an ACK_SUCCESS unless `nak` maps its command to
    another ACK; `finish` decides what follows 0x28: "vanish" raises the
    Linux disconnect (the success signature), an ACK code rejects it.
    """

    def __init__(self, state: int = 0x00, nak: dict | None = None,
                 finish: str | int = "vanish", answers=None):
        self.state = state
        self.nak = nak or {}
        self.finish = finish
        self.answers = None if answers is None else set(answers)
        self.sent: list[Frame] = []
        self._pending: deque = deque()
        self.closed = False

    def send(self, frame: Frame) -> None:
        self.sent.append(frame)
        if self.answers is not None and frame.dest not in self.answers:
            return                       # nobody at that address
        if frame.cmd == ota.CMD_OTA_FINISH:
            if self.finish == "vanish":
                self._pending.append(serial.SerialException("device disconnected"))
            else:
                self._pending.append(self._ack(frame, self.finish))
            return
        cmd = self.nak.get(frame.cmd, ACK_SUCCESS)
        data = b""
        if frame.cmd == ota.CMD_OTA_QUERY and cmd == ACK_SUCCESS:
            data = bytes([self.state]) + struct.pack("<IH", 0, 0)
        self._pending.append(self._ack(frame, cmd, data))

    @staticmethod
    def _ack(frame: Frame, cmd: int, data: bytes = b"") -> Frame:
        return Frame(dest=0x00, src=frame.dest, dev_type=0xFF, cmd=cmd,
                     data=data)

    def recv(self, timeout: float = 0.5):
        if not self._pending:
            import time
            time.sleep(min(timeout, 0.01))   # silence, without spinning
            return None
        item = self._pending.popleft()
        if isinstance(item, Exception):
            raise item
        return item

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True


def make_image(tmp_path: Path, size: int = 150) -> Path:
    folder = tmp_path / "FW_260903"
    folder.mkdir()
    image = folder / "OTA_16c.bin"
    image.write_bytes(bytes(range(256)) * (size // 256 + 1))
    image.write_bytes(image.read_bytes()[:size])
    return image


def make_updater(image, bus=None, **kwargs):
    """Updater with instant fakes; `flash`/`verify` default to success."""
    calls = {"flash": [], "verify": [], "rebind": 0}

    def fake_flash(bus, addr, image, log, progress):
        calls["flash"].append(addr)
        log("Image: fake")
        progress(len(image) // 2, len(image))
        progress(len(image), len(image))
        return True

    def fake_verify(addr, port_hint, wait_s, log, open_bus, locate):
        calls["verify"].append(addr)
        log("fake verify")
        return True

    kwargs.setdefault("flash", fake_flash)
    kwargs.setdefault("verify", fake_verify)
    kwargs.setdefault("rebind", None)
    kwargs.setdefault("open_bus", lambda port: bus or FakeOtaBus())
    kwargs.setdefault("locate", lambda: "/dev/fake")
    kwargs.setdefault("echo_log", False)
    kwargs.setdefault("boards", [1, 20])
    return FirmwareUpdater(image, **kwargs), calls


def make_app(updater=None, events=()):
    runner = FakeRunner()
    app = App(NullDisplay(), ScriptedInput(events), runner,
              port_label="/dev/fake", updater=updater)
    return app, runner


# ---- firmware discovery ----

def test_newest_ota_image_is_offered_and_hex_is_not(tmp_path):
    (tmp_path / "FW_260801").mkdir()
    (tmp_path / "FW_260801" / "OTA_6c.bin").write_bytes(b"old")
    (tmp_path / "FW_260801" / "SWD_6c.hex").write_bytes(b":00")
    (tmp_path / "FW_260903").mkdir()
    (tmp_path / "FW_260903" / "OTA_16c.bin").write_bytes(b"new")
    (tmp_path / "FW_260903" / "SWD_16c.hex").write_bytes(b":00")
    assert updater_mod.find_firmware(tmp_path).name == "OTA_16c.bin"
    assert updater_mod.find_firmware(tmp_path).parent.name == "FW_260903"
    assert updater_mod.find_firmware(tmp_path / "missing") is None


def test_vendor_named_image_in_a_newer_folder_wins(tmp_path):
    # FW_260917 came with the vendor's own file names, kept as delivered;
    # the folder date decides, not the file name.
    (tmp_path / "FW_260903").mkdir()
    (tmp_path / "FW_260903" / "OTA_16c.bin").write_bytes(b"old")
    (tmp_path / "FW_260917").mkdir()
    (tmp_path / "FW_260917" / "MCB_e16_2029.09.17.bin").write_bytes(b"new")
    (tmp_path / "FW_260917" / "MCB_e16_2029.09.17.hex").write_bytes(b":00")
    (tmp_path / "FW_260917" / "16_Color_Chart.xlsx").write_bytes(b"PK")
    image = updater_mod.find_firmware(tmp_path)
    assert image.parent.name == "FW_260917"
    assert image.name == "MCB_e16_2029.09.17.bin"


def test_repo_ships_the_fw_260917_image():
    # The whole "copy the firmware to the appliance" step is git pull,
    # which only works if the image is in the tree.
    image = updater_mod.find_firmware()
    assert image is not None
    assert image.parent.name >= "FW_260917"
    assert 0 < image.stat().st_size <= ota.MAX_IMAGE_SIZE


def test_menu_entry_names_the_image(tmp_path):
    image = make_image(tmp_path)
    updater, _ = make_updater(image)
    assert updater.menu_entry.key == "update"
    assert updater.menu_entry.detail == "FW_260903/OTA_16c.bin"
    none, _ = make_updater(None)
    assert "no firmware" in none.menu_entry.detail


# ---- updater flow ----

def test_update_flashes_verifies_and_reports_done(tmp_path):
    image = make_image(tmp_path)
    updater, calls = make_updater(image)
    updater.select(+1)                       # board 20
    assert updater.addr == 20
    updater.start()
    assert updater.busy
    assert wait_until(lambda: updater.phase == DONE)
    assert calls["flash"] == [20] and calls["verify"] == [20]
    assert updater.done == updater.size == image.stat().st_size
    assert updater.progress == 1.0
    assert updater.error is None
    assert any("board 20 updated" in line for line in updater.recent(10))
    assert not updater.busy and updater.finished


def test_failed_flash_is_reported_not_raised(tmp_path):
    image = make_image(tmp_path)

    def bad_flash(bus, addr, image, log, progress):
        log("OTA start failed: ACK_FAIL", error=True)
        return False

    updater, _ = make_updater(image, flash=bad_flash)
    updater.start()
    assert wait_until(lambda: updater.phase == FAILED)
    assert updater.error == "flash failed"
    lines = updater.recent(10)
    assert any("ERROR OTA start failed" in line for line in lines)
    # KEY1 on the result screen goes back to the confirm screen.
    updater.reset()
    assert updater.phase == IDLE and updater.error is None


def test_wedged_port_is_named_on_screen(tmp_path):
    image = make_image(tmp_path)

    def wedged(bus, addr, image, log, progress):
        raise serial.SerialTimeoutException("Write timeout")

    updater, _ = make_updater(image, flash=wedged)
    updater.start()
    assert wait_until(lambda: updater.phase == FAILED)
    assert "wedged" in updater.error


def test_silent_board_after_reboot_triggers_a_usb_rebind(tmp_path):
    # The Radxa's post-0x28 failure mode: the board reset but did not
    # re-enumerate. Cycling the xhci controller brought it back both
    # times on 2026-09-11, so that is the fallback - once.
    image = make_image(tmp_path)
    answers = iter([False, True])
    calls = {"rebind": 0}

    def flaky_verify(addr, port_hint, wait_s, log, open_bus, locate):
        return next(answers)

    def rebind(log):
        calls["rebind"] += 1
        log("usb rebind: unbind xhci-hcd.41.auto")
        return True

    updater, _ = make_updater(image, verify=flaky_verify, rebind=rebind)
    updater.start()
    assert wait_until(lambda: updater.finished)
    assert updater.phase == DONE
    assert calls["rebind"] == 1


def test_rebind_that_does_not_help_fails_the_update(tmp_path):
    image = make_image(tmp_path)

    def never(addr, port_hint, wait_s, log, open_bus, locate):
        return False

    updater, _ = make_updater(image, verify=never, rebind=lambda log: True)
    updater.start()
    assert wait_until(lambda: updater.finished)
    assert updater.phase == FAILED
    assert "did not come back" in updater.error


def test_no_rebind_without_the_xhci_driver(tmp_path):
    log_lines = []
    ok = updater_mod.usb_rebind(lambda m, error=False: log_lines.append(m),
                                driver=tmp_path / "xhci-hcd")
    assert ok is False
    assert log_lines and "no xhci" in log_lines[0]


def test_probe_reports_the_ota_state_of_the_chosen_board(tmp_path):
    image = make_image(tmp_path)
    updater, _ = make_updater(image, bus=FakeOtaBus(state=0x00))
    updater.probe()
    assert wait_until(lambda: updater.board_state.startswith("IDLE"))
    # Old firmware answers ACK_INVALID_CMD to 0x29; say so.
    old, _ = make_updater(image, bus=FakeOtaBus(
        nak={ota.CMD_OTA_QUERY: ACK_INVALID_CMD}))
    old.probe()
    assert wait_until(lambda: old.board_state == "ACK_INVALID_CMD")


def test_scan_lists_only_the_boards_that_answer():
    bus = FakeOtaBus(answers={7})
    found = ota.scan(bus, [1, 7, 20], timeout=0.05)
    assert list(found) == [7]
    assert ota.describe_state(found[7]).startswith("IDLE")
    assert [f.dest for f in bus.sent] == [1, 7, 20]


def test_choose_target_wants_exactly_one_answer():
    ack = Frame(dest=0, src=7, dev_type=0xFF, cmd=ACK_SUCCESS)
    assert ota.choose_target({7: ack}) == (7, "board 07 found")
    addr, reason = ota.choose_target({})
    assert addr is None and "no board" in reason
    addr, reason = ota.choose_target({1: ack, 20: ack})
    assert addr is None and "01,20" in reason and "485" in reason


def test_cli_addr_accepts_auto_and_numbers():
    assert ota._addr("auto") == "auto"
    assert ota._addr("AUTO") == "auto"
    assert ota._addr("0x14") == 20 and ota._addr("7") == 7


def test_scan_picks_the_single_answering_board(tmp_path):
    # The USB board answers at its own DIP address; the operator does
    # not have to read the switches.
    image = make_image(tmp_path)
    updater, _ = make_updater(image, bus=FakeOtaBus(answers={20}))
    assert updater.addr == 1
    updater.scan()
    assert wait_until(lambda: updater.board_state.endswith("(auto)"))
    assert updater.addr == 20
    assert updater.board_state.startswith("IDLE")
    assert updater.found == {20: updater.board_state[:-7]}
    assert any("board 20 found" in line for line in updater.recent(5))


def test_scan_refuses_to_guess_between_several_boards(tmp_path):
    # Relayed boards answer too when the 485 cable is still plugged in.
    image = make_image(tmp_path)
    updater, _ = make_updater(image, bus=FakeOtaBus(answers={1, 20}))
    updater.scan()
    assert wait_until(lambda: "485" in updater.board_state)
    assert updater.addr == 1                  # unchanged: operator picks
    assert "01,20" in updater.board_state
    assert sorted(updater.found) == [1, 20]


def test_scan_with_no_board_says_so(tmp_path):
    image = make_image(tmp_path)
    updater, _ = make_updater(image, bus=FakeOtaBus(answers=set()))
    updater.scan()
    assert wait_until(lambda: updater.board_state == "no board answers 0x29")
    assert updater.found == {}


def test_entering_update_scans_and_up_down_override(tmp_path):
    updater, _ = make_updater(make_image(tmp_path),
                              bus=FakeOtaBus(answers={20}))
    app, _ = make_app(updater)
    app.select("update")
    app.handle("key1")
    assert wait_until(lambda: updater.addr == 20)
    assert updater.board_state.endswith("(auto)")
    app.handle("up")                          # override: board 1
    assert updater.addr == 1
    assert wait_until(lambda: updater.board_state == "no reply")


def test_probe_without_a_port_says_so(tmp_path):
    image = make_image(tmp_path)
    updater, _ = make_updater(image, locate=lambda: None)
    updater.probe()
    assert wait_until(lambda: updater.board_state == "no serial port")


def test_no_image_fails_immediately():
    updater, calls = make_updater(None)
    updater.start()
    assert updater.phase == FAILED
    assert calls["flash"] == []


# ---- the App ----

def test_update_row_exists_only_with_an_updater(tmp_path):
    plain, _ = make_app()
    assert [p.key for p in plain.patterns] == [p.key for p in PATTERNS]
    updater, _ = make_updater(make_image(tmp_path))
    app, _ = make_app(updater)
    assert app.patterns[-1].key == "update"
    assert len(app.patterns) == len(PATTERNS) + 1


def test_entering_update_stops_the_runner_and_frees_the_port(tmp_path):
    updater, _ = make_updater(make_image(tmp_path))
    app, runner = make_app(updater)
    app.handle("up")                         # wraps to the last row
    assert app.patterns[app.selected].key == "update"
    app.handle("key1")
    assert app.screen is Screen.UPDATE
    assert runner.stops == 1
    assert runner.starts == []
    assert updater.phase == IDLE


def test_up_down_pick_the_board_and_key2_leaves_via_standby(tmp_path):
    updater, _ = make_updater(make_image(tmp_path))
    app, runner = make_app(updater)
    app.select("update")
    app.handle("key1")
    app.handle("down")
    assert updater.addr == 20
    app.handle("up")
    assert updater.addr == 1
    app.handle("key2")
    assert app.screen is Screen.MENU
    # The flashed board reboots into its factory autoplay; standby is
    # what silences it and repaints white.
    assert runner.standbys == 1


def test_key1_flashes_and_buttons_are_ignored_meanwhile(tmp_path):
    import threading

    release = threading.Event()

    def slow_flash(bus, addr, image, log, progress):
        release.wait(5.0)
        progress(len(image), len(image))
        return True

    updater, _ = make_updater(make_image(tmp_path), flash=slow_flash)
    app, runner = make_app(updater)
    app.select("update")
    app.handle("key1")
    app.handle("key1")                       # flash
    assert wait_until(lambda: updater.phase == FLASHING)
    for event in ("key2", "key1", "down", "key1_hold", "press"):
        app.handle(event)
    assert app.screen is Screen.UPDATE       # KEY2 did not leave
    assert updater.addr == 1                 # DOWN did not move
    assert runner.starts == []               # the hold did not restart a demo
    release.set()
    assert wait_until(lambda: updater.phase == DONE)
    # Afterwards KEY1 returns to the confirm screen, KEY2 to the menu.
    app.handle("key1")
    assert updater.phase == IDLE
    app.handle("key2")
    assert app.screen is Screen.MENU


def test_update_screen_redraws_as_the_transfer_advances(tmp_path):
    updater, _ = make_updater(make_image(tmp_path))
    app, _ = make_app(updater)
    app.select("update")
    app.handle("key1")
    app.draw()
    before = app.display.frames
    updater.done = 60                        # a chunk acknowledged
    app.tick(wait=0.0)
    assert app.display.frames == before + 1
    app.tick(wait=0.0)                       # nothing changed: no repaint
    assert app.display.frames == before + 1


def test_key3_still_blanks_during_an_update(tmp_path):
    updater, _ = make_updater(make_image(tmp_path))
    app, _ = make_app(updater)
    app.select("update")
    app.handle("key1")
    app.handle("key3")
    assert app.blanked
    app.handle("key2")                       # wakes only
    assert app.screen is Screen.UPDATE


# ---- rendering ----

def test_update_screen_renders_every_phase():
    for phase in ("idle", "flashing", "verifying", "done", "failed"):
        image = render.update_screen("FW_260903/OTA_16c.bin", 65544, 1,
                                     phase, "IDLE size=0 crc=0x0000",
                                     32000, ["10:00:00 port /dev/ttyACM0"],
                                     error="x" if phase == "failed" else None)
        assert image.size == (WIDTH, HEIGHT)
    empty = render.update_screen("none", 0, 1, "idle", "", 0, [])
    assert empty.size == (WIDTH, HEIGHT)     # size 0 must not divide


def test_progress_changes_the_picture():
    a = render.update_screen("fw", 100, 1, "flashing", "", 10, [])
    b = render.update_screen("fw", 100, 1, "flashing", "", 90, [])
    assert a.tobytes() != b.tobytes()


# ---- host/ota.py through the fake bus ----

def test_flash_sends_start_chunks_finish_and_reports_progress():
    image = bytes(range(256)) * 2            # 512 B -> 9 chunks
    bus = FakeOtaBus()
    seen = []
    lines = []
    ok = ota.flash(bus, 1, image, log=lambda m, error=False: lines.append(m),
                   progress=lambda d, s: seen.append((d, s)))
    assert ok
    cmds = [f.cmd for f in bus.sent]
    assert cmds[0] == ota.CMD_OTA_QUERY
    assert cmds[1] == ota.CMD_OTA_START
    assert cmds.count(ota.CMD_OTA_DATA) == 9
    assert cmds[-1] == ota.CMD_OTA_FINISH
    assert seen[-1] == (512, 512)
    assert seen[0] == (60, 512)
    # Chunks carry their offset, so a resend is idempotent.
    first = [f for f in bus.sent if f.cmd == ota.CMD_OTA_DATA][1]
    assert struct.unpack("<I", first.data[1:5])[0] == 60
    assert any("accepted image" in line for line in lines)


def test_rejected_finish_fails_and_old_firmware_is_named():
    image = b"\x01" * 100
    rejected = FakeOtaBus(finish=ACK_FAIL)
    assert ota.flash(rejected, 1, image, log=lambda *a, **k: None,
                     progress=lambda d, s: None) is False
    old = FakeOtaBus(nak={ota.CMD_OTA_QUERY: ACK_INVALID_CMD,
                          ota.CMD_OTA_START: ACK_INVALID_CMD})
    errors = []

    def log(message, error=False):
        if error:
            errors.append(message)

    assert ota.flash(old, 1, image, log=log, progress=lambda d, s: None) is False
    assert any("no OTA support" in m for m in errors)


def test_verify_reopens_until_the_board_answers():
    attempts = iter([None, FakeOtaBus(state=0x00)])
    opened = []

    def open_bus(port):
        bus = next(attempts)
        opened.append(port)
        if bus is None:
            raise serial.SerialException("could not open port")
        return bus

    ok = ota.verify(1, None, wait_s=5.0, log=lambda *a, **k: None,
                    open_bus=open_bus, locate=lambda: "/dev/ttyACM0",
                    settle_s=0.0)
    assert ok
    assert len(opened) == 2
