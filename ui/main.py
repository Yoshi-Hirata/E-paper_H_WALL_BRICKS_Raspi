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


def check() -> int:
    """Preflight: report whether the panels, SPI, GPIO and LCD are usable.

    Written for the day the HAT is fitted - it distinguishes "not wired
    up" from "another process owns these pins", which is the failure this
    Pi is prone to (a second Waveshare HAT shares GPIO 24/25 and SPI0).
    """
    from pathlib import Path as _Path

    from .config import BUTTON_PINS, PIN_BL, PIN_DC, PIN_RST

    ok = True
    port = find_port()
    print(f"panel serial port : {port or 'NOT FOUND'}")
    ok &= port is not None

    spidevs = sorted(str(p) for p in _Path("/dev").glob("spidev*"))
    print(f"spi devices       : {', '.join(spidevs) or 'NONE (dtparam=spi=on?)'}")
    ok &= bool(spidevs)

    try:
        from gpiozero import Device
        from gpiozero.pins.lgpio import LGPIOFactory
        Device.pin_factory = LGPIOFactory()
        print("gpio backend      : lgpio")
    except Exception as exc:
        print(f"gpio backend      : UNAVAILABLE ({exc})")
        return 1

    from gpiozero import Button, DigitalOutputDevice
    busy = []
    for name, pin in BUTTON_PINS.items():
        try:
            Button(pin, pull_up=True).close()
        except Exception as exc:
            busy.append(f"{name}(GPIO{pin}): {exc}")
    for name, pin in (("dc", PIN_DC), ("rst", PIN_RST), ("bl", PIN_BL)):
        try:
            DigitalOutputDevice(pin).close()
        except Exception as exc:
            busy.append(f"{name}(GPIO{pin}): {exc}")
    if busy:
        ok = False
        print("gpio pins         : BUSY")
        for line in busy:
            print(f"  {line}")
        print("  another process holds these lines - check: sudo lsof /dev/gpiochip0")
    else:
        print("gpio pins         : all 11 free")

    try:
        from .display import ST7789Display
        lcd = ST7789Display()
        lcd.close()
        print("lcd (ST7789)      : responds")
    except Exception as exc:
        ok = False
        print(f"lcd (ST7789)      : NOT USABLE ({exc})")

    ok &= check_boards(port)
    print("\nresult:", "ready" if ok else "not ready")
    return 0 if ok else 1


def check_boards(port: str | None, boards=(0x01, 0x02)) -> bool:
    """Ask each panel board to answer, so a dead link is found before a show.

    Needs the port to itself: stop the service first if it is running.
    """
    from epaper.commands import stop
    from epaper.transport import Bus

    if not port:
        return False
    try:
        bus = Bus(port, verbose=False)
    except Exception as exc:
        print(f"boards            : cannot open port ({exc})")
        print("                    stop epaper-ui/epaper-demo first")
        return False
    ok = True
    with bus:
        groups = max(len(boards), max(boards))
        for board in boards:
            ack = bus.request(stop(board, groups))
            if ack is not None and ack.cmd == 0x80:
                print(f"board 0x{board:02X}        : ACK")
            else:
                ok = False
                reason = "no ACK" if ack is None else f"NAK 0x{ack.cmd:02X}"
                print(f"board 0x{board:02X}        : {reason}")
    return ok


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
    ap.add_argument("--check", action="store_true",
                    help="report panel/SPI/GPIO/LCD readiness and exit")
    ap.add_argument("--port", help="serial port (default: auto-detect)")
    ap.add_argument("--boards", nargs="+", type=lambda v: int(v, 0),
                    default=[0x01, 0x02])
    ap.add_argument("--interval", type=float, default=60.0,
                    help="seconds between panel refreshes (default 60)")
    ap.add_argument("--guard-delay", type=float, default=12.0)
    ap.add_argument("--slot", type=int, default=TEST_SLOT)
    ap.add_argument("--pattern", choices=[p.key for p in PATTERNS],
                    help="start this pattern immediately instead of showing "
                         "the menu (with --display null --input none this is "
                         "how the headless service runs)")
    ap.add_argument("--locked", action="store_true",
                    help="ignore the buttons so a knock during a show cannot "
                         "stop the demo (unlock: KEY2 KEY3 KEY2; re-locks "
                         "itself after a minute of no input)")
    ap.add_argument("--blank-after", type=float, default=None, metavar="SEC",
                    help="blank the backlight after this idle time "
                         "(0 disables)")
    ap.add_argument("--max-ticks", type=int,
                    help="exit after N UI ticks (testing)")
    args = ap.parse_args()

    if args.preview:
        return preview(args.preview)
    if args.check:
        return check()

    port = args.port or find_port()
    runner = DemoRunner(boards=args.boards, interval=args.interval,
                        guard_delay=args.guard_delay, slot=args.slot,
                        port=port)

    display_kwargs = {"directory": args.frames} if args.display in ("png", "auto") else {}
    with make_display(args.display, **display_kwargs) as display, \
            make_input(args.input) as inputs:
        app_kwargs = {"port_label": port, "locked": args.locked}
        if args.blank_after is not None:
            app_kwargs["blank_after"] = args.blank_after
        app = App(display, inputs, runner, **app_kwargs)
        if args.pattern:
            app.select(args.pattern)
            app.handle("key1")
        app.run(max_ticks=args.max_ticks)
    return 0


if __name__ == "__main__":
    sys.exit(main())
