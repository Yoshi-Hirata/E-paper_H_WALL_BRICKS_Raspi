"""conductor/server.py: the workspace behind the web UI, and its HTTP API.

The server is started on an ephemeral localhost port with a temp
workspace; the CSVs are the small fixtures from tests/test_look.py.
"""

from __future__ import annotations

import http.client
import io
import json
import struct
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conductor.server import MAX_MUSIC, Workspace, make_server
from tests.test_look import GRID, MAP, MAP_SHIFT, SKIRT_GRID, SKIRT_MAP


@pytest.fixture
def workspace(tmp_path):
    ws = Workspace(tmp_path / "ws")
    ws.save("Look22_map.csv", MAP)
    ws.save("Look22_color_pattern01_grid.csv", GRID)
    return ws


def item(state, name):
    return next(i for i in state["items"] if i["item"] == name)


def test_state_joins_a_map_with_its_designs(workspace):
    state = workspace.state()
    look = item(state, "Look22")
    assert len(look["map"]["scales"]) == 4
    assert look["map"]["sides"] == ["front", "back"]
    # MAP has no shift column, so every row agrees with default_shift():
    # nothing to send, and the page's shiftOf() falls back on its own.
    assert look["map"]["shifts"] == {}
    assert [d["pattern"] for d in look["designs"]] == [1]
    assert look["designs"][0]["colors"]["front|1|1"] == 0x03
    assert look["designs"][0]["problems"] == [] and look["problems"] == []
    assert [b["dip_id"] for b in look["boards"]] == [1, 2, 3]
    assert state["palette"][5]["name"] == "Green"
    assert len(state["units"]) == 10


def test_state_carries_the_maps_own_shift_where_it_differs_from_default(tmp_path):
    ws = Workspace(tmp_path / "ws")
    ws.save("Look19_map.csv", MAP_SHIFT)
    look = item(ws.state(), "Look19")
    # front row 1 (0 in the map, would default to 0.5) and back row 0
    # (0.5 in the map, would default to 0.0) are the two that differ;
    # front row 0 (0 in the map, matches the 0.0 default) is left out.
    assert look["map"]["shifts"] == {"front|1": 0.0, "back|0": 0.5}


def test_problems_reach_the_page_instead_of_failing_the_request(workspace):
    workspace.save("Look22_color_pattern02_grid.csv",
                   GRID.replace("0x03,0x00,0", "0x03,-,0"))
    workspace.save("Look23_map.csv", "side,row,col\nfront,0,1\n")
    state = workspace.state()
    second = item(state, "Look22")["designs"][1]
    assert second["undecided"] == ["front|1|2"]
    # Not decided (-) fails a full cue but is fine as a partial one: the
    # page shows such a design as "partial", not as broken.
    assert any("not decided" in p for p in second["problems"])
    assert second["partial_problems"] == []
    assert any("board_no" in p for p in item(state, "Look23")["problems"])


def test_items_on_one_unit_share_its_addresses(workspace):
    workspace.save("Look20-Skirt_map.csv", SKIRT_MAP)
    workspace.save("Look20-Skirt_color_pattern01_grid.csv", SKIRT_GRID)
    alone = workspace.state()
    assert [b["dip_id"] for b in item(alone, "Look22")["boards"]] == [1, 2, 3]
    assert [b["dip_id"] for b in item(alone, "Look20-Skirt")["boards"]] == [1, 2]
    workspace.assign("Look22", "radxa-02")
    workspace.assign("Look20-Skirt", "radxa-02")
    shared = workspace.state()
    # Boards 1, 2 (skirt) then 17, 18, 20: one bus, no address twice.
    assert [b["dip_id"] for b in item(shared, "Look20-Skirt")["boards"]] == [1, 2]
    assert [b["dip_id"] for b in item(shared, "Look22")["boards"]] == [3, 4, 5]
    workspace.assign("Look22", None)
    assert item(workspace.state(), "Look22")["unit"] is None


def test_a_board_on_two_items_of_one_unit_is_flagged(workspace):
    workspace.save("Bag01_map.csv", SKIRT_MAP.replace(",2,8,002", ",17,8,017"))
    workspace.assign("Look22", "radxa-05")
    workspace.assign("Bag01", "radxa-05")
    state = workspace.state()
    assert any("board 17 is in both" in p
               for p in item(state, "Look22")["problems"])


def test_grid_without_its_map_waits_as_an_orphan(tmp_path):
    ws = Workspace(tmp_path)
    ws.save("Look24_color_pattern01_grid.csv", GRID)
    state = ws.state()
    assert state["items"] == []
    assert state["orphans"][0]["name"] == "Look24_color_pattern01_grid.csv"


def test_only_the_two_csv_kinds_are_accepted_and_names_are_tamed(tmp_path):
    ws = Workspace(tmp_path)
    with pytest.raises(ValueError):
        ws.save("cables.csv", "x")
    with pytest.raises(ValueError):
        ws.save("Look22_map.txt", "x")
    with pytest.raises(ValueError):
        ws.assign("Look22", "radxa-99")
    saved = ws.save("../../evil/Look22_map.csv", MAP)
    assert saved == "Look22_map.csv"
    assert (tmp_path / "files" / "Look22_map.csv").is_file()
    ws.delete("../files/Look22_map.csv")
    assert not (tmp_path / "files" / "Look22_map.csv").exists()


def test_http_api_round_trip(tmp_path):
    server = make_server(tmp_path, port=0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def call(path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}", data=data,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read()

    try:
        assert server.server_address[0] == "127.0.0.1"
        status, page = call("/")
        assert status == 200 and b"CONDUCTOR" in page
        _, raw = call("/api/files", {"files": [
            {"name": "Look22_map.csv", "text": MAP},
            {"name": "Look22_color_pattern01_grid.csv", "text": GRID},
            {"name": "notes.csv", "text": "x"}]})
        result = json.loads(raw)
        assert len(result["saved"]) == 2 and len(result["refused"]) == 1
        call("/api/assign", {"item": "Look22", "unit": "radxa-03"})
        state = json.loads(call("/api/state")[1])
        assert state["items"][0]["unit"] == "radxa-03"
        call("/api/delete", {"name": "Look22_color_pattern01_grid.csv"})
        assert json.loads(call("/api/state")[1])["items"][0]["designs"] == []
    finally:
        server.shutdown()
        server.server_close()


# ---- the timeline in show.json ----

def test_timeline_is_stored_cleaned_and_returned_with_its_times(workspace):
    workspace.save("Look22_color_pattern02_grid.csv", GRID)
    grid = "Look22_color_pattern0{}_grid.csv".format
    workspace.set_timeline("10:00", [
        {"id": "b", "item": "Look22", "at": "2:00", "design": grid(2)},
        {"id": "a", "item": "Look22", "at": 0, "design": grid(1),
         "align": "sideways"},
    ])
    show = workspace.state()["show"]
    assert show["duration"] == 600 and show["refresh_s"] == 7
    assert [c["id"] for c in show["cues"]] == ["a", "b"]        # by time
    preset, second = show["cues"]
    assert "align" not in preset and "align" not in second     # cleaned away
    assert (preset["sent"], preset["complete"]) == (-7, 0)
    assert (second["sent"], second["complete"]) == (120, 127)  # "at" IS Start
    assert preset["refresh"] == second["refresh"] == 7
    assert preset["refresh_source"] == second["refresh_source"] == "show"
    assert preset["end"] == 120 and preset["end_source"] == "next"
    assert second["end"] == 600 and second["end_source"] == "show"
    assert preset["problems"] == second["problems"] == []
    assert show["warnings"] == []
    # 3 boards: 7 s refresh + 3 x 0.22 s + 3 s margin.
    assert round(show["min_interval"]["(Look22)"], 2) == 10.66


def test_refresh_time_is_a_setting_of_the_show(workspace):
    cues = [{"id": "a", "item": "Look22", "at": "2:00",
             "design": "Look22_color_pattern01_grid.csv"}]
    workspace.set_timeline(600, cues, refresh=16)       # a unit on old firmware
    show = workspace.state()["show"]
    assert show["refresh_s"] == 16
    assert (show["cues"][0]["sent"], show["cues"][0]["complete"]) == (120, 136)
    assert round(show["min_interval"]["(Look22)"], 2) == 19.66
    workspace.set_timeline(600, cues)                   # not given: kept
    assert workspace.state()["show"]["refresh_s"] == 16
    assert workspace.undo() and workspace.state()["show"]["refresh_s"] == 7
    for bad in (0, 61, "fast"):
        with pytest.raises(ValueError):
            workspace.set_timeline(600, cues, refresh=bad)


def test_timeline_survives_a_unit_assignment_and_back(workspace):
    workspace.set_timeline(300, [{"id": "a", "item": "Look22", "at": 0,
                                  "design": "Look22_color_pattern01_grid.csv"}])
    workspace.assign("Look22", "radxa-04")
    state = workspace.state()
    assert state["items"][0]["unit"] == "radxa-04"
    assert len(state["show"]["cues"]) == 1
    assert "radxa-04" in state["show"]["min_interval"]


def test_timeline_rejects_nonsense_durations(workspace):
    for bad in (0, "abc", 7 * 3600):
        with pytest.raises(ValueError):
            workspace.set_timeline(bad, [])


# ---- undo / redo of the show ----

def _cue(id_, at):
    return {"id": id_, "item": "Look22", "at": at,
            "design": "Look22_color_pattern01_grid.csv"}


def _ids(workspace):
    return [c["id"] for c in workspace.state()["show"]["cues"]]


def test_undo_and_redo_walk_the_edits_of_the_show(workspace):
    assert workspace.state()["history"] == {"undo": 0, "redo": 0}
    assert workspace.undo() is False                 # nothing yet
    workspace.set_timeline(600, [_cue("a", 0)])
    workspace.set_timeline(600, [_cue("a", 0), _cue("b", 120)])
    workspace.assign("Look22", "radxa-03")
    assert workspace.state()["history"] == {"undo": 3, "redo": 0}

    assert workspace.undo()                          # the assignment
    assert workspace.state()["items"][0]["unit"] is None
    assert _ids(workspace) == ["a", "b"]
    assert workspace.undo()                          # cue b
    assert _ids(workspace) == ["a"]
    assert workspace.state()["history"] == {"undo": 1, "redo": 2}

    assert workspace.redo()
    assert _ids(workspace) == ["a", "b"]
    assert workspace.redo()
    assert workspace.state()["items"][0]["unit"] == "radxa-03"
    assert workspace.redo() is False


def test_a_new_edit_ends_the_redo_line(workspace):
    workspace.set_timeline(600, [_cue("a", 0)])
    workspace.set_timeline(600, [_cue("a", 0), _cue("b", 120)])
    workspace.undo()
    workspace.set_timeline(600, [_cue("a", 0), _cue("c", 300)])
    assert workspace.state()["history"] == {"undo": 2, "redo": 0}
    assert workspace.redo() is False
    assert _ids(workspace) == ["a", "c"]


def test_saving_the_same_show_again_is_not_a_step(workspace):
    workspace.set_timeline(600, [_cue("a", 0)])
    workspace.set_timeline(600, [_cue("a", 0)])
    workspace.assign("Look22", None)                 # already unassigned
    assert workspace.state()["history"]["undo"] == 1


def test_history_survives_a_restart_and_is_bounded(workspace, monkeypatch):
    from conductor import server

    monkeypatch.setattr(server, "HISTORY_DEPTH", 5)
    for n in range(8):
        workspace.set_timeline(600, [_cue("a", n * 30)])
    again = Workspace(workspace.root)                # a new server process
    assert again.state()["history"]["undo"] == 5
    assert again.undo()
    assert again.state()["show"]["cues"][0]["at"] == 180


def test_undo_over_http(tmp_path):
    server = make_server(tmp_path, port=0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def post(path, body):
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read())

    try:
        assert post("/api/undo", {}) == {"ok": False}
        post("/api/show", {"duration": "5:00", "cues": [_cue("a", 0)]})
        assert post("/api/undo", {}) == {"ok": True}
        assert post("/api/redo", {}) == {"ok": True}
    finally:
        server.shutdown()
        server.server_close()


# ---- the launcher ----

def test_a_second_conductor_on_the_same_port_steps_aside(tmp_path, capsys):
    from conductor.server import already_serving, serve

    server = make_server(tmp_path, port=0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert already_serving(port)
        # Returns at once instead of serving a second copy on the port.
        assert serve(tmp_path / "other", port) == 0
        assert "already running" in capsys.readouterr().out
        with pytest.raises(OSError):
            make_server(tmp_path, port=port)        # the port is exclusive
    finally:
        server.shutdown()
        server.server_close()
    assert not already_serving(port)


# ---- robustness found in review ----

def test_show_json_is_replaced_whole_never_half_written(workspace):
    workspace.set_timeline(600, [_cue("a", 0)])
    assert not list(workspace.root.glob("*.tmp"))
    assert json.loads((workspace.root / "show.json").read_text(encoding="utf-8"))["cues"]


def test_a_broken_map_is_named_when_the_show_is_compiled(workspace):
    workspace.assign("Look22", "radxa-01")
    workspace.set_timeline(600, [_cue("a", 0)])
    assert workspace.compile_show()[1] == []
    workspace.save("Look23_map.csv", "side,row,col\nfront,0,1\n")
    shows, problems = workspace.compile_show()
    assert shows == {} and "Look23_map.csv" in problems[0]


def test_start_needs_an_upload_and_does_not_restart_by_accident(tmp_path):
    from conductor.fleet import Fleet

    fleet = Fleet({})
    server = make_server(tmp_path, port=0, fleet=fleet)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def post(path, body):
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read())

    try:
        assert "Upload first" in post("/api/fleet/start", {})["note"]
        assert fleet.run is None
        fleet.shows = {"radxa-01": {"id": "x", "cues": [], "duration": 60}}
        fleet.links = {}
        post("/api/fleet/start", {"lead_s": 1})
        t0 = fleet.run["t0"]
        again = post("/api/fleet/start", {"lead_s": 1})     # the double click
        assert "already running" in again["note"] and fleet.run["t0"] == t0
        post("/api/fleet/start", {"lead_s": 1, "force": True})
        assert fleet.run["t0"] > t0
        assert "not on hold" in post("/api/fleet/resume", {})["note"]
        post("/api/fleet/stop", {})
        assert "not running" in post("/api/fleet/hold", {})["note"]
        assert "No cue ahead" in post("/api/fleet/next", {"lead_s": 1})["note"]
    finally:
        server.shutdown()
        server.server_close()


# ---- LOOK number and model number ----

def test_an_item_is_its_look_number_until_it_is_given_a_label(workspace):
    look = item(workspace.state(), "Look22")
    assert (look["look"], look["model"]) == ("22", "")


def test_look_and_model_number_are_editable_and_undoable(workspace):
    workspace.set_label("Look22", " 22 ", "AZ271SD1305")
    look = item(workspace.state(), "Look22")
    assert (look["look"], look["model"]) == ("22", "AZ271SD1305")
    workspace.set_label("Look22", "25", "AZ271SD1305")
    workspace.set_label("Look22", "25", "AZ271SD1305")      # no change, no step
    assert workspace.state()["history"]["undo"] == 2
    assert item(workspace.state(), "Look22")["look"] == "25"
    # The files keep their names; the design still belongs to its map.
    assert [d["pattern"] for d in item(workspace.state(), "Look22")["designs"]] == [1]
    workspace.undo()
    assert item(workspace.state(), "Look22")["look"] == "22"
    # An emptied label stays empty - it does not fall back to the file name.
    workspace.set_label("Look22", "", "")
    look = item(workspace.state(), "Look22")
    assert (look["look"], look["model"]) == ("", "")


def test_a_label_survives_the_timeline_and_is_bounded(workspace):
    workspace.set_label("Look22", "22", "AZ271SD1305")
    workspace.set_timeline(600, [_cue("a", 0)])
    workspace.assign("Look22", "radxa-04")
    assert item(workspace.state(), "Look22")["model"] == "AZ271SD1305"
    with pytest.raises(ValueError):
        workspace.set_label("Look22", "22", "x" * 41)
    with pytest.raises(ValueError):
        workspace.set_label("", "22", "")


def test_arranging_every_unit_at_once_is_one_step(workspace):
    workspace.save("Look20-Skirt_map.csv", SKIRT_MAP)
    workspace.assign("Look22", "radxa-01")
    steps = workspace.state()["history"]["undo"]
    workspace.arrange({"Look22": "radxa-04", "Look20-Skirt": "radxa-04",
                       "Gone": ""})
    state = workspace.state()
    assert item(state, "Look22")["unit"] == "radxa-04"
    assert item(state, "Look20-Skirt")["unit"] == "radxa-04"
    assert state["history"]["undo"] == steps + 1
    workspace.undo()
    state = workspace.state()
    assert item(state, "Look22")["unit"] == "radxa-01"
    assert item(state, "Look20-Skirt")["unit"] is None
    with pytest.raises(ValueError):
        workspace.arrange({"Look22": "radxa-11"})
    with pytest.raises(ValueError):
        workspace.arrange(["Look22"])


# ---- two garments of one shape: items of their own ----

def test_a_duplicate_is_an_item_of_its_own_with_its_own_files(workspace):
    workspace.set_label("Look22", "23", "AZ271SD1305")
    twin = workspace.duplicate("Look22")
    assert twin == "Look22-2"
    assert (workspace.files / "Look22-2_map.csv").read_bytes() ==         (workspace.files / "Look22_map.csv").read_bytes()
    state = workspace.state()
    first, second = item(state, "Look22"), item(state, twin)
    assert second["map"]["scales"] == first["map"]["scales"]
    assert second["model"] == "AZ271SD1305" and second["unit"] is None
    # Nothing is shared: the designs of the first are not the second's.
    assert [d["pattern"] for d in first["designs"]] == [1]
    assert second["designs"] == []
    assert workspace.duplicate(twin) == "Look22-3"
    with pytest.raises(ValueError):
        workspace.duplicate("Look99")


def test_each_garment_wears_its_own_designs_at_its_own_times(workspace):
    twin = workspace.duplicate("Look22")
    mine, its = "Look22_color_pattern01_grid.csv", "Look22-2_color_pattern01_grid.csv"
    workspace.save(its, GRID.replace("0x03", "0x05"))   # the same pattern number
    workspace.arrange({"Look22": "radxa-01", twin: "radxa-02"})
    workspace.set_timeline(600, [
        {"id": "a", "item": "Look22", "at": 0, "design": mine},
        {"id": "b", "item": twin, "at": 0, "design": its},
        {"id": "c", "item": twin, "at": 60, "design": mine}])       # not its
    cues = {c["id"]: c["problems"] for c in workspace.state()["show"]["cues"]}
    assert cues["a"] == [] and cues["b"] == [] and cues["c"]
    workspace.set_timeline(600, [
        {"id": "a", "item": "Look22", "at": 0, "design": mine},
        {"id": "b", "item": twin, "at": 0, "design": its},
        {"id": "d", "item": twin, "at": 90, "design": its}])
    shows, problems = workspace.compile_show()
    assert problems == []
    assert len(shows["radxa-01"]["cues"]) == 1 and len(shows["radxa-02"]["cues"]) == 2
    assert shows["radxa-01"]["cues"][0]["boards"] != shows["radxa-02"]["cues"][0]["boards"]
    # Deleting the files of one leaves the other whole.
    workspace.delete(its)
    workspace.delete("Look22-2_map.csv")
    assert [d["name"] for d in item(workspace.state(), "Look22")["designs"]] == [mine]


def test_the_boards_a_garment_really_carries_are_set_on_the_page(workspace):
    grid = "Look22_color_pattern01_grid.csv"
    twin = workspace.duplicate("Look22")
    workspace.save("Look22-2_color_pattern01_grid.csv", GRID)
    nos = [b["board_no"] for b in item(workspace.state(), "Look22")["boards"]]
    workspace.set_boards(twin, {str(no): 119 + n for n, no in enumerate(nos)})
    state = workspace.state()
    second = item(state, twin)
    assert [b["board_no"] for b in second["boards"]] == [119, 120, 121]
    assert [b["source_no"] for b in second["boards"]] == nos
    assert [b["dip_id"] for b in second["boards"]] == [1, 2, 3]
    assert {s[3] for s in second["map"]["scales"]} == {119, 120, 121}
    assert [b["board_no"] for b in item(state, "Look22")["boards"]] == nos
    # The CSV is not touched; the numbers are the show's, and undoable.
    assert (workspace.files / "Look22-2_map.csv").read_bytes() ==         (workspace.files / "Look22_map.csv").read_bytes()
    # What goes to the boards is the same picture under either numbering.
    workspace.arrange({"Look22": "radxa-01", twin: "radxa-02"})
    a, _ = workspace.compile_units({"Look22": grid}, "m")
    b, _ = workspace.compile_units({twin: "Look22-2_color_pattern01_grid.csv"}, "m")
    assert a["radxa-01"]["boards"] == b["radxa-02"]["boards"]
    # One number at a time, as the page sends them; rank decides the DIP id.
    workspace.set_boards(twin, {str(nos[0]): 150})
    second = item(workspace.state(), twin)
    assert [(b["board_no"], b["source_no"], b["dip_id"]) for b in second["boards"]]         == [(120, nos[1], 1), (121, nos[2], 2), (150, nos[0], 3)]
    workspace.undo()
    assert [b["board_no"] for b in item(workspace.state(), twin)["boards"]]         == [119, 120, 121]
    # Any item can be renumbered, not only a duplicate.
    workspace.set_boards("Look22", {str(nos[1]): 200})
    assert 200 in [b["board_no"] for b in item(workspace.state(), "Look22")["boards"]]
    # A duplicate starts with the numbers of what it copies.
    third = workspace.duplicate("Look22")
    assert 200 in [b["board_no"] for b in item(workspace.state(), third)["boards"]]


def test_board_numbers_are_checked_and_survive_a_new_csv(workspace):
    nos = [b["board_no"] for b in item(workspace.state(), "Look22")["boards"]]
    for wrong in ({str(nos[0]): nos[1]},            # there twice
                  {"999": 5},                       # not a board of the item
                  {str(nos[0]): 0}, {str(nos[0]): "x"}, [1]):
        with pytest.raises(ValueError):
            workspace.set_boards("Look22", wrong)
    with pytest.raises(ValueError):
        workspace.set_boards("Look99", {"1": 2})
    workspace.set_boards("Look22", {str(nos[0]): 300})
    # A new map whose own numbers collide with what was typed: the typed
    # numbers are dropped, and said so - never two boards merged into one.
    workspace.save("Look22_map.csv", MAP.replace(",20,5,020-05", ",300,5,300-05"))
    look = item(workspace.state(), "Look22")
    assert sorted(b["board_no"] for b in look["boards"]) == [17, 18, 300]
    assert any("no longer fit" in w for w in look["map"]["warnings"])


def test_designer_named_files_are_accepted_and_labelled(workspace):
    name = "Look22_color_ref_multicolor_redorange_s22_grid_A-1.csv"
    assert Workspace.kind(name) == "grid"
    assert Workspace.kind("AZ271SD1305_ref_multicolor_redorange_s22_HW.csv") is None
    workspace.save(name, GRID)
    designs = item(workspace.state(), "Look22")["designs"]
    assert [(d["label"], d["pattern"]) for d in designs] == \
        [("P01", 1), ("ref_multicolor_redorange_s22", None)]
    payloads, problems = workspace.compile_units({"Look22": name}, "m")
    assert problems == ["Look22: not assigned to a unit"]


# ---- sweeps ----

def test_the_show_file_carries_uint16_delay_tables_and_the_unit_of_ten_ms(workspace):
    grid = "Look22_color_pattern01_grid.csv"
    workspace.assign("Look22", "radxa-01")
    workspace.set_timeline(600, [
        {"id": "a", "item": "Look22", "at": 0, "design": grid},
        {"id": "b", "item": "Look22", "at": 60, "design": grid,
         "transition": "custom", "sequence": "top_down", "span_s": 2.0},
        {"id": "c", "item": "Look22", "at": 120, "design": grid}])
    show = workspace.state()["show"]
    a, b, c = show["cues"]
    assert (b["sweep"]["sequence"], b["sweep"]["span_s"], b["span"]) == \
        ("top_down", 2.0, 2.0)                              # rows 1 and 0
    assert (b["sent"], b["complete"]) == (60, 60 + 7 + 2)   # "at" IS Start
    assert a["span"] == 0 and a["problems"] == [] and b["problems"] == []
    assert workspace.state()["sequences"][0]["id"] == "natural"
    item22 = item(workspace.state(), "Look22")
    assert set(item22["sequences"]) == {"center", "top_down", "bottom_up",
                                        "left_right", "right_left"}
    assert len(item22["sequences"]["top_down"]) == len(item22["map"]["scales"])
    shows, problems = workspace.compile_show()
    assert problems == []
    assert shows["radxa-01"]["delay_unit_ms"] == 10
    cues = shows["radxa-01"]["cues"]
    # Every cue carries tables once one sweeps; the others say "no delay".
    assert [q["span"] for q in cues] == [0.0, 2.0, 0.0]
    assert [q["refresh_s"] for q in cues] == [7.0, 7.0, 7.0]   # the show's own
    assert set(cues[0]["delays"]) == {"1", "2", "3"}
    assert struct.unpack(">64H", bytes.fromhex(cues[0]["delays"]["1"])) == (0xFFFF,) * 64
    swept = struct.unpack(">64H", bytes.fromhex(cues[1]["delays"]["1"]))   # board 17: sockets 1, 60
    assert swept[1] == 0 and swept[60] == 0                 # row 1 is the top row
    row0 = struct.unpack(">64H", bytes.fromhex(cues[1]["delays"]["3"]))    # board 20, row 0
    assert row0[5] == 200                                    # 2.0 s = 200 frames of 10 ms
    # No sweep anywhere: no tables at all (the units need not write any).
    workspace.set_timeline(600, [{"id": "a", "item": "Look22", "at": 0, "design": grid}])
    shows, _ = workspace.compile_show()
    assert "delays" not in shows["radxa-01"]["cues"][0]


# ---- per-design transitions ----

def test_a_designs_transition_is_stored_and_undoable(workspace):
    grid = "Look22_color_pattern01_grid.csv"
    workspace.set_transition(grid, "top_down", 3.0)
    design = item(workspace.state(), "Look22")["designs"][0]
    assert design["transition"] == {"sequence": "top_down", "span_s": 3.0}
    assert workspace.state()["transitions"] == {grid: {"sequence": "top_down",
                                                        "span_s": 3.0}}
    assert workspace.undo()
    assert workspace.state()["transitions"] == {}
    assert item(workspace.state(), "Look22")["designs"][0]["transition"] == \
        {"sequence": "natural", "span_s": 0.0}
    with pytest.raises(ValueError):
        workspace.set_transition("nope.csv", "top_down", 3.0)
    # Natural, or a zero span, removes the entry - and is not a step if
    # there was nothing there to remove.
    steps = workspace.state()["history"]["undo"]
    workspace.set_transition(grid, "natural", 0)
    assert workspace.state()["history"]["undo"] == steps


def test_a_cue_inherits_the_design_transition_and_reaches_the_show_file(workspace):
    grid = "Look22_color_pattern01_grid.csv"
    workspace.assign("Look22", "radxa-01")
    workspace.set_transition(grid, "top_down", 2.0)
    workspace.set_timeline(600, [{"id": "a", "item": "Look22", "at": 0,
                                  "design": grid}])
    cue = workspace.state()["show"]["cues"][0]
    assert cue["transition"] == "design"                # nothing overridden
    assert cue["sweep"] == {"sequence": "top_down", "span_s": 2.0,
                            "source": "design"}
    assert cue["span"] == 2.0
    shows, problems = workspace.compile_show()
    assert problems == []
    assert shows["radxa-01"]["cues"][0]["span"] == 2.0
    assert "delays" in shows["radxa-01"]["cues"][0]
    # A cue that opts out of the design keeps its own numbers instead.
    workspace.set_timeline(600, [{"id": "a", "item": "Look22", "at": 0,
                                  "design": grid, "transition": "custom",
                                  "sequence": "natural", "span_s": 0}])
    cue = workspace.state()["show"]["cues"][0]
    assert cue["sweep"] == {"sequence": "natural", "span_s": 0.0, "source": "cue"}
    assert cue["span"] == 0.0


# ---- the show's music ----

def test_music_is_uploaded_served_and_removed(tmp_path):
    server = make_server(tmp_path, port=0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    try:
        data = (b"ID3" + bytes(range(256))) * 20             # a few KB
        upload = urllib.request.Request(
            f"{base}/api/music", data=data,
            headers={"X-File-Name": "AZ 27SS.DEMO.mp3"})
        with urllib.request.urlopen(upload, timeout=5) as response:
            body = json.loads(response.read())
        assert body["ok"] and body["music"]["name"] == "AZ 27SS.DEMO.mp3"
        assert body["music"]["size"] == len(data)
        assert body["music"]["type"] == "audio/mpeg"

        state = json.loads(urllib.request.urlopen(f"{base}/api/state",
                                                   timeout=5).read())
        assert state["music"]["name"] == "AZ 27SS.DEMO.mp3"
        assert state["music"]["size"] == len(data)
        assert state["music"]["url"].startswith("/api/music/file?v=")

        with urllib.request.urlopen(f"{base}{state['music']['url']}",
                                    timeout=5) as response:
            assert response.status == 200
            assert response.read() == data
            assert response.headers["Content-Type"] == "audio/mpeg"
            assert response.headers["Accept-Ranges"] == "bytes"
            # Versioned by ?v=<mtime>, so a long-lived cache is safe; an
            # ETag still saves the bytes again on a reload of it.
            assert response.headers["Cache-Control"] == \
                "private, max-age=31536000, immutable"
            assert response.headers["X-Content-Type-Options"] == "nosniff"
            etag = response.headers["ETag"]
            assert etag

        req = urllib.request.Request(f"{base}{state['music']['url']}",
                                     headers={"If-None-Match": etag})
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(req, timeout=5)
        assert caught.value.code == 304

        remove = urllib.request.Request(f"{base}/api/music/remove", data=b"{}")
        with urllib.request.urlopen(remove, timeout=5) as response:
            assert json.loads(response.read()) == {"ok": True}
        assert not (tmp_path / "music" / "AZ 27SS.DEMO.mp3").exists()
        state = json.loads(urllib.request.urlopen(f"{base}/api/state",
                                                   timeout=5).read())
        assert state["music"] is None
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(f"{base}/api/music/file", timeout=5)
        assert caught.value.code == 404
    finally:
        server.shutdown()
        server.server_close()


def test_music_answers_a_range_request_for_seeking(tmp_path):
    server = make_server(tmp_path, port=0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    try:
        data = bytes(range(256)) * 20                        # 5120 bytes
        upload = urllib.request.Request(f"{base}/api/music", data=data,
                                        headers={"X-File-Name": "clip.wav"})
        urllib.request.urlopen(upload, timeout=5).read()

        req = urllib.request.Request(f"{base}/api/music/file",
                                     headers={"Range": "bytes=10-19"})
        with urllib.request.urlopen(req, timeout=5) as response:
            assert response.status == 206
            assert response.read() == data[10:20]
            assert response.headers["Content-Range"] == f"bytes 10-19/{len(data)}"
            assert response.headers["Content-Length"] == "10"

        # A suffix range: the last 8 bytes.
        req = urllib.request.Request(f"{base}/api/music/file",
                                     headers={"Range": "bytes=-8"})
        with urllib.request.urlopen(req, timeout=5) as response:
            assert response.read() == data[-8:]

        # An end past EOF is clamped, not refused.
        req = urllib.request.Request(f"{base}/api/music/file",
                                     headers={"Range": f"bytes=0-{len(data) + 500}"})
        with urllib.request.urlopen(req, timeout=5) as response:
            assert response.status == 206 and response.read() == data

        # A malformed header falls back to a plain 200, not a refusal.
        req = urllib.request.Request(f"{base}/api/music/file",
                                     headers={"Range": "nonsense"})
        with urllib.request.urlopen(req, timeout=5) as response:
            assert response.status == 200 and response.read() == data

        # Entirely past the end: unsatisfiable.
        req = urllib.request.Request(f"{base}/api/music/file",
                                     headers={"Range": f"bytes={len(data)}-"})
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(req, timeout=5)
        assert caught.value.code == 416
        assert caught.value.headers["Content-Range"] == f"bytes */{len(data)}"

        req = urllib.request.Request(f"{base}/api/music/file", method="HEAD")
        with urllib.request.urlopen(req, timeout=5) as response:
            assert response.status == 200
            assert response.headers["Content-Length"] == str(len(data))
            assert response.headers["Accept-Ranges"] == "bytes"
    finally:
        server.shutdown()
        server.server_close()


def test_music_over_the_limit_is_refused(tmp_path):
    server = make_server(tmp_path, port=0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.putrequest("POST", "/api/music")
        conn.putheader("X-File-Name", "big.mp3")
        conn.putheader("Content-Length", str(MAX_MUSIC + 1))
        conn.endheaders()                    # no body: refused before reading
        response = conn.getresponse()
        assert response.status == 400
        body = json.loads(response.read())
        assert "MB" in body["error"]
        conn.close()
        assert not (tmp_path / "music").exists() or \
            not list((tmp_path / "music").glob("*"))
    finally:
        server.shutdown()
        server.server_close()


def test_music_and_transitions_survive_undo_and_redo(workspace):
    grid = "Look22_color_pattern01_grid.csv"
    workspace.set_transition(grid, "top_down", 2.0)                  # step 1
    workspace.save_music("clip.mp3", io.BytesIO(b"abcde"), 5)        # step 2
    assert workspace.music_info()["name"] == "clip.mp3"
    workspace.remove_music()                                        # step 3
    assert workspace.music_info() is None
    assert workspace.state()["history"]["undo"] == 3

    assert workspace.undo()                  # back to right after the upload...
    # ...but the file is gone: the pointer must not resurrect it.
    assert workspace.music_info() is None
    assert workspace.state()["transitions"][grid]["sequence"] == "top_down"

    assert workspace.undo()                  # back to before the upload
    assert workspace.music_info() is None
    assert workspace.state()["transitions"][grid]["sequence"] == "top_down"

    assert workspace.undo()                  # back to before the transition
    assert workspace.state()["transitions"] == {}

    assert workspace.redo() and workspace.redo() and workspace.redo()
    assert workspace.music_info() is None     # redoing "remove" again
    assert workspace.state()["transitions"][grid]["sequence"] == "top_down"


# ---- exporting and importing the timeline ----

def test_a_show_is_exported_as_a_json_file_and_imported_back(tmp_path):
    ws = Workspace(tmp_path / "ws")
    ws.save("Look22_map.csv", MAP)
    ws.save("Look22_color_pattern01_grid.csv", GRID)
    grid = "Look22_color_pattern01_grid.csv"
    ws.assign("Look22", "radxa-03")
    ws.set_transition(grid, "top_down", 2.0)
    ws.set_label("Look22", "22", "AZ271SD1305")
    ws.set_timeline(600, [{"id": "a", "item": "Look22", "at": 0, "design": grid}])

    server = make_server(ws.root, port=0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    try:
        with urllib.request.urlopen(f"{base}/api/show/export", timeout=5) as response:
            assert response.status == 200
            assert response.headers["Content-Type"] == "application/json"
            disposition = response.headers["Content-Disposition"]
            assert disposition.startswith("attachment;") and "ws-show-" in disposition
            exported = json.loads(response.read())
        assert exported["format"] == "epaper-show" and exported["version"] == 1
        assert exported["cues"][0]["item"] == "Look22"
        assert exported["transitions"][grid]["sequence"] == "top_down"
        assert exported["units"] == {"Look22": "radxa-03"}
        assert exported["music"] is None

        # Change everything, then import the export back.
        ws.set_timeline(600, [])
        ws.assign("Look22", None)
        steps = ws.state()["history"]["undo"]
        request = urllib.request.Request(
            f"{base}/api/show/import", data=json.dumps(exported).encode())
        with urllib.request.urlopen(request, timeout=5) as response:
            body = json.loads(response.read())
        assert body["ok"] and body["cues"] == 1 and body["warnings"] == []
        assert ws.state()["history"]["undo"] == steps + 1     # one step, not many
        state = ws.state()
        assert state["show"]["cues"][0]["item"] == "Look22"
        assert item(state, "Look22")["unit"] == "radxa-03"
        assert state["transitions"][grid]["sequence"] == "top_down"
    finally:
        server.shutdown()
        server.server_close()


def test_importing_the_wrong_kind_of_file_is_refused(workspace):
    with pytest.raises(ValueError):
        workspace.import_show({"version": 1, "cues": []})            # no format
    with pytest.raises(ValueError):
        workspace.import_show({"format": "epaper-show", "version": 2, "cues": []})
    with pytest.raises(ValueError):
        workspace.import_show({"format": "epaper-show", "version": 1,
                               "cues": "nope"})
    assert workspace.state()["history"]["undo"] == 0
    assert workspace.state()["show"]["cues"] == []


def test_import_keeps_what_the_file_does_not_mention(workspace):
    grid = "Look22_color_pattern01_grid.csv"
    workspace.save_music("clip.mp3", io.BytesIO(b"abcde"), 5)
    workspace.set_label("Look22", "22", "AZ271SD1305")
    cues, warnings = workspace.import_show({
        "format": "epaper-show", "version": 1,
        "cues": [{"id": "a", "item": "Look22", "at": 0, "design": grid}]})
    assert cues == 1 and warnings == []
    assert workspace.music_info()["name"] == "clip.mp3"                  # untouched
    assert item(workspace.state(), "Look22")["model"] == "AZ271SD1305"   # untouched
    assert workspace.state()["show"]["cues"][0]["item"] == "Look22"
    # A cue for an item or design this workspace does not have: a warning,
    # not a refusal - the import still lands, the timeline shows the problem.
    cues2, warnings2 = workspace.import_show({
        "format": "epaper-show", "version": 1,
        "cues": [{"id": "b", "item": "Nope", "at": 0, "design": "x.csv"}]})
    assert cues2 == 1 and warnings2 and "Nope" in warnings2[0]


# ---- Start / End / a cue's own refresh time ----

def test_old_cues_with_align_done_are_migrated_to_their_send_instant_once(workspace):
    grid = "Look22_color_pattern01_grid.csv"
    legacy = {"units": {"Look22": "radxa-01"}, "duration": 600, "refresh_s": 7,
              "cues": [{"id": "a", "item": "Look22", "at": 60, "design": grid,
                       "align": "done", "partial": False}]}
    (workspace.root / "show.json").write_text(json.dumps(legacy), encoding="utf-8")
    show = workspace.state()["show"]
    assert show["cues"][0]["at"] == 53          # 60 - 7 s refresh - 0 s span
    assert "align" not in show["cues"][0]
    assert workspace.state()["history"]["undo"] == 1       # one undo step
    # Idempotent: reading it again migrates nothing further.
    workspace.state()
    assert workspace.state()["history"]["undo"] == 1
    assert workspace.undo()
    restored = json.loads((workspace.root / "show.json").read_text(encoding="utf-8"))
    assert restored["cues"][0]["align"] == "done" and restored["cues"][0]["at"] == 60


def test_align_start_only_loses_the_key(workspace):
    grid = "Look22_color_pattern01_grid.csv"
    legacy = {"units": {}, "duration": 600, "refresh_s": 7,
              "cues": [{"id": "a", "item": "Look22", "at": 30, "design": grid,
                       "align": "start", "partial": False}]}
    (workspace.root / "show.json").write_text(json.dumps(legacy), encoding="utf-8")
    cue = workspace.state()["show"]["cues"][0]
    assert cue["at"] == 30 and "align" not in cue           # unchanged but for the key


def test_state_says_end_and_refresh_of_every_cue(workspace):
    grid = "Look22_color_pattern01_grid.csv"
    workspace.set_timeline(600, [
        {"id": "a", "item": "Look22", "at": 0, "design": grid},
        {"id": "b", "item": "Look22", "at": 60, "design": grid, "refresh_s": 3.0},
        {"id": "c", "item": "Look22", "at": 120, "design": grid}])
    cues = {c["id"]: c for c in workspace.state()["show"]["cues"]}
    assert cues["a"]["end"] == 60 and cues["a"]["end_source"] == "next"
    assert cues["b"]["end"] == 120 and cues["b"]["end_source"] == "next"
    assert cues["c"]["end"] == 600 and cues["c"]["end_source"] == "show"
    assert cues["b"]["refresh"] == 3.0 and cues["b"]["refresh_source"] == "cue"
    assert cues["a"]["refresh"] == 7.0 and cues["a"]["refresh_source"] == "show"


def test_export_import_round_trip_keeps_refresh_s(workspace):
    grid = "Look22_color_pattern01_grid.csv"
    workspace.set_timeline(600, [
        {"id": "a", "item": "Look22", "at": 0, "design": grid},
        {"id": "b", "item": "Look22", "at": 60, "design": grid, "refresh_s": 4.5}])
    exported = workspace.export_show()
    assert exported["cues"][1]["refresh_s"] == 4.5
    workspace.set_timeline(600, [])
    cues, warnings = workspace.import_show(exported)
    assert cues == 2 and warnings == []
    imported = {c["id"]: c for c in workspace.state()["show"]["cues"]}
    assert imported["b"]["refresh_s"] == 4.5
    assert imported["b"]["refresh"] == 4.5 and imported["b"]["refresh_source"] == "cue"


def test_a_custom_cue_with_zero_span_is_not_charged_bus_room_for_a_sweep(workspace):
    grid = "Look22_color_pattern01_grid.csv"
    workspace.assign("Look22", "radxa-01")
    workspace.set_timeline(600, [
        {"id": "a", "item": "Look22", "at": 0, "design": grid},
        {"id": "b", "item": "Look22", "at": 11, "design": grid,
         "transition": "custom", "sequence": "top_down", "span_s": 0}])
    show = workspace.state()["show"]
    b = show["cues"][1]
    assert b["sweep"]["sequence"] == "top_down" and b["sweep"]["span_s"] == 0.0
    assert b["span"] == 0.0
    assert b["problems"] == []
    shows, problems = workspace.compile_show()
    assert problems == []
    assert "delays" not in shows["radxa-01"]["cues"][1]     # nothing sweeps


# ---- adversarial-review fixes ----

def test_a_malformed_transitions_entry_from_import_does_not_break_state(workspace):
    grid = "Look22_color_pattern01_grid.csv"
    workspace.import_show({"format": "epaper-show", "version": 1,
                           "transitions": {grid: "oops", "other.csv": 5,
                                          "third.csv": {"sequence": "top_down",
                                                        "span_s": 2.0}}})
    assert workspace.state()["transitions"] == {
        "third.csv": {"sequence": "top_down", "span_s": 2.0}}
    workspace.set_timeline(600, [{"id": "a", "item": "Look22", "at": 0,
                                  "design": grid}])
    # state() must not crash resolving the cue's sweep against whatever
    # survived the import - a bad entry used to raise AttributeError here
    # and 500 every /api/state until an undo.
    state = workspace.state()
    assert state["show"]["cues"][0]["problems"] == []


def test_hostile_json_through_show_and_import_is_a_400_not_a_500(tmp_path):
    server = make_server(tmp_path, port=0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    def post(path, payload):
        request = urllib.request.Request(
            f"{base}{path}", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(request, timeout=5)
            return 200
        except urllib.error.HTTPError as exc:
            return exc.code
    try:
        assert post("/api/show", {"duration": None, "cues": []}) == 400
        assert post("/api/show", {"duration": 600,
                                  "cues": [{"id": "a", "item": "Look22",
                                           "at": None, "design": "x"}]}) == 400
        assert post("/api/show", {"duration": 600,
                                  "cues": [{"id": "a", "item": "Look22",
                                           "at": {}, "design": "x"}]}) == 400
        assert post("/api/show/import", {"format": "epaper-show", "version": 1,
                                         "duration": {}}) == 400
    finally:
        server.shutdown()
        server.server_close()


def test_set_transition_rejects_a_sweep_over_thirty_seconds(workspace):
    grid = "Look22_color_pattern01_grid.csv"
    with pytest.raises(ValueError):
        workspace.set_transition(grid, "top_down", 30.1)
    workspace.set_transition(grid, "top_down", 30.0)          # exactly 30 s: fine
    assert workspace.state()["transitions"][grid]["span_s"] == 30.0


def test_music_upload_rejects_an_unsupported_file_type(tmp_path):
    server = make_server(tmp_path, port=0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    try:
        upload = urllib.request.Request(
            f"{base}/api/music", data=b"not audio",
            headers={"X-File-Name": "notes.txt"})
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(upload, timeout=5)
        assert caught.value.code == 400
        body = json.loads(caught.value.read())
        assert "mp3" in body["error"]
        assert not (tmp_path / "music").exists()
    finally:
        server.shutdown()
        server.server_close()


def test_music_upload_name_is_unquoted_before_it_is_saved(tmp_path):
    server = make_server(tmp_path, port=0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    try:
        encoded = urllib.parse.quote("café.mp3")   # what encodeURIComponent sends
        upload = urllib.request.Request(
            f"{base}/api/music", data=b"abcde",
            headers={"X-File-Name": encoded})
        with urllib.request.urlopen(upload, timeout=5) as response:
            body = json.loads(response.read())
        # Unquoted first, so only the one accented letter is sanitised -
        # not every byte of its percent-encoding as well.
        assert body["music"]["name"] == "caf_.mp3"
    finally:
        server.shutdown()
        server.server_close()


def test_save_music_does_not_touch_the_old_file_if_the_commit_fails(workspace,
                                                                   monkeypatch):
    workspace.save_music("old.mp3", io.BytesIO(b"aaaaa"), 5)
    assert workspace.music_info()["name"] == "old.mp3"

    def boom(before, after):
        raise OSError("disk full")
    monkeypatch.setattr(workspace, "_commit", boom)
    with pytest.raises(OSError):
        workspace.save_music("new.mp3", io.BytesIO(b"bbbbb"), 5)
    # The pointer moves first: a failed commit must leave the old file
    # exactly as it was, never orphaned or deleted early.
    assert workspace.music_info()["name"] == "old.mp3"
    assert (workspace.music / "old.mp3").read_bytes() == b"aaaaa"
    assert not (workspace.music / "new.mp3").exists()
    assert not list(workspace.music.glob("*.part"))


# ---- SEEK and a manually-started position, over HTTP ----

def _post(port, path, body):
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_seek_needs_manual_control(tmp_path):
    from conductor.fleet import Fleet

    fleet = Fleet({})
    fleet.shows = {"radxa-01": {"id": "showA", "cues": [], "duration": 600}}
    server = make_server(tmp_path, port=0, fleet=fleet)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        for body in ({"to_s": 10.0}, {"to_s": 10.0, "manual": False},
                     {"to_s": 10.0, "manual": "true"},
                     {"to_s": 10.0, "manual": 1}):
            status, payload = _post(port, "/api/fleet/seek", body)
            assert status == 400
            assert payload["error"] == (
                'Manual control is off. Tick "Manual control" to move '
                "the show position.")
    finally:
        server.shutdown()
        server.server_close()


def test_seek_before_an_upload_says_upload_first(tmp_path):
    from conductor.fleet import Fleet

    fleet = Fleet({})
    server = make_server(tmp_path, port=0, fleet=fleet)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        status, payload = _post(port, "/api/fleet/seek",
                                {"to_s": 30.0, "manual": True})
        assert status == 200
        assert payload == {"units": {},
                           "note": "Nothing uploaded yet - Upload first."}
    finally:
        server.shutdown()
        server.server_close()


def test_seek_with_hostile_json_is_a_400_not_a_500(tmp_path):
    from conductor.fleet import Fleet

    fleet = Fleet({})
    fleet.shows = {"radxa-01": {"id": "showA", "cues": [], "duration": 600}}
    server = make_server(tmp_path, port=0, fleet=fleet)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        huge = 10 ** 400                          # float(huge) is OverflowError
        for body in ({"manual": True, "to_s": None},
                     {"manual": True, "to_s": "abc"},
                     {"manual": True, "to_s": {}},
                     {"manual": True, "to_s": [1]},
                     {"manual": True, "to_s": True},
                     {"manual": True},
                     {"manual": True, "to_s": 10.0, "lead_s": "fast"},
                     {"manual": True, "to_s": huge},
                     {"manual": True, "to_s": 10.0, "lead_s": huge}):
            status, _ = _post(port, "/api/fleet/seek", body)
            assert status == 400
    finally:
        server.shutdown()
        server.server_close()


def test_start_with_a_huge_json_integer_is_a_400_not_a_dropped_connection(tmp_path):
    # float(10**400) raises OverflowError, not ValueError - it must still
    # come back as a clean 400, not escape do_POST's except and drop the
    # connection (found in review).
    from conductor.fleet import Fleet

    fleet = Fleet({})
    fleet.shows = {"radxa-01": {"id": "showA", "cues": [], "duration": 600}}
    server = make_server(tmp_path, port=0, fleet=fleet)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        huge = 10 ** 400
        for body in ({"lead_s": huge}, {"from_s": huge, "manual": True},
                     {"lead_s": huge, "from_s": 10, "manual": True}):
            status, _ = _post(port, "/api/fleet/start", body)
            assert status == 400
    finally:
        server.shutdown()
        server.server_close()


def test_start_begins_where_the_seek_bar_was_left(tmp_path):
    from conductor.fleet import Fleet

    fleet = Fleet({})
    fleet.shows = {"radxa-01": {"id": "showA", "cues": [], "duration": 600}}
    fleet.links = {}
    server = make_server(tmp_path, port=0, fleet=fleet)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        status, seek_result = _post(port, "/api/fleet/seek",
                                    {"to_s": 90.0, "manual": True})
        assert status == 200
        assert seek_result["mode"] == "start_at"
        assert seek_result["start_at"] == 90.0
        assert seek_result["note"] == "START will begin at 1:30."
        status, start_result = _post(port, "/api/fleet/start", {"lead_s": 1})
        assert status == 200
        assert start_result["from_s"] == 90.0
        assert start_result["note"] == "Started from 1:30."
        assert fleet.start_at == 0.0                  # forgotten once used
    finally:
        server.shutdown()
        server.server_close()


def test_start_from_a_time_needs_manual_too(tmp_path):
    from conductor.fleet import Fleet

    fleet = Fleet({})
    fleet.shows = {"radxa-01": {"id": "showA", "cues": [], "duration": 600}}
    fleet.links = {}
    server = make_server(tmp_path, port=0, fleet=fleet)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        status, payload = _post(port, "/api/fleet/start",
                                {"lead_s": 1, "from_s": 90})
        assert status == 400
        assert payload["error"] == (
            'Manual control is off. Tick "Manual control" to start from '
            "a time other than 0:00.")
        assert fleet.run is None
        status, payload = _post(port, "/api/fleet/start",
                                {"lead_s": 1, "from_s": 90, "manual": True})
        assert status == 200 and payload["from_s"] == 90.0
        fleet.run = None
        # from_s = 0 never needs manual control.
        status, payload = _post(port, "/api/fleet/start",
                                {"lead_s": 1, "from_s": 0})
        assert status == 200 and payload["from_s"] == 0.0 and "note" not in payload
    finally:
        server.shutdown()
        server.server_close()


def test_start_from_a_seeked_position_is_refused_if_a_reupload_shrank_the_show(tmp_path):
    # The blocker found in review: SEEK to 9:00 on a 12:00 show, then a
    # re-upload shrinks it to 5:00 - the page never sends from_s, so
    # START must range-check the remembered position itself, or every
    # unit goes ENDED on its first tick and nothing ever fires.
    from conductor.fleet import Fleet

    fleet = Fleet({})
    fleet.shows = {"radxa-01": {"id": "showA", "cues": [], "duration": 720}}
    fleet.links = {}
    server = make_server(tmp_path, port=0, fleet=fleet)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        status, seek_result = _post(port, "/api/fleet/seek",
                                    {"to_s": 540.0, "manual": True})
        assert status == 200 and seek_result["mode"] == "start_at"
        fleet.shows = {"radxa-01": {"id": "showB", "cues": [],
                                    "duration": 300}}     # re-uploaded, shorter
        status, payload = _post(port, "/api/fleet/start", {"lead_s": 1})
        assert status == 400
        assert payload["error"] == "The show is 0:00 to 5:00."
        assert fleet.run is None                  # never started on a bad position
        assert fleet.start_at == 540.0             # unchanged - fixable, not lost
    finally:
        server.shutdown()
        server.server_close()


def test_seek_after_adopting_a_run_with_nothing_uploaded_here_is_honest(tmp_path):
    # A conductor restarted mid-show adopts the run from the units
    # (fleet.run is set) but has nothing of its own in fleet.shows - SEEK
    # cannot compute a show_duration or a sensible T0 there.
    from conductor.fleet import Fleet
    from tests.test_fleet import StubLink

    fleet = Fleet({})
    fleet.links = {"radxa-01": StubLink("radxa-01", "running")}
    server = make_server(tmp_path, port=0, fleet=fleet)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert fleet.snapshot()["run"] is not None     # adopted
        status, payload = _post(port, "/api/fleet/seek",
                                {"to_s": 30.0, "manual": True})
        assert status == 200
        assert payload == {"units": {}, "mode": "none", "note":
                           "This conductor did not upload the show - "
                           "Upload first."}
    finally:
        server.shutdown()
        server.server_close()


def test_a_seek_while_holding_answers_with_the_full_shape(tmp_path):
    from conductor.fleet import Fleet
    from tests.test_fleet import StubLink

    fleet = Fleet({})
    link = StubLink("radxa-01", "running")
    fleet.links = {"radxa-01": link}
    fleet.shows = {"radxa-01": {"id": "showA", "cues": [], "duration": 600}}
    fleet.run = {"t0": 700.0, "state": "holding", "held_at": 750.0}
    server = make_server(tmp_path, port=0, fleet=fleet)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        status, result = _post(port, "/api/fleet/seek",
                               {"to_s": 90.0, "manual": True})
        assert status == 200
        assert result["mode"] == "holding" and result["units"] == {}
        assert result["to_s"] == 90.0 and result["start_at"] == 0.0
        assert result["run"]["state"] == "holding"
        assert result["note"] == "On hold at 1:30. RESUME continues from here."
        assert link.posted == []                  # nothing sent while holding
    finally:
        server.shutdown()
        server.server_close()


def test_the_fleet_snapshot_carries_the_start_position_and_the_show_length(tmp_path):
    from conductor.fleet import Fleet

    fleet = Fleet({})
    server = make_server(tmp_path, port=0, fleet=fleet)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def get(path):
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}",
                                    timeout=5) as response:
            return json.loads(response.read())

    try:
        snap = get("/api/fleet")
        assert snap["start_at"] == 0.0 and snap["show_duration"] is None
        fleet.shows = {"radxa-01": {"id": "showA", "cues": [], "duration": 300},
                       "radxa-02": {"id": "showA", "cues": [], "duration": 480}}
        fleet.start_at = 45.0
        snap = get("/api/fleet")
        assert snap["start_at"] == 45.0 and snap["show_duration"] == 480.0
    finally:
        server.shutdown()
        server.server_close()

    # No fleet configured at all: the fallback carries the same keys.
    no_fleet = make_server(tmp_path, port=0)
    port = no_fleet.server_address[1]
    threading.Thread(target=no_fleet.serve_forever, daemon=True).start()
    try:
        snap = json.loads(urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/fleet", timeout=5).read())
        assert snap == {"units": [], "last_fire": None, "run": None,
                        "shows": {}, "corrections": [], "prepared": {},
                        "start_at": 0.0, "show_duration": None}
    finally:
        no_fleet.shutdown()
        no_fleet.server_close()


def test_a_seek_answers_per_unit_like_every_other_command(tmp_path):
    from conductor.fleet import Fleet
    from tests.test_fleet import StubLink

    fleet = Fleet({})
    link = StubLink("radxa-02", "running")
    fleet.links = {"radxa-02": link}
    fleet.shows = {"radxa-02": {"id": "showA", "cues": [], "duration": 600}}
    fleet.run = {"t0": 1000.0 - 60.0, "state": "running", "held_at": None}
    server = make_server(tmp_path, port=0, fleet=fleet)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        status, result = _post(port, "/api/fleet/seek",
                               {"to_s": 30.0, "manual": True, "lead_s": 0.5})
        assert status == 200 and result["mode"] == "running"
        # The same {unit: {"ok": ...}} shape every other fleet command answers with.
        assert result["units"] == {"radxa-02": {"ok": True}}
        status, hold_result = _post(port, "/api/fleet/hold", {})
        assert hold_result["units"] == {"radxa-02": {"ok": True, "phase": None}}
    finally:
        server.shutdown()
        server.server_close()


def test_a_manual_prepare_carries_the_designs_delay_tables(workspace):
    """The Designs tab's Prepare sweeps the way the design says, like a
    timeline cue does: one 128-byte table per board, the last scale on
    exactly the span; a natural design sends no tables at all."""
    import struct
    grid = "Look22_color_pattern01_grid.csv"
    workspace.assign("Look22", "radxa-01")
    payloads, problems = workspace.compile_units({"Look22": grid}, "m")
    assert problems == [] and "delays" not in payloads["radxa-01"]
    workspace.set_transition(grid, "top_down", 3.0)
    payloads, problems = workspace.compile_units({"Look22": grid}, "m")
    assert problems == []
    body = payloads["radxa-01"]
    assert sorted(body["delays"]) == sorted(body["boards"])
    frames = []
    for table in body["delays"].values():
        raw = bytes.fromhex(table)
        assert len(raw) == 128
        frames += [f for f in struct.unpack(">64H", raw) if f != 0xFFFF]
    assert min(frames) == 0 and max(frames) == 300      # 3.0 s in 10 ms frames

