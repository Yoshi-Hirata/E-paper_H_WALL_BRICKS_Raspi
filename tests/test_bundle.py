"""conductor/server.py: Workspace.import_bundle() and POST /api/bundle/import.

The designers' simulator (conductor/web/sim, plan_designer_sim.md, method
A - a single HTML file, no server) exports one JSON file - the CSVs
(verbatim text, no zip) plus the timeline - so a designer never has to
hand over a folder. This is the conductor side of that exchange: every
file name and the show are validated up front (whole or nothing - a bad
bundle writes nothing), the CSVs then save the same way /api/files does
(reporting which ones already existed as `overwritten` - Undo does not
bring those bytes back), and the timeline replaces itself exactly the
way /api/show/import does, in one undo step, except that a bundle with
no unit/board changes of its own (the normal case - the designers'
simulator has no notion of either) leaves the operator's assignments and
board renumbering here alone. Music always travels as a name only.

tests/fixtures/sim/bundle_v1.json (Coder P) is used when present; until
then a minimal bundle is hand-built here from tests/test_look.py's
MAP/GRID fixtures, in the exact shape of plan_designer_sim.md section 4.2.
"""

from __future__ import annotations

import io
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
    if not FIXTURE_PATH.exists():
        pytest.skip("tests/fixtures/sim/bundle_v1.json not pushed yet (Coder P)")
    bundle = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    ws = Workspace(tmp_path / "ws")
    result = ws.import_bundle(bundle)
    assert result["ok"] is True
    assert result["refused"] == []
    assert result["cues"] >= 1


# ---- fix round: boards kept like units, whole-or-nothing, overwrites ----

def test_bundle_boards_kept_when_the_bundle_carries_none(tmp_path):
    ws = Workspace(tmp_path / "ws")
    ws.save(MAP_NAME, MAP)
    ws.save(GRID_NAME, GRID)
    ws.set_boards("Look22", {17: 117})          # a real board renumbering
    before_boards = ws.export_show()["boards"]
    result = ws.import_bundle(make_bundle())    # default show has "boards": {}
    assert result["boards_kept"] is True
    assert ws.export_show()["boards"] == before_boards
    assert ws.export_show()["boards"]["Look22"] == {"17": 117}


def test_bundle_with_boards_still_applies_them(tmp_path):
    ws = Workspace(tmp_path / "ws")
    ws.save(MAP_NAME, MAP)
    ws.save(GRID_NAME, GRID)
    result = ws.import_bundle(make_bundle(
        extra_show={"boards": {"Look22": {"17": 217}}}))
    assert result["boards_kept"] is False
    assert ws.export_show()["boards"]["Look22"] == {"17": 217}


def test_bundle_units_null_entry_cannot_wipe_all_assignments(tmp_path):
    ws = Workspace(tmp_path / "ws")
    ws.save(MAP_NAME, MAP)
    ws.save(GRID_NAME, GRID)
    ws.assign("Look22", "radxa-04")
    # A bundle that mentions Look22 with a null unit still counts as
    # "brought no units of its own" - it must not replace the operator's
    # whole units map with {} the way a naive `if not show.get("units")`
    # (true for {"Look22": None}, since that dict is non-empty) would.
    result = ws.import_bundle(make_bundle(units={"Look22": None}))
    assert result["units_kept"] is True
    assert item(ws.state(), "Look22")["unit"] == "radxa-04"


def test_bundle_import_reports_overwritten_files(tmp_path):
    ws = Workspace(tmp_path / "ws")
    ws.save(MAP_NAME, MAP)                      # already there before the bundle
    result = ws.import_bundle(make_bundle())
    assert result["overwritten"] == [MAP_NAME]
    assert GRID_NAME not in result["overwritten"]
    assert sorted(result["saved"]) == [GRID_NAME, MAP_NAME]


def test_bundle_music_name_does_not_touch_the_real_music_file(tmp_path):
    ws = Workspace(tmp_path / "ws")
    ws.save_music("song.mp3", io.BytesIO(b"abc"), 3)
    result = ws.import_bundle(make_bundle(music={"name": "other.mp3"}))
    assert result["music"] == "other.mp3"       # handed back only for the toast
    assert ws.music_info()["name"] == "song.mp3"  # the real file is untouched


def test_bundle_refuses_unsafe_file_names_without_writing_anything(tmp_path):
    ws = Workspace(tmp_path / "ws")
    bundle = make_bundle(files={
        MAP_NAME: MAP,
        "../x_map.csv": "side,row\n",
        "/etc/x_map.csv": "side,row\n",
        "bad\x00name_map.csv": "side,row\n",
    }, cues=[])
    result = ws.import_bundle(bundle)
    assert result["saved"] == [MAP_NAME]
    assert len(result["refused"]) == 3
    assert all("unusable file name" in r for r in result["refused"])
    assert sorted(p.name for p in ws.files.glob("*.csv")) == [MAP_NAME]


def test_a_full_width_design_name_is_kept_exactly_as_the_site_writes_it(tmp_path):
    # 2026-09-26, from a real bundle: the designer typed the 配色案名 with
    # FULL-WIDTH digits (U+FF11), which every page on their side accepts,
    # and this import refused the file as an unusable name - so the cue
    # that pointed at it read "design ... is not loaded" and the garment
    # stayed dark. The 配線ナビ goes on writing names that way (the
    # operator's call), so the name is now KEPT, character for character,
    # and the cue that points at it simply works.
    wide = "Look22_color_１_HW_grid.csv"
    ws = Workspace(tmp_path / "ws")
    bundle = make_bundle(
        files={MAP_NAME: MAP, wide: GRID},
        cues=[{"id": "c0", "item": "Look22", "at": 0.0, "design": wide}],
        extra_show={"transitions": {wide: {"sequence": "top_down", "span_s": 2.0}}})
    result = ws.import_bundle(bundle)
    assert result["refused"] == []
    assert sorted(result["saved"]) == sorted([MAP_NAME, wide])
    assert result["renamed"] == {}          # nothing was respelled
    assert (ws.files / wide).is_file()
    state = ws.state()
    cue = state["show"]["cues"][0]
    assert cue["design"] == wide and cue["problems"] == []
    # And the garment really does have that design, with the transition
    # the designer set on it - "is not loaded" was the whole symptom.
    designs = item(state, "Look22")["designs"]
    assert [d["name"] for d in designs] == [wide]
    assert designs[0]["transition"] == {"sequence": "top_down", "span_s": 2.0}


def test_a_full_width_hw_name_and_a_japanese_design_name_both_round_trip(tmp_path):
    # The site's own _HW.csv spelling with a full-width digit, and a
    # 配色案名 in Japanese: neither is renamed, both resolve, and the
    # Designs list calls each one what the designer typed.
    wide_hw = "Look22_４_HW.csv"
    kana = "Look22_color_柄A_grid.csv"
    ws = Workspace(tmp_path / "ws")
    bundle = make_bundle(
        files={MAP_NAME: MAP, wide_hw: GRID, kana: GRID},
        cues=[{"id": "c0", "item": "Look22", "at": 0.0, "design": wide_hw},
              {"id": "c1", "item": "Look22", "at": 120.0, "design": kana}])
    result = ws.import_bundle(bundle)
    assert result["refused"] == [] and result["renamed"] == {}
    state = ws.state()
    assert [c["design"] for c in state["show"]["cues"]] == [wide_hw, kana]
    assert all(c["problems"] == [] for c in state["show"]["cues"])
    assert sorted(d["label"] for d in item(state, "Look22")["designs"]) == \
        ["柄A", "４"]


def test_a_decomposed_japanese_name_lands_on_the_composed_one(tmp_path):
    # NFC is the one respelling an import may do, and the reason it must
    # do it: a Mac hands file names over DECOMPOSED (NFD), so "ガラ"
    # arrives as カ + U+3099. Saved composed, it is the same file as the
    # one that name typed on Windows would make - not a second design
    # that looks identical in the list - and the cue naming it follows.
    composed = "Look22_color_ガラ_grid.csv"
    decomposed = "Look22_color_" + "ガラ" + "_grid.csv"
    assert composed != decomposed
    ws = Workspace(tmp_path / "ws")
    bundle = make_bundle(
        files={MAP_NAME: MAP, decomposed: GRID},
        cues=[{"id": "c0", "item": "Look22", "at": 0.0, "design": decomposed}])
    result = ws.import_bundle(bundle)
    assert result["refused"] == []
    assert result["renamed"] == {decomposed: composed}
    assert (ws.files / composed).is_file()
    cue = ws.state()["show"]["cues"][0]
    assert cue["design"] == composed and cue["problems"] == []


def test_a_name_that_is_still_unusable_after_composing_is_refused(tmp_path):
    # Composing (NFC) is the ONE change an import may make to a file
    # name. Anything else - a path, a control character, a full-width
    # solidus - is refused outright, never quietly mangled into some
    # other file's name.
    ws = Workspace(tmp_path / "ws")
    bundle = make_bundle(files={
        MAP_NAME: MAP,
        "bad\x00name_map.csv": "side,row\n",
        "sub／x_map.csv": "side,row\n",   # ／ is not a letter or a digit
    }, cues=[])
    result = ws.import_bundle(bundle)
    assert result["saved"] == [MAP_NAME]
    assert result["renamed"] == {}
    assert len(result["refused"]) == 2
    assert all("unusable file name" in r for r in result["refused"])
    assert sorted(p.name for p in ws.files.glob("*.csv")) == [MAP_NAME]


def test_bundle_with_a_non_string_file_value_writes_nothing(tmp_path):
    ws = Workspace(tmp_path / "ws")
    bundle = make_bundle(files={MAP_NAME: MAP, GRID_NAME: 12345}, cues=[])
    with pytest.raises(ValueError):
        ws.import_bundle(bundle)
    assert list(ws.files.glob("*.csv")) == []    # whole or nothing


def test_bundle_over_the_file_limit_is_refused(tmp_path):
    ws = Workspace(tmp_path / "ws")
    bundle = make_bundle(files={f"f{i}_map.csv": "x" for i in range(201)},
                         cues=[])
    with pytest.raises(ValueError):
        ws.import_bundle(bundle)
    assert list(ws.files.glob("*.csv")) == []


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
                     "files": {MAP_NAME: MAP, GRID_NAME: 12345}}) == 400
        assert post({"format": "epaper-show-bundle", "version": 1,
                     "show": {"format": "epaper-show", "version": 1},
                     "files": {f"f{i}_map.csv": "x" for i in range(201)}}) == 400
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
