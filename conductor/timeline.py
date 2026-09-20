"""The show's timeline: which design each item wears, and when.

Every item (a look, a bag) has its own track of cues, because the
models do not all change together. A cue is

    {"id", "item", "at", "design", "align", "partial"}

`at` is seconds from the start of the show. An e-paper refresh takes
several seconds from the command to the finished image (REFRESH_S), so
a time can mean two things and the cue says which:

    align "done"   the design is complete at `at`   (sent REFRESH_S earlier)
    align "start"  the change begins at `at`        (complete REFRESH_S later)

"done" is the default: a running order says what the look is at a given
moment. A cue at 0:00 is the preset - loaded before START, so the show
opens on it - and its align does not matter.

What one unit can do bounds the timeline. Before a refresh the unit has
to write every board (about 0.22 s each, docs/SCALING.md), and nothing
is sent to a bus that is still refreshing, so two refreshes on the same
unit need

    refresh + boards x 0.22 s + margin

between their send times. Items sharing a unit (Look 20's top and
skirt) share that budget - unless their cues fall on the same instant,
which is one refresh for both.

Pure data in, problems out: no files, no clock, so the rules are
testable and the web page and the units can both rely on them.
"""

from __future__ import annotations

import re

# Full repaint, command to finished image. It has moved with every
# firmware - 9.8 s (first boards), 16 s (production boards, 2026-08-14),
# about 7 s on the latest firmware (reported 2026-09-21) - so this is
# only the default: a show carries its own value (show.json refresh_s,
# editable on the timeline page) and every rule below takes it as an
# argument. A unit still on older firmware needs the older, longer value.
REFRESH_S = 7.0
REFRESH_RANGE_S = (1.0, 60.0)
SAVE_S_PER_BOARD = 0.22    # stop + save, measured
MARGIN_S = 3.0
DEFAULT_DURATION_S = 600.0
ALIGNS = ("done", "start")

_CLOCK = re.compile(r"^\s*(?:(\d+):)?(\d{1,2}):(\d{1,2}(?:\.\d+)?)\s*$")


def parse_clock(text) -> float:
    """'3:05' / '1:03:05' / '185' / 185 -> seconds."""
    if isinstance(text, (int, float)):
        return float(text)
    match = _CLOCK.match(str(text))
    if match:
        hours, minutes, seconds = match.groups()
        return int(hours or 0) * 3600 + int(minutes) * 60 + float(seconds)
    try:
        return float(text)
    except ValueError:
        raise ValueError(f"not a time: {text!r} (write m:ss)")


def format_clock(seconds: float) -> str:
    sign = "-" if seconds < 0 else ""
    total = int(round(abs(seconds)))
    return f"{sign}{total // 60}:{total % 60:02d}"


def min_interval(boards: int, refresh: float = REFRESH_S) -> float:
    """Seconds one unit needs between the send times of two refreshes."""
    return refresh + boards * SAVE_S_PER_BOARD + MARGIN_S


def times(cue: dict, refresh: float = REFRESH_S) -> "tuple[float, float]":
    """(sent, complete) for a cue. The preset is complete at 0."""
    at = float(cue["at"])
    if at <= 0:
        return -refresh, 0.0
    if cue.get("align", "done") == "start":
        return at, at + refresh
    return at - refresh, at


def clean(cues) -> "list[dict]":
    """Whatever the page posted -> well-formed cues, sorted by time."""
    result = []
    for raw in cues or []:
        if not isinstance(raw, dict):
            continue
        align = raw.get("align", "done")
        result.append({
            "id": str(raw.get("id") or f"c{len(result)}")[:40],
            "item": str(raw.get("item", "")),
            "at": max(0.0, round(parse_clock(raw.get("at", 0)), 1)),
            "design": str(raw.get("design", "")),
            "align": align if align in ALIGNS else "done",
            "partial": bool(raw.get("partial", False)),
        })
    result.sort(key=lambda c: (c["at"], c["item"].lower()))
    return result


def validate(cues: "list[dict]", items: "dict[str, dict]",
             duration: float = DEFAULT_DURATION_S,
             refresh: float = REFRESH_S) -> "tuple[dict, list[str]]":
    """({cue id: [problems]}, [warnings about the whole show]).

    `items` maps the lower-cased item name to
        {"item", "unit", "boards": n, "designs": {file: {"full": bool,
                                                       "partial": bool}}}
    where full/partial say whether the design passes that check.
    """
    problems: "dict[str, list[str]]" = {cue["id"]: [] for cue in cues}
    warnings: "list[str]" = []

    for cue in cues:
        mine = problems[cue["id"]]
        item = items.get(cue["item"].lower())
        if item is None:
            mine.append(f"{cue['item']}: no such item (is its map loaded?)")
            continue
        design = item["designs"].get(cue["design"])
        if design is None:
            mine.append(f"design {cue['design'] or '(none)'} is not loaded")
        elif not design["partial" if cue["partial"] else "full"]:
            mine.append(f"{cue['design']} has problems (see the Designs tab)"
                        + ("" if cue["partial"] or not design["partial"] else
                           " - it has undecided scales: make this a partial cue"))
        sent, complete = times(cue, refresh)
        if cue["at"] > duration:
            mine.append(f"{format_clock(cue['at'])} is after the end of the "
                        f"show ({format_clock(duration)})")
        if cue["at"] > 0 and sent < 0:
            mine.append(
                f"cannot be complete at {format_clock(cue['at'])}: a refresh "
                f"takes {refresh:.0f} s. Use 0:00 (the preset, before START) "
                f"or {format_clock(refresh)} and later")

    # One item cannot be told two things at once.
    seen: "dict[tuple, str]" = {}
    for cue in cues:
        key = (cue["item"].lower(), times(cue, refresh)[0])
        if key in seen:
            problems[cue["id"]].append(
                f"{cue['item']} already has a cue sent at the same moment")
        seen.setdefault(key, cue["id"])

    # Each unit's bus: refreshes need room between their send times.
    by_unit: "dict[str, list[dict]]" = {}
    for cue in cues:
        item = items.get(cue["item"].lower())
        if item is not None:
            # An unassigned item still has a bus of its own one day.
            unit = item.get("unit") or f"({item['item']})"
            by_unit.setdefault(unit, []).append(cue)
    for unit, unit_cues in by_unit.items():
        boards = sum(item["boards"] for item in items.values()
                     if (item.get("unit") or f"({item['item']})") == unit)
        need = min_interval(boards, refresh)
        unit_cues.sort(key=lambda c: times(c, refresh)[0])
        previous = None
        for cue in unit_cues:
            sent = times(cue, refresh)[0]
            if previous is not None and sent != previous:
                gap = sent - previous
                if gap < need:
                    problems[cue["id"]].append(
                        f"only {gap:.0f} s after the previous refresh on {unit}; "
                        f"its {boards} boards need {need:.0f} s "
                        f"({refresh:.0f} s refresh + writing the boards)")
            previous = sent

    for key, item in sorted(items.items()):
        track = [c for c in cues if c["item"].lower() == key]
        if track and not any(c["at"] <= 0 for c in track):
            warnings.append(f"{item['item']}: no preset at 0:00 - it opens "
                            "on whatever it showed before the show")
    return problems, warnings
