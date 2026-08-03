"""Entry point for the LCD HAT user interface.

    python -m ui.main                     # HAT if present, else PNG+keyboard
    python -m ui.main --display png --input keyboard --frames /tmp/ui
    python -m ui.main --preview /tmp/ui   # render sample screens and exit
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

from epaper.commands import TEST_SLOT
from epaper.transport import find_port

from .app import App
from .display import make_display
from .inputs import make_input
from .patterns import PATTERNS
from .runner import DemoRunner


def preview(directory: str) -> int:
    """Render every screen to PNGs so the UI can be reviewed without a HAT."""
    from . import render

    out = Path(directory)
    out.mkdir(parents=True, exist_ok=True)
    render.menu_screen(PATTERNS, 0, "/dev/ttyACM0").save(out / "menu_first.png")
    render.menu_screen(PATTERNS, 3, "/dev/ttyACM0").save(out / "menu_mid.png")
    render.menu_screen(PATTERNS, 0, None).save(out / "menu_noport.png")
    render.running_screen(
        "WAVE", 3725.0, 42,
        ["09:12:01 port /dev/ttyACM0", "09:12:02 start WAVE",
         "09:12:14 cycle 1 shown", "09:13:14 cycle 2 shown",
         "09:14:14 cycle 3 shown", "09:15:14 cycle 4 shown"],
    ).save(out / "running.png")
    render.running_screen(
        "RANDOM", 62.0, 1,
        ["09:20:00 start RANDOM", "09:20:31 ERROR save @02 no ACK"],
        error="save @02: no ACK",
    ).save(out / "running_error.png")
    render.message_screen("stopped", "KEY3 pressed").save(out / "stopped.png")
    print(f"wrote preview screens to {out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="LCD HAT UI for the e-paper demo")
    ap.add_argument("--display", default="auto",
                    choices=["auto", "lcd", "png", "null"])
    ap.add_argument("--input", default="auto",
                    choices=["auto", "gpio", "keyboard", "none"])
    ap.add_argument("--frames", default="frames",
                    help="output directory for the png display backend")
    ap.add_argument("--preview", metavar="DIR",
                    help="render sample screens to DIR and exit")
    ap.add_argument("--port", help="serial port (default: auto-detect)")
    ap.add_argument("--boards", nargs="+", type=lambda v: int(v, 0),
                    default=[0x01, 0x02])
    ap.add_argument("--interval", type=float, default=60.0,
                    help="seconds between panel refreshes (default 60)")
    ap.add_argument("--guard-delay", type=float, default=12.0)
    ap.add_argument("--slot", type=int, default=TEST_SLOT)
    ap.add_argument("--max-ticks", type=int,
                    help="exit after N UI ticks (testing)")
    args = ap.parse_args()

    if args.preview:
        return preview(args.preview)

    port = args.port or find_port()
    runner = DemoRunner(boards=args.boards, interval=args.interval,
                        guard_delay=args.guard_delay, slot=args.slot,
                        port=port)

    display_kwargs = {"directory": args.frames} if args.display in ("png", "auto") else {}
    with make_display(args.display, **display_kwargs) as display, \
            make_input(args.input) as inputs:
        app = App(display, inputs, runner, port_label=port)
        app.run(max_ticks=args.max_ticks)
    return 0


if __name__ == "__main__":
    sys.exit(main())
