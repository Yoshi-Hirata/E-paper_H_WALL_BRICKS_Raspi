"""Where the music ends, and a show as long as 15:00 (2026-09-28).

The show's music became a 10:54 track while the show was 10:00, and the
owner asked for 「シミュレーターとコンダクターのショーの長さが音源に対して短い。
15分まで延長し、音源の終わりがわかるように。」. This boots the real
conductor/web/index.html in a headless browser against a real Workspace (a
30.4 s WAV stands in for the track) and checks what the operator sees and
what the Timeline preview does:

* the Timeline draws `music ends 0:30` at the music's end and hatches the
  rest of the show as silence; a show shorter than the music says, in amber,
  `music continues 0:10 past the end of the show`;
* Fit to music sets Show length to the music's length rounded UP, and only
  when pressed; a length past 15:00 is refused with `A show is at most 15:00`;
* THE SHOW's clock carries `music ends 0:30`;
* the preview's clock is the master - it runs on in silence after the music
  ends, and stops the music at END when the music is the longer.

The <audio> element is faked for the transport half: headless runs on
virtual time, where a media clock never advances (tests/test_conductor_music.py
says the same), so the probe plays the element's part itself.
Behind CONDUCTOR_BROWSER_TESTS=1 like every browser test here.
"""
from __future__ import annotations

import http.server
import io
import json
import re
import struct
import subprocess
import sys
import threading
from html import unescape
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
INDEX_HTML = REPO / "conductor" / "web" / "index.html"

sys.path.insert(0, str(REPO))

from conductor.server import Workspace  # noqa: E402
from tests.test_conductor_music import _free_port  # noqa: E402
from tests.test_designer_build import _find_browser, _require_browser  # noqa: E402
from tests.test_look import GRID, MAP  # noqa: E402

TRACK_S = 30.4
RATE = 8000


def _wav(seconds=TRACK_S, rate=RATE):
    """Silence, 8-bit mono: a file the browser decodes to exactly `seconds`."""
    pcm = b"\x80" * int(round(seconds * rate))
    fmt = struct.pack("<4sIHHIIHH", b"fmt ", 16, 1, 1, rate, rate, 1, 8)
    data = struct.pack("<4sI", b"data", len(pcm)) + pcm
    body = b"WAVE" + fmt + data
    return struct.pack("<4sI", b"RIFF", len(body)) + body


class _Stand:
    """index.html with the probe, and a real Workspace behind /api/state and
    POST /api/show (so a refusal is the server's own sentence)."""

    def __init__(self, tmp_path, probe):
        ws = Workspace(tmp_path / "ws")
        ws.save("Look22_map.csv", MAP)
        ws.save("Look22_color_pattern01_grid.csv", GRID)
        ws.set_timeline(600, [{"id": "a", "item": "Look22", "at": 0,
                               "design": "Look22_color_pattern01_grid.csv"}])
        wav = _wav()
        ws.save_music("track.wav", io.BytesIO(wav), len(wav))
        page = INDEX_HTML.read_text(encoding="utf-8").replace("</body>", probe + "</body>", 1)

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
                if self.path == "/api/show":
                    try:
                        ws.set_timeline(body.get("duration", 600), body.get("cues", []),
                                        body.get("refresh_s"))
                    except ValueError as exc:
                        return self._json({"error": str(exc)})
                    return self._json({"ok": True})
                return self._json({})

            def do_GET(self):
                path = self.path.split("?")[0]
                if path in ("/", "/index.html"):
                    return self._send(page.encode("utf-8"), "text/html; charset=utf-8")
                if path == "/api/state":
                    return self._json(ws.state())
                if path == "/api/music/file":
                    return self._send(wav, "audio/wav")
                if path == "/api/fleet":
                    return self._json({"units": [], "last_fire": None, "run": None,
                                       "shows": {}, "corrections": [], "prepared": {},
                                       "start_at": 0.0, "show_duration": None,
                                       "timeline": None})
                if path == "/api/fleet/demos":
                    return self._json({"units": {}, "offline": [], "failed": {}})
                return self._json({})

        self.ws = ws
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


_PROBE = """
<script>
(function () {
  var out = { error: null };
  function publish() {
    var pre = document.createElement("pre");
    pre.id = "musicend-out";
    pre.textContent = JSON.stringify(out);
    document.body.appendChild(pre);
  }
  function wait(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
  function q(sel) { return document.querySelector(sel); }
  function toastNow() { return (q("#toast") || {}).textContent || ""; }
  function timeline() {
    var m = q("#music-end"), s = q("#music-silence"), f = q("#tl-fit");
    return { marker: m ? !m.hidden : null, label: (q("#music-end-label") || {}).textContent || "",
             left: m ? m.style.left : null, flip: m ? m.classList.contains("flip") : null,
             silence: s ? !s.hidden : null, silenceLeft: s ? s.style.left : null,
             silenceWord: s ? s.textContent : null,
             note: (q("#music-note") || {}).textContent || "",
             noteClass: (q("#music-note") || {}).className || "",
             fitDisabled: f ? f.disabled : null, fitTitle: f ? f.title : null,
             fitText: f ? f.textContent : null,
             fitNextToLength: !!(f && q("#duration") && f.parentNode === q("#duration").parentNode),
             duration: state.show.duration, field: (q("#duration") || {}).value };
  }
  async function typeLength(text) {
    var d = q("#duration");
    d.value = text;
    d.dispatchEvent(new Event("change", { bubbles: true }));
    await wait(700);
  }
  (async function () {
    try {
      for (var i = 0; i < 200 && state === null; i++) await wait(50);
      ui.tab = "timeline"; render();
      for (var j = 0; j < 100 && !(player.duration > 0); j++) await wait(50);
      await wait(200);
      out.musicLength = player.duration;

      // 1. The stored 10:00 show and a 0:30 track: the line and the silence.
      out.longShow = timeline();

      // 2. A show shorter than the music: no line, an amber note instead.
      await typeLength("0:20");
      out.shortShow = timeline();

      // 3. Fit to music: the length rounded UP, and said so.
      q("#toast").textContent = "";
      q("#tl-fit").click();
      await wait(700);
      out.fitted = timeline();
      out.fitToast = toastNow();

      // 4. Past 15:00 is refused and changes nothing; 15:00 itself is fine.
      await typeLength("15:01");
      out.refused = { toast: toastNow(), duration: state.show.duration, field: q("#duration").value };
      await typeLength("15:00");
      out.fifteen = timeline();

      // 5. THE SHOW's clock says where the music ends...
      ui.tab = "fleet"; render();
      await wait(1500);
      out.showClock = { text: (q("#show-clock") || {}).textContent || "",
                        end: (q("#show-music-end") || {}).textContent || "",
                        over: (q("#show-music-over") || {}).textContent || "" };
      // ...and that it runs past a show shorter than it.
      await saveShow(state.show.cues, 20);
      ui.tab = "fleet"; render();
      await wait(1500);
      out.showClockShort = { end: (q("#show-music-end") || {}).textContent || "",
                             over: (q("#show-music-over") || {}).textContent || "",
                             overColor: (function () { var e = q("#show-music-over");
                               return e ? getComputedStyle(e).color : ""; })(),
                             warnColor: getComputedStyle(document.documentElement).getPropertyValue("--warn").trim() };

      // 6. The preview's clock, with the element played by the probe - and
      //    its frames too: a headless dump never runs requestAnimationFrame,
      //    so step() calls tick() itself, as each frame would.
      function step() { tick(); }
      var fake = { paused: true, ended: false, t: 0, plays: 0, pauses: 0 };
      Object.defineProperty(player, "paused", { configurable: true, get: function () { return fake.paused; } });
      Object.defineProperty(player, "ended", { configurable: true, get: function () { return fake.ended; } });
      Object.defineProperty(player, "currentTime", { configurable: true,
        get: function () { return fake.t; }, set: function (v) { fake.t = v; } });
      player.play = function () { fake.plays++; fake.paused = false; fake.ended = false; return Promise.resolve(); };
      player.pause = function () { fake.pauses++; fake.paused = true; };

      // 6a. The music ends at 0:30 inside a 0:40 show: the clock runs on.
      await saveShow(state.show.cues, 40);
      ui.tab = "timeline"; render();
      await wait(300);
      ui.playhead = 28; startPlayback();
      out.startedInside = { playing: ui.playing, plays: fake.plays, pastMusic: ui.pastMusic };
      fake.t = 29;
      await wait(300); step();
      fake.t = player.duration; fake.paused = true; fake.ended = true;
      player.dispatchEvent(new Event("ended"));
      await wait(2000); step();
      out.afterTheMusic = { playing: ui.playing, pastMusic: ui.pastMusic, playhead: ui.playhead,
                            plays: fake.plays };
      // 6b. ...to END, where it stops.
      await wait(12000); step();
      out.atTheEnd = { playing: ui.playing, playhead: ui.playhead, paused: fake.paused };

      // 6c. Play from past the end of the music: nothing to hear, the clock runs.
      stopPlayback();
      ui.playhead = 34; var playsBefore = fake.plays;
      startPlayback();
      await wait(1000); step();
      out.startedPast = { playing: ui.playing, pastMusic: ui.pastMusic,
                          played: fake.plays - playsBefore, playhead: ui.playhead };
      // 6d. A seek back inside the music while running on: the music resumes there.
      player.currentTime = 10; previewSeeked(10);
      await wait(300);
      out.seekedBack = { playing: ui.playing, pastMusic: ui.pastMusic,
                         played: fake.plays - playsBefore, at: fake.t };
      pausePlayback();

      // 6e. Music longer than the show (0:30 track, 0:20 show): END stops it.
      await saveShow(state.show.cues, 20);
      ui.tab = "timeline"; render();
      await wait(300);
      ui.playhead = 18; startPlayback();
      fake.t = 19; await wait(300); step();
      var pausesBefore = fake.pauses;
      fake.t = 20.3; await wait(500); step();
      out.musicLonger = { playing: ui.playing, playhead: ui.playhead,
                          paused: fake.paused, pausedByTheEnd: fake.pauses > pausesBefore };
    } catch (e) { out.error = String((e && e.stack) || e); }
    publish();
  })();
})();
</script>
"""


def _dump(url, tmp_path):
    browser = _find_browser()
    args = [browser, "--headless=new", "--disable-gpu", "--no-sandbox", "--no-first-run",
            "--autoplay-policy=no-user-gesture-required", "--mute-audio",
            f"--user-data-dir={tmp_path / 'user-data'}",
            "--virtual-time-budget=90000", "--dump-dom", url]
    return subprocess.run(args, capture_output=True, timeout=180).stdout.decode(
        "utf-8", errors="replace")


@pytest.fixture(scope="module")
def page(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("musicend")
    _require_browser(tmp)
    stand = _Stand(tmp, _PROBE)
    try:
        dom = _dump(stand.url, tmp)
    finally:
        stand.close()
    match = re.search(r'<pre id="musicend-out">(.*?)</pre>', dom or "", re.S)
    assert match, f"no #musicend-out in the dumped DOM:\n{(dom or '')[:3000]}"
    data = json.loads(unescape(match.group(1)))
    assert data.get("error") is None, data["error"]
    assert abs((data["musicLength"] or 0) - TRACK_S) < 0.05, data
    return data


def test_the_timeline_marks_where_the_music_ends_and_the_silence_after_it(page):
    long = page["longShow"]
    assert long["duration"] == 600
    assert long["marker"] is True and long["label"] == "music ends 0:30", long
    # 30.4 s of a 600 s show: 5.067% along the track.
    assert long["left"] == f"{100 * TRACK_S / 600:.3f}%", long
    assert long["flip"] is False
    assert long["silence"] is True and long["silenceLeft"] == long["left"], long
    assert long["silenceWord"] == "silence"
    assert long["note"] == "", long


def test_a_show_shorter_than_the_music_says_how_much_music_is_left(page):
    short = page["shortShow"]
    assert short["duration"] == 20
    assert short["marker"] is False and short["silence"] is False, short
    assert short["note"] == "music continues 0:10 past the end of the show", short
    assert "warn" in short["noteClass"].split(), short


def test_fit_to_music_sits_next_to_show_length_and_rounds_up(page):
    long = page["longShow"]
    assert long["fitText"] == "Fit to music" and long["fitNextToLength"], long
    assert long["fitDisabled"] is False, "the music's length is known, Fit must be live"
    assert "0:31" in long["fitTitle"], long
    fitted = page["fitted"]
    # 30.4 s rounds UP to 31, never down to the music being cut.
    assert fitted["duration"] == 31 and fitted["field"] == "0:31", fitted
    assert fitted["note"] == "" and fitted["marker"] is True, fitted
    assert page["fitToast"] == "Show length is 0:31: the music's length, rounded up.", page["fitToast"]


def test_a_show_past_fifteen_minutes_is_refused(page):
    refused = page["refused"]
    assert refused["toast"] == "A show is at most 15:00", refused
    assert refused["duration"] == 31 and refused["field"] == "0:31", refused
    fifteen = page["fifteen"]
    assert fifteen["duration"] == 900 and fifteen["field"] == "15:00", fifteen
    assert fifteen["marker"] is True and fifteen["silence"] is True


def test_the_show_clock_says_where_the_music_ends(page):
    clock = page["showClock"]
    assert clock["end"] == "music ends 0:30", clock
    assert clock["over"] == "", clock
    short = page["showClockShort"]
    assert short["end"] == "music ends 0:30", short
    assert short["over"] == "music continues 0:10 past the end of the show", short


def test_the_preview_clock_runs_on_in_silence_after_the_music(page):
    started = page["startedInside"]
    assert started == {"playing": True, "plays": 1, "pastMusic": False}, started
    after = page["afterTheMusic"]
    # It used to stop dead at the end of the track.
    assert after["playing"] is True and after["pastMusic"] is True, after
    assert after["playhead"] > TRACK_S + 1, after
    assert after["plays"] == 1, "nothing tried to play the track again"
    end = page["atTheEnd"]
    assert end["playing"] is False and end["playhead"] == 40, end


def test_the_preview_can_start_past_the_music_and_seek_back_into_it(page):
    past = page["startedPast"]
    assert past["playing"] is True and past["pastMusic"] is True, past
    assert past["played"] == 0, "play() was called on a track that has ended"
    assert past["playhead"] > 34, past
    back = page["seekedBack"]
    assert back["pastMusic"] is False and back["played"] == 1 and back["at"] == 10, back


def test_the_end_of_the_show_stops_music_that_is_longer(page):
    longer = page["musicLonger"]
    assert longer["playing"] is False and longer["playhead"] == 20, longer
    assert longer["paused"] is True and longer["pausedByTheEnd"] is True, longer
