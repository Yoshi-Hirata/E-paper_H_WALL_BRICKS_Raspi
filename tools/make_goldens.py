"""tools/make_goldens.py - the Python side of the JS/Python cross-check.

Reads tests/fixtures/sim/*.csv and, when present, conductor/web/starter/
*.csv (the 10 items' committed real maps and sample grids - never
showdata/, which is git-ignored and only exists on the show PC: the
goldens must reproduce identically from committed files alone), and
writes tests/goldens/model.json and conductor/web/sim/goldens.js -
the SAME data, so a browser that never runs Python can still prove its
JS model matches conductor/look.py, conductor/sequence.py and
conductor/timeline.py exactly, digit for digit and string for string.

    python tools/make_goldens.py            # regenerate both files
    python tools/make_goldens.py --check     # exit 1 if either is stale

Hard rules for both output files (tests/test_sim_goldens.py checks
this): sorted keys, no timestamps, no absolute paths, "\n" endings,
UTF-8. Run this after any change to a tests/fixtures/sim/*.csv file, to
conductor/look.py, conductor/sequence.py or conductor/timeline.py, or
to this script.
"""
from __future__ import annotations

import argparse
import copy
import io
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from conductor import look, sequence, timeline  # noqa: E402
from conductor.look import Design, LookError, LookMap  # noqa: E402
from conductor.server import Workspace  # noqa: E402

FIXTURES_DIR = ROOT / "tests" / "fixtures" / "sim"
# The real-data state-digest cases come from the committed starter files
# (Coder Q's conductor/web/starter/*.csv - the 10 items' maps and sample
# grids), never from showdata/ (git-ignored, client data that only
# exists on the show PC): goldens must be reproducible from committed
# files alone, or `make_goldens.py --check` fails on any other machine.
STARTER_DIR = ROOT / "conductor" / "web" / "starter"
GOLDEN_JSON = ROOT / "tests" / "goldens" / "model.json"
GOLDEN_JS = ROOT / "conductor" / "web" / "sim" / "goldens.js"

FNV_OFFSET = 0xcbf29ce484222325
FNV_PRIME = 0x100000001b3
MASK64 = (1 << 64) - 1


def digest64(text: str) -> str:
    h = FNV_OFFSET
    for b in text.encode("utf-8"):
        h ^= b
        h = (h * FNV_PRIME) & MASK64
    return format(h, "016x")


def fixed(x, n: int) -> str:
    return f"{x:.{n}f}"


def canonical_string(s: str) -> str:
    out = ['"']
    for ch in s:
        code = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif code == 0x08:
            out.append("\\b")
        elif code == 0x0C:
            out.append("\\f")
        elif code == 0x0A:
            out.append("\\n")
        elif code == 0x0D:
            out.append("\\r")
        elif code == 0x09:
            out.append("\\t")
        elif code < 0x20 or code > 0x7E:
            if code > 0xFFFF:                 # a surrogate pair, matching
                v = code - 0x10000            # how JS iterates UTF-16 code
                hi = 0xD800 + (v >> 10)        # units one at a time
                lo = 0xDC00 + (v & 0x3FF)
                out.append(f"\\u{hi:04x}\\u{lo:04x}")
            else:
                out.append(f"\\u{code:04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def canonical(v) -> str:
    """Value-based, not Python-type-based: a number that IS a whole
    number prints as an int, any other number as fixed(x,3) - matching
    model.js's SIM.fmt.canonical exactly (JS numbers carry no int/float
    type of their own, so both sides use the same value rule)."""
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, (int, float)):
        fv = float(v)
        iv = int(fv)               # raises OverflowError/ValueError on inf/nan,
        if fv == iv and abs(fv) < 1e15:  # matching model.js's explicit throw there
            return str(iv)
        return fixed(fv, 3)
    if isinstance(v, str):
        return canonical_string(v)
    if isinstance(v, (list, tuple)):
        return "[" + ",".join(canonical(x) for x in v) + "]"
    if isinstance(v, dict):
        items = sorted(v.items(), key=lambda kv: str(kv[0]))
        return "{" + ",".join(f"{canonical_string(str(k))}:{canonical(v2)}"
                              for k, v2 in items) + "}"
    return canonical_string(str(v))


# A pinned table both sides must reproduce: tests/test_sim_goldens.py's
# own Python-only check imports this list directly (DRY with the golden
# "canonical" cases below, which are what actually exercises model.js's
# SIM.fmt.canonical()/digest64() in the browser self-test).
CANONICAL_TABLE = [0, 5, -5, 2.5, 0.5, "a", 'a"b', True, False, None,
                   [1, 2, "x"], {"b": 1, "a": 2}, "日本語"]


def canonical_cases() -> list:
    cases = []
    for value in CANONICAL_TABLE:
        text = canonical(value)
        cases.append({"kind": "canonical", "value": value,
                      "expectCanonical": text, "expectDigest": digest64(text)})
    return cases


def load_fixture(name: str) -> str:
    path = FIXTURES_DIR / name
    return path.read_text(encoding="utf-8")


def map_summary(m: LookMap) -> dict:
    return {
        "name": m.name, "item": m.item, "warnings": list(m.warnings),
        "shifts": {f"{side}|{row}": v for (side, row), v in m.shifts.items()},
        "sides": list(m.sides), "boardNos": list(m.board_nos),
        "scales": [[s.side, s.row, s.col, s.board_no, s.socket, s.label]
                  for s in m.scales],
    }


def design_summary(d: Design) -> dict:
    return {
        "name": d.name, "item": d.item, "pattern": d.pattern, "label": d.label,
        "colors": {f"{s}|{r}|{c}": v for (s, r, c), v in d.colors.items()},
        "shifts": {f"{s}|{r}": v for (s, r), v in d.shifts.items()},
        "undecided": sorted(f"{s}|{r}|{c}" for (s, r, c) in d.undecided),
    }


def auto_map_item(name: str):
    match = look._MAP_NAME.match(Path(name).stem)
    return match.group(1) if match else None


def parse_map_fixture(name: str, item="auto"):
    """(LookMap|None, problems, item-used) - name is the fixture's own
    filename, so error messages ("name:line") match what
    SIM.look.parseMap(text, {name, item}) produces for the identical
    text and opts. `item="auto"` derives it the normal way (mapItem());
    pass an explicit value (including None) to match a test that hands
    LookMap.parse() no item, same as tests/test_look.py does."""
    text = load_fixture(name)
    used_item = auto_map_item(name) if item == "auto" else item
    try:
        return LookMap.parse(io.StringIO(text), name=name, item=used_item), [], used_item
    except LookError as exc:
        return None, list(exc.problems), used_item


def parse_design_fixture(name: str):
    text = load_fixture(name)
    item, pattern, label = Design.name_parts(name)
    try:
        design = Design.parse(io.StringIO(text), name=name, item=item, pattern=pattern)
        design.label = label
        return design, [], item, pattern
    except LookError as exc:
        return None, list(exc.problems), item, pattern


# ============================================================
# fmt cases
# ============================================================

def fmt_cases() -> list:
    cases = []

    def add(op, x, n, expect):
        cases.append({"kind": "fmt", "op": op, "x": x, "n": n, "expect": expect})

    # Every x.x5 tie at 1 decimal, -5.0..5.0 (the round-half-to-even
    # headline case: 2.25 -> "2.2", not the naive "2.3").
    for k in range(-50, 51):
        x = k * 0.1 + 0.05
        add("fixed", x, 1, fixed(x, 1))
        add("round", x, 1, round(x, 1))
    # Every x.xx5 tie at 2 decimals, -1.0..1.0.
    for k in range(-100, 101):
        x = k * 0.01 + 0.005
        add("fixed", x, 2, fixed(x, 2))
        add("round", x, 2, round(x, 2))
    # Every x.5 tie at 0 decimals (roundInt), -20..20.
    for k in range(-20, 21):
        x = k + 0.5
        add("roundInt", x, None, int(round(x)))
        add("round", x, 0, round(x, 0))
    # The literal one-board fixture case and a few named landmarks.
    for x in (2.25, -2.25, 0.125, 2.675, 1.005, 0.0, -0.0, 7.0, 100.0):
        add("fixed", x, 1, fixed(x, 1))
        add("fixed", x, 2, fixed(x, 2))
        add("round", x, 1, round(x, 1))
        add("roundInt", x, None, int(round(x)))
    # %g representatives (own_refresh's format spec).
    for x in (7.0, 0.5, 99.0, -3.0, 0.3, 61.5, 1.0, 60.0, 0.0001, 123456.0,
              1234565.0, 100.0, -0.0):
        add("g", x, None, f"{x:g}")
    return cases


# ============================================================
# clock / mmss cases
# ============================================================

def clock_cases() -> list:
    cases = []

    def add_parse(text, expect=None, error=False):
        cases.append({"kind": "clock", "op": "parseClock", "input": text,
                     "error": error, "expect": expect})

    def add_format(seconds, expect):
        cases.append({"kind": "clock", "op": "formatClock", "input": seconds,
                     "error": False, "expect": expect})

    add_parse("3:05", 185)
    add_parse("1:03:05", 3785)
    add_parse("185", 185.0)
    add_parse(185, 185.0)
    add_parse("soon", error=True)
    add_format(185, "3:05")
    add_format(-16, "-0:16")
    add_format(0, "0:00")
    add_format(3599, "59:59")
    return cases


def mmss_cases() -> list:
    cases = []

    def add(op, arg, expect):
        cases.append({"kind": "mmss", "op": op, "input": arg, "expect": expect})

    for text, expect in [("3.05", 185), ("3.5", 185), ("3", 180), ("3:05", 185),
                         ("3.63", None), ("soon", None), ("0", 0), ("999.59", 59999)]:
        add("parse", text, expect)
    for sec, expect in [(185, "3.05"), (0, "0.00"), (5, "0.05"), (65, "1.05")]:
        add("format", sec, expect)
    for sec, expect in [(185, "3 min 05 s"), (0, "0 min 00 s"), (65, "1 min 05 s")]:
        add("human", sec, expect)
    return cases


# ============================================================
# map / design / check cases
# ============================================================

MAP_FIXTURES = ["Sample_map.csv", "SampleShift_map.csv", "Skirt_map.csv",
                "BadHeader_map.csv", "DoubledColumn_map.csv",
                "SocketRange_map.csv", "DupSocket_map.csv", "BadShift_map.csv",
                "BlankShift_map.csv", "OneBoard_map.csv", "SingleScale_map.csv",
                "Seq_map.csv", "CenterPlain_map.csv", "CenterShift_map.csv"]

DESIGN_FIXTURES = ["Sample_color_pattern01_grid.csv", "Skirt_color_pattern01_grid.csv",
                   "Sample_color_undecided_grid.csv", "Sample_color_zerowhite_grid.csv",
                   "Sample_color_extracells_grid.csv"]

# kind()/name_parts() on both spellings of a design file's name, the
# _HW.csv one included (2026-09-26). `items` is the garments the caller
# already knows about - the only way to tell where the item ends when the
# 配色案名 itself carries underscores.
NAME_CASES = [
    ("Sample_map.csv", None),
    ("Sample_color_pattern01_grid.csv", None),
    ("Look22_color_ref_multicolor_redorange_s22_grid_A-1.csv", None),
    ("AZ271SD1301_1_HW.csv", None),
    ("AZ271SD1301_summer_2_HW.csv", None),
    ("AZ271SD1301_summer_2_HW.csv", ["AZ271SD1301"]),
    ("AZ271SD1305_B_1_HW.csv", None),
    ("AZ271SD1305_B_1_HW.csv", ["AZ271SD1305", "AZ271SD1305_B"]),
    ("AZ271SD1301_pattern03_HW.csv", None),
    ("AZ271SD1301_HW.csv", None),          # no 配色案名: not a grid
    ("notes.csv", None),
    ("Sample_map.txt", None),
]


def name_cases() -> list:
    cases = []
    for filename, items in NAME_CASES:
        item, pattern, label = Design.name_parts(filename, items)
        cases.append({"kind": "names", "filename": filename, "items": items,
                      "expect": {"kind": look.kind(filename),
                                 "parts": [item, pattern, label]}})
    return cases


def map_cases() -> list:
    cases = []
    for name in MAP_FIXTURES:
        m, problems, item = parse_map_fixture(name)
        expect = {"ok": True, "map": map_summary(m)} if m else {"ok": False, "problems": problems}
        cases.append({"kind": "map", "fixture": name, "opts": {"name": name, "item": item},
                      "expect": expect})
    return cases


def design_cases() -> list:
    cases = []
    for name in DESIGN_FIXTURES:
        d, problems, item, pattern = parse_design_fixture(name)
        expect = {"ok": True, "design": design_summary(d)} if d else {"ok": False, "problems": problems}
        cases.append({"kind": "design", "fixture": name,
                      "opts": {"name": name, "item": item, "pattern": pattern},
                      "expect": expect})
    return cases


def check_cases() -> list:
    cases = []

    def add(map_fixture, map_item, design_fixture, calls, note=""):
        m, _, used_item = parse_map_fixture(map_fixture, item=map_item)
        d, _, design_item, pattern = parse_design_fixture(design_fixture)
        if m is None or d is None:
            raise RuntimeError(f"check_cases: {map_fixture}/{design_fixture} did not parse")
        results = []
        for partial in calls:
            results.append(look.check(m, d, partial))
        cases.append({
            "kind": "check", "mapFixture": map_fixture, "designFixture": design_fixture,
            "mapOpts": {"name": map_fixture, "item": used_item},
            "designOpts": {"name": design_fixture, "item": design_item, "pattern": pattern},
            "calls": calls, "note": note,
            "expect": {"results": results, "warningsAfter": list(m.warnings)},
        })

    add("Sample_map.csv", "auto", "Sample_color_pattern01_grid.csv", [False])
    # SampleShift_map.csv, parsed with no item (as tests/test_look.py's
    # MAP_SHIFT case does) - check() then never compares item names, so
    # the shift mismatch is the ONLY thing this case is about.
    add("SampleShift_map.csv", None, "Sample_color_pattern01_grid.csv", [False],
        note="the design's own shift disagrees with the map's shift column on 2 rows")
    # check() called full then partial on the SAME map, as state.js does -
    # the shift-mismatch note must appear once, not twice.
    add("SampleShift_map.csv", None, "Sample_color_pattern01_grid.csv", [False, True],
        note="de-duplicated warning across two calls")
    add("Sample_map.csv", "auto", "Sample_color_undecided_grid.csv", [False, True],
        note="undecided colour: blocks a full cue, not a partial one")
    add("Sample_map.csv", "auto", "Sample_color_zerowhite_grid.csv", [False, True],
        note="0 typed for white is caught, not lost - even when partial")
    return cases


# ============================================================
# ranks / span cases
# ============================================================

def ranks_cases() -> list:
    cases = []
    for map_fixture in ["Seq_map.csv", "OneBoard_map.csv", "SingleScale_map.csv",
                        "CenterPlain_map.csv", "CenterShift_map.csv",
                        "CenterTie_map.csv",
                        # A centroid that falls BETWEEN scales, so the raw
                        # centre distances start at 1 and the rebasing in
                        # ranks() is what makes the first scale rank 0
                        # (F2, 2026-09-26).
                        "CenterGap_map.csv"]:
        m, problems, used_item = parse_map_fixture(map_fixture)
        if m is None:
            continue
        for seq_name in sequence.SEQUENCES:
            ranked = sequence.ranks(m, seq_name)
            ranked_by_key = {f"{s}|{r}|{c}": v for (s, r, c), v in ranked.items()}
            for span in (0.0, 2.25, 3.0, 30.1):
                span_s = sequence.span_s(m, seq_name, span)
                cases.append({
                    "kind": "ranks", "mapFixture": map_fixture,
                    "opts": {"name": map_fixture, "item": used_item},
                    "sequence": seq_name,
                    "span": span, "expect": {"ranks": ranked_by_key, "spanS": span_s},
                })
    return cases


# ============================================================
# timeline cases (transcribed from tests/test_timeline.py)
# ============================================================

OK = {"full": True, "partial": True}


def _clean_one(raw):
    return timeline.clean([raw])[0]


def _cue(id_, item, at, design, partial=False, refresh_s=None, sweep=None, span=None):
    c = _clean_one({"id": id_, "item": item, "at": at, "design": design,
                    "partial": partial, "refresh_s": refresh_s})
    if sweep is not None:
        c["sweep"] = sweep
    if span is not None:
        c["span"] = span
    return c


def _serialize_cue(c: dict) -> dict:
    """The subset of a cleaned cue that reconstructs it exactly: feeding
    these fields back through SIM.timeline.clean() reproduces the same
    cue (clean() only reads id/item/at/design/partial/refresh_s plus
    transition/sequence/span_s, which these test cues never set); a
    manually-injected "sweep"/"span" (the sweep tests, which set these
    directly the way tests/test_timeline.py does, without going through
    apply_transitions) rides along separately."""
    return {
        "id": c["id"], "item": c["item"], "at": c["at"], "design": c["design"],
        "partial": c["partial"], "refresh_s": c["refresh_s"],
        "sweep": c.get("sweep"), "span": c.get("span"),
    }


def _run_timeline_case(name, cues, items, duration=600.0, refresh=7.0, gap=1.0):
    problems, warnings = timeline.validate(cues, items, duration, refresh, gap)
    ends = timeline.ends(cues, refresh, duration)
    unit_boards = {}
    for fact in items.values():
        bus = fact["unit"] or f"({fact['item']})"
        unit_boards[bus] = unit_boards.get(bus, 0) + fact["boards"]
    out_cues = []
    for c in cues:
        sent, complete = timeline.times(c, refresh)
        end, end_source = ends[c["id"]]
        out_cues.append({
            "id": c["id"], "sent": sent, "complete": complete,
            "refresh": timeline.effective_refresh(c, refresh),
            "refresh_source": "cue" if isinstance(c.get("refresh_s"), (int, float)) else "show",
            "end": end, "end_source": end_source,
        })
    return {
        "kind": "timeline", "name": name,
        "cues": [_serialize_cue(c) for c in cues], "items": items,
        "duration": duration, "refresh": refresh, "gap": gap,
        "expect": {
            "problems": problems, "warnings": warnings, "cues": out_cues,
            "minInterval": {u: timeline.min_interval(n, refresh) for u, n in unit_boards.items()},
        },
    }


def timeline_cases() -> list:
    cases = []
    items = {"look22": {"item": "Look22", "unit": "radxa-03", "boards": 16,
                        "designs": {"p1": OK, "p2": OK,
                                    "accent": {"full": False, "partial": True},
                                    "broken": {"full": False, "partial": False}}}}

    a = _cue("a", "Look22", "1:00", "p1")
    cases.append(_run_timeline_case("basic_times", [a], items))

    preset_a = _cue("a", "Look22", 0, "p1")
    preset_b = _cue("b", "Look22", 1, "p2")
    cases.append(_run_timeline_case("rejoin_after_preset", [preset_a, preset_b], items))

    own_ref = _cue("a", "Look22", "1:00", "p1", refresh_s=3.0)
    cases.append(_run_timeline_case("own_refresh", [own_ref], items))

    bad_ref = _cue("c", "Look22", "1:00", "p1", refresh_s=99)
    cases.append(_run_timeline_case("refresh_out_of_range", [bad_ref], items))

    items2 = {"look22": {"item": "Look22", "unit": "radxa-01", "boards": 2,
                         "designs": {"p1": OK, "p2": OK}}}
    early = _cue("a", "Look22", 60, "p1")
    late = _cue("b", "Look22", 65, "p2")
    cases.append(_run_timeline_case("same_item_overlap", [early, late], items2))
    late_ok = _cue("b", "Look22", 72, "p2")
    cases.append(_run_timeline_case("same_item_overlap_cleared", [early, late_ok], items2))

    items16 = {"look22": {"item": "Look22", "unit": "radxa-04", "boards": 16,
                          "designs": {"p1": OK, "p2": OK}}}
    a16 = _cue("a", "Look22", 60, "p1")
    tight = _cue("b", "Look22", 67.9, "p2")
    cases.append(_run_timeline_case("refresh_binding_tight", [a16, tight], items16))
    fine = _cue("b", "Look22", 68.0, "p2")
    cases.append(_run_timeline_case("refresh_binding_fine", [a16, fine], items16))

    items32 = {"look22": {"item": "Look22", "unit": "radxa-05", "boards": 32,
                          "designs": {"p1": OK, "p2": OK}}}
    cases.append(_run_timeline_case("write_binding", [
        _cue("a", "Look22", 60, "p1"), _cue("b", "Look22", 68.0, "p2")], items32))

    sweep_items = {"look22": {"item": "Look22", "unit": "radxa-06", "boards": 16,
                              "designs": {"p1": OK, "g1.csv": {"full": True, "partial": True}}}}
    a_s = _cue("a", "Look22", 60, "p1")
    b_s = _cue("b", "Look22", 68.0, "g1.csv",
              sweep={"sequence": "top_down", "span_s": 2.0, "source": "cue"}, span=2.0)
    cases.append(_run_timeline_case("sweep_doubles_write_term", [a_s, b_s], sweep_items))

    swept = _cue("a", "Look22", 49, "g1.csv",
                sweep={"sequence": "top_down", "span_s": 4.0, "source": "cue"}, span=4.0)
    tight2 = _cue("b", "Look22", 49 + 12 - 1, "g1.csv")
    items1 = {"look22": {"item": "Look22", "unit": "radxa-01", "boards": 2,
                         "designs": {"g1.csv": {"full": True, "partial": True}}}}
    cases.append(_run_timeline_case("sweep_lengthens_the_gap", [swept, tight2], items1))

    no_map_items = {"look22": {"item": "Look22", "unit": None, "boards": 2,
                               "designs": {"g1.csv": {"full": True, "partial": True}}}}
    sweep_no_map = _cue("a", "Look22", 60, "g1.csv",
                       sweep={"sequence": "center", "span_s": 0.1, "source": "cue"})
    cases.append(_run_timeline_case("sweep_without_its_map", [sweep_no_map], no_map_items))

    sweep_too_long = _cue("a", "Look22", 60, "g1.csv",
                          sweep={"sequence": "top_down", "span_s": 30.1, "source": "cue"}, span=30.1)
    cases.append(_run_timeline_case("sweep_over_max_delay", [sweep_too_long],
                                    {"look22": {"item": "Look22", "unit": "radxa-01", "boards": 2,
                                               "designs": {"g1.csv": {"full": True, "partial": True}}}}))

    cases.append(_run_timeline_case("no_preset_warning",
        [_cue("a", "Look22", "0:05", "p1")], items))

    cases.append(_run_timeline_case("design_kind_checks", [
        _cue("a", "Look22", "1:00", "nope"), _cue("b", "Look22", "2:00", "accent"),
        _cue("c", "Look22", "3:00", "accent", partial=True),
        _cue("d", "Look22", "4:00", "broken", partial=True),
        _cue("e", "Look99", "5:00", "p1")], items))

    cases.append(_run_timeline_case("after_end_and_double_booking", [
        _cue("a", "Look22", "6:00", "p1"), _cue("b", "Look22", "6:00", "p2")], items, duration=300.0))

    shared_items = {
        "look20-top": {"item": "Look20-Top", "unit": "radxa-02", "boards": 16,
                       "designs": {"t1": OK, "t2": OK}},
        "look20-skirt": {"item": "Look20-Skirt", "unit": "radxa-02", "boards": 16,
                         "designs": {"s1": OK, "s2": OK}},
    }
    same_moment = [_cue("a", "Look20-Top", 53, "t1"), _cue("b", "Look20-Skirt", 53, "s1")]
    cases.append(_run_timeline_case("items_share_a_unit_same_moment", same_moment, shared_items))
    staggered = [_cue("a", "Look20-Top", 53, "t1"), _cue("b", "Look20-Skirt", 59, "s1")]
    cases.append(_run_timeline_case("items_share_a_unit_staggered", staggered, shared_items))

    # Pre-burn: a board has MAX_CUES_PER_UNIT (18) usable slots - 19
    # distinct, well-spaced sends still overflow it, naming how many
    # pictures the unit actually carries.
    overflow_items = {"look22": {"item": "Look22", "unit": "radxa-08", "boards": 16,
                                 "designs": {f"p{n}": OK for n in range(19)}}}
    overflow_cues = [_cue(f"c{n}", "Look22", n * 20, f"p{n}") for n in range(19)]
    cases.append(_run_timeline_case("slot_capacity_overflow", overflow_cues, overflow_items))

    return cases


# ============================================================
# state / state-digest cases
# ============================================================

def build_reference_state(project: dict) -> dict:
    """The Python side of §1.3's adaptation: run the REAL Workspace over
    a temp workspace holding project["files"], then strip it down to
    what SIM.buildState(project) promises (no units/history/workspace,
    every item unassigned, music reshaped)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        files_dir = root / "files"
        files_dir.mkdir(parents=True, exist_ok=True)
        for name, text in (project.get("files") or {}).items():
            with open(files_dir / name, "w", encoding="utf-8", newline="") as handle:
                handle.write(text)
        show = copy.deepcopy(project.get("show") or {})
        show.pop("units", None)
        (root / "show.json").write_text(json.dumps(show), encoding="utf-8")
        workspace = Workspace(root)
        state = workspace.state()
    state["units"] = []
    for entry in state["items"]:
        entry["unit"] = None
    state.pop("history", None)
    state.pop("workspace", None)
    music = show.get("music")
    state["music"] = {"name": music.get("name") if isinstance(music, dict) else None, "url": None}
    return state


def _small_project_cases() -> list:
    bundle = json.loads((FIXTURES_DIR / "bundle_v1.json").read_text(encoding="utf-8"))
    projects = [("bundle_v1", {"files": bundle["files"], "show": bundle["show"]})]

    files = {"Sample_map.csv": load_fixture("Sample_map.csv"),
             "Sample_color_pattern01_grid.csv": load_fixture("Sample_color_pattern01_grid.csv")}
    show = {"duration": 120.0, "refresh_s": 7.0,
           "cues": [{"id": "a", "item": "Sample", "at": 0, "design": "Sample_color_pattern01_grid.csv"}],
           "transitions": {}, "labels": {}, "boards": {}, "music": None}
    projects.append(("single_item_preset", {"files": files, "show": show}))

    # state.transitions is the RAW show.transitions dict (server.py's
    # state() never runs _clean_transitions on it) - a span of 0 leaves
    # the sequence visible but times no sweep (sweeps() needs span>0);
    # a span over MAX_DELAY_S (30 s) is still shown as authored AND
    # produces the cue's ">30 s" validate() problem.
    show_zero = {"duration": 120.0, "refresh_s": 7.0,
                "cues": [{"id": "a", "item": "Sample", "at": 0,
                         "design": "Sample_color_pattern01_grid.csv"}],
                "transitions": {"Sample_color_pattern01_grid.csv":
                                {"sequence": "top_down", "span_s": 0}},
                "labels": {}, "boards": {}, "music": None}
    projects.append(("transition_span_zero", {"files": files, "show": show_zero}))

    show_over = {"duration": 120.0, "refresh_s": 7.0,
                "cues": [{"id": "a", "item": "Sample", "at": 0,
                         "design": "Sample_color_pattern01_grid.csv"}],
                "transitions": {"Sample_color_pattern01_grid.csv":
                                {"sequence": "top_down", "span_s": 45}},
                "labels": {}, "boards": {}, "music": None}
    projects.append(("transition_span_over_max", {"files": files, "show": show_over}))

    return projects


def state_cases() -> list:
    cases = []
    for pname, project in _small_project_cases():
        expect = build_reference_state(project)
        cases.append({"kind": "state", "name": pname, "project": project, "expect": expect})
    return cases


def state_digest_cases() -> list:
    """One case per starter item (conductor/web/starter/*.csv - Coder Q's
    committed copy of the 10 garments' real maps and sample grids).
    Never showdata/ (git-ignored, only exists on the show PC): goldens
    must reproduce identically from committed files on any machine, or
    `make_goldens.py --check` fails wherever showdata/ is absent."""
    cases = []
    if not STARTER_DIR.is_dir():
        return cases
    groups: "dict[str, dict]" = {}   # item.lower() -> {"item": item, "files": {name: text}}

    def group_for(item):
        return groups.setdefault(item.lower(), {"item": item, "files": {}})

    for path in sorted(STARTER_DIR.glob("*.csv")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if Workspace.kind(path.name) == "map":
            item = auto_map_item(path.name) or Path(path.name).stem
            group_for(item)["files"][path.name] = text
        elif Workspace.kind(path.name) == "grid":
            item, _pattern, _label = Design.name_parts(path.name)
            group_for(item or Path(path.name).stem)["files"][path.name] = text
    for key in sorted(groups):
        group = groups[key]
        project = {"files": group["files"], "show": {}}
        expected_state = build_reference_state(project)
        cases.append({
            "kind": "state-digest", "name": f"starter-{group['item']}", "project": project,
            "expect": digest64(canonical(expected_state)),
        })
    return cases


# ============================================================
# assembly
# ============================================================

def fixture_digests() -> dict:
    digests = {}
    for path in sorted(FIXTURES_DIR.glob("*.csv")):
        digests[path.name] = digest64(path.read_text(encoding="utf-8"))
    return digests


def fixture_text() -> dict:
    """The fixtures' own CSV text, embedded so a page that only loaded
    goldens.js (no filesystem access on file://) can still run the map/
    design/check/ranks cases against its own SIM.look/SIM.sequence."""
    text = {}
    for path in sorted(FIXTURES_DIR.glob("*.csv")):
        text[path.name] = path.read_text(encoding="utf-8")
    return text


def build_goldens() -> dict:
    cases = []
    cases += fmt_cases()
    cases += canonical_cases()
    cases += clock_cases()
    cases += mmss_cases()
    cases += name_cases()
    cases += map_cases()
    cases += design_cases()
    cases += check_cases()
    cases += ranks_cases()
    cases += timeline_cases()
    cases += state_cases()
    cases += state_digest_cases()
    return {
        "format": "epaper-sim-goldens", "version": 1,
        "fixtures": fixture_digests(),
        "fixtureText": fixture_text(),
        "cases": cases,
    }


def dumps_sorted(data) -> str:
    return json.dumps(data, sort_keys=True, ensure_ascii=True, indent=1) + "\n"


def render_js(data: dict) -> str:
    return ("/*\n"
           " * conductor/web/sim/goldens.js - GENERATED by tools/make_goldens.py.\n"
           " * Do not hand-edit; re-run the generator after changing a fixture CSV\n"
           " * or conductor/look.py, conductor/sequence.py or conductor/timeline.py.\n"
           " */\n"
           "globalThis.SIM = Object.assign(globalThis.SIM || {}, {\n"
           f"  GOLDENS: {dumps_sorted(data).rstrip()}\n"
           "});\n")


def write_files(data: dict) -> None:
    GOLDEN_JSON.parent.mkdir(parents=True, exist_ok=True)
    # write_bytes, not write_text(..., newline="\n") - that parameter needs
    # Python 3.10+ (plan_designer_sim.md: "Python 3.9 stdlib"), and dropping
    # newline= instead of switching to write_bytes would write CRLF on
    # Windows (adversarial review round 2 - F3). Both strings are "\n"-only
    # already, so encoding straight to bytes is exact.
    GOLDEN_JSON.write_bytes(dumps_sorted(data).encode("utf-8"))
    GOLDEN_JS.write_bytes(render_js(data).encode("utf-8"))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true",
                        help="exit 1 if the generated files are not up to date")
    args = parser.parse_args(argv)
    data = build_goldens()
    if args.check:
        current_json = GOLDEN_JSON.read_text(encoding="utf-8") if GOLDEN_JSON.exists() else ""
        current_js = GOLDEN_JS.read_text(encoding="utf-8") if GOLDEN_JS.exists() else ""
        if current_json != dumps_sorted(data) or current_js != render_js(data):
            print("tests/goldens/model.json or conductor/web/sim/goldens.js is "
                 "stale - run python tools/make_goldens.py", file=sys.stderr)
            return 1
        return 0
    write_files(data)
    print(f"wrote {GOLDEN_JSON.relative_to(ROOT)} and {GOLDEN_JS.relative_to(ROOT)} "
         f"({len(data['cases'])} cases, {len(data['fixtures'])} fixtures)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
