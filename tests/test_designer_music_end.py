"""The designers' simulator: a show up to 15:00, and where the music ends
(2026-09-28) - the same as the operator's Timeline
(tests/test_conductor_music_end.py), in the simulator's own mm.ss.

One headless run of the committed dist/az27ss-simulator.html with a probe
appended (never shipped): a 30.4 s WAV is picked as the music, and the probe
reads the marker, the note, Fit to music and the 15:00 refusal, then plays
the <audio> element's part itself to check the preview's clock runs on past
the end of the music and stops the music at END. Behind
CONDUCTOR_BROWSER_TESTS=1 like every browser test here.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tests.test_designer_build import DIST, _probe_page, _require_browser  # noqa: E402

TRACK_S = 30.4

_PROBE = """
<script>
(function () {
  var out = { error: null };
  function publish() {
    var pre = document.createElement("pre");
    pre.id = "simmusicend-out";
    pre.textContent = JSON.stringify(out);
    document.body.appendChild(pre);
  }
  function wait(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
  function q(sel) { return document.querySelector(sel); }
  function toastNow() { return (q("#toast") || {}).textContent || ""; }
  function ready() {
    try { var st = SIM.app.getState(); return !!(st && st.items && st.items.length); }
    catch (e) { return false; }
  }
  function wav(seconds, rate) {
    var n = Math.round(seconds * rate), buf = new ArrayBuffer(44 + n), v = new DataView(buf);
    function str(o, s) { for (var i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); }
    str(0, "RIFF"); v.setUint32(4, 36 + n, true); str(8, "WAVE"); str(12, "fmt ");
    v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
    v.setUint32(24, rate, true); v.setUint32(28, rate, true); v.setUint16(32, 1, true);
    v.setUint16(34, 8, true); str(36, "data"); v.setUint32(40, n, true);
    for (var i = 0; i < n; i++) v.setUint8(44 + i, 0x80);
    return new File([buf], "probe-track.wav", { type: "audio/wav" });
  }
  function timeline() {
    var m = q("#music-end"), s = q("#music-silence"), f = q("#tl-fit");
    return { marker: m ? !m.hidden : null, label: (q("#music-end-label") || {}).textContent || "",
             left: m ? m.style.left : null, silence: s ? !s.hidden : null,
             note: (q("#music-note") || {}).textContent || "",
             noteClass: (q("#music-note") || {}).className || "",
             fitDisabled: f ? f.disabled : null, fitText: f ? f.textContent : null,
             fitTitle: f ? f.title : null,
             duration: SIM.app.getState().show.duration,
             field: (q("#show-duration") || {}).value };
  }
  async function typeLength(text) {
    var d = q("#show-duration");
    d.value = text;
    d.dispatchEvent(new Event("change", { bubbles: true }));
    await wait(400);
  }
  (async function () {
    try {
      for (var i = 0; i < 200 && !ready(); i++) await wait(50);
      q('[data-tab="timeline"]').click();
      await wait(200);
      out.beforeMusic = timeline();
      SIM.app.pickMusic(wav(30.4, 8000));
      for (var j = 0; j < 100 && q("#tl-fit") && q("#tl-fit").disabled; j++) await wait(50);
      await wait(200);
      out.musicInfo = SIM.transport.musicEndInfo(654.25, 600, 900, SIM.mmss.format);
      out.tooLong = SIM.transport.musicEndInfo(962.2, 900, 900, SIM.mmss.format);

      out.longShow = timeline();                       // the starter's 10:00 show
      await typeLength("0.20");
      out.shortShow = timeline();
      q("#toast").textContent = "";
      q("#tl-fit").click();
      await wait(400);
      out.fitted = timeline();
      out.fitToast = toastNow();
      await typeLength("15.01");
      out.refused = { toast: toastNow(), duration: SIM.app.getState().show.duration,
                      field: q("#show-duration").value };
      await typeLength("15.00");
      out.fifteen = timeline();

      // The preview's clock. A headless dump runs no animation frames, so
      // they are timers here; and the media element is played by the probe
      // (its clock never advances under virtual time).
      window.requestAnimationFrame = function (cb) { return setTimeout(function () { cb(performance.now()); }, 20); };
      var fake = { paused: true, ended: false, t: 0, plays: 0, pauses: 0 };
      var P = HTMLMediaElement.prototype;
      Object.defineProperty(P, "paused", { configurable: true, get: function () { return fake.paused; } });
      Object.defineProperty(P, "ended", { configurable: true, get: function () { return fake.ended; } });
      Object.defineProperty(P, "currentTime", { configurable: true,
        get: function () { return fake.t; }, set: function (v) { fake.t = v; } });
      P.play = function () { fake.plays++; fake.paused = false; fake.ended = false; return Promise.resolve(); };
      P.pause = function () { fake.pauses++; fake.paused = true; };

      await typeLength("0.40");
      SIM.app.seek(28); SIM.app.play();
      fake.t = 29; await wait(200);
      fake.t = 30.4; fake.paused = true; fake.ended = true;     // the track runs out
      await wait(2000);
      out.afterTheMusic = { time: (q("#ph-time") || {}).textContent, plays: fake.plays,
                            button: (q("#tp-play") || {}).textContent };
      await wait(10000);
      out.atTheEnd = { time: (q("#ph-time") || {}).textContent,
                       button: (q("#tp-play") || {}).textContent };
      SIM.app.stop();

      await typeLength("0.20");                          // music longer than the show
      SIM.app.seek(18); SIM.app.play();
      fake.t = 19; await wait(200);
      var pausesBefore = fake.pauses;
      fake.t = 20.3; await wait(300);
      out.musicLonger = { time: (q("#ph-time") || {}).textContent,
                          button: (q("#tp-play") || {}).textContent,
                          paused: fake.paused, pausedByTheEnd: fake.pauses > pausesBefore };
    } catch (e) { out.error = String((e && e.stack) || e); }
    publish();
  })();
})();
</script>
"""


@pytest.fixture(scope="module")
def sim(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("simmusicend")
    _require_browser(tmp)
    assert DIST.exists(), "dist/az27ss-simulator.html has not been built yet"
    return _probe_page(tmp, _PROBE, "simmusicend-out")


def test_the_pure_comparison_reads_the_real_track(sim):
    info = sim["musicInfo"]            # 10:54.25 against the old 10:00 show
    assert info["label"] == "music ends 10.54"
    assert info["marker"] is False
    assert info["note"] == "music continues 0.54 past the end of the show"
    assert info["fit"] == 655 and info["tooLong"] is False
    too = sim["tooLong"]               # 16:02 is more than any show can be
    assert too["fit"] == 900 and too["tooLong"] is True


def test_fit_is_off_until_there_is_music(sim):
    assert sim["beforeMusic"]["fitDisabled"] is True
    assert sim["beforeMusic"]["fitText"] == "Fit to music"
    assert sim["beforeMusic"]["marker"] is False and sim["beforeMusic"]["note"] == ""


def test_the_marker_the_silence_and_the_note(sim):
    long = sim["longShow"]
    assert long["duration"] == 600 and long["fitDisabled"] is False, long
    assert long["marker"] is True and long["label"] == "music ends 0.30", long
    assert long["left"] == f"{100 * TRACK_S / 600:.3f}%" and long["silence"] is True, long
    short = sim["shortShow"]
    assert short["duration"] == 20 and short["marker"] is False, short
    assert short["note"] == "music continues 0.10 past the end of the show", short
    assert "warn" in short["noteClass"].split()


def test_fit_to_music_rounds_up_and_a_show_is_at_most_15_minutes(sim):
    assert sim["fitted"]["duration"] == 31 and sim["fitted"]["note"] == "", sim["fitted"]
    assert sim["fitToast"] == "Show length is 0.31: the music's length, rounded up."
    refused = sim["refused"]
    assert refused["duration"] == 31 and refused["field"] == "0.31", refused
    assert refused["toast"] == "A show is at most 15.00 - kept 0.31.", refused
    assert sim["fifteen"]["duration"] == 900, sim["fifteen"]


def test_the_preview_runs_on_in_silence_and_stops_at_the_end(sim):
    after = sim["afterTheMusic"]
    assert after["button"].endswith("Pause"), after      # still playing
    assert after["time"] in ("0.32", "0.33"), after
    assert after["plays"] == 1, after
    end = sim["atTheEnd"]
    assert end["time"] == "0.40" and end["button"].endswith("Play"), end


def test_the_end_of_the_show_stops_music_that_is_longer(sim):
    longer = sim["musicLonger"]
    assert longer["time"] == "0.20" and longer["button"].endswith("Play"), longer
    assert longer["paused"] is True and longer["pausedByTheEnd"] is True, longer
