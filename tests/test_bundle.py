"""conductor/server.py: Workspace.import_bundle() and POST /api/bundle/import.

The designers' simulator (conductor/web/sim, plan_designer_sim.md) exports
one JSON file - the CSVs plus the timeline - so a designer never has to
hand over a folder. This is the conductor side of that exchange: the
CSVs save the same way /api/files does, then the timeline replaces
itself exactly the way /api/show/import does, in one undo step, except
that a bundle with no unit assignments of its own (the normal case -
the designers' simulator has no notion of units) leaves the operator's
assignments here alone.

tests/fixtures/sim/bundle_v1.json (Coder P) is used when present; until
then a minimal bundle is hand-built here from tests/test_look.py's
MAP/GRID fixtures, in the exact shape of plan_designer_sim.md section 4.2.
"""

from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conductor.server import Workspace, make_server
from tests.test_look import GRID, MAP

FIXTURE_PATH = (Path(__file__).resolve().parent
                / "fixtures" / "sim" / "bundle_v1.json")

MAP_NAME = "Look22_map.csv"
GRID_NAME = "Look22_color_pattern01_grid.csv"
DEFAULT_CUES = [{"id": "c0", "item": "Look22", "at": 0.0,
                 "design": GRID_NAME}]


def make_bundle(*, files=None, cues=None, units=None, extra_show=None,
                music=None):
    """A bundle in the exact shape of plan_designer_sim.md 4.2, with
    every field a real export would carry filled in with something
    small."""
    show = {
        "format": "epaper-show", "version": 1,
        "exported": "2026-09-24T14:20:05",
        "workspace": "designer",
        "duration": 600.0,
        "refresh_s": 7.0,
        "cues": DEFAULT_CUES if cues is None else cues,
        "transitions": {GRID_NAME: {"sequence": "top_down", "span_s": 2.0}},
        "labels": {"Look22": {"look": "22", "model": "AZ271SD1305"}},
        "boards": {},
    }
    if units is not None:
        show["units"] = units
    if music is not None:
        show["music"] = music
    if extra_show:
        show.update(extra_show)
    bundle = {
        "format": "epaper-show-bundle", "version": 1,
        "exported": "2026-09-24T14:20:05", "app": "az27ss-simulator 1.0",
        "show": show,
        "files": files if files is not None else {
            MAP_NAME: MAP, GRID_NAME: GRID,
        },
    }
    if music is not None:
        bundle["music"] = music
    return bundle


def item(state, name):
    return next(i for i in state["items"] if i["item"] == name)


# ---- Workspace.import_bundle() ----

def test_bundle_import_saves_the_csvs_and_the_show(tmp_path):
    ws = Workspace(tmp_path / "ws")
    result = ws.import_bundle(make_bundle())
    assert result["ok"] is True
    assert sorted(result["saved"]) == [GRID_NAME, MAP_NAME]
    assert result["refused"] == []
    assert result["cues"] == 1
    assert result["units_kept"] is True
    assert (ws.files / MAP_NAME).read_text(encoding="utf-8") == MAP
    assert (ws.files / GRID_NAME).read_text(encoding="utf-8") == GRID
    state = ws.state()
    assert state["show"]["cues"][0]["item"] == "Look22"
    assert state["show"]["cues"][0]["design"] == GRID_NAME


def test_bundle_import_keeps_the_operators_units(tmp_path):
    ws = Workspace(tmp_path / "ws")
    ws.save(MAP_NAME, MAP)
    ws.save(GRID_NAME, GRID)
    ws.assign("Look22", "radxa-03")
    result = ws.import_bundle(make_bundle())
    assert result["units_kept"] is True
    assert item(ws.state(), "Look22")["unit"] == "radxa-03"


def test_bundle_with_units_still_applies_them(tmp_path):
    ws = Workspace(tmp_path / "ws")
    ws.save(MAP_NAME, MAP)
    ws.save(GRID_NAME, GRID)
    ws.assign("Look22", "radxa-03")
    result = ws.import_bundle(make_bundle(units={"Look22": "radxa-07"}))
    assert result["units_kept"] is False
    assert item(ws.state(), "Look22")["unit"] == "radxa-07"


def test_bundle_import_is_one_undo_step(tmp_path):
    ws = Workspace(tmp_path / "ws")
    ws.import_bundle(make_bundle())
    assert len(ws.state()["show"]["cues"]) == 1
    # The CSVs are not part of the undo history (same rule as /api/files):
    # only the timeline import is one commit, and undoing it once is
    # enough to get back to before the bundle ever arrived.
    assert ws.undo() is True
    assert ws.state()["show"]["cues"] == []
    assert ws.undo() is False
    assert (ws.files / MAP_NAME).exists()          # the CSVs stayed


def test_bundle_import_refuses_a_wrong_format(tmp_path):
    ws = Workspace(tmp_path / "ws")
    with pytest.raises(ValueError):
        ws.import_bundle(dict(make_bundle(), format="epaper-show"))
    with pytest.raises(ValueError):
        ws.import_bundle(dict(make_bundle(), version=2))
    with pytest.raises(ValueError):
        ws.import_bundle({"format": "epaper-show-bundle", "version": 1,
                          "show": "not an object"})


def test_bundle_import_refuses_a_bad_file_name(tmp_path):
    ws = Workspace(tmp_path / "ws")
    bundle = make_bundle(files={
        MAP_NAME: MAP,
        "not_a_map_or_grid.csv": "side,row\n",
    }, cues=[])
    result = ws.import_bundle(bundle)
    assert result["saved"] == [MAP_NAME]
    assert len(result["refused"]) == 1
    assert "not_a_map_or_grid.csv" in result["refused"][0]


def test_bundle_import_warnings_see_the_new_csvs(tmp_path):
    ws = Workspace(tmp_path / "ws")
    # The cue's design lives only inside this same bundle's own files -
    # the workspace check has to run after the CSVs are saved, not
    # before, or every first-time bundle would warn about its own cues.
    result = ws.import_bundle(make_bundle())
    assert result["warnings"] == []

    # A cue for an item this workspace still has no map for does warn.
    result2 = ws.import_bundle(make_bundle(
        files={}, cues=[{"id": "c1", "item": "Look99", "at": 0.0,
                         "design": GRID_NAME}]))
    assert any("Look99" in w for w in result2["warnings"])


def test_designer_bundle_fixture_imports(tmp_path):
    if FIXTURE_PATH.exists():
        bundle = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    else:
        bundle = make_bundle(music={"name": "az27ss.mp3"})
    ws = Workspace(tmp_path / "ws")
    result = ws.import_bundle(bundle)
    assert result["ok"] is True
    assert result["refused"] == []
    assert result["cues"] >= 1


# ---- the HTTP route ----

def test_plain_show_json_still_imports(tmp_path):
    """/api/bundle/import is additive: the existing /api/show/import
    route (a bare epaper-show file, no CSVs) must still work unchanged."""
    server = make_server(tmp_path, port=0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    try:
        payload = {"format": "epaper-show", "version": 1, "duration": 600,
                   "cues": []}
        request = urllib.request.Request(
            f"{base}/api/show/import", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=5) as response:
            result = json.loads(response.read())
        assert result["ok"] is True
    finally:
        server.shutdown()
        server.server_close()


def test_bundle_import_hostile_json_is_a_400(tmp_path):
    server = make_server(tmp_path, port=0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    def post(payload):
        request = urllib.request.Request(
            f"{base}/api/bundle/import", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(request, timeout=5)
            return 200
        except urllib.error.HTTPError as exc:
            return exc.code
    try:
        assert post({"format": "not-a-bundle"}) == 400
        assert post({"format": "epaper-show-bundle", "version": 2,
                     "show": {}}) == 400
        assert post({"format": "epaper-show-bundle", "version": 1,
                     "show": None}) == 400
        assert post({"format": "epaper-show-bundle", "version": 1,
                     "show": {"format": "epaper-show", "version": 1,
                              "duration": None}}) == 400
        assert post({"format": "epaper-show-bundle", "version": 1,
                     "show": {"format": "epaper-show", "version": 1},
                     "files": {MAP_NAME: 12345}}) == 400
        assert post({"format": "epaper-show-bundle", "version": 1,
                     "show": {"format": "epaper-show", "version": 1},
                     "files": "not an object"}) == 400
    finally:
        server.shutdown()
        server.server_close()


def test_bundle_import_via_http(tmp_path):
    server = make_server(tmp_path, port=0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    try:
        request = urllib.request.Request(
            f"{base}/api/bundle/import",
            data=json.dumps(make_bundle()).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=5) as response:
            result = json.loads(response.read())
        assert result["ok"] is True
        assert result["cues"] == 1
        assert sorted(result["saved"]) == [GRID_NAME, MAP_NAME]
        state = json.loads(urllib.request.urlopen(
            f"{base}/api/state", timeout=5).read())
        assert state["show"]["cues"][0]["item"] == "Look22"
    finally:
        server.shutdown()
        server.server_close()
