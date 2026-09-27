"""The timeline, compiled into what one unit needs to run it alone.

A unit is handed its whole show before the start and then keeps its own
time (ui/showplay.py): the Wi-Fi behind a stage is not something a cue
may depend on. So everything is resolved here, on the PC - designs to
arrays, board numbers to bus addresses, "complete at 2:00" to "send at
1:53" - and the unit's file is just a list of

    {"id", "at", "sent", "slot", "refresh_s", "label", "span",
     "boards": {addr: hex}, "state": {addr: hex}, "delays": {addr: hex}}

in the order they are sent. `sent` is seconds from the start (the preset,
loaded before START, has a negative one). `refresh_s` is the refresh
this moment actually waits for - the slowest of the cues sharing it, a
cue's own override or the show's default, never below one physical
panel repaint (conductor/timeline.py's panel_refresh()) - so a unit on
different firmware still gets the right room before its next write, and
the guard STOP it places at `refresh_s + span + margin` (ui/runner.py
`_guard_for()`) can never fall inside a repaint or a sweep.

One unit cue is one broadcast, so items that share a unit (Look 20's top
and skirt) and change at the same instant are one cue. Every cue writes
ALL the unit's boards: the ones it does not touch get an array of 0xFF,
"refresh nothing", because the show command is a broadcast and a board
left with an older array in its slot would repaint that.

`boards` is the change, `state` is the picture after it - the change
laid over everything before. A unit that comes late to a cue (rebooted,
started mid-show, jumped by NEXT) sends `state` instead and is right
again in one refresh, whatever it missed. (Since the pre-burn design the
unit burns `state` and reads nothing from `boards`; the diff stays in
the file only because the unit's validate_show still requires the key -
a follow-up in docs/STATUS.md.)

`delays` is a delay table per board (V1.4's 0x1F: 64 sockets of uint16
frames, or NO_DELAY) for EVERY cue and EVERY board of the unit. A cue
without a sweep carries the all-NO_DELAY table, which the unit writes
as "forget the sweep" (0x25) into that cue's slot: a board must never
keep a table from an earlier upload in a slot the new show uses without
one. The unit writes only a table that differs from the last one it
sent to that board and slot, so the cost is file size (256 hex
characters per board and cue), not burn time on a re-upload.

Every cue also carries `"slot"`: which of the board's 20 on-board storage
slots this picture is written into, one slot per cue in send order (the
preset first, as q00 -> slot 1). Slot 0 is the standby white and slot 19
is reserved for the manual one-shot (Designs tab Prepare, a standalone
demo) - never the show's own timeline - so a show's cues use slots 1-18
(conductor/timeline.py's MAX_CUES_PER_UNIT; the show carries the board's
total as `"slot_capacity"`, SLOT_CAPACITY). The unit writes every cue's
picture into its slot once, at Upload time (a "burn"), rather than during
the show - so a running send is just a broadcast trigger naming a slot,
never a write. Confirmed safe by the manufacturer
(docs/MERIS_REPLY_3SLOT.md, 2026-09-24): all 20 slots are identical and
writable at any time, and 0x13/0x1F/0x1B persist across power cycles, so
a burned slot survives a reboot. validate() reports a show that asks a
unit for more than 18 pictures.
"""

from __future__ import annotations

import hashlib
import json
import re
import struct
import unicodedata

from . import timeline
from .look import (ARRAY_LEN, MARKER, NO_REFRESH, Design, LookError, LookMap,
                   compile_design, unit_board_ids)
from .sequence import FRAME_S, NO_DELAY, compile_delays

DELAY_UNIT_MS = round(FRAME_S * 1000)   # 10: what a unit table's frame is
# The delay table of a board that sweeps nothing: every socket NO_DELAY.
# The unit writes it as "forget the sweep" (0x25) into the cue's slot.
NO_SWEEP_TABLE = struct.pack(">64H", *([NO_DELAY] * ARRAY_LEN))

NUMBER_BRAND = 0x03


def blank() -> bytearray:
    """An array that refreshes nothing."""
    array = bytearray([NO_REFRESH] * ARRAY_LEN)
    array[0] = array[-1] = MARKER
    return array


def lay_over(state: bytearray, change: bytes) -> None:
    for index in range(1, ARRAY_LEN - 1):
        if change[index] != NO_REFRESH:
            state[index] = change[index]


# Everything the unit's own screen cannot draw. ui/render.py writes with
# DejaVuSans, which has no CJK glyphs at all and whose coverage of the
# full-width forms is not something to bet a dress rehearsal on: a label
# it cannot draw comes out as a row of tofu boxes, which tells the
# operator less than nothing.
_UNIT_SCREEN = re.compile(r"[^\x20-\x7e]+")


def unit_label(text: str) -> str:
    """A cue label as the UNIT should show it: NFKC, then any run of
    characters outside printable ASCII as a single "?".

    This is the one place width is folded (2026-09-26, the operator's
    call): the 配線ナビ writes 配色案名 with full-width characters and the
    workspace, show.json, every cue.design reference and the Conductor's
    own screens keep them exactly as typed. Only the string that travels
    to a unit is reduced, and only because that unit's font cannot draw
    it - design "１" reaches the screen as "1", design "柄A" as "?A".
    Nothing in ui/ changes, and nothing here reads back: this is a label,
    never an identifier.
    """
    return _UNIT_SCREEN.sub("?", unicodedata.normalize("NFKC", str(text)))


def design_label(look_map: LookMap, design: Design, partial: bool) -> str:
    name = design.label or design.name
    return unit_label(f"{look_map.item} {name}") + ("*" if partial else "")


def build_unit_show(unit: str, maps: "list[LookMap]",
                    designs: "dict[tuple[str, str], Design]",
                    cues: "list[dict]", refresh: float, duration: float,
                    name: str = "show",
                    clear_after_show: bool = False) -> dict:
    """One unit's show file. `designs` is keyed (item lower-cased, file);
    `cues` are the timeline's cues for the items in `maps`.

    `clear_after_show` (show.json's own key) is carried into the file
    AFTER the id is computed - see below."""
    ids = unit_board_ids(maps)
    by_item = {(m.item or m.name).lower(): m for m in maps}
    addresses = sorted(ids.values())

    moments: "dict[float, list[dict]]" = {}
    for cue in cues:
        sent, _ = timeline.times(cue, refresh)
        moments.setdefault(sent, []).append(cue)

    # A sweep is a delay table per board (the firmware request). EVERY
    # cue carries a table for EVERY board of the unit - a cue (or an
    # item sharing the unit) without a sweep says "no delay" for its
    # boards - so a board never keeps a sweep it should not, whether
    # from an earlier cue or from an earlier upload that had one where
    # this show has none (found in review: the tables used to be left
    # out of a show without sweeps, and the boards kept the old ones).
    # The unit only writes a table that differs from the last it sent.
    state = {address: blank() for address in addresses}
    unit_cues = []
    for number, sent in enumerate(sorted(moments)):
        change = {address: blank() for address in addresses}
        delays = {address: NO_SWEEP_TABLE for address in addresses}
        labels = []
        span = 0.0
        for cue in sorted(moments[sent], key=lambda c: c["item"].lower()):
            look_map = by_item[cue["item"].lower()]
            design = designs[(cue["item"].lower(), cue["design"])]
            arrays = compile_design(look_map, design, partial=cue["partial"],
                                    ids=ids)
            for address, array in arrays.items():
                change[address] = bytearray(array)
            labels.append(design_label(look_map, design, cue["partial"]))
            span = max(span, timeline.span_of(cue))
            if timeline.sweeps(cue):
                delays.update(compile_delays(
                    look_map, cue["sweep"]["sequence"],
                    cue["sweep"]["span_s"], ids=ids))
        for address in addresses:
            lay_over(state[address], change[address])
        entry = {
            "id": f"q{number:02d}",
            "at": min(float(c["at"]) for c in moments[sent]),
            "sent": round(sent, 3),
            "slot": number + 1,      # 1..MAX_CUES_PER_UNIT (18); 0 is the
                                     # standby white, 19 the manual one-shot
            # Items sharing a moment (one broadcast) may want different
            # refresh times (different firmware): the unit waits for the
            # slowest one before it may write the next cue's boards.
            # timeline.panel_refresh() is what a unit is told - the
            # cue's own refresh, floored at one physical repaint - so
            # the guard STOP it computes from refresh_s + span can never
            # land before the picture is really finished.
            "refresh_s": max(timeline.panel_refresh(c, refresh)
                             for c in moments[sent]),
            "label": " + ".join(labels),
            "span": span,
            "boards": {str(a): bytes(change[a]).hex() for a in addresses},
            "state": {str(a): bytes(state[a]).hex() for a in addresses},
            "delays": {str(a): delays[a].hex() for a in addresses},
        }
        unit_cues.append(entry)

    show = {"name": name, "unit": unit, "dev_type": NUMBER_BRAND,
            "refresh_s": refresh, "duration": duration,
            "delay_unit_ms": DELAY_UNIT_MS,
            "slot_capacity": timeline.SLOT_CAPACITY,
            "boards": addresses, "cues": unit_cues}
    digest = hashlib.sha1(json.dumps(show, sort_keys=True).encode()).hexdigest()
    show["id"] = digest[:10]
    # Added AFTER the digest on purpose: ticking "Clear pictures after the
    # show" must not change the show's IDENTITY. Nothing about the
    # pictures changes, so a fleet that already holds this timeline goes
    # on holding it - no "✗ old version" on ten tiles, no re-Upload of
    # every picture, and no supervision reload the evening of the show.
    # The conductor sends POST /show/clear itself when the run ends or is
    # stopped; this key is the unit's OWN fallback, for a show that ends
    # with the PC gone, and it reaches the unit with the next Upload.
    if clear_after_show:
        show["clear_after_show"] = True
    return show


def build(maps: "dict[str, LookMap]", assigned: "dict[str, str]",
          load_design, cues: "list[dict]", refresh: float, duration: float,
          cue_problems: "dict[str, list[str]]", name: str = "show",
          only: "list[str] | None" = None,
          clear_after_show: bool = False
          ) -> "tuple[dict[str, dict], list[str], list[str]]":
    """({unit: show file}, problems, warnings). Nothing is built while the
    timeline still has a problem: a show goes out whole or not at all.

    `only` is the write's own definition of "whole" - the units of the
    one LOOK the operator picked in the dialog's "Which LOOKs"
    (conductor/fleet.py's `upload(only=...)`, which posts to nobody
    else). A problem about an item on ANOTHER unit cannot be about that
    unit's show file: it is not built from those cues, those boards or
    that garment's map, and the unit is not written to at all. So with
    `only` a problem is blocking when it belongs to a TARGETED unit's
    item, and a `warning` - reported, not refused - when it does not.

    Which is the whole point (2026-09-27): the operator asked for one bag
    on radxa-09 while the other five garments were between units, and the
    upload was refused with five "not assigned to a unit" about garments
    that were never going to be written. An unassigned item has no unit
    at all, so it can never be a targeted one's, and its cues can never
    reach a unit either way.

    A full upload (`only` None) keeps the rule as it was: every problem
    blocks, so START can never be handed a fleet built from half a
    timeline. "the timeline has no cues" blocks either way - there is
    nothing to write for anyone.
    """
    targeted = None if only is None else set(only)

    problems: "list[str]" = []
    warnings: "list[str]" = []
    # The units a warning is about: not written here, and not built
    # either - a cue of theirs may name a design that never loaded.
    warned: "set[str]" = set()

    def unit_of(item: str) -> "str | None":
        look_map = maps.get(item.lower())
        return assigned.get(look_map.item if look_map is not None else item)

    def note(unit: "str | None", text: str) -> None:
        if targeted is None or (unit is not None and unit in targeted):
            problems.append(text)
            return
        warnings.append(text)
        if unit is not None:
            warned.add(unit)

    for cue in cues:
        for problem in cue_problems.get(cue["id"], []):
            note(unit_of(cue["item"]),
                 f"{timeline.format_clock(cue['at'])} "
                 f"{cue['item']}: {problem}")
    used = {cue["item"].lower() for cue in cues}
    for key in sorted(used):
        look_map = maps.get(key)
        if look_map is not None and not assigned.get(look_map.item):
            note(None, f"{look_map.item}: not assigned to a unit")
    if not cues:
        problems.append("the timeline has no cues")
    if problems:
        return {}, problems, warnings

    by_unit: "dict[str, list[LookMap]]" = {}
    for key, look_map in maps.items():
        unit = assigned.get(look_map.item)
        if unit:
            by_unit.setdefault(unit, []).append(look_map)
    shows = {}
    for unit, unit_maps in sorted(by_unit.items()):
        if unit in warned:
            continue        # a problem of its own, and not this write's
        keys = {(m.item or m.name).lower() for m in unit_maps}
        unit_cues = [c for c in cues if c["item"].lower() in keys]
        if not unit_cues:
            continue
        try:
            designs = {(c["item"].lower(), c["design"]):
                       load_design(c["design"]) for c in unit_cues}
            shows[unit] = build_unit_show(unit, unit_maps, designs, unit_cues,
                                          refresh, duration, name,
                                          clear_after_show=clear_after_show)
        except (OSError, LookError) as exc:
            note(unit, f"{unit}: {exc}")
    if problems:
        return {}, problems, warnings
    return shows, problems, warnings
