"""Taking CSVs in on the Conductor page: what it refuses before reading a
byte, what it does with a dropped folder, and what it says happened.

One headless run of the REAL conductor/web/index.html against a REAL
Workspace (its /api/files is the intake path itself), so the whole chain
is under test: the page's pre-flight, the server's numbering/skipping,
and the toast that has to account for every file that was picked.

Behind CONDUCTOR_BROWSER_TESTS=1 like the other browser tests - skipped,
not failed, where no browser is installed. The plain-text checks above
the fixture run everywhere.
"""
from __future__ import annotations

import http.server
import json
import re
import socket
import sys
import threading
from html import unescape
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
INDEX_HTML = REPO / "conductor" / "web" / "index.html"

sys.path.insert(0, str(REPO))

from conductor.server import Workspace  # noqa: E402
from tests.test_designer_build import _dump_dom, _require_browser  # noqa: E402
from tests.test_look import GRID, MAP, SKIRT_MAP  # noqa: E402

PAGE = INDEX_HTML.read_text(encoding="utf-8")


# ------------------------------------------------------- the page as text

def test_a_picked_file_is_judged_by_its_name_before_it_is_read():
    """The one mistake this must not make: a dropped folder can hold a 2 GB
    video, and reading it as text to learn it is not a CSV freezes the tab
    (and, once sent, the conductor). The name check has to come first in the
    source, not merely somewhere in the function."""
    start = PAGE.index("async function readPickedFiles(")
    body = PAGE[start:PAGE.index("\n}", start)]
    assert body.index("/\\.csv$/i.test(macSafeName(f.name))") < body.index("await f.text()")
    assert body.index("isMacMetadata(f.name)") < body.index("await f.text()")


def _without_comments(source: str) -> str:
    """Line comments out - every check here is about what the code does,
    and the comments quote the very words being looked for."""
    return "\n".join(re.sub(r"//.*", "", line) for line in source.splitlines())


def test_the_drop_handler_reads_the_data_transfer_before_it_returns():
    # A DataTransfer's items are dead the moment the handler returns, so
    # filesFromDataTransfer() must be CALLED synchronously - an `await`
    # anywhere before it would empty a folder drop at random.
    # The LAST one: the first "drop" listener is the item-card drag, which
    # is not an upload at all.
    handler = PAGE[PAGE.rindex('addEventListener("drop"'):]
    handler = _without_comments(handler[:handler.index("\n});")])
    assert "await filesFromDataTransfer(e.dataTransfer)" in handler
    # The await on that line is fine - the CALL is made before it, and the
    # entries are out of the DataTransfer before the function's own first
    # await. An await on any EARLIER line is not.
    lines = handler.splitlines()
    reads = next(i for i, line in enumerate(lines)
                 if "filesFromDataTransfer" in line)
    assert not [line for line in lines[:reads] if "await" in line], \
        "something is awaited before the DataTransfer is read"


# ------------------------------------------------------- the page, running

def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _Stand:
    """index.html, a real Workspace behind /api/files and /api/state."""

    def __init__(self, tmp_path, probe, designs=1):
        self.ws = Workspace(tmp_path / "ws")
        self.ws.save("Look22_map.csv", MAP)
        for n in range(1, designs + 1):
            self.ws.save(f"Look22_color_pattern0{n}_grid.csv", GRID)
        # A second garment, with a map and no design of its own: the one
        # case where placing a cue has to be refused rather than guessed.
        self.ws.save("Skirt_map.csv", SKIRT_MAP)
        page = PAGE.replace("</body>", probe + "</body>", 1)
        workspace = self.ws

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _send(self, body, kind):
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, obj):
                self._send(json.dumps(obj).encode("utf-8"), "application/json")

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                if self.path == "/api/files":
                    item = body.get("item")
                    return self._json(workspace.intake(
                        body.get("files") or [],
                        str(item) if item else None))
                if self.path == "/api/show":
                    workspace.set_timeline(body.get("duration", 600),
                                           body.get("cues", []),
                                           body.get("refresh_s"))
                    return self._json({"ok": True})
                return self._json({})

            def do_GET(self):
                path = self.path.split("?")[0]
                if path in ("/", "/index.html"):
                    return self._send(page.encode("utf-8"),
                                      "text/html; charset=utf-8")
                if path == "/api/state":
                    return self._json(workspace.state())
                if path == "/api/fleet":
                    return self._json({"units": [], "last_fire": None,
                                       "run": None, "shows": {},
                                       "corrections": [], "prepared": {},
                                       "start_at": 0.0, "show_duration": 600.0,
                                       "timeline": None})
                if path == "/api/fleet/demos":
                    return self._json({"units": {}, "offline": [], "failed": {}})
                return self._json({})

        self.port = _free_port()
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", self.port),
                                                     Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


# One pass over the page, writing everything it saw into #intake-out. Files
# are plain {name, text()} objects rather than real File blobs on purpose:
# `text` is the thing being watched, and a spy says whether the page read a
# file it should have refused by its name alone.
_PROBE = """
<script>
(function () {
  var MAP = %(map)s, GRID = %(grid)s;
  var out = { error: null };
  function publish() {
    var pre = document.createElement("pre");
    pre.id = "intake-out";
    pre.textContent = JSON.stringify(out);
    document.body.appendChild(pre);
  }
  var wait = ms => new Promise(r => setTimeout(r, ms));
  var readNames = [];
  function pick(name, text) {
    return { name: name, text: function () { readNames.push(name); return Promise.resolve(text); } };
  }
  function toastNow() { return (document.querySelector("#toast") || {}).textContent || ""; }
  function designsOf(key) {
    var it = state.items.find(i => i.item === key);
    return it ? it.designs.map(d => d.name) : null;
  }
  function scalesOf(key) {
    var it = state.items.find(i => i.item === key);
    return it && it.map ? it.map.scales.length : null;
  }
  (async function () {
    try {
      await wait(400);
      out.booted = !!(state && state.items && state.items.length);

      // 1. Refused by NAME, before a byte is read: a video, a Mac's own
      //    clutter. Safari's ".csv.txt" is not refused - it IS a csv.
      readNames.length = 0;
      await upload([pick("clip.mp4", "binary"), pick("._Look22_map.csv", MAP),
                    pick(".DS_Store", "x"),
                    pick("Look22_color_fromsafari_grid.csv.txt", GRID)]);
      await wait(250);
      out.preflight = { read: readNames.slice(), toast: toastNow(),
                        files: designsOf("Look22") };

      // 2. The same bytes again: "already there", not a second copy.
      readNames.length = 0;
      await upload([pick("Look22_color_pattern01_grid.csv", GRID)]);
      await wait(250);
      out.samePickedTwice = { toast: toastNow(),
                              designs: designsOf("Look22").length };

      // 3. Different bytes under a name already here: numbered, and said.
      await upload([pick("Look22_color_pattern01_grid.csv", GRID.replace("0x03", "0x02"))]);
      await wait(250);
      out.numbered = { toast: toastNow(),
                       designs: designsOf("Look22") };

      // 4. A long refusal list is summarised, not spelled out.
      await upload(["a", "b", "c", "d", "e", "f"].map(n => pick(n + ".png", "x")));
      out.manyRefused = toastNow();

      // 5. A dropped FOLDER, with the CSVs one level down. The entries are
      //    the shape webkitGetAsEntry() hands over.
      function fileEntry(name, text) {
        return { isFile: true, isDirectory: false,
                 file: cb => cb(pick(name, text)) };
      }
      function dirEntry(children) {
        return { isFile: false, isDirectory: true, createReader: function () {
          var left = [children];
          return { readEntries: cb => cb(left.shift() || []) };
        } };
      }
      var dt = { items: [{ webkitGetAsEntry: () => dirEntry([
        dirEntry([fileEntry("Look22_color_deep_grid.csv", GRID)]),
        fileEntry("readme.txt", "hello")]) }] };
      var flat = await filesFromDataTransfer(dt);
      out.folderDrop = { picked: flat.map(f => f.name) };
      await upload(flat);
      await wait(250);
      out.folderDrop.toast = toastNow();
      out.folderDrop.designs = designsOf("Look22");

      // 6. A garment's own "Add CSV": the server renames the file onto
      //    that garment, and the toast says which garment it went to.
      await uploadOwn(itemByKey("Look22"), [pick("Whatever_color_own_grid.csv", GRID)]);
      await wait(250);
      out.perItem = { toast: toastNow(), designs: designsOf("Look22") };

      // 7. ...but never another garment's MAP, which would replace this
      //    garment's wiring with another garment's.
      await uploadOwn(itemByKey("Look22"), [pick("Skirt_map.csv", MAP)]);
      await wait(250);
      out.foreignMap = { toast: toastNow(), skirt: scalesOf("Skirt"),
                         look22: scalesOf("Look22") };
    } catch (e) { out.error = String((e && e.stack) || e); }
    publish();
  })();
})();
</script>
"""


@pytest.fixture(scope="module")
def intake(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("intake")
    _require_browser(tmp)
    probe = _PROBE % {"map": json.dumps(MAP), "grid": json.dumps(GRID)}
    stand = _Stand(tmp, probe)
    try:
        dom = _dump_dom(stand.url, tmp)
    finally:
        stand.close()
    match = re.search(r'<pre id="intake-out">(.*?)</pre>', dom or "", re.S)
    assert match, f"no #intake-out in the dumped DOM:\n{(dom or '')[:3000]}"
    data = json.loads(unescape(match.group(1)))
    assert data.get("error") is None, data["error"]
    assert data.get("booted"), "the page never loaded its state"
    return data


def test_a_video_and_a_macs_clutter_are_turned_away_unread(intake):
    pre = intake["preflight"]
    # The only file whose bytes were touched is the one that can be a CSV.
    assert pre["read"] == ["Look22_color_fromsafari_grid.csv.txt"], pre
    assert "clip.mp4" in pre["toast"] and "not a .csv file" in pre["toast"]
    # Safari's ".txt" came off and the design landed under its real name.
    assert "Look22_color_fromsafari_grid.csv" in pre["files"], pre


def test_the_same_file_picked_twice_is_already_there_not_a_second_design(intake):
    again = intake["samePickedTwice"]
    assert "already there" in again["toast"], again
    assert again["designs"] == 2, again      # pattern01 + the Safari one


def test_a_changed_design_of_a_name_already_here_is_numbered_and_named(intake):
    numbered = intake["numbered"]
    assert "Look22_color_pattern01-2_grid.csv" in numbered["designs"], numbered
    assert "Look22_color_pattern01_grid.csv" in numbered["designs"], numbered
    assert "saved as" in numbered["toast"] and "→" in numbered["toast"], numbered


def test_a_long_refusal_list_is_summarised(intake):
    toast = intake["manyRefused"]
    assert "6 refused" in toast, toast
    assert "+3 more" in toast, toast
    assert "f.png" not in toast, toast


def test_a_dropped_folder_is_walked_to_the_bottom(intake):
    folder = intake["folderDrop"]
    assert sorted(folder["picked"]) == ["Look22_color_deep_grid.csv",
                                        "readme.txt"], folder
    assert "Look22_color_deep_grid.csv" in folder["designs"], folder
    assert "readme.txt" in folder["toast"], folder


def test_a_garments_own_add_csv_says_which_garment_the_file_went_to(intake):
    per = intake["perItem"]
    assert "Look22_color_own_grid.csv" in per["designs"], per
    assert "imported to" in per["toast"], per
    assert "saved as" in per["toast"] and "→" in per["toast"], per


def test_another_garments_map_is_refused_on_the_page_too(intake):
    # The rename is the server's, so the refusal is too - but the
    # operator has to SEE it, or the only hint is several hundred CHECK
    # problems on a garment whose wiring has quietly been replaced.
    foreign = intake["foreignMap"]
    assert "another garment's map" in foreign["toast"], foreign
    assert "refused" in foreign["toast"], foreign
    # Neither garment's wiring moved (the two maps differ in size).
    assert foreign["look22"] == 4 and foreign["skirt"] != 4, foreign
