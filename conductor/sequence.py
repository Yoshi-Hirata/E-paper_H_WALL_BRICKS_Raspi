"""In what order the scales of a garment change.

A board refreshes its sockets P01 -> P60 about 0.1 s apart, in an order
the wiring decided. With the per-segment delay table of the firmware
request (docs/FW_REQUEST_SEGMENT_DELAY.md), which V1.4 (FW_260923)
shipped as 0x1F/0x25, the host can say instead when each socket starts,
so the change sweeps the garment in a direction:

    natural      as the wiring has it (no table)
    center       outward from the middle of the garment
    top_down     row by row from the top
    bottom_up    row by row from the hem
    left_right   column by column from the audience's left
    right_left   column by column from the audience's right

Every scale gets a rank (0, 1, 2 ...); its delay is proportional to that
rank, scaled so the last rank's delay is exactly the sweep's span (the
seconds from the first scale's command to the last one's). Rows and
columns are the grid's own (one row = one step, one column = one step),
which is what the designer sees in the CSV. Left and right are the
audience's, for each side as they face it: the CSV is drawn from the
inside, and the site's columns run towards the wearer's right on both
sides, so the front is mirrored and the back is not. The centre is the
front's centroid; the back uses the point straight behind it.
"""

from __future__ import annotations

import math
import struct

from .look import ARRAY_LEN, LookMap, default_shift

SEQUENCES = ("natural", "center", "top_down", "bottom_up",
             "left_right", "right_left")
LABELS = {"natural": "Socket order (P01 to P60)",
          "center": "Centre outward",
          "top_down": "Top to bottom",
          "bottom_up": "Bottom to top",
          "left_right": "Left to right (audience)",
          "right_left": "Right to left (audience)"}
FRAME_S = 0.01                  # V1.4's delay table unit (7.4.2)
NO_DELAY = 0xFFFF               # a socket without a scale; the runner sends 0
TABLE_LEN = 128                 # 64 sockets x uint16, big-endian
MAX_DELAY_S = 30.0              # firmware guidance -> validate() problem
SPAN_HARD_MAX_S = 120.0         # clean_span()'s clamp, so the file stays sane


def clean_sequence(value) -> str:
    return value if value in SEQUENCES else "natural"


def clean_span(value) -> float:
    try:
        span = round(float(value), 2)
    except (TypeError, ValueError):
        return 0.0
    if span != span:                     # NaN: junk, same as an unusable value
        return 0.0
    return min(SPAN_HARD_MAX_S, max(0.0, span))


def ranks(look_map: LookMap, sequence: str) -> "dict[tuple, int]":
    """position -> rank (0 first) for every scale of the map."""
    scales = look_map.scales
    if sequence == "natural" or not scales:
        return {s.position: 0 for s in scales}
    rows = [s.row for s in scales]
    if sequence == "top_down":
        top = max(rows)
        return {s.position: top - s.row for s in scales}
    if sequence == "bottom_up":
        hem = min(rows)
        return {s.position: s.row - hem for s in scales}
    if sequence in ("left_right", "right_left"):
        # Audience's left: for the front the wearer's right (the last
        # column), for the back the wearer's left (the first column).
        result = {}
        for side in look_map.sides:
            cols = [s.col for s in scales if s.side == side]
            first, last = min(cols), max(cols)
            from_wearers_right = (side == "front") == (sequence == "left_right")
            for s in scales:
                if s.side == side:
                    result[s.position] = (last - s.col if from_wearers_right
                                          else s.col - first)
        return result
    if sequence == "center":
        front = [s for s in scales if s.side == "front"] or scales
        xs = [s.col + default_shift(s.row) for s in front]
        ys = [s.row for s in front]
        cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
        return {s.position: int(round(math.hypot(
            s.col + default_shift(s.row) - cx, s.row - cy))) for s in scales}
    raise ValueError(f"unknown sequence {sequence!r}")


def span_s(look_map: LookMap, sequence: str, span: float) -> float:
    """The sweep's effective span: 0 for a natural sequence or a garment
    whose ranks never move (one scale, or all of a kind) - there is
    nothing to time. Otherwise `span` itself: the caller already knows
    the seconds wanted from the first scale to the last."""
    if sequence == "natural":
        return 0.0
    if max(ranks(look_map, sequence).values(), default=0) == 0:
        return 0.0
    return round(float(span), 2)


def compile_delays(look_map: LookMap, sequence: str, span: float,
                   ids: "dict[int, int] | None" = None
                   ) -> "dict[int, bytes]":
    """bus address -> 128-byte delay table (64 x uint16, big-endian, V1.4
    7.4), for every board of the item.

    Index = socket, value = the frame (10 ms) the socket's segment
    starts on; sockets without a scale, and index 0 and 63, are
    NO_DELAY. A natural sequence gives every board a table of NO_DELAY,
    which tells a board that held a sweep to forget it. The highest
    rank always lands on exactly `span` seconds - not "close to it" -
    because frames() multiplies before it divides by the rank count.
    """
    ids = ids or look_map.board_ids
    tables = {ids[no]: [NO_DELAY] * ARRAY_LEN for no in look_map.board_nos}
    if sequence != "natural":
        rank_of = ranks(look_map, sequence)
        max_rank = max(rank_of.values(), default=0)
        if max_rank > 0:
            for scale, rank in rank_of.items():
                frame = max(0, min(0xFFFF, int(round(
                    rank * span / max_rank / FRAME_S))))
                tables[ids[look_map.by_position[scale].board_no]][
                    look_map.by_position[scale].socket] = frame
    return {address: struct.pack(">64H", *table)
            for address, table in tables.items()}
