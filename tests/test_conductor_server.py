"""conductor/server.py: the workspace behind the web UI, and its HTTP API.

The server is started on an ephemeral localhost port with a temp
workspace; the CSVs are the small fixtures from tests/test_look.py.
"""

from __future__ import annotations

import json
import sys
import threading
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conductor.server import Workspace, make_server
from tests.test_look import GRID, MAP, SKIRT_GRID, SKIRT_MAP


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
    assert [d["pattern"] for d in look["designs"]] == [1]
    assert look["designs"][0]["colors"]["front|1|1"] == 0x03
    assert look["designs"][0]["problems"] == [] and look["problems"] == []
    assert [b["dip_id"] for b in look["boards"]] == [1, 2, 3]
    assert state["palette"][5]["name"] == "Green"
    assert len(state["units"]) == 10


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
    assert preset["align"] == "done"                            # cleaned
    assert (preset["sent"], preset["complete"]) == (-7, 0)
    assert (second["sent"], second["complete"]) == (113, 120)
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
    assert (show["cues"][0]["sent"], show["cues"][0]["complete"]) == (104, 120)
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
