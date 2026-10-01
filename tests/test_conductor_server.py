"""conductor/server.py: the workspace behind the web UI, and its HTTP API.

The server is started on an ephemeral localhost port with a temp
workspace; the CSVs are the small fixtures from tests/test_look.py.
"""

from __future__ import annotations

import base64
import http.client
import io
import json
import re
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from html import unescape
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conductor.server import (MAX_MUSIC, START_COUNTDOWN_S, Workspace,
                              check_start_countdown, make_server,
                              start_countdown_of)
from tests.test_fleet import StubLink
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


def test_only_the_two_csv_kinds_are_accepted_and_a_path_is_refused(tmp_path):
    ws = Workspace(tmp_path)
    with pytest.raises(ValueError):
        ws.save("cables.csv", "x")
    with pytest.raises(ValueError):
        ws.save("Look22_map.txt", "x")
    with pytest.raises(ValueError):
        ws.assign("Look22", "radxa-99")
    # A name with a path in it is REFUSED, not quietly reduced to its
    # basename (review of 3fd1a42): taking the basename let
    # "sub/Look22_map.csv" become a file of this workspace through
    # /api/files while the simulator and import_bundle both turned it
    # away, and the error it did raise quoted the stripped name rather
    # than what the caller sent.
    for bad in ("../../evil/Look22_map.csv", "sub/Look22_map.csv",
                "sub\\Look22_map.csv"):
        with pytest.raises(ValueError) as caught:
            ws.save(bad, MAP)
        assert str(caught.value).startswith(bad + ": ")
        assert "path separator" in str(caught.value)
    assert list((tmp_path / "files").glob("*.csv")) == []
    # The plain name still saves, and delete still takes a bare one.
    assert ws.save("Look22_map.csv", MAP) == "Look22_map.csv"
    assert (tmp_path / "files" / "Look22_map.csv").is_file()
    ws.delete("Look22_map.csv")
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
            # Straight from the wiring site's "HW 用 CSV" button, under
            # the name that button gives it (2026-09-26).
            {"name": "Look22_1_HW.csv", "text": GRID},
            {"name": "notes.csv", "text": "x"}]})
        result = json.loads(raw)
        assert len(result["saved"]) == 3 and len(result["refused"]) == 1
        state = json.loads(call("/api/state")[1])
        # The _HW.csv belongs to Look22 like any other design, and the
        # Designs list calls it by the 配色案名 in its name ("1").
        assert [(d["name"], d["label"]) for d in state["items"][0]["designs"]] == [
            ("Look22_color_pattern01_grid.csv", "P01"),
            ("Look22_1_HW.csv", "1")]
        call("/api/assign", {"item": "Look22", "unit": "radxa-03"})
        state = json.loads(call("/api/state")[1])
        assert state["items"][0]["unit"] == "radxa-03"
        call("/api/delete", {"name": "Look22_color_pattern01_grid.csv"})
        call("/api/delete", {"name": "Look22_1_HW.csv"})
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
    # A new show opens on the current default refresh, the effect included.
    assert show["duration"] == 600 and show["refresh_s"] == 8
    assert show["refresh_default"] == 8 and show["panel_repaint_s"] == 7
    assert show["gap_s"] == 1.0
    assert [c["id"] for c in show["cues"]] == ["a", "b"]        # by time
    preset, second = show["cues"]
    assert "align" not in preset and "align" not in second     # cleaned away
    assert (preset["sent"], preset["complete"]) == (-8, 0)
    assert (second["sent"], second["complete"]) == (120, 128)  # "at" IS Start
    assert preset["refresh"] == second["refresh"] == 8
    assert preset["refresh_source"] == second["refresh_source"] == "show"
    assert preset["end"] == 120 and preset["end_source"] == "next"
    assert second["end"] == 600 and second["end_source"] == "show"
    assert preset["problems"] == second["problems"] == []
    assert show["warnings"] == []
    # 3 boards: refresh-bound - the 8 s refresh + the director's 1 s gap is
    # more than writing 3 boards (3 x 0.22 s + 1 s margin) would need.
    assert round(show["min_interval"]["(Look22)"], 2) == 9.0


def test_refresh_time_is_a_setting_of_the_show(workspace):
    cues = [{"id": "a", "item": "Look22", "at": "2:00",
             "design": "Look22_color_pattern01_grid.csv"}]
    workspace.set_timeline(600, cues, refresh=16)       # a unit on old firmware
    show = workspace.state()["show"]
    assert show["refresh_s"] == 16
    assert (show["cues"][0]["sent"], show["cues"][0]["complete"]) == (120, 136)
    # Still refresh-bound at 16 s: 16 + 1 s gap.
    assert round(show["min_interval"]["(Look22)"], 2) == 17.0
    workspace.set_timeline(600, cues)                   # not given: kept
    assert workspace.state()["show"]["refresh_s"] == 16
    assert workspace.undo() and workspace.state()["show"]["refresh_s"] == 8
    for bad in (0, 61, "fast"):
        with pytest.raises(ValueError):
            workspace.set_timeline(600, cues, refresh=bad)


def test_the_shortest_interval_is_what_this_show_needs_on_that_unit(workspace):
    """The readout is per unit and per SHOW: a unit carrying a 7 s sweep needs
    15 s between sends, and saying 9 s while validate() rejects a cue 14 s
    later reads as a contradiction (review, 2026-09-26)."""
    grid = "Look22_color_pattern01_grid.csv"
    workspace.assign("Look22", "radxa-01")
    # No cues on it yet: the default floor, refresh + gap.
    assert round(workspace.state()["show"]["min_interval"]["radxa-01"], 2) == 9.0
    workspace.set_transition(grid, "top_down", 7.0)
    workspace.set_timeline(600, [{"id": "a", "item": "Look22", "at": 60,
                                  "design": grid}])
    state = workspace.state()
    cue = state["show"]["cues"][0]
    assert cue["span"] == 7.0 and cue["complete"] == 74.0
    assert round(state["show"]["min_interval"]["radxa-01"], 2) == 15.0
    # ...and that IS the floor validate() applies to the next send.
    workspace.set_timeline(600, [{"id": "a", "item": "Look22", "at": 60,
                                  "design": grid},
                                 {"id": "b", "item": "Look22", "at": 75,
                                  "design": grid}])
    assert workspace.state()["show"]["cues"][1]["problems"] == []
    workspace.set_timeline(600, [{"id": "a", "item": "Look22", "at": 60,
                                  "design": grid},
                                 {"id": "b", "item": "Look22", "at": 74.9,
                                  "design": grid}])
    assert workspace.state()["show"]["cues"][1]["problems"]


def test_timeline_survives_a_unit_assignment_and_back(workspace):
    workspace.set_timeline(300, [{"id": "a", "item": "Look22", "at": 0,
                                  "design": "Look22_color_pattern01_grid.csv"}])
    workspace.assign("Look22", "radxa-04")
    state = workspace.state()
    assert state["items"][0]["unit"] == "radxa-04"
    assert len(state["show"]["cues"]) == 1
    assert "radxa-04" in state["show"]["min_interval"]


def test_timeline_rejects_nonsense_durations(workspace):
    for bad in (0, "abc", 7 * 3600, float("nan")):
        with pytest.raises(ValueError):
            workspace.set_timeline(bad, [])


# ---- a show is at most 15:00 (2026-09-28: the music became 10:54) ----

def test_a_show_may_last_up_to_fifteen_minutes_and_no_longer(workspace):
    workspace.set_timeline("15:00", [])
    assert workspace.state()["show"]["duration"] == 900
    workspace.set_timeline(900, [])
    assert workspace.state()["show"]["duration"] == 900
    for too_long in (901, "15:01", 900.5, "1:00:00", float("inf")):
        with pytest.raises(ValueError) as caught:
            workspace.set_timeline(too_long, [])
        assert str(caught.value) == "A show is at most 15:00", too_long
    assert workspace.state()["show"]["duration"] == 900     # the refusal kept it


def test_a_cue_past_a_fifteen_minute_show_is_still_a_problem(workspace):
    grid = "Look22_color_pattern01_grid.csv"
    workspace.set_timeline(900, [{"id": "a", "item": "Look22", "at": 0, "design": grid},
                                 {"id": "b", "item": "Look22", "at": "14:59", "design": grid},
                                 {"id": "c", "item": "Look22", "at": "15:01", "design": grid}])
    problems = {c["id"]: c["problems"] for c in workspace.state()["show"]["cues"]}
    assert problems["a"] == [] and problems["b"] == []
    assert any("after the end of the show (15:00)" in p for p in problems["c"]), problems["c"]


def test_a_show_file_without_a_duration_is_still_ten_minutes(workspace):
    # LEGACY: show.json never written by set_timeline (a fresh workspace, or
    # one from before `duration` was stored) is a 600 s show - export,
    # state() and the fleet all read the same default.
    assert "duration" not in workspace._load_show()
    assert workspace.state()["show"]["duration"] == 600
    assert workspace.export_show()["duration"] == 600
    # ...and a show file that does not mention it keeps what is there.
    workspace.set_timeline("12:00", [])
    workspace.import_show({"format": "epaper-show", "version": 1, "cues": []})
    assert workspace.state()["show"]["duration"] == 720


def test_load_show_and_load_bundle_refuse_a_show_longer_than_fifteen_minutes(workspace):
    workspace.set_timeline(700, [])
    for payload in ({"format": "epaper-show", "version": 1, "duration": 901, "cues": []},):
        with pytest.raises(ValueError) as caught:
            workspace.import_show(payload)
        assert str(caught.value) == "A show is at most 15:00"
        with pytest.raises(ValueError) as caught:
            workspace.import_bundle({"format": "epaper-show-bundle", "version": 1,
                                     "files": {}, "show": payload})
        assert str(caught.value) == "A show is at most 15:00"
    assert workspace.state()["show"]["duration"] == 700
    workspace.import_show({"format": "epaper-show", "version": 1, "duration": 900,
                           "cues": []})
    assert workspace.state()["show"]["duration"] == 900


def test_post_show_says_why_a_long_show_was_refused(tmp_path):
    server = make_server(tmp_path, port=0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        status, answer = _post(port, "/api/show", {"duration": 901, "cues": []})
        assert status != 200 or not answer.get("ok"), answer
        assert "A show is at most 15:00" in json.dumps(answer), answer
        status, answer = _post(port, "/api/show", {"duration": "15:00", "cues": []})
        assert status == 200 and answer.get("ok"), answer
    finally:
        server.shutdown()
        server.server_close()


def test_the_pages_share_the_fifteen_minute_ceiling():
    from conductor import timeline

    assert timeline.MAX_DURATION_S == 900 and timeline.DEFAULT_DURATION_S == 600
    root = Path(__file__).resolve().parents[1] / "conductor" / "web"
    for name in ("index.html", "sim/designer-app.js"):
        text = (root / name).read_text(encoding="utf-8")
        assert re.findall(r"const MAX_SHOW_DURATION_S = (.*?);", text) == ["15 * 60"], name
    model = (root / "sim" / "model.js").read_text(encoding="utf-8")
    assert re.findall(r"^  const MAX_DURATION_S = ([0-9.]+);$", model, re.M) == ["900.0"]


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
        assert serve(tmp_path, port) == 0
        assert "already running" in capsys.readouterr().out
        # ...when it IS this Conductor. Another workspace (or label) on the
        # port is a DIFFERENT Conductor: said, not opened (2026-10-01: the
        # show's launcher must never open the exhibition's page, nor the
        # other way round).
        assert serve(tmp_path / "other", port) == 2
        out = capsys.readouterr().out
        assert "a different Conductor is on port" in out and "close it" in out
        assert f"workspace {tmp_path.name}" in out and "workspace other" in out
        assert serve(tmp_path, port, label="EXHIBITION") == 2
        assert "label none" in capsys.readouterr().out
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

    # These test START's own gates, not the exhibition's preset stage
    # (test_conductor_exhibition.py): off, so START is main's immediate one.
    Workspace(tmp_path).set_preset_before_start(False)
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
        # An online unit already holding this show - START's burn gate
        # (fleet.py) would otherwise refuse an unconfigured/offline one.
        link = StubLink("radxa-01", "stopped")
        link.status["show"]["id"] = "x"
        fleet.links = {"radxa-01": link}
        post("/api/fleet/start", {"lead_s": 1})
        t0 = fleet.run["t0"]
        again = post("/api/fleet/start", {"lead_s": 1})     # the double click
        assert "already running" in again["note"] and fleet.run["t0"] == t0
        post("/api/fleet/start", {"lead_s": 1, "force": True})
        assert fleet.run["t0"] > t0
        assert "not on hold" in post("/api/fleet/resume", {})["note"]
        post("/api/fleet/stop", {})
        # PRESET carries the page's `force` to the fleet (review F3): a
        # unit that failed to burn some boards is refused without it
        # and shown the preset with it, the unit hearing the same force.
        link.status["show"]["burn"] = {"done": 1, "total": 2,
                                       "failed": [[3, 1]], "state": "failed"}
        link.posted.clear()
        status, refused = _post(port, "/api/fleet/preset", {})
        assert status == 400
        assert ("radxa-01: 1 of 2 pictures not written on board 3"
                in refused["error"])
        assert link.posted == []
        shown = post("/api/fleet/preset", {"force": True})
        assert shown["units"]["radxa-01"]["ok"]
        assert link.posted == [("/show/preset", {"force": True})]
        # ...but a burn still in flight is refused whatever the page says.
        link.status["show"]["burn"]["state"] = "burning"
        link.posted.clear()
        status, refused = _post(port, "/api/fleet/preset", {"force": True})
        assert status == 400 and "still writing 1/2" in refused["error"]
        assert link.posted == []
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


# ---- a DIP ID set by hand (show.json's `dips`) ----

D1301 = "AZ271SD1301"
# The real garment's shape: 27 boards, map numbers 92-118, one scale each.
D1301_MAP = "side,row,col,board_no,socket\n" + "".join(
    f"front,0,{n + 1},{92 + n},1\n" for n in range(27))
D1301_GRID = ("side,row,shift," + ",".join(str(n + 1) for n in range(27))
              + "\nfront,0,0," + ",".join("0x00" for _ in range(27)) + "\n")


@pytest.fixture
def d1301(tmp_path):
    ws = Workspace(tmp_path / "ws")
    ws.save(f"{D1301}_map.csv", D1301_MAP)
    ws.save(f"{D1301}_color_pattern01_grid.csv", D1301_GRID)
    ws.assign(D1301, "radxa-03")
    return ws


def dips_of(state, name):
    return [(b["board_no"], b["dip_id"], b["dip_by_hand"])
            for b in item(state, name)["boards"]]


def test_a_dip_id_set_by_hand_reaches_the_page_and_the_unit(d1301):
    # LOOK 25's DIP 27 was changed to 28 on the garment (2026-09-27): a
    # 27-board item whose ranks can only reach 27.
    assert dips_of(d1301.state(), D1301)[-1] == (118, 27, False)
    d1301.set_dips(D1301, {"118": 28})
    rows = dips_of(d1301.state(), D1301)
    assert [dip for _, dip, _ in rows] == list(range(1, 27)) + [28]
    assert rows[-1] == (118, 28, True) and rows[-2] == (117, 26, False)
    # SWITCHES ON follows the DIP: 28 is 11100, three switches.
    last = item(d1301.state(), D1301)["boards"][-1]
    assert last["switches_on"] == "3 4 5"
    # ...and the unit's own show file addresses that board as 28.
    d1301.set_timeline(60, [{"id": "a", "item": D1301, "at": 0,
                             "design": f"{D1301}_color_pattern01_grid.csv"}],
                       refresh=1.0)
    shows, problems = d1301.compile_show()
    assert problems == []
    assert shows["radxa-03"]["boards"] == list(range(1, 27)) + [28]
    assert "28" in shows["radxa-03"]["cues"][0]["state"]
    # A manual cue (Designs tab Prepare) uses the same addresses.
    payloads, problems = d1301.compile_units(
        {D1301: f"{D1301}_color_pattern01_grid.csv"}, "m")
    assert problems == [] and "28" in payloads["radxa-03"]["boards"]


def test_a_dip_id_already_on_the_bus_is_refused_with_the_board_named(d1301):
    with pytest.raises(ValueError) as caught:
        d1301.set_dips(D1301, {"118": 26})
    assert str(caught.value) == ("DIP 26 would be used twice on radxa-03 "
                                 "(boards 117, 118)")
    assert d1301.state()["items"][0]["boards"][-1]["dip_id"] == 27
    for wrong in ({"118": 0}, {"118": 61}, {"999": 28}, {"118": "x"}, [1]):
        with pytest.raises(ValueError):
            d1301.set_dips(D1301, wrong)
    with pytest.raises(ValueError):
        d1301.set_dips("Look99", {"1": 2})


def test_a_dip_id_is_undone_and_only_none_takes_it_off(d1301):
    d1301.set_dips(D1301, {"118": 28})
    d1301.undo()
    assert dips_of(d1301.state(), D1301)[-1] == (118, 27, False)
    d1301.redo()
    assert dips_of(d1301.state(), D1301)[-1] == (118, 28, True)
    # The badge's "✕" - None - is the ONLY way back to the rank.
    d1301.set_dips(D1301, {"118": None})
    assert dips_of(d1301.state(), D1301)[-1] == (118, 27, False)
    # A setting that happens to equal the rank is a statement about the
    # switches, not a no-op: it is kept, badge and all, so a later
    # renumbering cannot move that board's address off the hardware with
    # nothing recorded to stop it (found in review).
    d1301.set_dips(D1301, {"118": 27})
    assert dips_of(d1301.state(), D1301)[-1] == (118, 27, True)
    assert d1301.export_show()["dips"] == {D1301: {"118": 27}}
    # ...and setting the same thing twice is still not a second step.
    before = d1301.state()["history"]
    d1301.set_dips(D1301, {"118": 27})
    assert d1301.state()["history"] == before


def test_renumbering_a_board_carries_its_hand_set_dip_over(d1301):
    """`dips` is keyed on the number the PAGE shows, which set_boards()
    changes - so it has to carry the setting to the board's new number in
    the same step. Renumbering 118 -> 119 used to leave `dips` saying
    {"118": 28}: the board went back to its rank while its switches still
    read 28, so its cue painted nothing and the board really at 27 took
    it - with no warning anywhere (found in review)."""
    d1301.set_dips(D1301, {"118": 28})
    d1301.set_boards(D1301, {"118": 119})
    rows = {no: (dip, hand) for no, dip, hand in dips_of(d1301.state(), D1301)}
    assert 118 not in rows and rows[119] == (28, True)
    assert d1301.export_show()["dips"] == {D1301: {"119": 28}}
    # Undo takes both halves back together: one step, one meaning.
    d1301.undo()
    assert dips_of(d1301.state(), D1301)[-1] == (118, 28, True)


def test_a_renumbering_onto_another_boards_number_moves_no_dip_onto_it(d1301):
    """The worse half of the same bug: 117 -> 118 and 118 -> 130 in one
    call. A stale {"118": 28} would have become the setting of what used to
    be 117 - a different board, silently addressed by hand."""
    d1301.set_dips(D1301, {"118": 28})
    d1301.set_boards(D1301, {"117": 118, "118": 130})
    rows = {no: (dip, hand) for no, dip, hand in dips_of(d1301.state(), D1301)}
    # 118 is now the board that was 117, and it was never set by hand.
    assert rows[118] == (26, False)
    # The setting followed its own board to 130.
    assert rows[130] == (28, True)
    assert d1301.export_show()["dips"] == {D1301: {"130": 28}}


def test_a_board_left_alone_by_a_renumbering_keeps_its_dip(d1301):
    # Two boards set by hand; one of them is renumbered, the other is not.
    d1301.set_dips(D1301, {"117": 40, "118": 28})
    d1301.set_boards(D1301, {"118": 119})
    assert d1301.export_show()["dips"] == {D1301: {"117": 40, "119": 28}}
    rows = {no: (dip, hand) for no, dip, hand in dips_of(d1301.state(), D1301)}
    assert rows[117] == (40, True) and rows[119] == (28, True)


def test_one_unusable_dip_in_a_hand_edited_show_leaves_the_others_badged(
        tmp_path):
    """A show.json edited by hand: board 20's DIP is out of range and board
    17's is a perfectly good 5. The bus falls back to the ranks and says why
    - but 17 still wears its badge, because its address really was set by
    hand. _own_dips() is per entry for exactly this (found in review)."""
    ws = Workspace(tmp_path / "ws")
    ws.save("Look22_map.csv", MAP)                  # boards 17, 18, 20
    (ws.root / "show.json").write_text(json.dumps(
        {"dips": {"Look22": {"17": 5, "20": 61}}}), encoding="utf-8")
    look = item(ws.state(), "Look22")
    assert [b["dip_id"] for b in look["boards"]] == [1, 2, 3]   # the ranks
    # Both were set by hand - 20's is refused, not unsaid - and 17 keeps
    # its badge instead of being thrown away with 20.
    assert [b["dip_by_hand"] for b in look["boards"]] == [True, False, True]
    assert look["problems"] == ["board 20: DIP 61 is outside 1-60"]
    # A value that is not a number at all cannot be shown as a DIP ID, so
    # there is no badge for it - but it is still said, never swallowed.
    (ws.root / "show.json").write_text(json.dumps(
        {"dips": {"Look22": {"17": 5, "20": "x"}}}), encoding="utf-8")
    look = item(ws.state(), "Look22")
    assert [b["dip_by_hand"] for b in look["boards"]] == [True, False, False]
    assert look["problems"] == [
        "board 20: the DIP ID set by hand ('x') is not a number"]


def test_a_dip_id_is_set_across_two_items_of_one_unit(workspace):
    # Look22 (17, 18, 20) and a skirt (1, 2) share radxa-02: ranks 3, 4, 5
    # and 1, 2. One board of each is typed over, and the two settings live
    # under their own items but are judged on the one bus.
    workspace.save("Look20-Skirt_map.csv", SKIRT_MAP)
    workspace.assign("Look22", "radxa-02")
    workspace.assign("Look20-Skirt", "radxa-02")
    workspace.set_dips("Look22", {"20": 30})
    workspace.set_dips("Look20-Skirt", {"1": 29})
    state = workspace.state()
    assert dips_of(state, "Look22") == [(17, 3, False), (18, 4, False),
                                        (20, 30, True)]
    assert dips_of(state, "Look20-Skirt") == [(1, 29, True), (2, 2, False)]
    # A DIP the OTHER item already holds is refused, unit named.
    with pytest.raises(ValueError) as caught:
        workspace.set_dips("Look20-Skirt", {"2": 30})
    assert "DIP 30 would be used twice on radxa-02" in str(caught.value)


def test_four_switches_on_is_a_warning_beside_upload_not_a_refusal(d1301):
    # 15 is 1111 and 27 is 11011: four switches, which the operator reports
    # unreliable on the bus (2026-09-27). Those the RANKS produce are
    # warned about too - nothing is refused, and nothing is renumbered.
    warnings = d1301.state()["show"]["warnings"]
    flagged = [w for w in warnings if "4+ switches on" in w]
    assert [w.split("DIP ")[1].split(" ")[0] for w in flagged] == \
        ["15", "23", "27"]
    assert flagged[0] == (f"{D1301} board 106: DIP 15 has 4+ switches on - "
                          "reported unreliable; set another ID by hand")
    assert [b["dip_unreliable"] for b in item(d1301.state(), D1301)["boards"]]\
        .count(True) == 3
    # Setting the flagged 27 to 28 (11100) takes that one off the list.
    d1301.set_dips(D1301, {"118": 28})
    after = [w for w in d1301.state()["show"]["warnings"]
             if "4+ switches on" in w]
    assert len(after) == 2 and all("DIP 27" not in w for w in after)
    # Still only a warning: the show builds.
    d1301.set_timeline(60, [{"id": "a", "item": D1301, "at": 0,
                             "design": f"{D1301}_color_pattern01_grid.csv"}],
                       refresh=1.0)
    assert d1301.compile_show()[1] == []


def test_dips_survive_an_export_and_an_import(d1301):
    d1301.set_dips(D1301, {"118": 28})
    exported = d1301.export_show()
    assert exported["dips"] == {D1301: {"118": 28}}
    d1301.set_dips(D1301, {"118": None})
    assert dips_of(d1301.state(), D1301)[-1] == (118, 27, False)
    d1301.import_show(exported)
    assert dips_of(d1301.state(), D1301)[-1] == (118, 28, True)


def test_a_bundle_without_dips_keeps_the_ones_this_workspace_has(d1301):
    # The switches are a fact about the boards standing here, not about the
    # timeline that arrived - the same rule `boards` follows.
    d1301.set_dips(D1301, {"118": 28})
    bundle = {"format": "epaper-show-bundle", "version": 1, "files": {},
              "show": d1301.export_show()}
    bundle["show"].pop("dips")
    answer = d1301.import_bundle(bundle)
    assert answer["dips_kept"] is True
    assert dips_of(d1301.state(), D1301)[-1] == (118, 28, True)
    # A bundle that DOES carry a populated mapping replaces it.
    bundle["show"]["dips"] = {D1301: {"117": 40}}
    answer = d1301.import_bundle(bundle)
    assert answer["dips_kept"] is False
    rows = {no: (dip, hand) for no, dip, hand in dips_of(d1301.state(), D1301)}
    assert rows[117] == (40, True) and rows[118] == (27, False)


def test_a_dip_id_is_set_over_http(tmp_path):
    """The exact call the operator's page makes - and the one to make by
    hand against the show PC when there is no time to click."""
    ws = Workspace(tmp_path / "ws")
    ws.save(f"{D1301}_map.csv", D1301_MAP)
    ws.assign(D1301, "radxa-03")
    server = make_server(ws.root, port=0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def post(path, body):
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read())

    try:
        assert post("/api/boards",
                    {"item": D1301, "dips": {"118": 28}}) == {"ok": True}
        state = json.loads(urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/state", timeout=5).read())
        assert dips_of(state, D1301)[-1] == (118, 28, True)
        # A clash comes back as the page's own toast, not a traceback.
        with pytest.raises(urllib.error.HTTPError) as caught:
            post("/api/boards", {"item": D1301, "dips": {"117": 28}})
        assert "would be used twice on radxa-03" in \
            json.loads(caught.value.read())["error"]
        # ...and the ✕ / empty cell sends null.
        assert post("/api/boards",
                    {"item": D1301, "dips": {"118": None}}) == {"ok": True}
        with pytest.raises(urllib.error.HTTPError):
            post("/api/boards", {"item": D1301})
    finally:
        server.shutdown()
        server.server_close()


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


# ---- taking picked CSVs in (/api/files -> Workspace.intake) ----

def _names(entries):
    return [{"name": name, "text": text} for name, text in entries]


def test_intake_never_lets_one_design_land_on_another(workspace):
    # 2026-09-26, "the 4th of 5 CSVs was overwritten": a second design
    # under a name the workspace already holds is numbered, not dropped
    # on top of the first - and the reply says so, naming both names.
    first = GRID
    second = GRID.replace("0x03", "0x02")
    result = workspace.intake(_names([
        ("Look22_color_pattern01_grid.csv", second)]))
    # The number goes on the DESIGN name, before _grid.csv.
    assert result["saved"] == ["Look22_color_pattern01-2_grid.csv"]
    assert result["renamed"] == [{"from": "Look22_color_pattern01_grid.csv",
                                  "to": "Look22_color_pattern01-2_grid.csv"}]
    assert result["refused"] == [] and result["skipped"] == []
    # The original is untouched.
    assert (workspace.files / "Look22_color_pattern01_grid.csv"
            ).read_text(encoding="utf-8") == first
    # A third copy numbers again.
    third = workspace.intake(_names([
        ("Look22_color_pattern01_grid.csv", GRID.replace("0x03", "0x04"))]))
    assert third["saved"] == ["Look22_color_pattern01-3_grid.csv"]


def test_intake_numbers_within_one_pick_as_well_as_against_the_disk(workspace):
    # Two files of ONE pick that resolve to the same name: the second is
    # kept beside the first, not silently swallowed by it.
    result = workspace.intake(_names([
        ("Look22_color_new_grid.csv", GRID),
        ("Look22_color_new_grid.csv", GRID.replace("0x03", "0x02"))]))
    assert result["saved"] == ["Look22_color_new_grid.csv",
                               "Look22_color_new-2_grid.csv"]
    assert result["renamed"] == [{"from": "Look22_color_new_grid.csv",
                                  "to": "Look22_color_new-2_grid.csv"}]


def test_intake_numbers_the_sites_own_HW_name_too(workspace):
    workspace.save("Look22_1_HW.csv", GRID)
    result = workspace.intake(_names([
        ("Look22_1_HW.csv", GRID.replace("0x03", "0x02"))]))
    assert result["saved"] == ["Look22_1-2_HW.csv"]


def test_intake_skips_the_same_file_picked_twice(workspace):
    # The same bytes under the same name is the same file, not a second
    # design: it is set aside as "already there" instead of becoming a
    # "-2" nobody asked for. The line endings it travelled under do not
    # make it a different file.
    result = workspace.intake(_names([
        ("Look22_color_pattern01_grid.csv", GRID.replace("\n", "\r\n"))]))
    assert result["saved"] == [] and result["renamed"] == []
    assert result["skipped"] == [{"from": "Look22_color_pattern01_grid.csv",
                                  "as": "Look22_color_pattern01_grid.csv"}]
    assert sorted(p.name for p in workspace.files.glob("*.csv")) == [
        "Look22_color_pattern01_grid.csv", "Look22_map.csv"]


def test_intake_refuses_a_second_wiring_file_rather_than_numbering_it(workspace):
    # A garment has ONE map: there is nowhere to put a "-2" that would
    # still be that garment's wiring, so a different one is refused with
    # what to do about it instead of overwriting the original.
    other = MAP.replace(",20,5,020-05", ",21,5,021-05")
    result = workspace.intake(_names([("Look22_map.csv", other)]))
    assert result["saved"] == []
    assert len(result["refused"]) == 1
    assert "one wiring file" in result["refused"][0]
    assert "delete it first" in result["refused"][0]
    assert (workspace.files / "Look22_map.csv").read_text(
        encoding="utf-8") == MAP
    # ...but the SAME map picked again is simply "already there".
    assert workspace.intake(_names([("Look22_map.csv", MAP)]))["skipped"] == [
        {"from": "Look22_map.csv", "as": "Look22_map.csv"}]


def test_intake_reads_safaris_csv_txt_as_the_csv_it_is(workspace):
    # Safari appends ".txt" to a text/plain download and Finder hides the
    # extension, so nobody sees why every CSV was refused (2026-09-25).
    result = workspace.intake(_names([
        ("Look22_color_pattern07_grid.csv.txt", GRID)]))
    assert result["saved"] == ["Look22_color_pattern07_grid.csv"]
    assert result["renamed"] == [{"from": "Look22_color_pattern07_grid.csv.txt",
                                  "to": "Look22_color_pattern07_grid.csv"}]


def test_intake_turns_away_the_clutter_a_mac_puts_beside_a_file(workspace):
    # "._NAME.csv" AppleDouble twins END in _map.csv and used to be taken
    # for garments of their own; the refusal says what they are.
    picked = [("._Look22_map.csv", MAP), (".DS_Store", "x"),
              ("__MACOSX/Look22_color_pattern09_grid.csv", GRID)]
    result = workspace.intake(_names(picked))
    assert result["saved"] == []
    assert len(result["refused"]) == 3
    assert all("macOS metadata" in line for line in result["refused"])
    assert all(line.split(":")[0] == name.split(":")[0]
               for line, (name, _) in zip(result["refused"], picked))
    assert sorted(p.name for p in workspace.files.glob("*.csv")) == [
        "Look22_color_pattern01_grid.csv", "Look22_map.csv"]


def test_intake_still_refuses_what_save_refuses_and_says_which_file(workspace):
    result = workspace.intake(_names([
        ("notes.csv", "x"), ("sub/Look22_map.csv", MAP),
        ("Look22_a:b_HW.csv", GRID)]))
    assert result["saved"] == []
    assert "notes.csv: " in result["refused"][0]
    assert "path separator" in result["refused"][1]
    assert "Windows keeps it" in result["refused"][2]


def test_intake_reports_a_file_with_no_text_instead_of_throwing(workspace):
    result = workspace.intake([{"name": "Look22_color_x_grid.csv"}])
    assert result["saved"] == [] and len(result["refused"]) == 1


def test_a_garments_own_add_csv_renames_a_design_onto_it(workspace):
    # A design CSV belongs to the garment its name begins with, so two
    # garments of the same shape come back from the designer under the
    # same file names - added through ONE garment's Add CSV, a file is
    # renamed onto it.
    result = workspace.intake(_names([
        ("AZ271SD1305_color_pattern07_grid.csv", GRID),
        ("AZ271SD1305_9_HW.csv", GRID)]), item="Look22")
    assert result["saved"] == ["Look22_color_pattern07_grid.csv",
                               "Look22_9_HW.csv"]
    assert result["renamed"] == [
        {"from": "AZ271SD1305_color_pattern07_grid.csv",
         "to": "Look22_color_pattern07_grid.csv"},
        {"from": "AZ271SD1305_9_HW.csv", "to": "Look22_9_HW.csv"}]
    # ...and the garment the file was NAMED after gets nothing.
    assert not list(workspace.files.glob("AZ271SD1305*"))


def test_another_garments_map_is_never_renamed_onto_this_one(workspace):
    # The simulator's own adversarial review F1: renaming another
    # garment's map onto this item replaced this garment's wiring with
    # another garment's, threw the original away, and reported it as a
    # success - the hundreds of CHECK problems that followed were the
    # only hint.
    workspace.save("Skirt_map.csv", SKIRT_MAP)
    other = SKIRT_MAP
    result = workspace.intake(_names([("Skirt_map.csv", other)]),
                              item="Look22")
    assert result["saved"] == []
    assert "another garment's map" in result["refused"][0]
    assert "Add CSV" in result["refused"][0]
    assert (workspace.files / "Look22_map.csv").read_text(
        encoding="utf-8") == MAP
    # A map of a garment this workspace does NOT have is the ordinary
    # rename case - and then runs into "a garment has one wiring file".
    stranger = workspace.intake(_names([("Nobody_map.csv", SKIRT_MAP)]),
                                item="Look22")
    assert stranger["saved"] == []
    assert "one wiring file" in stranger["refused"][0]


def test_a_per_item_pick_is_numbered_rather_than_overwriting(workspace):
    # 2026-09-26, "the 4th of 5 CSVs was overwritten": this is the very
    # path it happened on.
    result = workspace.intake(_names([
        ("Whatever_color_pattern01_grid.csv", GRID.replace("0x03", "0x02"))]),
        item="Look22")
    assert result["saved"] == ["Look22_color_pattern01-2_grid.csv"]
    assert (workspace.files / "Look22_color_pattern01_grid.csv").read_text(
        encoding="utf-8") == GRID


def test_a_per_item_pick_for_a_garment_that_is_not_here_takes_nothing(workspace):
    result = workspace.intake(_names([("Look22_color_x_grid.csv", GRID)]),
                              item="NoSuchLook")
    assert result["saved"] == [] and len(result["refused"]) == 1
    assert "no garment of that name" in result["refused"][0]


def test_a_per_item_pick_sniffs_an_unnamed_csv_onto_that_garment(workspace):
    # The whole point of the per-item button: a designer's file called
    # anything at all still lands on the garment they pressed it on.
    result = workspace.intake(_names([("summer.csv", GRID)]), item="Look22")
    assert result["saved"] == ["Look22_color_summer_grid.csv"]
    # A header that is neither is still refused, whatever it is called.
    assert workspace.intake(_names([("summer2.csv", "a,b\n1,2\n")]),
                            item="Look22")["saved"] == []


def test_two_intakes_at_once_cannot_land_on_one_file(workspace):
    """Two tabs, two operators, or a drop fired twice: the server is a
    ThreadingHTTPServer, so two /api/files requests really do run at the
    same time. Deciding which names are free and then writing without the
    lock let both pick the same free name and one clobber the other -
    25 runs out of 25 (review of dbed7d5)."""
    import threading

    grids = [GRID.replace("0x03", f"0x0{n}") for n in range(1, 9)]
    results = [None] * len(grids)

    def go(n):
        results[n] = workspace.intake(_names([
            ("Look22_color_race_grid.csv", grids[n])]))

    threads = [threading.Thread(target=go, args=(n,)) for n in range(len(grids))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    saved = [name for result in results for name in result["saved"]]
    assert len(saved) == len(grids), results
    # Every one of them under a name of its own...
    assert len(set(saved)) == len(grids), saved
    # ...and every one of them still on disk with its own bytes.
    on_disk = {p.read_text(encoding="utf-8")
               for p in workspace.files.glob("Look22_color_race*")}
    assert on_disk == set(grids)


def test_a_name_that_differs_only_in_case_is_the_same_file_here(workspace):
    """The operator's PC is Windows and NTFS cannot tell these apart, so
    neither may intake() - exact-case bookkeeping against a filesystem
    that folds meant the second file simply replaced the first, with the
    reply reporting a success (review of dbed7d5)."""
    changed = GRID.replace("0x03", "0x02")
    result = workspace.intake(_names([
        ("Look22_color_PATTERN01_grid.csv", changed)]))
    assert result["saved"] == ["Look22_color_PATTERN01-2_grid.csv"], result
    assert (workspace.files / "Look22_color_pattern01_grid.csv").read_text(
        encoding="utf-8") == GRID
    # The same bytes under a differently-cased name is still "already
    # there" - _existing_text() has always opened the case-insensitive
    # match, so the two halves used to disagree.
    again = workspace.intake(_names([("LOOK22_color_pattern01_GRID.csv", GRID)]))
    assert again["saved"] == []
    assert again["skipped"] == [{"from": "LOOK22_color_pattern01_GRID.csv",
                                 "as": "Look22_color_pattern01_grid.csv"}]
    # ...and a differently-cased MAP cannot slip past "one wiring file".
    other = MAP.replace(",20,5,020-05", ",21,5,021-05")
    refused = workspace.intake(_names([("look22_MAP.csv", other)]))
    assert refused["saved"] == [], refused
    assert "one wiring file" in refused["refused"][0]
    assert (workspace.files / "Look22_map.csv").read_text(
        encoding="utf-8") == MAP


def test_a_shouted_extension_is_written_back_in_lower_case(workspace):
    # "X.CSV" is X.csv to Windows and to the operator, but not to the
    # units' own glob("*.csv") - it would sit on top of the real file
    # here and be invisible there.
    result = workspace.intake(_names([("Look22_color_shout_grid.CSV", GRID)]))
    assert result["saved"] == ["Look22_color_shout_grid.csv"]


def test_numbering_never_steals_a_name_another_picked_file_wants(workspace):
    """A pick holding both a new "A_grid" and a genuine "A-2_grid": the
    new one lands on an occupied name and numbers, and used to take the
    real A-2_grid's name with it, pushing that file to "A-2-2_grid"
    (review of dbed7d5). Order-independent, so both orders are checked."""
    workspace.save("Look22_color_A_grid.csv", GRID)
    new_a = GRID.replace("0x03", "0x02")
    real_a2 = GRID.replace("0x03", "0x04")
    pick = [("Look22_color_A_grid.csv", new_a),
            ("Look22_color_A-2_grid.csv", real_a2)]
    result = workspace.intake(_names(pick))
    assert set(result["saved"]) == {"Look22_color_A-3_grid.csv",
                                    "Look22_color_A-2_grid.csv"}, result
    # The genuine A-2 is on disk under its own name, with its own bytes.
    assert (workspace.files / "Look22_color_A-2_grid.csv").read_text(
        encoding="utf-8") == real_a2
    # The other way round gives the same answer.
    for name in ("Look22_color_A-2_grid.csv", "Look22_color_A-3_grid.csv"):
        (workspace.files / name).unlink()
    other_way = workspace.intake(_names(list(reversed(pick))))
    assert set(other_way["saved"]) == {"Look22_color_A-3_grid.csv",
                                       "Look22_color_A-2_grid.csv"}, other_way
    assert (workspace.files / "Look22_color_A-2_grid.csv").read_text(
        encoding="utf-8") == real_a2


def test_a_file_with_no_design_name_is_refused_not_called_design(workspace):
    # "…_color_design_grid.csv" is a design nobody can find again, and
    # every such file in one pick would land on the same name.
    workspace.save("Look22-B_map.csv", MAP)
    result = workspace.intake(_names([("Look22-B.csv", GRID)]))
    assert result["saved"] == []
    assert "no design name" in result["refused"][0], result


def test_an_entry_that_is_not_a_file_is_refused_not_a_traceback(workspace):
    result = workspace.intake(["Look22_color_x_grid.csv", None, 7])
    assert result["saved"] == []
    assert len(result["refused"]) == 3
    assert all("{name, text}" in line for line in result["refused"])


def test_a_malformed_files_body_is_a_400_not_a_500(tmp_path):
    server = make_server(tmp_path, port=0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def post(body):
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/files",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    try:
        status, payload = post({"files": "Look22_map.csv"})
        assert status == 400 and "files" in payload["error"], payload
        status, payload = post({"files": ["Look22_map.csv"]})
        assert status == 200, payload
        assert payload["saved"] == [] and len(payload["refused"]) == 1
        status, payload = post({"files": [], "item": 7})
        assert status == 400 and "item" in payload["error"], payload
    finally:
        server.shutdown()
        server.server_close()


def test_import_bundle_still_overwrites_designs_and_reports_it(tmp_path):
    # The one path that DOES replace the DESIGNS it names, on purpose
    # (docs/SIMULATOR_FOR_DESIGNERS.md): a bundle is the designers'
    # project, and the operator asked for it. Nothing about the intake
    # rules above may change that.
    ws = Workspace(tmp_path / "ws")
    ws.save("Look22_map.csv", MAP)
    ws.save("Look22_color_pattern01_grid.csv", GRID)
    changed = GRID.replace("0x03", "0x02")
    result = ws.import_bundle({
        "format": "epaper-show-bundle", "version": 1,
        "files": {"Look22_color_pattern01_grid.csv": changed},
        "show": {"format": "epaper-show", "version": 1, "duration": 600,
                 "cues": []}})
    assert result["saved"] == ["Look22_color_pattern01_grid.csv"]
    assert result["overwritten"] == ["Look22_color_pattern01_grid.csv"]
    assert (ws.files / "Look22_color_pattern01_grid.csv").read_text(
        encoding="utf-8") == changed
    assert not list(ws.files.glob("*-2_grid.csv"))


def test_import_bundle_keeps_the_wiring_this_workspace_already_has(tmp_path):
    # ...and the one thing it does NOT replace (2026-09-27): the wiring is
    # the operator's, regenerated from the 配線ナビ when the site changes a
    # garment. A bundle carries whatever copy the designers started from,
    # and one of them put a stale AZ271SD1307_map.csv back over that
    # morning's board 150 without a word. The rule is now the same as
    # intake()'s: a garment has one wiring file, and replacing it is
    # deliberate (Delete, then Add CSV).
    ws = Workspace(tmp_path / "ws")
    current = MAP.replace("front,1,2,17,60,017-60", "front,1,2,17,59,017-59")
    ws.save("Look22_map.csv", current)
    result = ws.import_bundle({
        "format": "epaper-show-bundle", "version": 1,
        "files": {"Look22_map.csv": MAP,        # the designers' stale copy
                  "Look22_color_pattern01_grid.csv": GRID},
        "show": {"format": "epaper-show", "version": 1, "duration": 600,
                 "cues": []}})
    assert result["kept"] == [
        {"name": "Look22_map.csv",
         "why": "the workspace's wiring is kept (the bundle's copy differs)"}]
    assert (ws.files / "Look22_map.csv").read_text(encoding="utf-8") == current
    # The design in the same bundle arrived all the same.
    assert result["saved"] == ["Look22_color_pattern01_grid.csv"]


def test_designer_named_files_are_accepted_and_labelled(workspace):
    name = "Look22_color_ref_multicolor_redorange_s22_grid_A-1.csv"
    assert Workspace.kind(name) == "grid"
    # The production site's own "HW 用 CSV" name is a grid too, as of
    # 2026-09-26 - it used to be refused and had to be renamed by hand.
    assert Workspace.kind("AZ271SD1305_ref_multicolor_redorange_s22_HW.csv") == "grid"
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
    # "at" IS Start; the 2 s sweep runs past the 8 s refresh (7 s repaint
    # + 2 s sweep = 9 s), so it is what Complete follows.
    assert (b["sent"], b["complete"]) == (60, 60 + 7 + 2)
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
    # What a unit is told (timeline.panel_refresh): the show's own refresh,
    # floored at one physical repaint, so its guard STOP cannot land inside
    # a repaint or a sweep.
    assert [q["refresh_s"] for q in cues] == [8.0, 8.0, 8.0]
    assert set(cues[0]["delays"]) == {"1", "2", "3"}
    assert struct.unpack(">64H", bytes.fromhex(cues[0]["delays"]["1"])) == (0xFFFF,) * 64
    swept = struct.unpack(">64H", bytes.fromhex(cues[1]["delays"]["1"]))   # board 17: sockets 1, 60
    assert swept[1] == 0 and swept[60] == 0                 # row 1 is the top row
    row0 = struct.unpack(">64H", bytes.fromhex(cues[1]["delays"]["3"]))    # board 20, row 0
    assert row0[5] == 200                                    # 2.0 s = 200 frames of 10 ms
    # No sweep anywhere: still a table per board, all NO_DELAY - the unit
    # writes it as "forget the sweep" into the slot, so a board keeps no
    # table from an earlier upload that had one (review F5, 2026-09-25).
    workspace.set_timeline(600, [{"id": "a", "item": "Look22", "at": 0, "design": grid}])
    shows, _ = workspace.compile_show()
    plain = shows["radxa-01"]["cues"][0]
    assert set(plain["delays"]) == {"1", "2", "3"}
    assert all(struct.unpack(">64H", bytes.fromhex(h)) == (0xFFFF,) * 64
               for h in plain["delays"].values())


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


# ---- the designers' simulator, built on demand ----

# The generated object literal, not the bare identifier: designer-app.js
# reads globalThis.SIM.embeddedMusic, so the word itself is in EVERY build.
EMBED_MARK = b"embeddedMusic: {"

def test_simulator_downloads_lean_and_with_the_shows_music(tmp_path):
    import conductor.server as server_module
    server_module._simulator_cache.clear()
    server = make_server(tmp_path, port=0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    try:
        # No music loaded yet: music=1 still answers, with the lean page -
        # the simulator is useful silent, and the operator may simply not
        # have uploaded the track yet (the page says so in a toast).
        with urllib.request.urlopen(f"{base}/api/simulator?music=1",
                                    timeout=60) as response:
            silent = response.read()
            assert response.headers["Content-Type"] == "text/html; charset=utf-8"
            disposition = response.headers["Content-Disposition"]
        assert disposition.startswith('attachment; filename="az27ss-simulator-')
        assert "-with-music" not in disposition
        assert EMBED_MARK not in silent   # the identifier alone is in designer-app.js

        with urllib.request.urlopen(f"{base}/api/simulator", timeout=60) as response:
            lean = response.read()
        assert lean == silent

        data = (b"ID3" + bytes(range(256))) * 200            # ~50 KB
        upload = urllib.request.Request(
            f"{base}/api/music", data=data,
            headers={"X-File-Name": "AZ 27SS.DEMO.mp3"})
        with urllib.request.urlopen(upload, timeout=10) as response:
            assert json.loads(response.read())["ok"]

        started = time.monotonic()
        with urllib.request.urlopen(f"{base}/api/simulator?music=1",
                                    timeout=60) as response:
            with_music = response.read()
            disposition = response.headers["Content-Disposition"]
        first_build = time.monotonic() - started
        stamp = time.strftime("%Y%m%d")
        assert disposition == ('attachment; filename='
                               f'"az27ss-simulator-{stamp}-with-music.html"')
        assert EMBED_MARK in with_music
        assert b'name: "AZ 27SS.DEMO.mp3"' in with_music
        assert f"size: {len(data)}".encode() in with_music
        assert base64.b64encode(data) in with_music
        # Bigger than the lean page by at least the base64 of the audio.
        assert len(with_music) > len(lean) + len(data)

        # A second click is served from the cache, not rebuilt: keyed on
        # the music's name/size/mtime, none of which changed.
        started = time.monotonic()
        with urllib.request.urlopen(f"{base}/api/simulator?music=1",
                                    timeout=60) as response:
            again = response.read()
        cached = time.monotonic() - started
        assert again == with_music
        assert len(server_module._simulator_cache) == 2      # lean + this one
        assert cached <= max(first_build, 0.05), \
            f"second build took {cached:.3f}s vs {first_build:.3f}s - not cached"

        # ...and the lean page is still the lean page afterwards.
        with urllib.request.urlopen(f"{base}/api/simulator", timeout=60) as response:
            assert response.read() == lean
    finally:
        server.shutdown()
        server.server_close()


def test_simulator_notices_a_same_name_same_size_swap_in_one_second(tmp_path):
    """int(st_mtime) was not enough of a key.

    Re-exporting a mix under the same name at the same size and dropping
    it in within the same second - one drag and drop, not a contrived
    case - used to leave the cache key unchanged, so the operator handed
    the director's team the OLD track under the new name and nothing
    anywhere said so."""
    import conductor.server as server_module
    server_module._simulator_cache.clear()
    server = make_server(tmp_path, port=0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    try:
        first, second = b"A" * 4096, b"B" * 4096       # same name, same size
        assert len(first) == len(second)
        pages = []
        for payload in (first, second):
            upload = urllib.request.Request(f"{base}/api/music", data=payload,
                                            headers={"X-File-Name": "mix.mp3"})
            with urllib.request.urlopen(upload, timeout=10) as response:
                assert json.loads(response.read())["ok"]
            with urllib.request.urlopen(f"{base}/api/simulator?music=1",
                                        timeout=60) as response:
                pages.append(response.read())
        assert base64.b64encode(first) in pages[0]
        assert base64.b64encode(second) in pages[1]
        assert base64.b64encode(first) not in pages[1], \
            "the second download still carried the first track"
    finally:
        server.shutdown()
        server.server_close()


def test_a_failed_simulator_build_is_a_json_error_not_a_download(tmp_path):
    """The page fetches this before saving anything, so a failure has to
    be readable. It used to be a 500 whose JSON body the browser saved as
    az27ss-simulator-….html: a file that looks like the simulator, opens
    blank, and says nothing about what went wrong."""
    import conductor.server as server_module
    server_module._simulator_cache.clear()
    kept = server_module.DESIGNER_SOURCE
    server_module.DESIGNER_SOURCE = tmp_path / "not-a-page.html"
    server = make_server(tmp_path, port=0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/simulator",
                                   timeout=30)
        assert caught.value.code == 500
        assert caught.value.headers["Content-Type"].startswith("application/json")
        assert "Content-Disposition" not in caught.value.headers
        # The cause alone - the page adds "Could not build the simulator:"
        # in front of it, and both saying it read as a stutter.
        error = json.loads(caught.value.read())["error"]
        assert "Error" in error and "could not build" not in error.lower()
    finally:
        server_module.DESIGNER_SOURCE = kept
        server_module._simulator_cache.clear()
        server.shutdown()
        server.server_close()


def test_empty_music_is_refused(tmp_path):
    # A name in show.json with no audio under it is what makes the
    # designers' simulator claim a built-in track and then play nothing.
    server = make_server(tmp_path, port=0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    try:
        upload = urllib.request.Request(f"{base}/api/music", data=b"",
                                        headers={"X-File-Name": "empty.mp3"})
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(upload, timeout=5)
        assert caught.value.code == 400
        assert "empty" in json.loads(caught.value.read())["error"]
        state = json.loads(urllib.request.urlopen(f"{base}/api/state",
                                                  timeout=5).read())
        assert state["music"] is None
    finally:
        server.shutdown()
        server.server_close()
    # Directly too - save_music() has callers that never go through HTTP.
    ws = Workspace(tmp_path / "direct")
    with pytest.raises(ValueError):
        ws.save_music("empty.mp3", io.BytesIO(b""), 0)
    assert ws.music_info() is None


def test_simulator_rebuilds_when_the_music_is_replaced(tmp_path):
    """The whole reason this lives in the Conductor: "the music changed"
    must be answered by pressing the button again, not by a developer."""
    import conductor.server as server_module
    server_module._simulator_cache.clear()
    server = make_server(tmp_path, port=0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    try:
        for name, payload in [("first.mp3", b"AAAA" * 64), ("second.mp3", b"BBBB" * 64)]:
            upload = urllib.request.Request(f"{base}/api/music", data=payload,
                                            headers={"X-File-Name": name})
            with urllib.request.urlopen(upload, timeout=10) as response:
                assert json.loads(response.read())["ok"]
            with urllib.request.urlopen(f"{base}/api/simulator?music=1",
                                        timeout=60) as response:
                page = response.read()
            assert f'name: "{name}"'.encode() in page
            assert base64.b64encode(payload) in page
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


def test_a_show_saved_at_the_old_seven_second_refresh_keeps_working(workspace):
    """2026-09-26: the default refresh became 8 s, the effect included. A
    show.json still at 7.0 is never rewritten behind the operator's back -
    it keeps its own value, a swept cue reaches 8 s by the max rule, and
    the state carries the current default so the page can offer its hint."""
    grid = "Look22_color_pattern01_grid.csv"
    legacy = {"units": {"Look22": "radxa-01"}, "duration": 600,
              "refresh_s": 7.0,
              "transitions": {grid: {"sequence": "top_down", "span_s": 1.0}},
              "cues": [{"id": "a", "item": "Look22", "at": 0, "design": grid},
                       {"id": "b", "item": "Look22", "at": 60, "design": grid}]}
    (workspace.root / "show.json").write_text(json.dumps(legacy),
                                              encoding="utf-8")
    show = workspace.state()["show"]
    assert show["refresh_s"] == 7.0              # kept, not migrated
    assert show["refresh_default"] == 8.0        # what the hint compares to
    # A swept cue completes at 7 s repaint + 1 s sweep = 8 s...
    second = show["cues"][1]
    assert second["span"] == 1.0 and second["complete"] == 68.0
    assert second["problems"] == []
    # ...and the file on disk is untouched.
    assert json.loads((workspace.root / "show.json")
                      .read_text(encoding="utf-8"))["refresh_s"] == 7.0


def test_a_show_file_that_names_no_refresh_time_is_a_legacy_one(workspace):
    """A show.json from before 2026-09-26 may not name refresh_s at all, and a
    couple of hand-made workspaces name it as null. Either way it was drawn
    against the old 7.0 s: moving it silently to today's 8.0 s would change
    the operator's timing AND hide the hint that offers the change (review)."""
    grid = "Look22_color_pattern01_grid.csv"
    for stored in ({}, {"refresh_s": None}):
        legacy = dict({"units": {}, "duration": 600,
                       "cues": [{"id": "a", "item": "Look22", "at": 60,
                                 "design": grid}]}, **stored)
        (workspace.root / "show.json").write_text(json.dumps(legacy),
                                                  encoding="utf-8")
        show = workspace.state()["show"]
        assert show["refresh_s"] == 7.0, stored
        assert show["cues"][0]["complete"] == 67.0, stored
        # ...and it compiles, rather than raising on float(None).
        assert isinstance(workspace.compile_show(), tuple)

    # A workspace with NO show.json is a new show and opens on today's default.
    (workspace.root / "show.json").unlink()
    assert workspace.state()["show"]["refresh_s"] == 8.0
    # ...and the first thing written names it, so this version never leaves a
    # file the next load would mistake for a legacy one.
    workspace.assign("Look22", "radxa-01")
    assert json.loads((workspace.root / "show.json")
                      .read_text(encoding="utf-8"))["refresh_s"] == 8.0
    assert workspace.state()["show"]["refresh_s"] == 8.0


def test_a_null_refresh_means_not_given_everywhere_it_can_be_written(workspace):
    """`refresh_s: null` is what a hand-made file or a sparse PUT body carries.
    It means "I am not setting it", the same as leaving the key out - never a
    TypeError on float(None), and never today's default applied silently."""
    grid = "Look22_color_pattern01_grid.csv"
    cues = [{"id": "a", "item": "Look22", "at": 60, "design": grid}]
    workspace.set_timeline(600, cues, refresh=12.0)
    assert workspace.state()["show"]["refresh_s"] == 12.0
    # PUT /api/show passes body.get("refresh_s") straight in.
    workspace.set_timeline(300, cues, refresh=None)
    assert workspace.state()["show"]["refresh_s"] == 12.0      # kept
    assert workspace.state()["show"]["duration"] == 300
    for bad in ("fast", 0, 61, {}):
        with pytest.raises(ValueError):
            workspace.set_timeline(600, cues, refresh=bad)
    # ...and an imported show file that names it as null keeps what is here.
    workspace.import_show({"format": "epaper-show", "version": 1,
                           "refresh_s": None, "cues": cues})
    assert workspace.state()["show"]["refresh_s"] == 12.0


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
    assert cues["a"]["refresh"] == 8.0 and cues["a"]["refresh_source"] == "show"


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
    # Nothing sweeps: the cue's tables are the clearing all-NO_DELAY ones,
    # not a "sweep" of all-zero frames (every cue carries tables now).
    assert all(struct.unpack(">64H", bytes.fromhex(h)) == (0xFFFF,) * 64
               for h in shows["radxa-01"]["cues"][1]["delays"].values())


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
        encoded = urllib.parse.quote("caf*é.mp3")  # what encodeURIComponent sends
        upload = urllib.request.Request(
            f"{base}/api/music", data=b"abcde",
            headers={"X-File-Name": encoded})
        with urllib.request.urlopen(upload, timeout=5) as response:
            body = json.loads(response.read())
        # Unquoted first, so only the one character the workspace cannot
        # keep is sanitised - not every byte of the accented letter's
        # percent-encoding as well. The letter itself survives: a file
        # name may hold letters and digits of any script (2026-09-26).
        assert body["music"]["name"] == "caf_é.mp3"
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
    fleet.links = {"radxa-01": StubLink("radxa-01", "stopped")}
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

    # These test START's own gates, not the exhibition's preset stage
    # (test_conductor_exhibition.py): off, so START is main's immediate one.
    Workspace(tmp_path).set_preset_before_start(False)
    fleet = Fleet({})
    fleet.shows = {"radxa-01": {"id": "showA", "cues": [], "duration": 600}}
    fleet.links = {"radxa-01": StubLink("radxa-01", "stopped")}
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
        timeline = snap.pop("timeline")
        assert snap == {"units": [], "last_fire": None, "run": None,
                        "shows": {}, "corrections": [], "prepared": {},
                        "start_at": 0.0, "show_duration": None,
                        # EXHIBITION mode: the Loop is always an object,
                        # the speaker is null without --speaker.
                        "loop": {"on": False, "wait_s": 45, "next_in_s": None,
                                 "runs": 0, "problem": None, "min_wait_s": 0,
                                 "retrying": False, "waiting": False,
                                 "retry_in_s": None, "stored_wait_s": None},
                        "speaker": None}
        # Nothing to drive, but the page still asks the same question of
        # the workspace: what is the timeline now, and what was written.
        assert timeline["uploaded"] is None and timeline["demos"] == {}
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


# ---- STANDALONE DEMO ----

def _demo_workspace(tmp_path):
    ws = Workspace(tmp_path)
    ws.save("Look22_map.csv", MAP)
    ws.save("Look22_color_pattern01_grid.csv", GRID)
    ws.assign("Look22", "radxa-01")
    ws.set_timeline(600, [_cue("a", 0)])
    return ws


def test_write_demo_validates_the_name(tmp_path):
    from conductor.fleet import Fleet
    from conductor.server import DEMO_NAME_MESSAGE

    _demo_workspace(tmp_path)
    # A single configured (but unreachable) unit, not the ten real
    # defaults (Fleet({}) falls back to default_units() - 192.168.51.101…
    # - and the last call below has a name that passes validation, which
    # would otherwise reach the network for real (found in review)).
    server = make_server(tmp_path, port=0, fleet=Fleet({"radxa-01": "127.0.0.1:1"}))
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        # Empty (after stripping), too long (15), and not plain ASCII: one
        # message for all three, so the operator sees one clear rule.
        for name in ("", "   ", "A" * 15, "こんにちは", "デモ"):
            status, payload = _post(port, "/api/fleet/write_demo",
                                    {"name": name, "loop": False})
            assert status == 400
            assert payload["error"] == DEMO_NAME_MESSAGE
        # Exactly 14 plain-ASCII characters is fine.
        status, payload = _post(port, "/api/fleet/write_demo",
                                {"name": "A" * 14, "loop": False})
        assert status == 200
    finally:
        server.shutdown()
        server.server_close()


def test_write_demo_loop_must_be_a_json_boolean(tmp_path):
    from conductor.fleet import Fleet

    _demo_workspace(tmp_path)
    # Same reason as above: the final call's loop is omitted (reads as
    # False, not refused) and would reach the network for real.
    server = make_server(tmp_path, port=0, fleet=Fleet({"radxa-01": "127.0.0.1:1"}))
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        for loop in ("true", "false", 1, 0):
            status, payload = _post(port, "/api/fleet/write_demo",
                                    {"name": "DEMO", "loop": loop})
            assert status == 400
            assert payload["error"] == "loop must be true or false"
        # Omitted altogether reads as False, not a refusal.
        status, payload = _post(port, "/api/fleet/write_demo", {"name": "DEMO"})
        assert status == 200 and payload["name"] == "DEMO"
    finally:
        server.shutdown()
        server.server_close()


def test_write_demo_response_shape_and_name_is_upper_cased(tmp_path):
    from conductor.fleet import Fleet

    _demo_workspace(tmp_path)
    # Fleet({}) falls back to the ten default units (an empty dict is
    # falsy) - a unit this fleet was never told about at all, so
    # write_demo() reports it the same "unknown unit" way _each() gives
    # every other fleet command, with no network call for it to time out on.
    server = make_server(tmp_path, port=0, fleet=Fleet({"radxa-09": "127.0.0.1:1"}))
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        status, payload = _post(port, "/api/fleet/write_demo",
                                {"name": "demo paris", "loop": True})
        assert status == 200
        assert payload["name"] == "DEMO PARIS"
        assert payload["problems"] == []
        assert payload["units"] == {
            "radxa-01": {"ok": False, "error": "unknown unit"}}
    finally:
        server.shutdown()
        server.server_close()


def test_write_demo_name_is_trimmed_and_symbols_pass_through(tmp_path):
    # Leading/trailing whitespace is stripped; `"`, `<`, `>` are ordinary
    # printable ASCII and are not refused here - escaping them belongs to
    # whatever renders the name later (the page's esc()), not to this
    # validation.
    from conductor.fleet import Fleet

    _demo_workspace(tmp_path)
    server = make_server(tmp_path, port=0, fleet=Fleet({"radxa-09": "127.0.0.1:1"}))
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        status, payload = _post(port, "/api/fleet/write_demo",
                                {"name": '  demo "x" <y>  ', "loop": False})
        assert status == 200
        assert payload["name"] == 'DEMO "X" <Y>'
    finally:
        server.shutdown()
        server.server_close()


def test_upload_while_a_show_is_running_is_refused_unless_forced(tmp_path):
    # Every picture is written at Upload time now (the pre-burn design):
    # doing that while a show runs could rewrite a slot a unit is about
    # to trigger.
    from conductor.fleet import Fleet

    Workspace(tmp_path)                    # no map, no design, no timeline
    fleet = Fleet({"radxa-01": "127.0.0.1:1"})
    fleet.run = {"t0": 0.0, "state": "running", "held_at": None}
    server = make_server(tmp_path, port=0, fleet=fleet)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        status, payload = _post(port, "/api/fleet/upload", {})
        assert status == 400 and payload["error"] == "stop the show first"
        status, payload = _post(port, "/api/fleet/upload", {"force": True})
        assert status == 200      # goes on to compile_show() as usual
        assert payload["problems"] == ["the timeline has no cues"]
    finally:
        server.shutdown()
        server.server_close()


def test_write_demo_is_blocked_the_same_way_upload_is(tmp_path):
    from conductor.fleet import Fleet

    Workspace(tmp_path)                    # no map, no design, no timeline
    # compile_show() returns no shows here (a problem, "no cues"), so
    # write_demo() is never even called - Fleet({}) would be harmless in
    # this one test, but a non-default fleet costs nothing and reads the
    # same as its neighbours.
    server = make_server(tmp_path, port=0, fleet=Fleet({"radxa-01": "127.0.0.1:1"}))
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        status, payload = _post(port, "/api/fleet/write_demo",
                                {"name": "DEMO", "loop": False})
        assert status == 200
        assert payload["units"] == {}
        assert payload["problems"] == ["the timeline has no cues"]
    finally:
        server.shutdown()
        server.server_close()


def test_write_demo_is_refused_while_the_show_runs(tmp_path):
    # Saving a demo writes every picture, exactly as Upload does - during
    # a run that would rewrite slots a unit is about to trigger. Unlike
    # Upload there is no `force`: a demo is never the way back into a
    # running show.
    from conductor.fleet import Fleet

    _demo_workspace(tmp_path)
    fleet = Fleet({"radxa-01": "127.0.0.1:1"})
    fleet.run = {"t0": 0.0, "state": "running", "held_at": None}
    server = make_server(tmp_path, port=0, fleet=fleet)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        status, payload = _post(port, "/api/fleet/write_demo",
                                {"name": "DEMO", "loop": False})
        assert status == 400 and payload["error"] == "stop the show first"
        status, payload = _post(port, "/api/fleet/write_demo",
                                {"name": "DEMO", "loop": False, "force": True})
        assert status == 400 and payload["error"] == "stop the show first"
        # The name is still checked first, so a run never hides a typo.
        status, payload = _post(port, "/api/fleet/write_demo", {"name": ""})
        assert status == 400 and payload["error"] != "stop the show first"
    finally:
        server.shutdown()
        server.server_close()


def test_the_fleet_snapshot_carries_what_each_unit_holds_in_its_menu(tmp_path):
    # The tiles and the "Write to units…" chips are drawn from /api/fleet
    # alone: every unit's stored demos ride along with its tile, already
    # marked against the show this conductor uploaded to it.
    from conductor.fleet import Fleet
    from tests.test_fleet import StubLink

    _demo_workspace(tmp_path)
    holder = StubLink("radxa-01", "stopped")
    holder.demos = [{"slug": "demo-paris", "name": "DEMO PARIS", "cues": 4,
                     "duration": 90.0, "loop": True, "saved_at": 1.0,
                     "show_id": "showA"},
                    {"slug": "demo-old", "name": "DEMO OLD", "cues": 4,
                     "duration": 90.0, "loop": False, "saved_at": 2.0,
                     "show_id": "an-older-hash"}]
    never_asked = StubLink("radxa-02", "stopped")
    fleet = Fleet({})
    fleet.links = {"radxa-01": holder, "radxa-02": never_asked}
    fleet.shows = {"radxa-01": {"id": "showA", "cues": [], "duration": 90}}
    fleet._poll_demos(holder)
    server = make_server(tmp_path, port=0, fleet=fleet)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        units = {u["name"]: u for u in _get(port, "/api/fleet")["units"]}
        assert [(d["name"], d["cues"], d["duration"], d["loop"], d["current"])
                for d in units["radxa-01"]["demos"]] == [
            ("DEMO PARIS", 4, 90.0, True, True),
            ("DEMO OLD", 4, 90.0, False, False)]
        # Not asked yet is "not known" (null), never "no demo stored".
        assert units["radxa-02"]["demos"] is None
    finally:
        server.shutdown()
        server.server_close()


def test_the_snapshot_says_whether_the_timeline_moved_since_it_was_written(tmp_path):
    # The chips' "up to date" must not be a tautology: the ids the units
    # report are the ids this conductor sent them, so they agree with
    # themselves however much the timeline has been edited since. The
    # workspace's own revision is what tells the two apart - and it lives
    # on the server, so a page reload does not forget it.
    from conductor.fleet import Fleet
    from tests.test_fleet import StubLink

    ws = _demo_workspace(tmp_path)
    fleet = Fleet({})
    fleet.links = {"radxa-01": StubLink("radxa-01", "stopped")}
    server = make_server(tmp_path, port=0, fleet=fleet)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        before = _get(port, "/api/fleet")["timeline"]
        assert before["uploaded"] is None and before["demos"] == {}
        assert before["revision"]

        status, _ = _post(port, "/api/fleet/upload", {})
        assert status == 200
        after = _get(port, "/api/fleet")["timeline"]
        assert after["uploaded"] == after["revision"] == before["revision"]

        status, payload = _post(port, "/api/fleet/write_demo",
                                {"name": "paris ss26", "loop": False})
        assert status == 200 and payload["units"]["radxa-01"]["ok"]
        saved = _get(port, "/api/fleet")["timeline"]
        assert saved["demos"] == {"PARIS SS26": saved["revision"]}

        # One cue moved: everything on the units is now older than what
        # the operator is looking at, and stays that way for every later
        # request (this is the server's memory, not the page's).
        ws.set_timeline(600, [_cue("a", 0), _cue("b", 30)])
        edited = _get(port, "/api/fleet")["timeline"]
        assert edited["revision"] != after["revision"]
        assert edited["uploaded"] == after["revision"]
        assert edited["demos"] == {"PARIS SS26": after["revision"]}
        assert _get(port, "/api/fleet")["timeline"] == edited
    finally:
        server.shutdown()
        server.server_close()


def test_an_edit_during_the_write_belongs_to_the_next_one(tmp_path):
    # Writing ten units takes seconds. A cue moved while that is in
    # flight was never sent to anybody - recording the revision as of
    # the END of the write would have the chips call the units up to
    # date with a timeline they have never seen (found in review).
    from conductor.fleet import Fleet
    from tests.test_fleet import StubLink

    ws = _demo_workspace(tmp_path)
    fleet = Fleet({})
    fleet.links = {"radxa-01": StubLink("radxa-01", "stopped")}
    server = make_server(tmp_path, port=0, fleet=fleet)
    workspace = server.RequestHandlerClass.workspace
    compile_for_write = workspace.compile_for_write

    def edit_while_writing(only=None):
        compiled = compile_for_write(only)
        ws.set_timeline(600, [_cue("a", 0), _cue("b", 45)])   # the operator
        return compiled

    workspace.compile_for_write = edit_while_writing
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        before = _get(port, "/api/fleet")["timeline"]["revision"]
        assert _post(port, "/api/fleet/upload", {})[0] == 200
        timeline = _get(port, "/api/fleet")["timeline"]
        # The upload carried the timeline as it was when it started.
        assert timeline["uploaded"] == before
        # And the one on screen has moved on since: changed, not current.
        assert timeline["revision"] != before
    finally:
        workspace.compile_for_write = compile_for_write
        server.shutdown()
        server.server_close()


def test_the_revision_ignores_what_never_reaches_a_unit(tmp_path):
    # Music and the LOOK / model labels are the operator's own notes
    # about the show; loading a track or renaming a look must not turn
    # every chip red (found in review).
    ws = _demo_workspace(tmp_path)
    quiet = ws.revision()
    ws.save_music("track.mp3", io.BytesIO(b"ID3 and then some bytes"), 22)
    assert ws.music_info()["name"] == "track.mp3"
    assert ws.revision() == quiet
    ws.set_label("Look22", look="22", model="AZ271SD1305")
    assert ws.revision() == quiet
    # A moved cue is the other kind of change entirely.
    ws.set_timeline(600, [_cue("a", 0), _cue("b", 60)])
    assert ws.revision() != quiet


def test_a_write_that_reached_nobody_is_not_remembered_as_written(tmp_path):
    # Marking the timeline as "what the units hold" when not one of them
    # took it would have the chip say "up to date" about nothing.
    from conductor.fleet import Fleet

    _demo_workspace(tmp_path)
    # A configured unit that does not exist: every command comes back
    # failed, without a network call of its own.
    fleet = Fleet({"radxa-09": "127.0.0.1:1"})
    server = make_server(tmp_path, port=0, fleet=fleet)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        status, payload = _post(port, "/api/fleet/upload", {})
        assert status == 200
        assert payload["units"]["radxa-01"] == {"ok": False, "error": "unknown unit"}
        timeline = _get(port, "/api/fleet")["timeline"]
        assert timeline["uploaded"] is None and timeline["demos"] == {}
        assert timeline["uploaded_units"] == {}
    finally:
        server.shutdown()
        server.server_close()


# ---- "Which LOOKs": writing one look's units and not the whole fleet ----

def _two_unit_workspace(tmp_path):
    """Two garments on two units - the smallest timeline where writing
    one LOOK and leaving the other alone means anything."""
    ws = Workspace(tmp_path)
    ws.save("Look22_map.csv", MAP)
    ws.save("Look22_color_pattern01_grid.csv", GRID)
    ws.save("Look20-Skirt_map.csv", SKIRT_MAP)
    ws.save("Look20-Skirt_color_pattern01_grid.csv", SKIRT_GRID)
    ws.assign("Look22", "radxa-01")
    ws.assign("Look20-Skirt", "radxa-02")
    ws.set_timeline(600, [_cue("a", 0), _skirt_cue("b", 0)])
    return ws


def _skirt_cue(id_, at):
    return {"id": id_, "item": "Look20-Skirt", "at": at,
            "design": "Look20-Skirt_color_pattern01_grid.csv"}


def _two_unit_server(tmp_path):
    from conductor.fleet import Fleet

    fleet = Fleet({})
    fleet.links = {"radxa-01": StubLink("radxa-01", "stopped"),
                   "radxa-02": StubLink("radxa-02", "stopped")}
    server = make_server(tmp_path, port=0, fleet=fleet)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, fleet


def test_upload_writes_only_the_units_of_the_chosen_look(tmp_path):
    # The dialog's "Which LOOKs" radio: one look, for checking it, and
    # the other unit is not posted to at all.
    _two_unit_workspace(tmp_path)
    server, fleet = _two_unit_server(tmp_path)
    port = server.server_address[1]
    try:
        status, payload = _post(port, "/api/fleet/upload",
                                {"units": ["radxa-01"]})
        assert status == 200
        assert list(payload["units"]) == ["radxa-01"]
        assert payload["units"]["radxa-01"]["ok"]
        # Both units are still named in `shows` - that is the whole
        # timeline, which is what the page's "1 / 2" counts against.
        assert sorted(payload["shows"]) == ["radxa-01", "radxa-02"]
        assert [p for p, _ in fleet.links["radxa-01"].posted] == ["/show/load"]
        assert fleet.links["radxa-02"].posted == []
        # And this conductor knows only radxa-01 holds a show of its own.
        assert list(fleet.shows) == ["radxa-01"]
    finally:
        server.shutdown()
        server.server_close()


def test_a_write_refuses_a_unit_that_is_not_in_this_timeline(tmp_path):
    # A page showing a timeline the workspace has moved on from must not
    # be able to write a LOOK to nothing and be told it worked.
    _two_unit_workspace(tmp_path)
    server, fleet = _two_unit_server(tmp_path)
    port = server.server_address[1]
    try:
        for path in ("/api/fleet/upload", "/api/fleet/write_demo"):
            body = {"units": ["radxa-07"], "name": "PARIS"}
            status, payload = _post(port, path, body)
            assert status == 400, path
            assert payload["error"] == "radxa-07 is not a unit of this timeline"
            # Nothing at all went out - not even to the units that ARE in it.
            assert all(not link.posted for link in fleet.links.values())
        # The shape is checked too: not a list, and an empty one.
        status, payload = _post(port, "/api/fleet/upload", {"units": "radxa-01"})
        assert status == 400 and payload["error"] == "units must be a list of unit names"
        status, payload = _post(port, "/api/fleet/upload", {"units": [7]})
        assert status == 400 and payload["error"] == "units must be a list of unit names"
        status, payload = _post(port, "/api/fleet/upload", {"units": []})
        assert status == 400 and "pick a LOOK" in payload["error"]
        assert all(not link.posted for link in fleet.links.values())
    finally:
        server.shutdown()
        server.server_close()


def test_one_looks_upload_does_not_mark_the_whole_timeline_as_uploaded(tmp_path):
    # The chip exists to answer "is what I see on the units?". After one
    # LOOK went out, the answer for the fleet is no - so there is no
    # fleet-wide mark at all, only the per-unit one that says who does
    # hold this revision. "uploaded 1/2", never "2/2 · up to date".
    _two_unit_workspace(tmp_path)
    server, _ = _two_unit_server(tmp_path)
    port = server.server_address[1]
    try:
        rev = _get(port, "/api/fleet")["timeline"]["revision"]
        assert _post(port, "/api/fleet/upload", {"units": ["radxa-01"]})[0] == 200
        timeline = _get(port, "/api/fleet")["timeline"]
        assert timeline["uploaded"] is None
        assert timeline["uploaded_units"] == {"radxa-01": rev}
        # The second unit, written on its own straight after: the fleet
        # now holds the same revision everywhere, so the fleet-wide mark
        # comes back without a full upload having been asked for.
        assert _post(port, "/api/fleet/upload", {"units": ["radxa-02"]})[0] == 200
        timeline = _get(port, "/api/fleet")["timeline"]
        assert timeline["uploaded"] == rev
        assert timeline["uploaded_units"] == {"radxa-01": rev, "radxa-02": rev}
    finally:
        server.shutdown()
        server.server_close()


def test_one_looks_upload_after_a_full_one_takes_the_fleet_mark_away(tmp_path):
    # The dangerous order: everything was uploaded, then the timeline was
    # edited and one LOOK re-written. The fleet-wide mark must not be
    # left pointing at the old revision as if it were current, nor be
    # moved to the new one - one unit holds it, the other does not.
    ws = _two_unit_workspace(tmp_path)
    server, _ = _two_unit_server(tmp_path)
    port = server.server_address[1]
    try:
        first = _get(port, "/api/fleet")["timeline"]["revision"]
        assert _post(port, "/api/fleet/upload", {})[0] == 200
        assert _get(port, "/api/fleet")["timeline"]["uploaded"] == first
        # The operator edits - both units are still in the timeline.
        ws.set_timeline(600, [_cue("a", 30), _skirt_cue("b", 0)])
        second = _get(port, "/api/fleet")["timeline"]["revision"]
        assert second != first
        assert _post(port, "/api/fleet/upload", {"units": ["radxa-01"]})[0] == 200
        timeline = _get(port, "/api/fleet")["timeline"]
        assert timeline["uploaded"] is None
        assert timeline["uploaded_units"] == {"radxa-01": second,
                                              "radxa-02": first}
    finally:
        server.shutdown()
        server.server_close()


def _refused_upload(payload):
    """Did START / PRESET refuse because the fleet is not all on one
    upload? (Any other refusal - a unit that never burned its pictures,
    an offline one - is a different gate and not this test's business.)"""
    return "before the show" in (payload.get("error") or "")


def test_start_refuses_a_fleet_split_over_two_uploads(tmp_path):
    # The rule the dialog prints under a one-LOOK Upload - "START needs
    # every unit of the timeline to hold this upload" - is a rule only
    # here. Nothing downstream can catch it: a unit's show id is the id
    # THIS conductor gave it, so the burn gate's id check matches happily
    # for a unit still holding last hour's show (review F1).
    ws = _two_unit_workspace(tmp_path)
    server, fleet = _two_unit_server(tmp_path)
    port = server.server_address[1]
    try:
        assert _post(port, "/api/fleet/upload", {})[0] == 200
        # Everyone holds what is on screen: START is not this gate's
        # business (it goes on to the units, which is where it fails
        # here - the stubs hold no pictures).
        status, payload = _post(port, "/api/fleet/start", {"lead_s": 3})
        assert not _refused_upload(payload)

        ws.set_timeline(600, [_cue("a", 30), _skirt_cue("b", 0)])
        assert _post(port, "/api/fleet/upload", {"units": ["radxa-01"]})[0] == 200
        for command in ("start", "preset"):
            status, payload = _post(port, f"/api/fleet/{command}", {"lead_s": 3})
            assert status == 400, command
            assert payload["error"] == ("radxa-02 is not on this upload - "
                                        "Upload for All LOOKs before the show")
        # This gate has an answer of its own. The burn gate's `force` -
        # which the page may already have asked for, about failed boards -
        # does NOT get past it: one "yes" must never answer two questions
        # the operator was only asked one of (review N1).
        for command in ("start", "preset"):
            status, payload = _post(port, f"/api/fleet/{command}",
                                    {"lead_s": 3, "force": True})
            assert status == 400 and _refused_upload(payload), command
        # `split_ok` is that answer. (It then meets the ordinary burn
        # gate, which is a different question with a different sentence.)
        status, payload = _post(port, "/api/fleet/start",
                                {"lead_s": 3, "split_ok": True})
        assert not _refused_upload(payload)
        # ... and does not answer the burn gate either: these units hold
        # no pictures at all, which split_ok has nothing to say about.
        assert "has not taken this show yet" in (payload.get("error") or "")
    finally:
        server.shutdown()
        server.server_close()


def test_start_refuses_when_a_unit_of_the_timeline_was_never_uploaded(tmp_path):
    # The first thing that happens on a fresh conductor: one LOOK is
    # uploaded to check it, and the other unit has nothing of this show
    # at all. fleet.shows does not even mention it - it is the TIMELINE
    # that says the show needs it (Workspace.timeline_units, read out of
    # show.json without compiling anything).
    _two_unit_workspace(tmp_path)
    server, fleet = _two_unit_server(tmp_path)
    port = server.server_address[1]
    try:
        assert _post(port, "/api/fleet/upload", {"units": ["radxa-01"]})[0] == 200
        assert list(fleet.shows) == ["radxa-01"]
        status, payload = _post(port, "/api/fleet/start", {"lead_s": 3})
        assert status == 400
        assert payload["error"] == ("radxa-02 is not on this upload - "
                                    "Upload for All LOOKs before the show")
        # Nothing was started - the one unit that does hold it included.
        assert fleet.run is None
        assert _post(port, "/api/fleet/upload", {})[0] == 200
        status, payload = _post(port, "/api/fleet/start", {"lead_s": 3})
        assert not _refused_upload(payload)
    finally:
        server.shutdown()
        server.server_close()


def test_start_refuses_a_fleet_that_is_a_whole_timeline_behind(tmp_path):
    # Everyone agrees with everyone, and all of them are older than what
    # the operator is looking at: the ordinary "edited and forgot to
    # upload". The revision ignores labels and music, so this is always
    # a real change to the cues, the CSVs or the units.
    ws = _two_unit_workspace(tmp_path)
    server, _ = _two_unit_server(tmp_path)
    port = server.server_address[1]
    try:
        assert _post(port, "/api/fleet/upload", {})[0] == 200
        ws.set_label("Look22", look="22", model="AZ271SD1305")
        status, payload = _post(port, "/api/fleet/start", {"lead_s": 3})
        assert not _refused_upload(payload)  # a label
        ws.set_timeline(600, [_cue("a", 45), _skirt_cue("b", 0)])
        status, payload = _post(port, "/api/fleet/start", {"lead_s": 3})
        assert status == 400
        assert payload["error"] == ("every unit holds an older upload than "
                                    "the timeline on screen - Upload again "
                                    "before the show")
    finally:
        server.shutdown()
        server.server_close()


def test_the_gate_does_not_say_upload_again_about_a_timeline_that_cannot_be_uploaded(tmp_path):
    # "Upload again" is no use when an Upload could not happen - the
    # operator would press it, watch it refuse, and be none the wiser
    # (review N2). Read off the last compile, so START pays nothing.
    ws = _two_unit_workspace(tmp_path)
    server, _ = _two_unit_server(tmp_path)
    port = server.server_address[1]
    try:
        assert _post(port, "/api/fleet/upload", {})[0] == 200
        # A cue pointing at a design that is not in the workspace: the
        # same problem the Timeline tab shows, and compile_show() builds
        # nothing at all while it is there.
        ws.set_timeline(600, [_cue("a", 0), _skirt_cue("b", 0),
                              {"id": "c", "item": "Look22", "at": 45,
                               "design": "Look22_color_pattern09_grid.csv"}])
        status, payload = _post(port, "/api/fleet/upload", {})
        assert status == 200 and payload["problems"] and not payload["shows"]
        status, payload = _post(port, "/api/fleet/start", {"lead_s": 3})
        assert status == 400
        assert payload["error"] == ("the timeline has problems - fix them "
                                    "on the Timeline tab, then Upload")
        # Fixed: the ordinary sentence is back.
        ws.set_timeline(600, [_cue("a", 45), _skirt_cue("b", 0)])
        status, payload = _post(port, "/api/fleet/start", {"lead_s": 3})
        assert status == 400 and "Upload again before the show" in payload["error"]
    finally:
        server.shutdown()
        server.server_close()


def test_start_refuses_after_a_one_look_upload_that_left_a_garment_off_its_unit(tmp_path):
    """The hole the unit counting cannot see (found in review, 2026-09-27).

    A garment with cues and no unit is in nobody's marks and in
    `timeline_units()` either, so after a one-LOOK upload of the garments
    that DO have units there is nothing "missing" and nothing "behind" -
    the fleet agrees with itself about a show that is missing a dress.
    The timeline's own problems have to be read BEFORE that count, not
    after it."""
    ws = Workspace(tmp_path)
    ws.save("Look22_map.csv", MAP)
    ws.save("Look22_color_pattern01_grid.csv", GRID)
    ws.save("Look20-Skirt_map.csv", SKIRT_MAP)
    ws.save("Look20-Skirt_color_pattern01_grid.csv", SKIRT_GRID)
    ws.assign("Look22", "radxa-01")             # ...and the skirt has none
    ws.set_timeline(600, [_cue("a", 0), _skirt_cue("b", 0)])
    server, fleet = _two_unit_server(tmp_path)
    port = server.server_address[1]
    try:
        status, payload = _post(port, "/api/fleet/upload",
                                {"units": ["radxa-01"]})
        assert status == 200 and payload["units"]["radxa-01"]["ok"]
        assert payload["warnings"] == ["Look20-Skirt: not assigned to a unit"]
        # radxa-01 holds exactly what is on screen, and it is the only
        # unit the timeline names - the count comes out clean.
        assert ws.timeline_units() == {"radxa-01"}
        for command in ("start", "preset"):
            status, payload = _post(port, f"/api/fleet/{command}",
                                    {"lead_s": 3})
            assert status == 400, command
            assert payload["error"] == ("the timeline has problems - fix them "
                                        "on the Timeline tab, then Upload"), command
        assert fleet.run is None
        # The burn gate's `force` is not an answer to this one, as it is
        # not an answer to the split (review N1): one "yes" never answers
        # a question the operator was not asked.
        status, payload = _post(port, "/api/fleet/start",
                                {"lead_s": 3, "force": True})
        assert status == 400
        assert payload["error"].startswith("the timeline has problems")
        # `split_ok` does get past it - as it always has, for every
        # timeline this gate refuses: it skips the gate whole, and what
        # is left is the burn gate (these stubs hold no pictures).
        status, payload = _post(port, "/api/fleet/start",
                                {"lead_s": 3, "split_ok": True})
        assert status == 400 and "has not taken this show yet" in payload["error"]
        # Give the skirt a unit and the gate goes back to counting units:
        # radxa-02 was never written at all, and giving it the skirt moved
        # the timeline on past what radxa-01 holds.
        ws.assign("Look20-Skirt", "radxa-02")
        status, payload = _post(port, "/api/fleet/start", {"lead_s": 3})
        assert status == 400
        assert payload["error"] == ("radxa-02, radxa-01 are not on this "
                                    "upload - Upload for All LOOKs before "
                                    "the show")
    finally:
        server.shutdown()
        server.server_close()


def test_a_conductor_that_knows_nothing_does_not_refuse_start(tmp_path):
    # Restarted mid-show: it has no marks of its own and must not refuse
    # on a guess - the units are running and it has just adopted them.
    _two_unit_workspace(tmp_path)
    server, fleet = _two_unit_server(tmp_path)
    port = server.server_address[1]
    try:
        fleet.shows = {"radxa-01": {"id": "showA", "cues": [], "duration": 600},
                       "radxa-02": {"id": "showA", "cues": [], "duration": 600}}
        status, payload = _post(port, "/api/fleet/start", {"lead_s": 3})
        assert not _refused_upload(payload)
        status, payload = _post(port, "/api/fleet/preset", {})
        assert not _refused_upload(payload)
    finally:
        server.shutdown()
        server.server_close()


def test_a_unit_taken_out_of_the_timeline_stops_holding_start_up(tmp_path):
    # Its mark would otherwise sit in the workspace for ever and refuse
    # START for a garment that left the show weeks ago (review F7).
    ws = _two_unit_workspace(tmp_path)
    server, _ = _two_unit_server(tmp_path)
    port = server.server_address[1]
    workspace = server.RequestHandlerClass.workspace
    try:
        assert _post(port, "/api/fleet/upload", {})[0] == 200
        ws.set_timeline(600, [_cue("a", 0)])            # the skirt is out
        assert _post(port, "/api/fleet/upload", {})[0] == 200
        assert list(workspace.unit_marks["upload"]) == ["radxa-01"]
        status, payload = _post(port, "/api/fleet/start", {"lead_s": 3})
        assert not _refused_upload(payload)
    finally:
        server.shutdown()
        server.server_close()


def test_deleting_a_demo_forgets_what_was_written_under_that_name(tmp_path):
    # The name is free again: a mark left behind would have the next demo
    # written under it inherit an "up to date" it never earned (F7).
    _two_unit_workspace(tmp_path)
    server, _ = _two_unit_server(tmp_path)
    port = server.server_address[1]
    try:
        assert _post(port, "/api/fleet/write_demo",
                     {"name": "paris ss26", "loop": False})[0] == 200
        timeline = _get(port, "/api/fleet")["timeline"]
        assert list(timeline["demos"]) == ["PARIS SS26"]
        assert list(timeline["demo_units"]) == ["PARIS SS26"]
        assert _post(port, "/api/fleet/delete_demo",
                     {"slug": "paris-ss26"})[0] == 200
        timeline = _get(port, "/api/fleet")["timeline"]
        assert timeline["demos"] == {} and timeline["demo_units"] == {}
    finally:
        server.shutdown()
        server.server_close()


def test_one_looks_demo_is_saved_on_that_unit_alone(tmp_path):
    _two_unit_workspace(tmp_path)
    server, fleet = _two_unit_server(tmp_path)
    port = server.server_address[1]
    try:
        rev = _get(port, "/api/fleet")["timeline"]["revision"]
        status, payload = _post(port, "/api/fleet/write_demo",
                                {"name": "paris", "loop": False,
                                 "units": ["radxa-02"]})
        assert status == 200 and list(payload["units"]) == ["radxa-02"]
        assert [p for p, _ in fleet.links["radxa-02"].posted] == ["/demo/save"]
        assert fleet.links["radxa-01"].posted == []
        timeline = _get(port, "/api/fleet")["timeline"]
        assert timeline["demos"] == {}          # not the whole fleet's
        assert timeline["demo_units"] == {"PARIS": {"radxa-02": rev}}
    finally:
        server.shutdown()
        server.server_close()


def test_the_workspace_revision_follows_the_csvs_as_well_as_the_timeline(tmp_path):
    # compile_show() reads both, so both have to move the revision - a
    # redrawn design is as much a reason to upload again as a moved cue.
    ws = _demo_workspace(tmp_path)
    first = ws.revision()
    assert ws.revision() == first            # nothing changed: the same
    ws.save("Look22_color_pattern02_grid.csv", GRID)
    assert ws.revision() != first


def test_delete_demo_validates_the_slug(tmp_path):
    from conductor.fleet import Fleet

    # A single configured unit that was never polled (UnitLink.online is
    # False until a poll sets last_seen) - delete_demo() only posts to
    # online links, so a valid slug never even reaches the network here.
    server = make_server(tmp_path, port=0, fleet=Fleet({"radxa-01": "127.0.0.1:1"}))
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        for bad in ("", "   ", "UPPER", "-leading", "has spaces", 'has"quote'):
            status, payload = _post(port, "/api/fleet/delete_demo",
                                    {"slug": bad})
            assert status == 400 and payload["error"] == "bad demo id"
        status, payload = _post(port, "/api/fleet/delete_demo",
                                {"slug": "demo-paris"})
        assert status == 200
        # Never online: skipped, not a real (and pointless) connection.
        assert payload["units"] == {
            "radxa-01": {"ok": False, "error": "offline"}}
    finally:
        server.shutdown()
        server.server_close()


def _get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
        return json.loads(r.read())


def test_get_fleet_demos_with_no_fleet_configured(tmp_path):
    server = make_server(tmp_path, port=0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert _get(port, "/api/fleet/demos") == {
            "units": {}, "offline": [], "failed": {}}
    finally:
        server.shutdown()
        server.server_close()


def test_get_fleet_demos_falls_back_to_compiling_when_nothing_was_uploaded(tmp_path):
    # fleet.shows is empty (nothing uploaded this session) - the only
    # reference left is compiling the timeline right now.
    from conductor.fleet import Fleet
    from tests.test_fleet import StubLink

    ws = _demo_workspace(tmp_path)
    shows, problems = ws.compile_show()
    assert problems == []
    current_id = shows["radxa-01"]["id"]
    online = StubLink("radxa-01", "stopped")
    online.demos = [
        {"slug": "demo-a", "name": "DEMO A", "cues": 1, "duration": 600,
         "loop": False, "saved_at": 1, "show_id": current_id},
        {"slug": "demo-b", "name": "DEMO B", "cues": 1, "duration": 600,
         "loop": False, "saved_at": 2, "show_id": "an-older-hash"},
    ]
    offline = StubLink("radxa-02", "stopped")
    offline.online = False
    fleet = Fleet({})
    fleet.links = {"radxa-01": online, "radxa-02": offline}
    assert fleet.shows == {}
    server = make_server(tmp_path, port=0, fleet=fleet)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        payload = _get(port, "/api/fleet/demos")
        demos = {d["slug"]: d for d in payload["units"]["radxa-01"]}
        assert demos["demo-a"]["current"] is True
        assert demos["demo-b"]["current"] is False
        assert payload["offline"] == ["radxa-02"]
        assert payload["failed"] == {}
    finally:
        server.shutdown()
        server.server_close()


def test_get_fleet_demos_prefers_what_was_actually_uploaded(tmp_path):
    # fleet.shows (a live upload) is cheap and already in memory - it is
    # used ahead of compiling the timeline again, and can disagree with
    # what compile_show() would say right now (the timeline moved on
    # since that upload).
    from conductor.fleet import Fleet
    from tests.test_fleet import StubLink

    _demo_workspace(tmp_path)
    online = StubLink("radxa-01", "stopped")
    online.demos = [{"slug": "demo-a", "name": "DEMO A", "cues": 1,
                     "duration": 600, "loop": False, "saved_at": 1,
                     "show_id": "uploaded-hash"}]
    fleet = Fleet({})
    fleet.links = {"radxa-01": online}
    fleet.shows = {"radxa-01": {"id": "uploaded-hash", "cues": [],
                                "duration": 600}}
    server = make_server(tmp_path, port=0, fleet=fleet)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        payload = _get(port, "/api/fleet/demos")
        assert payload["units"]["radxa-01"][0]["current"] is True
    finally:
        server.shutdown()
        server.server_close()


def test_get_fleet_demos_shows_a_dash_not_older_when_there_is_no_reference(tmp_path):
    # No upload this session AND the timeline has a problem (compile_show()
    # returns no shows) - "current" must read null (the page's "—"), never
    # False ("older"), for every demo: there is nothing to compare against.
    from conductor.fleet import Fleet
    from tests.test_fleet import StubLink

    Workspace(tmp_path)                    # no map, no design, no timeline
    online = StubLink("radxa-01", "stopped")
    online.demos = [{"slug": "demo-a", "name": "DEMO A", "cues": 1,
                     "duration": 600, "loop": False, "saved_at": 1,
                     "show_id": "some-hash"}]
    fleet = Fleet({})
    fleet.links = {"radxa-01": online}
    server = make_server(tmp_path, port=0, fleet=fleet)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        payload = _get(port, "/api/fleet/demos")
        assert payload["units"]["radxa-01"][0]["current"] is None
    finally:
        server.shutdown()
        server.server_close()


def test_get_fleet_demos_separates_offline_from_a_unit_that_answered_with_an_error(tmp_path):
    # An older agent with no /demo/list at all answers, just not with
    # 200 - that unit is not "offline" (it is right there on the LAN),
    # it "failed", with its own message.
    from conductor.fleet import Fleet
    from tests.test_fleet import StubLink

    class OldAgentLink(StubLink):
        def get(self, path, learn=True, timeout=None):
            raise RuntimeError("HTTP 404: not found")

    fleet = Fleet({})
    fleet.links = {"radxa-01": OldAgentLink("radxa-01", "stopped")}
    server = make_server(tmp_path, port=0, fleet=fleet)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        payload = _get(port, "/api/fleet/demos")
        assert payload["offline"] == []
        assert payload["failed"] == {"radxa-01": "HTTP 404: not found"}
        assert payload["units"] == {}
    finally:
        server.shutdown()
        server.server_close()



# ---- the page itself: "Write to units…" ----
#
# The page is one file of vanilla JS served as it is, so what it offers can
# be read straight out of it. These are not a substitute for looking at it
# (that is the browser check in the commit message) - they are the guard
# that an id a handler talks to, or the words that tell the operator which
# of the two ways they are choosing, do not quietly disappear in an edit.

@pytest.fixture
def page(tmp_path):
    server = make_server(tmp_path, port=0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as r:
            yield r.read().decode("utf-8")
    finally:
        server.shutdown()
        server.server_close()


def test_the_page_opens_the_write_dialog_from_all_three_doors(page):
    # The Timeline toolbar, THE SHOW's ④, and the demo card - the whole
    # point of the round: one obvious entry point, reachable from wherever
    # the operator happens to be standing.
    for opener in ('id="tl-write"', 'id="show-write"', 'id="demo-open"'):
        assert opener in page
    assert "Write to units…" in page
    assert "④ Save on units (plays without this PC)" in page
    assert "PLAY WITHOUT THIS PC (DEMO STORED ON THE UNITS)" in page
    # The card that hid the feature is gone by that name, and so is the
    # name field it used to carry (it lives in the dialog now).
    assert "<h2>STANDALONE DEMO</h2>" not in page
    assert 'id="demo-name"' not in page


def test_the_page_dialog_is_two_choices_with_their_consequence(page):
    for part in ('id="write-back"', 'id="write-dialog"', 'id="write-choice-upload"',
                 'id="write-choice-demo"', 'id="write-upload"', 'id="write-demo"',
                 'id="write-name"', 'id="write-loop"', 'id="write-close"',
                 'id="write-upload-why"', 'id="write-demo-why"', 'id="write-progress"'):
        assert part in page, part
    assert "Upload for the show" in page and "Save on the units" in page
    assert "This PC then runs the show" in page
    assert "The unit plays it from KEY1 with no PC at all" in page
    # The estimate is the measured one, per picture (docs/STATUS.md).
    assert "const BURN_S_PER_PICTURE = 0.31;" in page
    assert "on the slowest unit," in page
    # The steps on the unit, after a demo is written - and the unit has
    # no DEMO submenu: a demo is a row in the menu, right below STANDBY
    # (ui/app.py), which is what the operator has to look for.
    assert "KEY2</b> opens the menu" in page and "KEY1</b> plays it" in page
    assert "(just below STANDBY)" in page and "<b>DEMO</b>" not in page
    # The name rule is the server's own, said while it is being typed.
    assert "A-Z, 0-9 and symbols only, up to 14 characters." in page
    assert r"const DEMO_NAME_OK = /^[\x20-\x7e]+$/;" in page
    assert "the unit's LCD cannot display Japanese." in page
    assert "the unit's menu row fits ${DEMO_NAME_MAX}." in page
    # A result heading never claims more than happened, and a unit that
    # is not answering is said before the write, not only after it.
    assert "function writeOutcome(result)" in page
    assert 'ok: out.all' in page and "Nothing was uploaded" in page
    assert "No unit is answering" in page and 'id="write-upload-note"' in page
    # The modal owns the keyboard and the backdrop while it is open.
    assert 'body.busy #write-back { pointer-events: none; }' in page
    assert 'if (e.key !== "Tab") return;' in page
    assert "if (commandInFlight) return;" in page
    # Every reason a choice cannot be taken says so where the choice is.
    for why in ("The timeline has no cues yet", "fix them on the Timeline tab",
                "The show is running — press STOP first.",
                "playing a demo", "Type a name for the demo first."):
        assert why in page, why


def test_the_page_dialog_can_write_one_looks_units(page):
    # The radio group at the top: All LOOKs by default, then one row per
    # LOOK the timeline reaches, in the order the LOOKS AT row uses.
    for part in ('id="write-only"', 'id="write-only-h"', 'id="write-only-all"',
                 'name="write-only"', 'role="radiogroup"',
                 'id="write-upload-only"', 'id="write-demo-only"'):
        assert part in page, part
    assert "Which LOOKs" in page and "All LOOKs — ${all} unit" in page
    assert "function writeRows()" in page and "const groups = lookGroups();" in page
    # A row names its LOOK, its garments, its units and its wait; one with
    # no unit is listed and disabled rather than hidden.
    for part in ("<b>LOOK ${esc(r.look)}</b>", "esc(r.units.join(\", \"))",
                 "picture${r.pictures === 1 ? \"\" : \"s\"}", "≈ ${r.seconds} s",
                 '"no unit"', "r.why ? \" disabled\" : \"\""):
        assert part in page, part
    # Both choices say what a one-LOOK write leaves behind - and the
    # Upload one says what it means for START.
    assert "START needs every unit of the timeline to hold this upload" in page
    assert "use this for checking one look, then Upload for all before the show." in page
    assert "the other units keep theirs." in page
    # What goes to the server, and the question asked during a run.
    assert "s.only ? { units: s.only } : {}" in page
    assert "const uploadDuringRunQuestion = (seconds, only) =>" in page
    assert 'only ? only.join(", ") : "every unit"' in page
    # A LOOK that has gone from the timeline is refused, never widened
    # back out to the whole fleet behind the operator's back.
    assert "The LOOK you picked is no longer in the timeline" in page
    assert "ui.writeOnly = null;" in page       # every time the dialog opens
    # Which units hold what is on screen, from the server's per-unit mark.
    assert "function unitHoldsRevision(unit)" in page
    assert "mark.uploaded_units || {}" in page
    assert "· has this upload" in page and "· older upload" in page
    # The units NOT being written stay on screen with that same marker -
    # they are why START refuses afterwards (review F8).
    assert '"write-left-upload"' in page and '"write-left-demo"' in page
    assert "Left as they are:" in page and "function writeLeftHtml(s, id)" in page
    # Only units the compiled show actually has are sent (review F4).
    assert "row.targets.map(t => t.unit)" in page
    assert "no cue yet" in page
    # A unit is written its whole show, not one look of it.
    assert "the unit's other looks ride along" in page
    # After a one-LOOK write the choice goes back to All LOOKs, and the
    # result says what is still to do (review F6).
    assert "if (s.only) ui.writeOnly = null;" in page
    assert 'id="write-next"' in page
    assert "START refuses a fleet split over two uploads" in page
    # ... which is a real refusal, and one the operator can still override
    # on purpose (conductor/server.py's _one_timeline).
    assert "const splitUpload = error =>" in page
    assert "two different timelines at the same time." in page
    # Each gate has its own question and its own answer: the burn gate's
    # `force` never answers the split one (review N1).
    assert "const answers = { force: again, split_ok: false };" in page
    assert "answers.split_ok = true;" in page
    assert "{ lead_s: lead, ...answers }" in page


def test_the_page_chips_say_what_the_units_hold(page):
    assert 'class="unit-chips"' in page and "function writeChipsHtml()" in page
    for words in ("not uploaded", "no demo", "uploaded <b>",
                  "· up to date", "· changed since", "<span>On unit</span>"):
        assert words in page, words
    # The chips read the snapshot's own fields - the ids the units report
    # against the ids this conductor wrote (units[].demos, /api/fleet shows)
    # AND the workspace revision behind them, which is the only thing that
    # can see the timeline having been edited since it was written.
    assert "u.demos" in page and "fleet.shows" in page
    assert "fleet?.timeline" in page and "h.mark.uploaded" in page
    # The verdict word is droppable at a narrow window, so the chips stay
    # on one row in the Timeline dock's head.
    assert 'class="verdict"' in page and "@media (max-width: 1280px)" in page

# ---- "Clear pictures after the show" (show.json's clear_after_show) ----
# 2026-09-27: a garment unplugged with its boards still on battery restarted
# the factory autoplay and cycled slots 0-18 - it replayed the show's
# pictures on its own. The checkbox is per SHOW, because it is a property of
# the evening rather than of whoever's browser is open.

def test_the_clear_after_show_setting_is_off_until_it_is_asked_for(workspace):
    assert workspace.state()["show"]["clear_after_show"] is False
    workspace.set_clear_after_show(True)
    assert workspace.state()["show"]["clear_after_show"] is True
    # ...and surviving a restart is the whole point of storing it here.
    again = Workspace(workspace.root)
    assert again.state()["show"]["clear_after_show"] is True
    # Off writes no key at all: a show.json that never mentions it behaves
    # exactly as every one written before this did.
    again.set_clear_after_show(False)
    stored = json.loads((workspace.root / "show.json").read_text(encoding="utf-8"))
    assert "clear_after_show" not in stored


def test_the_clear_after_show_setting_is_undoable(workspace):
    workspace.set_timeline(600, [_cue("a", 0)])
    workspace.set_clear_after_show(True)
    assert workspace.undo() is True
    assert workspace.state()["show"]["clear_after_show"] is False
    assert workspace.state()["show"]["cues"]                # the timeline stays
    assert workspace.redo() is True
    assert workspace.state()["show"]["clear_after_show"] is True
    # Setting it to what it already is is not a step of its own.
    depth = workspace.state()["history"]["undo"]
    workspace.set_clear_after_show(True)
    assert workspace.state()["history"]["undo"] == depth


def test_ticking_the_box_does_not_make_the_units_look_out_of_date(workspace):
    # Nothing about the pictures changes, and the show keeps its id, so a
    # re-Upload would rewrite nothing - and asking for one, on ten tiles, the
    # evening of the show, is worse than the unit's own fallback copy of the
    # flag being one Upload behind (the conductor sends the clear itself).
    grid = "Look22_color_pattern01_grid.csv"
    workspace.set_timeline(600, [{"id": "a", "item": "Look22", "at": 0,
                                  "design": grid}])
    before = workspace.revision()
    workspace.set_clear_after_show(True)
    assert workspace.revision() == before
    # ...and the show files keep their identity, which is what the tiles and
    # the supervision compare.
    workspace.assign("Look22", "radxa-01")
    shows = workspace.compile_show()[0]
    workspace.set_clear_after_show(False)
    assert workspace.compile_show()[0]["radxa-01"]["id"] == shows["radxa-01"]["id"]


def test_the_clear_after_show_setting_refuses_anything_but_a_bool(workspace):
    for junk in ("yes", 1, None, {}):
        with pytest.raises(ValueError, match="must be true or false"):
            workspace.set_clear_after_show(junk)


def test_export_and_import_carry_the_clear_after_show_setting(workspace):
    grid = "Look22_color_pattern01_grid.csv"
    workspace.set_timeline(600, [{"id": "a", "item": "Look22", "at": 0,
                                  "design": grid}])
    workspace.set_clear_after_show(True)
    exported = workspace.export_show()
    assert exported["clear_after_show"] is True
    other = Workspace(workspace.root.parent / "other")
    other.import_show(exported)
    assert other.state()["show"]["clear_after_show"] is True
    # A show file that says nothing about it leaves this workspace's own
    # answer alone - refresh_s's rule, and what every file written before
    # this version is.
    exported.pop("clear_after_show")
    other.import_show(exported)
    assert other.state()["show"]["clear_after_show"] is True
    # ...and false really does turn it off.
    other.import_show(dict(exported, clear_after_show=False))
    assert other.state()["show"]["clear_after_show"] is False
    with pytest.raises(ValueError, match="clear_after_show: must be true or "
                                         "false"):
        other.import_show(dict(exported, clear_after_show="yes"))


def test_the_designers_bundle_never_touches_the_clear_after_show_setting(tmp_path):
    # The simulator has no notion of the fleet, so its bundles carry no such
    # key - and a bundle must not quietly untick the operator's box.
    ws = Workspace(tmp_path / "ws")
    ws.save("Look22_map.csv", MAP)
    ws.save("Look22_color_pattern01_grid.csv", GRID)
    ws.set_clear_after_show(True)
    ws.import_bundle({"format": "epaper-show-bundle", "version": 1,
                      "files": {}, "show": {"format": "epaper-show",
                                            "version": 1, "cues": []}})
    assert ws.state()["show"]["clear_after_show"] is True


def test_clear_after_show_over_http(tmp_path):
    server = make_server(tmp_path, port=0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def post(path, body):
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    try:
        assert post("/api/show/clear_after", {"on": True}) == (200, {"ok": True})
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state",
                                    timeout=5) as response:
            assert json.loads(response.read())["show"]["clear_after_show"] is True
        code, answer = post("/api/show/clear_after", {"on": "yes"})
        assert code == 400 and "true or false" in answer["error"]
    finally:
        server.shutdown()
        server.server_close()


# ---- "Countdown before START" (show.json's start_countdown_s) ----
# 2026-09-29, the owner: 「コンダクターのTHE SHOWについて、ショー開始までの
# カウントダウン時間を設定できるように ... -11秒スタートとなるようにして」.
# ③ START's own lead, stored with the show; NEXT / MOVE keep theirs.

def test_the_countdown_before_start_is_eleven_seconds_until_changed(workspace):
    assert START_COUNTDOWN_S == 11.0
    assert workspace.state()["show"]["start_countdown_s"] == 11.0
    assert workspace.start_countdown() == 11.0
    workspace.set_start_countdown(15)
    assert workspace.state()["show"]["start_countdown_s"] == 15.0
    # Surviving a restart (a reload of the page) is the point of storing it.
    again = Workspace(workspace.root)
    assert again.state()["show"]["start_countdown_s"] == 15.0
    # Decimal seconds are fine, to a tenth.
    again.set_start_countdown(7.44)
    assert again.start_countdown() == 7.4
    # The default writes no key: a show.json that never mentions it is 11 s.
    again.set_start_countdown(11)
    stored = json.loads((workspace.root / "show.json").read_text(encoding="utf-8"))
    assert "start_countdown_s" not in stored
    assert again.state()["show"]["start_countdown_s"] == 11.0


def test_the_countdown_is_undoable_and_no_change_is_no_step(workspace):
    workspace.set_timeline(600, [_cue("a", 0)])
    workspace.set_start_countdown(20)
    assert workspace.undo() is True
    assert workspace.state()["show"]["start_countdown_s"] == 11.0
    assert workspace.state()["show"]["cues"]                # the timeline stays
    assert workspace.redo() is True
    assert workspace.state()["show"]["start_countdown_s"] == 20.0
    depth = workspace.state()["history"]["undo"]
    workspace.set_start_countdown(20.0)
    assert workspace.state()["history"]["undo"] == depth


def test_the_countdown_refuses_anything_outside_three_to_sixty(workspace):
    for junk in (2.9, 60.1, 0, -11, "soon", None, True, {}, [], float("nan")):
        with pytest.raises(ValueError, match="start_countdown_s: 3 to 60"):
            workspace.set_start_countdown(junk)
    for fine in (3, 60, 11, "12", 4.5):
        workspace.set_start_countdown(fine)
    assert check_start_countdown("12") == 12.0
    # The page's own rule (parseSeconds): NFKC, then a plain decimal only.
    assert check_start_countdown("１１") == 11.0
    assert check_start_countdown(" 2.96 ") == 3.0
    for junk in ("0x10", "1_1", "١١", "", "inf", "11s"):
        with pytest.raises(ValueError, match="start_countdown_s: 3 to 60"):
            check_start_countdown(junk)
    # A hand-edited show.json with a broken value still counts down 11 s.
    assert start_countdown_of({"start_countdown_s": "junk"}) == 11.0
    assert start_countdown_of({}) == 11.0


def test_the_countdown_is_not_part_of_the_upload_or_the_show_id(workspace):
    # Changing it must never ask for a new Upload: it never reaches a unit.
    grid = "Look22_color_pattern01_grid.csv"
    workspace.set_timeline(600, [{"id": "a", "item": "Look22", "at": 0,
                                  "design": grid}])
    workspace.assign("Look22", "radxa-01")
    before, shows = workspace.revision(), workspace.compile_show()[0]
    workspace.set_start_countdown(25)
    assert workspace.revision() == before, "the chips would ask for an Upload"
    after = workspace.compile_show()[0]
    assert after["radxa-01"]["id"] == shows["radxa-01"]["id"]
    assert after["radxa-01"] == shows["radxa-01"]
    assert "start_countdown_s" not in json.dumps(after)


def test_export_and_import_carry_the_countdown(workspace):
    grid = "Look22_color_pattern01_grid.csv"
    workspace.set_timeline(600, [{"id": "a", "item": "Look22", "at": 0,
                                  "design": grid}])
    assert workspace.export_show()["start_countdown_s"] == 11.0
    workspace.set_start_countdown(14.5)
    exported = workspace.export_show()
    assert exported["start_countdown_s"] == 14.5
    other = Workspace(workspace.root.parent / "other")
    assert other.state()["show"]["start_countdown_s"] == 11.0
    other.import_show(exported)
    assert other.state()["show"]["start_countdown_s"] == 14.5
    # A file without it (every one written before today) leaves this
    # workspace's own countdown alone - 11 s on a fresh one.
    exported.pop("start_countdown_s")
    other.import_show(exported)
    assert other.state()["show"]["start_countdown_s"] == 14.5
    fresh = Workspace(workspace.root.parent / "fresh")
    fresh.import_show(exported)
    assert fresh.state()["show"]["start_countdown_s"] == 11.0
    # An import is undoable like any other edit.
    other.import_show(dict(exported, start_countdown_s=30))
    assert other.state()["show"]["start_countdown_s"] == 30.0
    assert other.undo() is True
    assert other.state()["show"]["start_countdown_s"] == 14.5
    with pytest.raises(ValueError, match="start_countdown_s: 3 to 60"):
        other.import_show(dict(exported, start_countdown_s=90))
    assert other.state()["show"]["start_countdown_s"] == 14.5


def test_the_default_countdown_is_never_written_as_a_key(workspace):
    # Importing an export (which always carries the value) must not leave
    # `start_countdown_s: 11.0` in show.json - on any write path (LOW-5).
    grid = "Look22_color_pattern01_grid.csv"
    workspace.set_timeline(600, [{"id": "a", "item": "Look22", "at": 0,
                                  "design": grid}])
    stored = lambda: json.loads((workspace.root / "show.json").read_text(encoding="utf-8"))
    exported = workspace.export_show()
    assert exported["start_countdown_s"] == 11.0
    # The first import also writes the export's own spelling of the other
    # keys (labels, units, dips...); the second one is the pure round trip.
    workspace.import_show(exported)
    assert "start_countdown_s" not in stored()
    exported = workspace.export_show()
    before, depth = workspace.revision(), workspace.state()["history"]["undo"]
    workspace.import_show(exported)
    assert "start_countdown_s" not in stored()
    assert workspace.revision() == before
    assert workspace.state()["history"]["undo"] == depth, "a no-op import made a step"
    # A chosen value then an import of the default: the key goes.
    workspace.set_start_countdown(20)
    assert stored()["start_countdown_s"] == 20.0
    workspace.import_show(dict(exported, start_countdown_s="11.0"))
    assert "start_countdown_s" not in stored()
    assert workspace.start_countdown() == 11.0
    # ...a bundle the same.
    workspace.set_start_countdown(20)
    workspace.import_bundle({"format": "epaper-show-bundle", "version": 1, "files": {},
                             "show": {"format": "epaper-show", "version": 1,
                                      "start_countdown_s": 11}})
    assert "start_countdown_s" not in stored()
    # ...and so does setting it back by hand.
    workspace.set_start_countdown(20)
    workspace.set_start_countdown(11.0)
    assert "start_countdown_s" not in stored()


def test_a_bundle_carries_the_countdown_only_when_it_has_one(tmp_path):
    ws = Workspace(tmp_path / "ws")
    ws.save("Look22_map.csv", MAP)
    ws.save("Look22_color_pattern01_grid.csv", GRID)
    ws.set_start_countdown(9)
    bundle = {"format": "epaper-show-bundle", "version": 1, "files": {},
              "show": {"format": "epaper-show", "version": 1, "cues": []}}
    # The designers' simulator has no fleet and no START: its bundles carry
    # no countdown, and one must not quietly reset the operator's.
    ws.import_bundle(json.loads(json.dumps(bundle)))
    assert ws.state()["show"]["start_countdown_s"] == 9.0
    carried = json.loads(json.dumps(bundle))
    carried["show"]["start_countdown_s"] = 13
    ws.import_bundle(carried)
    assert ws.state()["show"]["start_countdown_s"] == 13.0
    fresh = Workspace(tmp_path / "fresh")
    fresh.import_bundle(json.loads(json.dumps(bundle)))
    assert fresh.state()["show"]["start_countdown_s"] == 11.0


def test_the_countdown_over_http_and_start_without_a_lead(tmp_path):
    from conductor.fleet import Fleet

    # These test START's own gates, not the exhibition's preset stage
    # (test_conductor_exhibition.py): off, so START is main's immediate one.
    Workspace(tmp_path).set_preset_before_start(False)
    fleet = Fleet({})
    server = make_server(tmp_path, port=0, fleet=fleet)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def post(path, body):
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    try:
        assert post("/api/show/start_countdown", {"s": 16}) == (200, {"ok": True})
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state",
                                    timeout=5) as response:
            assert json.loads(response.read())["show"]["start_countdown_s"] == 16.0
        code, answer = post("/api/show/start_countdown", {"s": 2})
        assert code == 400 and "3 to 60" in answer["error"]
        code, answer = post("/api/show/start_countdown", {})
        assert code == 400 and "3 to 60" in answer["error"]
        # START with no lead of its own takes the show's countdown: t0 lands
        # 16 s ahead, so the position reads -0:16 and counts up to 0:00.
        fleet.shows = {"radxa-01": {"id": "x", "cues": [], "duration": 60}}
        link = StubLink("radxa-01", "stopped")
        link.status["show"]["id"] = "x"
        fleet.links = {"radxa-01": link}
        before = fleet._clock()
        code, answer = post("/api/fleet/start", {})
        assert code == 200 and answer["lead_s"] == 16.0, answer
        assert 15.5 < fleet.run["t0"] - before < 16.5
        post("/api/fleet/stop", {})
        # ...and the page's own lead_s still wins when it sends one.
        code, answer = post("/api/fleet/start", {"lead_s": 4, "force": True})
        assert code == 200 and answer["lead_s"] == 4.0, answer
        post("/api/fleet/stop", {})
        # A START from a mark is not a show opening: with no lead of its own
        # it takes the ordinary 3 s, never the countdown (review MED-1) -
        # whether the mark comes in the request or from an earlier MOVE.
        code, answer = post("/api/fleet/start", {"from_s": 20, "manual": True})
        assert code == 200 and answer["lead_s"] == 3.0 and answer["from_s"] == 20.0, answer
        post("/api/fleet/stop", {})
        fleet.start_at = 30.0
        code, answer = post("/api/fleet/start", {})
        assert code == 200 and answer["lead_s"] == 3.0 and answer["from_s"] == 30.0, answer
        post("/api/fleet/stop", {})
        # ...and an explicit from_s of 0 is the opening again.
        code, answer = post("/api/fleet/start", {"from_s": 0})
        assert code == 200 and answer["lead_s"] == 16.0, answer
        # NEXT with no lead is 3 s, as it always was.
        code, answer = post("/api/fleet/next", {})
        assert code == 200 and answer["lead_s"] == 3.0, answer
    finally:
        server.shutdown()
        server.server_close()


# ---- one LOOK is written while another garment has no unit (2026-09-27) ----
#
# The operator asked for one bag on radxa-09 while five other garments were
# between units, and the whole upload was refused with five "not assigned to
# a unit" about garments that were never going to be written. What follows
# is the workspace's own rule, then the endpoints', then the words the
# dialog says about it.

def _bag_workspace(tmp_path):
    """Two garments with cues; only one of them has a unit - the shape of
    the refusal above, at its smallest."""
    ws = Workspace(tmp_path)
    ws.save("Look22_map.csv", MAP)
    ws.save("Look22_color_pattern01_grid.csv", GRID)
    ws.save("Look20-Skirt_map.csv", SKIRT_MAP)
    ws.save("Look20-Skirt_color_pattern01_grid.csv", SKIRT_GRID)
    ws.assign("Look22", "radxa-01")             # the one garment on a Radxa
    ws.set_timeline(600, [_cue("a", 0), _skirt_cue("b", 0)])
    return ws


def test_one_looks_upload_is_not_refused_over_another_garments_problems(tmp_path):
    ws = _bag_workspace(tmp_path)
    # A full upload is unchanged: whole or not at all, and by name.
    shows, problems = ws.compile_show()
    assert shows == {} and problems == ["Look20-Skirt: not assigned to a unit"]
    # The same timeline, compiled FOR radxa-01 alone: built, with the other
    # garment reported rather than refused.
    shows, problems, warnings = ws.compile_for_write(["radxa-01"])
    assert problems == []
    assert warnings == ["Look20-Skirt: not assigned to a unit"]
    assert list(shows) == ["radxa-01"] and shows["radxa-01"]["cues"]
    # Whatever a one-LOOK compile waved through, the TIMELINE still has it -
    # which is what START reads to say "fix them, then Upload" rather than
    # sending the operator to an Upload that cannot happen.
    assert ws.compiled["problems"] == ["Look20-Skirt: not assigned to a unit"]


def test_a_targeted_units_own_problem_still_refuses_its_one_look_write(tmp_path):
    # The line is "is this problem about a unit I am writing to?", never
    # "am I writing one look only?" - a cue of the chosen LOOK that does
    # not build is exactly what this write was going to send.
    ws = _bag_workspace(tmp_path)
    ws.set_timeline(600, [
        {"id": "a", "item": "Look22", "at": 0, "design": "nosuch_grid.csv"},
        _skirt_cue("b", 0)])
    shows, problems, warnings = ws.compile_for_write(["radxa-01"])
    assert shows == {}
    assert problems == ["0:00 Look22: design nosuch_grid.csv is not loaded"]
    assert warnings == ["Look20-Skirt: not assigned to a unit"]
    # And a timeline with no cues is nobody's business in particular and
    # everybody's refusal.
    ws.set_timeline(600, [])
    assert ws.compile_for_write(["radxa-01"])[1] == ["the timeline has no cues"]


def test_the_endpoints_write_one_look_and_report_what_they_left_out(tmp_path):
    _bag_workspace(tmp_path)
    server, fleet = _two_unit_server(tmp_path)
    port = server.server_address[1]
    try:
        # All LOOKs: refused, and not one unit is posted to.
        status, payload = _post(port, "/api/fleet/upload", {})
        assert status == 200 and payload["units"] == {}
        assert payload["problems"] == ["Look20-Skirt: not assigned to a unit"]
        assert payload["warnings"] == []
        assert all(not link.posted for link in fleet.links.values())
        # The one garment that has a unit, alone: written, with the other
        # named as a warning rather than as a refusal.
        status, payload = _post(port, "/api/fleet/upload",
                                {"units": ["radxa-01"]})
        assert status == 200 and payload["problems"] == []
        assert payload["warnings"] == ["Look20-Skirt: not assigned to a unit"]
        assert payload["units"]["radxa-01"]["ok"]
        assert list(payload["shows"]) == ["radxa-01"]
        assert [p for p, _ in fleet.links["radxa-01"].posted] == ["/show/load"]
        assert fleet.links["radxa-02"].posted == []
        # A timeline with a garment still off its unit is not "on the
        # units" because one LOOK of it is: no fleet-wide mark, and the
        # per-unit one says who does hold it.
        timeline = _get(port, "/api/fleet")["timeline"]
        assert timeline["uploaded"] is None
        assert list(timeline["uploaded_units"]) == ["radxa-01"]
        # Save on the units answers the same way, with the same warning.
        status, payload = _post(port, "/api/fleet/write_demo",
                                {"name": "PARIS", "units": ["radxa-01"]})
        assert status == 200 and payload["problems"] == []
        assert payload["warnings"] == ["Look20-Skirt: not assigned to a unit"]
        assert payload["units"]["radxa-01"]["ok"]
        # A unit the timeline does not have is still refused by name, after
        # the compile rather than before it, and still writes nothing.
        status, payload = _post(port, "/api/fleet/upload",
                                {"units": ["radxa-07"]})
        assert status == 400
        assert payload["error"] == "radxa-07 is not a unit of this timeline"
    finally:
        server.shutdown()
        server.server_close()


def test_clear_pictures_is_refused_while_the_show_runs(tmp_path):
    class StubFleet:
        run = {"state": "running"}
        shows = {"radxa-01": {"id": "showA"}}

        def clear_pictures(self, only=None):
            raise AssertionError("must never be reached")

    server = make_server(tmp_path, port=0, fleet=StubFleet())
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/fleet/clear_pictures",
            data=b"{}", headers={"Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(request, timeout=5)
        assert exc.value.code == 400
        assert "stop the show first" in json.loads(exc.value.read())["error"]
    finally:
        server.shutdown()
        server.server_close()

def test_the_page_pre_check_agrees_about_one_look_and_the_rest(page):
    # The dialog must not disable a button the server would have taken:
    # its reasons are counted over the units the write goes to, exactly as
    # conductor/showfile.py's build(only=...) counts its problems.
    assert "const mine = only ? state.show.cues.filter(" in page
    assert "only.includes(itemByKey(c.item)?.unit))" in page
    assert "const bad = mine.reduce((n, c) => n + c.problems.length, 0);" in page
    # An item with cues and no unit: a refusal for All LOOKs (the server
    # refuses that too), and the way out is named in the same breath.
    assert "else if (!only && unassigned.length)" in page
    assert "cues but no unit" in page
    assert "or pick one LOOK" in page and "above to write just that one." in page
    # ...and, with one LOOK chosen, what will not be written is said under
    # the choice rather than in place of it.
    assert "function notWrittenText(s)" in page
    assert "Not written: ${s.unassigned.join(\", \")} — no unit yet" in page
    assert "give ${s.unassigned.length === 1 ? \"it\" : \"them\"} one on the Designs tab" in page
    # The server's own `warnings`, amber under the result - never counted
    # as a failure, which is what `problems` are for.
    assert "function writeWarningsHtml(r)" in page
    assert 'class="warn">not written: ${esc(w)}' in page
    assert "warnings: result.warnings || []," in page
    assert "warnings: out.warnings" in page


# ---- and the same pre-check, run: one page, no server, no fleet ----
#
# The checks above say the words are in the file. This one RUNS the page's
# own writeState() over a timeline with a garment off its unit and asks the
# operator's question: is the Upload button live? The code is lifted out of
# conductor/web/index.html by name, so the test can only ever be about the
# page that ships (tests/test_conductor_board.py lifts the show board the
# same way). Behind CONDUCTOR_BROWSER_TESTS=1 like every browser test here.

PAGE_SRC = (Path(__file__).resolve().parents[1]
            / "conductor" / "web" / "index.html").read_text(encoding="utf-8")


def _page_function(name):
    """One top-level `function name(...) {...}` of index.html, signature
    and all, found by counting braces from its first one."""
    start = PAGE_SRC.index(f"function {name}(")
    depth = 0
    for i in range(PAGE_SRC.index("{", start), len(PAGE_SRC)):
        depth += (PAGE_SRC[i] == "{") - (PAGE_SRC[i] == "}")
        if depth == 0:
            return PAGE_SRC[start:i + 1]
    raise AssertionError(f"function {name} is never closed")


def _page_const(name):
    """One top-level `const name = ...;` of index.html, on its own line."""
    for line in PAGE_SRC.splitlines():
        if line.startswith(f"const {name} = "):
            return line
    raise AssertionError(f"const {name} is gone from the page")


# Everything writeState() stands on, and nothing else.
_WRITE_LIFTED = ["itemByKey", "BURN_S_PER_PICTURE", "DEMO_NAME_MAX",
                 "DEMO_NAME_OK"], [
    "trackItems", "lookGroups", "unitPlaysADemo", "writeTargets", "writeRows",
    "writeRowByKey", "notWrittenText", "uploadOnlyText", "demoOnlyText",
    "writeState"]

_WRITE_PROBE = """<!doctype html><meta charset="utf-8"><title>write</title><body>
<script>
"use strict";
%(code)s
var state = null, fleet = null, ui = null;
var CASES = %(cases)s;
var out = { error: null, results: {} };
try {
  for (var key in CASES) {
    state = CASES[key].state; fleet = CASES[key].fleet; ui = CASES[key].ui;
    var s = writeState();
    out.results[key] = { uploadWhy: s.uploadWhy, demoWhy: s.demoWhy,
                         bad: s.bad, only: s.only, unassigned: s.unassigned,
                         onlyText: uploadOnlyText(s), demoText: demoOnlyText(s) };
  }
} catch (e) { out.error = String((e && e.stack) || e); }
var pre = document.createElement("pre");
pre.id = "write-out";
pre.textContent = JSON.stringify(out);
document.body.appendChild(pre);
</script>
"""

# The row's own title is the MODEL label; what is named as not written is
# the ITEM, the same string the server's refusal and its amber line use.
BAG, TOPS = "AZ271SG1036 Bag 02", "AZ271SB2303 Tops"
BAG_ITEM, TOPS_ITEM = "AZ271SG1036", "AZ271SB2303"
BAG_ROW = "I" + BAG_ITEM         # writeRows' key for a look-less item


def _dlg_case(only=None, tops_unit=None, bag_problems=(), tops_problems=()):
    """The 2026-09-27 timeline: a bag on its own Radxa and one other
    garment, which may or may not have a unit of its own."""
    items = [{"item": "AZ271SG1036", "model": BAG, "look": None,
              "unit": "radxa-09", "boards": [1, 2], "map": {"scales": [[0, 0, 0]]}},
             {"item": "AZ271SB2303", "model": TOPS, "look": "24",
              "unit": tops_unit, "boards": [1], "map": {"scales": [[0, 0, 0]]}}]
    cues = [{"item": "AZ271SG1036", "sent": 0, "problems": list(bag_problems)},
            {"item": "AZ271SB2303", "sent": 30, "problems": list(tops_problems)}]
    names = ["radxa-09"] + ([tops_unit] if tops_unit else [])
    return {"state": {"items": items, "show": {"cues": cues, "duration": 600}},
            "fleet": {"units": [{"name": n, "online": True} for n in names],
                      "run": None, "shows": {}},
            "ui": {"writeOnly": only, "demoName": "PARIS", "demoLoop": False}}


_DLG_CASES = {
    # The refusal the operator met, now said in the dialog instead of by
    # the server after the press - with the way out in the same sentence.
    "all_looks_one_off_its_unit": _dlg_case(),
    # The fix: that same timeline, with the bag's own row chosen.
    "one_look_off_its_unit": _dlg_case(only=BAG_ROW),
    # Nothing to say once every garment has a unit.
    "all_looks_all_assigned": _dlg_case(tops_unit="radxa-02"),
    # A cue problem on ANOTHER unit's garment is not this write's business...
    "one_look_anothers_broken_cue": _dlg_case(
        only=BAG_ROW, tops_unit="radxa-02",
        tops_problems=["design pattern09_grid.csv is not loaded"]),
    # ...but one on the chosen LOOK's own cue is exactly what it would send.
    "one_look_its_own_broken_cue": _dlg_case(
        only=BAG_ROW, tops_unit="radxa-02",
        bag_problems=["design pattern09_grid.csv is not loaded"]),
    # And a full upload is still whole or not at all.
    "all_looks_a_broken_cue": _dlg_case(
        tops_unit="radxa-02",
        tops_problems=["design pattern09_grid.csv is not loaded"]),
}


@pytest.fixture(scope="module")
def dialog_states(tmp_path_factory):
    from tests.test_designer_build import _dump_dom, _require_browser

    tmp = tmp_path_factory.mktemp("writestate")
    _require_browser(tmp)
    consts, functions = _WRITE_LIFTED
    code = "\n".join([_page_const(n) for n in consts]
                     + [_page_function(n) for n in functions])
    probe = tmp / "writestate.html"
    probe.write_text(_WRITE_PROBE % {"code": code,
                                     "cases": json.dumps(_DLG_CASES)},
                     encoding="utf-8")
    dom = _dump_dom(probe.as_uri(), tmp)
    match = re.search(r'<pre id="write-out">(.*?)</pre>', dom or "", re.S)
    assert match, f"no #write-out in the dumped DOM:\n{(dom or '')[:3000]}"
    data = json.loads(unescape(match.group(1)))
    assert data["error"] is None, data["error"]
    return data["results"]


def test_the_dialog_enables_upload_for_one_look_whose_unit_is_assigned(dialog_states):
    one = dialog_states["one_look_off_its_unit"]
    # The whole point: the button is live, for the unit that has a show.
    assert one["uploadWhy"] is None and one["demoWhy"] is None
    assert one["only"] == ["radxa-09"] and one["bad"] == 0
    # And it says what it is not writing, on both choices - by the same
    # name the server's amber line will use afterwards.
    assert one["unassigned"] == [TOPS_ITEM]
    assert f"Not written: {TOPS_ITEM} — no unit yet" in one["onlyText"]
    assert "give it one on the Designs tab before the show" in one["onlyText"]
    assert f"Not written: {TOPS_ITEM}" in one["demoText"]
    assert f"Only {BAG}'s unit is written" in one["onlyText"]


def test_the_dialog_still_refuses_a_full_upload_with_a_garment_off_its_unit(dialog_states):
    every = dialog_states["all_looks_one_off_its_unit"]
    assert every["uploadWhy"] == (
        f"{TOPS_ITEM} has cues but no unit — give it one on the Designs tab,"
        " or pick one LOOK above to write just that one.")
    assert every["demoWhy"] == every["uploadWhy"]     # the same hole, both ways
    assert every["only"] is None and every["onlyText"] == ""
    # Nothing left to say once every garment has a unit of its own.
    assert dialog_states["all_looks_all_assigned"]["uploadWhy"] is None


def test_the_dialogs_problems_are_counted_over_the_units_it_writes_to(dialog_states):
    # Another LOOK's broken cue: warned about by the server, not a refusal
    # here either - the bag's show file is not built from that cue.
    other = dialog_states["one_look_anothers_broken_cue"]
    assert other["uploadWhy"] is None and other["bad"] == 0
    # The chosen LOOK's own broken cue: refused, and named as that LOOK's.
    mine = dialog_states["one_look_its_own_broken_cue"]
    assert mine["uploadWhy"] == (
        f"{BAG} has 1 problem — fix it on the Timeline tab"
        " (nothing is written while one is left).")
    # A full upload counts every cue of the timeline, as it always has.
    assert dialog_states["all_looks_a_broken_cue"]["uploadWhy"] == (
        "The timeline has 1 problem — fix it on the Timeline tab"
        " (nothing is written while one is left).")
