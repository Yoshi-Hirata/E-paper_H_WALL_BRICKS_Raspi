"""OTA firmware upgrade (protocol V1.1 §11, commands 0x26-0x29).

Usage:
    python host/ota.py --check --addr 1              # query OTA state only
    python host/ota.py FW.bin --addr 1               # flash board 1
    python host/ota.py FW.bin --addr 2               # flash board 2 (via relay)

Flow per spec 11.3: start (0x26, size+CRC) -> data (0x27, 60-byte
chunks, ACK each) -> finish (0x28). On success the device resets
WITHOUT acking, so a silent 0x28 means the upgrade was accepted; the
tool then re-probes with 0x29 to confirm the new firmware is alive.

Point-to-point only: broadcast is forbidden for OTA (spec 11.2).
Any mid-flight failure is recoverable by rerunning from 0x26.
"""

from __future__ import annotations

import argparse
import struct
import sys
import time

from epaper.protocol import (
    ACK_FAIL,
    ACK_INVALID_CMD,
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
             retries: int = 3) -> Frame | None:
    for attempt in range(1, retries + 1):
        bus.send(frame)
        ack = _ack_from(bus, frame.dest, timeout)
        if ack:
            return ack
        if attempt < retries:
            print(f"  timeout, retry {attempt}/{retries - 1}")
    return None


def query_state(bus: Bus, addr: int, quiet: bool = False) -> Frame | None:
    ack = _request(bus, _frame(addr, CMD_OTA_QUERY), timeout=0.8)
    if quiet:
        return ack
    if ack is None:
        print(f"Board 0x{addr:02X}: no reply to OTA query (0x29).")
    elif ack.cmd == ACK_SUCCESS and len(ack.data) >= 7:
        state, size, crc = ack.data[0], *struct.unpack("<IH", ack.data[1:7])
        print(f"Board 0x{addr:02X}: OTA state "
              f"{OTA_STATES.get(state, f'0x{state:02X}')}, "
              f"size={size}, crc=0x{crc:04X}")
    else:
        print(f"Board 0x{addr:02X}: {ack.describe()}")
    return ack


def _transfer(bus: Bus, addr: int, image: bytes) -> bool:
    """One 0x26 + all 0x27 chunks. False on any unrecovered chunk."""
    size = len(image)
    crc = crc16_modbus(image)

    # Start: device erases its staging area before acking and stops
    # draining USB meanwhile -- a resend into that window wedges the CDC
    # port (write timeout, board 2 2026-08-28). Single send, long wait.
    print(f"OTA start (0x26) -> board 0x{addr:02X}")
    ack = _request(bus, _frame(addr, CMD_OTA_START,
                               struct.pack("<IH", size, crc)),
                   timeout=30.0, retries=1)
    if ack is None or ack.cmd == ACK_INVALID_CMD:
        print(f"Board 0x{addr:02X} does not accept OTA start (0x26): "
              f"{ack.describe() if ack else 'no ACK'}. Current firmware "
              "has no OTA support -- flash via SWD with full_flash.hex.",
              file=sys.stderr)
        return False
    if ack.cmd != ACK_SUCCESS:
        print(f"OTA start failed: {ack.describe()}", file=sys.stderr)
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
                    print(f"\n  chunk @ {offset}: slow ACK {dt:.1f}s "
                          "(staging flush)")
                break
            print(f"\n  chunk @ {offset}: 45s silence, one resend")
        if ack is None or ack.cmd != ACK_SUCCESS:
            print(f"\nchunk @ {offset} failed: "
                  f"{ack.describe() if ack else 'no ACK'}", file=sys.stderr)
            return False
        done = offset + len(chunk)
        if done == size or (offset // CHUNK_SIZE) % 50 == 0:
            pct = done * 100 // size
            print(f"\r  {done}/{size} bytes ({pct}%)", end="", flush=True)
    print(f"\nTransfer done in {time.monotonic() - t0:.1f}s")
    return True


def flash(bus: Bus, addr: int, image: bytes) -> bool:
    print(f"Image: {len(image)} bytes, CRC16 0x{crc16_modbus(image):04X}, "
          f"{(len(image) + CHUNK_SIZE - 1) // CHUNK_SIZE} chunks")

    # Advisory preflight; 0x26 is the authoritative support test.
    ack = query_state(bus, addr, quiet=True)
    if ack is None or ack.cmd == ACK_INVALID_CMD:
        print(f"note: board 0x{addr:02X} ignores 0x29; "
              "probing with 0x26 anyway.")

    # Any mid-transfer failure restarts cleanly from 0x26 (spec 11.4).
    for round_no in range(1, 4):
        if round_no > 1:
            print(f"Restarting transfer from 0x26 (round {round_no}/3)")
            time.sleep(1.0)
        if _transfer(bus, addr, image):
            break
    else:
        print("Transfer failed after 3 rounds.", file=sys.stderr)
        return False

    # Finish: success = device resets silently; only failures ACK.
    print("OTA finish (0x28)")
    bus.send(_frame(addr, CMD_OTA_FINISH))
    ack = _ack_from(bus, addr, timeout=3.0)
    if ack is not None:
        print(f"OTA finish rejected: {ack.describe()} -- "
              "image incomplete or CRC mismatch; rerun from 0x26.",
              file=sys.stderr)
        return False
    print("No ACK to 0x28 -> board accepted image and is rebooting.")
    return True


def verify(addr: int, port_hint: str | None, wait_s: float = 25.0) -> bool:
    """Re-probe after reboot. The direct USB board re-enumerates, so
    reopen the port (it may briefly disappear) until 0x29 answers."""
    print(f"Waiting for board 0x{addr:02X} to come back...")
    deadline = time.monotonic() + wait_s
    time.sleep(3.0)  # bootloader copy + restart (spec 11.3 step 5)
    while time.monotonic() < deadline:
        port = port_hint or find_port()
        if port:
            try:
                with Bus(port, verbose=False) as bus:
                    ack = query_state(bus, addr, quiet=True)
                    if ack is not None and ack.cmd == ACK_SUCCESS:
                        print(f"Board 0x{addr:02X} is back on new firmware.")
                        return True
            except Exception:
                pass  # port vanished mid-open during re-enumeration
        time.sleep(1.0)
    print(f"Board 0x{addr:02X} did not answer within {wait_s:.0f}s. "
          "The board resets without signalling USB disconnect, so the "
          "host-side CDC port can wedge (open fails with 'device not "
          "functioning'): replug the USB cable, or run as admin\n"
          "  pnputil /restart-device <USB\\VID_0483&PID_5740\\...>\n"
          "then re-check with: python host/ota.py --check --addr N",
          file=sys.stderr)
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="OTA firmware upgrade (0x26-0x29)")
    ap.add_argument("bin", nargs="?", help="firmware .bin image")
    ap.add_argument("--addr", type=lambda v: int(v, 0), required=True,
                    help="target board address (point-to-point only)")
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

    if args.check:
        with Bus(port, verbose=False) as bus:
            ack = query_state(bus, args.addr)
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
    with Bus(port, verbose=False) as bus:
        ok = flash(bus, args.addr, image)
    if not ok:
        return 1
    return 0 if verify(args.addr, args.port) else 1


if __name__ == "__main__":
    sys.exit(main())
