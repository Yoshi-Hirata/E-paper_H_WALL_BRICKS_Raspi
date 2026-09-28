"""THE SHOW's "Countdown before START" (2026-09-29).

The owner, verbatim: 「コンダクターのTHE SHOWについて、ショー開始までの
カウントダウン時間を設定できるように。以前実装されていた記憶がある。任意に
設定するのが難しい場合、-11秒スタートとなるようにして。」

③ START gets a lead of its own, stored with the show (show.json's
`start_countdown_s`, 11 s when it says nothing): the fleet counts down
-0:11 ... -0:01 and the show - and its music - begins at 0:00. NEXT / MOVE /
Back to 0:00 / GO keep the "take effect in" field's in-memory lead.

The server half (validation, default, persistence, undo, not in the show id,
export/import, bundles) is in tests/test_conductor_server.py. This file has:

* plain text checks over conductor/web/index.html;
* one headless run of the NOW -> NEXT board's own pure layer (SHOWBOARD) -
  the countdown's words, number and colours;
* one headless run of the whole page against a stand-in server that records
  every fleet command's body - which lead each button really sends, and what
  the big clock and the board print while a START counts down.

The browser halves are behind CONDUCTOR_BROWSER_TESTS=1, like the rest.
"""
from __future__ import annotations

import http.server
import json
import re
import threading
from html import unescape

import pytest

from conductor.server import Workspace, check_start_countdown
from tests.test_conductor_board import (ALL, D, INDEX_HTML, NAMES, PAGE, _MODULE,
                                        _dump_dom_long, _free_port,
                                        _function_body, _unit)
from tests.test_designer_build import _dump_dom, _require_browser
from tests.test_look import GRID, MAP


# --------------------------------------------------------------- the page

def test_the_two_fields_say_which_buttons_they_are_for():
    assert ('Countdown before START <input type="text"\n'
            '          id="show-countdown" value="${startCountdown()}"') in PAGE
    assert ('NEXT / MOVE take effect in <input type="text" id="fleet-lead" '
            'value="${ui.lead}"') in PAGE
    assert "START / NEXT / MOVE take effect in" not in PAGE, \
        "the old label still claims START"
    # Right next to ③ START, before the clear-after checkbox.
    start = PAGE.index('id="show-start"')
    assert start < PAGE.index('id="show-countdown"') < PAGE.index('id="show-clear-after"')


def test_start_sends_the_countdown_from_zero_and_the_other_lead_from_a_mark():
    handler = _strip(PAGE[PAGE.index('if (e.target.id === "show-start")'):
                          PAGE.index('if (e.target.id === "show-next-cue")')])
    assert "let lead = ui.lead;" in handler
    assert "if (!(startAt > 0)) {" in handler
    assert "lead = startCountdown();" in handler
    assert "{ lead_s: lead, ...answers }" in handler
    # A value typed and not yet saved is saved first (LOW-1).
    assert "if (countdownSaving) await countdownSaving;" in handler
    assert "await saveCountdown(typed);" in handler
    # No timer of the page's own for the lead (LOW-2): the countdown reads
    # off the position alone.
    assert "startLeadUntil" not in PAGE and "startLeft" not in PAGE
    # NEXT, MOVE, Back to 0:00 and GO are exactly as they were.
    assert 'fleetCommand("next", { lead_s: lead }' in PAGE
    assert "const lead = ui.lead;\n    ui.nextLeadUntil" in PAGE
    assert 'fleetCommand("seek", { to_s: t, manual: true, lead_s: ui.lead }' in PAGE
    assert 'fleetCommand("seek", { to_s: 0, manual: true, lead_s: ui.lead }' in PAGE
    assert 'fleetCommand("fire", { lead_s: ui.lead }' in PAGE
    assert "lead: 3," in PAGE, "the NEXT / MOVE lead's default moved"
    # There is one START on the page: every run starts from this handler.
    assert PAGE.count('fleetCommand("start"') == 2       # the send, and its retry


def test_the_countdown_is_stored_with_the_show_not_in_the_browser():
    body = _function_body("startCountdown")
    assert "state.show.start_countdown_s" in body
    assert "START_COUNTDOWN_DEFAULT_S" in body
    assert "const START_COUNTDOWN_DEFAULT_S = 11, START_COUNTDOWN_MIN_S = 3, " \
           "START_COUNTDOWN_MAX_S = 60;" in PAGE
    handler = PAGE[PAGE.index('if (id === "show-countdown")'):]
    handler = handler[:handler.index("return;\n  }\n") + 20]
    assert "await saveCountdown(seconds);" in handler
    assert 'api("/api/show/start_countdown", { s: seconds })' in _function_body("saveCountdown")
    assert "localStorage" not in handler
    # refresh() renders already: no second render() after it (LOW-7).
    assert "render();" not in _function_body("saveCountdown")
    assert "render();" not in handler
    # ...and the page and the server agree on the range.
    for fine in (3, 60, 11):
        assert check_start_countdown(fine) == fine


def _strip(source):
    return "\n".join(re.sub(r"//.*", "", line) for line in source.splitlines())


# ------------------------------------------------- the pure layer, running

_CALLS = {
    "cd_eleven": [-11.0],
    "cd_part_second": [-10.2],
    "cd_ten": [-10.0],
    "cd_four": [-3.4],
    "cd_three": [-3.0],
    "cd_last_tenth": [-0.1],
    "cd_zero": [0.0],
    "cd_running": [12.0],
    "cd_a_minute": [-60.0],
    "cd_none": [None],
}


def _heads():
    def one(phase, t, **kw):
        now = {"phase": phase, "t": t, "duration": D, "startAt": 0.0,
               "names": NAMES, "cues": ALL, "ago": True, "leadLeft": None}
        now.update(kw)
        return now
    return {
        "head_minus_eleven": one("running", -11.0),
        "head_minus_nine": one("running", -9.0),
        "head_minus_two": one("running", -1.5),
        "head_at_zero": one("running", 0.0),
        "head_held_in_the_countdown": one("holding", -5.0),
        # A START from a mark leads in over a position that is not negative:
        # the board reads exactly as it did before the countdown existed.
        "head_from_a_mark": one("running", 25.0),
    }


_PROBE = """<!doctype html><meta charset="utf-8"><title>countdown</title><body>
<script>
"use strict";
%(module)s
var CALLS = %(calls)s, HEADS = %(heads)s;
var out = { error: null, results: {} };
try {
  for (var name in CALLS) out.results[name] = SHOWBOARD.countdown.apply(null, CALLS[name]);
  for (var h in HEADS) {
    var now = HEADS[h];
    now.groups = SHOWBOARD.ahead(now.cues, now.t);
    now.agoS = now.ago ? SHOWBOARD.firedAgo(now.cues, now.t) : null;
    out.results[h] = SHOWBOARD.header(now);
  }
} catch (e) { out.error = String((e && e.stack) || e); }
var pre = document.createElement("pre");
pre.id = "cd-out";
pre.textContent = JSON.stringify(out);
document.body.appendChild(pre);
</script></body>
"""


@pytest.fixture(scope="module")
def layer(tmp_path_factory):
    assert _MODULE, "the SHOWBOARD markers are gone from conductor/web/index.html"
    tmp = tmp_path_factory.mktemp("countdown")
    _require_browser(tmp)
    page = tmp / "countdown.html"
    page.write_text(_PROBE % {"module": _MODULE.group(1), "calls": json.dumps(_CALLS),
                              "heads": json.dumps(_heads())}, encoding="utf-8")
    dom = _dump_dom("file:///" + str(page.resolve()).replace("\\", "/"), tmp)
    match = re.search(r'<pre id="cd-out">(.*?)</pre>', dom or "", re.S)
    assert match, f"no #cd-out in the dumped DOM:\n{(dom or '')[:3000]}"
    data = json.loads(unescape(match.group(1)))
    assert data["error"] is None, data["error"]
    return data["results"]


def test_the_countdown_reads_minus_eleven_to_minus_one(layer):
    assert layer["cd_eleven"] == {"secs": 11, "big": "-0:11",
                                  "label": "START in 11 s", "tone": ""}
    # Whole seconds rounded UP, like every countdown on the board: the label
    # and the big number always say the same second.
    assert layer["cd_part_second"]["big"] == "-0:11"
    assert layer["cd_part_second"]["label"] == "START in 11 s"
    assert layer["cd_last_tenth"] == {"secs": 1, "big": "-0:01",
                                      "label": "START in 1 s", "tone": "red"}
    assert layer["cd_a_minute"]["big"] == "-1:00"
    # At 0:00 and after it there is nothing to count down: the clock runs.
    assert layer["cd_zero"] is None and layer["cd_running"] is None
    assert layer["cd_none"] is None


def test_the_countdown_uses_the_boards_own_colours(layer):
    assert layer["cd_eleven"]["tone"] == ""
    assert layer["cd_ten"]["tone"] == "amber"
    assert layer["cd_four"]["tone"] == "amber"          # 4 s left
    assert layer["cd_three"]["tone"] == "red"


def test_the_board_header_is_the_countdown_until_zero(layer):
    h = layer["head_minus_eleven"]
    assert h["cap"] == "START in 11 s", h
    assert h["countText"] == "-0:11" and h["count"] == 11 and h["tone"] == ""
    assert h["note"] == "The show starts at 0:00.", h
    # What fires next follows underneath, the first group included since the
    # countdown took the header's own slot. At -0:11 that is the 0:00 preset's
    # own trigger (sent one repaint before 0:00, ~-8 s), then the 0:30 change.
    assert h["following"][0] == {"at": "0:00", "what": "LOOK 23 Tops + LOOK 24 Skirt",
                                 "design": "ivory"}, h
    assert h["following"][1] == {"at": "0:30", "what": "LOOK 23 Tops + LOOK 24 Skirt",
                                 "design": "scarlet + gold"}, h
    assert h["time"] == "" and h["garments"] == []
    nine = layer["head_minus_nine"]
    assert nine["cap"] == "START in 9 s" and nine["countText"] == "-0:09"
    assert nine["tone"] == "amber"
    two = layer["head_minus_two"]
    assert two["countText"] == "-0:02" and two["tone"] == "red"
    # 0:00: the countdown is over and the header is the ordinary NEXT again.
    zero = layer["head_at_zero"]
    assert zero["cap"] == "NEXT" and zero["countText"] is None
    assert zero["time"] == "0:30" and zero["count"] == 30


def test_a_hold_during_the_countdown_is_a_hold(layer):
    held = layer["head_held_in_the_countdown"]
    assert held["cap"] == "NEXT (HELD)" and held["countText"] is None, held


def test_a_start_from_a_mark_reads_as_it_always_did(layer):
    h = layer["head_from_a_mark"]
    assert h["cap"] == "NEXT" and h["note"] == "" and h["countText"] is None, h
    assert h["time"] == "0:30" and h["count"] == 5


# ------------------------------------------------- the whole page, running

_RUNS = {
    "none": None,
    # A START from 0:00 with the 11 s countdown, a moment after it landed.
    # The page carries the position forward with performance.now() between
    # polls, so the number read can be a second lower - the tests allow that.
    "countdown": {"t0": 0.0, "state": "running", "held_at": None, "force": False,
                  "now": -10.9},
    "late_countdown": {"t0": 0.0, "state": "running", "held_at": None,
                       "force": False, "now": -2.6},
    "running": {"t0": 0.0, "state": "running", "held_at": None, "force": False,
                "now": 5.0},
    "holding": {"t0": 0.0, "state": "holding", "held_at": 20.0, "force": False,
                "now": 20.0},
    # HOLD pressed during the countdown, and RESUME after it (LOW-2): the
    # fleet keeps the position, so 6 s are still to go.
    "held_countdown": {"t0": 0.0, "state": "holding", "held_at": -6.0,
                       "force": False, "now": -6.0},
    "resumed_countdown": {"t0": 0.0, "state": "running", "held_at": None,
                          "force": False, "now": -5.9},
}


class _Stand:
    """index.html, a real /api/state, and a fleet that records what it is told."""

    def __init__(self, tmp_path, probe):
        ws = Workspace(tmp_path / "ws")
        for item in ("Look23", "Look24"):
            ws.save(f"{item}_map.csv", MAP)
            ws.save(f"{item}_color_ivory_grid.csv", GRID)
            ws.save(f"{item}_color_scarlet_grid.csv", GRID)
        ws.assign("Look23", "radxa-01")
        ws.assign("Look24", "radxa-02")
        ws.set_timeline(180, [
            {"id": "p23", "item": "Look23", "at": 0, "design": "Look23_color_ivory_grid.csv"},
            {"id": "p24", "item": "Look24", "at": 0, "design": "Look24_color_ivory_grid.csv"},
            {"id": "c23", "item": "Look23", "at": 60, "design": "Look23_color_scarlet_grid.csv"},
        ])
        self.ws = ws
        written = ws.written_state()
        page = INDEX_HTML.read_text(encoding="utf-8").replace("</body>", probe + "</body>", 1)
        self.run = "none"
        self.start_at = 0.0                  # a MOVE made before START
        self.sent = []                       # [command, body] as they arrived
        stand = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _send(self, body, kind, code=200):
                self.send_response(code)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, obj, code=200):
                self._send(json.dumps(obj).encode("utf-8"), "application/json", code)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length > 0 else b"{}"
                body = json.loads(raw or b"{}")
                path = self.path.split("?")[0]
                if path == "/api/show/start_countdown":
                    # The real Workspace, so the page talks to the real rule.
                    try:
                        stand.ws.set_start_countdown(body.get("s"))
                    except ValueError as exc:
                        return self._json({"error": str(exc)}, 400)
                    stand.sent.append(["countdown", body])
                    return self._json({"ok": True})
                if path.startswith("/api/fleet/"):
                    command = path[len("/api/fleet/"):]
                    stand.sent.append([command, body])
                    answer = {"units": {"radxa-01": {"ok": True}}}
                    if command == "start":
                        answer.update(lead_s=body.get("lead_s"), from_s=0.0)
                    return self._json(answer)
                self.do_GET()

            def do_GET(self):
                path = self.path.split("?")[0]
                if path in ("/", "/index.html"):
                    return self._send(page.encode("utf-8"), "text/html; charset=utf-8")
                if path == "/api/state":
                    return self._json(stand.ws.state())
                if path == "/api/fleet":
                    return self._json({
                        "units": [_unit("radxa-01"), _unit("radxa-02")],
                        "last_fire": None, "run": _RUNS[stand.run],
                        "shows": {n: {"id": "S1", "cues": 2, "boards": []}
                                  for n in ("radxa-01", "radxa-02")},
                        "clear_in_s": None, "corrections": [], "prepared": {},
                        "start_at": stand.start_at, "show_duration": 180.0,
                        "burn": {"burned": 0, "total": 0}, "timeline": written})
                if path == "/api/fleet/demos":
                    return self._json({"units": {}, "offline": [], "failed": {}})
                if path == "/test/fleet":
                    args = dict(p.split("=", 1) for p in
                                self.path.partition("?")[2].split("&") if "=" in p)
                    stand.run = args.get("run", stand.run)
                    stand.start_at = float(args.get("start_at", stand.start_at))
                    return self._json({"run": stand.run})
                if path == "/test/sent":
                    return self._json(stand.sent)
                return self._json({})

        self.port = _free_port()
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


# Typed values the page and the server must read the same way: boundaries,
# tenths, exact binary ties (2.25 -> 2.2 in Python, half to even), full-width
# digits, blanks and junk.
PARSE_CASES = ["11", "3", "60", "2.94", "2.95", "2.96", "60.04", "60.05", "60.06",
               "7.25", "7.35", "7.75", "12.25", "12.75", "4.45", "59.95", "3.05", "１１", " 15 ", "１２.５",
               "", "  ", "abc", "1e1", "NaN", "Infinity", "-11", "0x10", "3.049999"]

_PAGE_PROBE = """
<script>
var PARSE_CASES = %(parse)s;
(function () {
  var out = { error: null };
  function wait(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
  function $q(sel) { return document.querySelector(sel); }
  function label(id) { var el = $q("#" + id); return el ? el.parentElement.textContent.replace(/\\s+/g, " ").trim() : null; }
  function sent() { return fetch("/test/sent").then(function (r) { return r.json(); }); }
  function type(id, value) {
    var el = $q("#" + id);
    el.value = value;
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }
  function clockNow() {
    var box = $q("#show-clock");
    return { word: box.querySelector("small").textContent,
             big: box.childNodes[1] ? box.childNodes[1].textContent : "",
             cls: box.className };
  }
  function head(sel) { var el = $q("#nownext " + sel); return el ? el.textContent.trim() : null; }
  (async function () {
    try {
      for (var i = 0; i < 200 && state === null; i++) await wait(50);
      out.booted = state !== null;
      window.confirm = function () { return true; };
      ui.tab = "fleet"; render();
      await wait(1500);

      // 1. The two fields, and what they hold on a show that never set one.
      out.countdownLabel = label("show-countdown");
      out.leadLabel = label("fleet-lead");
      out.countdownValue = $q("#show-countdown").value;
      out.leadValue = $q("#fleet-lead").value;

      // 2. Changing the countdown posts it to the show and reads it back.
      type("show-countdown", "15");
      await wait(1200);
      out.afterSet = $q("#show-countdown").value;
      out.stateAfterSet = state.show.start_countdown_s;
      // ...a reload of the state (what a page reload does) brings it back.
      await refresh(); render();
      await wait(400);
      out.afterReload = $q("#show-countdown").value;
      // ...and a value outside 3-60 is refused on the page, never sent.
      type("show-countdown", "2");
      await wait(600);
      out.afterBad = $q("#show-countdown").value;
      out.badToast = $q("#toast").textContent;
      // ...full-width digits are read as the server reads them (LOW-6).
      type("show-countdown", "\\uff11\\uff12");            // "１２"
      await wait(1200);
      out.afterFullWidth = state.show.start_countdown_s;

      // 3. The NEXT / MOVE field is its own, and in memory as before.
      type("fleet-lead", "\\uff15");                       // "５"
      await wait(300);
      out.leadAfter = ui.lead;

      // 4. ③ START from 0:00 sends the countdown - the one just TYPED, with
      //    no Enter and no blur before the click (LOW-1).
      $q("#show-countdown").value = "15";
      $q("#show-start").click();
      await wait(2000);
      out.startLog = ui.showLog.split("\\n")[0];
      out.savedBeforeStart = state.show.start_countdown_s;
      out.fieldAfterStart = $q("#show-countdown").value;
      // ...from a mark it sends the NEXT / MOVE lead, not the countdown (MED-1).
      await fetch("/test/fleet?start_at=60");
      await wait(1600);
      out.startBtnFromMark = $q("#show-start").textContent;
      $q("#show-start").click();
      await wait(1500);
      out.markLog = ui.showLog.split("\\n")[0];
      await fetch("/test/fleet?start_at=0");
      await wait(1600);
      // NEXT, MOVE, Back to 0:00 and GO send the other field too.
      ui.manual = true;
      await commitSeek(30);
      await wait(600);
      await fetch("/test/fleet?run=holding");
      await wait(1600);
      $q("#show-next-cue").click();
      await wait(1200);
      await fetch("/test/fleet?run=none");
      await wait(1600);
      $q("#fleet-fire").click();
      await wait(1200);
      out.sent = await sent();

      // 5. The countdown on the big clock and the board: -0:11 counting to
      //    0:00, "START in 11 s", the board's own colours.
      await fetch("/test/fleet?run=countdown");
      await wait(1600);
      await refreshFleetNow();
      out.cdClock = clockNow();
      out.cdHead = { cap: head("[data-cap]"), count: head("[data-count]"),
                     cls: ($q("#nownext [data-count]") || {}).className,
                     note: head("[data-note]") };
      // NEXT is off while the countdown runs, and says why (LOW-3).
      out.cdNext = { disabled: $q("#show-next-cue").disabled, title: $q("#show-next-cue").title };
      var before = (await sent()).length;
      window.toastWas = null;
      $q("#show-next-cue").disabled = false;          // even forced on, the click refuses
      $q("#show-next-cue").click();
      await wait(600);
      out.cdNextSent = (await sent()).length - before;
      out.cdNextToast = $q("#toast").textContent;
      showClockText();
      await fetch("/test/fleet?run=late_countdown");
      await wait(1600);
      await refreshFleetNow();
      out.lateClock = clockNow();
      out.lateHead = { count: head("[data-count]"),
                       cls: ($q("#nownext [data-count]") || {}).className };
      // ...the stage monitor is the same board, so it reads the same.
      $q("#nn-stage-btn").click();
      await wait(600);
      out.stageCount = ($q("#nn-stage #nownext [data-count]") || {}).textContent;
      $q("#nn-stage-btn").click();
      await wait(600);
      // 6. HOLD during the countdown, then RESUME: the clock reads off the
      //    position, so it holds and then goes on from what was left (LOW-2).
      await fetch("/test/fleet?run=held_countdown");
      await wait(1600);
      await refreshFleetNow();
      out.heldClock = clockNow();
      out.heldNext = { disabled: $q("#show-next-cue").disabled, title: $q("#show-next-cue").title };
      await fetch("/test/fleet?run=resumed_countdown");
      await wait(1600);
      await refreshFleetNow();
      out.resumedClock = clockNow();
      // ...and STOP leaves nothing counting.
      await fetch("/test/fleet?run=none");
      await wait(1600);
      await refreshFleetNow();
      out.stoppedClock = clockNow();
      out.stoppedHead = head("[data-cap]");
      // 7. Once the show is running the clock is the show's position again,
      //    and NEXT is back.
      await fetch("/test/fleet?run=running");
      await wait(1600);
      await refreshFleetNow();
      out.runClock = clockNow();
      out.runHeadCap = head("[data-cap]");
      // (This stand's units report no cue ahead, so a running show's NEXT
      // has nothing to bring forward; a HOLD past 0:00 is where it is on.)
      await fetch("/test/fleet?run=holding");
      await wait(1600);
      await refreshFleetNow();
      out.runNext = { disabled: $q("#show-next-cue").disabled, title: $q("#show-next-cue").title };
      // 8. The page reads a typed number exactly as the server does (LOW-6).
      out.parsed = PARSE_CASES.map(function (c) { return parseSeconds(c, 3, 60); });
    } catch (e) { out.error = String((e && e.stack) || e); }
    var pre = document.createElement("pre");
    pre.id = "page-out";
    pre.textContent = JSON.stringify(out);
    document.body.appendChild(pre);
  })();
  // One poll, now, and a repaint: the numbers read straight after it are the
  // stand-in's own position, not one a second of carrying forward moved.
  async function refreshFleetNow() {
    fleet = await (await fetch("/api/fleet")).json(); fleet.at = performance.now();
    showClockText(); paintBoard();
  }
})();
</script>
"""


@pytest.fixture(scope="module")
def page(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("countdownpage")
    _require_browser(tmp)
    stand = _Stand(tmp, _PAGE_PROBE.replace("%(parse)s", json.dumps(PARSE_CASES)))
    try:
        dom = _dump_dom_long(stand.url, tmp)
    finally:
        stand.close()
    match = re.search(r'<pre id="page-out">(.*?)</pre>', dom or "", re.S)
    assert match, f"no #page-out in the dumped DOM:\n{(dom or '')[:3000]}"
    data = json.loads(unescape(match.group(1)))
    assert data.get("error") is None, data["error"]
    assert data["booted"], "the page never loaded its state"
    return data


def test_the_page_shows_both_fields_with_their_defaults(page):
    assert page["countdownLabel"] == "Countdown before START s", page["countdownLabel"]
    assert page["countdownValue"] == "11", "a show that never set it counts down 11 s"
    assert page["leadLabel"] == "NEXT / MOVE take effect in s"
    assert page["leadValue"] == "3"


def test_the_countdown_is_saved_with_the_show_and_survives_a_reload(page):
    assert page["afterSet"] == "15" and page["stateAfterSet"] == 15
    assert page["afterReload"] == "15"
    assert page["afterBad"] == "15", "a refused value stayed in the field"
    assert "3 to 60" in page["badToast"]
    assert page["afterFullWidth"] == 12, "full-width digits were refused"
    saved = [body for command, body in page["sent"] if command == "countdown"]
    # 15 typed, 2 refused (never sent), １２ typed, then 15 typed and START
    # pressed straight after - saved once, before the START went out.
    assert saved == [{"s": 15}, {"s": 12}, {"s": 15}], page["sent"]
    assert page["leadAfter"] == 5, "the full-width NEXT / MOVE lead was refused"


def test_start_from_zero_uses_the_value_typed_just_before_it(page):
    order = [command for command, _ in page["sent"]]
    starts = [body for command, body in page["sent"] if command == "start"]
    assert starts[0]["lead_s"] == 15, page["sent"]
    # The save of 15 went out BEFORE the START that depends on it.
    saves = [i for i, command in enumerate(order) if command == "countdown"]
    assert len(saves) == 3 and saves[2] < order.index("start"), order
    assert page["startLog"].startswith("③ START (in 15 s)"), page["startLog"]
    assert page["savedBeforeStart"] == 15 and page["fieldAfterStart"] == "15"


def test_start_from_a_mark_uses_the_next_move_lead(page):
    starts = [body for command, body in page["sent"] if command == "start"]
    assert len(starts) == 2, page["sent"]
    assert page["startBtnFromMark"] == "③ START FROM 1:00"
    assert starts[1]["lead_s"] == 5, "a START from a mark counted the countdown down"
    assert page["markLog"].startswith("③ START (in 5 s)"), page["markLog"]


def test_next_move_and_go_send_their_own_lead(page):
    leads = {command: body.get("lead_s") for command, body in page["sent"]
             if command not in ("countdown", "start")}
    assert leads["seek"] == 5 and leads["next"] == 5 and leads["fire"] == 5, page["sent"]


def test_next_waits_for_the_countdown(page):
    assert page["cdNext"] == {"disabled": True, "title": "The show has not started yet"}
    assert page["cdNextSent"] == 0, "NEXT went out during the countdown"
    assert page["cdNextToast"] == "The show has not started yet"
    assert page["heldNext"] == {"disabled": True, "title": "The show has not started yet"}
    assert page["runNext"] == {"disabled": False, "title": ""}


def test_hold_resume_and_stop_during_the_countdown_read_right(page):
    held = page["heldClock"]
    assert held["word"] == "ON HOLD" and held["big"] == "-0:06", held
    assert "cd-" not in held["cls"], "a held countdown is coloured like a running one"
    resumed = page["resumedClock"]
    assert resumed["big"] in ("-0:06", "-0:05"), resumed
    assert resumed["word"] == "START in " + resumed["big"][-1] + " s", resumed
    assert "cd-amber" in resumed["cls"], resumed
    stopped = page["stoppedClock"]
    assert stopped["word"] == "NOT STARTED" and stopped["big"] == "0:00", stopped
    assert "START in" not in page["stoppedHead"]


def test_the_page_reads_a_typed_number_exactly_as_the_server_does(page):
    def server(text):
        try:
            return check_start_countdown(text)
        except ValueError:
            return None
    assert page["parsed"] == [server(c) for c in PARSE_CASES], \
        list(zip(PARSE_CASES, page["parsed"], [server(c) for c in PARSE_CASES]))
    # ...and the cases that matter at the edges, spelled out.
    got = dict(zip(PARSE_CASES, page["parsed"]))
    assert got["2.96"] == 3.0 and got["2.94"] is None
    assert got["60.04"] == 60.0 and got["60.06"] is None
    assert got["１１"] == 11 and got["0x10"] is None and got[""] is None


def test_the_big_clock_counts_down_to_zero(page):
    cd = page["cdClock"]
    number = re.fullmatch(r"-0:(\d\d)", cd["big"])
    assert number, cd
    assert int(number.group(1)) in (10, 11), cd
    assert cd["word"] == f"START in {int(number.group(1))} s", cd
    late = page["lateClock"]
    assert late["big"] in ("-0:03", "-0:02"), late
    assert "cd-red" in late["cls"], late
    assert page["runClock"]["big"].startswith("0:0"), page["runClock"]
    assert page["runClock"]["word"] == "RUNNING", page["runClock"]


def test_the_board_and_the_stage_monitor_count_down_too(page):
    head = page["cdHead"]
    assert head["cap"] in ("START in 11 s", "START in 10 s"), head
    assert head["count"] in ("-0:11", "-0:10"), head
    assert head["count"][-2:] == head["cap"].split()[2].zfill(2), head
    assert head["note"] == "The show starts at 0:00.", head
    assert page["lateHead"]["count"] in ("-0:03", "-0:02")
    assert "red" in page["lateHead"]["cls"]
    assert page["stageCount"] in ("-0:03", "-0:02"), page["stageCount"]
    assert page["runHeadCap"] == "NEXT"
