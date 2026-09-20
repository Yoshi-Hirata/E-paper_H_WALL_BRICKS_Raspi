"""conductor/look.py: a look's two CSVs -> one 64-byte array per board.

The fixtures are tiny hand-written garments in the delivered format
(Look22, 2026-09-21): a map of side,row,col,board_no,socket,label and a
colour grid of side,row,shift,1,2,3...
"""

from __future__ import annotations

import io
import json
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


def test_palette_is_the_fw_260917_chart():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))
    from epaper.pattern import COLOR_LABELS_16, COLOR_RGB_16

    assert PALETTE == list(zip(COLOR_LABELS_16, COLOR_RGB_16))
    assert PALETTE[0x05] == ("Green", (0, 200, 0))
    assert PALETTE[0x06] == ("Turquoise", (64, 224, 208))
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
    assert (255, 0, 0) in {rgb for _, rgb in designed.getcolors(1 << 24)}
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
