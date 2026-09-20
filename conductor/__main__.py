"""Check, preview and bench-send a look's CSVs.

    python -m conductor serve                # the web UI, http://127.0.0.1:8765
    python -m conductor check  Look22_map.csv Look22_color_pattern01_grid.csv
    python -m conductor check  Look22_map.csv                 # the map alone
    python -m conductor preview Look22_map.csv GRID.csv -o look22_p01.png
    python -m conductor preview Look22_map.csv -o look22_wiring.png
    python -m conductor dip    Look22_map.csv -o look22_dip.csv
    python -m conductor arrays Look22_map.csv GRID.csv -o look22_p01.json
    python -m conductor send   Look22_map.csv GRID.csv --only 1 2

`send` is for the bench: run it on the machine the boards are plugged
into (stop epaper-ui first - it owns the port). The show itself goes
through the unit's remote agent, not through this.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from .look import Design, LookError, LookMap, compile_design, summary

NUMBER_BRAND = 0x03        # the device type the UI sends (ui/patterns.py)


def _load(args) -> "tuple[LookMap, Design | None]":
    look_map = LookMap.from_csv(args.map)
    design = Design.from_csv(args.grid) if getattr(args, "grid", None) else None
    return look_map, design


def _report(look_map: LookMap, design: "Design | None") -> None:
    for line in summary(look_map, design):
        print(line)
    for warning in look_map.warnings:
        print(f"warning: {warning}")


def cmd_check(args) -> int:
    look_map, design = _load(args)
    if design is not None:
        compile_design(look_map, design, partial=args.partial)
    _report(look_map, design)
    print("OK")
    return 0


def cmd_preview(args) -> int:
    from .preview import render

    look_map, design = _load(args)
    if design is not None:
        compile_design(look_map, design, partial=args.partial)
    render(look_map, design, cell=args.cell).save(args.output)
    print(f"wrote {args.output}")
    return 0


def cmd_dip(args) -> int:
    look_map = LookMap.from_csv(args.map)
    sheet = look_map.dip_sheet()
    out = open(args.output, "w", newline="", encoding="utf-8") \
        if args.output else sys.stdout
    try:
        writer = csv.DictWriter(out, fieldnames=list(sheet[0]))
        writer.writeheader()
        writer.writerows(sheet)
    finally:
        if args.output:
            out.close()
            print(f"wrote {args.output}")
    return 0


def cmd_arrays(args) -> int:
    look_map, design = _load(args)
    arrays = compile_design(look_map, design, partial=args.partial)
    payload = {"map": look_map.name, "design": design.name,
               "dev_type": NUMBER_BRAND,
               "boards": {str(address): array.hex()
                          for address, array in arrays.items()}}
    text = json.dumps(payload, indent=1)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"wrote {args.output}")
    else:
        print(text)
    return 0


def cmd_send(args) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))
    from epaper.commands import (TEST_SLOT, save_color, show_single,
                                 slot_config, stop)
    from epaper.transport import Bus, find_port

    look_map, design = _load(args)
    arrays = compile_design(look_map, design, partial=args.partial)
    targets = [a for a in sorted(arrays) if not args.only or a in args.only]
    if not targets:
        print("no board left to send to", file=sys.stderr)
        return 2
    port = args.port or find_port()
    if not port:
        print("No serial port found. Specify with --port.", file=sys.stderr)
        return 2
    groups = max(len(arrays), max(arrays))
    failed = []
    with Bus(port, verbose=False) as bus:
        for address in targets:
            steps = (("stop", stop(address, groups)),
                     ("slot", slot_config(address, TEST_SLOT,
                                          group_count=groups,
                                          dev_type=NUMBER_BRAND)),
                     ("save", save_color(address, TEST_SLOT, arrays[address],
                                         group_count=groups,
                                         dev_type=NUMBER_BRAND)))
            for label, frame in steps:
                ack = bus.request(frame)
                if ack is None or ack.cmd != 0x80:
                    answer = "no ACK" if ack is None else ack.describe()
                    print(f"board {address:02d} {label}: {answer}")
                    failed.append(address)
                    break
            else:
                print(f"board {address:02d}: saved")
        # Once, never repeated: a board queues what arrives mid-refresh
        # and repaints once per copy (docs/STATUS.md, 2026-08-14).
        bus.send(show_single(0xFF, TEST_SLOT, groups, dev_type=NUMBER_BRAND))
    print(f"show broadcast sent; {len(targets) - len(failed)}/{len(targets)} "
          "boards saved. The refresh takes several seconds.")
    return 1 if failed else 0


def cmd_serve(args) -> int:
    from .server import serve

    return serve(args.workspace, args.port, open_browser=args.open)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m conductor",
                                 description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="command", required=True)

    def add(name, func, grid, output=False, help_=""):
        p = sub.add_parser(name, help=help_)
        p.add_argument("map", help="LookNN_map.csv")
        if grid == "required":
            p.add_argument("grid", help="LookNN_color_patternMM_grid.csv")
        elif grid == "optional":
            p.add_argument("grid", nargs="?",
                           help="LookNN_color_patternMM_grid.csv")
        if grid:
            p.add_argument("--partial", action="store_true",
                           help="scales without a colour keep what they show "
                                "instead of being an error")
        if output:
            p.add_argument("-o", "--output", required=(output == "required"))
        p.set_defaults(func=func)
        return p

    add("check", cmd_check, "optional", help_="validate the CSVs")
    p = add("preview", cmd_preview, "optional", output="required",
            help_="draw the garment (design view, or wiring view "
                  "without a grid)")
    p.add_argument("--cell", type=int, default=26,
                   help="pixels per scale (default 26)")
    add("dip", cmd_dip, None, output=True, help_="DIP switch sheet")
    add("arrays", cmd_arrays, "required", output=True,
        help_="the 64-byte array of every board, as JSON")
    p = add("send", cmd_send, "required",
            help_="bench: write the design to the boards on this machine")
    p.add_argument("--port", help="serial port (default: auto-detect)")
    p.add_argument("--only", nargs="+", type=int, metavar="ID",
                   help="send to these bus addresses only")
    p = sub.add_parser("serve", help="the web UI on this PC (localhost only)")
    p.add_argument("--workspace", default="showdata",
                   help="folder holding the CSVs and show.json "
                        "(default ./showdata)")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--open", action="store_true",
                   help="open the page in the default browser")
    p.set_defaults(func=cmd_serve)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except LookError as exc:
        for problem in exc.problems[:40]:
            print(f"ERROR {problem}", file=sys.stderr)
        if len(exc.problems) > 40:
            print(f"... and {len(exc.problems) - 40} more", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
