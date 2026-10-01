"""conductor/durable.py: every persistent write survives a power cut.

radxa-05 lost its power twice on 2026-10-01 (ext4 "recovery complete" at
the next boot). A write in the seconds before such a cut could come back
empty or old; now each one is temp file -> fsync -> replace -> directory
fsync, and every reader takes an empty or torn file from an OLDER version
as "no record" with one log line instead of crashing at boot.

Three parts: the helper itself; one test per converted writer (it goes
through the helper, or a crash before the replace leaves the old file);
the readers on empty and torn files.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import stat
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from conductor import durable
from conductor import server as srv
from conductor.server import Workspace, write_fleet_template


@pytest.fixture
def fsyncs(monkeypatch):
    """Every os.fsync() call's fd, the real fsync still done."""
    calls = []
    real = os.fsync

    def spy(fd):
        calls.append(fd)
        return real(fd)
    monkeypatch.setattr(durable.os, "fsync", spy)
    return calls


@pytest.fixture
def dir_syncs(monkeypatch):
    """Every fsync_dir() call's path - recorded only, so `fsyncs` counts
    the files' own syncs on every OS (fsync_dir itself is tested apart)."""
    calls = []

    def spy(path):
        calls.append(Path(path))
    monkeypatch.setattr(durable, "fsync_dir", spy)
    return calls


def spy_on(monkeypatch, name):
    """Record each call of durable.<name> (args, kwargs) and still do it."""
    calls = []
    real = getattr(durable, name)

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real(*args, **kwargs)
    monkeypatch.setattr(durable, name, spy)
    return calls


def leftovers(folder: Path) -> "list[str]":
    return sorted(p.name for p in Path(folder).iterdir() if p.name.endswith(".tmp"))


# ---- the helper ----

def test_atomic_write_bytes_fsyncs_the_file_and_its_folder(tmp_path, fsyncs,
                                                           dir_syncs):
    path = tmp_path / "state.json"
    durable.atomic_write_bytes(path, b"new")
    assert path.read_bytes() == b"new"
    assert len(fsyncs) >= 1                   # the file itself, before the replace
    assert dir_syncs == [tmp_path]            # ...then the rename
    assert leftovers(tmp_path) == []


def test_atomic_write_replaces_an_existing_file_whole(tmp_path):
    path = tmp_path / "state.json"
    path.write_bytes(b"old and longer")
    durable.atomic_write_bytes(path, b"new")
    assert path.read_bytes() == b"new"
    assert leftovers(tmp_path) == []


def test_the_old_files_mode_is_kept(tmp_path, monkeypatch):
    path = tmp_path / "fleet.json"
    path.write_text("{}", encoding="utf-8")
    os.chmod(path, 0o600)
    before = stat.S_IMODE(os.stat(path).st_mode)
    applied = []
    real = os.chmod

    def spy(target, mode, *args, **kwargs):
        applied.append(mode)
        return real(target, mode, *args, **kwargs)
    monkeypatch.setattr(durable.os, "chmod", spy)
    durable.atomic_write_json(path, {"passcode": "x"})
    assert applied == [before]
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_an_explicit_mode_is_applied(tmp_path, monkeypatch):
    applied = []
    real = os.chmod

    def spy(target, mode, *args, **kwargs):
        applied.append(mode)
        return real(target, mode, *args, **kwargs)
    monkeypatch.setattr(durable.os, "chmod", spy)
    path = tmp_path / "secret.json"
    durable.atomic_write_text(path, "{}", mode=0o640)
    assert applied == [0o640]
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o640


def test_a_new_file_gets_the_plain_default_mode_not_mkstemps_0600(tmp_path):
    if os.name == "nt":
        pytest.skip("POSIX permission bits")
    mask = os.umask(0)
    os.umask(mask)
    path = tmp_path / "new.json"
    durable.atomic_write_text(path, "{}")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o666 & ~mask


def test_a_crash_before_the_replace_leaves_the_old_file_and_no_temp(tmp_path,
                                                                    monkeypatch):
    path = tmp_path / "show.json"
    path.write_text('{"old": true}', encoding="utf-8")

    def cut(src, dst):
        raise OSError("power cut")
    monkeypatch.setattr(durable.os, "replace", cut)
    with pytest.raises(OSError, match="power cut"):
        durable.atomic_write_json(path, {"new": True})
    assert json.loads(path.read_text(encoding="utf-8")) == {"old": True}
    assert leftovers(tmp_path) == []


def test_a_reader_holding_the_file_on_windows_is_waited_out_briefly(tmp_path,
                                                                    monkeypatch):
    path = tmp_path / "show-run.json"
    path.write_text("old", encoding="utf-8")
    real = os.replace
    refused = []

    def busy(src, dst):
        if len(refused) < 2:
            refused.append(dst)
            raise PermissionError("in use by another process")
        return real(src, dst)
    monkeypatch.setattr(durable.os, "replace", busy)
    monkeypatch.setattr(durable, "_POSIX", False)
    durable.atomic_write_text(path, "new")
    assert len(refused) == 2 and path.read_text(encoding="utf-8") == "new"
    # POSIX never refuses a rename for a reader: an error there is real.
    refused.clear()
    monkeypatch.setattr(durable, "_POSIX", True)
    with pytest.raises(PermissionError):
        durable.atomic_write_text(path, "newer")
    assert path.read_text(encoding="utf-8") == "new"
    assert leftovers(tmp_path) == []


def test_a_failed_fsync_leaves_the_old_file_and_no_temp(tmp_path, monkeypatch):
    path = tmp_path / "show.json"
    path.write_text('{"old": true}', encoding="utf-8")

    def eio(fd):
        raise OSError("EIO")
    monkeypatch.setattr(durable.os, "fsync", eio)
    with pytest.raises(OSError, match="EIO"):
        durable.atomic_write_json(path, {"new": True})
    assert json.loads(path.read_text(encoding="utf-8")) == {"old": True}
    assert leftovers(tmp_path) == []


def test_the_folder_fsync_is_tried_on_posix_and_skipped_on_windows(tmp_path,
                                                                   monkeypatch):
    opened, synced, closed = [], [], []
    monkeypatch.setattr(durable.os, "open",
                        lambda path, flags, *a: opened.append((path, flags)) or 4242)
    monkeypatch.setattr(durable.os, "fsync", lambda fd: synced.append(fd))
    monkeypatch.setattr(durable.os, "close", lambda fd: closed.append(fd))
    monkeypatch.setattr(durable, "_POSIX", True)
    durable.fsync_dir(tmp_path)
    assert [path for path, _ in opened] == [str(tmp_path)]
    assert synced == [4242] and closed == [4242]
    # A folder that will not sync is not an error: the data already is.
    monkeypatch.setattr(durable.os, "fsync",
                        lambda fd: (_ for _ in ()).throw(OSError("EINVAL")))
    durable.fsync_dir(tmp_path)
    assert closed == [4242, 4242]
    opened.clear()
    monkeypatch.setattr(durable, "_POSIX", False)
    durable.fsync_dir(tmp_path)
    assert opened == []


def test_text_newlines_follow_open(tmp_path):
    path = tmp_path / "a.csv"
    durable.atomic_write_text(path, "a\nb\r\n", newline="")
    assert path.read_bytes() == b"a\nb\r\n"
    durable.atomic_write_text(path, "a\nb")
    assert path.read_bytes() == ("a" + os.linesep + "b").encode()
    durable.atomic_write_text(path, "x\ny", newline="\r\n")
    assert path.read_bytes() == b"x\r\ny"


def test_json_roundtrip_with_options(tmp_path):
    path = tmp_path / "a.json"
    durable.atomic_write_json(path, {"b": 1, "a": "é"}, indent=1,
                              sort_keys=True, ensure_ascii=False,
                              trailing_newline=True)
    text = path.read_text(encoding="utf-8")
    assert json.loads(text) == {"a": "é", "b": 1}
    assert text.index('"a"') < text.index('"b"') and text.endswith("\n")


def test_fsync_tree_syncs_every_file_and_folder(tmp_path, fsyncs, dir_syncs):
    tree = tmp_path / "stage"
    (tree / "files").mkdir(parents=True)
    (tree / "music").mkdir()
    for name in ("show.json", "files/a.csv", "files/b.csv", "music/m.mp3"):
        (tree / name).write_bytes(b"x")
    durable.fsync_tree(tree)
    assert len(fsyncs) == 4
    assert set(dir_syncs) == {tree, tree / "files", tree / "music"}
    assert dir_syncs[-1] == tree                       # bottom-up, root last


def test_an_unreadable_file_is_said_once_per_version(tmp_path, capsys):
    path = tmp_path / "show-run.json"
    path.write_bytes(b"")
    durable.note_unreadable(path, "Expecting value")
    durable.note_unreadable(path, "Expecting value")
    err = capsys.readouterr().err
    assert err.count("show-run.json") == 1 and "empty" in err
    path.write_bytes(b'{"show": ')            # a different torn version
    durable.note_unreadable(path, "Expecting value")
    assert "9 bytes" in capsys.readouterr().err


def test_durable_imports_nothing_of_the_conductor():
    """ui/ (Python 3.9 on the units) imports it as conductor.durable: it
    must stay standard library only, with conductor/__init__.py empty of
    imports too."""
    for source in (ROOT / "conductor" / "durable.py",
                   ROOT / "conductor" / "__init__.py"):
        text = source.read_text(encoding="utf-8")
        assert "from ." not in text and "import conductor" not in text


# ---- the Conductor's writers ----

def _workspace(root):
    from tests.test_conductor_exhibition import _workspace as make
    return make(root)


def test_show_and_history_go_through_the_helper(tmp_path, monkeypatch):
    ws = Workspace(tmp_path / "ws")
    calls = spy_on(monkeypatch, "atomic_write_json")
    ws.set_timeline(600, [])
    written = sorted(Path(args[0]).name for args, _ in calls)
    assert "show.json" in written and "history.json" in written
    assert leftovers(ws.root) == []


def test_a_cut_before_the_replace_keeps_the_old_show_json(tmp_path, monkeypatch):
    ws = Workspace(tmp_path / "ws")
    ws.set_timeline(600, [])
    before = (ws.root / "show.json").read_bytes()

    def cut(src, dst):
        raise OSError("power cut")
    monkeypatch.setattr(durable.os, "replace", cut)
    with pytest.raises(OSError):
        ws.set_timeline(900, [])
    monkeypatch.undo()
    assert (ws.root / "show.json").read_bytes() == before
    assert leftovers(ws.root) == []


def test_an_uploaded_csv_goes_through_the_helper_untranslated(tmp_path,
                                                              monkeypatch):
    ws = Workspace(tmp_path / "ws")
    calls = spy_on(monkeypatch, "atomic_write_text")
    ws.save("Look23_map.csv", "a,b\r\nc,d\n")
    (args, kwargs), = calls
    assert Path(args[0]).name == "Look23_map.csv" and kwargs["newline"] == ""
    assert (ws.files / "Look23_map.csv").read_bytes() == b"a,b\r\nc,d\n"
    assert leftovers(ws.files) == []


def test_a_cut_while_replacing_a_csv_keeps_the_old_one(tmp_path, monkeypatch):
    ws = Workspace(tmp_path / "ws")
    ws.save("Look23_map.csv", "old")

    def cut(src, dst):
        raise OSError("power cut")
    monkeypatch.setattr(durable.os, "replace", cut)
    with pytest.raises(OSError):
        ws.save("Look23_map.csv", "new")
    monkeypatch.undo()
    assert (ws.files / "Look23_map.csv").read_text(encoding="utf-8") == "old"
    assert leftovers(ws.files) == []


def test_a_duplicated_map_goes_through_the_helper(tmp_path, monkeypatch):
    ws = _workspace(tmp_path / "ws")
    calls = spy_on(monkeypatch, "atomic_write_bytes")
    twin = ws.duplicate("Look23")
    assert [Path(args[0]).name for args, _ in calls] == [f"{twin}_map.csv"]
    assert ((ws.files / f"{twin}_map.csv").read_bytes()
            == (ws.files / "Look23_map.csv").read_bytes())


def test_music_is_fsynced_once_at_its_end_and_its_folder_after(tmp_path,
                                                               fsyncs, dir_syncs):
    ws = Workspace(tmp_path / "ws")
    size = srv.MUSIC_CHUNK * 3 + 7                  # several chunks...
    before = len(fsyncs)
    ws.save_music("song.mp3", io.BytesIO(b"\xff" * size), size)
    # ...one fsync for the bytes, plus the two JSON commits' own.
    music_syncs = len(fsyncs) - before - 2
    assert music_syncs == 1
    assert ws.music in dir_syncs
    assert (ws.music / "song.mp3").stat().st_size == size


def test_a_workspace_import_is_fsynced_before_the_swap(tmp_path, monkeypatch):
    a = _workspace(tmp_path / "a")
    packed = io.BytesIO()
    a.export_tar(packed)
    tar_path = tmp_path / "ws.tar"
    tar_path.write_bytes(packed.getvalue())
    b = Workspace(tmp_path / "b")
    story = []
    real_tree, real_dir, real_swap = (durable.fsync_tree, durable.fsync_dir,
                                      b._swap_in)

    def tree(path):
        story.append(("tree", Path(path).name.startswith(".import-")))
        # Everything is already staged when the tree is synced.
        assert (Path(path) / "show.json").is_file()
        return real_tree(path)

    def folder(path):
        story.append(("dir", Path(path) == b.root))
        return real_dir(path)

    def swap(stage, aside):
        story.append(("swap", None))
        return real_swap(stage, aside)
    monkeypatch.setattr(durable, "fsync_tree", tree)
    monkeypatch.setattr(durable, "fsync_dir", folder)
    monkeypatch.setattr(b, "_swap_in", swap)
    b.import_tar(tar_path)
    assert story[0] == ("tree", True)
    assert story.index(("swap", None)) < story.index(("dir", True))
    assert (b.root / "show.json").read_bytes() == (a.root / "show.json").read_bytes()


def test_set_fleet_option_goes_through_the_helper(tmp_path, monkeypatch):
    ws = Workspace(tmp_path / "ws")
    (ws.root / "fleet.json").write_text('{"passcode": "pc"}', encoding="utf-8")
    calls = spy_on(monkeypatch, "atomic_write_json")
    ws.set_fleet_option("speaker_volume", 40)
    assert [Path(args[0]).name for args, _ in calls] == ["fleet.json"]
    assert json.loads((ws.root / "fleet.json").read_text(encoding="utf-8")) == {
        "passcode": "pc", "speaker_volume": 40}
    assert leftovers(ws.root) == []


def test_the_fleet_template_is_fsynced(tmp_path, fsyncs, dir_syncs):
    assert write_fleet_template(tmp_path) is True
    assert len(fsyncs) == 1 and dir_syncs == [tmp_path]
    assert json.loads((tmp_path / "fleet.json").read_text(encoding="utf-8"))


# ---- the units' writers ----

def test_the_units_show_and_run_records_go_through_the_helper(tmp_path,
                                                              monkeypatch):
    from ui.showplay import ShowPlayer
    calls = spy_on(monkeypatch, "atomic_write_json")
    holder = SimpleNamespace(store=tmp_path)
    for name in ("show.json", "show-run.json", "show-burn.json"):
        ShowPlayer._write(holder, name, {"id": name})
    assert [Path(args[0]) for args, _ in calls] == [
        tmp_path / "show.json", tmp_path / "show-run.json",
        tmp_path / "show-burn.json"]
    assert json.loads((tmp_path / "show-run.json").read_text(encoding="utf-8")) == {
        "id": "show-run.json"}
    assert leftovers(tmp_path) == []


def test_a_cut_before_the_replace_keeps_the_units_old_run_record(tmp_path,
                                                                 monkeypatch):
    from ui.showplay import ShowPlayer
    holder = SimpleNamespace(store=tmp_path)
    ShowPlayer._write(holder, "show-run.json", {"show": "old"})

    def cut(src, dst):
        raise OSError("power cut")
    monkeypatch.setattr(durable.os, "replace", cut)
    with pytest.raises(OSError):
        ShowPlayer._write(holder, "show-run.json", {"show": "new"})
    monkeypatch.undo()
    assert json.loads((tmp_path / "show-run.json").read_text(
        encoding="utf-8")) == {"show": "old"}
    assert leftovers(tmp_path) == []


def test_a_demo_goes_through_the_helper(tmp_path, monkeypatch):
    from tests.test_showplay import make_show
    from ui.demos import DemoStore
    store = DemoStore(tmp_path)
    calls = spy_on(monkeypatch, "atomic_write_json")
    slug = store.save("PARIS", make_show())
    assert [Path(args[0]).name for args, _ in calls] == [
        f"{slug}.json", f"{slug}.meta.json"]
    assert leftovers(tmp_path) == []


def test_a_flash_record_goes_through_the_helper(tmp_path, monkeypatch):
    from ui import flashlog
    calls = spy_on(monkeypatch, "atomic_write_json")
    path = tmp_path / "state" / "flash-log.json"
    flashlog.record("SERIAL", 1, "FW/x.bin", 10, 0x1234, path=path, when=1.0)
    (args, kwargs), = calls
    assert Path(args[0]) == path and kwargs == {"indent": 1, "sort_keys": True}
    assert flashlog.lookup("SERIAL", path)["crc"] == 0x1234


def test_runlog_fsyncs_its_file_and_folder(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("runlog_under_test",
                                                  ROOT / "raspi" / "runlog.py")
    runlog = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runlog)
    synced = []
    real = os.fsync

    def spy(fd):
        synced.append(fd)
        try:
            return real(fd)
        except OSError:
            return None                 # Windows: no fsync of a folder handle
    monkeypatch.setattr(runlog.os, "fsync", spy)
    path = tmp_path / "latest.txt"
    runlog.write_atomic(path, "cycle 1\n")
    assert path.read_text() == "cycle 1\n"
    assert len(synced) >= 1


# ---- readers: an empty or torn file from an OLDER version ----

TORN = ("", "   \n", '{"show": "abc", "sta')


@pytest.mark.parametrize("text", TORN)
def test_a_torn_show_or_history_opens_as_a_new_show(tmp_path, capsys, text):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "show.json").write_text(text, encoding="utf-8")
    (root / "history.json").write_text(text, encoding="utf-8")
    ws = Workspace(root)
    assert ws._load_show() == {}
    assert ws._load_history() == {"undo": [], "redo": []}
    ws.set_timeline(600, [])                           # and it is usable again
    assert json.loads((root / "show.json").read_text(encoding="utf-8"))
    err = capsys.readouterr().err
    assert "show.json: unreadable" in err and "history.json: unreadable" in err


@pytest.mark.parametrize("text", TORN)
def test_a_torn_fleet_json_reads_as_none(tmp_path, capsys, text):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "fleet.json").write_text(text, encoding="utf-8")
    ws = Workspace(root)
    units, token = ws.fleet_config()
    assert units and token is None
    assert ws.fleet_option("passcode") is None
    assert "fleet.json: unreadable" in capsys.readouterr().err


@pytest.mark.parametrize("text", ["", "  \n"])
def test_an_empty_fleet_json_is_written_anew_by_set_fleet_option(tmp_path, text):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "fleet.json").write_text(text, encoding="utf-8")
    ws = Workspace(root)
    ws.set_fleet_option("speaker_volume", 40)
    assert json.loads((root / "fleet.json").read_text(encoding="utf-8")) == {
        "speaker_volume": 40}


def test_a_torn_but_not_empty_fleet_json_is_still_never_overwritten(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "fleet.json").write_text('{"passcode": "pc", "spe', encoding="utf-8")
    ws = Workspace(root)
    with pytest.raises(RuntimeError, match="does not parse"):
        ws.set_fleet_option("speaker_volume", 40)
    assert (root / "fleet.json").read_text(encoding="utf-8") == '{"passcode": "pc", "spe'


def _reborn(store):
    from tests.test_ui_remote import make_session
    from ui.showplay import ShowPlayer
    session, runner, _ = make_session()
    return ShowPlayer(session, store=store, tick_s=0.02), runner


@pytest.mark.parametrize("broken", ["show.json", "show-run.json"])
@pytest.mark.parametrize("text", TORN + ("[]", "null"))
def test_a_torn_show_or_run_record_restores_as_nothing_loaded(tmp_path, capsys,
                                                              broken, text):
    from tests.test_showplay import make_show
    show = make_show(duration=600)
    good = {"show.json": show,
            "show-run.json": {"show": show["id"], "state": "running",
                              "applied": "q01", "demo": False,
                              "t0_wall": time.time() - 1.0}}
    for name, payload in good.items():
        (tmp_path / name).write_text(json.dumps(payload), encoding="utf-8")
    (tmp_path / broken).write_text(text, encoding="utf-8")
    player, runner = _reborn(tmp_path)
    try:
        player.restore()                    # never raises at boot
        assert player.show is None and not player.restored_running
        assert f"{broken}: unreadable" in capsys.readouterr().err
    finally:
        player.close()
        runner.stop()


@pytest.mark.parametrize("text", TORN + ("[]",))
def test_a_torn_burn_record_is_no_burn_and_a_running_show_is_not_resumed(
        tmp_path, capsys, text):
    from tests.test_showplay import make_show
    from ui.showplay import BURN_FILE, LOADED
    show = make_show(duration=600)
    (tmp_path / "show.json").write_text(json.dumps(show), encoding="utf-8")
    (tmp_path / "show-run.json").write_text(json.dumps({
        "show": show["id"], "state": "running", "applied": "q01",
        "demo": False, "t0_wall": time.time() - 1.0}), encoding="utf-8")
    (tmp_path / BURN_FILE).write_text(text, encoding="utf-8")
    player, runner = _reborn(tmp_path)
    try:
        player.restore()
        assert player.show is not None and player.state == LOADED
        assert player._burn_disk is None
        assert "no record of its pictures" in player.note
        if text != "[]":
            assert f"{BURN_FILE}: unreadable" in capsys.readouterr().err
    finally:
        player.close()
        runner.stop()


@pytest.mark.parametrize("text", TORN)
def test_torn_demo_files_are_skipped_or_rebuilt_never_a_crash(tmp_path, capsys,
                                                              text):
    from tests.test_showplay import make_show
    from ui.demos import DemoStore
    from ui.remote import RemoteError
    store = DemoStore(tmp_path)
    whole = store.save("WHOLE", make_show())
    torn = store.save("TORN", make_show())
    (tmp_path / f"{whole}.meta.json").write_text(text, encoding="utf-8")
    (tmp_path / f"{torn}.json").write_text(text, encoding="utf-8")
    (tmp_path / f"{torn}.meta.json").unlink()
    fresh = DemoStore(tmp_path)                       # as after a reboot
    listed = [d["slug"] for d in fresh.list()]
    assert listed == [whole]                          # its sidecar rebuilt
    assert json.loads((tmp_path / f"{whole}.meta.json").read_text(
        encoding="utf-8"))["slug"] == whole
    with pytest.raises(RemoteError):
        fresh.load(torn)
    err = capsys.readouterr().err
    assert f"{whole}.meta.json: unreadable" in err and f"{torn}.json: unreadable" in err


@pytest.mark.parametrize("text", TORN)
def test_a_torn_flash_log_reads_as_empty_and_is_replaced_whole(tmp_path, capsys,
                                                               text):
    from ui import flashlog
    path = tmp_path / "flash-log.json"
    path.write_text(text, encoding="utf-8")
    assert flashlog.load(path) == {}
    assert "flash-log.json: unreadable" in capsys.readouterr().err
    flashlog.record("S", 1, "FW/x.bin", 1, 2, path=path, when=1.0)
    assert set(flashlog.load(path)) == {"S"}


# ---- review of 411fd10: short temp names, the template never empty ----

def test_a_long_name_gets_a_short_temp_name(tmp_path, monkeypatch):
    long_name = "AZ271SD1306_" + "x" * 200 + "_grid.csv"         # 221 characters
    seen = []
    real = os.replace

    def spy(src, dst):
        seen.append(Path(src).name)
        return real(src, dst)
    monkeypatch.setattr(durable.os, "replace", spy)
    durable.atomic_write_text(tmp_path / long_name, "a,b\n", newline="")
    (temp,) = seen
    assert len(temp) <= 1 + 40 + 1 + 12 + 4 and temp.startswith(".AZ271SD1306_")
    assert (tmp_path / long_name).read_text(encoding="utf-8") == "a,b\n"


def test_create_writes_once_and_never_over_anything(tmp_path, fsyncs, dir_syncs):
    path = tmp_path / "fleet.json"
    assert durable.atomic_create_text(path, '{"a": 1}') is True
    assert len(fsyncs) == 1 and dir_syncs == [tmp_path]
    assert durable.atomic_create_text(path, '{"b": 2}') is False
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 1}
    assert leftovers(tmp_path) == []


def test_a_cut_before_the_link_leaves_no_file_at_all_never_an_empty_one(
        tmp_path, monkeypatch):
    path = tmp_path / "fleet.json"

    def cut(src, dst):
        raise KeyboardInterrupt("power cut")         # nothing after this runs
    monkeypatch.setattr(durable.os, "link", cut)
    with pytest.raises(KeyboardInterrupt):
        durable.atomic_create_text(path, '{"a": 1}')
    assert not path.exists() and leftovers(tmp_path) == []


def test_create_without_hard_links_falls_back_to_an_exclusive_create(tmp_path,
                                                                    monkeypatch):
    path = tmp_path / "fleet.json"

    def no_links(src, dst):
        raise OSError(1, "Operation not permitted")
    monkeypatch.setattr(durable.os, "link", no_links)
    assert durable.atomic_create_text(path, '{"a": 1}') is True
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 1}
    assert durable.atomic_create_text(path, '{"b": 2}') is False
    assert leftovers(tmp_path) == []


# ---- review of 411fd10: the unit's records are written OFF the player's lock ----

class Slow:
    """A write that takes `delay` seconds (an SD card's fsync tail), and
    records what it wrote, in order."""

    def __init__(self, real, delay):
        import threading
        self.real, self.delay = real, delay
        self.names, self.payloads = [], []
        self.started = threading.Event()

    def __call__(self, name, payload):
        self.started.set()
        time.sleep(self.delay)
        self.names.append(name)
        self.payloads.append(payload)
        return self.real(name, payload)


def _player(store):
    from tests.test_ui_remote import make_session
    from ui.showplay import ShowPlayer
    session, runner, _ = make_session()
    return ShowPlayer(session, store=store, tick_s=0.02), runner


def wait_for(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_status_is_not_blocked_while_a_record_is_being_written(tmp_path):
    from tests.test_showplay import make_show, wait_burned
    player, runner = _player(tmp_path)
    try:
        player.load(make_show(duration=60))
        assert wait_burned(player) and player.flush(timeout=5)
        slow = Slow(player._write, 1.0)
        player._write = slow
        with player._lock:                     # e.g. _plan()'s per-cue verdict
            player._persist()
        assert slow.started.wait(2)
        began = time.monotonic()
        assert player.status() is not None
        assert player._lock.acquire(timeout=0.1)       # the lock is free
        player._lock.release()
        assert time.monotonic() - began < 0.3, "status() waited on the card"
        assert player.flush(timeout=5) and slow.names == ["show-run.json"]
    finally:
        player.close()
        runner.stop()


def test_only_the_latest_snapshot_of_a_file_is_written(tmp_path):
    import threading
    from ui.showplay import _DiskWriter
    gate = threading.Event()
    written = []

    def write(name, payload):
        written.append((name, payload))
        gate.wait(5)                          # the first write is slow

    writer = _DiskWriter(tmp_path, write, lambda *a: None)
    try:
        writer.submit([("show-run.json", {"n": 1})])
        assert wait_for(lambda: written)
        for n in (2, 3, 4):                    # queued behind it
            writer.submit([("show-run.json", {"n": n})])
        gate.set()
        assert writer.wait(timeout=5)
        assert [p["n"] for _, p in written] == [1, 4]
    finally:
        writer.close(timeout=5)


def test_a_load_writes_burn_delete_then_run_record_then_show_whatever_was_queued(
        tmp_path):
    import threading
    from ui.showplay import BURN_FILE, _DELETE, _DiskWriter
    gate = threading.Event()
    order = []

    def write(name, payload):
        gate.wait(5)

    writer = _DiskWriter(tmp_path, write, lambda *a: None)
    real_batch = writer._write_batch

    def spy(batch):
        order.append([(name, "DELETE" if p is _DELETE else p.get("v"))
                      for name, p in batch])
        return real_batch(batch)
    writer._write_batch = spy
    try:
        writer.submit([("busy.json", {"v": 0})])          # the card is busy...
        assert wait_for(lambda: order)
        writer.submit([("show-run.json", {"v": "X"})])     # ...a state change,
        writer.submit([(BURN_FILE, {"v": "W"})], to_end=True)   # a burn record,
        writer.submit([(BURN_FILE, _DELETE), ("show-run.json", {"v": "A"}),
                       ("show.json", {"v": "S"})], to_end=True)  # then a load
        writer.submit([("show-run.json", {"v": "B"})])     # and a change after it
        gate.set()
        assert writer.wait(timeout=5)
        # The load's order holds, and the later run record replaced the
        # queued one IN PLACE - it never overtakes the show.json behind it.
        assert order[1] == [(BURN_FILE, "DELETE"), ("show-run.json", "B"),
                            ("show.json", "S")]
    finally:
        writer.close(timeout=5)


def test_load_answers_with_its_files_on_disk_in_order(tmp_path):
    from tests.test_showplay import make_show, wait_burned
    from ui.showplay import BURN_FILE
    player, runner = _player(tmp_path)
    try:
        player.load(make_show(duration=60))
        assert wait_burned(player) and player.flush(timeout=5)
        assert (tmp_path / BURN_FILE).exists()
        spy = Slow(player._write, 0.05)
        player._write = spy
        player.load(dict(make_show(duration=60), id="second0001"))
        # load() returned: both files are on disk, run record first, and
        # the first show's burn record is gone (deleted before them).
        assert spy.names[:2] == ["show-run.json", "show.json"]
        assert spy.payloads[0]["show"] == "second0001"
        assert json.loads((tmp_path / "show.json").read_text(
            encoding="utf-8"))["id"] == "second0001"
        assert not (tmp_path / BURN_FILE).exists() or json.loads(
            (tmp_path / BURN_FILE).read_text(encoding="utf-8"))["burned"] == "second0001"
    finally:
        player.close()
        runner.stop()


def test_close_writes_what_is_queued(tmp_path):
    from tests.test_showplay import make_show
    player, runner = _player(tmp_path)
    try:
        player.load(make_show(duration=60), demo=True, name="X", slug="x")
        player._write = Slow(player._write, 0.3)
        with player._lock:
            player.demo_name = "LATEST"
            player._persist()
    finally:
        player.close()                         # flushes before it returns
        runner.stop()
    run = json.loads((tmp_path / "show-run.json").read_text(encoding="utf-8"))
    assert run["demo_name"] == "LATEST"


def test_a_write_error_is_said_once_and_never_raised_into_the_player(tmp_path,
                                                                     capsys):
    from tests.test_showplay import make_show
    player, runner = _player(tmp_path)
    try:
        player.load(make_show(duration=60))
        assert player.flush(timeout=5)

        def full(name, payload):
            raise OSError("no space left on device")
        player._write = full
        for _ in range(3):
            with player._lock:
                player._persist()                  # never raises
            assert player.flush(timeout=5)
        err = capsys.readouterr().err
        lines = [line for line in err.splitlines()
                 if "could not write" in line and "show-run.json" in line]
        assert len(lines) == 1 and "no space left" in lines[0]
        assert "cannot save the show" in player.note
        # No record is the honest state after a run record that would
        # not go: restore() then waits for the PC.
        assert not (tmp_path / "show-run.json").exists()
    finally:
        player.close()
        runner.stop()


# ---- final review of 669979b: never burn over a burn record still on disk ----

def _loaded_and_burned(tmp_path):
    from tests.test_showplay import make_show, wait_burned
    from ui.showplay import BURN_FILE
    player, runner = _player(tmp_path)
    player.load(make_show(duration=60))
    assert wait_burned(player) and player.flush(timeout=5)
    assert (tmp_path / BURN_FILE).exists()
    burns = []
    real_burn = player.session.burn

    def spy(*args, **kwargs):
        burns.append(args)
        return real_burn(*args, **kwargs)
    player.session.burn = spy
    return player, runner, burns


def test_a_load_the_card_does_not_take_in_time_starts_no_burn(tmp_path,
                                                              monkeypatch):
    from tests.test_showplay import make_show
    from ui import showplay
    from ui.remote import RemoteError
    monkeypatch.setattr(showplay, "LOAD_WRITE_WAIT_S", 0.2)
    player, runner, burns = _loaded_and_burned(tmp_path)
    try:
        player._write = Slow(player._write, 1.0)           # a stalled card
        with pytest.raises(RemoteError, match="SD card did not take the show"):
            player.load(dict(make_show(duration=60), id="second0001"))
        assert burns == []                               # nothing onto the slots
        assert "Upload again" in player.note
        burn = player.status()["burn"]
        assert burn["state"] == "none", burn
        with pytest.raises(RemoteError, match="SD card did not take the show"):
            player.run(time.monotonic() + 5)             # START is refused too
    finally:
        player.close()
        runner.stop()


def test_a_burn_record_that_cannot_be_deleted_starts_no_burn(tmp_path):
    from tests.test_showplay import make_show
    from ui.remote import RemoteError
    from ui.showplay import BURN_FILE
    player, runner, burns = _loaded_and_burned(tmp_path)
    try:
        # A record the disk thread cannot unlink (a folder in its place).
        (tmp_path / BURN_FILE).unlink()
        (tmp_path / BURN_FILE).mkdir()
        (tmp_path / BURN_FILE / "keep").write_text("x", encoding="utf-8")
        with pytest.raises(RemoteError, match="cannot clear the burn record"):
            player.load(dict(make_show(duration=60), id="second0001"))
        assert burns == [] and "cannot clear the burn record" in player.note
    finally:
        player.close()
        runner.stop()


def test_a_burn_record_never_replaces_a_queued_delete(tmp_path):
    import threading
    from ui.showplay import BURN_FILE, _DELETE, _DiskWriter
    gate = threading.Event()
    order = []

    def write(name, payload):
        gate.wait(5)

    writer = _DiskWriter(tmp_path, write, lambda *a: None)
    real_batch = writer._write_batch

    def spy(batch):
        order.append([(name, "DELETE" if p is _DELETE else p.get("v"))
                      for name, p in batch])
        return real_batch(batch)
    writer._write_batch = spy
    try:
        writer.submit([("busy.json", {"v": 0})])
        assert wait_for(lambda: order)
        writer.submit([(BURN_FILE, _DELETE), ("show-run.json", {"v": "A"})],
                      to_end=True)                      # load()'s delete...
        writer.submit([(BURN_FILE, {"v": "W1"})], to_end=True)   # ...a poll's record
        writer.submit([(BURN_FILE, {"v": "W2"})])                # ...and another
        gate.set()
        assert writer.wait(timeout=5)
        assert order[1] == [(BURN_FILE, "DELETE"), ("show-run.json", "A"),
                            (BURN_FILE, "W2")]
    finally:
        writer.close(timeout=5)


def test_the_disk_thread_never_dies(tmp_path, monkeypatch):
    from ui.showplay import _DiskWriter
    written = []
    writer = _DiskWriter(tmp_path, lambda name, payload: written.append(name),
                         lambda *a: (_ for _ in ()).throw(RuntimeError("cb")))
    try:
        real_batch = writer._write_batch
        calls = {"n": 0}

        def broken_once(batch):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("a bug in the writer")
            return real_batch(batch)
        writer._write_batch = broken_once
        writer.submit([("a.json", {})])
        assert writer.wait(timeout=5)                    # not hung
        writer._write_batch = real_batch

        def failing(name, payload):
            raise OSError("EIO")
        writer._write = failing
        writer._log = lambda *a: (_ for _ in ()).throw(ValueError("stderr closed"))
        writer.submit([("b.json", {})])                  # log AND callback raise
        assert writer.wait(timeout=5)
        writer._write = lambda name, payload: written.append(name)
        writer.submit([("c.json", {})])
        assert writer.wait(timeout=5) and written == ["c.json"]
        assert writer._thread.is_alive()
    finally:
        writer.close(timeout=5)


def test_sigterm_unwinds_as_an_exception_so_the_player_is_closed():
    import signal
    from ui import main as ui_main
    previous = ui_main.install_sigterm_exit()
    try:
        assert signal.getsignal(signal.SIGTERM) is ui_main._exit_on_sigterm
        with pytest.raises(SystemExit):
            ui_main._exit_on_sigterm(signal.SIGTERM, None)
        closed = []
        with pytest.raises(ui_main.Terminated):
            try:
                ui_main._exit_on_sigterm(signal.SIGTERM, None)  # inside app.run
            finally:
                closed.append(True)                             # main()'s finally
        assert closed == [True]
    finally:
        signal.signal(signal.SIGTERM, previous if previous is not None
                      else signal.SIG_DFL)
