/* transport.js — play / pause / stop / playhead / music, extracted and adapted
 * from conductor/web/index.html (as of commit 243bbb1): the singleton Audio
 * and playerUrl (376-378), syncPlayerSrc (395-407), updatePlayheadDom
 * (1314-1321), renderTransport (1322-1324), haltPlaybackAt (1328-1333), tick
 * (1335-1360), startPlayback/pausePlayback/stopPlayback/togglePlayback
 * (1362-1388), the "ended"/"seeked" listeners (1392-1393), and the playhead-
 * drag pointer handlers (rulerSeconds/rulerX/scrubTo/the pointerdown/move/up/
 * cancel listeners, 2308-2378).
 *
 * The operator page wires all of this straight to module-level globals
 * (`ui.playing`, `state.show.duration`, `THUMBS.timeline`, a `renderTimeline()`
 * that owns #content, a server upload for music). None of that exists for the
 * designer, and the frozen API (plan_designer_sim.md §2.3) does not name a
 * `SIM.transport` shape at all - `play()/pause()/stop()/seek()` are `SIM.app`
 * methods instead. So this module is not itself part of the frozen surface:
 * it is a factory, `SIM.transport.create(cfg)`, that designer-app.js (the one
 * file allowed to know about `SIM.app`) instantiates once and drives; the
 * maths and control flow inside each method are unchanged from index.html.
 *
 * Music is an object URL (URL.createObjectURL), never uploaded anywhere -
 * plan_designer_sim.md §3.7. setMusic() does not care where that URL came
 * from, and there are now two sources: the file the designer picks for a
 * session, and the show's own audio built into the page by
 * tools/build_designer.py --music (designer-app.js's built-in track). The
 * choice between them is designer-app.js's business; this module only ever
 * sees the winner.
 */
(function () {
  "use strict";

  const MAX_MUSIC = 64 * 1024 * 1024;

  // cfg: {
  //   duration(): number,                    show.duration right now
  //   isActive(): bool,                       true while the Timeline tab is showing
  //   onPlayingChange(playing),               update the Play/Pause button
  //   onPlayhead(t),                          cheap per-frame DOM poke (marker position, time labels)
  //   onThumbTick(t),                         throttled (~100ms) redraw of the looks row
  //   toast(msg),
  //   onMusicLength(),                        optional: the music's length became known (or went)
  // }
  function create(cfg) {
    const player = new Audio();
    let playerUrl = null;
    let playing = false, anchor = 0, startT = 0, playhead = 0;
    let seeking = false;               // a manual scrub is in progress - tick() must not fight it
    let lastThumbAt = 0;
    // Past the end of the music, the show goes on in silence to its END on
    // the wall clock (2026-09-28: a show may now be longer than its music -
    // index.html's ui.pastMusic, the same rule). The show's clock is the
    // master: at END the music stops with it, however much is left.
    let pastMusic = false;
    const MUSIC_END_SLACK_S = 0.05;
    function musicLength() {
      return playerUrl && Number.isFinite(player.duration) && player.duration > 0 ? player.duration : 0;
    }
    function runOnPastTheMusic(t) { pastMusic = true; anchor = performance.now(); startT = t; }
    // A seek while playing: past the end of the track the wall clock runs
    // on from there; back inside it, the music picks up again.
    function followSeek(t) {
      if (!playing || !playerUrl) return;
      const len = musicLength();
      if (len > 0 && t >= len - MUSIC_END_SLACK_S) { player.pause(); runOnPastTheMusic(t); }
      else if (pastMusic) {
        pastMusic = false;
        player.currentTime = t;
        const p = player.play();
        if (p?.catch) p.catch(err => { playing = false; cfg.onPlayingChange(false); cfg.toast("Could not play the music: " + err.message); });
      }
    }

    function updatePlayheadDom(t) { cfg.onPlayhead(t); }

    function haltPlaybackAt(t) {
      playhead = t;
      updatePlayheadDom(t);
      cfg.onThumbTick(t);
      pausePlayback();
    }
    function tick() {
      if (!playing) return;
      if (!cfg.isActive()) { pausePlayback(); return; }
      // Paused by itself (autoplay policy, media keys) stops the preview;
      // the track having ENDED does not - `ended` is read here as well as
      // listened for, since this frame can see the pause before the event.
      if (playerUrl && !pastMusic && player.paused) {
        if (player.ended) runOnPastTheMusic(Math.max(playhead, player.currentTime));
        else { pausePlayback(); return; }
      }
      if (seeking) { requestAnimationFrame(tick); return; }
      const D = cfg.duration();
      let t = playerUrl && !pastMusic ? player.currentTime : (performance.now() - anchor) / 1000 + startT;
      if (t >= D) { haltPlaybackAt(D); return; }
      playhead = t;
      updatePlayheadDom(t);
      const now = performance.now();
      if (now - lastThumbAt >= 100) { lastThumbAt = now; cfg.onThumbTick(t); }
      requestAnimationFrame(tick);
    }
    function startPlayback() {
      if (playing || !cfg.isActive()) return;
      playing = true;
      pastMusic = false;
      const len = musicLength();
      if (playerUrl && len > 0 && playhead >= len - MUSIC_END_SLACK_S) {
        player.pause();                  // nothing left to hear, but the show goes on
        runOnPastTheMusic(playhead);
      } else if (playerUrl) {
        player.currentTime = playhead;
        const p = player.play();
        if (p?.catch) p.catch(err => { playing = false; cfg.onPlayingChange(false); cfg.toast("Could not play the music: " + err.message); });
      } else {
        anchor = performance.now(); startT = playhead;
      }
      cfg.onPlayingChange(true);
      requestAnimationFrame(tick);
    }
    function pausePlayback() {
      if (!playing) return;
      playing = false;
      pastMusic = false;
      player.pause();
      cfg.onPlayingChange(false);
    }
    function stopPlayback() {
      playing = false;
      pastMusic = false;
      player.pause();
      if (playerUrl) player.currentTime = 0;
      playhead = 0;
      // Unconditionally, not just in the no-music branch (adversarial
      // review, 2026-09-25): Stop pressed mid-drag (or called
      // programmatically while a scrub was outstanding) used to leave
      // `seeking` true forever, since nothing else was going to clear it -
      // tick() checks that flag on every frame and would have refused to
      // move the playhead again until the next manual drag happened to
      // finish cleanly.
      seeking = false;
      cfg.onPlayingChange(false);
      updatePlayheadDom(0);
      cfg.onThumbTick(0);
    }
    function togglePlayback() { playing ? pausePlayback() : startPlayback(); }
    function seekTo(t) {
      const D = cfg.duration();
      t = Math.max(0, Math.min(D, t));
      playhead = t;
      updatePlayheadDom(t);
      cfg.onThumbTick(t);
      if (playerUrl) { player.currentTime = t; followSeek(t); }
      else if (playing) { anchor = performance.now(); startT = t; }
    }
    // The track ended before the show: carry on in silence (it used to stop
    // dead here, at the end of the track rather than the end of the show).
    player.addEventListener("ended", () => {
      if (playing && !pastMusic) runOnPastTheMusic(Math.max(playhead, player.currentTime));
    });
    player.addEventListener("seeked", () => { seeking = false; });
    for (const ev of ["loadedmetadata", "durationchange", "emptied"])
      player.addEventListener(ev, () => { if (cfg.onMusicLength) cfg.onMusicLength(); });

    function setMusic(url) {
      if (url === playerUrl) return;
      playerUrl = url;
      player.pause();
      if (url) player.src = url;
      else { player.removeAttribute("src"); player.load(); }
      playing = false;
      pastMusic = false;
      cfg.onPlayingChange(false);
    }

    // Ruler-drag scrubbing (index.html:2308-2372), parameterised on the ruler
    // element instead of a hard-coded "#ruler" id so a page can host more than
    // one instance (dev sandbox pages, tests) without them fighting.
    function rulerSeconds(ruler, clientX) {
      const D = cfg.duration();
      if (!ruler) return playhead;
      const box = ruler.getBoundingClientRect();
      return Math.max(0, Math.min(D, (clientX - box.left) / box.width * D));
    }
    function rulerX(ruler, t) {
      const D = cfg.duration();
      if (!ruler || !D) return 0;
      const box = ruler.getBoundingClientRect();
      return box.left + (t / D) * box.width;
    }
    let phDrag = null;
    function beginDrag(ruler, clientX, grabbedHead) {
      seeking = true;
      const grabOffset = grabbedHead ? clientX - rulerX(ruler, playhead) : 0;
      phDrag = { ruler, grabOffset, lastSeekAt: 0 };
      seekScrub(rulerSeconds(ruler, clientX - grabOffset));
    }
    function seekScrub(t) {
      playhead = t;
      updatePlayheadDom(t);
      cfg.onThumbTick(t);
      if (playerUrl) {
        const now = performance.now();
        if (phDrag && now - phDrag.lastSeekAt >= 250) { phDrag.lastSeekAt = now; player.currentTime = t; }
      }
    }
    function dragMove(clientX) {
      if (!phDrag) return;
      seekScrub(rulerSeconds(phDrag.ruler, clientX - phDrag.grabOffset));
    }
    function endDrag() {
      if (!phDrag) return;
      phDrag = null;
      const t = playhead;
      if (playerUrl) { player.currentTime = t; followSeek(t); }
      else if (playing) { anchor = performance.now(); startT = t; }
      // Unconditionally (adversarial review, 2026-09-25), not only in the
      // no-music branch: the playerUrl branch was relying on the audio
      // element's own "seeked" event to clear this later, but a seek to a
      // currentTime the element already coalesces away (or a src that never
      // finished loading) can simply never fire it - tick() would then
      // refuse to advance the playhead again for the rest of the session.
      seeking = false;
    }
    function cancelDrag() { phDrag = null; seeking = false; }

    return {
      MAX_MUSIC,
      get playing() { return playing; },
      get playhead() { return playhead; },
      get pastMusic() { return pastMusic; },
      get musicLength() { return musicLength(); },
      setMusic, play: startPlayback, pause: pausePlayback, stop: stopPlayback, toggle: togglePlayback,
      seek: seekTo, beginDrag, dragMove, endDrag, cancelDrag,
      get seeking() { return seeking; },
    };
  }

  // The music's length against the show's (index.html's musicEndInfo(), the
  // same answers; `clock` is the page's own time format):
  //   label   "music ends 10.54"        marker  the music ends inside the show
  //   silence show left after the music note    the music outlasts the show
  //   fit     Fit to music's length: rounded UP, at most `max`
  //   tooLong the music is longer than any show can be
  function musicEndInfo(music, show, max, clock) {
    if (!(music > 0) || !(show > 0)) return null;
    const whole = Math.max(1, Math.ceil(music - 0.001));
    const over = music - show;
    return { at: music, label: "music ends " + clock(music),
             marker: music <= show, silence: Math.max(0, show - music),
             note: over > 0.001 ? `music continues ${clock(Math.max(1, over))} past the end of the show` : "",
             fit: Math.min(max, whole), tooLong: whole > max };
  }

  globalThis.SIM = Object.assign(globalThis.SIM || {}, {
    transport: { create, MAX_MUSIC, musicEndInfo },
  });
})();
