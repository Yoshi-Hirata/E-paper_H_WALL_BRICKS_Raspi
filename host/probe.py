"""Communication probe: try frame variants on each port and log any reply.

Helps find which port / field combination the firmware answers to.

Usage:
    python host/probe.py [--ports COM3 COM5] [--listen 1.0]
"""

from __future__ import annotations

import argparse
import sys
import time

import serial

from epaper.protocol import Frame, hexdump, decode
from epaper.transport import BAUDRATE

VARIANTS = [
    # (label, frame)
    ("get_version dev=0x01 to 0x01",
     Frame(dest=0x01, src=0x00, dev_type=0x01, cmd=0x02,
           group_no=1, group_count=1, chip_count=1)),
    ("get_version dev=0x00 to 0x01",
     Frame(dest=0x01, src=0x00, dev_type=0x00, cmd=0x02,
           group_no=1, group_count=1, chip_count=2)),
    ("stop dev=0x01 CC=1 to 0x01",
     Frame(dest=0x01, src=0x00, dev_type=0x01, cmd=0x17,
           group_no=1, group_count=1, chip_count=1)),
    ("stop doc-example fields (dev=0x00 CC=2) to 0x01",
     Frame(dest=0x01, src=0x00, dev_type=0x00, cmd=0x17,
           group_no=1, group_count=1, chip_count=2)),
    ("stop dev=0x01 to 0x02",
     Frame(dest=0x02, src=0x00, dev_type=0x01, cmd=0x17,
           group_no=2, group_count=2, chip_count=1)),
    ("stop broadcast dev=0x01",
     Frame(dest=0xFF, src=0x00, dev_type=0x01, cmd=0x17,
           group_no=1, group_count=2, chip_count=1)),
]


def probe_port(port: str, listen_s: float) -> list[str]:
    hits = []
    try:
        ser = serial.Serial(port, BAUDRATE, timeout=0.05)
    except serial.SerialException as e:
        print(f"  cannot open {port}: {e}")
        return hits
    with ser:
        # Passive listen first: is anything already talking?
        t0 = time.monotonic()
        noise = b""
        while time.monotonic() - t0 < 1.0:
            noise += ser.read(256)
        if noise:
            print(f"  passive RX ({len(noise)}B): {hexdump(noise[:64])}")
            hits.append(f"passive traffic: {len(noise)}B")

        for label, frame in VARIANTS:
            raw = frame.encode()
            ser.reset_input_buffer()
            ser.write(raw)
            ser.flush()
            print(f"  [{label}]")
            print(f"    TX> {hexdump(raw)}")
            rx = b""
            t0 = time.monotonic()
            while time.monotonic() - t0 < listen_s:
                rx += ser.read(256)
            if rx:
                print(f"    RX< {hexdump(rx)}")
                f, _ = decode(rx)
                if f:
                    print(f"    -> decoded: {f.describe()}")
                hits.append(f"{label}: {hexdump(rx)}")
            else:
                print("    RX< (nothing)")
            time.sleep(0.2)
    return hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ports", nargs="+", default=["COM3", "COM5"])
    ap.add_argument("--listen", type=float, default=1.0)
    args = ap.parse_args()

    all_hits = {}
    for port in args.ports:
        print(f"=== {port} ===")
        all_hits[port] = probe_port(port, args.listen)

    print("\n=== SUMMARY ===")
    any_hit = False
    for port, hits in all_hits.items():
        for h in hits:
            print(f"{port}: {h}")
            any_hit = True
    if not any_hit:
        print("No response on any port/variant.")
    return 0 if any_hit else 1


if __name__ == "__main__":
    sys.exit(main())
