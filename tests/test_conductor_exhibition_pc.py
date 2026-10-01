"""The exhibition Conductor on the PC as a SEPARATE application (2026-10-01):
the same `conductor` package as the show's, started by its own launcher on
its own port with its own workspace folder and an amber badge on the page,
so the operator never mistakes one window - or one data set - for the other.

* Start Exhibition Conductor.bat / Backup exhibition-data.bat exist and carry
  the expected flags and folder; exhibition-data/ is git-ignored;
* `serve --label` reaches serve(), is tidied, and /api/state carries `label`
  and `workspace_name`; without it `label` is null;
* a labelled serve() writes PC_FLEET_TEMPLATE whenever its workspace has no
  fleet.json - hotspot radxa-05, no passcode, no adopt, no speaker - and never
  overwrites one that is there (even a broken one, even one that appears in a
  race: open "x"); a write that fails is a warning, not a refusal; an
  unlabelled serve() writes nothing;
* the other launcher's Conductor (review of c176aa7, M1): an OPEN idle
  Conductor still acts on the units, so each Conductor watches the other
  launcher's port (8765 <-> 8766), warns on the console at start and carries
  `other_conductor` in /api/conductor and /api/state for the page's red line,
  which goes when the other closes; a DIFFERENT Conductor on this one's own
  port is said, not opened (L2);
* the page (a headless browser against a real make_server()): with a label
  the badge sits right after the app name, the title starts with the label
  and the tab gets an amber icon, and the red line appears with the warning;
  without a label (and no other Conductor) the page is exactly as before.

The browser half is behind CONDUCTOR_BROWSER_TESTS=1, like the rest.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from conductor import __main__ as cli
from conductor.server import (EXHIBITION_CONDUCTOR_PORT, LABEL_MAX, PC_FLEET_TEMPLATE,
                              SHOW_CONDUCTOR_PORT, OtherConductorWatch, clean_label,
                              conductor_info, make_server, other_conductor_port,
                              other_conductor_warning, write_fleet_template)
from tests.test_conductor_board import _free_port
from tests.test_designer_build import _dump_dom, _require_browser

REPO = Path(__file__).resolve().parents[1]
PAGE_SRC = (REPO / "conductor" / "web" / "index.html").read_text(encoding="utf-8")
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


def _get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
        return json.loads(r.read())


def test_api_state_carries_the_label_and_the_folder_name(tmp_path):
    server = make_server(tmp_path / "exhibition-data", port=0, label="EXHIBITION")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        state = _state(port)
        assert state["label"] == "EXHIBITION"
        assert state["workspace_name"] == "exhibition-data"
        assert state["other_conductor"] is None
        assert Path(state["workspace"]) == (tmp_path / "exhibition-data").resolve()
        # The identity document on its own, and what a probe makes of it.
        assert _get(port, "/api/conductor") == {
            "label": "EXHIBITION", "workspace_name": "exhibition-data",
            "workspace": state["workspace"], "port": port, "other_conductor": None}
        assert conductor_info(port) == {
            "port": port, "label": "EXHIBITION", "workspace_name": "exhibition-data",
            "workspace": state["workspace"]}
    finally:
        server.shutdown()
        server.server_close()
    # The show PC's own: null, the same folder-name key, the same path key.
    server = make_server(tmp_path / "showdata", port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        state = _state(port)
        assert state["label"] is None
        assert state["workspace_name"] == "showdata"
        assert "workspace" in state
        assert conductor_info(port)["label"] is None
    finally:
        server.shutdown()
        server.server_close()
    assert conductor_info(port) is None, "a closed Conductor is nobody"


# ------------------------------------------- 2b. the other launcher's Conductor

def test_each_launcher_watches_the_others_port_never_its_own():
    assert (SHOW_CONDUCTOR_PORT, EXHIBITION_CONDUCTOR_PORT) == (8765, 8766)
    assert other_conductor_port(8766, "EXHIBITION") == 8765
    assert other_conductor_port(8765, None) == 8766
    # Started on the other launcher's port by hand: still the other one.
    assert other_conductor_port(8765, "EXHIBITION") == 8766
    assert other_conductor_port(8766, None) == 8765
    # Any other port: the launcher's counterpart as usual.
    assert other_conductor_port(8800, None) == 8766
    assert other_conductor_port(8800, "X") == 8765
    text = other_conductor_warning({"port": 8765, "label": None, "workspace_name": "showdata"})
    assert text.startswith("another Conductor is running on port 8765 (workspace showdata) - close its black window (Ctrl+C) before Upload or START here")
    assert "[EXHIBITION]" in other_conductor_warning(
        {"port": 8766, "label": "EXHIBITION", "workspace_name": "exhibition-data"})


def test_the_watch_reports_the_other_conductor_while_it_answers_and_clears_after(tmp_path):
    answers = {"info": {"port": 8765, "label": None, "workspace_name": "showdata"}}
    probes = []

    def probe(port):
        probes.append(port)
        return answers["info"]

    class Bound:
        other_conductor = None

    watch = OtherConductorWatch(Bound, 8765, probe=probe, every_s=0.05)
    seen = watch.check()
    assert probes == [8765]
    assert seen == Bound.other_conductor
    assert seen["port"] == 8765 and seen["workspace_name"] == "showdata"
    assert seen["warning"].startswith("another Conductor is running on port 8765")
    watch.start()
    try:
        answers["info"] = None
        for _ in range(100):
            if Bound.other_conductor is None:
                break
            time.sleep(0.02)
        assert Bound.other_conductor is None, "the note must go when the other closes"
        answers["info"] = {"port": 8765, "label": None, "workspace_name": "showdata"}
        for _ in range(100):
            if Bound.other_conductor is not None:
                break
            time.sleep(0.02)
        assert Bound.other_conductor is not None, "...and come back when it is up again"
        # A probe that raises never ends the watch: the note is simply off.
        def boom(port):
            raise RuntimeError("no")
        watch.probe = boom
        for _ in range(100):
            if Bound.other_conductor is None:
                break
            time.sleep(0.02)
        assert Bound.other_conductor is None and watch.is_alive()
    finally:
        watch.stop()
        watch.join(timeout=2)
    assert not watch.is_alive()
    # No port to watch (own port is the only one): nothing, ever.
    none = OtherConductorWatch(Bound, None, probe=probe)
    assert none.check() is None and Bound.other_conductor is None


def test_serve_warns_at_start_and_the_page_can_read_it(tmp_path, monkeypatch, capsys):
    """The exhibition Conductor starting while the show's is up: the console
    line, and other_conductor in both documents."""
    import conductor.server as srv

    other = {"port": 8765, "label": None, "workspace_name": "showdata",
             "workspace": "C:/x/showdata"}
    asked = []

    def probe(port, timeout=2.0):
        asked.append(port)
        return other if port == 8765 else None

    monkeypatch.setattr(srv, "conductor_info", probe)
    monkeypatch.setattr(srv, "default_units", lambda: {})
    captured = {}

    class Once(srv._Server):
        def serve_forever(self, poll_interval=0.5):
            captured["other"] = self.RequestHandlerClass.other_conductor

    monkeypatch.setattr(srv, "_Server", Once)
    # port=0: an ephemeral port, never the real 8765 / 8766 (the show PC's
    # own Conductor may well be up while this runs). A labelled Conductor
    # on any port watches 8765; the probe is a stub either way.
    assert srv.serve(tmp_path / "exhibition-data", port=0, label="EXHIBITION") == 0
    out = capsys.readouterr().out
    assert "WARNING: another Conductor is running on port 8765 (workspace showdata)" in out
    assert "close its black window (Ctrl+C) before Upload or START here" in out
    assert asked == [8765], asked               # port 0 is nobody's: only the other
    assert captured["other"]["port"] == 8765 and "warning" in captured["other"]
    # The show's own Conductor starting while the exhibition's is up: the
    # mirror image, on the console it reads with the label.
    other.update(port=8766, label="EXHIBITION", workspace_name="exhibition-data")
    probe2 = lambda port, timeout=2.0: other if port == 8766 else None  # noqa: E731
    monkeypatch.setattr(srv, "conductor_info", probe2)
    assert srv.serve(tmp_path / "showdata", port=0) == 0
    out = capsys.readouterr().out
    assert "WARNING: another Conductor is running on port 8766 (workspace exhibition-data) [EXHIBITION]" in out
    # Nobody on the other port: no warning at all.
    monkeypatch.setattr(srv, "conductor_info", lambda port, timeout=2.0: None)
    assert srv.serve(tmp_path / "showdata", port=0) == 0
    assert "WARNING" not in capsys.readouterr().out


def test_the_documents_carry_other_conductor_over_http(tmp_path):
    server = make_server(tmp_path / "exhibition-data", port=0, label="EXHIBITION")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        handler = server.RequestHandlerClass
        watch = OtherConductorWatch(handler, 8765, probe=lambda p: {
            "port": 8765, "label": None, "workspace_name": "showdata"})
        watch.check()
        for path in ("/api/conductor", "/api/state"):
            other = _get(port, path)["other_conductor"]
            assert other["port"] == 8765 and other["workspace_name"] == "showdata"
            assert other["warning"].startswith("another Conductor is running on port 8765")
        watch.probe = lambda p: None
        watch.check()
        assert _get(port, "/api/conductor")["other_conductor"] is None
        assert _get(port, "/api/state")["other_conductor"] is None
    finally:
        server.shutdown()
        server.server_close()


def test_a_different_conductor_on_this_port_is_said_not_opened(tmp_path, monkeypatch, capsys):
    """L2: the show's launcher finding the exhibition's Conductor on its
    port (a hand-started `serve --port 8765 --label ...`, say) must not
    open the wrong page; the same Conductor twice still just opens it."""
    import conductor.server as srv

    opened = []
    import webbrowser
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url))
    there = {"port": 8766, "label": "EXHIBITION", "workspace_name": "exhibition-data",
             "workspace": "x"}
    monkeypatch.setattr(srv, "conductor_info", lambda port, timeout=2.0: there)
    # The same launcher again: opens.
    assert srv.serve(tmp_path / "exhibition-data", port=8766, label="EXHIBITION",
                     open_browser=True) == 0
    assert "already running" in capsys.readouterr().out and opened == ["http://127.0.0.1:8766"]
    # Another label, or another folder, on that port: refused with the facts.
    assert srv.serve(tmp_path / "exhibition-data", port=8766, open_browser=True) == 2
    out = capsys.readouterr().out
    assert "a different Conductor is on port 8766 (label EXHIBITION, workspace exhibition-data)" in out
    assert "close it (its black window, Ctrl+C)" in out and "label none, workspace exhibition-data" in out
    assert srv.serve(tmp_path / "showdata", port=8766, label="EXHIBITION") == 2
    assert "workspace showdata" in capsys.readouterr().out
    assert opened == ["http://127.0.0.1:8766"], "the wrong page was opened"
    # An older Conductor (no workspace_name in its reply) cannot be told
    # apart: opened as before.
    older = {"port": 8765, "label": None, "workspace": "x"}
    monkeypatch.setattr(srv, "conductor_info", lambda port, timeout=2.0: dict(older))
    assert srv.serve(tmp_path / "anything", port=8765) == 0
    assert "already running" in capsys.readouterr().out


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
    # Exclusive create: a file that appears between the look and the write
    # is kept too (the template is fsynced under a temp name and then
    # hard-linked into place - os.link refuses an existing name, so there
    # is no look-then-write window; conductor/durable.py).
    path.unlink()
    from conductor import durable
    real_link = durable.os.link

    def racing_link(src, dst, *args, **kwargs):
        if str(dst) == str(path):
            path.write_text('{"passcode": "raced in"}', encoding="utf-8")
        return real_link(src, dst, *args, **kwargs)

    durable.os.link = racing_link
    try:
        assert write_fleet_template(root) is False
    finally:
        durable.os.link = real_link
    assert json.loads(path.read_text(encoding="utf-8")) == {"passcode": "raced in"}
    assert sorted(p.name for p in root.iterdir()) == ["fleet.json"]   # no temp left
    # A folder that cannot be written raises: the caller (serve) warns.
    with pytest.raises(OSError):
        write_fleet_template(tmp_path / "no-such-folder")


def test_a_template_that_cannot_be_written_is_a_warning_not_a_refusal(tmp_path, monkeypatch, capsys):
    import conductor.server as srv

    def refuse(root, template=None):
        raise PermissionError("read-only")

    monkeypatch.setattr(srv, "write_fleet_template", refuse)
    root = tmp_path / "exhibition-data"
    out = _serve_once(monkeypatch, capsys, root, label="EXHIBITION")
    assert "warning: could not write" in out and "fleet.json" in out and "read-only" in out
    assert "serving without it" in out
    assert "EXHIBITION conductor UI:" in out, "it still served"
    assert not (root / "fleet.json").exists()


def _serve_once(monkeypatch, capsys, workspace, **kw):
    """serve() up to serve_forever(), with no unit anywhere on the fleet
    (never the real 192.168.51.10x) and no browser."""
    import conductor.server as srv

    class Once(srv._Server):
        def serve_forever(self, poll_interval=0.5):
            pass

    monkeypatch.setattr(srv, "_Server", Once)
    # Nobody on any port - never a probe at the real 8765 / 8766.
    monkeypatch.setattr(srv, "conductor_info", lambda port, timeout=2.0: None)
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

def _serve_page(tmp_path, label, other=None):
    """A real Conductor (no fleet) on a free port, and its page as the
    browser drew it. `other` is what OtherConductorWatch would have found
    on the other launcher's port (set the way check() sets it - no probe
    of the real 8765 / 8766 is ever made here)."""
    server = make_server(tmp_path / ("exhibition-data" if label else "showdata"),
                         port=_free_port(), label=label)
    if other:
        OtherConductorWatch(server.RequestHandlerClass, other["port"],
                            probe=lambda port: other).check()
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
    return {"labelled": _serve_page(tmp / "a", "EXHIBITION",
                                    other={"port": 8765, "label": None,
                                           "workspace_name": "showdata"}),
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


def test_the_other_conductors_red_line_is_in_the_top_bar(pages):
    header = pages["labelled"][pages["labelled"].index("<header>"):pages["labelled"].index("</header>")]
    note = re.search(r'<div id="other-conductor" class="other-conductor" role="alert">(.*?)</div>',
                     header, re.S)
    assert note, header[-800:]
    text = note.group(1)
    assert text.startswith("⚠ another Conductor is running on port 8765 (workspace showdata)"), text
    assert "close its black window (Ctrl+C) before Upload or START here" in text
    # The page keeps asking on its own, on every tab, so the line goes when
    # the other Conductor is closed.
    assert "const OTHER_CONDUCTOR_POLL_MS = 5000;" in PAGE_SRC
    poll = PAGE_SRC[PAGE_SRC.index("async function pollOtherConductor()"):]
    poll = poll[:poll.index("\n}\n") + 3]
    assert 'api("/api/conductor")' in poll and "setTimeout(pollOtherConductor, OTHER_CONDUCTOR_POLL_MS)" in poll
    assert "paintOtherConductor(state.other_conductor);" in PAGE_SRC
    assert "if (!other) { if (note) note.remove(); return; }" in PAGE_SRC


def test_the_plain_page_is_what_it_always_was(pages):
    dom = pages["plain"]
    assert _title(dom) == "E-paper Show Conductor", _title(dom)
    assert 'id="app-label"' not in dom and 'class="app-label"' not in dom.split("</style>")[1]
    assert 'rel="icon"' not in dom
    assert 'id="other-conductor"' not in dom
    header = dom[dom.index("<header>"):dom.index("</header>")]
    assert re.search(r"<h1>E-PAPER SHOW CONDUCTOR</h1>\s*<div class=\"tabs\">", header), \
        header[:400]
    assert header.rstrip().endswith("</span>"), "nothing after the workspace line"
