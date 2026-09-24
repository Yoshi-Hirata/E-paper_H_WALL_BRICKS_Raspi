"""The show's timeline: which design each item wears, and when.

Every item (a look, a bag) has its own track of cues, because the
models do not all change together. A cue is

    {"id", "item", "at", "design", "partial", "refresh_s",
     "transition", "sequence", "span_s"}

    Start ──refresh──▶ (sweep) ──▶ Complete ── held ──▶ End (next Start)

`at` IS **Start**: the instant the unit sends the show command and the
e-paper begins refreshing. The one exception is the preset (`at <= 0`),
shown before START - it is sent one refresh early, so it is already on
the garment (complete at 0:00) when the show begins.

**Complete** is Start + refresh + the sweep's span (server field
`complete`); **End** is the next cue's Start on the same item, or the
show's duration for the last one (ends() below; server fields
`end`/`end_source`, merged into state()'s cues by conductor/server.py).

`refresh_s`, optional: `None` means the show's own refresh time
(show.json's `refresh_s`); a number 1-60 (one decimal) overrides it for
this cue alone - a board on older firmware, a different refresh mode.
`cue["refresh"]` (server state) is the effective value actually used;
`cue["refresh_source"]` says "show" or "cue".

`transition` says whether the cue sweeps as its design does ("design",
the default) or has its own sweep ("custom"). `sequence` and `span_s`
are always stored (so switching back and forth keeps both), but only
used when `transition` is "custom" - see resolve() and
apply_transitions() below, and conductor/sequence.py for what a
sequence and a span mean. `span_s` is seconds from the command to the
first scale to the command to the last one; a sweep makes the change
last longer than one refresh, by that span. The resolved sweep and the
seconds it actually adds (which needs the garment's map) come from the
server, as cue["sweep"] and cue["span"].

What one unit can do bounds the timeline. Every cue's picture is
written into its own on-board slot (1-18; slot 0 is the standby white,
slot 19 the manual one-shot of the Designs tab and the demo rows)
at Upload time - conductor/showfile.py's `build_unit_show` - so nothing
is written any more while the show runs: a running cue's send is one
broadcast trigger (`show_single`) naming its slot. There is therefore
only one bound left, the director's own: at least GAP_AFTER_REFRESH_S
(1 s) between a picture finishing and the next refresh starting -

    refresh + gap

- for every pair of sends on one unit's bus, including the very first
cue after the preset (nothing is written there either any more: the
picture already sits in its slot, burned at Upload time). Items sharing
a unit (Look 20's top and skirt) share that budget - unless their cues
fall on the same instant, which is one refresh for both.

A board holds 18 show pictures (slots 1-18, MAX_CUES_PER_UNIT; slot 0 is
the standby white, slot 19 the manual one-shot), so a unit's bus may
carry at most 18 distinct sends (the preset counts as one) - validate()
below reports a show that asks for more, naming how many it actually
carries.

Pure data in, problems out: no files, no clock, so the rules are
testable and the web page and the units can both rely on them.
"""

from __future__ import annotations

import re

from .sequence import MAX_DELAY_S, clean_sequence, clean_span

# Full repaint, command to finished image. It has moved with every
# firmware - 9.8 s (first boards), 16 s (production boards, 2026-08-14),
# about 7 s on the latest firmware (reported 2026-09-21) - so this is
# only the default: a show carries its own value (show.json refresh_s,
# editable on the timeline page) and every rule below takes it as an
# argument. A unit still on older firmware needs the older, longer value.
REFRESH_S = 7.0
REFRESH_RANGE_S = (1.0, 60.0)
# The director's minimum from "picture complete" to the next send
# (2026-09-24: "Reflesh が終わった後、1 秒後に次のデザインへの refresh に
# 入ることができるようにしたい") - a show setting one day (gap_s), taken
# as an argument here meanwhile, same as refresh.
GAP_AFTER_REFRESH_S = 1.0

# A board has 20 slots. Slot 0 is the standby white; slot 19 is reserved
# for the manual one-shot (Designs tab Prepare / a standalone demo), never
# the show's own timeline; slots 1-18 hold a show's pictures (showfile.py's
# build_unit_show, one per cue in send order). Nothing is written while a
# show runs any more (2026-09-24: "毎回リアルタイムに書き込みを行うのは
# ショーにおいてリスクが高い" - every picture is burned into its slot at
# Upload time instead, docs/MERIS_REPLY_3SLOT.md confirms every slot is
# identical and writable at any time), so a unit's own write speed no
# longer bounds the timeline here - only how many pictures fit on a board.
SLOT_CAPACITY = 20
MAX_CUES_PER_UNIT = 18

DEFAULT_DURATION_S = 600.0

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
    except (TypeError, ValueError):
        # None, a dict, a list from hostile JSON - anything unparsable
        # is the same "not a time", not a crash the caller must expect.
        raise ValueError(f"not a time: {text!r} (write m:ss)")


def format_clock(seconds: float) -> str:
    sign = "-" if seconds < 0 else ""
    total = int(round(abs(seconds)))
    return f"{sign}{total // 60}:{total % 60:02d}"


def min_interval(boards: int, refresh: float = REFRESH_S,
                 gap: float = GAP_AFTER_REFRESH_S) -> float:
    """Seconds one unit needs between the send times of two refreshes:
    the picture is already burned into its slot (Upload time), so a
    running send is one broadcast trigger - nothing is written - and
    the only floor left is the director's own, refresh + gap. `boards`
    is kept (unused) so callers built for the old, per-board write term
    do not need to change; it may matter again if a future board count
    changes how the trigger itself is addressed.

    This is what conductor/server.py shows as the show's "shortest
    interval per unit". validate() below applies the same floor cue by
    cue, with the previous cue's own refresh time.
    """
    return refresh + gap


def span_of(cue: dict) -> float:
    """Seconds a cue's sweep adds to the refresh (0 without one)."""
    try:
        return max(0.0, float(cue.get("span") or 0.0))
    except (TypeError, ValueError):
        return 0.0


def clean_refresh(value) -> "float | None":
    """`None`, or a number rounded to 1 decimal, whatever its range -
    validate() is where "1-60 s" is enforced, as a problem the operator
    sees and can fix, never a silent clamp. Junk (a string, a dict, NaN)
    is `None`: the show's own refresh_s, same as not overriding it."""
    if value is None:
        return None
    try:
        refresh = round(float(value), 1)
    except (TypeError, ValueError):
        return None
    return None if refresh != refresh else refresh      # NaN


def effective_refresh(cue: dict, refresh: float = REFRESH_S) -> float:
    """The refresh this cue actually uses: its own `refresh_s` when it
    is set, else the show's (the argument every caller passes)."""
    own = cue.get("refresh_s")
    return float(own) if isinstance(own, (int, float)) else refresh


def times(cue: dict, refresh: float = REFRESH_S) -> "tuple[float, float]":
    """(sent, complete) for a cue.

    `at` IS sent, Start - except the preset (`at <= 0`), sent one
    refresh before the show begins so it is already complete at 0:00.
    Complete is sent + refresh + the sweep's span, using the cue's own
    refresh when it set one. Rounded to the millisecond: send instants
    are compared and used as keys ("the same moment" is one broadcast,
    showfile.py), and 10.3 - 7.3 is 3.000000000000001, not the 3.0 of a
    cue starting at 3.
    """
    at = float(cue["at"])
    eff = effective_refresh(cue, refresh)
    sent = round(at, 3) if at > 0 else round(-eff, 3)
    complete = round(sent + eff + span_of(cue), 3)
    return sent, complete


def ends(cues: "list[dict]", refresh: float = REFRESH_S,
        duration: float = DEFAULT_DURATION_S) -> "dict[str, tuple[float, str]]":
    """{cue id: (end, "next"|"show")} for every cue - the next cue's
    Start on the same item (by sent order), or the show's duration for
    the last one. Kept separate from validate()/times() since it needs
    every cue of an item's track at once rather than one at a time."""
    result: "dict[str, tuple[float, str]]" = {}
    by_item: "dict[str, list[dict]]" = {}
    for cue in cues:
        by_item.setdefault(cue["item"].lower(), []).append(cue)
    for item_cues in by_item.values():
        ordered = sorted(item_cues, key=lambda c: times(c, refresh)[0])
        for index, cue in enumerate(ordered):
            if index + 1 < len(ordered):
                result[cue["id"]] = (times(ordered[index + 1], refresh)[0],
                                     "next")
            else:
                result[cue["id"]] = (duration, "show")
    return result


def sweeps(cue: dict) -> bool:
    """Whether a cue's resolved sweep (cue["sweep"], apply_transitions())
    actually delays anything - the one definition showfile.py and
    validate() must agree on, so a custom transition left at span 0
    (switched back towards natural but not saved yet) is never charged
    for delay tables it will not write."""
    sweep = cue.get("sweep") or {}
    try:
        span = float(sweep.get("span_s") or 0.0)
    except (TypeError, ValueError):
        return False
    return sweep.get("sequence", "natural") != "natural" and span > 0


def clean(cues) -> "list[dict]":
    """Whatever the page posted -> well-formed cues, sorted by time."""
    result = []
    for raw in cues or []:
        if not isinstance(raw, dict):
            continue
        transition = raw.get("transition")
        result.append({
            "id": str(raw.get("id") or f"c{len(result)}")[:40],
            "item": str(raw.get("item", "")),
            "at": max(0.0, round(parse_clock(raw.get("at", 0)), 1)),
            "design": str(raw.get("design", "")),
            "partial": bool(raw.get("partial", False)),
            "refresh_s": clean_refresh(raw.get("refresh_s")),
            "transition": transition if transition in ("design", "custom")
                         else "design",
            "sequence": clean_sequence(raw.get("sequence", "natural")),
            "span_s": clean_span(raw.get("span_s", 0.0)),
        })
    result.sort(key=lambda c: (c["at"], c["item"].lower()))
    return result


def resolve(cue: dict, transitions: dict) -> dict:
    """The sweep a cue actually plays: its design's transition, unless
    the cue overrides it. `transitions` is show.json's map of design
    file -> {"sequence", "span_s"} (conductor/server.py Workspace).

    `source` says where the values came from - "design" so the page can
    show "design default", "cue" so it shows the cue's own numbers -
    not whether they happen to differ.
    """
    if cue.get("transition", "design") == "custom":
        return {"sequence": clean_sequence(cue.get("sequence", "natural")),
                "span_s": clean_span(cue.get("span_s", 0.0)),
                "source": "cue"}
    entry = transitions.get(cue.get("design", ""))
    entry = entry if isinstance(entry, dict) else {}
    return {"sequence": clean_sequence(entry.get("sequence", "natural")),
            "span_s": clean_span(entry.get("span_s", 0.0)),
            "source": "design"}


def apply_transitions(cues: "list[dict]", transitions: dict) -> None:
    """Give every cue its resolved sweep, as cue["sweep"] - in place, so
    the same list can go on to _time_sweeps() and validate()."""
    for cue in cues:
        cue["sweep"] = resolve(cue, transitions)


def validate(cues: "list[dict]", items: "dict[str, dict]",
             duration: float = DEFAULT_DURATION_S,
             refresh: float = REFRESH_S,
             gap: float = GAP_AFTER_REFRESH_S) -> "tuple[dict, list[str]]":
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
        if cue["at"] > duration:
            mine.append(f"{format_clock(cue['at'])} is after the end of the "
                        f"show ({format_clock(duration)})")
        own_refresh = cue.get("refresh_s")
        if own_refresh is not None:
            low, high = REFRESH_RANGE_S
            if not low <= own_refresh <= high:
                mine.append(f"this cue's refresh time ({own_refresh:g} s) "
                           f"must be {low:.0f}-{high:.0f} s")
        sweep = cue.get("sweep") or {"sequence": "natural", "span_s": 0.0}
        if sweep["sequence"] != "natural":
            if cue.get("span") is None:
                mine.append("the sweep needs the item's map to be timed")
            if sweep["span_s"] > MAX_DELAY_S:
                mine.append("a sweep is at most 30 s from the first scale "
                           "to the last (the firmware's limit)")

    # One item cannot be told two things at once.
    seen: "dict[tuple, str]" = {}
    for cue in cues:
        key = (cue["item"].lower(), times(cue, refresh)[0])
        if key in seen:
            problems[cue["id"]].append(
                f"{cue['item']} already has a cue sent at the same moment")
        seen.setdefault(key, cue["id"])

    # A garment cannot start its next look before this one has finished
    # painting: two cues of the SAME item, in the order they are sent.
    overlapped: "set[str]" = set()
    by_item: "dict[str, list[dict]]" = {}
    for cue in cues:
        by_item.setdefault(cue["item"].lower(), []).append(cue)
    for item_cues in by_item.values():
        ordered = sorted(item_cues, key=lambda c: times(c, refresh)[0])
        for prev, cur in zip(ordered, ordered[1:]):
            prev_sent, prev_complete = times(prev, refresh)
            cur_sent = times(cur, refresh)[0]
            if cur_sent != prev_sent and cur_sent < prev_complete:
                problems[cur["id"]].append(
                    "starts before the previous picture is complete "
                    f"({format_clock(prev_complete)})")
                overlapped.add(cur["id"])

    # Each unit's bus: refreshes need room between their send times, and
    # a board holds only MAX_CUES_PER_UNIT pictures.
    by_unit: "dict[str, list[dict]]" = {}
    for cue in cues:
        item = items.get(cue["item"].lower())
        if item is not None:
            # An unassigned item still has a bus of its own one day.
            unit = item.get("unit") or f"({item['item']})"
            by_unit.setdefault(unit, []).append(cue)
    for unit, unit_cues in by_unit.items():
        # Grouped by send instant, matching showfile.py's own broadcasts:
        # items sharing a unit and an instant (Look 20's top and skirt)
        # are ONE send, and the room needed after it is set by ALL of
        # them together (the slowest refresh, the longest sweep) - not by
        # whichever of them happens to sort last (found in review).
        moment_cues: "dict[float, list[dict]]" = {}
        for cue in unit_cues:
            moment_cues.setdefault(times(cue, refresh)[0], []).append(cue)
        moments = sorted(moment_cues)

        # A board has MAX_CUES_PER_UNIT usable slots for the show (0 is
        # the standby white, 19 the manual one-shot): more distinct sends
        # than that do not fit, whatever their spacing.
        if len(moments) > MAX_CUES_PER_UNIT:
            order = {sent: index for index, sent in enumerate(moments)}
            for cue in unit_cues:
                if order[times(cue, refresh)[0]] >= MAX_CUES_PER_UNIT:
                    problems[cue["id"]].append(
                        f"{unit} carries {len(moments)} pictures but a "
                        f"board holds {MAX_CUES_PER_UNIT} show pictures "
                        "(slot 0 is the white standby, slot 19 the manual "
                        "one-shot) - merge or remove cues")

        previous_sent = previous_group = None
        for sent in moments:
            group = moment_cues[sent]
            if previous_sent is not None:
                spacing = sent - previous_sent
                # Every picture is already burned into its slot at
                # Upload time (showfile.py): a running send is one
                # broadcast trigger, nothing is written - so the only
                # floor left is the director's gap after the PREVIOUS
                # send's own refresh (plus its sweep's span, if any of
                # its cues had one), for every pair, including the first
                # send after the preset.
                before_refresh = max(effective_refresh(c, refresh)
                                     for c in previous_group)
                before_span = max(span_of(c) for c in previous_group)
                need = before_refresh + before_span + gap
                if spacing < need:
                    detail = f"{before_refresh:.1f} s refresh"
                    if before_span:
                        detail += f" + {before_span:.1f} s sweep"
                    detail += f" + {gap:.1f} s gap"
                    for cue in group:
                        same_item = any(cue["item"].lower()
                                        == prev["item"].lower()
                                        for prev in previous_group)
                        if same_item and cue["id"] in overlapped:
                            continue
                        problems[cue["id"]].append(
                            f"only {spacing:.1f} s after the previous send "
                            f"on {unit}; at least {need:.1f} s is needed "
                            f"({detail})")
            previous_sent, previous_group = sent, group

    for key, item in sorted(items.items()):
        track = [c for c in cues if c["item"].lower() == key]
        if track and not any(c["at"] <= 0 for c in track):
            warnings.append(f"{item['item']}: no preset at 0:00 - it opens "
                            "on whatever it showed before the show")
    return problems, warnings
