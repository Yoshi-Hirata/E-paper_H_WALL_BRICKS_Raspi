"""The host-side flash record: what each board (by USB serial) last got.

The boards cannot report their build, so UPDATE FW writes the accepted
image against the USB serial number and FW VERSION reads it back.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

import ota
from ui import flashlog
from ui.updater import DONE
from ui.versions import DONE as SCAN_DONE
from tests.test_ui_runner import wait_until
from tests.test_ui_update import FakeOtaBus, make_image, make_updater
from tests.test_ui_versions import NEW, alone, make_versions

SERIAL = "48EC7570324C"
WHEN = time.mktime((2026, 9, 17, 17, 19, 19, 0, 0, -1))


def test_record_lookup_and_describe_roundtrip(tmp_path):
    path = tmp_path / "state" / "flash-log.json"
    assert flashlog.lookup(SERIAL, path) is None
    assert flashlog.lookup(None, path) is None
    entry = flashlog.record(SERIAL, 1, "FW_260917/MCB_e16_2029.09.17.bin",
                            64188, 0x2008, path=path, when=WHEN)
    assert flashlog.lookup(SERIAL, path) == entry
    assert flashlog.describe(entry) == "flashed FW_260917 09-17 17:19"
    assert flashlog.describe(None) == "no flash record here"
    # A later flash of the same board replaces the entry.
    flashlog.record(SERIAL, 1, "FW_260903/OTA_16c.bin", 65544, 0x2B93,
                    path=path, when=WHEN + 60)
    assert flashlog.lookup(SERIAL, path)["image"].startswith("FW_260903")
    assert len(flashlog.load(path)) == 1


def test_garbage_or_missing_file_reads_as_empty(tmp_path):
    path = tmp_path / "flash-log.json"
    assert flashlog.load(path) == {}
    path.write_text("[not a dict]", encoding="utf-8")
    assert flashlog.load(path) == {}
    path.write_text("{ broken", encoding="utf-8")
    assert flashlog.load(path) == {}


def test_update_records_the_accepted_image_against_the_usb_serial(tmp_path):
    image = make_image(tmp_path)
    path = tmp_path / "flash-log.json"
    updater, _ = make_updater(image, serial_of=lambda port: SERIAL,
                              flash_log=path)
    updater.start()
    assert wait_until(lambda: updater.phase == DONE)
    entry = flashlog.lookup(SERIAL, path)
    assert entry["addr"] == 1
    assert entry["image"] == "FW_260903/OTA_16c.bin"
    assert (entry["size"], entry["crc"]) == ota.image_fingerprint(
        image.read_bytes())
    assert any("recorded FW_260903" in line for line in updater.recent(10))


def test_no_usb_serial_means_no_record_and_no_failure(tmp_path):
    image = make_image(tmp_path)
    path = tmp_path / "flash-log.json"
    updater, _ = make_updater(image, serial_of=lambda port: None,
                              flash_log=path)
    updater.start()
    assert wait_until(lambda: updater.phase == DONE)
    assert not path.exists()
    assert any("not recorded" in line for line in updater.recent(10))


def test_fw_version_names_the_recorded_image_for_the_usb_board(tmp_path):
    path = tmp_path / "flash-log.json"
    flashlog.record(SERIAL, 2, "FW_260917/MCB_e16_2029.09.17.bin",
                    64188, 0x2008, path=path, when=WHEN)
    # A V1.1 board (refuses 0x25) that this unit flashed with FW_260917.
    versions = make_versions(tmp_path, bus=alone(2, v14=False),
                             serial_of=lambda port: SERIAL, flash_log=path)
    versions.scan()
    assert wait_until(lambda: versions.phase == SCAN_DONE)
    assert versions.rows == [(2, "V1.1 16-color, flashed FW_260917 09-17 17:19")]
    assert versions.status == f"USB {SERIAL}"
    # A board this unit never flashed says so instead of guessing.
    other = make_versions(tmp_path, bus=alone(2, v14=False),
                          serial_of=lambda port: "48EB685C324C",
                          flash_log=path)
    other.scan()
    assert wait_until(lambda: other.phase == SCAN_DONE)
    assert other.rows == [(2, "V1.1 16-color, no flash record here")]
    # A V1.0 board or a real size/crc answer is labelled as before.
    v10 = make_versions(tmp_path, bus=alone(2, v10=True),
                        serial_of=lambda port: SERIAL, flash_log=path)
    v10.scan()
    assert wait_until(lambda: v10.phase == SCAN_DONE)
    assert v10.rows == [(2, "V1.0 6-color (no OTA)")]
    named = make_versions(tmp_path, bus=alone(2, firmware=NEW),
                          serial_of=lambda port: SERIAL, flash_log=path)
    named.scan()
    assert wait_until(lambda: named.phase == SCAN_DONE)
    assert named.rows == [(2, "FW_260917")]
