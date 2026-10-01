"""Automatic workspace backups and restore (conductor/backups.py, the
exhibition's Conductor on radxa-05, 2026-10-01).

* when a generation is taken: after an import, after an Upload (marked as
  the units'), after edits - debounced on a fake clock - and never again
  for content the newest one already holds;
* what is kept: the newest N and the uploaded one;
* power-safe: every write fsync'ed, a write cut short never indexed, stray
  temp files and tars that do not read whole cleaned up at the next start;
* restore: refused during a run / a START's preset, a round trip through
  the import's swap that never touches fleet.json, "Upload needed" vs
  "units already hold it", the list API and the passcode gate;
* the PC's default Conductor: nothing made on disk, `serve` called exactly
  as before;
* the LCD's BACKUPS page (ui/exhibition.py) against a fake Conductor, and
  once end to end against a real one on an ephemeral port.

No unit, no production Conductor (8765 / 8766 / 10.42.0.1) is touched.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import tarfile
import threading
import urllib.error
from pathlib import Path

import pytest

from conductor import backups as backups_mod
from conductor import __main__ as cli
from conductor.backups import (BACKUP_KEEP, BACKUPS_OFF, INDEX_NAME, Backups,
                               backups_folder, clean_keep, content_revision,
                               inspect_tar, start_backups)
from conductor.fleet import Fleet
from conductor.server import STAGING_REFUSAL, Workspace, make_server
from tests.test_conductor_exhibition import Clock, _get, _post, _serve, _workspace
from tests.test_fleet import StubLink
from tests.test_ui_exhibition import CONDUCTOR_URL, FakeConductor, make_app
from tests.test_ui_runner import wait_until
from ui import render
from ui.app import Screen
from ui.config import HEIGHT, WIDTH
from ui.exhibition import (BACKUPS, BACKUPS_UNSUPPORTED, DONE, FAILED, IDLE, MAIN,
                           SENDING, Exhibition, http_json)

QUIET = backups_mod.EDIT_QUIET_S


def _quiet(*_):
    pass


def _keeper(ws, clock=None, keep=BACKUP_KEEP, **kw):
    """Backups with no worker thread: every generation is taken inline."""
    return Backups(ws, clock=clock or Clock(), keep=keep, log=_quiet, **kw)


def _names(keeper):
    return [row["name"] for row in keeper.listing()["backups"]]


def _reasons(keeper):
    return [row["reason"] for row in keeper.listing()["backups"]]


def _upload_server(tmp_path, adopt=False, keep=BACKUP_KEEP, clock=None):
    """A Conductor on a free port with one stub unit (radxa-01, which the
    workspace's Look23 is assigned to) and backups on, inline."""
    ws = _workspace(tmp_path / "exhibition")
    fleet = Fleet({})
    fleet.links = {"radxa-01": StubLink("radxa-01", "stopped")}
    server = make_server(tmp_path / "exhibition", port=0, fleet=fleet, adopt=adopt)
    handler = server.RequestHandlerClass
    keeper = _keeper(handler.workspace, clock=clock, keep=keep)
    handler.backups = keeper
    return ws, fleet, server, handler, keeper, _serve(server)


def _stop(server):
    server.shutdown()
    server.server_close()


# ------------------------------------------------------------ where, what

def test_the_folder_is_beside_the_workspace_never_inside_it(tmp_path):
    assert backups_folder(tmp_path / "exhibition") == (tmp_path / "exhibition-backups").resolve()
    assert backups_folder("/home/radxa/exhibition").name == "exhibition-backups"
    assert clean_keep(5) == 5 and clean_keep(1) == 1 and clean_keep(50) == 50
    for bad in (0, 51, "5", 5.0, True, None):
        assert clean_keep(bad) == BACKUP_KEEP


def test_a_generation_is_the_export_tar_and_its_revision_is_the_content(tmp_path):
    ws = _workspace(tmp_path / "exhibition")
    keeper = _keeper(ws)
    job = keeper.request("edit")
    assert job.error is None and job.result["reason"] == "edit"
    path = keeper.path(job.result["name"])
    assert path.parent == (tmp_path / "exhibition-backups").resolve()
    with tarfile.open(str(path), mode="r:") as tar:
        names = tar.getnames()
    assert names == ["show.json", "history.json", "files/Look23_color_ivory_grid.csv",
                     "files/Look23_color_scarlet_grid.csv", "files/Look23_map.csv",
                     "music/show.mp3"]
    # Named YYYYmmdd-HHMMSS-<reason>-<revision8>.tar; the revision is the
    # content's - the same with or without a tar in between.
    record = job.result
    assert record["name"].endswith(f"-edit-{record['revision'][:8]}.tar")
    assert record["revision"] == content_revision(ws) == inspect_tar(path)["revision"]
    assert record["cues"] == 2 and record["size"] == path.stat().st_size
    # mtimes are not content: touching every file changes Workspace.revision()
    # (an import does exactly that) but not the generation's revision.
    before = ws.revision()
    for csv in ws.files.glob("*.csv"):
        os.utime(csv, (1, 1))
    assert ws.revision() != before and content_revision(ws) == record["revision"]
    # fleet.json never travels.
    (ws.root / "fleet.json").write_text('{"passcode": "x"}', encoding="utf-8")
    assert content_revision(ws) == record["revision"]


# ------------------------------------------------------------ when

def test_edits_make_one_generation_after_60_s_of_quiet(tmp_path):
    clock = Clock(1000.0)
    ws = _workspace(tmp_path / "exhibition")
    keeper = _keeper(ws, clock=clock)
    assert keeper.tick() is None                  # the first look: the baseline
    assert _names(keeper) == []
    ws.set_label("Look23", "LOOK 23", "A")         # an edit
    clock.now += 1
    assert keeper.tick() is None                  # seen, the quiet starts
    clock.now += QUIET - 5
    assert keeper.tick() is None                  # not quiet long enough
    ws.set_label("Look23", "LOOK 23", "B")         # ...and another edit restarts it
    clock.now += 10
    assert keeper.tick() is None
    clock.now += QUIET - 1
    assert keeper.tick() is None
    clock.now += 1
    taken = keeper.tick()
    assert taken is not None and taken["reason"] == "edit"
    assert _reasons(keeper) == ["edit"]
    # Quiet from here on: nothing more, however long.
    for _ in range(5):
        clock.now += QUIET * 2
        assert keeper.tick() is None
    assert len(_names(keeper)) == 1
    # An edit and its undo inside the window: the content is the newest
    # generation's again - skipped, nothing written.
    ws.set_label("Look23", "LOOK 23", "C")
    clock.now += 1
    keeper.tick()
    ws.set_label("Look23", "LOOK 23", "B")
    clock.now += QUIET + 1
    keeper.tick()
    clock.now += 1
    skipped = keeper.tick()
    assert skipped is None or skipped.get("skipped")
    assert len(_names(keeper)) == 1
    assert not [p for p in keeper.folder.iterdir() if p.name.endswith(".part")]


def test_the_worker_thread_takes_a_start_generation_and_watches_for_edits(tmp_path):
    ws = _workspace(tmp_path / "exhibition")
    keeper = Backups(ws, quiet_s=0.2, poll_s=0.05, log=_quiet).start()
    try:
        assert wait_until(lambda: _reasons(keeper) == ["start"], timeout=10)
        ws.set_label("Look23", "LOOK 23", "edited")
        assert wait_until(lambda: _reasons(keeper) == ["edit", "start"], timeout=10)
    finally:
        keeper.stop()
    # Restarted on the same content (a power cut): no second "start".
    again = Backups(ws, quiet_s=0.2, poll_s=0.05, log=_quiet).start()
    try:
        job = again.request("edit", wait=10)      # queued behind its start
        assert job.done.is_set() and job.result.get("skipped")
        assert _reasons(again) == ["edit", "start"]
    finally:
        again.stop()
    # An empty workspace (a fresh radxa-05) has nothing to keep.
    empty = Workspace(tmp_path / "fresh")
    keeper = _keeper(empty)
    assert keeper.request("start").result is None
    assert not keeper.folder.exists()


def test_import_and_upload_make_generations_and_the_upload_is_marked(tmp_path):
    ws, fleet, server, handler, keeper, port = _upload_server(tmp_path)
    try:
        other = _workspace(tmp_path / "pc")
        other.set_label("Look23", "LOOK 23", "from the PC")
        packed = io.BytesIO()
        other.export_tar(packed)
        status, answer = _post(port, "/api/workspace/import", packed.getvalue())
        assert status == 200, answer
        assert _reasons(keeper) == ["import"]
        assert keeper.listing()["uploaded"] is None
        # The import's content is the newest: an Upload right after it
        # writes nothing new - it marks that generation as the units'.
        status, answer = _post(port, "/api/fleet/upload", {})
        assert status == 200 and answer["units"]["radxa-01"]["ok"], answer
        listing = keeper.listing()
        assert _reasons(keeper) == ["import"]
        assert listing["uploaded"] == listing["backups"][0]["name"]
        assert listing["backups"][0]["uploaded"] is True
        # An edit, then an Upload: a new generation, now the units'.
        handler.workspace.set_label("Look23", "LOOK 23", "edited")
        status, answer = _post(port, "/api/fleet/upload", {})
        assert status == 200
        listing = keeper.listing()
        assert [r["reason"] for r in listing["backups"]] == ["upload", "import"]
        assert [r["uploaded"] for r in listing["backups"]] == [True, False]
        index = json.loads((keeper.folder / INDEX_NAME).read_text(encoding="utf-8"))
        assert index["uploaded"]["name"] == listing["backups"][0]["name"]
        assert index["uploaded"]["whole"] is True
        # A refused import makes nothing.
        assert _post(port, "/api/workspace/import", b"hello")[0] == 400
        assert len(_names(keeper)) == 2
    finally:
        _stop(server)


def test_an_edit_landing_between_the_upload_and_the_tar_is_not_marked(tmp_path):
    ws = _workspace(tmp_path / "exhibition")
    keeper = _keeper(ws)
    keeper.request("edit")
    rev = ws.revision()                           # what the Upload carried
    ws.set_timeline(90, [])                       # ...and a cue edit lands
    keeper.after_upload(rev, whole=True)
    listing = keeper.listing()
    assert listing["backups"][0]["reason"] == "upload"
    assert listing["uploaded"] is None            # no generation is what the units hold
    # A label never reaches a unit (Workspace.revision ignores it): a
    # generation that differs from the upload only there is still theirs.
    rev = ws.revision()
    ws.set_label("Look23", "LOOK 23", "late label")
    keeper.after_upload(rev, whole=True)
    listing = keeper.listing()
    assert listing["uploaded"] == listing["backups"][0]["name"]


def test_keep_n_and_always_the_uploaded_one(tmp_path):
    ws = _workspace(tmp_path / "exhibition")
    keeper = _keeper(ws, keep=2)
    ws.set_label("Look23", "LOOK 23", "v1")
    keeper.after_upload(ws.revision(), whole=True)
    uploaded = _names(keeper)[0]
    for n in range(2, 6):
        ws.set_label("Look23", "LOOK 23", f"v{n}")
        keeper.request("edit")
    listing = keeper.listing()
    assert [r["reason"] for r in listing["backups"]] == ["edit", "edit", "upload"]
    assert listing["backups"][-1]["name"] == uploaded and listing["uploaded"] == uploaded
    on_disk = sorted(p.name for p in keeper.folder.glob("*.tar"))
    assert on_disk == sorted(_names(keeper))
    # A new Upload moves the mark; the old uploaded one is then just old.
    ws.set_label("Look23", "LOOK 23", "v6")
    keeper.after_upload(ws.revision(), whole=True)
    assert _reasons(keeper) == ["upload", "edit"]
    assert uploaded not in _names(keeper)
    assert not (keeper.folder / uploaded).exists()


# ------------------------------------------------------------ power-safe

def test_every_write_is_fsynced(tmp_path, monkeypatch):
    synced = []
    real = os.fsync

    def spy(fd):
        synced.append(fd)
        return real(fd)

    monkeypatch.setattr(backups_mod.os, "fsync", spy)
    ws = _workspace(tmp_path / "exhibition")
    keeper = _keeper(ws)
    replaced = []
    real_replace = os.replace
    monkeypatch.setattr(backups_mod.os, "replace",
                        lambda a, b: (replaced.append(Path(b).name), real_replace(a, b))[1])
    job = keeper.request("edit")
    assert job.error is None
    # The tar and index.json: each fsync'ed before its rename (and the
    # folder after it, on POSIX).
    assert len(synced) >= (2 if os.name == "nt" else 4)
    assert replaced == [job.result["name"], INDEX_NAME]


def test_a_write_cut_short_is_never_indexed(tmp_path, monkeypatch):
    ws = _workspace(tmp_path / "exhibition")
    keeper = _keeper(ws)
    real_export = ws.export_tar

    def power_cut(out):
        whole = io.BytesIO()
        real_export(whole)
        out.write(whole.getvalue()[:5000])
        raise OSError("power cut")

    monkeypatch.setattr(ws, "export_tar", power_cut)
    job = keeper.request("edit")
    assert job.result is None and "power cut" in job.error
    assert keeper.listing()["backups"] == [] and "power cut" in keeper.problem
    assert [p.name for p in keeper.folder.iterdir()] == []      # the temp went too

    # A tar that exports fine but does not read whole (the SD card) is not
    # renamed into place either.
    def short(out):
        whole = io.BytesIO()
        real_export(whole)
        out.write(whole.getvalue()[:len(whole.getvalue()) // 2])

    monkeypatch.setattr(ws, "export_tar", short)
    job = keeper.request("edit")
    assert job.result is None and "not a whole tar" in job.error
    assert [p.name for p in keeper.folder.iterdir()] == []


def test_a_tar_cut_at_a_member_boundary_does_not_read_whole(tmp_path):
    ws = _workspace(tmp_path / "exhibition")
    whole = io.BytesIO()
    ws.export_tar(whole)
    good = tmp_path / "good.tar"
    good.write_bytes(whole.getvalue())
    assert inspect_tar(good)["cues"] == 2
    with tarfile.open(str(good), mode="r:") as tar:
        for _ in tar:
            pass
        end = tar.offset
    cut = tmp_path / "cut.tar"
    cut.write_bytes(whole.getvalue()[:end])        # tarfile alone reads this as valid
    with tarfile.open(str(cut), mode="r:") as tar:
        assert len(tar.getnames()) == 6
    with pytest.raises(ValueError, match="no end"):
        inspect_tar(cut)
    mid = tmp_path / "mid.tar"
    mid.write_bytes(whole.getvalue()[:end - 3000])  # inside the music
    with pytest.raises(ValueError):
        inspect_tar(mid)
    junk = tmp_path / "junk.tar"
    junk.write_bytes(b"hello")
    with pytest.raises(ValueError):
        inspect_tar(junk)


def test_the_next_start_cleans_up_after_a_power_cut(tmp_path):
    ws = _workspace(tmp_path / "exhibition")
    keeper = _keeper(ws)
    kept = keeper.request("edit").result["name"]
    folder = keeper.folder
    # What a power cut can leave: a temp tar, a temp index, a tar under a
    # generation's name that never finished, and a whole tar the index
    # never heard of (renamed, then the power went before the index).
    (folder / f".20261001-120000-edit-1234{backups_mod.TEMP_SUFFIX}").write_bytes(b"half")
    (folder / f".{INDEX_NAME}.1-2{backups_mod.TEMP_SUFFIX}").write_bytes(b"{")
    (folder / f".{INDEX_NAME}.0123456789ab.tmp").write_bytes(b"{")   # durable.py's temp
    good = (folder / kept).read_bytes()
    (folder / "20261001-120001-edit-deadbeef.tar").write_bytes(good[:3000])
    ws.set_label("Look23", "LOOK 23", "after")
    later = io.BytesIO()
    ws.export_tar(later)
    (folder / "20991231-235959-upload-0badf00d.tar").write_bytes(later.getvalue())
    unrelated = folder / "notes.txt"
    unrelated.write_text("mine", encoding="utf-8")

    fresh = _keeper(ws)
    fresh.load()
    assert sorted(p.name for p in folder.iterdir()) == sorted(
        [kept, "20991231-235959-upload-0badf00d.tar", INDEX_NAME, "notes.txt"])
    listing = fresh.listing()
    assert [r["name"] for r in listing["backups"]] == [
        "20991231-235959-upload-0badf00d.tar", kept]          # newest by when written
    assert listing["backups"][0]["revision"] == content_revision(ws)
    # A lost or unreadable index is rebuilt from the folder (the uploaded
    # mark cannot be - it is simply absent).
    (folder / INDEX_NAME).write_text("{ not json", encoding="utf-8")
    rebuilt = _keeper(ws)
    assert sorted(_names(rebuilt)) == sorted(_names(fresh))
    assert rebuilt.listing()["uploaded"] is None
    # An indexed tar that went missing is dropped from the list.
    (folder / kept).unlink()
    again = _keeper(ws)
    assert _names(again) == ["20991231-235959-upload-0badf00d.tar"]


def test_a_damaged_show_json_is_not_backed_up_over_a_good_one(tmp_path):
    ws = _workspace(tmp_path / "exhibition")
    keeper = _keeper(ws)
    keeper.request("edit")
    (ws.root / "show.json").write_text('{"cues": [', encoding="utf-8")
    assert keeper.request("edit").result is None
    assert len(_names(keeper)) == 1 and "show.json does not parse" in keeper.problem


def test_no_edit_or_start_generation_while_the_show_runs(tmp_path):
    """MED-2: the SD card and the CPU are the show's during a run or a
    preset stage; the change is kept and taken once the run has ended."""
    clock = Clock(1000.0)
    busy = [True]
    ws = _workspace(tmp_path / "exhibition")
    keeper = _keeper(ws, clock=clock, busy=lambda: busy[0])
    job = keeper.request("start")
    assert job.result is None and "running" in job.not_taken
    assert not keeper.folder.exists()
    ws.set_label("Look23", "LOOK 23", "during the run")
    for _ in range(4):
        clock.now += QUIET
        assert keeper.tick() is None
    assert keeper.request("edit").result is None
    assert not keeper.folder.exists()
    # Imports and Uploads are not held back (they are refused during a run
    # anyway, or are the operator's own forced write).
    busy[0] = False
    clock.now += 1
    taken = keeper.tick()                         # quiet long since: taken at once
    assert taken is not None and taken["reason"] == "edit"
    assert keeper.listing()["backups"][0]["revision"] == content_revision(ws)
    # What `busy` is on the server: a run that has not ended, or a stage.
    fleet = Fleet({}, clock=lambda: 2000.0)
    assert backups_mod.fleet_busy(None) is False and backups_mod.fleet_busy(fleet) is False
    fleet.shows = {"radxa-01": {"id": "a", "cues": [], "duration": 100.0}}
    fleet.run = {"t0": 1950.0, "state": "running", "held_at": None}
    assert backups_mod.fleet_busy(fleet) is True
    fleet.run = {"t0": 1800.0, "state": "running", "held_at": None}    # ENDED / Loop wait
    assert backups_mod.fleet_busy(fleet) is False
    fleet.run = None
    fleet._staging = {"gen": 1}
    assert backups_mod.fleet_busy(fleet) is True


def test_no_generation_when_the_disk_would_be_left_nearly_full(tmp_path, monkeypatch):
    ws = _workspace(tmp_path / "exhibition")
    keeper = _keeper(ws)
    usage = shutil.disk_usage(str(tmp_path))
    monkeypatch.setattr(backups_mod.shutil, "disk_usage",
                        lambda path: usage._replace(free=150 * 1024 * 1024))
    job = keeper.request("edit")
    assert job.result is None and "not enough free space" in job.not_taken
    assert "200 MB must stay free" in keeper.problem
    assert not list(keeper.folder.glob("*.tar"))


def test_an_index_entry_without_a_revision_is_read_again(tmp_path):
    ws = _workspace(tmp_path / "exhibition")
    keeper = _keeper(ws)
    record = keeper.request("edit").result
    index_path = keeper.folder / INDEX_NAME
    index = json.loads(index_path.read_text(encoding="utf-8"))
    del index["generations"][0]["revision"]
    index_path.write_text(json.dumps(index), encoding="utf-8")
    again = _keeper(ws)
    assert again.find(record["name"])["revision"] == record["revision"]
    index["generations"][0]["revision"] = None
    index_path.write_text(json.dumps(index), encoding="utf-8")
    assert _keeper(ws).find(record["name"])["revision"] == record["revision"]


# ------------------------------------------------------------ restore

def test_a_restore_that_cannot_keep_the_current_state_first_is_refused(tmp_path, monkeypatch):
    """MED-3: the prerestore copy still queued after the wait -> 503, and
    that copy is never taken later (it would land after the swap)."""
    import conductor.server as server_mod

    monkeypatch.setattr(server_mod, "RESTORE_WAIT_S", 0.2)
    ws, fleet, server, handler, keeper, port = _upload_server(tmp_path)
    try:
        first = keeper.request("edit").result
        handler.workspace.set_label("Look23", "LOOK 23", "now")
        before = content_revision(handler.workspace)
        keeper._thread = threading.Thread()      # a worker that never gets to it
        status, answer = _post(port, "/api/backups/restore", {"name": first["name"]})
        assert status == 503
        assert answer["error"].startswith("could not keep the current state first - not restored")
        assert content_revision(handler.workspace) == before
        queued = keeper._jobs.get_nowait()
        assert queued.state == "cancelled"
        keeper._run(queued)                       # the worker reaching it at last
        assert queued.result is None and _names(keeper) == [first["name"]]
    finally:
        _stop(server)


def test_a_restore_over_a_damaged_workspace_says_no_copy_was_kept(tmp_path):
    ws, fleet, server, handler, keeper, port = _upload_server(tmp_path)
    try:
        first = keeper.request("edit").result
        (handler.workspace.root / "show.json").write_text('{"cues": [', encoding="utf-8")
        status, answer = _post(port, "/api/backups/restore", {"name": first["name"]})
        assert status == 200, answer
        assert any(p.startswith("the state before the restore was not kept (the "
                                "workspace is damaged - show.json does not parse")
                   for p in answer["problems"]), answer["problems"]
        assert content_revision(handler.workspace) == first["revision"]
    finally:
        _stop(server)


def test_restore_round_trip_keeps_fleet_json_and_the_state_before(tmp_path):
    ws, fleet, server, handler, keeper, port = _upload_server(tmp_path)
    workspace = handler.workspace
    fleet_json = '{"passcode": "venue-pass", "hotspot": "radxa-05", "units": {"radxa-01": "127.0.0.1:1"}}'
    (workspace.root / "fleet.json").write_text(fleet_json, encoding="utf-8")
    try:
        first = keeper.request("edit").result
        good_show = (workspace.root / "show.json").read_bytes()
        # The show gets damaged on site: a cue gone, a CSV deleted.
        workspace.set_timeline(120, [{"id": "p", "item": "Look23", "at": 0,
                                      "design": "Look23_color_ivory_grid.csv"}])
        workspace.delete("Look23_color_scarlet_grid.csv")
        status, listed = _get(port, "/api/backups")[:2]
        listed = json.loads(listed)
        assert status == 200 and listed["enabled"] is True
        assert [b["name"] for b in listed["backups"]] == [first["name"]]
        assert set(listed["backups"][0]) == {"name", "at", "reason", "revision",
                                             "uploaded", "cues", "size"}
        status, answer = _post(port, "/api/backups/restore", {"name": first["name"]})
        assert status == 200, answer
        assert answer["ok"] and answer["restored"] == first["name"]
        assert answer["cues"] == 2 and answer["files"] == 3 and answer["music"] == "show.mp3"
        assert answer["units_hold"] is False and answer["note"] == "restored - Upload needed"
        assert (workspace.root / "show.json").read_bytes() == good_show
        assert (workspace.files / "Look23_color_scarlet_grid.csv").is_file()
        assert content_revision(workspace) == first["revision"]
        # fleet.json - passcode, units, hotspot - is the host's, untouched.
        assert (workspace.root / "fleet.json").read_text(encoding="utf-8") == fleet_json
        # The damaged state was kept first, and the restore itself is not
        # taken for an edit.
        assert _reasons(keeper) == ["prerestore", "edit"]
        keeper.tick()
        keeper._clock.now += QUIET * 3
        assert keeper.tick() is None and len(_names(keeper)) == 2
        # ...so the restore can itself be undone.
        damaged = keeper.listing()["backups"][0]["name"]
        status, answer = _post(port, "/api/backups/restore", {"name": damaged})
        assert status == 200 and answer["cues"] == 1
        assert not (workspace.files / "Look23_color_scarlet_grid.csv").exists()
        # Nothing left behind in the workspace or the folder.
        assert not [p for p in workspace.root.iterdir() if p.name.startswith(".import")]
        assert not [p for p in keeper.folder.iterdir() if p.name.endswith(".part")]
    finally:
        _stop(server)


def test_restore_is_refused_during_a_run_and_a_preset_stage(tmp_path):
    ws, fleet, server, handler, keeper, port = _upload_server(tmp_path)
    try:
        name = keeper.request("edit").result["name"]
        handler.workspace.set_label("Look23", "LOOK 23", "now")
        before = content_revision(handler.workspace)
        fleet.run = {"t0": 0.0, "state": "running", "held_at": None}
        status, answer = _post(port, "/api/backups/restore", {"name": name})
        assert status == 409 and answer == {"error": "a run is active on this "
                                                     "Conductor - STOP it first"}
        fleet.run = None
        fleet._staging = {"gen": 1}
        status, answer = _post(port, "/api/backups/restore", {"name": name})
        assert status == 409 and answer["error"] == STAGING_REFUSAL
        fleet._staging = None
        assert content_revision(handler.workspace) == before
        assert len(_names(keeper)) == 1                       # no prerestore either
        # Unknown names are 404; a missing name too.
        for body in ({"name": "nope.tar"}, {"name": "../exhibition/show.json"}, {}):
            assert _post(port, "/api/backups/restore", body)[0] == 404, body
        # A generation that rotted on the card: 400, the workspace unchanged.
        (keeper.folder / name).write_bytes(b"rotten")
        status, answer = _post(port, "/api/backups/restore", {"name": name})
        assert status == 400 and "the workspace is unchanged" in answer["error"]
        assert content_revision(handler.workspace) == before
    finally:
        _stop(server)


def test_restoring_what_the_units_hold_needs_no_upload_on_the_exhibition_conductor(tmp_path):
    ws, fleet, server, handler, keeper, port = _upload_server(tmp_path, adopt=True)
    workspace = handler.workspace
    link = fleet.links["radxa-01"]
    try:
        status, answer = _post(port, "/api/fleet/upload", {})
        assert status == 200 and answer["units"]["radxa-01"]["ok"]
        show_id = answer["shows"]["radxa-01"]
        link.status = {"show": {"id": show_id, "state": "stopped", "synced": True,
                                "burn": {"state": "burned"}}}
        uploaded = keeper.listing()["uploaded"]
        assert uploaded
        workspace.set_label("Look23", "LOOK 23", "edited after the upload")
        keeper.request("edit")
        status, answer = _post(port, "/api/backups/restore", {"name": uploaded})
        assert status == 200, answer
        assert answer["units_hold"] is True
        assert answer["note"] == "restored - units already hold it"
        assert answer["shows"] == {"radxa-01": show_id}
        # The compiled show is offered again: the unit holding it is adopted
        # on its next poll, as after a restart - START needs no Upload.
        assert fleet._offered["radxa-01"]["id"] == show_id
        fleet._adopt_show(link)
        assert fleet.shows["radxa-01"]["id"] == show_id
        assert workspace.unit_marks["upload"]["radxa-01"] == workspace.revision()
        # Another generation: an Upload is needed, and said.
        edited = [r["name"] for r in keeper.listing()["backups"] if r["reason"] == "edit"][0]
        status, answer = _post(port, "/api/backups/restore", {"name": edited})
        assert status == 200
        assert answer["units_hold"] is False and answer["note"] == "restored - Upload needed"
        # A unit that says it holds something else vetoes the claim.
        link.status = {"show": {"id": "someone-elses", "state": "stopped"}}
        status, answer = _post(port, "/api/backups/restore", {"name": uploaded})
        assert answer["units_hold"] is False
        # Restoring what the workspace already is: no swap at all - the
        # units' adoption and marks are left exactly as they are.
        marks = dict(workspace.unit_marks.get("upload", {}))
        status, answer = _post(port, "/api/backups/restore", {"name": uploaded})
        assert status == 200 and answer["unchanged"] is True
        assert answer["note"] == "already the workspace - nothing restored"
        assert dict(workspace.unit_marks.get("upload", {})) == marks
    finally:
        _stop(server)


def test_a_restore_protects_its_own_generation_from_the_prune(tmp_path):
    ws = _workspace(tmp_path / "exhibition")
    server = make_server(tmp_path / "exhibition", port=0, fleet=Fleet({}))
    handler = server.RequestHandlerClass
    keeper = _keeper(handler.workspace, keep=1)
    handler.backups = keeper
    port = _serve(server)
    try:
        oldest = keeper.request("edit").result["name"]
        handler.workspace.set_label("Look23", "LOOK 23", "newer")
        status, answer = _post(port, "/api/backups/restore", {"name": oldest})
        assert status == 200, answer
        assert content_revision(handler.workspace) == keeper.find(oldest)["revision"]
    finally:
        _stop(server)


def test_the_list_and_the_restore_are_behind_the_passcode_for_other_hosts(tmp_path):
    ws, fleet, server, handler, keeper, port = _upload_server(tmp_path)
    handler.passcode = "venue-pass"
    try:
        name = keeper.request("edit").result["name"]
        assert _get(port, "/api/backups")[0] == 200          # this host: free (the LCD)
        handler.local_hosts = ()
        assert _get(port, "/api/backups")[0] == 401
        assert _post(port, "/api/backups/restore", {"name": name})[0] == 401
        assert _get(port, "/api/backups", {"X-Passcode": "venue-pass"})[0] == 200
        assert _post(port, "/api/backups/restore", {"name": name},
                     {"X-Passcode": "venue-pass"})[0] == 200
    finally:
        handler.local_hosts = ("127.0.0.1", "::1", "::ffff:127.0.0.1")
        _stop(server)


# ------------------------------------------------------------ the PC's default

def test_the_pc_default_conductor_makes_nothing_on_disk(tmp_path):
    ws, fleet, server, handler, keeper, port = _upload_server(tmp_path)
    handler.backups = None                        # what make_server() / serve() leave
    try:
        other = io.BytesIO()
        _workspace(tmp_path / "pc").export_tar(other)
        assert _post(port, "/api/workspace/import", other.getvalue())[0] == 200
        assert _post(port, "/api/fleet/upload", {})[0] == 200
        handler.workspace.set_label("Look23", "LOOK 23", "edit")
        status, raw, _ = _get(port, "/api/backups")
        assert status == 200 and json.loads(raw) == {
            "enabled": False, "backups": [], "uploaded": None, "problem": None,
            "why": BACKUPS_OFF}
        status, answer = _post(port, "/api/backups/restore", {"name": "x.tar"})
        assert status == 400 and answer["error"] == BACKUPS_OFF
        assert not (tmp_path / "exhibition-backups").exists()
        assert sorted(p.name for p in tmp_path.iterdir()) == ["exhibition", "pc"]
    finally:
        _stop(server)
    # make_server() itself never turns them on.
    plain = make_server(tmp_path / "other", port=0)
    try:
        assert plain.RequestHandlerClass.backups is None
    finally:
        plain.server_close()


def test_serve_is_called_exactly_as_before_without_the_flag(monkeypatch):
    seen = []

    def fake_serve(*args, **kwargs):
        seen.append((args, kwargs))
        return 0

    import conductor.server
    monkeypatch.setattr(conductor.server, "serve", fake_serve)
    assert cli.main(["serve"]) == 0
    assert "backups" not in seen[-1][1]
    assert seen[-1][1] == {"open_browser": False, "host": "127.0.0.1", "speaker": False,
                           "speaker_lead_ms": None, "speaker_output": None,
                           "passcode": None, "adopt": False, "label": None}
    assert cli.main(["serve", "--backups", "--adopt"]) == 0
    assert seen[-1][1]["backups"] is True and seen[-1][1]["adopt"] is True
    service = (Path(__file__).resolve().parents[1] / "radxa"
               / "epaper-conductor.service").read_text(encoding="utf-8")
    exec_line = service.split("ExecStart=")[1].split("\n")[0]
    assert " --backups" in exec_line and "--workspace /home/radxa/exhibition " in exec_line


def test_start_backups_follows_the_flag_and_fleet_json(tmp_path, capsys):
    class Handler:
        workspace = None

    root = tmp_path / "exhibition"
    config = Workspace(root)
    Handler.workspace = config
    assert start_backups(Handler, config, False) is None
    assert not (tmp_path / "exhibition-backups").exists()
    assert capsys.readouterr().out == ""
    keeper = start_backups(Handler, config, True)
    try:
        assert Handler.backups is keeper and keeper.keep == BACKUP_KEEP
        assert "backups: " in capsys.readouterr().out
    finally:
        keeper.stop()
    (root / "fleet.json").write_text('{"backups": true, "backup_keep": 99}',
                                     encoding="utf-8")
    keeper = start_backups(Handler, config, False)
    try:
        assert keeper is not None and keeper.keep == BACKUP_KEEP
        assert "backup_keep" in capsys.readouterr().out          # said, not fatal
    finally:
        keeper.stop()
    (root / "fleet.json").write_text('{"backups": true, "backup_keep": 12}',
                                     encoding="utf-8")
    keeper = start_backups(Handler, config, False)
    try:
        assert keeper.keep == 12
    finally:
        keeper.stop()
    (root / "fleet.json").write_text('{"backups": "yes"}', encoding="utf-8")
    assert start_backups(Handler, config, False) is None        # only true counts


# ------------------------------------------------------------ the page

def test_the_units_tab_lists_the_backups_and_restores_after_asking():
    from tests.test_conductor_board import PAGE, _function_body

    fleet = _function_body("renderFleet")
    assert fleet.index('id="ws-send"') < fleet.index("BACKUPS ON THIS CONDUCTOR") \
        < fleet.index('id="backup-table"') < fleet.index("MANUAL CUE")
    assert "if (!backupListTried) refreshBackups();" in fleet
    assert 'api("/api/backups")' in _function_body("refreshBackups")
    table = _function_body("backupTableHtml")
    assert "data-backup-restore" in table and "b.uploaded" in table
    restore = _function_body("restoreBackup")
    assert restore.index("confirm(") < restore.index('api("/api/backups/restore", { name })')
    assert "if (restoreInFlight) return;" in restore
    assert "fleet.json (passcode, units, hotspot) stays" in restore
    assert 'if (e.target.id === "backup-refresh") { await refreshBackups(); return; }' in PAGE
    assert 'e.target.closest("[data-backup-restore]")' in PAGE


# ------------------------------------------------------------ the LCD

GENERATIONS = [
    {"name": "20261001-152003-upload-1a2b3c4d.tar", "at": "2026-10-01 15:20:03",
     "reason": "upload", "revision": "1a2b3c4d5e6f7a8b", "uploaded": True,
     "cues": 18, "size": 31 * 1024 * 1024},
    {"name": "20261001-140000-edit-9f8e7d6c.tar", "at": "2026-10-01 14:00:00",
     "reason": "edit", "revision": "9f8e7d6c00000000", "uploaded": False,
     "cues": 17, "size": 30 * 1024 * 1024},
    {"name": "20260930-090000-import-00aa11bb.tar", "at": "2026-09-30 09:00:00",
     "reason": "import", "revision": "00aa11bb00000000", "uploaded": False,
     "cues": 1, "size": 1024 * 1024},
]


class BackupConductor(FakeConductor):
    """The fake Conductor of tests/test_ui_exhibition.py, with
    /api/backups and /api/backups/restore. `no_backups`: an older
    Conductor (404); `enabled` False: one without --backups."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.generations = [dict(g) for g in GENERATIONS]
        self.enabled = True
        self.no_backups = False

    def __call__(self, method, url, body, timeout):
        path = url[len(CONDUCTOR_URL):]
        if path not in ("/api/backups", "/api/backups/restore"):
            return super().__call__(method, url, body, timeout)
        self.calls.append((method, path, body, timeout))
        if self.hold_paths is None or path in self.hold_paths:
            self.release.wait(5.0)
        if self.down:
            raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
        if self.no_backups:
            return 404, "not found"
        if method == "GET":
            if not self.enabled:
                return 200, {"enabled": False, "backups": [], "why": BACKUPS_OFF}
            return 200, {"enabled": True, "backups": [dict(g) for g in self.generations]}
        if self.run is not None:
            return 409, {"error": "a run is active on this Conductor - STOP it first"}
        chosen = [g for g in self.generations if g["name"] == body.get("name")]
        if not chosen:
            return 404, {"error": f"{body.get('name')}: no such backup on this Conductor"}
        hold = chosen[0]["uploaded"]
        self.generations.insert(0, {"name": "20261001-160000-prerestore-77777777.tar",
                                    "at": "2026-10-01 16:00:00", "reason": "prerestore",
                                    "revision": "7777777700000000", "uploaded": False,
                                    "cues": 18, "size": 1})
        return 200, {"ok": True, "restored": chosen[0]["name"], "units_hold": hold,
                     "note": ("restored - units already hold it" if hold
                              else "restored - Upload needed")}


def _lcd(**kw):
    fake = BackupConductor(**kw)
    ex = Exhibition(http=fake, poll_open_s=60.0, poll_idle_s=60.0, echo_log=False)
    ex.poll()
    app, _ = make_app(ex)
    app.select("exhibition")
    app.handle("key1")
    assert app.screen is Screen.EXHIBITION
    return ex, fake, app


def _backup_gets(fake):
    return sum(1 for method, path, _, _ in fake.calls
               if method == "GET" and path == "/api/backups")


def test_down_opens_the_backups_page_and_the_list_is_read_only_while_it_is_open():
    ex, fake, app = _lcd()
    ex.poll()
    assert _backup_gets(fake) == 0                 # the EXHIBITION page never asks
    app.handle("down")
    assert ex.page == BACKUPS and app.screen is Screen.EXHIBITION
    # Read at once, on the reader's thread (the HAT loop never waits).
    assert wait_until(lambda: ex.backups is not None)
    assert _backup_gets(fake) >= 1
    assert ex.backup_rows() == [("10-01 15:20 upload *", True),
                                ("10-01 14:00 edit", False),
                                ("09-30 09:00 import", False)]
    assert ex.backup_choice == 0
    assert ex.backup_detail() == "18 cues · 31.0 MB · 1a2b3c4d"
    assert ex.backups_text() == "newest first  * = on the units"
    frame = app.frame()
    assert frame.size == (WIDTH, HEIGHT)
    # UP/DOWN move the cursor and stay inside the list.
    for event, choice in (("down", 1), ("down", 2), ("down", 2), ("up", 1)):
        app.handle(event)
        assert ex.backup_choice == choice
    # A refresh that brings a newer generation keeps the cursor on the
    # same generation, not the same row.
    fake.generations.insert(0, dict(GENERATIONS[1], name="20261001-170000-edit-12345678.tar",
                                    at="2026-10-01 17:00:00"))
    ex.poll()
    assert ex.backup_choice == 2 and ex.chosen_backup()["name"] == GENERATIONS[1]["name"]
    # KEY3 held and LEFT/RIGHT do nothing here; a plain KEY1 neither.
    for event in ("key3_hold", "left", "right", "key1", "press"):
        app.handle(event)
    ex.join(0.2)
    assert fake.posts() == [] and fake.loop["on"] is False
    # KEY2: back to the EXHIBITION page (not the menu), and no more reads.
    app.handle("key2")
    assert ex.page == MAIN and app.screen is Screen.EXHIBITION and ex.is_open
    asked = _backup_gets(fake)
    ex.poll()
    ex.poll()
    assert _backup_gets(fake) == asked
    # KEY2 again leaves for the menu; reopening starts on the EXHIBITION page.
    app.handle("down")
    app.handle("key2")
    app.handle("key2")
    assert app.screen is Screen.MENU and ex.page == MAIN


def test_a_held_key1_restores_the_chosen_one_and_says_what_the_units_need():
    ex, fake, app = _lcd()
    app.handle("down")
    assert wait_until(lambda: ex.backups)
    app.handle("down")                             # the edit one
    app.handle("key1_hold")                        # the first hold only arms it
    ex.join(0.2)
    assert fake.posts() == []
    assert ex.status_text() == "hold KEY1 again to restore <10-01 14:00 edit>"
    assert app.frame().size == (WIDTH, HEIGHT)
    app.handle("key1_hold")
    assert wait_until(lambda: ex.phase == DONE)
    assert fake.posts() == [("/api/backups/restore",
                             {"name": "20261001-140000-edit-9f8e7d6c.tar"})]
    restore = [c for c in fake.calls if c[1] == "/api/backups/restore"][0]
    assert restore[3] >= 180                       # a restore is given time
    assert ex.status_text() == "restored - Upload needed"
    # The list was read again after the answer: the prerestore is there.
    assert ex.backup_rows()[0][0] == "10-01 16:00 prerestore"
    assert ex.chosen_backup()["name"] == "20261001-140000-edit-9f8e7d6c.tar"
    assert app.frame().size == (WIDTH, HEIGHT)
    # The uploaded one: the units already hold it.
    for _ in range(5):
        app.handle("up")
    app.handle("down")
    assert ex.chosen_backup()["uploaded"] is True
    app.handle("key1_hold")
    app.handle("key1_hold")
    assert wait_until(lambda: ex.phase == DONE)
    assert ex.status_text() == "restored - units already hold it"


def test_a_stray_down_and_one_hold_never_restore():
    """MED-1 (review of 795bb0e): DOWN by mistake, then the hold meant as
    START - only arms. The arming lasts 5 s and UP/DOWN/KEY2 drop it."""
    now = [1000.0]
    fake = BackupConductor()
    # The units hold the middle one: the page opens on it, not the newest.
    fake.generations = [dict(GENERATIONS[1]), dict(GENERATIONS[0]), dict(GENERATIONS[2])]
    ex = Exhibition(http=fake, poll_open_s=60.0, poll_idle_s=60.0, echo_log=False,
                    clock=lambda: now[0])
    ex.poll()
    app, _ = make_app(ex)
    app.select("exhibition")
    app.handle("key1")
    app.handle("down")
    assert wait_until(lambda: ex.backups)
    assert ex.backup_choice == 1 and ex.chosen_backup()["uploaded"] is True
    app.handle("key1_hold")
    ex.join(0.2)
    assert fake.posts() == [] and ex.armed_text()
    # Expired: the next hold arms again, it does not restore.
    now[0] += 5.1
    assert ex.armed_text() == "" and ex.status_text() == ""
    app.handle("key1_hold")
    ex.join(0.2)
    assert fake.posts() == []
    # UP / DOWN / KEY2 disarm.
    for cancel in ("up", "down", "key2"):
        assert ex.armed_text()                     # (armed by the hold before)
        app.handle(cancel)
        assert ex.armed_text() == ""
        if cancel == "key2":
            assert ex.page == MAIN
            app.handle("down")
            assert wait_until(lambda: ex.backups)
        app.handle("key1_hold")                    # arms again, nothing sent
        ex.join(0.2)
        assert fake.posts() == []
    # Armed on one generation, moved to another: that one is not restored.
    app.handle("down")
    app.handle("key1_hold")
    ex.join(0.2)
    assert fake.posts() == []
    # ...and two holds on the same one, within 5 s, do.
    now[0] += 2.0
    app.handle("key1_hold")
    assert wait_until(lambda: ex.phase == DONE)
    assert len(fake.posts()) == 1


def test_a_refused_restore_is_shown_verbatim_and_the_keys_wait_while_it_runs():
    ex, fake, app = _lcd()
    fake.run = {"t0": 0.0, "state": "running", "now": 30.0}
    ex.poll()
    app.handle("down")
    assert wait_until(lambda: ex.backups)
    app.handle("key1_hold")
    app.handle("key1_hold")
    assert wait_until(lambda: ex.phase == FAILED)
    assert ex.status_text() == "ERROR a run is active on this Conductor - STOP it first"
    # While a restore is on its way only KEY2 is heard - back to EXHIBITION.
    fake.run = None
    fake.release.clear()
    fake.hold_paths = {"/api/backups/restore"}
    try:
        app.handle("key1_hold")
        app.handle("key1_hold")
        assert wait_until(lambda: ex.phase == SENDING)
        assert ex.status_text() == "restoring…"
        assert app.frame().size == (WIDTH, HEIGHT)
        app.handle("down")
        app.handle("key1_hold")
        assert len(fake.posts()) == 2
        app.handle("key2")
        assert ex.page == MAIN and app.screen is Screen.EXHIBITION
    finally:
        fake.release.set()
    assert wait_until(lambda: ex.phase == DONE)
    assert ex.status_text() == "restored - units already hold it"


def test_an_older_conductor_or_one_without_backups_is_said_so():
    ex, fake, app = _lcd()
    fake.no_backups = True
    app.handle("down")
    ex.poll()
    assert ex.backups_text() == BACKUPS_UNSUPPORTED and ex.backup_rows() == []
    app.handle("key1_hold")
    ex.join(0.2)
    assert fake.posts() == []                      # nothing to restore
    app.handle("key2")
    fake.no_backups = False
    fake.enabled = False
    app.handle("down")
    ex.poll()
    assert ex.backups_text() == "backups are off on this conductor"
    fake.enabled = True
    fake.generations = []
    ex.poll()
    assert ex.backups_text() == "no backups yet"
    # Without a Conductor the page is not even opened.
    app.handle("key2")
    fake.down = True
    ex.poll()
    app.handle("down")
    assert ex.page == MAIN


def test_the_backups_screen_renders_every_state():
    rows = [("10-01 15:20 upload *", True), ("10-01 14:00 edit", False)] * 4
    for phase, status in ((IDLE, ""), (SENDING, "restoring…"),
                          (DONE, "restored - Upload needed"),
                          (FAILED, "ERROR a run is active on this Conductor - STOP it first")):
        for choice in (0, 3, 7):
            image = render.backups_screen(rows, choice, phase, status=status,
                                          info="newest first  * = on the units",
                                          detail="18 cues · 31.0 MB · 1a2b3c4d",
                                          host="radxa-05")
            assert image.size == (WIDTH, HEIGHT)
    assert render.backups_screen([], 0, IDLE, info="no backups yet",
                                 locked=True).size == (WIDTH, HEIGHT)
    # The EXHIBITION page names the way there; without the flag it is as before.
    show = ("AZ_show_2026", "18 cues · 10:54")
    plain = render.exhibition_screen(True, show, "idle", "", "", "", IDLE, host="radxa-05")
    keyed = render.exhibition_screen(True, show, "idle", "", "", "", IDLE, host="radxa-05",
                                     backups_key=True)
    assert plain.tobytes() != keyed.tobytes()


def test_the_lcd_restores_through_a_real_conductor(tmp_path):
    """End to end: ui/exhibition.py's real HTTP against a real Conductor on
    an ephemeral port (never 8765)."""
    ws = _workspace(tmp_path / "exhibition")
    server = make_server(tmp_path / "exhibition", port=0, fleet=Fleet({}))
    handler = server.RequestHandlerClass
    keeper = _keeper(handler.workspace)
    handler.backups = keeper
    port = _serve(server)
    try:
        first = keeper.request("edit").result
        handler.workspace.set_timeline(30, [])
        ex = Exhibition(http=http_json, base=f"http://127.0.0.1:{port}",
                        poll_open_s=60.0, poll_idle_s=60.0, echo_log=False)
        ex.poll()
        assert ex.available
        ex.open_backups()
        ex.poll()
        assert [row["name"] for row in ex.backups] == [first["name"]]
        ex.restore_backup()                       # arms
        ex.restore_backup()                       # sends
        ex.join(30)
        assert ex.phase == DONE and ex.status_text() == "restored - Upload needed"
        assert content_revision(handler.workspace) == first["revision"]
        assert [row["reason"] for row in ex.backups] == ["prerestore", "edit"]
        ex.shutdown()
    finally:
        _stop(server)
