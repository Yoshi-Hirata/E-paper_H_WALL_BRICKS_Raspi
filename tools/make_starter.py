"""Build the designer simulator's starter data (plan_designer_sim.md §3.2, §5):

  * copies every CSV in showdata/files/ (the site's wiring maps + the sample
    designs - kept out of git, see the "az27ss-looks" memory note) into
    conductor/web/starter/*.csv, verbatim except line endings (normalised to
    "\\n", matching how the app itself stores dropped files - see
    designer-app.js's addFiles());
  * writes conductor/web/sim/starter.js, a committed JS file (fetch() does not
    work on file://) holding `SIM.STARTER = {files: {...}, show: {...}}`.

Deterministic: no timestamps, no absolute paths, sorted keys, "\\n" endings,
UTF-8. Run with --check to verify the committed output is up to date (used by
tests/test_designer_build.py::test_starter_is_current) instead of writing it.

Usage: make_starter.py [--source DIR] [--check]
    --source DIR   where the CSVs live (default: <repo>/showdata/files)
"""
import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STARTER_DIR = REPO / "conductor" / "web" / "starter"
STARTER_JS = REPO / "conductor" / "web" / "sim" / "starter.js"

# The show line-up's own labels, copied from the production show.json (the
# operator page's `labels` map) so a designer opening the simulator reads the
# same "LOOK 23 · AZ271SD1305" the operator does - the starter used to write
# {"look": n, "model": ""}, which showed every garment as "LOOK 23" with an
# empty "model no." box next to it.
#
# item -> (LOOK number, model number). An empty LOOK is deliberate and matches
# production: the three bags are not part of the numbered line-up, so they are
# named by their model alone and the designer's own LOOK field stays blank
# ("-") until someone types a number. Two garments CAN share a LOOK (the skirt
# and the top of LOOK 26 ride one Radxa), which is why the model number is
# what tells them apart.
STARTER_LABELS = {
    "AZ271SD1305":   ("23", "AZ271SD1305"),
    "AZ271SD1305_B": ("24", "AZ271SD1305"),
    "AZ271SD1301":   ("25", "AZ271SD1301"),
    "AZ271SB2303":   ("26", "AZ271SB2303 (Skirt)"),
    "AZ271SC6302":   ("26", "AZ271SC6302 (Tops)"),
    "AZ271SD1306":   ("27", "AZ271SD1306"),
    "AZ271SD1307":   ("28", "AZ271SD1307"),
    "AZ271SG1035":   ("",   "AZ271SG1035 (Bag 01)"),
    "AZ271SG1036":   ("",   "AZ271SG1036 (Bag 02)"),
    "AZ271SG3037":   ("",   "AZ271SG3037 (Bag 03)"),
}

# Derived, never a second hand-maintained list (the two used to drift): the
# items that DO carry a LOOK number, which is what orders the line-up.
STARTER_LOOKS = {item: look for item, (look, _model) in STARTER_LABELS.items() if look}

DEFAULT_DURATION_S = 600.0
DEFAULT_REFRESH_S = 7.0


def kind(name: str) -> "str | None":
    import re
    stem = name[:-4] if name.lower().endswith(".csv") else None
    if stem is None:
        return None
    if re.search(r"_color_.+grid", stem, re.IGNORECASE):
        return "grid"
    if re.search(r"_map$", stem, re.IGNORECASE):
        return "map"
    return None


def map_item(name: str) -> str:
    import re
    m = re.match(r"(.+?)_map", name, re.IGNORECASE)
    return m.group(1) if m else name[:-4]


def grid_item(name: str) -> "str | None":
    import re
    m = re.match(r"(.+?)_color_", name, re.IGNORECASE)
    return m.group(1) if m else None


def read_normalised(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")


def build_files(source: Path) -> "dict[str, str]":
    files = {}
    for path in sorted(source.glob("*.csv")):
        if kind(path.name) is None:
            print(f"make_starter: skipping {path.name} (not a *_map.csv or "
                  "*_color_NAME_grid.csv)", file=sys.stderr)
            continue
        files[path.name] = read_normalised(path)
    return files


def build_show(files: "dict[str, str]") -> dict:
    items = sorted({map_item(n) for n in files if kind(n) == "map"})
    designs_of = {item: sorted(n for n in files if kind(n) == "grid" and grid_item(n) == item)
                  for item in items}

    def look_rank(item):
        look = STARTER_LOOKS.get(item)
        return (0, int(look)) if look else (1, item)
    ordered = sorted(items, key=look_rank)

    cues = []
    cue_id = 1
    for i, item in enumerate(ordered):
        designs = designs_of.get(item, [])
        if not designs:
            continue
        cues.append({"id": cue_id, "item": item, "at": 0, "design": designs[0],
                     "partial": False, "refresh_s": None, "transition": "design",
                     "sequence": "natural", "span_s": 0})
        cue_id += 1
        if len(designs) > 1:
            at = 30.0 + i * 40.0
            cues.append({"id": cue_id, "item": item, "at": at, "design": designs[1],
                         "partial": False, "refresh_s": None, "transition": "design",
                         "sequence": "natural", "span_s": 0})
            cue_id += 1

    labels = {item: {"look": STARTER_LABELS[item][0], "model": STARTER_LABELS[item][1]}
              for item in ordered if item in STARTER_LABELS}

    return {"duration": DEFAULT_DURATION_S, "refresh_s": DEFAULT_REFRESH_S,
            "cues": cues, "transitions": {}, "labels": labels, "boards": {}, "music": None}


def render_js(files: "dict[str, str]", show: dict) -> str:
    payload = {"files": dict(sorted(files.items())), "show": show}
    body = json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True)
    return ("/* starter.js — GENERATED by tools/make_starter.py. Do not edit by hand.\n"
            " * The show's own wiring maps and sample designs (showdata/files, kept out\n"
            " * of git), copied verbatim (line endings normalised to \\n) so the page can\n"
            " * seed a first-ever-open workspace without fetch() on file://. */\n"
            "globalThis.SIM = Object.assign(globalThis.SIM || {}, { STARTER: "
            + body + " });\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", type=Path, default=REPO / "showdata" / "files")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    if not args.source.is_dir():
        print(f"make_starter: {args.source} does not exist - nothing to do "
              "(showdata/ is gitignored; ask for a copy of the wiring maps)", file=sys.stderr)
        return 0 if args.check else 1

    files = build_files(args.source)
    show = build_show(files)
    js_text = render_js(files, show)

    if args.check:
        # Byte comparison, not read_text()/read_normalised() (adversarial
        # review round 2 - F7): both of those do their own newline handling
        # (universal-newline translation, or this file's own CRLF->LF
        # normalisation for reading legitimately-CRLF SOURCE data), so a
        # committed starter.js or starter CSV corrupted to CRLF by a bad
        # Windows checkout would silently compare equal to the correct
        # "\n"-only content every time - --check exists specifically to
        # catch a stale (or corrupted) generated file, so it must compare
        # exactly what write_bytes() below would write.
        ok = True
        if not STARTER_JS.exists() or STARTER_JS.read_bytes() != js_text.encode("utf-8"):
            print("make_starter --check: conductor/web/sim/starter.js is stale", file=sys.stderr)
            ok = False
        for name, text in files.items():
            p = STARTER_DIR / name
            if not p.exists() or p.read_bytes() != text.encode("utf-8"):
                print(f"make_starter --check: conductor/web/starter/{name} is stale", file=sys.stderr)
                ok = False
        existing = {p.name for p in STARTER_DIR.glob("*.csv")} if STARTER_DIR.is_dir() else set()
        extra = existing - set(files)
        if extra:
            print(f"make_starter --check: extra file(s) in conductor/web/starter/: {sorted(extra)}", file=sys.stderr)
            ok = False
        return 0 if ok else 1

    STARTER_DIR.mkdir(parents=True, exist_ok=True)
    for old in STARTER_DIR.glob("*.csv"):
        if old.name not in files:
            old.unlink()
    # write_bytes, not write_text(..., newline="\n") - that parameter needs
    # Python 3.10+ and this repo promises 3.9 (plan_designer_sim.md: "Python
    # 3.9 stdlib"). Every string here is already "\n"-only, so encoding
    # straight to bytes is exact and platform-independent.
    for name, text in files.items():
        (STARTER_DIR / name).write_bytes(text.encode("utf-8"))
    STARTER_JS.write_bytes(js_text.encode("utf-8"))
    print(f"make_starter: wrote {len(files)} CSV(s) to {STARTER_DIR} and {STARTER_JS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
