"""conductor/look.py: a look's two CSVs -> one 64-byte array per board.

The fixtures are tiny hand-written garments in the delivered format
(Look22, 2026-09-21): a map of side,row,col,board_no,socket,label and a
colour grid of side,row,shift,1,2,3...
"""

from __future__ import annotations

import io
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conductor import look as look_mod
from conductor.__main__ import main
from conductor.look import (MARKER, NO_REFRESH, PALETTE, Design, LookError,
                            LookMap, check, compile_design, compile_unit,
                            default_shift, unit_board_ids)

MAP = """side,row,col,board_no,socket,label
front,1,1,17,1,017-01
front,1,2,17,60,017-60
front,0,1,20,5,020-05
back,0,2,18,12,018-12
"""

GRID = """side,row,shift,1,2,3
front,1,0.5,0x03,0x00,0
front,0,0,0x0F,0,0
back,0,0,0,0x05,0
"""


def parse(map_text=MAP, grid_text=GRID):
    return (LookMap.parse(io.StringIO(map_text), name="m.csv"),
            Design.parse(io.StringIO(grid_text), name="g.csv"))


def problems_of(call):
    with pytest.raises(LookError) as caught:
        call()
    return caught.value.problems


# ---- the map ----

def test_boards_are_addressed_by_rank_not_by_number():
    look_map, _ = parse()
    assert look_map.board_nos == [17, 18, 20]
    # 20 is the third board, so it is address 3 - the gap at 19 is closed.
    assert look_map.board_ids == {17: 1, 18: 2, 20: 3}
    assert look_map.boards == [1, 2, 3]
    assert look_map.sides == ["front", "back"]


def test_dip_sheet_is_binary_switch_n_is_bit_n_minus_1():
    look_map, _ = parse()
    sheet = {line["board_no"]: line for line in look_map.dip_sheet()}
    assert sheet[17]["switches_on"] == "1"
    assert sheet[18]["switches_on"] == "2"
    assert sheet[20]["switches_on"] == "1 2"          # address 3
    assert sheet[17]["scales"] == 2
    many = "side,row,col,board_no,socket\n" + "".join(
        f"front,0,{n},{n},1\n" for n in range(1, 21))
    twenty = LookMap.parse(io.StringIO(many)).dip_sheet()[-1]
    assert twenty["dip_id"] == 20 and twenty["switches_on"] == "3 5"


def test_map_lists_every_problem_at_once():
    bad = MAP + ("front,1,1,18,2,018-02\n"      # position taken
                 "back,5,5,17,1,017-01\n"       # socket taken
                 "back,6,6,17,61,017-61\n"      # no such socket
                 "back,x,1,17,2,\n")            # not a number
    problems = problems_of(lambda: LookMap.parse(io.StringIO(bad), name="m"))
    text = "\n".join(problems)
    assert len(problems) == 4
    assert "already holds a scale (line 2)" in text
    assert "board 17 socket 1 is already used (line 2)" in text
    assert "socket 61 is not 1-60" in text
    assert "whole numbers" in text


def test_map_needs_its_columns():
    problems = problems_of(
        lambda: LookMap.parse(io.StringIO("side,row,col\nfront,0,1\n")))
    assert "board_no" in problems[0] and "socket" in problems[0]


def test_label_mismatch_is_a_warning_not_an_error():
    look_map = LookMap.parse(io.StringIO(
        "side,row,col,board_no,socket,label\nfront,0,1,17,1,017-02\n"))
    assert len(look_map.warnings) == 1 and "017-01" in look_map.warnings[0]


def test_more_boards_than_a_bus_holds_is_refused():
    rows = "".join(f"front,0,{n},{n},1,\n" for n in range(1, 62))
    problems = problems_of(lambda: LookMap.parse(io.StringIO(
        "side,row,col,board_no,socket,label\n" + rows)))
    assert "61 boards" in problems[0]


# ---- the map's own shift column (the site's per-row rule) ----

MAP_SHIFT = """side,row,col,board_no,socket,label,shift
front,1,1,17,1,017-01,0
front,1,2,17,60,017-60,0
front,0,1,20,5,020-05,0
back,0,2,18,12,018-12,0.5
"""


def test_map_without_a_shift_column_defaults_row_by_row():
    look_map, _ = parse()
    assert look_map.shifts == {}
    assert look_map.shift("front", 1) == default_shift(1) == 0.5
    assert look_map.shift("front", 0) == default_shift(0) == 0.0


def test_map_shift_column_is_read_and_wins_over_the_default():
    look_map = LookMap.parse(io.StringIO(MAP_SHIFT))
    # front row 1 would default to 0.5 (odd) - the map says 0 instead.
    assert look_map.shift("front", 1) == 0.0
    # back row 0 would default to 0.0 (even) - the map says 0.5 instead.
    assert look_map.shift("back", 0) == 0.5
    assert look_map.shifts == {("front", 1): 0.0, ("front", 0): 0.0,
                               ("back", 0): 0.5}
    # A row the map never mentions still falls back to the default.
    assert look_map.shift("front", 9) == default_shift(9)


def test_map_shift_must_agree_across_every_line_of_the_same_row():
    bad = MAP_SHIFT.replace("front,1,2,17,60,017-60,0",
                            "front,1,2,17,60,017-60,0.5")
    problems = problems_of(lambda: LookMap.parse(io.StringIO(bad), name="m"))
    assert any("does not match" in p for p in problems)


def test_map_shift_must_be_a_number():
    bad = MAP_SHIFT.replace("020-05,0\n", "020-05,x\n")
    problems = problems_of(lambda: LookMap.parse(io.StringIO(bad), name="m"))
    assert any("0 or 0.5" in p for p in problems)


def test_map_shift_must_be_exactly_0_or_0_5():
    # A number that parses fine but isn't one of the site's own two values
    # (a full scale or a half-scale stagger) is still a typo, not a third
    # kind of offset (fix round finding 10).
    bad = MAP_SHIFT.replace("020-05,0\n", "020-05,0.3\n")
    problems = problems_of(lambda: LookMap.parse(io.StringIO(bad), name="m"))
    assert any("0 or 0.5" in p and "0.3" in p for p in problems)


def test_map_shift_blank_on_every_line_of_a_row_warns_and_falls_back():
    blank = MAP_SHIFT.replace("020-05,0\n", "020-05,\n")
    look_map = LookMap.parse(io.StringIO(blank), name="m.csv")
    # front row 0 (020-05) is the only row left blank - it still falls back
    # to the odd/even rule instead of erroring.
    assert look_map.shift("front", 0) == default_shift(0)
    assert any("shift is blank" in w and "front row 0" in w
               for w in look_map.warnings)
    # The rows that DO carry a shift are unaffected.
    assert look_map.shift("front", 1) == 0.0
    assert look_map.shift("back", 0) == 0.5


# ---- the grid ----

def test_grid_reads_colours_and_shifts_and_skips_empty_cells():
    _, design = parse()
    assert design.colors == {("front", 1, 1): 0x03, ("front", 1, 2): 0x00,
                             ("front", 0, 1): 0x0F, ("back", 0, 2): 0x05}
    assert design.shift("front", 1) == 0.5
    assert design.shift("front", 0) == 0.0
    assert design.shift("back", 7) == default_shift(7) == 0.5


def test_blank_is_no_hole_and_dash_is_a_hole_without_a_colour():
    # The designers' README: 0 = no hole, - = colour not decided.
    design = Design.parse(io.StringIO("side,row,shift,1,2,3\nfront,0,0,,-,0x01\n"))
    assert design.colors == {("front", 0, 3): 0x01}
    assert design.undecided == {("front", 0, 2)}


def test_undecided_colour_blocks_a_full_cue_but_not_a_partial_one():
    grid = GRID.replace("0x03,0x00,0", "0x03,-,0")
    look_map, design = parse(grid_text=grid)
    problems = problems_of(lambda: compile_design(look_map, design))
    assert len(problems) == 1 and "not decided (-)" in problems[0]
    assert "board 17 socket 60" in problems[0]
    assert compile_design(look_map, design, partial=True)[1][60] == NO_REFRESH


def test_undecided_hole_without_a_scale_in_the_map_is_an_error():
    grid = GRID.replace("0x0F,0,0", "0x0F,0,-")
    look_map, design = parse(grid_text=grid)
    assert any("front row 0 col 3" in p for p in check(look_map, design, True))


@pytest.mark.parametrize("cell", ["4", "0x10", "red", "0x", "0xFF", "00"])
def test_anything_but_a_hex_colour_is_refused(cell):
    problems = problems_of(lambda: Design.parse(io.StringIO(
        f"side,row,shift,1\nfront,0,0,{cell}\n"), name="g"))
    assert "0x00-0x0F" in problems[0] and "g:2" in problems[0]


def test_grid_header_and_duplicate_rows():
    assert "side,row,shift" in problems_of(lambda: Design.parse(
        io.StringIO("row,side,shift,1\n")))[0]
    problems = problems_of(lambda: Design.parse(io.StringIO(
        "side,row,shift,1\nfront,0,0,0x01\nfront,0,0,0x02\n")))
    assert "appears twice" in problems[0]


# ---- the two together ----

def test_compile_puts_each_colour_at_its_socket():
    look_map, design = parse()
    arrays = compile_design(look_map, design)
    assert sorted(arrays) == [1, 2, 3]
    for array in arrays.values():
        assert len(array) == 64
        assert array[0] == array[63] == MARKER
    assert arrays[1][1] == 0x03 and arrays[1][60] == 0x00
    assert arrays[2][12] == 0x05            # board 18 -> address 2
    assert arrays[3][5] == 0x0F             # board 20 -> address 3
    # Sockets without a scale are left alone, not painted.
    assert all(b == NO_REFRESH for i, b in enumerate(arrays[1])
               if i not in (0, 1, 60, 63))
    assert arrays[1][61] == arrays[1][62] == NO_REFRESH


def test_zero_typed_for_white_is_caught_not_lost():
    grid = GRID.replace("0x03,0x00,0", "0x03,0,0")
    look_map, design = parse(grid_text=grid)
    problems = problems_of(lambda: compile_design(look_map, design))
    assert len(problems) == 1
    assert "front row 1 col 2" in problems[0]
    assert "board 17 socket 60" in problems[0]
    assert "white is 0x00" in problems[0]


def test_partial_cue_leaves_uncoloured_scales_as_they_are():
    grid = GRID.replace("0x03,0x00,0", "0x03,0,0")
    look_map, design = parse(grid_text=grid)
    arrays = compile_design(look_map, design, partial=True)
    assert arrays[1][1] == 0x03
    assert arrays[1][60] == NO_REFRESH


def test_check_warns_not_errors_when_the_design_disagrees_with_the_maps_shift():
    # GRID's own shift for front row 1 is 0.5 (its own grid CSV, unrelated
    # to the map); MAP_SHIFT's own column says 0 for that row, and 0.5 for
    # back row 0 where GRID says 0 - two rows disagree. Wiring view and the
    # sweep both use the map's value regardless, so this is a warning on
    # the map, not a problem with the design (fix round finding 12).
    look_map = LookMap.parse(io.StringIO(MAP_SHIFT), name="m.csv")
    design = Design.parse(io.StringIO(GRID), name="g.csv")
    assert check(look_map, design) == []
    assert any("differs from the map on 2 rows" in w for w in look_map.warnings)


def test_check_says_nothing_when_the_design_agrees_with_the_map():
    look_map, design = parse()          # MAP has no shift column at all
    assert check(look_map, design) == []
    assert look_map.warnings == []


def test_colour_where_the_map_has_no_scale_is_an_error_even_when_partial():
    grid = GRID.replace("0x0F,0,0", "0x0F,0,0x01")
    look_map, design = parse(grid_text=grid)
    for partial in (False, True):
        problems = check(look_map, design, partial=partial)
        assert any("front row 0 col 3" in p and "no scale there" in p
                   for p in problems)


# ---- one unit, several items (Look 20: top + skirt) ----

SKIRT_MAP = """side,row,col,board_no,socket,label
front,1,1,1,7,001-07
front,0,1,2,8,002-08
"""
SKIRT_GRID = """side,row,shift,1
front,1,0.5,0x0A
front,0,0,0x0B
"""


def test_unit_addresses_run_across_all_its_items():
    top, top_design = parse()
    skirt = LookMap.parse(io.StringIO(SKIRT_MAP), name="skirt.csv")
    skirt_design = Design.parse(io.StringIO(SKIRT_GRID), name="skirt_g.csv")
    ids = unit_board_ids([top, skirt])
    # Each garment alone would start at 1 and collide on the shared bus.
    assert ids == {1: 1, 2: 2, 17: 3, 18: 4, 20: 5}
    arrays = compile_unit([(top, top_design), (skirt, skirt_design)])
    assert sorted(arrays) == [1, 2, 3, 4, 5]
    assert arrays[1][7] == 0x0A and arrays[2][8] == 0x0B
    assert arrays[3][1] == 0x03 and arrays[5][5] == 0x0F
    # The same position key in both garments stays apart.
    assert arrays[3][7] == NO_REFRESH
    sheet = {l["board_no"]: l["dip_id"] for l in top.dip_sheet(ids)}
    assert sheet == {17: 3, 18: 4, 20: 5}


def test_one_board_cannot_belong_to_two_items():
    top, _ = parse()
    clash = LookMap.parse(
        io.StringIO(SKIRT_MAP.replace(",2,8,002", ",17,8,017")),
        name="skirt.csv")
    problems = problems_of(lambda: unit_board_ids([top, clash]))
    assert "board 17 is in both m.csv and skirt.csv" in problems[0]


def test_files_of_different_looks_do_not_mix(tmp_path):
    (tmp_path / "Look22_map.csv").write_text(MAP, encoding="utf-8")
    (tmp_path / "Look07_color_pattern03_grid.csv").write_text(
        GRID, encoding="utf-8")
    look_map = LookMap.from_csv(tmp_path / "Look22_map.csv")
    design = Design.from_csv(tmp_path / "Look07_color_pattern03_grid.csv")
    assert (look_map.item, design.item, design.pattern) == ("Look22", "Look07", 3)
    assert "Look07" in check(look_map, design)[0]


def test_item_names_need_not_be_a_look_number(tmp_path):
    # Look 20 is two garments with their own boards, and the bags are
    # not looks at all (AZ-27SS index, 2026-09-21).
    (tmp_path / "Look20-Skirt_map.csv").write_text(MAP, encoding="utf-8")
    (tmp_path / "look20-skirt_color_pattern02_grid.csv").write_text(
        GRID, encoding="utf-8")
    look_map = LookMap.from_csv(tmp_path / "Look20-Skirt_map.csv")
    design = Design.from_csv(tmp_path / "look20-skirt_color_pattern02_grid.csv")
    assert look_map.item == "Look20-Skirt" and design.pattern == 2
    assert check(look_map, design) == []


def test_excel_bom_is_tolerated(tmp_path):
    path = tmp_path / "Look22_map.csv"
    path.write_text(MAP, encoding="utf-8-sig")
    assert len(LookMap.from_csv(path).scales) == 4


def test_palette_names_the_fw_chart_and_shows_the_site_sample_colours():
    """Codes and names are the FW_260917 chart (shared with the boards);
    the RGB is the production site's chart 260921 - the colour of the
    real paper as seen, so the page looks like the garment will."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))
    from epaper.pattern import COLOR_LABELS_16

    assert [name for name, _ in PALETTE] == COLOR_LABELS_16
    assert PALETTE[0x05][0] == "Green" and PALETTE[0x06][0] == "Turquoise"
    site_260921 = {0x00: "#89ADC3", 0x01: "#B4AE40", 0x02: "#005CB6", 0x03: "#72473B",
                   0x04: "#1A3757", 0x05: "#438372", 0x06: "#76944C", 0x07: "#777A65",
                   0x08: "#707070", 0x09: "#2473B3", 0x0A: "#815242", 0x0B: "#86AE59",
                   0x0C: "#3C8374", 0x0D: "#7E553F", 0x0E: "#6C634B", 0x0F: "#387793"}
    for code, hex_ in site_260921.items():
        assert PALETTE[code][1] == tuple(int(hex_[i:i + 2], 16) for i in (1, 3, 5))
    assert len(PALETTE) == look_mod.COLOR_COUNT == 16


# ---- the command line and the preview ----

@pytest.fixture
def files(tmp_path):
    map_path = tmp_path / "Look22_map.csv"
    grid_path = tmp_path / "Look22_color_pattern01_grid.csv"
    map_path.write_text(MAP, encoding="utf-8")
    grid_path.write_text(GRID, encoding="utf-8")
    return map_path, grid_path


def test_cli_check_passes_and_fails(files, capsys):
    map_path, grid_path = files
    assert main(["check", str(map_path), str(grid_path)]) == 0
    assert "4 scales on 3 boards" in capsys.readouterr().out
    grid_path.write_text(GRID.replace("0x05", "0"), encoding="utf-8")
    assert main(["check", str(map_path), str(grid_path)]) == 1
    assert "ERROR" in capsys.readouterr().err
    assert main(["check", str(map_path), str(grid_path), "--partial"]) == 0


def test_cli_arrays_and_dip(files, tmp_path, capsys):
    map_path, grid_path = files
    out = tmp_path / "arrays.json"
    assert main(["arrays", str(map_path), str(grid_path), "-o", str(out)]) == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["dev_type"] == 0x03
    assert sorted(payload["boards"]) == ["1", "2", "3"]
    assert bytes.fromhex(payload["boards"]["3"])[5] == 0x0F
    capsys.readouterr()
    assert main(["dip", str(map_path)]) == 0
    assert "20,3,1 2,1" in capsys.readouterr().out


def test_preview_draws_both_views(files, tmp_path):
    from conductor.preview import render

    map_path, grid_path = files
    look_map = LookMap.from_csv(map_path)
    design = Design.from_csv(grid_path)
    designed = render(look_map, design)
    wiring = render(look_map)
    assert designed.size == wiring.size and designed.width > designed.height / 4
    # The red scale at front row 1 col 1 is somewhere in the design view.
    assert PALETTE[0x03][1] in {rgb for _, rgb in designed.getcolors(1 << 24)}   # red, as seen
    out = tmp_path / "p.png"
    assert main(["preview", str(map_path), str(grid_path), "-o", str(out)]) == 0
    assert out.stat().st_size > 0


# ---- nothing is overwritten silently ----

def test_a_column_named_twice_is_refused_in_the_map():
    doubled = MAP.replace("board_no,socket,label", "board_no,socket,socket")
    problems = problems_of(lambda: LookMap.parse(io.StringIO(doubled), name="m"))
    assert "socket" in problems[0] and "more than once" in problems[0]


def test_a_position_column_named_twice_is_refused_in_the_grid():
    problems = problems_of(lambda: Design.parse(io.StringIO(
        "side,row,shift,1,2,2\nfront,0,0,0x01,0x02,0x03\n"), name="g"))
    assert "2" in problems[0] and "more than once" in problems[0]


def test_side_names_match_whatever_their_case():
    look_map = LookMap.parse(io.StringIO(MAP.replace("front,", "Front,")))
    design = Design.parse(io.StringIO(GRID.replace("back,", "BACK,")))
    assert look_map.sides == ["front", "back"]
    assert check(look_map, design) == []


def test_a_design_is_named_after_what_the_designer_typed():
    # <item>_color_<name>_grid[...].csv - the wiring page's export; the
    # name is a pattern number or whatever the designer called it.
    assert Design.name_parts("Look22_color_pattern01_grid.csv") == ("Look22", 1, "P01")
    assert Design.name_parts("Look22_color_pattern 3.csv") == ("Look22", 3, "P03")
    assert Design.name_parts(
        "AZ271SD1305_color_ref_multicolor_redorange_s22_grid_A-1.csv"
    ) == ("AZ271SD1305", None, "ref_multicolor_redorange_s22")
    assert Design.name_parts(
        "AZ271SD1305_color_ref_multicolor_redorange_s22_grid-2_A-2.csv"
    ) == ("AZ271SD1305", None, "ref_multicolor_redorange_s22")
    assert Design.name_parts("notes.csv") == (None, None, "notes")


def test_the_wiring_sites_own_hw_name_is_a_design_too():
    # The site's "HW 用 CSV" button writes <型番>_<配色案名>_HW.csv, its
    # official name for what this module otherwise calls
    # <item>_color_<name>_grid.csv (2026-09-26).
    assert look_mod.kind("AZ271SD1301_1_HW.csv") == "grid"
    assert look_mod.kind("AZ271SD1301_map.csv") == "map"
    assert look_mod.kind("AZ271SD1301_color_pattern01_grid.csv") == "grid"
    assert look_mod.kind("AZ271SD1301_1_HW.txt") is None
    # No 配色案名 at all: nothing to call the design, so not a grid.
    assert look_mod.kind("AZ271SD1301_HW.csv") is None
    assert Design.name_parts("AZ271SD1301_1_HW.csv") == ("AZ271SD1301", None, "1")
    # A 配色案名 with underscores of its own: without a list of the
    # garments that exist, the item is what precedes the FIRST underscore.
    assert Design.name_parts("AZ271SD1301_summer_2_HW.csv") == \
        ("AZ271SD1301", None, "summer_2")
    # With one, the longest garment that fits wins - which is the only way
    # to tell AZ271SD1305_B's design "1" from AZ271SD1305's design "B_1".
    assert Design.name_parts("AZ271SD1305_B_1_HW.csv") == \
        ("AZ271SD1305", None, "B_1")
    assert Design.name_parts("AZ271SD1305_B_1_HW.csv",
                             ["AZ271SD1305", "AZ271SD1305_B"]) == \
        ("AZ271SD1305_B", None, "1")
    # patternNN still numbers the design, whichever name it arrives under.
    assert Design.name_parts("AZ271SD1301_pattern03_HW.csv") == \
        ("AZ271SD1301", 3, "P03")


def test_the_hw_grammar_does_not_swallow_ordinary_names():
    # Review of a6b610b. "_HW" is the site's own button, spelled in
    # capitals: a lower-case "_hw" at the end of somebody's file name is
    # an ordinary word, and used to make "my_notes_hw.csv" design "notes"
    # of a garment called "my".
    assert look_mod.kind("my_notes_hw.csv") is None
    assert Design.name_parts("my_notes_hw.csv") == (None, None, "my_notes_hw")
    assert look_mod.kind("my_notes_HW.csv") == "grid"      # ...but this is
    # A name claiming to be both files at once is neither.
    assert look_mod.kind("AZ271SD1301_map_HW.csv") is None
    assert look_mod.kind("AZ271SD1305_B_map_HW.csv") is None
    assert Design.name_parts("AZ271SD1301_map_HW.csv") == \
        (None, None, "AZ271SD1301_map_HW")
    # Outer whitespace is trimmed before anything is read, so it can never
    # end up inside the item.
    assert look_mod.kind("  AZ271SD1301_1_HW.csv  ") == "grid"
    assert Design.name_parts(" AZ271SD1301_1_HW.csv") == ("AZ271SD1301", None, "1")


def test_a_pattern_number_is_ascii_on_both_sides():
    # Python's \d matches a full-width digit and JavaScript's does not, so
    # "pattern１" used to be P01 here and the literal name in model.js -
    # the same file, two different designs (review of a6b610b).
    assert Design.name_parts("AZ271SD1301_pattern1_HW.csv") == \
        ("AZ271SD1301", 1, "P01")
    assert Design.name_parts("AZ271SD1301_pattern１_HW.csv") == \
        ("AZ271SD1301", None, "pattern１")


# ---- what a workspace file may be called ----

def test_japanese_punctuation_is_an_ordinary_part_of_a_name():
    # The 配線ナビ writes these, and each one is its own design: the
    # Conductor used to refuse them all and /api/files used to fold the
    # first three onto one "柄_A" (review of a6b610b).
    for name in ["Look22_柄・A_HW.csv", "Look22_（A）_HW.csv",
                 "Look22_柄＋A_HW.csv", "Look22_か゚_HW.csv",
                 "Look22_柄：A_HW.csv", "Look22_夏_2_HW.csv"]:
        assert look_mod.name_problem(name) is None, name
        assert look_mod.kind(name) == "grid", name
        assert look_mod.normalize_name(name) == name, name
    # Five different names, five different designs - not one file.
    designs = {Design.name_parts(n)[2] for n in
               ["Look22_柄・A_HW.csv", "Look22_柄 A_HW.csv",
                "Look22_柄＋A_HW.csv", "Look22_（A）_HW.csv",
                "Look22_か゚_HW.csv"]}
    assert len(designs) == 5


def test_the_ideographic_space_becomes_an_ordinary_one():
    assert look_mod.normalize_name("Look22_柄　A_HW.csv") == "Look22_柄 A_HW.csv"
    assert look_mod.name_problem("Look22_柄　A_HW.csv") is None


def test_a_decomposed_name_composes():
    assert look_mod.normalize_name("Look22_color_" + "ガ" + "_grid.csv") \
        == "Look22_color_ガ_grid.csv"


@pytest.mark.parametrize("name,problem", [
    ("", "a file name cannot be empty"),
    ("   ", "a file name cannot be empty"),
    ("bad\x00_map.csv", "a file name cannot contain a control character"),
    ("a/b_map.csv", 'a file name cannot contain "/" (a path separator)'),
    ("a\\b_map.csv", 'a file name cannot contain "\\" (a path separator)'),
    ("a／b_map.csv", 'a file name cannot contain "／" (a path separator)'),
    ("a＼b_map.csv", 'a file name cannot contain "＼" (a path separator)'),
    ("a:b_map.csv", 'a file name cannot contain ":" (Windows keeps it)'),
    ("a|b_map.csv", 'a file name cannot contain "|" (Windows keeps it)'),
    ('a"b_map.csv', 'a file name cannot contain """ (Windows keeps it)'),
    (".Look22_map.csv", "a file name cannot start or end with a dot"),
])
def test_what_no_workspace_file_name_may_hold(name, problem):
    assert look_mod.name_problem(name) == problem
    assert look_mod.kind(name) is None          # ...and so it is no file at all


def test_outer_whitespace_is_trimmed_not_refused():
    assert look_mod.name_problem("  Look22_map.csv  ") is None
    assert look_mod.normalize_name("  Look22_map.csv  ") == "Look22_map.csv"


# ---- a design drawn for another layout of the same garment ----

# The real map of AZ271SD1301 runs front rows 0-33, back rows 0-34, 30
# columns (conductor/web/starter/AZ271SD1301_map.csv, 1482 scales). This is
# that geometry in four lines: the corners are all geometry_problem() ever
# looks at. tests/fixtures/sim/AZ271SD1301_map.csv is the same truncation
# at full width, for the JS cross-check.
OLD_LAYOUT_MAP = """side,row,col,board_no,socket,label
front,0,1,1,1,001-01
front,0,30,1,2,001-02
front,33,10,1,3,001-03
back,0,3,2,1,002-01
back,0,28,2,2,002-02
back,34,10,2,3,002-03
"""


WHOLE_GARMENT = {"front": 33, "back": 34}      # OLD_LAYOUT_MAP's own top rows


def _old_layout_grid(top=18, width: int = 31) -> str:
    """The 2026-09-26 file: rows 0..`top` a side (an int for both sides, or
    a dict per side), `width` position columns, every cell 0x04 - the shape
    of a grid exported from a layout of this garment that is gone."""
    tops = top if isinstance(top, dict) else {"front": top, "back": top}
    cols = list(range(1, width + 1))
    lines = ["side,row,shift," + ",".join(str(c) for c in cols)]
    for side, side_top in tops.items():
        for row in range(side_top, -1, -1):
            lines.append(f"{side},{row},{0.5 if row % 2 else 0.0},"
                         + ",".join(["0x04"] * width))
    return "\n".join(lines) + "\n"


def _pair(map_text, grid_text, name="AZ271SD1301_1_HW.csv",
          map_name="AZ271SD1301_map.csv", map_item="AZ271SD1301"):
    item, pattern, _label = Design.name_parts(name)
    return (LookMap.parse(io.StringIO(map_text), name=map_name, item=map_item),
            Design.parse(io.StringIO(grid_text), name=name, item=item,
                         pattern=pattern))


def _geometry(map_text, grid_text, **kw):
    """geometry_problem() on its own - the cell-by-cell problems of a
    deliberately sparse fixture map would only get in the way here."""
    return look_mod.geometry_problem(*_pair(map_text, grid_text, **kw))


def test_a_grid_from_an_older_layout_says_so_first():
    problem = _geometry(OLD_LAYOUT_MAP, _old_layout_grid())
    assert problem == (
        "AZ271SD1301_1_HW.csv covers rows 0-18 (front) and 0-18 (back) but "
        "this garment's wiring has rows 0-33 (front) and 0-34 (back) with 30 "
        "columns - the design was made for another layout of AZ271SD1301; "
        "export it again from the current 配線ナビ "
        "(配色) page")
    # It LEADS check()'s list, both ways round: a partial cue is no excuse
    # (the rows are simply not there), and as a full cue it still comes
    # before every per-scale line rather than at the end of a thousand.
    look_map, design = _pair(OLD_LAYOUT_MAP, _old_layout_grid())
    for partial in (True, False):
        assert check(look_map, design, partial=partial)[0] == problem
    # None of the words the designers' page is not allowed to print.
    assert not re.search(r"\b(unit|radxa|bus|board|dip|socket)s?\b", problem,
                         re.IGNORECASE)


def test_a_grid_that_covers_the_whole_garment_is_never_flagged():
    # The same map, a grid that reaches both top rows: no geometry problem,
    # whatever its cells say - even when every one of them is undecided.
    whole = _old_layout_grid(top=WHOLE_GARMENT)
    assert _geometry(OLD_LAYOUT_MAP, whole) is None
    assert _geometry(OLD_LAYOUT_MAP, whole.replace("0x04", "-")) is None
    # Nor when a real partial design leaves most of its cells blank.
    assert _geometry(OLD_LAYOUT_MAP, whole.replace("0x04", "0")) is None
    # Nor a row or two short of the top: a layout is not two rows (34 and
    # 35 rows a side, so three is still under the tenth this needs).
    assert _geometry(OLD_LAYOUT_MAP, _old_layout_grid(top=33)) is None
    assert _geometry(OLD_LAYOUT_MAP, _old_layout_grid(top=32)) is None
    assert _geometry(OLD_LAYOUT_MAP, _old_layout_grid(top=31)) is None
    # Four short of a 34-row side is over the tenth, and is flagged.
    assert _geometry(OLD_LAYOUT_MAP, _old_layout_grid(top=29)) is not None
    # Nor a side the grid leaves out entirely - that IS a partial cue.
    front_only = "\n".join(line for line in whole.splitlines()
                           if not line.startswith("back,")) + "\n"
    assert _geometry(OLD_LAYOUT_MAP, front_only) is None


def test_the_grid_may_be_wider_than_the_garment_but_not_narrower():
    # AZ271SD1307's own committed grid is 31 columns against a 30-column
    # map: the site pads the right edge, and an extra column with a colour
    # in it is already caught position by position.
    assert _geometry(OLD_LAYOUT_MAP, _old_layout_grid(top=WHOLE_GARMENT, width=40)) is None
    narrow = _geometry(OLD_LAYOUT_MAP, _old_layout_grid(top=WHOLE_GARMENT, width=24))
    assert narrow is not None and "with only 24 columns" in narrow
    assert "with 30 columns" in narrow


def test_a_grid_with_rows_the_garment_does_not_have_is_flagged():
    taller = _geometry(OLD_LAYOUT_MAP, _old_layout_grid(top=40))
    assert taller is not None
    assert "covers rows 0-40 (front) and 0-40 (back)" in taller


def test_another_garments_grid_is_still_told_apart_from_an_old_layout():
    # Against a map of a DIFFERENT garment every row and column differs,
    # and "made for another layout of X" would be the wrong story about a
    # file that was never meant for X - the item mismatch is the whole
    # problem, and it stays the only one of the two that is reported.
    look_map = LookMap.parse(io.StringIO(OLD_LAYOUT_MAP), name="m.csv",
                             item="AZ271SD1306")
    design = Design.parse(io.StringIO(_old_layout_grid()),
                          name="AZ271SD1301_1_HW.csv", item="AZ271SD1301")
    problems = check(look_map, design, partial=True)
    assert "is for AZ271SD1301 but m.csv is AZ271SD1306" in problems[0]
    assert not any("another layout" in p for p in problems)


def test_the_starter_garments_own_designs_are_never_called_an_old_layout():
    # The ten committed maps and their sample grids (conductor/web/starter):
    # a false alarm here would make the simulator refuse the show's own
    # data, which is the one thing this check must never do.
    starter = Path(__file__).resolve().parents[1] / "conductor" / "web" / "starter"
    maps = {m.item: m for m in
            (LookMap.from_csv(p) for p in sorted(starter.glob("*_map.csv")))}
    checked = 0
    for path in sorted(starter.glob("*.csv")):
        if look_mod.kind(path.name) != "grid":
            continue
        design = Design.from_csv(path, items=list(maps))
        look_map = maps[design.item]
        assert look_mod.geometry_problem(look_map, design) is None, path.name
        checked += 1
    assert checked >= 20


# ---- tools/make_sample_grids.py: sample grids follow the map's own shift ----

def test_sample_grid_rows_carry_the_maps_own_shift():
    import importlib.util

    tools_path = Path(__file__).resolve().parents[1] / "tools" / "make_sample_grids.py"
    spec = importlib.util.spec_from_file_location("make_sample_grids", tools_path)
    make_sample_grids = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(make_sample_grids)

    look_map = LookMap.parse(io.StringIO(MAP_SHIFT))
    text = make_sample_grids.grid_csv(look_map, lambda s: 0x01)
    rows = {line.split(",")[0] + "|" + line.split(",")[1]: line.split(",")[2]
            for line in text.splitlines()[1:]}
    # front row 1 is 0 in the map (would default to 0.5, odd row).
    assert rows["front|1"] == str(look_map.shift("front", 1)) == "0.0"
    # back row 0 is 0.5 in the map (would default to 0.0, even row).
    assert rows["back|0"] == str(look_map.shift("back", 0)) == "0.5"


def test_a_map_row_with_a_stray_trailing_comma_still_loads(tmp_path):
    """csv.DictReader parks the extra cell in a list under restkey; that
    must not become an AttributeError (a 500 on /api/state) - the row is
    read from its named columns and the extra cell is ignored."""
    text = MAP.replace("front,14,10,17,1,017-01", "front,14,10,17,1,017-01,", 1)
    path = tmp_path / "Look22_map.csv"
    path.write_text(text, encoding="utf-8")
    look_map = LookMap.from_csv(path)
    assert any(s.board_no == 17 and s.socket == 1 for s in look_map.scales)

