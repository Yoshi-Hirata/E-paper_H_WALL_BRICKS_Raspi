"""The exhibition Conductor on the PC as a SEPARATE application (2026-10-01):
the same `conductor` package as the show's, started by its own launcher on
its own port with its own workspace folder and an amber badge on the page,
so the operator never mistakes one window - or one data set - for the other.

* Start Exhibition Conductor.bat / Backup exhibition-data.bat exist and carry
  the expected flags and folder; exhibition-data/ is git-ignored;
* `serve --label` reaches serve(), is tidied, and /api/state carries `label`
  and `workspace_name`; without it `label` is null;
* a labelled serve() writes PC_FLEET_TEMPLATE into an empty workspace once -
  hotspot radxa-05, no passcode, no adopt, no speaker - and never overwrites
  a fleet.json that is there (even a broken one); an unlabelled serve()
  writes nothing;
* the page (a headless browser against a real make_server()): with a label
  the badge sits right after the app name, the title starts with the label
  and the tab gets an amber icon; without one the page is exactly as before.

The browser half is behind CONDUCTOR_BROWSER_TESTS=1, like the rest.
"""
from __future__ import annotations

import json
import re
import threading
import urllib.request
from pathlib import Path

import pytest

from conductor import __main__ as cli
from conductor.server import (LABEL_MAX, PC_FLEET_TEMPLATE, clean_label,
                              make_server, write_fleet_template)
from tests.test_conductor_board import _free_port
from tests.test_designer_build import _dump_dom, _require_browser

REPO = Path(__file__).resolve().parents[1]
START_SHOW = REPO / "Start Conductor.bat"
START_EXHIBITION = REPO / "Start Exhibition Conductor.bat"
BACKUP_SHOW = REPO / "Backup showdata.bat"
BACKUP_EXHIBITION = REPO / "Backup exhibition-data.bat"


def _lines_of(path):
    return path.read_text(encoding="utf-8").splitlines()


def _command_line(path):
    """The one line that starts the Conductor."""
    lines = [line for line in _lines_of(path) if "-m conductor serve" in line]
    assert len(lines) == 1, lines
    return lines[0]


# ------------------------------------------------------------ 1. launchers

def test_the_exhibition_launcher_is_its_own_application():
    assert START_EXHIBITION.is_file(), "Start Exhibition Conductor.bat is missing"
    command = _command_line(START_EXHIBITION)
    for flag in ("--workspace exhibition-data", "--port 8766", "--label EXHIBITION",
                 "--open"):
        assert flag in command, command
    assert command.startswith("%PY% -m conductor serve"), command
    assert "title E-paper Exhibition Conductor" in _lines_of(START_EXHIBITION)
    text = START_EXHIBITION.read_text(encoding="utf-8")
    assert "radxa-05" in text and "Send" in text, "the comment must say what it is for"
    assert "cd /d \"%~dp0\"" in text and "if errorlevel 1 pause" in text
    # The same Python detection as the show's launcher, word for word.
    show = START_SHOW.read_text(encoding="utf-8")
    probe = show[show.index("set PY=python"):show.index("%PY% -m conductor")]
    assert probe in text, "the Python detection drifted from Start Conductor.bat"
    # ...and the show's own launcher is untouched: no label, no port, no folder.
    command = _command_line(START_SHOW)
    assert "--label" not in command and "8766" not in command \
        and "exhibition" not in show.lower()
    assert "title E-paper Show Conductor" in _lines_of(START_SHOW)


def test_the_exhibition_backup_mirrors_the_shows():
    assert BACKUP_EXHIBITION.is_file(), "Backup exhibition-data.bat is missing"
    text = BACKUP_EXHIBITION.read_text(encoding="utf-8")
    assert 'if not exist "exhibition-data\\"' in text
    assert "set OUT=..\\exhibition-data-%STAMP%.zip" in text
    assert "Compress-Archive -Path 'exhibition-data' -DestinationPath '%OUT%' -Force" in text
    assert "title Backup exhibition-data" in _lines_of(BACKUP_EXHIBITION)
    # Never the show's folder, in either direction: the one path that is
    # zipped, and the one name the zip gets, are the exhibition's.
    assert "-Path 'showdata'" not in text and "showdata-%STAMP%" not in text
    show = BACKUP_SHOW.read_text(encoding="utf-8")
    assert "exhibition-data" not in show
    # The stamp and the pause-on-failure shape are the show's, word for word.
    stamp = show[show.index("for /f"):show.index("set OUT=")]
    assert stamp in text


def test_exhibition_data_is_git_ignored_like_showdata():
    ignored = [line.strip() for line in _lines_of(REPO / ".gitignore")]
    assert "showdata/" in ignored and "exhibition-data/" in ignored


# ---------------------------------------------------------- 2. --label

def test_label_reaches_serve_and_is_tidied(monkeypatch):
    import conductor.server as srv

    calls = {}
    monkeypatch.setattr(srv, "serve", lambda *a, **kw: calls.update(kw) or 0)
    assert cli.main(["serve"]) == 0
    assert calls["label"] is None
    assert cli.main(["serve", "--workspace", "exhibition-data", "--port", "8766",
                     "--label", "EXHIBITION"]) == 0
    assert calls["label"] == "EXHIBITION"
    assert clean_label(None) is None and clean_label("") is None
    assert clean_label("   ") is None
    assert clean_label("  EXHIBITION\n") == "EXHIBITION"
    assert clean_label("PARIS\tSS26  demo") == "PARIS SS26 demo"
    assert clean_label("x" * 100) == "x" * LABEL_MAX
    assert LABEL_MAX <= 32, "it sits in the top bar and the tab's title"


def _state(port):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=5) as r:
        return json.loads(r.read())


def test_api_state_carries_the_label_and_the_folder_name(tmp_path):
    server = make_server(tmp_path / "exhibition-data", port=0, label="EXHIBITION")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        state = _state(server.server_address[1])
        assert state["label"] == "EXHIBITION"
        assert state["workspace_name"] == "exhibition-data"
        assert Path(state["workspace"]) == (tmp_path / "exhibition-data").resolve()
    finally:
        server.shutdown()
        server.server_close()
    # The show PC's own: null, the same folder-name key, the same path key.
    server = make_server(tmp_path / "showdata", port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        state = _state(server.server_address[1])
        assert state["label"] is None
        assert state["workspace_name"] == "showdata"
        assert "workspace" in state
    finally:
        server.shutdown()
        server.server_close()


# ------------------------------------------------- 3. the fleet.json template

def test_the_pc_template_is_hotspot_only():
    assert PC_FLEET_TEMPLATE["hotspot"] == "radxa-05"
    for never in ("passcode", "adopt", "speaker_volume", "units", "token"):
        assert never not in PC_FLEET_TEMPLATE, never
    assert set(PC_FLEET_TEMPLATE) == {"_comment", "hotspot"}
    assert "passcode" in PC_FLEET_TEMPLATE["_comment"], \
        "the comment must say where radxa-05's passcode goes for Send"


def test_write_fleet_template_writes_once_and_never_over_anything(tmp_path):
    root = tmp_path / "exhibition-data"
    root.mkdir()
    path = root / "fleet.json"
    assert write_fleet_template(root) is True
    assert json.loads(path.read_text(encoding="utf-8")) == PC_FLEET_TEMPLATE
    assert not path.with_name("fleet.json.tmp").exists()
    # The operator's edits (the passcode for Send) stay.
    path.write_text('{"hotspot": "radxa-05", "passcode": "mine"}', encoding="utf-8")
    assert write_fleet_template(root) is False
    assert json.loads(path.read_text(encoding="utf-8"))["passcode"] == "mine"
    # A broken file is still the operator's: not replaced by the template.
    path.write_text("{not json", encoding="utf-8")
    assert write_fleet_template(root) is False
    assert path.read_text(encoding="utf-8") == "{not json"


def _serve_once(monkeypatch, capsys, workspace, **kw):
    """serve() up to serve_forever(), with no unit anywhere on the fleet
    (never the real 192.168.51.10x) and no browser."""
    import conductor.server as srv

    class Once(srv._Server):
        def serve_forever(self, poll_interval=0.5):
            pass

    monkeypatch.setattr(srv, "_Server", Once)
    monkeypatch.setattr(srv, "already_serving", lambda port: False)
    monkeypatch.setattr(srv, "default_units", lambda: {})
    assert srv.serve(workspace, port=0, **kw) == 0
    return capsys.readouterr().out


def test_a_labelled_serve_makes_the_folder_and_the_template_once(tmp_path, monkeypatch, capsys):
    root = tmp_path / "exhibition-data"
    assert not root.exists()
    out = _serve_once(monkeypatch, capsys, root, label="EXHIBITION")
    assert (root / "files").is_dir(), "the workspace folder is made as always"
    written = json.loads((root / "fleet.json").read_text(encoding="utf-8"))
    assert written == PC_FLEET_TEMPLATE
    assert "wrote" in out and "fleet.json" in out and "passcode" in out, out
    assert "label: EXHIBITION" in out and "EXHIBITION conductor UI:" in out, out
    # A second start: the file is the operator's now, and nothing is said.
    (root / "fleet.json").write_text('{"hotspot": "radxa-05", "passcode": "mine"}',
                                     encoding="utf-8")
    out = _serve_once(monkeypatch, capsys, root, label="EXHIBITION")
    assert json.loads((root / "fleet.json").read_text(encoding="utf-8"))["passcode"] == "mine"
    assert "wrote" not in out, out
    # ...and the passcode from the file is picked up as before (loopback
    # needs none, so it is only reported).
    assert "passcode: set" in out, out


def test_an_unlabelled_serve_writes_no_fleet_json(tmp_path, monkeypatch, capsys):
    root = tmp_path / "showdata"
    out = _serve_once(monkeypatch, capsys, root)
    assert (root / "files").is_dir()
    assert not (root / "fleet.json").exists(), "the show PC's workspace gained a fleet.json"
    assert "label:" not in out and out.startswith("conductor UI:"), out
    # An empty or blank label is no label.
    out = _serve_once(monkeypatch, capsys, root, label="   ")
    assert not (root / "fleet.json").exists()
    assert "label:" not in out and out.startswith("conductor UI:"), out


# --------------------------------------------------------------- 4. the page

def _serve_page(tmp_path, label):
    """A real Conductor (no fleet) on a free port, and its page as the
    browser drew it."""
    server = make_server(tmp_path / ("exhibition-data" if label else "showdata"),
                         port=_free_port(), label=label)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        return _dump_dom(f"http://127.0.0.1:{server.server_address[1]}/", tmp_path) or ""
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(scope="module")
def pages(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("labelpage")
    _require_browser(tmp)
    return {"labelled": _serve_page(tmp / "a", "EXHIBITION"),
            "plain": _serve_page(tmp / "b", None)}


def _title(dom):
    match = re.search(r"<title>(.*?)</title>", dom, re.S)
    return match.group(1) if match else None


def test_the_labelled_page_wears_the_badge_the_title_and_the_icon(pages):
    dom = pages["labelled"]
    assert _title(dom) == "EXHIBITION · Conductor", _title(dom)
    header = dom[dom.index("<header>"):dom.index("</header>")]
    badge = re.search(r'<h1>E-PAPER SHOW CONDUCTOR</h1>\s*<span id="app-label" '
                      r'class="app-label" title="([^"]*)">EXHIBITION</span>', header)
    assert badge, header[:600]
    assert "workspace exhibition-data" in badge.group(1), badge.group(1)
    assert "not the show's own" in badge.group(1)
    assert header.index('id="app-label"') < header.index('data-tab="items"'), \
        "the badge sits with the app name, before the tab strip"
    icon = re.search(r'<link id="app-icon" rel="icon" type="image/svg\+xml" '
                     r'href="data:image/svg\+xml,([^"]+)">', dom)
    assert icon, "no amber tab icon"
    assert "e0a200" in icon.group(1) and "%3Ctext" in icon.group(1), icon.group(1)
    assert "workspace: " in header and "exhibition-data" in header


def test_the_plain_page_is_what_it_always_was(pages):
    dom = pages["plain"]
    assert _title(dom) == "E-paper Show Conductor", _title(dom)
    assert 'id="app-label"' not in dom and 'class="app-label"' not in dom.split("</style>")[1]
    assert 'rel="icon"' not in dom
    header = dom[dom.index("<header>"):dom.index("</header>")]
    assert re.search(r"<h1>E-PAPER SHOW CONDUCTOR</h1>\s*<div class=\"tabs\">", header), \
        header[:400]
