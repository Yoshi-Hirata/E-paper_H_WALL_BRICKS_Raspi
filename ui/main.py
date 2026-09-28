"""Entry point for the LCD HAT user interface.

    python -m ui.main                     # HAT if present, else PNG+keyboard
    python -m ui.main --display png --input keyboard --frames /tmp/ui
    python -m ui.main --preview /tmp/ui   # render sample screens and exit
"""

from __future__ import annotations

import argparse
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

from epaper.commands import TEST_SLOT
from epaper.transport import find_port

from .agent import DEFAULT_PORT, Agent
from .app import App
from .demos import DemoStore
from .display import make_display
from .inputs import make_input
from .patterns import PATTERNS
from .puller import RepoPuller
from .rebooter import Rebooter
from .remote import RemoteSession
from .showplay import ShowPlayer
from .runner import (DEFAULT_BOARDS, PRECHECK_S, REMOTE_GUARD_S,
                     VERIFY_AFTER_S, WITNESS_ANY, WITNESS_USB, DemoRunner)
from .updater import (FirmwareUpdater, find_firmware, find_firmware_images,
                      usb_rebind)
from .versions import BoardVersions
from .boardinfo import BoardInfo


def preview(directory: str) -> int:
    """Render every screen to PNGs so the UI can be reviewed without a HAT."""
    from . import render

    out = Path(directory)
    out.mkdir(parents=True, exist_ok=True)
    render.menu_screen(PATTERNS, 0, "/dev/ttyACM0",
                       host="radxa-01").save(out / "menu_first.png")
    render.menu_screen(PATTERNS, 3, "/dev/ttyACM0").save(out / "menu_mid.png")
    render.menu_screen(PATTERNS, 0, None).save(out / "menu_noport.png")
    render.menu_screen(PATTERNS, 0, "/dev/ttyACM0",
                       status="standby: white, boards 2/20 OK"
                       ).save(out / "menu_standby.png")
    render.running_screen(
        "WAVE", 3725.0, 42,
        ["09:12:01 port /dev/ttyACM0", "09:12:02 start WAVE",
         "09:12:14 cycle 1 shown", "09:13:14 cycle 2 shown",
         "09:14:14 cycle 3 shown", "09:15:14 cycle 4 shown"],
        host="radxa-01",
    ).save(out / "running.png")
    render.running_screen(
        "RANDOM", 62.0, 1,
        ["09:20:00 start RANDOM", "09:20:31 ERROR save @02 no ACK"],
        error="save @02: no ACK",
    ).save(out / "running_error.png")
    render.message_screen("stopped", "KEY3 pressed").save(out / "stopped.png")
    fw, size = "FW_260903/OTA_16c.bin", 65544
    render.update_screen(fw, size, 1, "idle", "IDLE size=0 crc=0x0000", 0,
                         ["10:00:00 port /dev/ttyACM0"]
                         ).save(out / "update_confirm.png")
    render.update_screen(fw, size, 1, "flashing", "IDLE size=0 crc=0x0000",
                         27300,
                         ["10:00:01 update board 01 <- " + fw,
                          "10:00:01 Image: 65544 bytes, CRC16 0x1234",
                          "10:00:02 OTA start (0x26) -> board 0x01",
                          "10:00:20 chunk @ 9600: slow ACK 12.3s"]
                         ).save(out / "update_flashing.png")
    render.update_screen(fw, size, 1, "done", "updated", size,
                         ["10:01:10 OTA finish (0x28)",
                          "10:01:10 Port vanished after 0x28 -> rebooting",
                          "10:01:25 Board 0x01 is back on new firmware",
                          "10:01:25 board 01 updated"]
                         ).save(out / "update_done.png")
    render.update_screen(fw, size, 20, "failed", "no reply", 0,
                         ["10:02:00 update board 20 <- " + fw,
                          "10:02:31 ERROR Board 0x14 does not accept 0x26",
                          "10:02:31 ERROR flash failed"],
                         error="flash failed").save(out / "update_failed.png")
    old = "b5706ee Bundle FW_260917 and let UPDATE FW pick any .bin"
    new = "c0ffee1 Show the hostname on every screen, add GIT PULL"
    render.pull_screen(old, None, "idle", [], host="radxa-01"
                       ).save(out / "pull_confirm.png")
    render.pull_screen(old, None, "pulling",
                       ["11:00:00 git pull --ff-only (main) at b5706ee"],
                       host="radxa-01").save(out / "pull_pulling.png")
    render.pull_screen(old, new, "done", changed=True, host="radxa-01",
                       log_lines=["11:00:00 git pull --ff-only (main) at b5706ee",
                                  "11:00:03 Updating b5706ee..c0ffee1",
                                  "11:00:03 Fast-forward",
                                  "11:00:03  ui/app.py | 40 ++++--",
                                  "11:00:03 b5706ee -> c0ffee1: restart to apply"]
                       ).save(out / "pull_done.png")
    render.pull_screen(old, old, "done", ["11:05:00 Already up to date.",
                                          "11:05:00 up to date at b5706ee"],
                       host="radxa-01").save(out / "pull_uptodate.png")
    render.pull_screen(old, None, "failed",
                       ["11:06:00 git pull --ff-only (main) at b5706ee",
                        "11:06:20 ERROR fatal: unable to access "
                        "'https://github.com/...': Could not resolve host",
                        "11:06:20 ERROR git pull failed (exit 1)"],
                       error="git pull failed (exit 1)", host="radxa-01"
                       ).save(out / "pull_failed.png")
    render.message_screen("restarting", "now at c0ffee1, UI back in ~15 s",
                          host="radxa-01").save(out / "restarting.png")
    remote = {"phase": "armed", "cue": "c12", "label": "Look22 P02",
              "boards": list(range(1, 17)), "live": list(range(1, 17)),
              "saved": list(range(1, 17)), "failed": [], "error": None,
              "fire_at": 102.4, "fired_at": None, "late_ms": None,
              "standby_ready": False}
    render.remote_screen(remote, ["13:00:01 start REMOTE",
                                  "13:00:05 cue c12 saved 16/16 in 3.6 s"],
                         now=100.0, host="radxa-03"
                         ).save(out / "remote_armed.png")
    render.remote_screen(dict(remote, phase="fired", fired_at=102.403,
                              late_ms=3.0, saved=list(range(1, 16)),
                              failed=[16]),
                         ["13:00:05 cue c12 saved 15/16 in 9.8 s",
                          "13:00:08 cue c12 fired +3 ms"],
                         now=103.0, host="radxa-03"
                         ).save(out / "remote_fired.png")
    render.reboot_screen("idle", [], host="radxa-01"
                         ).save(out / "reboot_confirm.png")
    render.reboot_screen("rebooting",
                         ["12:00:00 reboot: sudo -n systemctl reboot",
                          "12:00:00 reboot requested - going down"],
                         host="radxa-01").save(out / "reboot_going.png")
    render.reboot_screen("failed",
                         ["12:00:00 reboot: sudo -n systemctl reboot",
                          "12:00:00 ERROR sudo: a password is required",
                          "12:00:00 ERROR reboot refused (exit 1)"],
                         error="reboot refused (exit 1)", host="radxa-01"
                         ).save(out / "reboot_failed.png")
    rows = [(1, "FW_260917"), (2, "FW_260903"), (7, "V1.0 6-color (no OTA)"),
            (20, "V1.1 16-color, build unknown")]
    render.versions_screen(rows[:1], "scanning 3/20...", "scanning",
                           "FW_260917", host="radxa-01"
                           ).save(out / "versions_scanning.png")
    render.versions_screen(rows, "4/20 boards answer", "done", "FW_260917",
                           host="radxa-01").save(out / "versions_done.png")
    render.versions_screen([], "no serial port", "done", "FW_260917",
                           host="radxa-01").save(out / "versions_noport.png")
    usb = "USB 0483:5740  bcd 0200  /dev/ttyACM0"
    render.boardinfo_screen(
        [("SERIAL", "5CF26F473930", ""),
         ("TYPE", "3930 (differs from most: 324C)", "warn"),
         ("FW", "V1.4 16-color (FW_260923+)", ""),
         ("FLASHED", "FW_260923 09-28 18:00 here", "")],
        False, usb_line=usb, host="radxa-01").save(out / "boardinfo_3930.png")
    render.boardinfo_screen(
        [("SERIAL", "48E8854C324C", ""), ("TYPE", "324C", ""),
         ("FW", "(not read while the PC is driving)", ""),
         ("", "no flash record here", "")],
        False, usb_line=usb, host="radxa-01").save(out / "boardinfo_pc.png")
    print(f"wrote preview screens to {out}")
    return 0


def check(boards=None) -> int:
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

    ok &= check_boards(port, boards)
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


def check_boards(port: str | None, boards=None,
                 patience_s: float = 20.0) -> bool:
    """Ask every board to answer, and report which ones are present.

    A silent board is reported but does not fail the check: the wall is
    built for 20 boards and runs with whatever subset is powered, so
    during bring-up most sockets are empty on purpose. The whole scan
    shares one patience budget, sweeping the list again while time is
    left - a board ignores everything for the 9.8 s its e-paper takes
    to repaint, and after a power-on the factory autoplay is doing
    exactly that, so one silent pass must not misread it as missing.

    Needs the port to itself: stop the service first if it is running.
    """
    import time

    from epaper.commands import stop
    from epaper.transport import Bus

    boards = list(boards or DEFAULT_BOARDS)
    if not port:
        return False
    try:
        bus = Bus(port, verbose=False)
    except Exception as exc:
        print(f"boards            : cannot open port ({exc})")
        print("                    stop epaper-ui/epaper-demo first")
        return False
    with bus:
        groups = max(len(boards), max(boards))
        status = {board: "no answer" for board in boards}
        deadline = time.monotonic() + patience_s
        while True:
            pending = [b for b in boards if status[b] != "ACK"]
            if not pending or time.monotonic() >= deadline:
                break
            for board in pending:
                ack = bus.request(stop(board, groups), retries=1)
                if ack is not None and ack.src != board:
                    ack = None          # a late ACK from another board
                if ack is not None:
                    status[board] = ("ACK" if ack.cmd == 0x80
                                     else f"NAK 0x{ack.cmd:02X}")
            time.sleep(1.0)
        for board in boards:
            print(f"board {board:>2}          : {status[board]}")
        answering = sum(1 for s in status.values() if s == "ACK")
        print(f"boards            : {answering}/{len(boards)} answering")
    return answering > 0


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
                    default=None,
                    help="board addresses to drive (default: explore 1..60 "
                         "and stop past the last board that answers; boards "
                         "that do not answer are skipped and re-probed)")
    ap.add_argument("--interval", type=float, default=60.0,
                    help="seconds between panel refreshes (default 60)")
    # 30 s since the 2026-09-26 rehearsal: the tops on radxa-04 lost its
    # LAST cue every time while the skirt on radxa-05 did not - the only
    # thing that reaches the boards after the last fire is this guard
    # STOP, and 0x17 inside a repaint leaves the picture half drawn (or
    # not begun: SPECIFICATION 4.2). Its boards repaint slower than the
    # 7 s the 12 s default assumed. Between cues the next fire replaces
    # the guard, so a longer wait costs nothing there.
    ap.add_argument("--guard-delay", type=float, default=30.0)
    # The landing check (ui/runner.py's VERIFY_AFTER_S). OFF by default
    # since the 2026-09-26 rehearsal: the current firmware answers 0x02
    # while it repaints, so the check read every landed cue as lost and
    # re-sent it - a double repaint on nearly every cue. It stays opt-in
    # until the boards' behaviour during a repaint is measured again.
    ap.add_argument("--verify-fire", action="store_true",
                    help="check that a cue's show broadcast reached the "
                         "boards (one read-only query after the cue, one "
                         "re-send if the witness board answered) - only "
                         "after the deaf window has been measured on the "
                         "boards in use; off by default")
    ap.add_argument("--no-verify-fire", action="store_true",
                    help="(kept for older service files) the default")
    # The fire-time re-send (ui/runner.py's RESEND_STALL_MS). OFF by default
    # (PM, after the review of 349dcdd): a stalled show frame is one of two
    # states - DEGRADED (accepted, never executed, master silent: radxa-07)
    # or SLOW (every picture appears ~0.36 s late, master answers: LOOK23) -
    # and a re-send in the second paints the slot twice. There is no safe
    # question to tell them apart at fire time (0x02 is never answered on
    # this firmware; a unicast STOP there could cancel a delayed picture),
    # so even when turned on it only says the stall and re-sends nothing.
    ap.add_argument("--resend-on-stall", action="store_true",
                    help="say a cue whose own broadcast blocked 200 ms or "
                         "more; it is NOT re-sent - there is no safe way "
                         "to ask the master right after a show frame (0x02 "
                         "is never answered on this firmware, and a STOP "
                         "could cancel a delayed picture; 2026-09-28). The "
                         "pre-cue check and the idle recovery are the cure")
    ap.add_argument("--no-resend-on-stall", action="store_true",
                    help="(the default; accepted for service files that "
                         "name it)")
    # Kill switches for the bus recovery (ui/runner.py, docs/SPECIFICATION.md
    # 4.5). All on by default; each one is reported in /status, so the PC
    # can see what a unit is actually running.
    ap.add_argument("--precheck", type=float, default=PRECHECK_S,
                    metavar="SECONDS",
                    help=f"how long before each cue the unit checks its "
                         f"serial bus (a timed broadcast STOP and a "
                         f"unicast STOP to a live board - its ACK is the "
                         f"answer) "
                         f"and cures a degraded one with a USB reset "
                         f"(default {PRECHECK_S:g}; 0 switches it off). "
                         f"Never inside the 5 s before a trigger, never "
                         f"inside the last picture - so only a cue about "
                         f"40 s or more after the one before it is checked. "
                         f"At {PRECHECK_S:g} both questions and the reset "
                         f"fit with every limit at once; with less, a check "
                         f"can run out of time for the reset (and one miss "
                         f"behind a fast STOP is then left unreset)")
    ap.add_argument("--no-port-watch", action="store_true",
                    help="do not watch the USB device node every 0.2 s; a "
                         "re-enumeration is then found at the next write, "
                         "as before 2026-09-28")
    ap.add_argument("--no-auto-recover", action="store_true",
                    help="do not recover the bus on its own: not after two "
                         "stalled heartbeats, not before an owed probe "
                         "sweep, and no USB reset when a sweep finds no "
                         "boards answering; the PC's Recover bus button "
                         "still works")
    ap.add_argument("--no-keep-away", action="store_true",
                    help="do not send the keep-away stop: by default the "
                         "master never goes 60 s without a broadcast STOP "
                         "while the PC drives the unit - once 40 s have "
                         "passed one goes out after the last picture is "
                         "complete and 1.5 s or more before the next cue, "
                         "even inside the heartbeat's own hold (the master "
                         "resumes its factory autoplay ~85 s after the last "
                         "STOP; SPECIFICATION 4.6)")
    ap.add_argument("--verify-after", type=float, default=VERIFY_AFTER_S,
                    help=f"seconds after a cue's broadcast (and after the "
                         f"witness board's own sweep start) before that "
                         f"check asks (default {VERIFY_AFTER_S})")
    ap.add_argument("--verify-witness", choices=(WITNESS_USB, WITNESS_ANY),
                    default=WITNESS_USB,
                    help="which board that check may ask: usb (default) "
                         "asks only the board on the USB cable, since a "
                         "relayed query is not known to be safe "
                         "(SPECIFICATION 5.7); any asks whichever live "
                         "board starts repainting first - only after the "
                         "bench test in DEVELOPMENT.md section 6")
    # The same rehearsal's other finding, a morning later: between two
    # cues nothing reaches the boards at all, and the tops' master
    # restarted its own autoplay in that gap (SPECIFICATION 4.2, 5.4).
    ap.add_argument("--remote-guard", type=float, default=REMOTE_GUARD_S,
                    metavar="SEC",
                    help=f"while the show PC drives this unit, re-send the "
                         f"broadcast stop this often when the worker is idle "
                         f"and no cue is near (default {REMOTE_GUARD_S:g}; "
                         f"0 disables)")
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
    ap.add_argument("--firmware", metavar="BIN",
                    help="the one OTA image the UPDATE FW menu row offers "
                         "(default: every .bin under FW/FW_*, newest first, "
                         "LEFT/RIGHT on the screen picks another)")
    ap.add_argument("--no-usb-rebind", action="store_true",
                    help="after an update, do not cycle the xhci host "
                         "controller (needs sudo) when the rebooted board "
                         "fails to re-enumerate")
    ap.add_argument("--no-remote", action="store_true",
                    help="do not start the agent the show PC drives this "
                         "unit through (ui/agent.py)")
    ap.add_argument("--remote-port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--remote-token", metavar="TOKEN",
                    help="require this X-Show-Token on every agent request")
    ap.add_argument("--max-ticks", type=int,
                    help="exit after N UI ticks (testing)")
    args = ap.parse_args()

    if args.preview:
        return preview(args.preview)
    if args.check:
        return check(args.boards)

    port = args.port or find_port()
    # Pass args.port, not the detected one: pinning the name found at
    # startup would defeat the runner's re-detection, and a USB replug
    # can bring the boards back as ttyACM1. `port` is only the label.
    runner = DemoRunner(boards=args.boards, interval=args.interval,
                        guard_delay=args.guard_delay, slot=args.slot,
                        remote_guard=args.remote_guard,
                        port=args.port,
                        verify_fire=args.verify_fire and not args.no_verify_fire,
                        verify_after=args.verify_after,
                        verify_witness=args.verify_witness,
                        # --no-resend-on-stall is the default spelled out,
                        # a no-op: only --resend-on-stall turns it on.
                        resend_on_stall=args.resend_on_stall,
                        precheck=args.precheck,
                        port_watch=not args.no_port_watch,
                        auto_recover=not args.no_auto_recover,
                        keep_away=not args.no_keep_away)

    firmware = Path(args.firmware) if args.firmware else find_firmware()
    images = [firmware] if args.firmware else find_firmware_images()
    updater = FirmwareUpdater(firmware, images=images,
                              boards=args.boards, port=args.port,
                              rebind=None if args.no_usb_rebind else usb_rebind)
    puller = RepoPuller()
    rebooter = Rebooter()
    versions = BoardVersions(boards=args.boards, port=args.port)
    # Their own (bounded) USB descriptor reads also fill the runner's cache
    # behind /status usb_board - read on the unit's own screen, never by
    # /status itself.
    boardinfo = BoardInfo(versions, port=args.port, cache=runner)
    updater.usb_board_sink = runner.note_usb_board
    host = socket.gethostname() or None

    remote = agent = demo_store = None
    if not args.no_remote:
        remote = RemoteSession(runner)
        player = ShowPlayer(remote)
        demo_store = DemoStore()
        remote.on_release = player.stop
        agent = Agent(remote, port=args.remote_port, token=args.remote_token,
                      commit=puller.before.commit, name=host, player=player,
                      demos=demo_store)
        try:
            print(f"remote agent on port {agent.start()}", flush=True)
        except OSError as exc:
            # A second instance, or the port taken: the unit still works
            # from its own buttons, which matters more than the agent.
            print(f"remote agent not started: {exc}", flush=True)
            remote = agent = demo_store = None
        else:
            # A unit that restarted in the middle of a show rejoins it.
            player.restore()
            if player.running or player.restored_running:
                # Not white. The garment is holding a picture of this
                # show, and the standby paint would flash it white for
                # the length of a probing sweep (16 s with six absent
                # boards, radxa-01 2026-09-25) in the middle of the
                # show. `restored_running` covers the case where the T0
                # still needs the PC: the picture stays either way.
                args.no_standby = True
                print("show restored after restart, no standby", flush=True)

    display_kwargs = {"directory": args.frames} if args.display in ("png", "auto") else {}
    with make_display(args.display, **display_kwargs) as display, \
            make_input(args.input) as inputs:
        app_kwargs = {"port_label": port, "locked": args.locked,
                      "updater": updater, "puller": puller, "host": host,
                      "versions": versions, "rebooter": rebooter,
                      "boardinfo": boardinfo,
                      "remote": remote,
                      "player": player if remote is not None else None,
                      "demos": demo_store}
        if args.blank_after is not None:
            app_kwargs["blank_after"] = args.blank_after
        app = App(display, inputs, runner, **app_kwargs)
        if remote is not None:
            app.show_status = player.status
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
