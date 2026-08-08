"""Random-color demo for two H_WALL_BRICKS panels.

Each cycle: save a random pattern to the test slot on every board
(no display), then broadcast show-single (0x1D) so all panels refresh
simultaneously. Repeats for --cycles rounds.

Note: every cycle writes flash on each board (0x13). Keep cycle counts
reasonable; --cycles 0 (endless) is available but not recommended for
long unattended runs.

Usage:
    python host/demo.py                          # 10 cycles, 20s apart
    python host/demo.py --cycles 5 --interval 30
    python host/demo.py --mutate 10              # change only 10 triangles/cycle
"""

from __future__ import annotations

import argparse
import random
import sys
import time

from epaper.commands import TEST_SLOT, save_color, show_single, slot_config, stop
from epaper.pattern import (
    COLOR_NAMES,
    VALID_TRIANGLES,
    build_hexagon_array,
)
from epaper.transport import Bus, find_port
from show import request_checked

ALL_COLORS = list(COLOR_NAMES.values())


def random_pattern(rng: random.Random, colors: list[int]) -> dict[int, int]:
    return {tri: rng.choice(colors) for tri in VALID_TRIANGLES}


def mutate_pattern(rng: random.Random, base: dict[int, int],
                   count: int, colors: list[int]) -> dict[int, int]:
    out = dict(base)
    for tri in rng.sample(sorted(VALID_TRIANGLES), count):
        out[tri] = rng.choice([c for c in colors if c != out.get(tri)])
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Random color demo (2 panels)")
    ap.add_argument("--port", help="serial port (default: auto-detect)")
    ap.add_argument("--boards", nargs="+", type=lambda v: int(v, 0),
                    default=[0x01, 0x02])
    ap.add_argument("--cycles", type=int, default=10,
                    help="number of rounds; 0 = endless (default 10)")
    ap.add_argument("--interval", type=float, default=20.0,
                    help="seconds between rounds (default 20, allow refresh)")
    ap.add_argument("--mutate", type=int, default=0, metavar="N",
                    help="change only N random triangles per cycle "
                         "(default 0 = full random repaint)")
    ap.add_argument("--colors", nargs="+", default=sorted(COLOR_NAMES),
                    choices=sorted(COLOR_NAMES),
                    help="color palette to draw from")
    ap.add_argument("--slot", type=int, default=TEST_SLOT)
    ap.add_argument("--seed", type=int, help="RNG seed for reproducible runs")
    args = ap.parse_args()

    port = args.port or find_port()
    if not port:
        print("No serial port found. Specify with --port.", file=sys.stderr)
        return 2

    rng = random.Random(args.seed)
    palette = [COLOR_NAMES[c] for c in args.colors]
    groups = max(len(args.boards), max(args.boards))
    patterns: dict[int, dict[int, int]] = {}

    print(f"Port {port}, boards {[hex(b) for b in args.boards]}, "
          f"slot {args.slot}, cycles {args.cycles or 'endless'}, "
          f"interval {args.interval}s, palette {args.colors}")

    with Bus(port, verbose=False) as bus:
        # One-time setup: stop playback and configure the slot on each board.
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
                cycle += 1
                print(f"--- cycle {cycle}"
                      + (f"/{args.cycles}" if args.cycles else "") + " ---")
                for board in args.boards:
                    if args.mutate and board in patterns:
                        patterns[board] = mutate_pattern(
                            rng, patterns[board], args.mutate, palette)
                    else:
                        patterns[board] = random_pattern(rng, palette)
                    arr = build_hexagon_array(patterns[board])
                    if not request_checked(
                            bus, save_color(board, args.slot, arr, groups),
                            f"save @0x{board:02X}"):
                        return 1
                bus.send(show_single(0xFF, args.slot, groups))
                print("broadcast show sent; panels refreshing")
                if args.cycles == 0 or cycle < args.cycles:
                    time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nInterrupted by user.")
    print("Demo finished.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
