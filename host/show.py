"""Safe color-change test CLI for H_WALL_BRICKS panels.

Runs the spec 10.1 sequence against a dedicated test slot:
    stop (0x17) -> slot config (0x1B) -> save color (0x13) -> show (0x1D)

Usage:
    python host/show.py --addr 1 --fill white
    python host/show.py --addr 1 --fill white --set 2=red --set 35=blue
    python host/show.py --addr 1 --delete            # remove test slot data
    python host/show.py --broadcast-show             # 0x1D to all boards
"""

import argparse
import sys
import time

from epaper.commands import (
    TEST_SLOT,
    delete_slot,
    save_color,
    show_single,
    slot_config,
    stop,
)
from epaper.pattern import COLOR_NAMES, build_hexagon_array
from epaper.protocol import ACK_BUSY, ACK_SUCCESS, Frame
from epaper.transport import Bus, find_port

BUSY_RETRIES = 5
BUSY_WAIT_S = 1.0


def request_checked(bus: Bus, frame: Frame, label: str) -> bool:
    """Send frame, wait for ACK; retry on ACK_BUSY. True on ACK_SUCCESS."""
    for _ in range(BUSY_RETRIES):
        ack = bus.request(frame)
        if ack is None:
            print(f"{label}: no ACK (timeout)", file=sys.stderr)
            return False
        if ack.cmd == ACK_BUSY:
            print(f"{label}: device busy, waiting {BUSY_WAIT_S}s...")
            time.sleep(BUSY_WAIT_S)
            continue
        ok = ack.cmd == ACK_SUCCESS
        print(f"{label}: {ack.describe()}")
        return ok
    print(f"{label}: still busy after {BUSY_RETRIES} retries", file=sys.stderr)
    return False


def parse_set(spec: str) -> tuple[int, int]:
    tri_s, _, col_s = spec.partition("=")
    if col_s.lower() not in COLOR_NAMES:
        raise argparse.ArgumentTypeError(
            f"unknown color '{col_s}' (choose: {', '.join(COLOR_NAMES)})")
    return int(tri_s), COLOR_NAMES[col_s.lower()]


def main() -> int:
    ap = argparse.ArgumentParser(description="Color-change test (safe slot)")
    ap.add_argument("--port", help="serial port (default: auto-detect)")
    ap.add_argument("--addr", type=lambda v: int(v, 0), default=0x01)
    ap.add_argument("--slot", type=int, default=TEST_SLOT,
                    help=f"target slot (default {TEST_SLOT}, the test slot)")
    ap.add_argument("--fill", default="white", choices=sorted(COLOR_NAMES),
                    help="base color for all triangles")
    ap.add_argument("--set", action="append", type=parse_set, default=[],
                    metavar="N=COLOR", help="override triangle N (repeatable)")
    ap.add_argument("--groups", type=int, default=2)
    ap.add_argument("--delete", action="store_true",
                    help="delete the slot's color data instead of showing")
    ap.add_argument("--broadcast-show", action="store_true",
                    help="only send show-single (0x1D) to 0xFF; no data")
    ap.add_argument("--no-show", action="store_true",
                    help="save color data but skip the display step")
    args = ap.parse_args()

    port = args.port or find_port()
    if not port:
        print("No serial port found. Specify with --port.", file=sys.stderr)
        return 2

    with Bus(port) as bus:
        if args.delete:
            ok = request_checked(
                bus, delete_slot(args.addr, args.slot, args.groups),
                f"delete slot {args.slot} @0x{args.addr:02X}")
            return 0 if ok else 1

        if args.broadcast_show:
            bus.send(show_single(0xFF, args.slot, args.groups))
            print(f"Broadcast show slot {args.slot} sent (no ACK expected). "
                  "Panels should start refreshing.")
            return 0

        array = build_hexagon_array(dict(args.set), COLOR_NAMES[args.fill])
        overrides = ", ".join(f"{t}={c}" for t, c in args.set) or "none"
        print(f"Target 0x{args.addr:02X}, slot {args.slot}, "
              f"fill {args.fill}, overrides: {overrides}")

        steps = [
            (stop(args.addr, args.groups), "stop"),
            (slot_config(args.addr, args.slot, group_count=args.groups),
             "slot config"),
            (save_color(args.addr, args.slot, array, group_count=args.groups),
             "save color"),
        ]
        if not args.no_show:
            steps.append(
                (show_single(args.addr, args.slot, args.groups), "show single"))
        for frame, label in steps:
            if not request_checked(bus, frame, label):
                print("Aborting sequence.", file=sys.stderr)
                return 1
        if args.no_show:
            print("Color data saved (display skipped).")
        else:
            print("Sequence complete. Panel is refreshing now "
                  "(allow several seconds); verify visually.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
