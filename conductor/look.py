"""A look's CSVs -> the 64-byte colour array of every board.

Two files, as delivered by the designers (2026-09-21, Look22):

  LookNN_map.csv                    side,row,col,board_no,socket,label
      one line per scale: where it sits on the garment and which
      board/socket drives it. Written once per garment.

  LookNN_color_patternMM_grid.csv   side,row,shift,1,2,3,...
      one line per garment row, one column per position; a cell is the
      colour code of the scale there (0x00-0x0F), 0 where the garment
      has no hole, or - for a hole whose colour is not decided yet. One
      file per cue. `shift` (0 / 0.5) is the half-scale offset of that
      row (the rows are laid like bricks), used only to draw the preview.

They join on (side, row, col). row 0 is the hem, the highest row the
neck. Both files are drawn as seen from the INSIDE of the garment (the
side the boards are on) - the designers' README says so - which is the
mirror image of what the audience sees; the previews flip it.

One unit can carry several items: Look 20 is a top and a skirt on one
Radxa. Their boards share one bus, so the addresses are ranked across
all of the unit's maps together (unit_board_ids), not per garment.

board_no is the board's own serial number, not its bus address: within
a look the boards are addressed 1, 2, 3... in ascending board_no (by
rank, so a gap in the numbers leaves no gap in the addresses), and that
address is what the DIP switches are set to (dip_sheet()).

socket N is array index N (P1-P60 of the NUMBER_BRAND layout the UI
already sends). A socket with no scale stays 0xFF - "do not refresh".

"0" against "0x00": a bare 0 means *no hole*, 0x00 means *white*. A
designer who types 0 for white would silently lose scales, so the two
files are cross-checked both ways - a scale in the map with no colour in
the grid is an error, and so is a colour where the map has no scale.
Nothing is converted until every problem in both files has been listed.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

ARRAY_LEN = 64
MARKER = 0xFE              # array start/end (V1.1)
NO_REFRESH = 0xFF          # pad / leave this segment as it is
SOCKETS = range(1, 61)
MAX_BOARDS = 60            # one unit's bus, by the project's own limit
COLOR_COUNT = 16

# FW_260917 palette - FW/FW_260917/260917_16_Color_Chart_ changed.xlsx,
# the chart the designers work from: green is 0x05, turquoise 0x06.
# Kept in step with host/epaper/pattern.py by tests/test_look.py; it is
# repeated here so this module stays importable with the stdlib alone.
PALETTE = [
    ("White", (255, 255, 255)),
    ("Yellow", (255, 255, 0)),
    ("Blue", (0, 0, 255)),
    ("Red", (255, 0, 0)),
    ("Black", (0, 0, 0)),
    ("Green", (0, 200, 0)),
    ("Turquoise", (64, 224, 208)),
    ("Almond", (235, 210, 180)),
    ("Light Pink", (255, 209, 220)),
    ("Sky Blue", (135, 206, 235)),
    ("Orange", (255, 140, 0)),
    ("Yellow Green", (154, 205, 50)),
    ("Olive Gray", (112, 116, 85)),
    ("Brown", (139, 69, 19)),
    ("Dark Brown", (92, 51, 23)),
    ("Smoke Blue", (110, 130, 150)),
]
assert len(PALETTE) == COLOR_COUNT

# The item is whatever precedes _map / _color_patternMM: "Look22", but
# also "Look20-Skirt" (a look in two garments) or a bag's own name.
_MAP_NAME = re.compile(r"(.+?)_map", re.IGNORECASE)
_GRID_NAME = re.compile(r"(.+?)_color_pattern\s*0*(\d+)", re.IGNORECASE)
_MAP_COLUMNS = ("side", "row", "col", "board_no", "socket")
_EMPTY_CELLS = ("", "0")       # no hole here
_UNDECIDED = "-"               # a hole, colour not chosen yet



class LookError(ValueError):
    """Every problem found, not just the first: a designer fixing a CSV
    wants the whole list in one go."""

    def __init__(self, problems: "list[str]"):
        self.problems = list(problems)
        more = f" (+{len(problems) - 1} more)" if len(problems) > 1 else ""
        super().__init__(f"{problems[0]}{more}")


@dataclass(frozen=True)
class Scale:
    side: str
    row: int
    col: int
    board_no: int
    socket: int
    label: str = ""

    @property
    def position(self) -> "tuple[str, int, int]":
        return (self.side, self.row, self.col)


@dataclass
class LookMap:
    """Where every scale of one garment is, and what drives it."""

    name: str
    scales: "list[Scale]"
    item: "str | None" = None
    warnings: "list[str]" = field(default_factory=list)

    @property
    def board_nos(self) -> "list[int]":
        return sorted({s.board_no for s in self.scales})

    @property
    def board_ids(self) -> "dict[int, int]":
        """board_no -> bus address: 1, 2, 3... by ascending board_no."""
        return {no: rank for rank, no in enumerate(self.board_nos, start=1)}

    @property
    def boards(self) -> "list[int]":
        """The bus addresses this look uses - what the runner drives."""
        return list(range(1, len(self.board_nos) + 1))

    @property
    def by_position(self) -> "dict[tuple[str, int, int], Scale]":
        return {s.position: s for s in self.scales}

    @property
    def sides(self) -> "list[str]":
        seen: "list[str]" = []
        for s in self.scales:
            if s.side not in seen:
                seen.append(s.side)
        return seen

    @classmethod
    def from_csv(cls, path) -> "LookMap":
        path = Path(path)
        match = _MAP_NAME.match(path.stem)
        item = match.group(1) if match else None
        with open(path, newline="", encoding="utf-8-sig") as handle:
            return cls.parse(handle, name=path.name, item=item)

    @classmethod
    def parse(cls, lines, name: str = "map", item: "str | None" = None
              ) -> "LookMap":
        reader = csv.DictReader(lines)
        header = [h.strip() for h in (reader.fieldnames or [])]
        missing = [c for c in _MAP_COLUMNS if c not in header]
        if missing:
            raise LookError([f"{name}: missing column(s) {', '.join(missing)}"
                             f" - expected {', '.join(_MAP_COLUMNS)},label"])
        problems: "list[str]" = []
        warnings: "list[str]" = []
        scales: "list[Scale]" = []
        seen_pos: "dict[tuple, int]" = {}
        seen_socket: "dict[tuple, int]" = {}
        for line_no, raw in enumerate(reader, start=2):
            row = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
            if not any(row.values()):
                continue
            where = f"{name}:{line_no}"
            try:
                scale = Scale(row["side"], int(row["row"]), int(row["col"]),
                              int(row["board_no"]), int(row["socket"]),
                              row.get("label", ""))
            except ValueError:
                problems.append(f"{where}: row/col/board_no/socket must be "
                                f"whole numbers: {dict(row)}")
                continue
            if not scale.side:
                problems.append(f"{where}: side is empty")
            if scale.socket not in SOCKETS:
                problems.append(f"{where}: socket {scale.socket} is not 1-60")
            if scale.board_no < 1:
                problems.append(f"{where}: board_no {scale.board_no} "
                                "must be 1 or more")
            if scale.position in seen_pos:
                problems.append(
                    f"{where}: {_pos(scale.position)} already holds a scale "
                    f"(line {seen_pos[scale.position]})")
            seen_pos.setdefault(scale.position, line_no)
            key = (scale.board_no, scale.socket)
            if key in seen_socket:
                problems.append(
                    f"{where}: board {scale.board_no} socket {scale.socket} "
                    f"is already used (line {seen_socket[key]})")
            seen_socket.setdefault(key, line_no)
            expected = f"{scale.board_no:03d}-{scale.socket:02d}"
            if scale.label and scale.label != expected:
                warnings.append(f"{where}: label {scale.label!r} does not "
                                f"match board/socket ({expected})")
            scales.append(scale)
        if not scales and not problems:
            problems.append(f"{name}: no scales")
        boards = {s.board_no for s in scales}
        if len(boards) > MAX_BOARDS:
            problems.append(f"{name}: {len(boards)} boards, but one unit "
                            f"drives at most {MAX_BOARDS}")
        if problems:
            raise LookError(problems)
        return cls(name=name, scales=scales, item=item, warnings=warnings)

    def dip_sheet(self, ids: "dict[int, int] | None" = None) -> "list[dict]":
        """One line per board for whoever sets the DIP switches.

        Pure binary, switch n = bit n-1 (Datasheet/PCBA_DIP_SWITCH_
        H_WALL_BRICKS.png): address 20 is switches 3 and 5.
        """
        counts: "dict[int, int]" = {}
        for s in self.scales:
            counts[s.board_no] = counts.get(s.board_no, 0) + 1
        sheet = []
        ids = ids or self.board_ids
        for board_no in self.board_nos:
            address = ids[board_no]
            switches = [n + 1 for n in range(8) if address >> n & 1]
            sheet.append({"board_no": board_no, "dip_id": address,
                          "switches_on": " ".join(str(n) for n in switches),
                          "scales": counts[board_no]})
        return sheet


@dataclass
class Design:
    """One cue's colours: position -> colour code."""

    name: str
    colors: "dict[tuple[str, int, int], int]"
    shifts: "dict[tuple[str, int], float]" = field(default_factory=dict)
    item: "str | None" = None
    pattern: "int | None" = None
    # Holes written "-": there is a scale, its colour is not decided.
    undecided: "set[tuple[str, int, int]]" = field(default_factory=set)

    @classmethod
    def from_csv(cls, path) -> "Design":
        path = Path(path)
        match = _GRID_NAME.match(path.stem)
        item, pattern = ((match.group(1), int(match.group(2)))
                         if match else (None, None))
        with open(path, newline="", encoding="utf-8-sig") as handle:
            return cls.parse(handle, name=path.name, item=item,
                             pattern=pattern)

    @classmethod
    def parse(cls, lines, name: str = "grid", item: "str | None" = None,
              pattern: "int | None" = None) -> "Design":
        reader = csv.reader(lines)
        header = [h.strip() for h in next(reader, [])]
        if header[:3] != ["side", "row", "shift"]:
            raise LookError([f"{name}: header must start with side,row,shift "
                             f"- got {','.join(header[:3]) or 'nothing'}"])
        problems: "list[str]" = []
        try:
            cols = [int(h) for h in header[3:]]
        except ValueError:
            raise LookError([f"{name}: the columns after shift must be "
                             "position numbers (1,2,3...)"])
        if not cols:
            raise LookError([f"{name}: no position columns after shift"])
        colors: "dict[tuple[str, int, int], int]" = {}
        undecided: "set[tuple[str, int, int]]" = set()
        shifts: "dict[tuple[str, int], float]" = {}
        for line_no, raw in enumerate(reader, start=2):
            cells = [c.strip() for c in raw]
            if not any(cells):
                continue
            where = f"{name}:{line_no}"
            try:
                side, row = cells[0], int(cells[1])
                shift = float(cells[2] or 0)
            except (ValueError, IndexError):
                problems.append(f"{where}: needs side, a whole-number row "
                                "and a shift")
                continue
            if (side, row) in shifts:
                problems.append(f"{where}: {side} row {row} appears twice")
                continue
            shifts[(side, row)] = shift
            if len(cells) - 3 > len(cols):
                problems.append(f"{where}: {len(cells) - 3} cells but only "
                                f"{len(cols)} position columns")
            for col, cell in zip(cols, cells[3:]):
                if cell in _EMPTY_CELLS:
                    continue
                if cell == _UNDECIDED:
                    undecided.add((side, row, col))
                    continue
                code = _color(cell)
                if code is None:
                    problems.append(
                        f"{where}: {_pos((side, row, col))} has {cell!r} - "
                        "write colours as 0x00-0x0F (0 = no hole, "
                        "- = not decided)")
                    continue
                colors[(side, row, col)] = code
        if problems:
            raise LookError(problems)
        return cls(name=name, colors=colors, shifts=shifts, item=item,
                   pattern=pattern, undecided=undecided)

    def shift(self, side: str, row: int) -> float:
        return self.shifts.get((side, row), default_shift(row))


def default_shift(row: int) -> float:
    """Half-scale offset of a row when no grid says: odd rows sit half a
    scale to the right, as in every grid delivered so far."""
    return 0.5 if row % 2 else 0.0


def _color(cell: str) -> "int | None":
    if not re.fullmatch(r"0[xX][0-9a-fA-F]{1,2}", cell):
        return None
    code = int(cell, 16)
    return code if code < COLOR_COUNT else None


def _pos(position) -> str:
    side, row, col = position
    return f"{side} row {row} col {col}"


def check(look_map: LookMap, design: Design, partial: bool = False
          ) -> "list[str]":
    """Problems that only show with both files side by side."""
    problems: "list[str]" = []
    if (look_map.item and design.item
            and look_map.item.lower() != design.item.lower()):
        problems.append(f"{design.name} is for {design.item} but "
                        f"{look_map.name} is {look_map.item}")
    scales = look_map.by_position
    for position in sorted(set(design.colors) | design.undecided):
        if position not in scales:
            problems.append(f"{design.name}: {_pos(position)} is a hole in "
                            f"the grid but {look_map.name} has no scale there")
    if not partial:
        for position in sorted(scales):
            if position in design.colors:
                continue
            scale = scales[position]
            wiring = f"(board {scale.board_no} socket {scale.socket})"
            if position in design.undecided:
                problems.append(f"{design.name}: colour not decided (-) for "
                                f"{_pos(position)} {wiring}")
            else:
                problems.append(
                    f"{design.name}: no colour for {_pos(position)} "
                    f"{wiring} - 0 means no hole, white is 0x00")
    return problems


def unit_board_ids(maps: "list[LookMap]") -> "dict[int, int]":
    """board_no -> bus address across every item one unit carries."""
    owner: "dict[int, str]" = {}
    problems = []
    for look_map in maps:
        for board_no in look_map.board_nos:
            if board_no in owner:
                problems.append(f"board {board_no} is in both "
                                f"{owner[board_no]} and {look_map.name}")
            owner.setdefault(board_no, look_map.name)
    if len(owner) > MAX_BOARDS:
        problems.append(f"{len(owner)} boards on one unit, but a bus holds "
                        f"at most {MAX_BOARDS}")
    if problems:
        raise LookError(problems)
    return {no: rank for rank, no in enumerate(sorted(owner), start=1)}


def compile_unit(pairs: "list[tuple[LookMap, Design]]", partial: bool = False
                 ) -> "dict[int, bytes]":
    """Every board of a unit that carries several items (Look 20)."""
    ids = unit_board_ids([look_map for look_map, _ in pairs])
    problems: "list[str]" = []
    for look_map, design in pairs:
        problems += check(look_map, design, partial=partial)
    if problems:
        raise LookError(problems)
    arrays: "dict[int, bytes]" = {}
    for look_map, design in pairs:
        arrays.update(compile_design(look_map, design, partial=partial,
                                     ids=ids))
    return dict(sorted(arrays.items()))


def compile_design(look_map: LookMap, design: Design, partial: bool = False,
                   ids: "dict[int, int] | None" = None) -> "dict[int, bytes]":
    """bus address -> 64-byte array, for every board of the item.

    `partial` lets a cue colour only some scales; the rest are sent as
    0xFF and keep what they show. Without it a scale with no colour is
    an error (see the module docstring on 0 against 0x00). `ids` is the
    unit-wide addressing when the item shares its unit with another.
    """
    problems = check(look_map, design, partial=partial)
    if problems:
        raise LookError(problems)
    ids = {no: (ids or look_map.board_ids)[no] for no in look_map.board_nos}
    arrays = {address: bytearray([NO_REFRESH] * ARRAY_LEN)
              for address in ids.values()}
    for array in arrays.values():
        array[0] = array[-1] = MARKER
    for scale in look_map.scales:
        code = design.colors.get(scale.position)
        if code is not None:
            arrays[ids[scale.board_no]][scale.socket] = code
    return {address: bytes(array) for address, array in arrays.items()}


def summary(look_map: LookMap, design: "Design | None" = None) -> "list[str]":
    lines = [f"{look_map.name}: {len(look_map.scales)} scales on "
             f"{len(look_map.board_nos)} boards "
             f"(board_no {look_map.board_nos[0]}-{look_map.board_nos[-1]} "
             f"-> DIP 1-{len(look_map.board_nos)})"]
    for side in look_map.sides:
        count = sum(1 for s in look_map.scales if s.side == side)
        lines.append(f"  {side}: {count} scales")
    if design is not None:
        used: "dict[int, int]" = {}
        for code in design.colors.values():
            used[code] = used.get(code, 0) + 1
        lines.append(f"{design.name}: {len(design.colors)} coloured scales")
        for code in sorted(used):
            lines.append(f"  0x{code:02X} {PALETTE[code][0]:<12} {used[code]}")
    return lines
