"""Send PLAY_STOP (0x17) to an e-paper board and report the ACK.

Usage:
    python host/stop.py                 # stop board 0x01 via auto-detected port
    python host/stop.py --addr 2       # stop board 0x02
    python host/stop.py --broadcast    # stop all boards (no ACK expected)
    python host/stop.py --port COM5
"""

import argparse
import sys

from epaper.protocol import (
    ADDR_BROADCAST,
    ADDR_PC,
    CMD_PLAY_STOP,
    DEV_H_WALL_BRICKS,
    Frame,
)
from epaper.transport import Bus, find_port


def main() -> int:
    ap = argparse.ArgumentParser(description="Send PLAY_STOP (0x17)")
    ap.add_argument("--port", help="serial port (default: auto-detect)")
    ap.add_argument("--addr", type=lambda v: int(v, 0), default=0x01,
                    help="target board address (default 0x01)")
    ap.add_argument("--broadcast", action="store_true",
                    help="send to 0xFF (all boards, no ACK)")
    ap.add_argument("--groups", type=int, default=2,
                    help="total group count on the bus (default 2)")
    ap.add_argument("--dev-type", type=lambda v: int(v, 0),
                    default=DEV_H_WALL_BRICKS,
                    help="device type (default 0x01 H_WALL_BRICKS)")
    args = ap.parse_args()

    port = args.port or find_port()
    if not port:
        print("No serial port found. Specify with --port.", file=sys.stderr)
        return 2

    dest = ADDR_BROADCAST if args.broadcast else args.addr
    frame = Frame(
        dest=dest,
        src=ADDR_PC,
        dev_type=args.dev_type,
        cmd=CMD_PLAY_STOP,
        group_no=1 if args.broadcast else args.addr,
        group_count=args.groups,
        chip_count=1,
    )

    print(f"Port: {port}, target: 0x{dest:02X}")
    with Bus(port) as bus:
        if args.broadcast:
            bus.send(frame)
            print("Broadcast sent (no ACK expected).")
            # Listen briefly anyway to surface unexpected traffic.
            ack = bus.recv(timeout=0.5)
            if ack:
                print(f"Unexpected reply: {ack.describe()}")
            return 0
        ack = bus.request(frame)
        if ack is None:
            print("FAILED: no ACK after retries.", file=sys.stderr)
            return 1
        print(f"Reply: {ack.describe()}")
        return 0 if ack.cmd == 0x80 else 1


if __name__ == "__main__":
    sys.exit(main())
