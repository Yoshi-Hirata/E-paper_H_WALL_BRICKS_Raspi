"""OTA firmware upgrade (protocol V1.1 §11, commands 0x26-0x29).

Usage:
    python host/ota.py --check --addr 1              # query OTA state only
    python host/ota.py FW.bin --addr 1               # flash board 1
    python host/ota.py FW.bin --addr 2               # flash board 2 (via relay)
    python host/ota.py FW.bin --addr auto            # find the one board answering
    python host/ota.py --check --addr auto           # list every board answering

`--addr auto` scans 1..20 with the 0x29 state query and uses the single
address that answers - the USB-attached board's own DIP setting (all
DIP off answers as 1). With the 485 cable still connected the relayed
boards answer too, and the scan refuses to guess between them.

Flow per spec 11.3: start (0x26, size+CRC) -> data (0x27, 60-byte
chunks, ACK each) -> finish (0x28). On success the device resets
WITHOUT acking, so a silent 0x28 means the upgrade was accepted; the
tool then re-probes with 0x29 to confirm the new firmware is alive.

Point-to-point only: broadcast is forbidden for OTA (spec 11.2).
Any mid-flight failure is recoverable by rerunning from 0x26.

The flashing functions take `log` / `progress` callbacks so the LCD HAT
UI (ui/updater.py) can run the same code and show it on the screen;
the defaults print, which is what the CLI wants.
"""

from __future__ import annotations

import argparse
import struct
import sys
import time
from pathlib import Path
from typing import Callable

import serial

from epaper.protocol import (
    ACK_FAIL,
    ACK_INVALID_CMD,
    ACK_NAMES,
    ACK_SUCCESS,
    ADDR_PC,
    Frame,
    crc16_modbus,
)
from epaper.transport import Bus, find_port

CMD_OTA_START = 0x26
CMD_OTA_DATA = 0x27
CMD_OTA_FINISH = 0x28
CMD_OTA_QUERY = 0x29

CHUNK_SIZE = 60          # spec 11.2.2 recommends 60 (limit 61)
MAX_IMAGE_SIZE = 98304   # 96 KB (spec 11.1)

OTA_STATES = {0x00: "IDLE", 0x01: "RECEIVING", 0x02: "READY"}

SCAN_BOARDS = list(range(1, 21))   # the wall's address range
SCAN_TIMEOUT_S = 0.3               # a local answer arrives within ~50 ms

WEDGE_MSG = ("Serial write timed out: the board's CDC stopped draining "
             "USB (wedged port). Power-cycle the board or replug USB, "
             "then rerun; a mid-flight transfer restarts from 0x26.")

Log = Callable[..., None]              # log(message, error=False)
Progress = Callable[[int, int], None]  # progress(bytes_done, image_size)


def print_log(message: str, error: bool = False) -> None:
    """Default `log`: stdout for progress, stderr for failures."""
    print(message, file=sys.stderr if error else sys.stdout, flush=True)


def print_progress(done: int, size: int) -> None:
    """Default `progress`: one updating line every 50 chunks."""
    if done == size or (done // CHUNK_SIZE) % 50 == 0:
        print(f"\r  {done}/{size} bytes ({done * 100 // size}%)",
              end="\n" if done == size else "", flush=True)


def _frame(dest: int, cmd: int, data: bytes = b"") -> Frame:
    # OTA commands ignore DeviceType/group/chip fields (spec 11.2).
    return Frame(dest=dest, src=ADDR_PC, dev_type=0x00, cmd=cmd, data=data)


def _ack_from(bus: Bus, dest: int, timeout: float) -> Frame | None:
    """Next ACK frame originating from `dest`; other traffic is ignored."""
    deadline = time.monotonic() + timeout
    while (left := deadline - time.monotonic()) > 0:
        frame = bus.recv(timeout=left)
        if frame and frame.is_ack and frame.src == dest:
            return frame
    return None


def _request(bus: Bus, frame: Frame, timeout: float,
             retries: int = 3, log: Log = print_log) -> Frame | None:
    for attempt in range(1, retries + 1):
        bus.send(frame)
        ack = _ack_from(bus, frame.dest, timeout)
        if ack:
            return ack
        if attempt < retries:
            log(f"  timeout, retry {attempt}/{retries - 1}")
    return None


def describe_state(ack: Frame | None) -> str:
    """One line for a 0x29 answer: "IDLE size=0 crc=0x0000", "no reply",
    or the ACK name (ACK_INVALID_CMD = firmware without OTA support)."""
    if ack is None:
        return "no reply"
    if ack.cmd == ACK_SUCCESS and len(ack.data) >= 7:
        state, size, crc = ack.data[0], *struct.unpack("<IH", ack.data[1:7])
        return (f"{OTA_STATES.get(state, f'0x{state:02X}')} "
                f"size={size} crc=0x{crc:04X}")
    return ACK_NAMES.get(ack.cmd, f"cmd 0x{ack.cmd:02X}")


def image_fingerprint(image: bytes) -> tuple[int, int]:
    """(size, Modbus CRC16) - what 0x26 sends and 0x29 echoes back."""
    return len(image), crc16_modbus(image)


def bundled_images(directory: Path) -> dict[tuple[int, int], str]:
    """{(size, crc): folder name} for every FW_<yymmdd>/*.bin in the repo,
    so a board's 0x29 answer can be named after the image it was flashed
    with. A folder with several .bin files contributes all of them."""
    catalog: dict[tuple[int, int], str] = {}
    for path in sorted(Path(directory).glob("FW_*/*.bin")):
        try:
            catalog[image_fingerprint(path.read_bytes())] = path.parent.name
        except OSError:
            continue
    return catalog


def identify(ack: Frame | None, catalog: dict[tuple[int, int], str]) -> str:
    """Firmware label from a 0x29 answer, one short line for the LCD.

    The protocol has no version command, so the answer to the OTA state
    query is the fingerprint: V1.0 firmware rejects it (ACK_INVALID_CMD),
    V1.1 reports the size and CRC it was given at the last 0x26, which
    names the bundled image when it matches. A V1.1 board that reports
    size 0 (never OTA'd, or a build that clears the record on boot) is
    only "V1.1".
    """
    if ack is None:
        return "no reply"
    if ack.cmd == ACK_INVALID_CMD:
        return "V1.0 6-color (no OTA)"
    if ack.cmd == ACK_SUCCESS and len(ack.data) >= 7:
        state, size, crc = ack.data[0], *struct.unpack("<IH", ack.data[1:7])
        name = catalog.get((size, crc))
        busy = "" if state == 0x00 else f" {OTA_STATES.get(state, f'0x{state:02X}')}"
        if name:
            return f"{name}{busy}"
        if size == 0 and crc == 0:
            return f"V1.1 16-color, build unknown{busy}"
        return f"V1.1 size={size} crc=0x{crc:04X}{busy}"
    return ACK_NAMES.get(ack.cmd, f"cmd 0x{ack.cmd:02X}")


def query_state(bus: Bus, addr: int, quiet: bool = False,
                log: Log = print_log) -> Frame | None:
    ack = _request(bus, _frame(addr, CMD_OTA_QUERY), timeout=0.8, log=log)
    if quiet:
        return ack
    if ack is None:
        log(f"Board 0x{addr:02X}: no reply to OTA query (0x29).")
    elif ack.cmd == ACK_SUCCESS and len(ack.data) >= 7:
        log(f"Board 0x{addr:02X}: OTA state {describe_state(ack)}")
    else:
        log(f"Board 0x{addr:02X}: {ack.describe()}")
    return ack


def scan(bus: Bus, boards=SCAN_BOARDS,
         timeout: float = SCAN_TIMEOUT_S,
         progress: Callable[[int, Frame | None], None] | None = None
         ) -> dict[int, Frame]:
    """Which addresses answer the 0x29 state query: {addr: ack}.

    One query each, short wait: the USB-attached board answers its own
    DIP address locally within milliseconds, and everything else is
    relayed to the 485 bus, where a missing board is simply silence.
    The whole range costs about `timeout` x len(boards) when only one
    board is there. `progress(addr, ack)` is called after every address,
    answer or not, so a screen can fill in as the scan runs.
    """
    found: dict[int, Frame] = {}
    for addr in boards:
        bus.send(_frame(addr, CMD_OTA_QUERY))
        ack = _ack_from(bus, addr, timeout)
        if ack is not None:
            found[addr] = ack
        if progress is not None:
            progress(addr, ack)
    return found


def choose_target(found: dict) -> tuple[int | None, str]:
    """Pick the OTA target from a scan: (addr, reason).

    Exactly one answer is the point-to-point case the spec wants; none
    or several is a wiring situation the operator has to resolve, so
    the reason spells it out instead of guessing.
    """
    if len(found) == 1:
        addr = next(iter(found))
        return addr, f"board {addr:02d} found"
    if not found:
        return None, "no board answers 0x29"
    listed = ",".join(f"{a:02d}" for a in sorted(found))
    return None, f"boards {listed} answer: unplug 485 or pick one"


def _transfer(bus: Bus, addr: int, image: bytes, log: Log = print_log,
              progress: Progress = print_progress) -> bool:
    """One 0x26 + all 0x27 chunks. False on any unrecovered chunk."""
    size = len(image)
    crc = crc16_modbus(image)

    # Start: device erases its staging area before acking and stops
    # draining USB meanwhile -- a resend into that window wedges the CDC
    # port (write timeout, board 2 2026-08-28). Single send, long wait.
    log(f"OTA start (0x26) -> board 0x{addr:02X}")
    ack = _request(bus, _frame(addr, CMD_OTA_START,
                               struct.pack("<IH", size, crc)),
                   timeout=30.0, retries=1, log=log)
    if ack is None or ack.cmd == ACK_INVALID_CMD:
        log(f"Board 0x{addr:02X} does not accept OTA start (0x26): "
            f"{ack.describe() if ack else 'no ACK'}. Current firmware "
            "has no OTA support -- flash via SWD with full_flash.hex.",
            error=True)
        return False
    if ack.cmd != ACK_SUCCESS:
        log(f"OTA start failed: {ack.describe()}", error=True)
        return False

    # Data: the board goes silent for 9-17s roughly every 9.6KB while it
    # flushes staging flash (measured 2026-08-28, board 1). It still ACKs
    # afterwards, but frames resent during the stall wedge the transfer
    # (reproducibly died at the same chunk with quick retries). So: send
    # each chunk ONCE and wait out the stall; resend only after a long
    # quiet as a lost-frame fallback (chunks are offset-addressed, so a
    # resend is idempotent).
    t0 = time.monotonic()
    for offset in range(0, size, CHUNK_SIZE):
        chunk = image[offset:offset + CHUNK_SIZE]
        data = bytes([0]) + struct.pack("<I", offset) + chunk
        frame = _frame(addr, CMD_OTA_DATA, data)
        ack = None
        for _ in range(2):
            bus.send(frame)
            t1 = time.monotonic()
            ack = _ack_from(bus, addr, timeout=45.0)
            if ack is not None:
                if (dt := time.monotonic() - t1) > 0.5:
                    log(f"  chunk @ {offset}: slow ACK {dt:.1f}s "
                        "(staging flush)")
                break
            log(f"  chunk @ {offset}: 45s silence, one resend")
        if ack is None or ack.cmd != ACK_SUCCESS:
            log(f"chunk @ {offset} failed: "
                f"{ack.describe() if ack else 'no ACK'}", error=True)
            return False
        progress(offset + len(chunk), size)
    log(f"Transfer done in {time.monotonic() - t0:.1f}s")
    return True


def flash(bus: Bus, addr: int, image: bytes, log: Log = print_log,
          progress: Progress = print_progress) -> bool:
    log(f"Image: {len(image)} bytes, CRC16 0x{crc16_modbus(image):04X}, "
        f"{(len(image) + CHUNK_SIZE - 1) // CHUNK_SIZE} chunks")

    # Advisory preflight; 0x26 is the authoritative support test.
    ack = query_state(bus, addr, quiet=True, log=log)
    if ack is None or ack.cmd == ACK_INVALID_CMD:
        log(f"note: board 0x{addr:02X} ignores 0x29; "
            "probing with 0x26 anyway.")

    # Any mid-transfer failure restarts cleanly from 0x26 (spec 11.4).
    for round_no in range(1, 4):
        if round_no > 1:
            log(f"Restarting transfer from 0x26 (round {round_no}/3)")
            time.sleep(1.0)
        if _transfer(bus, addr, image, log, progress):
            break
    else:
        log("Transfer failed after 3 rounds.", error=True)
        return False

    # Finish: success = device resets silently; only failures ACK.
    log("OTA finish (0x28)")
    bus.send(_frame(addr, CMD_OTA_FINISH))
    try:
        ack = _ack_from(bus, addr, timeout=3.0)
    except serial.SerialException as exc:
        # Linux notices the reset: the CDC device vanishes under the
        # open port and the read raises "device disconnected". That is
        # the success signature (both boards, 2026-09-11), not a fault.
        log(f"Port vanished after 0x28 ({exc}) -> board accepted image "
            "and is rebooting.")
        return True
    if ack is not None:
        log(f"OTA finish rejected: {ack.describe()} -- "
            "image incomplete or CRC mismatch; rerun from 0x26.",
            error=True)
        return False
    log("No ACK to 0x28 -> board accepted image and is rebooting.")
    return True


def verify(addr: int, port_hint: str | None, wait_s: float = 25.0,
           log: Log = print_log, open_bus=None, locate=find_port,
           settle_s: float = 3.0) -> bool:
    """Re-probe after reboot. The direct USB board re-enumerates, so
    reopen the port (it may briefly disappear) until 0x29 answers."""
    open_bus = open_bus or (lambda p: Bus(p, verbose=False))
    log(f"Waiting for board 0x{addr:02X} to come back...")
    deadline = time.monotonic() + wait_s
    time.sleep(settle_s)  # bootloader copy + restart (spec 11.3 step 5)
    while time.monotonic() < deadline:
        port = port_hint or locate()
        if port:
            try:
                with open_bus(port) as bus:
                    ack = query_state(bus, addr, quiet=True, log=log)
                    if ack is not None and ack.cmd == ACK_SUCCESS:
                        log(f"Board 0x{addr:02X} is back on new firmware "
                            f"({describe_state(ack)}).")
                        return True
            except Exception:
                pass  # port vanished mid-open during re-enumeration
        time.sleep(1.0)
    log(f"Board 0x{addr:02X} did not answer within {wait_s:.0f}s. "
        "The board resets without signalling USB disconnect, so the "
        "host-side CDC port can wedge (open fails with 'device not "
        "functioning'): replug the USB cable, or run as admin\n"
        "  pnputil /restart-device <USB\\VID_0483&PID_5740\\...>\n"
        "then re-check with: python host/ota.py --check --addr N",
        error=True)
    return False


def _addr(value: str):
    if value.lower() == "auto":
        return "auto"
    return int(value, 0)


def main() -> int:
    ap = argparse.ArgumentParser(description="OTA firmware upgrade (0x26-0x29)")
    ap.add_argument("bin", nargs="?", help="firmware .bin image")
    ap.add_argument("--addr", type=_addr, required=True,
                    help="target board address (point-to-point only), or "
                         "'auto' to use the one board answering 0x29")
    ap.add_argument("--port", help="serial port (default: auto-detect)")
    ap.add_argument("--check", action="store_true",
                    help="only query OTA state (0x29), no flashing")
    args = ap.parse_args()

    if args.addr == 0xFF:
        print("Broadcast OTA is forbidden (spec 11.2).", file=sys.stderr)
        return 2

    port = args.port or find_port()
    if not port:
        print("No serial port found. Specify with --port.", file=sys.stderr)
        return 2

    if args.addr == "auto":
        try:
            with Bus(port, verbose=False) as bus:
                found = scan(bus)
        except serial.SerialTimeoutException:
            print(WEDGE_MSG, file=sys.stderr)
            return 1
        for addr in sorted(found):
            print(f"Board 0x{addr:02X}: OTA state {describe_state(found[addr])}")
        chosen, reason = choose_target(found)
        if args.check:
            print(reason)
            return 0 if found else 1
        if chosen is None:
            print(f"Cannot pick a target: {reason}.", file=sys.stderr)
            return 2
        print(reason)
        args.addr = chosen

    if args.check:
        try:
            with Bus(port, verbose=False) as bus:
                ack = query_state(bus, args.addr)
        except serial.SerialTimeoutException:
            print(WEDGE_MSG, file=sys.stderr)
            return 1
        return 0 if ack is not None and ack.cmd == ACK_SUCCESS else 1

    if not args.bin:
        ap.error("firmware .bin path required unless --check")
    with open(args.bin, "rb") as f:
        image = f.read()
    if not 0 < len(image) <= MAX_IMAGE_SIZE:
        print(f"Image size {len(image)} out of range (1..{MAX_IMAGE_SIZE}).",
              file=sys.stderr)
        return 2
    if args.bin.lower().endswith(".hex"):
        print("HEX files are for SWD burning; OTA needs the .bin.",
              file=sys.stderr)
        return 2

    print(f"Port: {port}, target board 0x{args.addr:02X}")
    try:
        with Bus(port, verbose=False) as bus:
            ok = flash(bus, args.addr, image)
    except serial.SerialTimeoutException:
        print(WEDGE_MSG, file=sys.stderr)
        return 1
    if not ok:
        return 1
    return 0 if verify(args.addr, args.port) else 1


if __name__ == "__main__":
    sys.exit(main())
