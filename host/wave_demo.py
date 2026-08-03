"""Two-panel effect demo: radial gradient + clockwise inward spiral.

Board 1 (first in --boards) shows concentric color waves radiating from
the center outward; board 2 shows colors rotating clockwise along an
outside-in spiral. Adjacent triangles are guaranteed to differ in color.

Each cycle saves the next pattern to every board (no display), then
broadcasts show-single (0x1D) so both panels refresh together.

Usage:
    python host/wave_demo.py                     # 10 cycles, 20s apart
    python host/wave_demo.py --cycles 6 --interval 30
"""

import argparse
import sys
import time

from epaper.commands import TEST_SLOT, save_color, show_single, slot_config, stop
from epaper.effects import gradient_pattern, spiral_pattern
from epaper.pattern import COLOR_NAMES, build_hexagon_array
from epaper.transport import Bus, find_port
from show import request_checked

EFFECTS = [("gradient", gradient_pattern), ("spiral", spiral_pattern)]


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Gradient + spiral demo on two panels")
    ap.add_argument("--port", help="serial port (default: auto-detect)")
    ap.add_argument("--boards", nargs="+", type=lambda v: int(v, 0),
                    default=[0x01, 0x02],
                    help="board addresses; first gets the gradient, "
                         "second the spiral")
    ap.add_argument("--cycles", type=int, default=10,
                    help="number of rounds; 0 = endless (default 10)")
    ap.add_argument("--interval", type=float, default=20.0,
                    help="seconds between rounds (default 20)")
    ap.add_argument("--colors", nargs="+",
                    default=["white", "yellow", "red", "blue", "green",
                             "black"],
                    choices=sorted(COLOR_NAMES),
                    help="ordered palette the effects cycle through")
    ap.add_argument("--slot", type=int, default=TEST_SLOT)
    ap.add_argument("--guard-delay", type=float, default=12.0,
                    help="seconds after each show at which a broadcast stop "
                         "is sent to suppress the factory autoplay from "
                         "resuming mid-interval; must exceed the panel "
                         "refresh time and stay below --interval. "
                         "0 disables the guard (default 12)")
    args = ap.parse_args()

    if len(args.boards) > len(EFFECTS):
        print(f"At most {len(EFFECTS)} boards supported.", file=sys.stderr)
        return 2
    port = args.port or find_port()
    if not port:
        print("No serial port found. Specify with --port.", file=sys.stderr)
        return 2

    palette = [COLOR_NAMES[c] for c in args.colors]
    groups = max(len(args.boards), max(args.boards))
    assignments = list(zip(args.boards, EFFECTS))
    plan = ", ".join(f"0x{b:02X}={name}" for b, (name, _) in assignments)
    print(f"Port {port}, slot {args.slot}, cycles {args.cycles or 'endless'}, "
          f"interval {args.interval}s")
    print(f"Effects: {plan}")

    with Bus(port, verbose=False) as bus:
        # Broadcast stop first: silences the factory power-on autoplay on
        # every board on the bus, including ones not listed in --boards.
        bus.send(stop(0xFF, groups))
        time.sleep(0.3)
        for board in args.boards:
            if not request_checked(bus, stop(board, groups),
                                   f"stop @0x{board:02X}"):
                return 1
            if not request_checked(
                    bus, slot_config(board, args.slot, group_count=groups),
                    f"slot config @0x{board:02X}"):
                return 1

        cycle = 0
        try:
            while args.cycles == 0 or cycle < args.cycles:
                print(f"--- cycle {cycle + 1}"
                      + (f"/{args.cycles}" if args.cycles else "") + " ---")
                # Re-assert stop each cycle in case autoplay resumed.
                for board in args.boards:
                    if not request_checked(bus, stop(board, groups),
                                           f"stop @0x{board:02X}"):
                        return 1
                for board, (name, generator) in assignments:
                    pattern = generator(cycle, palette)
                    arr = build_hexagon_array(pattern)
                    if not request_checked(
                            bus, save_color(board, args.slot, arr, groups),
                            f"save {name} @0x{board:02X}"):
                        return 1
                bus.send(show_single(0xFF, args.slot, groups))
                print("broadcast show sent; panels refreshing")
                cycle += 1
                if args.cycles == 0 or cycle < args.cycles:
                    guard = args.guard_delay
                    if 0 < guard < args.interval:
                        # After our refresh has finished, kill any pending
                        # autoplay slot-switch before it repaints the panel.
                        time.sleep(guard)
                        bus.send(stop(0xFF, groups))
                        print(f"guard stop sent at +{guard:.0f}s")
                        time.sleep(args.interval - guard)
                    else:
                        time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nInterrupted by user.")
    print("Demo finished.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
