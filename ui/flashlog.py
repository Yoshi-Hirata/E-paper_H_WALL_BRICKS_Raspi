"""Which image was last flashed into which board, by USB serial number.

The boards cannot say what firmware build they run (no version command;
0x29 reports size 0 after the reboot), so the host keeps the record:
every OTA that the board accepted (CRC verified by its bootloader,
then it answered 0x29 on the new firmware) is written here, keyed by
the USB serial number - the STM32's unique ID, which the USB-attached
board reports in its descriptor and never changes with firmware.

FW VERSION reads the record back for the board on USB. It is only as
complete as the flashes done from this unit: a board flashed elsewhere
shows "no flash record here".
"""

from __future__ import annotations

import json
import time
from pathlib import Path

DEFAULT_PATH = Path.home() / ".epaper" / "flash-log.json"


def load(path: Path = DEFAULT_PATH) -> dict[str, dict]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def record(serial: str, addr: int, image: str, size: int, crc: int,
           path: Path = DEFAULT_PATH, when: float | None = None) -> dict:
    """Remember that the board with this USB serial accepted `image`."""
    entry = {"addr": addr, "image": image, "size": size, "crc": crc,
             "when": when if when is not None else time.time()}
    data = load(path)
    data[serial] = entry
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, sort_keys=True),
                    encoding="utf-8")
    return entry


def lookup(serial: str | None, path: Path = DEFAULT_PATH) -> dict | None:
    if not serial:
        return None
    return load(path).get(serial)


def describe(entry: dict | None) -> str:
    """Short LCD text: 'flashed FW_260917 09-17 17:19' or the absence."""
    if not entry:
        return "no flash record here"
    stamp = time.strftime("%m-%d %H:%M", time.localtime(entry.get("when", 0)))
    image = str(entry.get("image", "?"))
    folder = image.split("/")[0] if "/" in image else image
    return f"flashed {folder} {stamp}"
