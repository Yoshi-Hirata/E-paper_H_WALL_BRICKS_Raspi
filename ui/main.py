"""Entry point for the LCD HAT user interface.

    python -m ui.main                     # HAT if present, else PNG+keyboard
    python -m ui.main --display png --input keyboard --frames /tmp/ui
    python -m ui.main --preview /tmp/ui   # render sample screens and exit
"""

from __future__ import annotations

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
    render.menu_screen(PATTERNS, 0, "/dev/ttyACM0",
                       status="panels: white (standby)"
                       ).save(out / "menu_standby.png")
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

    Written to be run before a show, and to say which part is missing
    rather than just failing: "not wired up" and "another process owns
    these pins" need different fixes.
    """
    from pathlib import Path as _Path

    from .boards import BOARD

    ok = True
    print(f"board             : {BOARD.label} ({BOARD.key})")

    port = find_port()
    print(f"panel serial port : {port or 'NOT FOUND'}")
    ok &= port is not None

    spidevs = sorted(str(p) for p in _Path("/dev").glob("spidev*"))
    expected = f"/dev/spidev{BOARD.spi_bus}.{BOARD.spi_device}"
    print(f"spi devices       : {', '.join(spidevs) or 'NONE'}"
          f"  (need {expected})")
    ok &= expected in spidevs

    print(f"gpio backend      : {BOARD.gpio_backend}")
    busy = _check_gpio_lines(BOARD)
    if busy:
        ok = False
        print("gpio pins         : BUSY")
        for line in busy:
            print(f"  {line}")
        print("  another process holds these lines - check: sudo lsof /dev/gpiochip*")
    else:
        print(f"gpio pins         : all {len(BOARD.buttons) + 3} free")

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


def _check_gpio_lines(board) -> "list[str]":
    """Open and release every line the HAT uses; report the ones that fail."""
    busy = []
    lines = list(board.buttons.items()) + [
        ("dc", board.dc), ("rst", board.rst), ("bl", board.bl)]
    for name, line in lines:
        is_input = name in board.buttons
        try:
            if board.gpio_backend == "gpiozero":
                from gpiozero import Button, DigitalOutputDevice
                device = (Button(line.line, pull_up=True) if is_input
                          else DigitalOutputDevice(line.line))
                device.close()
            else:
                from .gpio import OutputLine, _open
                if is_input:
                    _open(line, "in", bias="pull_up").close()
                else:
                    OutputLine(line).close()
        except Exception as exc:
            busy.append(f"{name} (chip{line.chip} line{line.line}): {exc}")
    return busy


def check_boards(port: str | None, boards=(0x01, 0x02),
                 patience_s: float = 20.0) -> bool:
    """Ask each panel board to answer, so a dead link is found before a show.

    Keeps asking for `patience_s`: a board ignores everything for the
    9.8 s its e-paper takes to repaint, and after a power-on the factory
    autoplay is doing exactly that. Reporting "no ACK" for a board that
    is merely busy would be a false alarm at the worst moment.

    Needs the port to itself: stop the service first if it is running.
    """
    import time

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
            deadline = time.monotonic() + patience_s
            reason = "no ACK"
            while True:
                ack = bus.request(stop(board, groups))
                if ack is not None and ack.cmd == 0x80:
                    print(f"board 0x{board:02X}        : ACK")
                    break
                if ack is not None:
                    reason = f"NAK 0x{ack.cmd:02X}"
                if time.monotonic() >= deadline:
                    ok = False
                    print(f"board 0x{board:02X}        : {reason} "
                          f"(after {patience_s:.0f}s)")
                    break
                time.sleep(1.0)
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
    ap.add_argument("--no-standby", action="store_true",
                    help="do not white out the panels at startup; leave "
                         "whatever they are showing (the factory autoplay "
                         "keeps running)")
    ap.add_argument("--max-ticks", type=int,
                    help="exit after N UI ticks (testing)")
    args = ap.parse_args()

    if args.preview:
        return preview(args.preview)
    if args.check:
        return check()

    port = args.port or find_port()
    # Pass args.port, not the detected one: pinning the name found at
    # startup would defeat the runner's re-detection, and a USB replug
    # can bring the boards back as ttyACM1. `port` is only the label.
    runner = DemoRunner(boards=args.boards, interval=args.interval,
                        guard_delay=args.guard_delay, slot=args.slot,
                        port=args.port)

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
        elif not args.no_standby:
            # No demo asked for, so put the panels into the agreed idle
            # state: factory autoplay stopped, every sector white.
            app.enter_standby()
        app.run(max_ticks=args.max_ticks)
    return 0


if __name__ == "__main__":
    sys.exit(main())
