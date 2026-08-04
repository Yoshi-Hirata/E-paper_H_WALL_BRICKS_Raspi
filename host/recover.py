"""Drive a panel through full black/white swings to clear a stuck segment.

E-paper segments can latch when a waveform is interrupted (power cut
mid-refresh, a dropped frame): the particle stack stays where it was and
that triangle then ignores new colours while its neighbours update.
Repeatedly driving the whole panel between the two extremes applies the
strongest available swing to every segment, which usually unsticks it.

    python host/recover.py --addr 1              # 3 black/white rounds
    python host/recover.py --addr 1 --rounds 6
    python host/recover.py --addr 1 --end white  # leave it white

Each round writes the boards' flash twice, so keep the count modest.
"""

import argparse
import sys
import time

from epaper.commands import TEST_SLOT, save_color, show_single, slot_config, stop
from epaper.pattern import COLOR_NAMES, build_hexagon_array
from epaper.transport import Bus, find_port
from show import request_checked

# Measured on this hardware: a full repaint takes 9.8 s and the board
# ignores commands until it finishes.
REFRESH_S = 12.0


def paint(bus, board, groups, slot, color_name, wait_s) -> bool:
    arr = build_hexagon_array(fill=COLOR_NAMES[color_name])
    if not request_checked(bus, stop(board, groups), f"stop @0x{board:02X}"):
        return False
    if not request_checked(bus, save_color(board, slot, arr, groups),
                           f"save {color_name} @0x{board:02X}"):
        return False
    bus.send(show_single(board, slot, groups))
    print(f"  {color_name} shown, waiting {wait_s:.0f}s for the repaint")
    time.sleep(wait_s)
    return True


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Unstick e-paper segments with full-swing refreshes")
    ap.add_argument("--port", help="serial port (default: auto-detect)")
    ap.add_argument("--addr", type=lambda v: int(v, 0), default=0x01,
                    help="board address to recover (default 1)")
    ap.add_argument("--rounds", type=int, default=3,
                    help="black+white pairs to run (default 3)")
    ap.add_argument("--end", default="white", choices=sorted(COLOR_NAMES),
                    help="colour to leave the panel on (default white)")
    ap.add_argument("--groups", type=int, default=2)
    ap.add_argument("--slot", type=int, default=TEST_SLOT)
    ap.add_argument("--refresh", type=float, default=REFRESH_S,
                    help=f"seconds to wait per repaint (default {REFRESH_S})")
    args = ap.parse_args()

    port = args.port or find_port()
    if not port:
        print("No serial port found. Specify with --port.", file=sys.stderr)
        return 2

    print(f"Port {port}, board 0x{args.addr:02X}, {args.rounds} rounds "
          f"(about {args.rounds * 2 * args.refresh / 60:.1f} min)")
    with Bus(port, verbose=False) as bus:
        if not request_checked(bus, stop(args.addr, args.groups),
                               f"stop @0x{args.addr:02X}"):
            return 1
        if not request_checked(
                bus, slot_config(args.addr, args.slot, group_count=args.groups),
                f"slot config @0x{args.addr:02X}"):
            return 1
        for round_no in range(1, args.rounds + 1):
            print(f"--- round {round_no}/{args.rounds} ---")
            for color in ("black", "white"):
                if not paint(bus, args.addr, args.groups, args.slot, color,
                             args.refresh):
                    return 1
        if args.end not in ("white",):
            print(f"--- settling on {args.end} ---")
            if not paint(bus, args.addr, args.groups, args.slot, args.end,
                         args.refresh):
                return 1
    print("Recovery sequence finished. Check whether the segment follows now.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
