"""EXHIBITION mode on the page (2026-09-30): what conductor/web/index.html
does with the Loop, the Conductor host's speaker, the workspace transfer
and the units' Wi-Fi.

* plain text checks over the page;
* one headless run of the NOW -> NEXT board's pure layer (SHOWBOARD) - the
  header while a Loop restart is pending;
* one headless run of the whole page against a stand-in server that records
  every command's body: the Loop control posts /api/loop, the big clock and
  the board count the next run down, a Conductor with a speaker mutes the
  page's player by default (and only by default), the tiles show the
  units' Wi-Fi, the fleet-wide Wi-Fi buttons post /api/fleet/wifi_select,
  and "Send workspace to …" polls its job to the receiver's reply.

The browser halves are behind CONDUCTOR_BROWSER_TESTS=1, like the rest.
"""
from __future__ import annotations

import http.server
import json
import re
import threading
from html import unescape

import pytest

from conductor.server import Workspace, check_loop_wait
from tests.test_conductor_board import (ALL, D, INDEX_HTML, NAMES, PAGE, _MODULE,
                                        _dump_dom_long, _free_port,
                                        _function_body, _unit)
from tests.test_designer_build import _dump_dom, _require_browser
from tests.test_look import GRID, MAP


# --------------------------------------------------------------- the page

def test_the_loop_control_sits_with_the_countdown_and_posts_api_loop():
    start = PAGE.index('id="show-start"')
    assert start < PAGE.index('id="show-countdown"') < PAGE.index('id="show-loop"') \
        < PAGE.index('id="show-loop-wait"') < PAGE.index('id="show-clear-after"')
    assert "Loop: next run after <input" in PAGE
    body = _function_body("saveLoop")
    assert 'api("/api/loop", waitS === null ? { on: false } : { on: true, wait_s: waitS })' in body
    assert "localStorage" not in body, "the Loop is the show's, not the browser's"
    handler = PAGE[PAGE.index('if (id === "show-loop" || id === "show-loop-wait")'):]
    handler = handler[:handler.index("return;\n  }\n") + 20]
    assert "parseSeconds(field ? field.value : \"\", LOOP_WAIT_MIN_S, LOOP_WAIT_MAX_S)" in handler
    assert "const LOOP_WAIT_DEFAULT_S = 45, LOOP_WAIT_MIN_S = 40, LOOP_WAIT_MAX_S = 600;" in PAGE
    for fine in (40, 600, 45):
        assert check_loop_wait(fine) == fine
    assert "state.show.loop_wait_s" in _function_body("loopWait")


def test_the_next_run_is_counted_down_off_the_poll_not_a_timer():
    body = _function_body("loopNextIn")
    assert "fleet.loop" in body and "performance.now() - fleet.at" in body
    clock = _function_body("showClockText")
    assert '"ENDED · NEXT RUN IN " + clock(loopIn)' in clock
    assert "loopIn: loopNextIn()" in _function_body("paintBoard")
    assert "loopProblem: fleet?.loop?.problem || null" in _function_body("paintBoard")


def test_a_speaker_conductor_mutes_the_page_by_default_only():
    body = _function_body("applySpeakerDefault")
    assert "fleet.speaker" in body and "player.muted = true" in body
    assert 'localStorage.getItem("show.muted")' in body
    assert "stored === null" in body, "a stored choice must win over the default"
    assert "applySpeakerDefault();" in _function_body("fetchFleet")
    assert 'const SPEAKER_NOTE = "music plays on the Conductor host (USB speaker)";' in PAGE
    assert "speakerNote(sp)" in _function_body("paintShowMusic")
    # The mute button still stores the choice, which is the override.
    handler = PAGE[PAGE.index('if (e.target.id === "show-music-mute")'):]
    assert "saveMusicAudioPrefs();" in handler[:300]


def test_send_workspace_and_wifi_controls_are_on_the_fleet_tab():
    fleet = _function_body("renderFleet")
    assert 'id="ws-send-to"' in fleet and 'id="ws-send"' in fleet
    assert 'id="wifi-hotspot"' in fleet and 'id="wifi-router"' in fleet
    assert 'id="wifi-router-name"' in fleet
    assert 'const SEND_TO_DEFAULT = "radxa-05:8765";' in PAGE
    send = _function_body("sendWorkspace")
    assert 'api("/api/workspace/send", { to })' in send
    assert "/api/workspace/send?job=" in send
    assert "confirm(" in send, "a whole workspace is replaced: ask first"
    wifi = _function_body("switchFleetWifi")
    assert "confirm(" in wifi
    assert 'fleetCommand("wifi_select", { profile: name, after_s: WIFI_SWITCH_AFTER_S }' in wifi
    assert 'const WIFI_HOTSPOT = "AZ-Epaper", WIFI_SWITCH_AFTER_S = 20;' in PAGE
    assert "${wifiRow(u)}" in _function_body("renderTiles")
    row = _function_body("wifiRow")
    assert "w.mode" in row and "w.signal" in row


# ------------------------------------------------- the pure layer, running

def _heads():
    def one(phase, t, **kw):
        now = {"phase": phase, "t": t, "duration": D, "startAt": 0.0,
               "names": NAMES, "cues": ALL, "ago": True, "leadLeft": None,
               "cleared": None}
        now.update(kw)
        return now
    return {
        "ended_plain": one("ended", D + 3.0),
        "ended_loop": one("ended", D + 3.0, loopIn=25.0),
        "ended_loop_last": one("ended", D + 30.0, loopIn=0.4),
        "ended_loop_problem": one("ended", D + 40.0, loopIn=4.0,
                                  loopProblem="radxa-02: not answering"),
        "running_with_loop_field": one("running", 12.0, loopIn=25.0),
    }


_PROBE = """<!doctype html><meta charset="utf-8"><title>loop</title><body>
<script>
"use strict";
%(module)s
var HEADS = %(heads)s;
var out = { error: null, results: {} };
try {
  for (var h in HEADS) {
    var now = HEADS[h];
    now.groups = SHOWBOARD.ahead(now.cues, now.t);
    now.agoS = now.ago ? SHOWBOARD.firedAgo(now.cues, now.t) : null;
    out.results[h] = SHOWBOARD.header(now);
  }
} catch (e) { out.error = String((e && e.stack) || e); }
var pre = document.createElement("pre");
pre.id = "loop-out";
pre.textContent = JSON.stringify(out);
document.body.appendChild(pre);
</script></body>
"""


@pytest.fixture(scope="module")
def layer(tmp_path_factory):
    assert _MODULE, "the SHOWBOARD markers are gone from conductor/web/index.html"
    tmp = tmp_path_factory.mktemp("loopboard")
    _require_browser(tmp)
    page = tmp / "loop.html"
    page.write_text(_PROBE % {"module": _MODULE.group(1), "heads": json.dumps(_heads())},
                    encoding="utf-8")
    dom = _dump_dom("file:///" + str(page.resolve()).replace("\\", "/"), tmp)
    match = re.search(r'<pre id="loop-out">(.*?)</pre>', dom or "", re.S)
    assert match, f"no #loop-out in the dumped DOM:\n{(dom or '')[:3000]}"
    data = json.loads(unescape(match.group(1)))
    assert data["error"] is None, data["error"]
    return data["results"]


def test_the_board_counts_the_next_run_down_once_the_show_has_ended(layer):
    plain = layer["ended_plain"]
    assert plain["cap"] == "SHOW ENDED" and plain["count"] is None
    loop = layer["ended_loop"]
    assert loop["cap"] == "NEXT RUN in 25 s" and loop["count"] == 25
    assert loop["countText"] == "0:25" and loop["tone"] == ""
    assert loop["note"].startswith("Loop: the show starts again from 0:00")
    last = layer["ended_loop_last"]
    assert last["cap"] == "NEXT RUN in 1 s" and last["tone"] == "red"
    problem = layer["ended_loop_problem"]
    assert problem["tone"] == "amber"
    assert problem["note"] == "Loop: waiting to start again — radxa-02: not answering"
    # A running show ignores the field: only an ENDED one has a next run.
    running = layer["running_with_loop_field"]
    assert running["cap"] == "NEXT" and running["time"] == "0:30"


# ------------------------------------------------- the whole page, running

_RUNS = {
    "none": None,
    "running": {"t0": 0.0, "state": "running", "held_at": None, "force": False,
                "now": 5.0, "loops": 0},
    # Past the end of a 180 s show: the Loop's wait is what the page shows.
    "ended": {"t0": 0.0, "state": "running", "held_at": None, "force": False,
              "now": 183.0, "loops": 0},
}


def _speaker_report(kind):
    """/api/fleet's `speaker` as a speaker Conductor reports it: "1" a USB
    speaker (no Bluetooth watched), "bt" a dropped Bose, "btok" a connected
    one, "btpair" one being re-paired - the shapes conductor/speaker.py's
    status() publishes (its API contract)."""
    report = {"available": True, "error": None, "state": "loaded", "track": "show.mp3",
              "latency_ms": 62.0, "playing": False, "log": [], "volume": 70,
              "applied": "pulse", "volume_error": None, "bluetooth": False,
              "device": None, "connection": "no_device",
              "reconnect": {"attempts": 0, "next_in_s": None, "last_error": None},
              "pairing": None}
    if kind == "1":
        return report
    device = {"mac": "AC:BF:71:FA:8F:AB", "name": "Bose Flex SoundLink", "paired": True,
              "trusted": True, "connected": kind == "btok", "sink_present": kind == "btok",
              "last_connected_at": 1700000000.0, "last_error": None}
    report.update(bluetooth=True, device=device, applied="bluez" if kind == "btok" else None)
    if kind == "bt":
        report.update(connection="disconnected",
                      reconnect={"attempts": 2, "next_in_s": 17.4,
                                 "last_error": "Failed to connect: org.bluez.Error.Failed"})
    elif kind == "btok":
        report.update(connection="connected")
    elif kind == "btpair":
        report.update(connection="pairing",
                      pairing={"phase": "scanning", "started_at": 1700000000.0,
                               "note": "forgetting the old pairing, scanning - put the speaker in pairing mode"})
    return report


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
        self.loop_next = None                # next_in_s the stand-in reports
        self.speaker = None
        self.passcode = None                 # set: POSTs need X-Passcode
        self.sent = []                       # [command, body] as they arrived
        self.jobs = {}
        stand = self

        def unit(name, wifi):
            u = _unit(name)
            u["wifi"] = wifi
            return u

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

            def _loop(self):
                wait = stand.ws.loop_wait()
                return {"on": wait is not None, "wait_s": int(wait or 45),
                        "next_in_s": stand.loop_next, "runs": 0,
                        "problem": None}

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length > 0 else b"{}"
                body = json.loads(raw or b"{}")
                path = self.path.split("?")[0]
                # The passcode gate, as the real server applies it to a
                # client from another host: 401 until X-Passcode matches.
                if stand.passcode and self.headers.get("X-Passcode") != stand.passcode:
                    stand.sent.append(["refused", {"path": path, "given": self.headers.get("X-Passcode")}])
                    return self._json({"error": "passcode required"}, 401)
                if path == "/api/loop":
                    # The real Workspace, so the page talks to the real rule.
                    try:
                        if body.get("on"):
                            stand.ws.set_loop(body.get("wait_s", 45))
                        else:
                            stand.ws.set_loop(None)
                    except ValueError as exc:
                        return self._json({"error": str(exc)}, 400)
                    stand.sent.append(["loop", body])
                    return self._json(self._loop())
                if path in ("/api/speaker/connect", "/api/speaker/pair"):
                    kind = path.rsplit("/", 1)[1]
                    stand.sent.append(["speaker_" + kind, body])
                    return self._json({"ok": True, "error": None,
                                       "connection": "connecting" if kind == "connect" else "pairing"})
                if path == "/api/workspace/send":
                    stand.sent.append(["send", body])
                    job = {"job": "j1", "state": "sending", "to": body.get("to"),
                           "total": 3 * 1024 * 1024, "sent": 1024 * 1024,
                           "counts": None, "reply": None, "error": None, "ok": True}
                    stand.jobs["j1"] = job
                    return self._json(job)
                if path.startswith("/api/fleet/"):
                    command = path[len("/api/fleet/"):]
                    stand.sent.append([command, body])
                    if command == "start":
                        return self._json({"units": {"radxa-01": {"ok": True}},
                                           "lead_s": body.get("lead_s"), "from_s": 0.0})
                    if command == "wifi_select":
                        return self._json({"units": {
                            "radxa-01": {"ok": True, "scheduled": True, "after_s": 20},
                            "radxa-02": {"ok": False, "error": "a show is running"}},
                            "last": [], "profile": body.get("profile"), "after_s": 20})
                    return self._json({"units": {"radxa-01": {"ok": True}}})
                self.do_GET()

            def do_GET(self):
                path = self.path.split("?")[0]
                if path in ("/", "/index.html"):
                    return self._send(page.encode("utf-8"), "text/html; charset=utf-8")
                if path == "/api/state":
                    return self._json(stand.ws.state())
                if path == "/api/fleet":
                    return self._json({
                        "units": [unit("radxa-01", {"ssid": "AZ-Epaper", "ip": "10.42.0.101",
                                                    "signal": 72, "mode": "client",
                                                    "profile": "AZ-Epaper"}),
                                  unit("radxa-02", {"ssid": "AZ-Epaper", "ip": "10.42.0.1",
                                                    "signal": None, "mode": "hotspot",
                                                    "profile": "AZ-Epaper"})],
                        "last_fire": None, "run": _RUNS[stand.run],
                        "shows": {n: {"id": "S1", "cues": 2, "boards": []}
                                  for n in ("radxa-01", "radxa-02")},
                        "clear_in_s": None, "corrections": [], "prepared": {},
                        "start_at": 0.0, "show_duration": 180.0,
                        "loop": self._loop(), "speaker": stand.speaker,
                        "burn": {"burned": 0, "total": 0}, "timeline": written})
                if path == "/api/fleet/demos":
                    return self._json({"units": {}, "offline": [], "failed": {}})
                if path == "/api/workspace/send":
                    job = dict(stand.jobs.get("j1") or {"error": "no such transfer"})
                    if job.get("state") == "sending":
                        job.update(state="done", sent=job["total"],
                                   counts={"files": 6, "music": "show.mp3",
                                           "show": True, "history": True},
                                   reply={"ok": True, "files": 6, "music": "show.mp3",
                                          "cues": 3, "history": True,
                                          "shows": {"radxa-01": "abc123def4",
                                                    "radxa-02": "0123456789"},
                                          "problems": []})
                        stand.jobs["j1"] = job
                    return self._json(job)
                if path == "/test/fleet":
                    args = dict(p.split("=", 1) for p in
                                self.path.partition("?")[2].split("&") if "=" in p)
                    stand.run = args.get("run", stand.run)
                    if "loop_next" in args:
                        stand.loop_next = (None if args["loop_next"] == "null"
                                           else float(args["loop_next"]))
                    if "passcode" in args:
                        stand.passcode = None if args["passcode"] == "null" else args["passcode"]
                    if "speaker" in args:
                        stand.speaker = (None if args["speaker"] == "null" else
                                         _speaker_report(args["speaker"]))
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


_PAGE_PROBE = """
<script>
(function () {
  var out = { error: null };
  function wait(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
  function $q(sel) { return document.querySelector(sel); }
  function sent() { return fetch("/test/sent").then(function (r) { return r.json(); }); }
  function type(id, value) {
    var el = $q("#" + id);
    el.value = value;
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }
  function tick(id, on) {
    var el = $q("#" + id);
    el.checked = on;
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }
  function clockNow() {
    var box = $q("#show-clock");
    return { word: box.querySelector("small").textContent,
             big: box.childNodes[1] ? box.childNodes[1].textContent : "" };
  }
  function head(sel) { var el = $q("#nownext " + sel); return el ? el.textContent.trim() : null; }
  async function refreshFleetNow() {
    fleet = await (await fetch("/api/fleet")).json(); fleet.at = performance.now();
    applySpeakerDefault();
    showClockText(); paintBoard(); renderTiles(); paintShowMusic();
  }
  (async function () {
    try {
      for (var i = 0; i < 200 && state === null; i++) await wait(50);
      out.booted = state !== null;
      window.confirm = function () { return true; };
      ui.tab = "fleet"; render();
      await wait(1500);

      // 1. The Loop control: off, 30 in the field; tick -> POST /api/loop
      //    {on:true, wait_s:30}; type 45 -> {on:true, wait_s:45}; untick ->
      //    {on:false}; a bad number never goes out.
      out.loopOffAtStart = { checked: $q("#show-loop").checked, wait: $q("#show-loop-wait").value };
      tick("show-loop", true);
      await wait(1200);
      out.loopOn = { checked: $q("#show-loop").checked, state: state.show.loop_wait_s };
      type("show-loop-wait", "60");
      await wait(1200);
      out.loopWait = { field: $q("#show-loop-wait").value, state: state.show.loop_wait_s };
      type("show-loop-wait", "30");
      await wait(600);
      out.loopBad = { field: $q("#show-loop-wait").value, toast: $q("#toast").textContent };
      tick("show-loop", false);
      await wait(1200);
      out.loopOff = { checked: $q("#show-loop").checked, state: state.show.loop_wait_s,
                      field: $q("#show-loop-wait").value };
      out.loopSent = (await sent()).filter(function (s) { return s[0] === "loop"; });

      // 2. The show ended with a next run pending: the clock and the board.
      tick("show-loop", true);
      await wait(1200);
      await fetch("/test/fleet?run=ended&loop_next=25");
      await refreshFleetNow();
      out.loopClock = clockNow();
      out.loopHead = { cap: head("[data-cap]"), count: head("[data-count]"), note: head("[data-note]") };
      $q("#nn-stage-btn").click();
      await wait(400);
      out.stageCap = ($q("#nn-stage #nownext [data-cap]") || {}).textContent;
      $q("#nn-stage-btn").click();
      await wait(400);
      out.stopQuestion = stopQuestion();
      // (3) START during the wait: the next run, asked about like a first
      // one, sent WITHOUT force (M5).
      out.overDuringWait = runIsOver();
      var confirms = [];
      window.confirm = function (msg) { confirms.push(msg); return true; };
      $q("#show-start").click();
      await wait(1500);
      window.confirm = function () { return true; };
      out.startDuringWait = (await sent()).filter(function (s) { return s[0] === "start"; }).pop();
      out.startConfirms = confirms;
      await fetch("/test/fleet?run=ended&loop_next=null");
      await refreshFleetNow();
      out.endedClock = clockNow();
      out.endedCap = head("[data-cap]");
      out.overWhenEnded = runIsOver();
      await fetch("/test/fleet?run=running");
      await refreshFleetNow();
      out.overWhenRunning = runIsOver();

      // 3. A Conductor with a speaker: the player mutes itself once, unless
      //    this browser has chosen.
      out.mutedBefore = player.muted;
      await fetch("/test/fleet?run=none&speaker=1");
      await refreshFleetNow();
      out.mutedWithSpeaker = player.muted;
      out.speakerNote = $q("#show-music-host").textContent;
      out.speakerShown = $q("#show-music-host").style.display;
      // The operator unmutes: stored, and a later default cannot re-mute.
      $q("#show-music-mute").click();
      await wait(100);
      out.unmuted = player.muted;
      speakerDefaulted = false;
      applySpeakerDefault();
      out.staysUnmuted = player.muted;
      try { out.storedMuted = localStorage.getItem("show.muted"); } catch (e) { out.storedMuted = "n/a"; }
      // 3b. The Bluetooth speaker's line: hidden for a USB speaker, red
      //     with the reason and the retry while disconnected (Connect posts,
      //     Re-pair asks first and posts), green connected, amber pairing.
      out.btHiddenForUsb = $q("#spk-conn").style.display;
      await fetch("/test/fleet?speaker=bt");
      await refreshFleetNow();
      out.btShown = $q("#spk-conn").style.display;
      out.btDropped = { text: $q("#spk-conn-text").textContent, tone: $q("#spk-conn-text").className,
                        connectDisabled: $q("#spk-connect").disabled, pairDisabled: $q("#spk-pair").disabled };
      $q("#spk-connect").click();
      await wait(600);
      out.btConnectToast = $q("#toast").textContent;
      // The reply's "connecting" is painted at once - both buttons grey
      // out until the next poll says otherwise.
      out.btAfterConnect = { text: $q("#spk-conn-text").textContent, pairDisabled: $q("#spk-pair").disabled };
      await refreshFleetNow();
      var pairConfirms = [];
      window.confirm = function (msg) { pairConfirms.push(msg); return true; };
      $q("#spk-pair").click();
      await wait(600);
      window.confirm = function () { return true; };
      out.btPairConfirms = pairConfirms;
      out.btPairToast = $q("#toast").textContent;
      out.btSent = (await sent()).filter(function (s) { return s[0].indexOf("speaker_") === 0; });
      await fetch("/test/fleet?speaker=btok");
      await refreshFleetNow();
      out.btOk = { text: $q("#spk-conn-text").textContent, tone: $q("#spk-conn-text").className,
                   connectDisabled: $q("#spk-connect").disabled, pairDisabled: $q("#spk-pair").disabled };
      await fetch("/test/fleet?speaker=btpair");
      await refreshFleetNow();
      out.btPairing = { text: $q("#spk-conn-text").textContent, tone: $q("#spk-conn-text").className,
                        connectDisabled: $q("#spk-connect").disabled, pairDisabled: $q("#spk-pair").disabled };
      out.btVolumeStays = $q("#show-music-hostvol").style.display;
      await fetch("/test/fleet?speaker=null");
      await refreshFleetNow();
      out.noteHiddenWithout = $q("#show-music-host").style.display;
      out.btHiddenWithout = $q("#spk-conn").style.display;

      // 4. The tiles' Wi-Fi rows, and the fleet-wide buttons.
      out.wifiRows = Array.prototype.slice.call(document.querySelectorAll("#tiles .row"))
        .filter(function (r) { return r.firstElementChild && r.firstElementChild.textContent === "Wi-Fi"; })
        .map(function (r) { return r.querySelector("b").textContent; });
      $q("#wifi-hotspot").click();
      await wait(1500);
      out.wifiToast = $q("#toast").textContent;
      type("wifi-router-name", "show-router");
      $q("#wifi-router").click();
      await wait(1500);
      out.wifiSent = (await sent()).filter(function (s) { return s[0] === "wifi_select"; });

      // 5. Send workspace to ...: the default address, the job, the reply.
      out.sendToDefault = $q("#ws-send-to").value;
      type("ws-send-to", "10.42.0.1:8765");
      $q("#ws-send").click();
      await wait(2500);
      out.sendLog = $q("#ws-send-log").textContent;
      out.sendSent = (await sent()).filter(function (s) { return s[0] === "send"; });
      try { out.sendToStored = localStorage.getItem("ws.sendTo"); } catch (e) { out.sendToStored = "n/a"; }

      // 6. The passcode: the first 401 asks once, the answer is stored and
      //    sent with every request after that; a wrong one asks again.
      await fetch("/test/fleet?passcode=open-sesame");
      var prompts = [];
      var answers = ["wrong", "open-sesame"];
      window.prompt = function (msg) { prompts.push(msg); return answers.shift() || null; };
      var before = (await sent()).length;
      type("show-loop-wait", "50");                    // the Loop is on: a save
      await wait(1500);
      out.passcodeSent = (await sent()).slice(before);
      out.passcodePrompts = prompts.length;
      try { out.passcodeStored = localStorage.getItem("conductor.passcode"); } catch (e) { out.passcodeStored = "n/a"; }
      out.passcodeCookie = document.cookie.indexOf("passcode=open-sesame") >= 0;
      before = (await sent()).length;
      type("show-loop-wait", "55");
      await wait(1200);
      out.passcodeNext = (await sent()).slice(before);
      out.passcodePromptsAfter = prompts.length;
    } catch (e) { out.error = String((e && e.stack) || e); }
    var pre = document.createElement("pre");
    pre.id = "page-out";
    pre.textContent = JSON.stringify(out);
    document.body.appendChild(pre);
  })();
})();
</script>
"""


@pytest.fixture(scope="module")
def page(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("exhibitionpage")
    _require_browser(tmp)
    stand = _Stand(tmp, _PAGE_PROBE)
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


def test_the_loop_control_talks_to_api_loop(page):
    assert page["loopOffAtStart"] == {"checked": False, "wait": "45"}
    assert page["loopOn"] == {"checked": True, "state": 45}
    assert page["loopWait"] == {"field": "60", "state": 60}
    assert page["loopBad"]["field"] == "60" and "40 to 600" in page["loopBad"]["toast"]
    assert page["loopOff"] == {"checked": False, "state": None, "field": "60"}
    assert page["loopSent"][:3] == [["loop", {"on": True, "wait_s": 45}],
                                    ["loop", {"on": True, "wait_s": 60}],
                                    ["loop", {"on": False}]], page["loopSent"]


def test_the_clock_and_the_board_count_the_next_run_down(page):
    clock = page["loopClock"]
    assert re.fullmatch(r"ENDED · NEXT RUN IN 0:2[45] \(run 2\)", clock["word"]), clock
    head = page["loopHead"]
    assert head["cap"] in ("NEXT RUN in 25 s", "NEXT RUN in 24 s"), head
    assert head["count"] in ("0:25", "0:24"), head
    assert head["note"].startswith("Loop: the show starts again"), head
    assert page["stageCap"] in ("NEXT RUN in 25 s", "NEXT RUN in 24 s")
    assert "The Loop stops too" in page["stopQuestion"]
    assert page["endedClock"]["word"] == "ENDED"
    assert page["endedCap"] == "SHOW ENDED"


def test_start_during_the_loop_wait_is_the_next_run_without_force(page):
    assert page["overDuringWait"] is True and page["overWhenEnded"] is True
    assert page["overWhenRunning"] is False
    start = page["startDuringWait"]
    assert start and start[1]["force"] is False and start[1]["split_ok"] is False, start
    assert start[1]["lead_s"] == 11
    # Not "The show is running. Start it again ...?" - the run is over.
    assert not any("is running" in c for c in page["startConfirms"]), page["startConfirms"]


def test_the_passcode_is_asked_once_and_sent_ever_after(page):
    sent = page["passcodeSent"]
    # Refused (no passcode), asked, refused (the wrong one), asked, accepted.
    kinds = [s[0] for s in sent]
    assert kinds == ["refused", "refused", "loop"], sent
    assert sent[0][1]["given"] is None and sent[1][1]["given"] == "wrong"
    assert page["passcodePrompts"] == 2
    assert page["passcodeStored"] == "open-sesame" and page["passcodeCookie"] is True
    # The next request carries it without asking.
    assert [s[0] for s in page["passcodeNext"]] == ["loop"], page["passcodeNext"]
    assert page["passcodePromptsAfter"] == 2


def test_a_speaker_conductor_mutes_the_page_once_and_the_operator_wins(page):
    assert page["mutedBefore"] is False, "a fresh browser starts unmuted without a speaker"
    assert page["mutedWithSpeaker"] is True
    assert page["speakerShown"] == ""
    assert page["speakerNote"].startswith("music plays on the Conductor host (USB speaker)")
    assert "loaded, show.mp3" in page["speakerNote"] and "62 ms early" in page["speakerNote"]
    assert page["unmuted"] is False and page["staysUnmuted"] is False
    assert page["storedMuted"] == "0"
    assert page["noteHiddenWithout"] == "none"


def test_the_bluetooth_speakers_line_says_the_state_and_the_buttons_post(page):
    assert page["btHiddenForUsb"] == "none", "a USB speaker has no connection to show"
    assert page["btShown"] == ""
    dropped = page["btDropped"]
    assert dropped["text"] == ("Bose Flex SoundLink · not connected (Failed to connect: "
                               "org.bluez.Error.Failed - another phone? off?) · retry in 18 s"), dropped
    assert dropped["tone"] == "err"
    assert dropped["connectDisabled"] is False and dropped["pairDisabled"] is False
    assert page["btConnectToast"] == "Speaker: connecting…"
    assert page["btAfterConnect"] == {"text": "Bose Flex SoundLink · connecting…", "pairDisabled": True}
    # Re-pair asks first - the operator has to put the speaker in pairing
    # mode and switch the phones' Bluetooth off - then posts.
    assert len(page["btPairConfirms"]) == 1, (page["btPairConfirms"], page["btPairToast"], page["btSent"])
    assert "pairing mode" in page["btPairConfirms"][0] and "Bluetooth OFF" in page["btPairConfirms"][0]
    assert page["btPairToast"].startswith("Speaker: re-pairing")
    assert page["btSent"] == [["speaker_connect", {}], ["speaker_pair", {}]], page["btSent"]
    ok = page["btOk"]
    assert ok["text"] == "Bose Flex SoundLink · connected" and ok["tone"] == "ok"
    assert ok["connectDisabled"] is True and ok["pairDisabled"] is False
    pairing = page["btPairing"]
    assert pairing["text"].startswith("Bose Flex SoundLink · pairing: scanning… forgetting the old pairing")
    assert pairing["tone"] == "busy"
    assert pairing["connectDisabled"] is True and pairing["pairDisabled"] is True
    assert page["btVolumeStays"] == "", "the volume slider stays beside the connection line"
    assert page["btHiddenWithout"] == "none"


def test_the_tiles_show_the_units_wifi_and_the_buttons_switch_the_fleet(page):
    rows = page["wifiRows"]
    assert any("AZ-Epaper · 10.42.0.101 · 72%" in r for r in rows), rows
    assert any("AZ-Epaper · 10.42.0.1 · hotspot" in r for r in rows), rows
    assert "radxa-01 accepted" in page["wifiToast"] and "radxa-02 (a show is running)" in page["wifiToast"]
    assert page["wifiSent"] == [["wifi_select", {"profile": "AZ-Epaper", "after_s": 20}],
                                ["wifi_select", {"profile": "show-router", "after_s": 20}]]


def test_send_workspace_polls_the_job_to_the_receivers_reply(page):
    assert page["sendToDefault"] == "radxa-05:8765"
    assert page["sendSent"] == [["send", {"to": "10.42.0.1:8765"}]]
    assert page["sendToStored"] == "10.42.0.1:8765"
    log = page["sendLog"]
    assert log.startswith("Sent to 10.42.0.1:8765 ("), log
    assert "3.0 MB" in log and "6 CSV file(s), 3 cue(s), music show.mp3, history yes" in log
    assert "radxa-01 abc123def4, radxa-02 0123456789" in log
