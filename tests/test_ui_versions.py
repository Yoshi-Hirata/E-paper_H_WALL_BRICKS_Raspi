"""Firmware inventory: the FW VERSION menu row and ui/versions.py.

Boards are the fake OTA bus from the update tests, answering 0x29 the
way V1.0 (ACK_INVALID_CMD) and V1.1 (state + size + crc) firmware do,
so the scan, the labelling against the bundled images, and the screen
flow are covered without a wall.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

import ota
from epaper.protocol import ACK_INVALID_CMD, ACK_SUCCESS, Frame
from ui import render
from ui.app import App, Screen
from ui.config import HEIGHT, WIDTH
from ui.display import NullDisplay
from ui.inputs import ScriptedInput
from ui.patterns import PATTERNS
from ui.versions import DONE, IDLE, SCANNING, BoardVersions
from tests.test_ui_app import FakeRunner
from tests.test_ui_runner import wait_until
from tests.test_ui_update import FakeOtaBus

NEW = b"\x01" * 300          # stands in for FW_260917
OLD = b"\x02" * 200          # stands in for FW_260903


def make_catalog(tmp_path: Path) -> dict:
    (tmp_path / "FW_260903").mkdir(exist_ok=True)
    (tmp_path / "FW_260903" / "OTA_16c.bin").write_bytes(OLD)
    (tmp_path / "FW_260917").mkdir(exist_ok=True)
    (tmp_path / "FW_260917" / "MCB_e16.bin").write_bytes(NEW)
    (tmp_path / "FW_260917" / "MCB_e16.hex").write_bytes(b":00")
    return ota.bundled_images(tmp_path)


def make_versions(tmp_path, bus=None, boards=(1, 2, 7, 20), **kwargs):
    kwargs.setdefault("open_bus", lambda port: bus or FakeOtaBus())
    kwargs.setdefault("locate", lambda: "/dev/fake")
    kwargs.setdefault("echo_log", False)
    kwargs.setdefault("catalog", make_catalog(tmp_path))
    kwargs.setdefault("scan", lambda b, boards, progress=None: ota.scan(
        b, boards, timeout=0.02, progress=progress))
    return BoardVersions(boards=list(boards), **kwargs)


def make_app(versions):
    runner = FakeRunner()
    app = App(NullDisplay(), ScriptedInput(()), runner, port_label="/dev/fake",
              versions=versions, host="radxa-01")
    return app, runner


def wall(tmp_path):
    """Four boards on three firmwares, plus silence at every other addr."""
    new, old = ota.image_fingerprint(NEW), ota.image_fingerprint(OLD)
    return FakeOtaBus(answers={1, 2, 7, 20},
                      per_addr={1: new, 2: old, 7: ACK_INVALID_CMD})


# ---- host/ota.py ----

def test_bundled_images_are_keyed_by_size_and_crc(tmp_path):
    catalog = make_catalog(tmp_path)
    assert catalog[ota.image_fingerprint(NEW)] == "FW_260917"
    assert catalog[ota.image_fingerprint(OLD)] == "FW_260903"
    assert len(catalog) == 2                 # the .hex is not an image


def test_repo_catalog_names_both_shipped_images():
    catalog = ota.bundled_images(Path(__file__).resolve().parents[1] / "FW")
    assert {"FW_260903", "FW_260917"} <= set(catalog.values())


def test_identify_names_the_firmware_from_the_0x29_answer(tmp_path):
    catalog = make_catalog(tmp_path)
    size, crc = ota.image_fingerprint(NEW)
    ack = Frame(dest=0, src=1, dev_type=0xFF, cmd=ACK_SUCCESS,
                data=bytes([0]) + size.to_bytes(4, "little")
                + crc.to_bytes(2, "little"))
    assert ota.identify(ack, catalog) == "FW_260917"
    blank = Frame(dest=0, src=1, dev_type=0xFF, cmd=ACK_SUCCESS,
                  data=bytes(7))
    assert ota.identify(blank, catalog) == "V1.1 16-color, build unknown"
    other = Frame(dest=0, src=1, dev_type=0xFF, cmd=ACK_SUCCESS,
                  data=bytes([1]) + (1234).to_bytes(4, "little")
                  + (0xBEEF).to_bytes(2, "little"))
    assert ota.identify(other, catalog) == "V1.1 size=1234 crc=0xBEEF RECEIVING"
    v10 = Frame(dest=0, src=1, dev_type=0xFF, cmd=ACK_INVALID_CMD)
    assert ota.identify(v10, catalog) == "V1.0 6-color (no OTA)"
    assert ota.identify(None, catalog) == "no reply"


def test_scan_reports_progress_per_address():
    bus = FakeOtaBus(answers={7})
    seen = []
    found = ota.scan(bus, [1, 7, 20], timeout=0.02,
                     progress=lambda addr, ack: seen.append((addr, ack is not None)))
    assert list(found) == [7]
    assert seen == [(1, False), (7, True), (20, False)]


# ---- the worker ----

def test_scan_lists_every_answering_board_with_its_firmware(tmp_path):
    versions = make_versions(tmp_path, bus=wall(tmp_path))
    assert versions.phase == IDLE
    versions.scan()
    assert wait_until(lambda: versions.phase == DONE)
    assert versions.rows == [(1, "FW_260917"), (2, "FW_260903"),
                             (7, "V1.0 6-color (no OTA)"),
                             (20, "V1.1 16-color, build unknown")]
    assert versions.status == "4/4 boards answer"
    assert versions.asked == 4
    assert versions.bundled == "FW_260917"
    assert any("board 07: V1.0" in line for line in versions.recent(10))


def test_silent_wall_and_missing_port_are_said_on_screen(tmp_path):
    quiet = make_versions(tmp_path, bus=FakeOtaBus(answers=set()))
    quiet.scan()
    assert wait_until(lambda: quiet.phase == DONE)
    assert quiet.rows == [] and quiet.status == "no board answers 0x29"
    unplugged = make_versions(tmp_path, locate=lambda: None)
    unplugged.scan()
    assert wait_until(lambda: unplugged.phase == DONE)
    assert unplugged.status == "no serial port"


def test_scroll_is_clamped_to_the_rows(tmp_path):
    versions = make_versions(tmp_path)
    versions.rows = [(n, "x") for n in range(1, 16)]
    versions.phase = DONE
    versions.scroll(-1, 10)
    assert versions.offset == 0
    for _ in range(20):
        versions.scroll(+1, 10)
    assert versions.offset == 5


def test_menu_entry_names_the_address_range(tmp_path):
    versions = make_versions(tmp_path, boards=range(1, 21))
    assert versions.menu_entry.key == "versions"
    assert versions.menu_entry.label == "FW VERSION"
    assert "01-20" in versions.menu_entry.detail


# ---- the App ----

def test_versions_row_exists_only_with_a_worker(tmp_path):
    plain = App(NullDisplay(), ScriptedInput(()), FakeRunner())
    assert [p.key for p in plain.patterns] == [p.key for p in PATTERNS]
    app, _ = make_app(make_versions(tmp_path))
    assert app.patterns[-1].key == "versions"


def test_entering_stops_the_runner_scans_and_key2_leaves_via_standby(tmp_path):
    versions = make_versions(tmp_path, bus=wall(tmp_path))
    app, runner = make_app(versions)
    app.select("versions")
    app.handle("key1")
    assert app.screen is Screen.VERSIONS
    assert runner.stops == 1
    assert wait_until(lambda: versions.phase == DONE)
    assert len(versions.rows) == 4
    app.handle("key2")
    assert app.screen is Screen.MENU
    assert runner.standbys == 1


def test_buttons_wait_for_the_scan_then_key1_rescans(tmp_path):
    release = threading.Event()
    calls = {"scans": 0}

    def slow_scan(bus, boards, progress=None):
        calls["scans"] += 1
        release.wait(5.0)
        return {}

    versions = make_versions(tmp_path, scan=slow_scan)
    app, runner = make_app(versions)
    app.select("versions")
    app.handle("key1")
    assert wait_until(lambda: versions.phase == SCANNING)
    for event in ("key2", "key1", "down", "key1_hold"):
        app.handle(event)
    assert app.screen is Screen.VERSIONS
    assert runner.standbys == 0 and runner.starts == []
    release.set()
    assert wait_until(lambda: versions.phase == DONE)
    app.handle("key1")
    assert wait_until(lambda: calls["scans"] == 2)


def test_screen_redraws_as_boards_answer(tmp_path):
    versions = make_versions(tmp_path)
    versions.phase = DONE
    app, _ = make_app(versions)
    app.screen = Screen.VERSIONS
    app.draw()
    before = app.display.frames
    versions.rows.append((3, "FW_260917"))
    app.tick(wait=0.0)
    assert app.display.frames == before + 1
    app.tick(wait=0.0)
    assert app.display.frames == before + 1


# ---- rendering ----

def test_versions_screen_renders_every_phase_and_scrolls():
    rows = [(n, "FW_260917" if n % 2 else "FW_260903") for n in range(1, 21)]
    for phase in ("idle", "scanning", "done"):
        image = render.versions_screen(rows, "20/20 boards answer", phase,
                                       "FW_260917", host="radxa-01")
        assert image.size == (WIDTH, HEIGHT)
    top = render.versions_screen(rows, "", "done", "FW_260917", offset=0)
    down = render.versions_screen(rows, "", "done", "FW_260917", offset=10)
    assert top.tobytes() != down.tobytes()
    empty = render.versions_screen([], "no serial port", "done", "none")
    assert empty.size == (WIDTH, HEIGHT)
