/* designer-app.js — the designers' simulator app: store, autosave, CSV drops,
 * item/design editing, the timeline (tracks/cue editor/cue table/dock), music,
 * bundle save/open, and the Japanese Help tab. This is a fresh page built for
 * plan_designer_sim.md §3-§4 (not an index.html extraction): the operator's
 * page is single-workspace/server-backed with a unit/board/DIP vocabulary this
 * page must never show; the shapes below (project, SIM.app) are new, but the
 * playhead/drag/track math follows the same patterns as index.html's
 * renderTimeline()/renderCueEditor() (see transport.js's own header for the
 * exact source lines that WERE extracted).
 *
 * SIM.app is the frozen seam (plan_designer_sim.md §2.3): selftest.js drives
 * it for the round-trip smoke test once Coder P's selftest.js is wired in.
 */
(function () {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const esc = s => String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  // Cue ids are strings on the model side (timeline.py:241's
  // str(raw.get("id") or f"c{len(result)}")[:40], ported as-is by
  // model.js's clean()) but this page's own addCue() used to hand back a
  // bare JS number, and several places compared with `===` or wrapped one
  // side in Number(...) - either way a real id ("2") never matched a
  // stray-typed one (2), so EDIT CUE/selection/drag silently never found
  // their cue (adversarial review, 2026-09-25). Compare through this
  // helper everywhere, never bare `===` or Number(...), regardless of which
  // side happens to be a string today.
  const sameId = (a, b) => String(a) === String(b);
  const PROJECT_KEY = "az27ss.project.v1";
  const UI_KEY = "az27ss.ui.v1";
  const MAX_PROJECT_BYTES = 4 * 1024 * 1024;
  const WARN_BUNDLE_BYTES = 6 * 1024 * 1024;
  const REFUSE_BUNDLE_BYTES = 8 * 1024 * 1024;
  const MAX_SHOW_DURATION_S = 99 * 60 + 59;   // the Show length field's own 99:59 ceiling

  function freshProject() {
    return { files: {}, show: { duration: 600, refresh_s: 7.0, cues: [], transitions: {}, labels: {}, boards: {}, music: null } };
  }
  function cloneStarter() {
    const s = globalThis.SIM.STARTER || { files: {}, show: {} };
    return { files: Object.assign({}, s.files), show: JSON.parse(JSON.stringify(Object.assign(freshProject().show, s.show))) };
  }

  let project = null;
  let state = null;                 // last SIM.buildState(project)
  let musicUrl = null;              // object URL of the picked File, or null
  // The File itself, kept alongside musicUrl (adversarial review round 2 -
  // F5): an object URL, once revoked, is permanently dead - a snapshot that
  // only remembers the URL STRING (as runSelfTestSafely() used to) cannot
  // recover after the self-test's own newProject() revokes it. The File
  // object has no such problem; a fresh URL can always be minted from it.
  let musicFile = null;
  let autosaveWarned = false;
  let autosaveTimer = null;

  // ------------------------------------------------------------------
  // The built-in track (SIM.embeddedMusic)
  // ------------------------------------------------------------------
  // A browser cannot open a file off the disk by itself, so a page that only
  // knows the music's NAME is a silent page - which is exactly what the
  // director's team got. tools/build_designer.py --music (and the
  // Conductor's own "Simulator for designers…" button, which is how the
  // operator regenerates this file when the music changes) embeds the show's
  // audio as SIM.embeddedMusic = {name, type, size, dataUrl}; from here on it
  // is "the built-in track", and it is what plays unless the designer picks
  // something else for the session.
  //
  // It is decoded EXACTLY ONCE, on the first ask, and the base64 string is
  // dropped straight afterwards: for the real show that string is ~23 MB and
  // the Blob another ~17.5 MB, and holding both for the life of the page for
  // no reason is the difference between a heavy file and an unusable one.
  // The resulting object URL lives as long as the page - it is never
  // revoked, because there is nothing to revoke it in favour of (a picked
  // file gets its own URL, and going back to the built-in track must not
  // find a dead one).
  let builtInUrl = null;
  let builtInDecoded = false;
  function builtInMusic() { return globalThis.SIM.embeddedMusic || null; }
  // The built-in track's name - and null unless there is decodable audio
  // behind it (adversarial review - F4). A 0-byte or corrupt embed used to
  // leave the name in place, so the music line read "NAME · built in" over
  // silence and Play simply did nothing; treating it as no embed at all
  // falls through to the honest "(re-pick the file…)" bar instead. Every
  // caller asking "is there a built-in track" asks through this.
  function builtInMusicName() {
    const m = builtInMusic();
    return (m && m.name && builtInMusicUrl()) ? m.name : null;
  }
  function builtInMusicUrl() {
    const m = builtInMusic();
    if (!m || !m.size) return null;          // an empty embed is not a track
    if (builtInDecoded) return builtInUrl;
    builtInDecoded = true;
    try {
      const url = String(m.dataUrl || "");
      const binary = atob(url.slice(url.indexOf(",") + 1));
      const bytes = new Uint8Array(binary.length);
      for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
      if (!bytes.length) { builtInUrl = null; return null; }
      builtInUrl = URL.createObjectURL(new Blob([bytes], { type: m.type || "audio/mpeg" }));
      m.dataUrl = null;        // the big string has done its job - let it go
    } catch (err) {
      builtInUrl = null;       // a truncated/corrupt embed is silent, not fatal
    }
    return builtInUrl;
  }
  // What the transport should be playing: the file the designer picked for
  // this session if there is one, otherwise the built-in track, otherwise
  // nothing. Every path that changes either of those ends here, so the two
  // can never disagree.
  function pushMusicToTransport() { ensureTransport().setMusic(musicUrl || builtInMusicUrl()); }
  // A fresh/loaded project with no music of its own adopts the built-in
  // track's name, so the bundle the designer hands back names the audio the
  // show is actually running on. A project that already names something
  // else is left alone - that disagreement is worth showing, not papering
  // over (see musicControl()).
  function adoptBuiltInName(p) {
    const name = builtInMusicName();
    if (name && p && p.show && !p.show.music) p.show.music = { name };
    return p;
  }

  const ui = { tab: "designs", item: null, design: null, view: "outside", cue: null, simView: false };

  function loadUiPrefs() {
    try {
      const raw = localStorage.getItem(UI_KEY);
      if (!raw) return;
      const p = JSON.parse(raw);
      if (p && typeof p === "object") Object.assign(ui, p, { cue: null });
    } catch {}
  }
  function saveUiPrefs() {
    try { localStorage.setItem(UI_KEY, JSON.stringify({ tab: ui.tab, item: ui.item, design: ui.design, view: ui.view, simView: ui.simView })); } catch {}
  }

  // The autosave wins over the shipped starter data, which is right - it is
  // the designer's own work - but it also meant that every designer who had
  // ever opened this page kept the old starter labels forever, with no route
  // back to the corrected ones short of "Start a new (empty) project" (which
  // throws the timeline away). Adversarial review F2: fill in a model number
  // the stored project does not have from the starter's own table, and never
  // touch one that is already there. An empty model is the absence of a
  // label, not a typed one - nothing in this page writes "" over a model
  // number except a designer clearing the box, and a cleared box comes back
  // filled rather than staying blank, which is the one cost of this.
  function backfillStarterLabels(p) {
    const starter = ((globalThis.SIM.STARTER || {}).show || {}).labels;
    if (!starter || !p || !p.show) return p;
    const labels = p.show.labels = p.show.labels || {};
    for (const item of Object.keys(starter)) {
      const stored = labels[item];
      const known = starter[item] || {};
      if (typeof stored !== "object" || stored === null || Array.isArray(stored)) {
        labels[item] = { look: String(known.look || ""), model: String(known.model || "") };
        continue;
      }
      // Only the model, and only when it is missing. The LOOK number is
      // left exactly as stored even when it is blank: the old starter DID
      // write every garment's LOOK, so a blank one now is a designer who
      // cleared the box on purpose, and putting it back would undo that.
      if (!stored.model) stored.model = String(known.model || "");
    }
    return p;
  }
  function loadProject() {
    try {
      const raw = localStorage.getItem(PROJECT_KEY);
      if (raw) { const p = JSON.parse(raw); if (p && p.files && p.show) return backfillStarterLabels(p); }
    } catch {}
    return cloneStarter();          // first ever open (empty localStorage), or a corrupt value
  }
  // Exposed for tests only, like __displaycheck below: the back-fill's whole
  // point is what happens to a project that was stored BEFORE the labels
  // were corrected, and a headless page cannot be handed one of those
  // without either seeding localStorage and reloading (which the page test
  // does) or calling this directly (which a unit-style check does).
  globalThis.__labelBackfill = backfillStarterLabels;
  function scheduleAutosave() {
    clearTimeout(autosaveTimer);
    autosaveTimer = setTimeout(() => {
      // A `project` that fails to build is exactly the one copy autosave
      // must NOT overwrite the last-good save with (adversarial review,
      // 2026-09-25, on rebuild()'s own error path: the whole point of
      // "reload to recover the autosave" only holds if the autosave itself
      // was never poisoned in the first place).
      if (buildError) return;
      try {
        const text = JSON.stringify(project);
        // Bytes, not JS string length (adversarial review, 2026-09-25): a
        // .length count under-measures anything outside the Latin-1 range
        // (design/model names, the Japanese Help text never lands in the
        // project, but a designer's own typed model number might) - the
        // 4 MB figure this compares against, and the one localStorage
        // itself enforces, are both a byte budget.
        const bytes = new Blob([text]).size;
        if (bytes > MAX_PROJECT_BYTES) {
          if (!autosaveWarned) { autosaveWarned = true; toast("This project is too large to autosave (" + fmtSize(bytes) + ") - use Save project… to keep a copy."); }
          return;
        }
        localStorage.setItem(PROJECT_KEY, text);
      } catch (e) {
        // A quota error, localStorage disabled (Safari on file://, a
        // private window), or any other access failure: silently doing
        // nothing here used to mean the designer's edits looked saved but
        // never were (adversarial review, 2026-09-25) - say so, once.
        if (!autosaveWarned) {
          autosaveWarned = true;
          toast("This browser is not keeping an autosaved copy - use Save project… to keep one.");
        }
      }
    }, 800);
  }
  function persist() { scheduleAutosave(); saveUiPrefs(); }

  function fmtSize(bytes) {
    const mb = bytes / (1024 * 1024);
    return mb >= 1 ? mb.toFixed(1) + " MB" : Math.max(1, Math.round(bytes / 1024)) + " KB";
  }
  const pad2 = n => String(n).padStart(2, "0");
  // conductor/server.py's export_show() stamps "exported" with
  // datetime.now().isoformat(timespec="seconds") - local time, no "Z" (the
  // machine's own clock, not UTC). exportBundle() used to call
  // toISOString(), which is UTC (adversarial review, 2026-09-25: it also
  // disagreed with this same function's own download filename, which was
  // already local time) - built from local parts the same way, it now
  // matches both.
  function localIso(d) {
    d = d || new Date();
    return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}T` +
           `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
  }
  function toast(text) {
    const t = $("#toast"); t.textContent = text; t.style.display = "block";
    clearTimeout(toast.timer); toast.timer = setTimeout(() => t.style.display = "none", 6000);
  }

  // Set the moment buildState() throws, cleared the moment it succeeds
  // again; render() checks this FIRST and shows nothing else while it is
  // set (see its own comment). Every mutator (addFiles, updateCue, setShow,
  // ...) already changes `project` before calling rebuild() - if
  // buildState() then throws, the old code below used to leave `state`
  // (and everything on screen, built from it) exactly as it was before that
  // mutation, while `project` already held the new, apparently-poisonous
  // data: a silent mismatch where the screen looks fine but is describing a
  // project that no longer exists, and the very next edit would start from
  // the bad `project` regardless (adversarial review, 2026-09-25).
  // Rendering the error instead - rather than trying to roll `project`
  // back, which would need every one of those mutators to snapshot first -
  // makes the mismatch impossible to miss instead of impossible to see.
  let buildError = null;
  function rebuild() {
    let built;
    try { built = globalThis.SIM.buildState(project); }
    catch (e) {
      console.error("buildState failed", e);
      buildError = (e && e.message) ? e.message : String(e);
      render();
      return;
    }
    buildError = null;
    state = built;
    globalThis.SIM.flicker.clearCaches();
    if (!state.items.some(i => i.item === ui.item)) ui.item = state.items[0]?.item ?? null;
    if (ui.item) {
      const item = state.items.find(i => i.item === ui.item);
      if (!item.designs.some(d => d.name === ui.design)) ui.design = item.designs[0]?.name ?? null;
    }
    // Same pruning as ui.item/ui.design just above (index.html:1864 does the
    // same for its own selection state): a cue deleted, or one whose id
    // changed shape under an Open project/self-test, must not leave EDIT CUE
    // pointed at nothing.
    if (ui.cue !== null && !state.show.cues.some(c => sameId(c.id, ui.cue))) ui.cue = null;
    render();
  }

  // Two garments can share one LOOK on purpose (the show line-up puts a top
  // and its skirt on the same Radxa under one LOOK number - LOOK 26 in the
  // shipped starter data is exactly this: AZ271SC6302 and AZ271SB2303).
  // "LOOK 26" alone is ambiguous between them wherever a garment is named
  // one at a time (a track row, the cue table, SHORTEST INTERVAL PER
  // GARMENT) - adversarial review, 2026-09-25. lookDisplay() appends the
  // item code only when it actually needs to.
  function sharesLook(item) {
    // SIM.looks.lookKey(), not a raw item.look === item.look comparison
    // (adversarial review round 2 - F9): lookGroups() (looks.js) already
    // normalises "22" and "022" to the same group; this used to compare the
    // untouched strings, so two garments written as "26" and "026" would
    // stack together in the dock's looks row (needing no disambiguation by
    // lookGroups' own logic) while still showing "LOOK 26" unqualified on
    // each one's own track row, as if there were only one.
    if (!item || !item.look) return false;
    const key = globalThis.SIM.looks.lookKey(item);
    return state.items.filter(x => globalThis.SIM.looks.lookKey(x) === key).length > 1;
  }
  function lookDisplay(item) {
    if (!item || !item.look) return null;
    // The model number is what a designer knows the two garments of a shared
    // LOOK by ("AZ271SB2303 (Skirt)" / "AZ271SC6302 (Tops)"); the item code
    // is the file-name stem and only the last resort, for a garment nobody
    // has typed a model number for yet (adversarial review F4 - SHORTEST
    // INTERVAL PER ITEM and the track rows used to disambiguate by code).
    return "LOOK " + item.look + (sharesLook(item) ? " · " + (item.model || item.item) : "");
  }
  // ---- display-only rewrite of the model's bus fallback name (plan §1.3/§3.6):
  // the model always calls an unassigned item's bus "(<item>)"; the UI shows
  // "LOOK n" (or "LOOK n · ITEM" when that LOOK is shared, or the bare item
  // name with no LOOK yet) instead, everywhere.
  function unitLabel(key) {
    const m = /^\((.+)\)$/.exec(key);
    if (!m) return key;
    const it = state.items.find(i => i.item.toLowerCase() === m[1].toLowerCase());
    // itemName(), not lookDisplay(): a garment with no LOOK number (the bags)
    // is named by its model number, never by the raw item code.
    return (it && itemName(it)) || m[1];
  }
  function labelize(msg) {
    // Any standalone "(known item)", not only one preceded by " on "
    // (adversarial review round 2 - F6): model.js's pre-burn-cap message
    // (the MAX_CUES_PER_UNIT check) interpolates the same "(<item>)" bus
    // fallback as a bare sentence-opening prefix - "(AZ271SX0002) carries
    // 21 pictures…" - which the narrower " on (...)" pattern never matched,
    // leaking the raw item code. Matching on "is this parenthetical exactly
    // a known item's name" (not just "looks like one") means this can never
    // eat an unrelated parenthetical aside.
    return String(msg).replace(/\(([^()]+)\)/g, (m, name) => {
      const it = state.items.find(i => i.item.toLowerCase() === name.toLowerCase());
      return it ? itemName(it) : m;
    });
  }
  // model.js's pre-burn cue-limit message, hand-rewritten rather than left
  // to deJargon()'s word-for-word swap (adversarial review round 2 - F6):
  // "(AZ271SX0002) carries 21 pictures but a board holds 18 show pictures
  // (slot 0 is the white standby, slot 19 the manual one-shot) - merge or
  // remove cues" would still name an internal detail (which slot number is
  // reserved for what) no designer decision hinges on, even after
  // board->segment. Matched on the ORIGINAL wording (before deJargon would
  // touch "board"), so this must run first - see clean() below.
  function rewriteCueLimitMessage(msg) {
    const m = /^\(([^()]+)\) carries (\d+) pictures but a board holds (\d+) show pictures\b/.exec(String(msg));
    if (!m) return null;
    const [, name, carries, holds] = m;
    const it = state.items.find(i => i.item.toLowerCase() === name.toLowerCase());
    const label = (it && itemName(it)) || name;
    return `${label} has ${carries} pictures but an item can hold ${holds} in one show - merge or remove cues`;
  }
  // look.py's malformed-row message, hand-rendered rather than left to
  // deJargon's word-for-word swap (adversarial review round 2, third pass -
  // N2): the message is "<file>:<line>: row/col/board_no/socket must be
  // whole numbers: {'side': …, 'socket': 'x', …}" - a LITERAL column-name
  // list plus a Python dict repr using those same names as its own quoted
  // keys. deJargon()'s \bsocket\b -> "position" swap does not know "socket"
  // here is a column name, not prose, so it renamed the column (and the
  // dict's key, though not its value) while "board_no" merely survived by
  // luck (no word boundary inside it for \bboard\b to match) - either way
  // the result no longer named a real CSV column at all. This drops the raw
  // dict entirely along with the ambiguity: a designer needs the line
  // number and which columns to check, not a Python repr.
  function rewriteMalformedRowMessage(msg) {
    const m = /^(.+):(\d+): row\/col\/board_no\/socket must be whole numbers\b/.exec(String(msg));
    if (!m) return null;
    const [, , lineNo] = m;
    return `line ${lineNo}: row, col, board_no and socket must be whole numbers`;
  }
  // Adversarial review (2026-09-25): labelize() only ever rewrote the one
  // "on (<item>)" bus fallback - real problem/warning strings ported
  // verbatim from conductor/look.py and conductor/timeline.py still say
  // things like "(board 12 socket 7)", "23 boards, but one unit drives at
  // most 60", "writing its 12 boards needs 3.2 s", "the unit may still be
  // rejoining…". Those model strings must stay byte-identical to Python (the
  // goldens compare them) - so this is a DISPLAY-ONLY word swap, applied
  // after labelize() at every place a model string reaches the screen
  // (problems, warnings, toasts - see clean() and toast() below), never
  // touching SIM.* itself. Plain word substitution, not a rewording: the
  // numbers and sentence structure stay exactly as Python phrased them.
  function deJargon(msg) {
    return String(msg)
      .replace(/\bunits\b/gi, "items").replace(/\bunit\b/gi, "item")
      .replace(/\bboards\b/gi, "segments").replace(/\bboard\b/gi, "segment")
      .replace(/\bsockets\b/gi, "positions").replace(/\bsocket\b/gi, "position")
      .replace(/\bbus(es)?\b/gi, "shared line")
      .replace(/\bdip\b/gi, "").replace(/\bradxa\b/gi, "controller")
      .replace(/[ \t]{2,}/g, " ").trim();
  }
  const clean = msg => rewriteCueLimitMessage(msg) ?? rewriteMalformedRowMessage(msg) ?? deJargon(labelize(msg));
  // The "natural" sequence's real label, ported verbatim from
  // conductor/sequence.py's LABELS ("Socket order (P01 to P60)"), is exactly
  // the kind of string plan_designer_sim.md §3 bans from this UI - unlike
  // the problem/warning text above, deJargon()'s word-for-word swap would
  // read badly here ("position order (P01 to P60)"), so this one sequence
  // gets an outright display override instead; the other five labels
  // ("Top to bottom" etc.) already carry no banned vocabulary and pass
  // through untouched. SIM.sequence.LABELS itself is never modified - the
  // override lives only in how this page prints it.
  function seqLabel(s) { return s.id === "natural" ? "Default (as wired)" : s.label; }
  // A tiny, deterministic self-check a headless browser can run without any
  // UI (tests/test_designer_build.py::test_banned_vocabulary_never_reaches_ui
  // drives this): representative dirty strings copied verbatim from the
  // Python f-string templates that generate them (conductor/look.py,
  // conductor/timeline.py), asserting deJargon()/seqLabel() scrub every
  // banned word while leaving the numbers intact.
  function displayCheck() {
    const dirty = [
      "(board 12 socket 7)",
      "17: 4 boards, but one unit drives at most 60",
      "only 2.0 s after the previous send on (AZ271SD1301); writing its 12 boards needs 3.2 s (12 × 0.20 s + 1.0 s)",
      "only 1.0 s after the previous send on (AZ271SD1301); the unit may still be rejoining and needs at least 9.0 s",
      "9 boards on one unit, but a bus holds at most 8",
      "board 7 is in both front row 3 and back row 9",
    ];
    // model.js's pre-burn cap message (adversarial review round 2 - F6):
    // run through clean() (labelize -> rewriteCueLimitMessage -> deJargon
    // in that priority, see clean()'s own definition), not deJargon() alone
    // - the whole point is that the bare "(item)" prefix and the slot-
    // number detail need the hand-written rewrite, not a word swap.
    const cleanDirty = ["(AZ271SD1301) carries 21 pictures but a board holds 18 show pictures "
      + "(slot 0 is the white standby, slot 19 the manual one-shot) - merge or remove cues"];
    // look.py's malformed-row message (adversarial review round 2, third
    // pass - N2) checked separately, for correctness rather than for the
    // absence of banned words: "socket" and "board_no" are the CSV format's
    // OWN column names (side,row,col,board_no,socket,label,shift - the
    // literal header row of a *_map.csv), not hardware jargon about this
    // show's equipment, and a designer fixing a bad row needs to see the
    // real column name their spreadsheet uses, not a renamed one that no
    // longer matches what is actually in the file.
    const malformedRowDirty = [["AZ271SD1301_map.csv:5: row/col/board_no/socket must be whole numbers: "
      + "{'side': 'front', 'row': 'x', 'col': '3', 'board_no': '12', 'socket': '5', 'label': ''}",
      "line 5: row, col, board_no and socket must be whole numbers"]];
    const banned = /\b(unit|units|radxa|bus|buses|board|boards|dip|socket|sockets)\b/i;
    const failures = [];
    for (const msg of dirty) {
      const got = deJargon(msg);
      if (banned.test(got)) failures.push(`deJargon(${JSON.stringify(msg)}) -> ${JSON.stringify(got)} still has banned vocabulary`);
    }
    for (const msg of cleanDirty) {
      const got = clean(msg);
      if (banned.test(got)) failures.push(`clean(${JSON.stringify(msg)}) -> ${JSON.stringify(got)} still has banned vocabulary`);
      if (/slot 0|slot 19|one-shot/.test(got)) failures.push(`clean(${JSON.stringify(msg)}) -> ${JSON.stringify(got)} still leaks the internal slot numbering`);
    }
    for (const [msg, expect] of malformedRowDirty) {
      const got = clean(msg);
      if (got !== expect) failures.push(`clean(${JSON.stringify(msg)}) -> ${JSON.stringify(got)}, expected ${JSON.stringify(expect)}`);
    }
    const seqs = (globalThis.SIM.sequence && globalThis.SIM.sequence.LABELS) || {};
    Object.keys(seqs).forEach(id => {
      const got = seqLabel({ id, label: seqs[id] });
      if (banned.test(got)) failures.push(`seqLabel(${id}) -> ${JSON.stringify(got)} still has banned vocabulary`);
    });
    const result = { ok: failures.length === 0, total: dirty.length + cleanDirty.length + malformedRowDirty.length + Object.keys(seqs).length, failed: failures.length, failures };
    try {
      let pre = document.getElementById("displaycheck-out");
      if (!pre) { pre = document.createElement("pre"); pre.id = "displaycheck-out"; document.body.appendChild(pre); }
      pre.setAttribute("data-ok", String(result.ok));
      pre.setAttribute("data-total", String(result.total));
      pre.setAttribute("data-failed", String(result.failed));
      pre.textContent = JSON.stringify(result, null, 1);
    } catch {}
    return result;
  }
  // Exposed for the Help tab's "Run self-test" area and for tests; the
  // #displaycheck hash's own auto-run lives in boot(), AFTER rebuild() -
  // displayCheck() now looks items up in `state` (F6's rewrite needs to
  // find the item behind a bare "(item)" prefix), which does not exist yet
  // this early (this listener would otherwise fire before boot()'s own,
  // registered further down the file, ever runs).
  globalThis.__displaycheck = displayCheck;
  // The model number, not the raw item code, is the fallback for a garment
  // with no LOOK number (the three bags in the shipped starter data are
  // exactly this - they are not part of the numbered line-up, so production
  // names them "AZ271SG1035 (Bag 01)"). The item code is the file-name stem
  // and the last resort: a designer has no reason to read it.
  const itemName = i => !i ? "" : lookDisplay(i) || i.model || i.item;
  // The long form: "LOOK 23 · AZ271SD1305". Not a blind join of the two
  // (adversarial review F4, second look): lookDisplay() appends the model
  // number itself when a LOOK is shared, so joining unconditionally read
  // "LOOK 26 · AZ271SB2303 (Skirt) · AZ271SB2303 (Skirt)" in the cue table,
  // the EDIT CUE heading and every track row's tooltip.
  const itemFull = i => {
    if (!i) return "";
    const name = lookDisplay(i);
    if (!name) return i.model || i.item;
    return i.model && !name.includes(i.model) ? name + " · " + i.model : name;
  };
  // The second line under a track's name: the model number, unless the name
  // line already carries it - either because it IS the model number (a
  // garment with no LOOK) or because lookDisplay() appended it to
  // disambiguate a shared LOOK, which used to print
  // "LOOK 26 · AZ271SB2303 (Skirt)" over "AZ271SB2303 (Skirt)"
  // (adversarial review F4).
  const itemSub = i => (i && i.model && !itemName(i).includes(i.model)) ? i.model : "";
  const designLabel = d => d.label || d.name;
  const designState = d => !d.problems.length ? "ok" : !d.partial_problems.length ? "partial" : "bad";

  // ==================================================================
  // ITEMS sidebar (plan §3.3)
  // ==================================================================
  function problemsOf(item) { return item.problems.length + item.designs.reduce((n, d) => n + d.partial_problems.length, 0); }
  function itemCard(item) {
    const bad = problemsOf(item);
    const dot = !item.map ? "none" : bad ? "err" : "ok";
    return `<div class="item ${item.item === ui.item ? "sel" : ""}" data-item="${esc(item.item)}">
      <div class="name"><span class="dot ${dot}"></span><em>LOOK</em>
        <input class="lab look" data-label="look" value="${esc(item.look)}" placeholder="–" maxlength="12" title="LOOK number (sets the order items appear in)">
        <input class="lab model" data-label="model" value="${esc(item.model)}" placeholder="model no." maxlength="60" title="Model number">
      </div>
      <div class="meta">${item.map ? `${item.map.scales.length} scales · ${item.designs.length} design${item.designs.length === 1 ? "" : "s"}` : "no map CSV yet"}
        ${bad ? ` · <span style="color:var(--err)">${bad} problem${bad === 1 ? "" : "s"}</span>` : ""}</div>
    </div>`;
  }
  // plan_designer_sim.md §3.3: "the LOOK number is the only ordering control
  // (list and looks row both order by LOOK number)". SIM.buildState (P's
  // model) sorts state.items by unit-then-name, matching the operator page's
  // own state() - since the sim's unit is always null, that is really just
  // alphabetical by item name. SIM.looks.lookGroups() already does its own
  // LOOK-first sort for the looks row (see looks.js); this is the same
  // comparator applied to the raw item list, for the ITEMS sidebar and the
  // Timeline's tracks/cue table.
  function orderByLook(items) {
    return items.slice().sort((a, b) => {
      const an = a.look ? Number(a.look) : NaN, bn = b.look ? Number(b.look) : NaN;
      const aNum = Number.isFinite(an), bNum = Number.isFinite(bn);
      if (aNum && bNum && an !== bn) return an - bn;
      if (aNum !== bNum) return aNum ? -1 : 1;
      return a.item < b.item ? -1 : a.item > b.item ? 1 : 0;
    });
  }
  function renderSidebar() {
    const ordered = orderByLook(state.items);
    $("#items").innerHTML = ordered.map(itemCard).join("") || `<div class="meta">Drop CSV files to begin.</div>`;
    $("#orphans").innerHTML = state.orphans.length
      ? `<h2>DESIGNS WAITING FOR A MAP</h2>` + state.orphans.map(o =>
          `<div class="files"><span>${esc(o.name)} <button data-del="${esc(o.name)}">×</button></span></div>`).join("")
      : "";
    $("#ws").textContent = state.items.length + " item" + (state.items.length === 1 ? "" : "s")
      + (state.show.cues.length ? " · " + state.show.cues.length + " cue" + (state.show.cues.length === 1 ? "" : "s") : "");
  }

  // ==================================================================
  // mm.ss fields (plan §3.1)
  // ==================================================================
  function mmssField(id, sec, opts) {
    opts = opts || {};
    const text = globalThis.SIM.mmss.format(sec);
    return `<span class="mmss" data-mmss-field="${id}">
      <input type="text" id="${id}" value="${esc(text)}" ${opts.disabled ? "disabled" : ""} inputmode="decimal">
      <span class="mmss-badge" title="Minutes.seconds - 3.05 = 3 min 05 s, 3.5 = 3 min 05 s too (one digit = that many seconds)">mm.ss</span>
      <span class="mmss-echo" id="${id}-echo">${esc(globalThis.SIM.mmss.human(sec))}</span></span>`;
  }
  // currentValueFn (optional): the seconds to revert to on invalid input,
  // computed FRESH at the moment of the revert - not input.defaultValue
  // (adversarial review round 2 - F10: the go-to box's defaultValue is
  // whatever the playhead happened to read when the dock last re-rendered,
  // which - since it does not re-render every animation frame - is stale
  // the instant the transport moves on; every other mm.ss field's
  // defaultValue IS the current value, because those only ever change via a
  // fresh render(), so they keep the simpler default).
  function wireMmss(id, onChange, currentValueFn) {
    const input = $("#" + id); if (!input) return;
    const echo = $("#" + id + "-echo");
    const paint = () => {
      const sec = globalThis.SIM.mmss.parse(input.value);
      input.classList.toggle("bad", sec === null);
      if (echo) { echo.classList.toggle("bad", sec === null); echo.textContent = sec === null ? "read as mm.ss, e.g. 3.05 = 3 min 05 s" : globalThis.SIM.mmss.human(sec); }
      return sec;
    };
    input.addEventListener("input", paint);
    input.addEventListener("change", () => {
      const sec = paint();
      if (sec !== null) onChange(sec);
      else {
        input.value = currentValueFn ? globalThis.SIM.mmss.format(currentValueFn()) : input.defaultValue;
        paint();   // revert AND re-paint, so the red/bad state clears with it
      }
    });
    paint();
  }

  // ==================================================================
  // Designs tab (plan §3.4)
  // ==================================================================
  const SEQ_OPTIONS = () => state.sequences;
  function transitionControl(name, tr) {
    return `<select data-tr-seq="${esc(name)}">${SEQ_OPTIONS().map(s =>
        `<option value="${esc(s.id)}" ${s.id === tr.sequence ? "selected" : ""}>${esc(seqLabel(s))}</option>`).join("")}</select>
      <span ${tr.sequence === "natural" ? 'style="display:none"' : ""}><input type="text" data-tr-span="${esc(name)}" size="4" value="${tr.span_s}"> s</span>`;
  }
  function renderDesigns() {
    const root = $("#content");
    const item = state.items.find(i => i.item === ui.item);
    if (!item) { root.innerHTML = `<div class="empty">Drop the item's map and design CSV files here (or use Add CSV above) to begin.</div>`; return; }
    if (!item.map || !item.map.scales.length) {
      root.innerHTML = `<div class="card"><h2>${esc(itemFull(item))}</h2>
        <ul class="problems">${item.problems.map(p => `<li>${esc(clean(p))}</li>`).join("") || "<li>no map CSV</li>"}</ul></div>`;
      return;
    }
    const design = item.designs.find(d => d.name === ui.design) || null;
    const usage = {};
    if (design) for (const code of Object.values(design.colors)) usage[code] = (usage[code] || 0) + 1;
    const kind = design ? designState(design) : "ok";
    const problems = [...item.problems, ...(design && kind === "bad" ? design.problems : [])];
    const selTr = design ? (design.transition || { sequence: "natural", span_s: 0 }) : null;
    const used = state.show.cues.filter(c => c.item === item.item);

    root.innerHTML = `
      <div class="toolbar">
        <div class="group"><span>Design</span>
          <select id="design-pick">${item.designs.map(d =>
            `<option value="${esc(d.name)}" ${d.name === ui.design ? "selected" : ""}>${esc(designLabel(d))}${designState(d) === "bad" ? " ⚠" : ""}</option>`).join("")
            || "<option>(no design CSV yet)</option>"}</select></div>
        <div class="group"><span>Seen from</span>
          <button data-view="outside" class="${ui.view === "outside" ? "on" : ""}">Outside (audience)</button>
          <button data-view="inside" class="${ui.view === "inside" ? "on" : ""}">Inside (as in the CSV)</button></div>
        ${design ? `<div class="group" data-design="${esc(design.name)}"><span>Transition</span>${transitionControl(design.name, selTr)}</div>` : ""}
      </div>
      <div id="stage">${globalThis.SIM.render.renderGarment(item, { cell: 22, mode: "design", labels: true, view: ui.view,
        colors: design?.colors || {}, undecided: design?.undecided, shifts: design?.shifts, palette: state.palette })}</div>
      <div id="info" class="meta">Hover a scale to see its colour.</div>
      <div class="cols">
        <div>
          <div class="card"><h2>CHECK</h2>
            ${problems.length
              ? `<ul class="problems">${problems.slice(0, 60).map(p => `<li>${esc(clean(p))}</li>`).join("")}</ul>`
              : `<div class="okline">✓ No problems — ${item.map.scales.length} scales${design ? ` / design ${Object.keys(design.colors).length} coloured` : ""}</div>`}
            ${kind === "partial" ? `<div class="warn">A partial design: some scales are left "-" or uncoloured and keep whatever they already show. Fine as a partial cue on the Timeline.</div>` : ""}
            ${(item.map.warnings || []).map(w => `<div class="warn">⚠ ${esc(clean(w))}</div>`).join("")}
          </div>
          <div class="card"><h2>DESIGNS OF THIS ITEM (${item.designs.length})</h2>
            <div class="dsg-list">${item.designs.map(d => {
              const at = used.filter(c => c.design === d.name).map(c => globalThis.SIM.mmss.format(c.at));
              const tr = d.transition || { sequence: "natural", span_s: 0 };
              const usageTxt = at.length ? "timeline " + at.slice(0, 3).join(", ") + (at.length > 3 ? ` +${at.length - 3} more` : "") : "not on the timeline";
              return `<div class="dsg pick ${d.name === ui.design ? "hl" : ""}" data-design="${esc(d.name)}">
                <div class="dsg-main"><div class="dsg-top"><b>${esc(designLabel(d))}</b>
                  ${designState(d) === "ok" ? '<span class="okline">OK</span>' : designState(d) === "partial" ? '<span class="warn">partial only</span>' : `<span style="color:var(--err)">${d.partial_problems.length} problems</span>`}
                  <span class="use">${esc(usageTxt)}</span></div>
                  <div class="dsg-file" title="${esc(d.name)}">${esc(d.name)}</div></div>
                <div class="dsg-tr">${transitionControl(d.name, tr)}<button data-del="${esc(d.name)}" title="Remove from the workspace">×</button></div>
              </div>`; }).join("") || `<div class="meta">Add design CSV files (*_color_NAME_grid.csv)</div>`}</div>
            <div class="meta" style="margin-top:8px">
              <label class="filebtn" tabindex="0" role="button" data-pick-item="${esc(item.item)}">Add CSV<input type="file" accept=".csv" multiple></label>
              <span style="margin-left:6px">whatever name the files arrive under, they are saved as ${esc(item.item)}_… and belong to this item alone</span></div>
          </div>
          ${design ? `<div class="card"><h2>COLOURS USED</h2><table><tbody>
            ${Object.keys(usage).sort((a, b) => a - b).map(c => `<tr><td><span class="sw" style="background:rgb(${state.palette[c].rgb.join(",")})"></span>${esc(state.palette[c].name)}</td><td>${usage[c]}</td></tr>`).join("")}
            ${design.undecided.length ? `<tr><td>- undecided</td><td>${design.undecided.length}</td></tr>` : ""}
            </tbody></table></div>` : ""}
          <div class="card"><h2>MAP FILE</h2><div class="files"><span>${esc(item.map.name)} <button data-del="${esc(item.map.name)}">×</button></span></div></div>
        </div>
      </div>`;
  }

  // ==================================================================
  // Timeline tab (plan §3.5)
  // ==================================================================
  function trackItems() { return orderByLook(state.items.filter(i => i.map && i.map.scales.length)); }
  function cuesOf(item) { return state.show.cues.filter(c => c.item === item.item).sort((a, b) => a.sent - b.sent); }
  const ctx = () => ({ cuesOf, palette: state.palette, refreshS: state.show.refresh_s });

  function nextUnusedDesign(item) {
    const used = new Set(cuesOf(item).map(c => c.design));
    return item.designs.find(d => !used.has(d.name)) || item.designs[0] || null;
  }
  function clockShort(sec) { return globalThis.SIM.mmss.format(sec); }

  function renderCueEditor(cue) {
    const card = $("#cue-editor-body");
    if (!cue) { card.innerHTML = `<div class="meta">Select a cue, or click a track to add one.</div>`; return; }
    const item = state.items.find(i => i.item === cue.item);
    const isPreset = cue.at <= 0;
    const design = item?.designs.find(d => d.name === cue.design);
    const designTr = design?.transition || { sequence: "natural", span_s: 0 };
    const transitionMode = cue.transition || "design";
    const sweep = cue.sweep || { sequence: "natural", span_s: 0 };
    const refr = cue.refresh_s != null ? { value: cue.refresh_s, source: "cue" } : { value: state.show.refresh_s, source: "show" };
    const cuesOnItem = item ? cuesOf(item) : [];
    const idx = cuesOnItem.findIndex(c => sameId(c.id, cue.id));
    const isLast = idx === cuesOnItem.length - 1;
    card.innerHTML = `<div class="cue-card">
      <div class="cue-head"><b>${esc(itemFull(item) || cue.item)}</b>
        <span><button id="cue-del" class="danger">Delete</button></span></div>
      <div class="cue-row"><div class="lbl">Start</div><div class="ctl">${mmssField("cue-start", cue.at, { disabled: isPreset })}</div>
        <div class="why">${isPreset ? "Preset: shown before the show starts" : "Refresh begins here"}</div></div>
      <div class="cue-row"><div class="lbl">Refresh</div>
        <div class="ctl"><label class="rad"><input type="radio" name="cue-refresh-mode" id="cue-refresh-show" ${refr.source === "show" ? "checked" : ""}> show default (${state.show.refresh_s.toFixed(1)} s)</label>
          <label class="rad"><input type="radio" name="cue-refresh-mode" id="cue-refresh-cue" ${refr.source === "cue" ? "checked" : ""}> this cue</label>
          <input type="text" id="cue-refresh" value="${refr.value.toFixed(1)}" ${refr.source === "cue" ? "" : "disabled"}> s</div>
        <div class="why">How long this item's e-paper takes to redraw</div></div>
      <div class="cue-row"><div class="lbl">Transition</div>
        <div class="ctl" style="flex-direction:column;align-items:flex-start;gap:6px">
          <label class="rad"><input type="radio" name="cue-transition" id="cue-transition-design" ${transitionMode === "design" ? "checked" : ""}> this design (all cues using it)
            ${design ? `<span style="display:inline-flex;gap:8px;align-items:center;margin-left:4px">${transitionControl(cue.design, designTr)}</span>` : ""}</label>
          <label class="rad"><input type="radio" name="cue-transition" id="cue-transition-custom" ${transitionMode === "custom" ? "checked" : ""}> this cue only
            <select id="cue-seq" ${transitionMode !== "custom" ? "disabled" : ""}>${SEQ_OPTIONS().map(s =>
              `<option value="${s.id}" ${s.id === (cue.sequence || "natural") ? "selected" : ""}>${esc(seqLabel(s))}</option>`).join("")}</select>
            <span ${(cue.sequence || "natural") === "natural" ? 'style="display:none"' : ""}><input type="text" id="cue-span" value="${cue.span_s ?? 0}" size="4" ${transitionMode !== "custom" ? "disabled" : ""}> s</span></label>
        </div><div class="why"></div></div>
      <div class="cue-computed">Refresh completes at ${clockShort(cue.complete)} (Start + ${refr.value.toFixed(1)} s refresh${sweep.span_s > 0 ? ` + ${sweep.span_s.toFixed(1)} s sweep` : ""})</div>
      <div class="cue-row"><div class="lbl">End</div><div class="ctl">${mmssField("cue-end", cue.end, { disabled: isLast })}</div>
        <div class="why">${isLast ? `Shown until the end of the show (${clockShort(state.show.duration)})` : "Shown until the next cue of this item starts"}</div></div>
      <div class="cue-design"><label>Design <select id="cue-design">${(item?.designs || []).map(d =>
        `<option value="${esc(d.name)}" ${d.name === cue.design ? "selected" : ""}>${esc(designLabel(d))}</option>`).join("")}</select></label>
        <label><input type="checkbox" id="cue-partial" ${cue.partial ? "checked" : ""}> partial</label></div>
      ${(cue.problems || []).length ? `<ul class="problems">${cue.problems.map(p => `<li>${esc(clean(p))}</li>`).join("")}</ul>` : ""}
    </div>`;
    wireMmss("cue-start", sec => globalThis.SIM.app.updateCue(cue.id, { at: sec }));
    wireMmss("cue-end", sec => globalThis.SIM.app.updateCue(cue.id, { end: sec }));
    $("#cue-del").onclick = () => globalThis.SIM.app.deleteCue(cue.id);
    $("#cue-refresh-show").onchange = () => globalThis.SIM.app.updateCue(cue.id, { refresh_s: null });
    $("#cue-refresh-cue").onchange = () => globalThis.SIM.app.updateCue(cue.id, { refresh_s: Number($("#cue-refresh").value) || state.show.refresh_s });
    $("#cue-refresh").onchange = e => { if (refr.source === "cue") globalThis.SIM.app.updateCue(cue.id, { refresh_s: Number(e.target.value) || 0 }); };
    $("#cue-transition-design").onchange = () => globalThis.SIM.app.updateCue(cue.id, { transition: "design" });
    $("#cue-transition-custom").onchange = () => globalThis.SIM.app.updateCue(cue.id, { transition: "custom" });
    $("#cue-seq").onchange = e => globalThis.SIM.app.updateCue(cue.id, { sequence: e.target.value });
    $("#cue-span").onchange = e => globalThis.SIM.app.updateCue(cue.id, { span_s: Number(e.target.value) || 0 });
    $("#cue-design").onchange = e => globalThis.SIM.app.updateCue(cue.id, { design: e.target.value });
    $("#cue-partial").onchange = e => globalThis.SIM.app.updateCue(cue.id, { partial: e.target.checked });
    card.querySelectorAll("[data-tr-seq]").forEach(sel => sel.onchange = e => {
      const name = e.target.dataset.trSeq, span = card.querySelector(`[data-tr-span="${CSS.escape(name)}"]`);
      globalThis.SIM.app.setDesignTransition(name, e.target.value, Number(span?.value) || 0);
    });
    card.querySelectorAll("[data-tr-span]").forEach(inp => inp.onchange = e => {
      const name = e.target.dataset.trSpan, sel = card.querySelector(`[data-tr-seq="${CSS.escape(name)}"]`);
      globalThis.SIM.app.setDesignTransition(name, sel.value, Number(e.target.value) || 0);
    });
  }

  function renderTracks() {
    const D = state.show.duration;
    const items = trackItems();
    const pct = t => (100 * Math.max(0, Math.min(D, t)) / D).toFixed(3) + "%";
    const rows = items.map(item => {
      const cues = cuesOf(item);
      const bands = cues.map(cue => {
        const bandW = pct(Math.max(0, cue.complete - cue.at));
        const holdX = pct(cue.complete), holdW = pct(Math.max(0, cue.end - cue.complete));
        const bad = (cue.problems || []).length > 0;
        return `<div class="cue-band ${sameId(cue.id, ui.cue) ? "sel" : ""}" data-cue="${esc(cue.id)}" style="left:${pct(cue.at)};width:${bandW}"></div>
          <div class="cue-hold ${bad ? "bad" : ""} ${sameId(cue.id, ui.cue) ? "sel" : ""}" data-cue="${esc(cue.id)}" data-drag="${cue.at <= 0 ? "0" : "1"}"
            style="left:${holdX};width:${holdW}" title="${esc(designLabel(item.designs.find(d => d.name === cue.design) || { name: cue.design }))}">${cue.at <= 0 ? "PRESET · " : ""}${esc(designLabel(item.designs.find(d => d.name === cue.design) || { name: cue.design }))}</div>`;
      }).join("");
      return `<div class="tl-row"><div class="tl-name" title="${esc(itemFull(item))}">${esc(itemName(item))}<small>${esc(itemSub(item))}</small></div>
        <div class="tl-track" data-track="${esc(item.item)}">${bands}</div></div>`;
    }).join("");
    // Same geometry as index.html: the ruler sits in a track row behind an
    // empty 150 px name column, and the playhead lives in a wrapper that
    // starts where the bands start - otherwise the ticks and the red line
    // are 150 px (about 1.5 min on a 10 min show) left of every cue.
    return `<div class="tl-wrap">
      <div class="tl-row" style="border:0;min-height:22px"><div></div><div class="tl-ruler" id="ruler">${rulerTicks(D)}</div></div>
      ${rows || `<div class="empty">No items yet.</div>`}
      <div style="position:absolute;left:150px;right:0;top:0;bottom:0;pointer-events:none"><div id="playhead"><div id="ph-head" title="Drag to seek, or press and drag anywhere on the ruler"><svg width="14" height="10"><polygon points="0,0 14,0 7,10" fill="var(--err)"/></svg><span id="ph-time"></span></div></div></div></div>`;
  }
  function rulerTicks(D) {
    // The existing tiers, but never finer than D/200 (adversarial review
    // round 2 - F2): duration is clamped to 99:59 everywhere this page
    // writes it now, but state.show.duration ultimately comes straight from
    // SIM.buildState(project) with no clamp of its own - a defensive floor
    // here means a pathological D (reached some other way) draws at most
    // ~200 ticks instead of, at the review's own example, ~83,000.
    const step = Math.max(D > 900 ? 60 : D > 300 ? 30 : 10, D / 200);
    let html = "";
    for (let t = 0; t <= D; t += step) html += `<i style="left:${(100 * t / D).toFixed(3)}%"></i><span style="left:${(100 * t / D).toFixed(3)}%">${clockShort(t)}</span>`;
    return html;
  }
  // index.html:1051's reference: the show's own warnings (overlap/sweep/
  // preset notes that timeline.validate() emits) were never surfaced on
  // this page at all (adversarial review, 2026-09-25) - shown once, above
  // the cue table, same treatment (labelize+deJargon) as every other
  // model-generated string.
  function warningsBlock() {
    const warnings = state.show.warnings || [];
    if (!warnings.length) return "";
    return `<div class="card">${warnings.map(w => `<div class="warn">⚠ ${esc(clean(w))}</div>`).join("")}</div>`;
  }
  function minIntervalTable() {
    const rows = Object.entries(state.show.min_interval || {}).filter(([, v]) => v !== null && v !== undefined);
    if (!rows.length) return "";
    return `<div class="card"><h2>SHORTEST INTERVAL PER ITEM</h2><table><tbody>
      ${rows.map(([k, v]) => `<tr><td>${esc(unitLabel(k))}</td><td>${v.toFixed(1)} s</td></tr>`).join("")}
      </tbody></table></div>`;
  }
  function cueTable() {
    const rows = state.show.cues.slice().sort((a, b) => a.at - b.at);
    if (!rows.length) return `<div class="meta">No cues yet - click a track above to add one.</div>`;
    return `<table><thead><tr><th>START</th><th>COMPLETE</th><th>END</th><th>ITEM</th><th>DESIGN</th><th>TRANSITION</th><th>STATUS</th></tr></thead><tbody>
      ${rows.map(c => {
        const item = state.items.find(i => i.item === c.item);
        const trSeq = SEQ_OPTIONS().find(s => s.id === (c.sequence || "natural"));
        const tr = c.transition === "custom" ? (trSeq ? seqLabel(trSeq) : c.sequence) : "design default";
        const status = (c.problems || []).length ? `<span style="color:var(--err)">${c.problems.length} problem${c.problems.length === 1 ? "" : "s"}</span>` : '<span class="okline">OK</span>';
        return `<tr class="pick ${sameId(c.id, ui.cue) ? "hl" : ""}" data-cue="${esc(c.id)}">
          <td>${clockShort(c.at)}</td><td>${clockShort(c.complete)}</td><td>${clockShort(c.end)}</td>
          <td>${esc(itemFull(item) || c.item)}</td><td>${esc(designLabel(item?.designs.find(d => d.name === c.design) || { name: c.design }))}${c.partial ? " (partial)" : ""}</td>
          <td>${esc(tr)}</td><td>${status}</td></tr>`;
      }).join("")}</tbody></table>`;
  }
  function musicControl() {
    const m = state.music || { name: null, url: null };
    // Which source is playing is a fact about this session, not about the
    // project, so it is read off the module rather than out of state.music
    // (see boot()'s buildState wrapper for why that object stays exactly
    // {name, url}).
    const embedded = builtInMusicName();     // null unless it really plays
    const onBuiltIn = !!embedded && !musicUrl;
    const pick = `<label class="filebtn" tabindex="0" role="button">${embedded ? "Pick another file…" : "Pick file…"}<input id="music-pick" type="file" accept="audio/*"></label>`;
    if (onBuiltIn) {
      // The built-in track is what will play. Either it is also what the
      // project names (the ordinary case - say so plainly and offer the
      // per-session override), or the project names something else, in
      // which case BOTH facts matter: the old yellow "re-pick the file"
      // bar is still the truth about the project's own music, and the
      // sentence after it is the truth about what pressing Play will
      // actually produce. Saying only one of the two is how a designer
      // ends up timing cues against the wrong piece of audio.
      const differs = m.name && m.name !== embedded;
      const label = differs
        ? `${esc(m.name)} <span class="warn">(re-pick the file: it is not kept between reloads)</span> · Playing the built-in track ${esc(embedded)} instead`
        : `${esc(embedded)} · built in`;
      return `<div class="group"><span>Music</span><span class="meta">${label}</span>${pick}</div>`;
    }
    if (!m.name) return `<div class="group"><span>Music</span>${pick}</div>`;
    const missing = !m.url;
    // "Back to the built-in track", not "Remove", whenever there is one to
    // go back to: Remove would be a lie (the page would keep playing) and
    // the way back has to be somewhere.
    const undo = embedded
      ? `<button id="music-builtin">Back to the built-in track</button>`
      : `<button id="music-clear">Remove</button>`;
    return `<div class="group"><span>Music</span><span class="meta">${esc(m.name)}${missing ? ' <span class="warn">(re-pick the file: it is not kept between reloads)</span>' : ""}</span>
      ${pick}
      ${undo}</div>`;
  }
  function renderTimelineTab() {
    ensureTransport();
    const root = $("#content");
    const items = trackItems();
    root.innerHTML = `<div class="toolbar">
        <div class="group"><span>Show length</span>${mmssField("show-duration", state.show.duration)}</div>
        <div class="group"><span>Default refresh time</span><input type="text" id="show-refresh" size="4" value="${state.show.refresh_s.toFixed(1)}"> s</div>
        <div class="group"><button id="save-project">Save project…</button><label class="filebtn" tabindex="0" role="button">Open project…<input id="open-project" type="file" accept=".json"></label></div>
        ${musicControl()}
      </div>
      ${items.length ? `<div id="tl-editing">
        <div class="tl-left">
          <div class="card">${renderTracks()}</div>
          ${warningsBlock()}
          <div class="card"><h2>CUES</h2>${cueTable()}</div>
          ${minIntervalTable()}
        </div>
        <div class="card" id="tl-editor-card"><h2>EDIT CUE</h2><div id="cue-editor-body"></div></div>
      </div>` : `<div class="empty">Add the map and design CSV files on the Designs tab first.</div>`}`;
    if (items.length) {
      // Clamped to 1 s..99:59 (adversarial review, 2026-09-25), and 0
      // itself is a revert, not a clamp-up-to-1: "0" is what an accidental
      // double-backspace leaves behind, and silently turning that into a
      // 1-second show is a worse surprise than just putting back what was
      // there. wireMmss() cannot tell the difference on its own - 0 parses
      // as a perfectly valid mm.ss - so the revert (and its echo/red-state
      // repaint) happens here, not inside wireMmss's generic bad-input path.
      wireMmss("show-duration", sec => {
        if (sec <= 0) {
          toast("Show length must be at least 1 s - kept " + globalThis.SIM.mmss.format(state.show.duration) + ".");
          const input = $("#show-duration");
          if (input) { input.value = globalThis.SIM.mmss.format(state.show.duration); input.dispatchEvent(new Event("input")); }
          return;
        }
        globalThis.SIM.app.setShow({ duration: Math.min(MAX_SHOW_DURATION_S, sec) });
      });
      $("#show-refresh").onchange = e => { const v = Number(e.target.value); if (v >= 1 && v <= 60) globalThis.SIM.app.setShow({ refresh_s: v }); else e.target.value = state.show.refresh_s.toFixed(1); };
      renderCueEditor(state.show.cues.find(c => sameId(c.id, ui.cue)) || null);
      wireTrackEvents();
    }
    $("#save-project").onclick = saveProjectFile;
    $("#open-project").onchange = e => { if (e.target.files[0]) openProjectFile(e.target.files[0]); e.target.value = ""; };
    const mp = $("#music-pick"); if (mp) mp.onchange = e => { if (e.target.files[0]) globalThis.SIM.app.pickMusic(e.target.files[0]); e.target.value = ""; };
    const mc = $("#music-clear"); if (mc) mc.onclick = () => globalThis.SIM.app.clearMusic();
    const mb = $("#music-builtin"); if (mb) mb.onclick = () => globalThis.SIM.app.useBuiltInMusic();
    renderDock();
    updatePlayheadDom(transport.playhead);
  }
  // Module-level so the drag-tracking pointermove/pointerup/pointercancel
  // listeners can be registered ONCE (see wireGlobalDragHandlers, called once
  // from boot()) instead of once per render - re-adding document-level
  // listeners on every render() (which fires on nearly every edit) would
  // otherwise pile up forever and both slow the page down and eventually
  // move several cues at once per drag.
  let cueDrag = null;
  function cueDragAt(e) {
    const D = state.show.duration;
    const dt = (e.clientX - cueDrag.startX) / cueDrag.boxWidth * D;
    const snap = e.shiftKey ? 5 : 1;
    return Math.max(0, Math.min(D, Math.round((cueDrag.startAt + dt) / snap) * snap));
  }
  function wireGlobalDragHandlers() {
    document.addEventListener("pointermove", e => {
      if (cueDrag) {
        const at = cueDragAt(e);
        const marker = document.querySelector(`.cue-hold[data-cue="${CSS.escape(cueDrag.id)}"]`);
        if (marker) marker.style.left = (100 * at / state.show.duration).toFixed(3) + "%";
      }
      if (transport) transport.dragMove(e.clientX);
    });
    document.addEventListener("pointerup", e => {
      if (cueDrag) {
        const at = cueDragAt(e);
        document.querySelectorAll(".cue-hold.dragging").forEach(el => el.classList.remove("dragging"));
        const id = cueDrag.id; cueDrag = null;
        globalThis.SIM.app.updateCue(id, { at });
      }
      if (transport) transport.endDrag();
    });
    document.addEventListener("pointercancel", () => {
      cueDrag = null; document.querySelectorAll(".cue-hold.dragging").forEach(el => el.classList.remove("dragging"));
      if (transport) transport.cancelDrag();
    });
  }
  function wireTrackEvents() {
    document.querySelectorAll(".tl-track").forEach(track => {
      track.addEventListener("click", e => {
        if (e.target.closest(".cue-hold") || e.target.closest(".cue-band")) return;
        const box = track.getBoundingClientRect();
        const D = state.show.duration;
        const t = Math.round(Math.max(0, Math.min(D, (e.clientX - box.left) / box.width * D)));
        const item = state.items.find(i => i.item === track.dataset.track);
        const design = nextUnusedDesign(item);
        if (!design) {
          // Only garments with at least one design ever get a track at all
          // (trackItems() filters on item.map), but ALL of that garment's
          // designs can still be used up (nextUnusedDesign() then falls
          // back to item.designs[0] || null) or - the actual case this
          // guards - it can have a map and zero designs yet. Either way, a
          // cue with design:null used to get created anyway, and every
          // later render of it read "design None is not loaded"
          // (adversarial review round 2 - F10).
          toast("Load a design CSV for this item first.");
          return;
        }
        const id = globalThis.SIM.app.addCue({ item: item.item, at: t, design: design.name, partial: false });
        ui.cue = id; render();
      });
    });
    document.querySelectorAll(".cue-hold, .cue-band").forEach(el => el.addEventListener("click", e => {
      e.stopPropagation();
      ui.cue = el.dataset.cue; render();
    }));
    document.querySelectorAll("tr[data-cue]").forEach(tr => tr.addEventListener("click", () => { ui.cue = tr.dataset.cue; render(); }));
    document.querySelectorAll('.cue-hold[data-drag="1"]').forEach(el => {
      el.addEventListener("pointerdown", e => {
        e.preventDefault(); e.stopPropagation();
        const track = el.closest(".tl-track");
        const box = track.getBoundingClientRect();
        const cue = state.show.cues.find(c => sameId(c.id, el.dataset.cue));
        cueDrag = { id: cue.id, boxWidth: box.width, startAt: cue.at, startX: e.clientX };
        el.classList.add("dragging");
        try { el.setPointerCapture(e.pointerId); } catch {}
      });
    });
  }

  // ---- Simulator dock (looks row, transport, playhead go-to) ----
  const THUMB_VIEW = {
    sel: "#tl-thumbs", cache: [], fill: false, height: 130, cell: 8, head: 30,
    items: () => trackItems(),
    onPlayhead: t => updatePlayheadDom(t),
  };
  function updatePlayheadDom(t) {
    const D = state.show.duration;
    const marker = $("#playhead"); if (marker) marker.style.left = (100 * Math.max(0, Math.min(D, t)) / D).toFixed(3) + "%";
    const phTime = $("#ph-time"); if (phTime) phTime.textContent = clockShort(t);
    const tpTime = $("#tp-time"); if (tpTime) tpTime.textContent = `${clockShort(t)} / ${clockShort(D)}`;
    const goto = $("#goto-input");
    if (goto && document.activeElement !== goto) {
      goto.value = globalThis.SIM.mmss.format(t);
      goto.classList.remove("bad");
      const gotoEcho = $("#goto-input-echo");
      if (gotoEcho) { gotoEcho.classList.remove("bad"); gotoEcho.textContent = globalThis.SIM.mmss.human(t); }
    }
  }
  function redrawThumbs(t) { if (state && $(THUMB_VIEW.sel)) globalThis.SIM.looks.updateThumbColors(t, THUMB_VIEW, ctx()); }
  function fullRedrawThumbs(t) { if (state) globalThis.SIM.looks.renderThumbs(THUMB_VIEW, t, ctx()); }

  let transport = null;
  function ensureTransport() {
    if (transport) return transport;
    transport = globalThis.SIM.transport.create({
      duration: () => state.show.duration,
      isActive: () => ui.tab === "timeline",
      onPlayingChange: playing => { const b = $("#tp-play"); if (b) b.textContent = playing ? "⏸ Pause" : "▶ Play"; },
      onPlayhead: t => updatePlayheadDom(t),
      onThumbTick: t => redrawThumbs(t),
      toast,
    });
    return transport;
  }
  // The dock is `position:fixed` at the bottom of the window (designer.css)
  // so it always floats over whatever #content last scrolled to - including
  // the last ~220px of the Timeline tab's own content, which it simply hid
  // behind itself with no compensating space (adversarial review,
  // 2026-09-25). Measuring the dock's real height into a custom property
  // and giving #content a matching padding-bottom keeps that content
  // reachable, and self-corrects if the dock's height ever changes (a
  // narrower window wrapping the toolbar to two lines, for one).
  function syncDockHeight() {
    const dock = $("#tl-dock");
    const h = (dock && ui.tab === "timeline") ? dock.offsetHeight : 0;
    document.documentElement.style.setProperty("--tl-dock-h", h + "px");
  }
  function renderDock() {
    ensureTransport();
    const dock = $("#tl-dock");
    dock.style.display = ui.tab === "timeline" ? "block" : "none";
    if (ui.tab !== "timeline") { syncDockHeight(); return; }
    dock.innerHTML = `<div class="tl-dock-head">
      <span class="tl-dock-title">THE LOOKS AT <b id="tp-time"></b></span>
      <button id="tp-play">${transport.playing ? "⏸ Pause" : "▶ Play"}</button>
      <button id="tp-stop">⏹ Stop</button>
      <span style="margin-left:6px">${mmssField("goto-input", transport.playhead)}</span>
      <button id="goto-go">Go to</button>
      <button id="sim-view" class="${ui.simView ? "on" : ""}" style="margin-left:auto">Simulator view</button>
    </div>
    <div class="thumbs" id="tl-thumbs"></div>`;
    $("#tp-play").onclick = () => transport.toggle();
    $("#tp-stop").onclick = () => transport.stop();
    // wireMmss (not a bare parse-on-click): a typo in the go-to box gets the
    // same red/echo treatment as every other clock field instead of Go to
    // silently doing nothing (adversarial review, 2026-09-25); Enter/blur
    // (wireMmss's "change") seeks, the button re-triggers the same commit
    // for a value that is already valid.
    wireMmss("goto-input", sec => transport.seek(sec), () => transport.playhead);
    $("#goto-go").onclick = () => $("#goto-input").dispatchEvent(new Event("change"));
    $("#sim-view").onclick = () => { ui.simView = !ui.simView; persist(); render(); };
    THUMB_VIEW.fill = ui.simView;
    fullRedrawThumbs(transport.playhead);
    wirePlayheadDrag();
    syncDockHeight();
  }
  // Per-element pointerdown only - rebound every render since #ph-head/#ruler
  // are recreated by innerHTML each time. The move/up/cancel listeners are
  // document-level and registered ONCE, in wireGlobalDragHandlers() (see its
  // own comment: re-adding these every render would pile up forever).
  function wirePlayheadDrag() {
    const head = $("#ph-head"), ruler = $("#ruler");
    if (head) head.addEventListener("pointerdown", e => { e.preventDefault(); transport.beginDrag(ruler, e.clientX, true); try { head.setPointerCapture(e.pointerId); } catch {} });
    if (ruler) ruler.addEventListener("pointerdown", e => { if (e.target.closest("#ph-head")) return; transport.beginDrag(ruler, e.clientX, false); });
  }
  // index.html:1248-1262's own rAF-debounced resize handler, missing here
  // entirely (adversarial review, 2026-09-25): without it the looks row
  // never re-measures after the window (or the dock, once its own height is
  // wired to --tl-dock-h - see syncDockHeight()) changes size, so it clips
  // at narrow widths instead of reflowing - worst in Simulator view, which
  // is exactly when the row is meant to fill the window.
  let resizeScheduled = false;
  window.addEventListener("resize", () => {
    if (resizeScheduled) return;
    resizeScheduled = true;
    requestAnimationFrame(() => {
      resizeScheduled = false;
      if (ui.tab === "timeline" && state) { globalThis.SIM.looks.layoutLooks(THUMB_VIEW); syncDockHeight(); }
    });
  });
  // A "filebtn" is a <label> wrapping a display:none file input - it looks
  // and reads like a button, so it is given tabindex="0"/role="button" and
  // has to answer to Enter and Space like one (a <label> does not on its
  // own, and a hidden input cannot be focused at all). Capture phase, so
  // this runs before the Space-is-play handler below and can stop it: the
  // Timeline's own "Pick another file…" is a filebtn too, and a Space on it
  // must open the picker, not start the show.
  document.addEventListener("keydown", e => {
    if (e.key !== "Enter" && e.key !== " " && e.code !== "Space") return;
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const label = e.target.closest && e.target.closest("label.filebtn");
    const input = label && label.querySelector('input[type="file"]');
    if (!input) return;
    e.preventDefault(); e.stopPropagation();
    input.click();
  }, true);
  document.addEventListener("keydown", e => {
    if ((e.key === " " || e.code === "Space") && !e.ctrlKey && !e.metaKey && !e.altKey && !e.shiftKey) {
      const tag = (e.target.tagName || "").toLowerCase();
      if (["input", "select", "textarea", "button"].includes(tag)) return;
      if (ui.tab !== "timeline" || !transport) return;
      e.preventDefault(); transport.toggle();
    }
  });

  // ---- Save/Open project (bundle v1, plan §4.2) ----
  function downloadBlob(text, filename) {
    const blob = new Blob([text], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = filename; document.body.appendChild(a); a.click();
    a.remove(); setTimeout(() => URL.revokeObjectURL(url), 4000);
  }
  function saveProjectFile() {
    const bundle = globalThis.SIM.app.exportBundle();
    const text = JSON.stringify(bundle, null, 1);
    const bytes = new Blob([text]).size;
    if (bytes > REFUSE_BUNDLE_BYTES) { toast(`Refused: this project is ${fmtSize(bytes)} - over the 8 MB limit.`); return; }
    if (bytes > WARN_BUNDLE_BYTES) toast(`This is a large project (${fmtSize(bytes)}) - saving anyway.`);
    const now = new Date();
    const name = `az27ss-${now.getFullYear()}${pad2(now.getMonth() + 1)}${pad2(now.getDate())}-${pad2(now.getHours())}${pad2(now.getMinutes())}.json`;
    downloadBlob(text, name);
    toast(`Saved ${name} (${fmtSize(bytes)})`);
  }
  async function openProjectFile(file) {
    let data;
    try { data = JSON.parse(await file.text()); } catch { toast("Not a project file"); return; }
    const result = globalThis.SIM.app.openBundle(data);
    if (!result.ok) { toast(result.error); return; }
    toast(`Opened ${file.name}: ${result.saved?.length || 0} CSV(s), ${result.cues ?? 0} cue(s)`);
    ui.cue = null; render();
  }

  // ==================================================================
  // Help tab (Japanese, plan §3.8)
  // ==================================================================
  function renderHelp() {
    $("#content").innerHTML = `<div class="help">
      <h2>Quick start (English)</h2>
      <p>Double-click <code>az27ss-simulator.html</code> (or <code>designer.html</code> during development) - no install, no server. Drop the garments' map and design CSV files anywhere on this page to begin (a folder works too), or use <b>Add CSV</b> in the header. The <b>Designs</b> tab has an <b>Add CSV</b> of its own under DESIGNS OF THIS ITEM, which adds the files you pick to that one garment: whatever they are named, they are saved under that garment's name and belong to it alone. A <code>*_map.csv</code> that belongs to another garment already in the project is refused there - a garment's wiring is not interchangeable the way its patterns are - so use the header's <b>Add CSV</b> for that one. Open the <b>Timeline</b> tab and click anywhere on an item's track to add a cue there, using that item's next design; click an existing cue to edit it, or drag it to move it (hold Shift for 5 s steps instead of 1 s).</p>
      <p><b>Red</b> always means "this needs fixing before it is right": a red-outlined mm.ss field could not be read as minutes.seconds; a red left border on a cue means the model found a problem with it (open it to see why); a dot next to an item in the sidebar is red when that item has one or more problems. Clicking a track for an item that has no design CSV yet is refused with a toast, rather than creating a cue with nothing to show.</p>
      <p>When the timeline is ready, <b>Save project…</b> writes everything (every CSV plus the whole timeline) into one <code>.json</code> file - hand that file to whoever runs the show; on the operator's own page, "Load bundle…" reads it in and keeps everything already in place exactly as it was, replacing only the CSVs and the timeline.</p>
      <p><b>Music.</b> The show's music is built into the file the operator gave you, so it plays as soon as you press Play - nothing to pick, nothing to install. If the show's music changes you receive a new file; the Timeline toolbar tells you which track is built in. You can still choose a different audio file with <b>Pick another file…</b>, which lasts for this session only - <b>Back to the built-in track</b> returns to the one that came with the file.</p>
      <p>Clock positions (Start, End, Show length, the dock's go-to box) are typed as mm.ss - minutes and seconds, not a decimal fraction of a minute: <code>3.05</code> is 3 minutes 05 seconds; a single-digit second still counts as seconds, so <code>3.5</code> is also 3 minutes 05 seconds; <code>3.60</code> is not valid (there is no 60th second) and turns the field red. The badge and the live "3 min 05 s" readout next to every one of these fields are there so this never has to be memorised.</p>
      <p>Supported browsers: Safari 14.1 or later, or a recent Chrome or Edge. A private/incognito window may refuse to keep the autosaved copy at all (see the warning banner in the header when that happens) - use <b>Save project…</b> there instead of relying on autosave.</p>
      <h2>開き方</h2><p>このファイル（<code>az27ss-simulator.html</code> または <code>designer.html</code>）をダブルクリックするだけで開きます。インストールもサーバーも不要です。Windows は Edge か Chrome、macOS は Safari か Chrome を推奨します。</p>
      <h2>CSV の入れ方</h2><p>マップCSV（<code>*_map.csv</code>）とデザインCSV（<code>*_color_名前_grid.csv</code>）を、このページのどこにでもドラッグ＆ドロップしてください（フォルダごとも可）。ヘッダーの「Add CSV」ボタンでも選べます。<b>Designs</b> タブの「DESIGNS OF THIS ITEM」にある「Add CSV」を使うと、選んだファイルはその1着だけに追加されます（別の型番の名前でも、その1着の名前で保存されます）。ただし<b>他の衣装のマップCSV（<code>*_map.csv</code>）は受け付けません</b>（配線図は柄と違って入れ替えられるものではないため）。その場合はヘッダーの「Add CSV」を使ってください。同じ名前のファイルは上書きされます。</p>
      <h2>mm.ss の読み方</h2><p>開始・終了・ショー全体の長さなど「時刻」は分.秒（mm.ss）で入力します。例：<code>3.05</code> → 3分05秒。<code>3.5</code> のように秒が1桁でも「3分05秒」として読みます。<code>3.60</code> のように60秒以上は無効（赤色）になります。入力欄の横に読み方がそのまま表示されます（例：「3 min 05 s」）。</p>
      <h2>音楽</h2><p>ショーの音源は、オペレーターから渡されたこのファイルの中に埋め込まれています。再生ボタンを押せばそのまま鳴ります（選び直す操作は不要です）。音源が差し替わったときは、新しいファイルが届きます ―― Timeline のツールバーに、いま埋め込まれている曲名が出ます。別の音源で確認したいときは「Pick another file…」で選べます（そのセッションの間だけ。「Back to the built-in track」で元の埋め込み音源に戻ります）。</p>
      <h2>遷移（トランジション）6種</h2><p>各デザインの塗り替え方向を選べます：既定（配線どおり、変更なし）、Top to bottom（上から下）、Bottom to top（下から上）、Left to right (audience)（観客席から見て左から右）、Right to left (audience)（観客席から見て右から左）、Centre outward（中心から外へ）。「秒」は最初の一列が変わってから最後の一列が変わるまでの時間です。</p>
      <h2>保存と受け渡し</h2><p>「Save project…」でこのブラウザ内のプロジェクト全体（CSVとタイムライン）を1つのJSONファイルに書き出します。「Open project…」で読み込みます。オペレーター側の「Load bundle…」に同じファイルを渡すと、ユニットの割り当てはそのままに、CSVとタイムラインだけが更新されます。</p>
      <h2>制限</h2><p>自動保存はブラウザに約4MBまで。自分で選んだ音楽ファイルは64MBまでで、名前だけが記録されます（プロジェクトファイルには音のデータは入りません。再読み込み後は選び直すか、埋め込みの音源に戻してください）。保存ファイルは6MBを超えると警告、8MBを超えると保存を拒否します。プライベートブラウジングでは自動保存が効かないことがあります（そのときはヘッダーに警告が出ます）。</p>
      <h2>自己テスト</h2><p><button id="run-selftest">Run self-test</button> <span id="selftest-result"></span></p>
      <h2>連絡先</h2><p>不具合や質問は ${esc("y.hirata@r2-engineering.com")} まで。</p>
    </div>`;
    $("#run-selftest").onclick = () => {
      if (typeof globalThis.__selftest !== "function") {
        // The shipped dist/az27ss-simulator.html is build_designer.py's
        // DEFAULT (no --with-goldens - DIST SIZE, adversarial review round
        // 2): the Python-cross-check data alone was over half the page's
        // weight, of no use to a designer double-clicking this file. The
        // developer build (conductor/web/designer.html) always has it.
        const out = $("#selftest-result");
        out.textContent = "not in this file - the self-test lives in the developer build (conductor/web/designer.html), not the one designers open";
        out.className = ""; return;
      }
      const r = runSelfTestSafely();
      // Re-query, don't reuse a reference captured before the call
      // (adversarial review follow-up, 2026-09-25's own fix): restoring the
      // project runs rebuild()->render(), which - since the Help tab is
      // still open - calls renderHelp() again and replaces #content
      // wholesale; the element this closure grabbed a moment ago is now
      // detached, so writing to it silently went nowhere.
      const out = $("#selftest-result");
      if (out) { out.textContent = r.ok ? `OK - ${r.total} check(s) passed` : `${r.failed} of ${r.total} failed`; out.className = r.ok ? "ok" : "fail"; }
    };
  }
  // The golden self-test (selftest.js, Coder P) drives SIM.app through
  // newProject()/addFiles()/exportBundle()/newProject()/openBundle() as its
  // own app-smoke case - fine in isolation, but calling it from THIS page's
  // Help tab used to run those straight against the live session: it wiped
  // whatever the designer was working on and overwrote the autosave with
  // the starter data (adversarial review, 2026-09-25). Snapshot everything
  // the self-test can touch first (a deep copy of `project`, plus musicUrl
  // and the ui selection, both of which live outside `project`), run it,
  // then restore all three - the designer never sees so much as a flicker
  // of the substitute project, and the autosave is untouched.
  function runSelfTestSafely() {
    const savedProject = JSON.parse(JSON.stringify(project));
    const savedMusicFile = musicFile;   // the File, not musicUrl - see musicFile's own comment
    const savedUi = Object.assign({}, ui);
    let result;
    try {
      result = globalThis.__selftest();
    } finally {
      Object.assign(ui, savedUi);
      // Re-mint the object URL from the ORIGINAL File, and BEFORE
      // setProject() below, not after (adversarial review round 2 - F5):
      // the self-test's own newProject()/openBundle() calls revoke
      // whatever musicUrl was live, so a snapshot of the URL string itself
      // is already dead by the time this runs - only the File can make a
      // working URL again. Doing it first also means setProject()'s own
      // rebuild() sees the correct, live musicUrl on its very first pass,
      // not one render later.
      if (savedMusicFile) {
        if (musicUrl) { URL.revokeObjectURL(musicUrl); musicUrl = null; }
        musicFile = savedMusicFile;
        musicUrl = URL.createObjectURL(savedMusicFile);
      } else {
        if (musicUrl) { URL.revokeObjectURL(musicUrl); musicUrl = null; }
        musicFile = null;
      }
      // pushMusicToTransport(), never setMusic(null): the no-picked-file
      // branch used to hand the transport a bare null, which with an
      // embedded track would mean "Run self-test silenced the file the
      // operator built for you" - the built-in track is not the
      // designer's to lose.
      pushMusicToTransport();
      app.setProject(savedProject);
    }
    return result;
  }

  // ==================================================================
  // CSV drops / Add CSV (plan §3.2)
  // ==================================================================
  function readFileAsText(file) { return new Promise((res, rej) => { const r = new FileReader(); r.onload = () => res(r.result); r.onerror = rej; r.readAsText(file); }); }
  // What a Mac does to file names on the way here (2026-09-25, a designer's
  // Mac refused every CSV that a Windows PC accepted):
  //  - a zip extracted by Finder carries "__MACOSX/._NAME.csv" AppleDouble
  //    twins and ".DS_Store" - the "._" twins END in _map.csv and used to be
  //    accepted as junk garments named "._AZ271…"; they are metadata, so
  //    they are set aside by name with a reason that says so;
  //  - Safari appends ".txt" to a text/plain download ("NAME.csv.txt"), and
  //    Finder hides the extension so nobody sees it - the trailing .txt is
  //    dropped when what is left is a .csv;
  //  - HFS+/APFS hand names back in NFD; ASCII names do not care, but a
  //    designer's own file name might - normalise to NFC first.
  function macSafeName(name) {
    let n = String(name);
    try { n = n.normalize("NFC"); } catch {}
    if (/\.csv\.txt$/i.test(n)) n = n.replace(/\.txt$/i, "");
    return n;
  }
  function isMacMetadata(name) {
    const n = String(name);
    return /^\._/.test(n) || n === ".DS_Store" || /^__MACOSX$/i.test(n);
  }
  async function addFilesFromBlobs(files) {
    // Non-CSV names are refused BY NAME, like the per-item path, instead of
    // dropped in silence (adversarial review F6): dropping a folder whose
    // CSVs sit one level further down used to look exactly like dropping a
    // folder of CSVs that the page rejected - nothing happened either way.
    // Still filtered on the name before reading, never after: a dropped
    // folder can hold a video, and reading one as text to learn it is not a
    // CSV is the one mistake this must not make.
    const list = [], refused = [];
    const skipped = [];
    for (const f of files) {
      if (isMacMetadata(f.name)) { skipped.push(String(f.name)); continue; }
      const name = macSafeName(f.name);
      if (!/\.csv$/i.test(name)) { refused.push({ name: String(f.name), error: refuseReason(name) }); continue; }
      try { list.push({ name, text: await readFileAsText(f) }); }
      catch { refused.push({ name: String(f.name), error: "could not be read" }); }
    }
    const result = list.length ? globalThis.SIM.app.addFiles(list) : { saved: [], refused: [] };
    if (skipped.length) console.info("skipped macOS metadata files:", skipped);
    const all = refused.concat(result.refused);
    const parts = [];
    if (result.saved.length) parts.push(`${result.saved.length} file(s) added`);
    if (all.length) parts.push(`${all.length} refused: ` + briefly(all, r => `${r.name} (${r.error})`));
    toast(parts.join(" · ") || "No CSV files found");
    ui.cue = null; render();
  }
  // ---- Add CSV to ONE item (the Designs tab's own button) ----
  // index.html's uploadOwn(), same rule: a design CSV belongs to the item
  // its name begins with, so two garments of the same shape come back from
  // the designer under the same file names. Picked here, a file is renamed
  // ONTO this item - Look22_color_pattern01_grid.csv is saved as
  // <item>_color_pattern01_grid.csv - rather than refused for having the
  // wrong prefix; a name that is neither a *_map.csv nor a
  // *_color_NAME_grid.csv is refused, because there is nothing to rename it
  // to. Returns null for "cannot belong to any item".
  function renameOntoItem(itemKey, name) {
    const raw = String(name);
    const m = raw.match(/(_map|_color_.+grid).*\.csv$/i);
    return m ? itemKey + raw.slice(m.index) : null;
  }
  async function addFilesToItemFromBlobs(itemKey, files) {
    const list = [], refused = [];
    for (const f of files) {
      if (isMacMetadata(f.name)) continue;
      const name = macSafeName(f.name);
      if (renameOntoItem(itemKey, name) === null) { refused.push({ name: String(f.name), error: refuseReason(name) }); continue; }
      try { list.push({ name, text: await readFileAsText(f) }); }
      catch { refused.push({ name: String(f.name), error: "could not be read" }); }
    }
    const result = list.length ? globalThis.SIM.app.addFilesToItem(itemKey, list)
                               : { saved: [], renamed: [], refused: [] };
    const all = refused.concat(result.refused);
    const item = state.items.find(i => i.item === itemKey);
    const parts = [];
    if (result.saved.length) parts.push(`${result.saved.length} file(s) added to ${itemName(item) || itemKey}`);
    // Both names, so nobody has to guess what happened to a file they picked.
    if (result.renamed.length) parts.push("saved as " + briefly(result.renamed, r => `${r.from} → ${r.to}`));
    if (all.length) parts.push(`${all.length} refused: ` + briefly(all, r => `${r.name} (${r.error})`));
    toast(parts.join(" · ") || "No CSV files found");
    ui.cue = null; render();
  }
  function traverseEntry(entry) {
    return new Promise(resolve => {
      if (entry.isFile) entry.file(file => resolve([file]));
      else if (entry.isDirectory) {
        const reader = entry.createReader();
        const all = [];
        const readMore = () => reader.readEntries(async entries => {
          if (!entries.length) { const nested = await Promise.all(all.map(traverseEntry)); resolve(nested.flat()); return; }
          all.push(...entries); readMore();
        });
        readMore();
      } else resolve([]);
    });
  }
  async function handleDataTransfer(dt) {
    if (dt.items && dt.items[0] && dt.items[0].webkitGetAsEntry) {
      const entries = [...dt.items].map(i => i.webkitGetAsEntry()).filter(Boolean);
      const groups = await Promise.all(entries.map(traverseEntry));
      await addFilesFromBlobs(groups.flat());
    } else {
      await addFilesFromBlobs([...dt.files]);
    }
  }
  function wireDropZone() {
    let depth = 0;
    document.addEventListener("dragenter", e => { e.preventDefault(); depth++; $("#drop").classList.add("on"); });
    document.addEventListener("dragover", e => e.preventDefault());
    document.addEventListener("dragleave", () => { depth = Math.max(0, depth - 1); if (!depth) $("#drop").classList.remove("on"); });
    document.addEventListener("drop", e => { e.preventDefault(); depth = 0; $("#drop").classList.remove("on"); handleDataTransfer(e.dataTransfer); });
    $("#pick").addEventListener("change", e => { addFilesFromBlobs([...e.target.files]); e.target.value = ""; });
  }

  // ==================================================================
  // Master render / tab wiring
  // ==================================================================
  // Deliberately not the usual tab content: `state` (everything the normal
  // render path reads) is stale relative to `project` the moment
  // buildError is set, so nothing built from it - the ITEMS sidebar
  // included - is trustworthy to show. Reload is the only escape offered
  // because it is the only one guaranteed correct: it drops back to the
  // last state that DID build (the autosave, or the on-disk starter if
  // even that failed).
  function renderBuildErrorCard(message) {
    document.querySelectorAll("[data-tab]").forEach(b => b.classList.toggle("on", b.dataset.tab === ui.tab));
    $("#items").innerHTML = ""; $("#orphans").innerHTML = ""; $("#ws").textContent = "";
    $("#tl-dock").style.display = "none";
    document.documentElement.style.setProperty("--tl-dock-h", "0px");   // else stale from before the error (adversarial review round 2 - F10)
    $("#content").innerHTML = `<div class="card"><h2 style="color:var(--err)">Internal error</h2>
      <p>This project could not be rebuilt: <code>${esc(message)}</code></p>
      <p>Nothing shown here can be trusted until this is fixed. Your last change is not lost, but the screen cannot reflect it - reload to get back to the last copy that DID build (the autosave, or Open project… to load a saved one).</p>
      <button id="build-error-reload">Reload</button></div>`;
    const btn = $("#build-error-reload"); if (btn) btn.onclick = () => location.reload();
  }
  function render() {
    if (buildError) { renderBuildErrorCard(buildError); return; }
    // Adversarial review round 2 (F2): rebuild()'s own try/catch only ever
    // covered SIM.buildState() itself - a throw from any of the render
    // functions below (all of them read `state`, none of them are proven
    // safe against every value it can hold) used to escape render()
    // entirely, past whatever called it, with nothing on screen updated and
    // no indication anything had gone wrong.
    try {
      document.querySelectorAll("[data-tab]").forEach(b => b.classList.toggle("on", b.dataset.tab === ui.tab));
      renderSidebar();
      if (ui.tab === "timeline") renderTimelineTab();
      else { $("#tl-dock").style.display = "none"; syncDockHeight(); if (ui.tab === "help") renderHelp(); else renderDesigns(); }
    } catch (e) {
      console.error("render failed", e);
      buildError = (e && e.message) ? e.message : String(e);
      renderBuildErrorCard(buildError);
    }
  }
  function wireChrome() {
    document.querySelectorAll("[data-tab]").forEach(b => b.addEventListener("click", () => { ui.tab = b.dataset.tab; ui.cue = null; persist(); render(); }));
    $("#new-project").addEventListener("click", () => {
      if (!confirm("Start a new, empty project? Drop new CSV files to fill it (your autosaved project is replaced).")) return;
      globalThis.SIM.app.newProject();
    });
    document.body.addEventListener("click", e => {
      const card = e.target.closest(".item"); if (card && !e.target.closest("input")) { ui.item = card.dataset.item; ui.design = null; persist(); render(); }
      const del = e.target.closest("[data-del]"); if (del) { globalThis.SIM.app.removeFile(del.dataset.del); render(); }
      const dsg = e.target.closest(".dsg[data-design]"); if (dsg && !e.target.closest("select,input,button")) { ui.design = dsg.dataset.design; persist(); render(); }
      const vb = e.target.closest("[data-view]"); if (vb) { ui.view = vb.dataset.view; persist(); render(); }
    });
    document.body.addEventListener("change", e => {
      // The Designs tab's per-item "Add CSV". The item key travels on the
      // button itself rather than being read back off ui.item, so the files
      // can only ever land on the item whose card was actually clicked.
      const picker = e.target.closest("[data-pick-item]");
      if (picker && e.target.type === "file") {
        const files = [...e.target.files];
        e.target.value = "";
        addFilesToItemFromBlobs(picker.dataset.pickItem, files);
        return;
      }
      const lab = e.target.closest("[data-label]");
      if (lab) {
        const card = lab.closest(".item"); const item = card.dataset.item;
        const other = card.querySelector(lab.dataset.label === "look" ? '[data-label="model"]' : '[data-label="look"]');
        const patch = { look: "", model: "" }; patch[lab.dataset.label] = lab.value;
        patch[other.dataset.label] = other.value;
        globalThis.SIM.app.setLabel(item, patch);
      }
      if (e.target.id === "design-pick") { ui.design = e.target.value; persist(); render(); }
      const trSeq = e.target.closest("[data-tr-seq]");
      if (trSeq && !trSeq.closest("#cue-editor-body")) {
        const name = trSeq.dataset.trSeq, span = document.querySelector(`[data-tr-span="${CSS.escape(name)}"]`);
        globalThis.SIM.app.setDesignTransition(name, trSeq.value, Number(span?.value) || 0);
      }
      const trSpan = e.target.closest("[data-tr-span]");
      if (trSpan && !trSpan.closest("#cue-editor-body")) {
        const name = trSpan.dataset.trSpan, sel = document.querySelector(`[data-tr-seq="${CSS.escape(name)}"]`);
        globalThis.SIM.app.setDesignTransition(name, sel.value, Number(trSpan.value) || 0);
      }
    });
  }

  // #debug fps overlay (plan §3.9)
  function wireFps() {
    if (!/#debug/.test(location.hash)) return;
    const el = $("#fps"); el.style.display = "block";
    let frames = 0, last = performance.now();
    (function loop() {
      frames++;
      const now = performance.now();
      if (now - last >= 500) { el.textContent = (frames / ((now - last) / 1000)).toFixed(0) + " fps"; frames = 0; last = now; }
      requestAnimationFrame(loop);
    })();
  }

  // ==================================================================
  // SIM.app — the frozen seam (plan §2.3)
  // ==================================================================
  // Says what is wrong with THIS name (adversarial review F6: it used to
  // take the name and ignore it, so a dropped .xlsx and a mis-named CSV got
  // the same sentence, and the .xlsx one did not describe the problem).
  function refuseReason(name) {
    return /\.csv$/i.test(String(name)) ? "not a *_map.csv or *_color_NAME_grid.csv"
                                        : "not a .csv file";
  }
  // A refusal/rename list, short enough to read in a toast: a dropped folder
  // can hold a hundred files nobody wants named one by one.
  function briefly(entries, format, limit) {
    const shown = entries.slice(0, limit || 3).map(format).join("; ");
    return entries.length > (limit || 3) ? `${shown} +${entries.length - (limit || 3)} more` : shown;
  }
  // The one place that touches `musicUrl` and `project.show.music` together
  // (pickMusic/clearMusic/newProject/openBundle all go through it): a File
  // (or null to clear) revokes whatever object URL was live first, then
  // creates the new one and pushes it to the transport - so there is no
  // window where musicUrl is stale, un-revoked, or silently mismatched with
  // project.show.music.name (adversarial review, 2026-09-25: newProject()
  // used to null musicUrl without revoking it - a leak, AND it left the OLD
  // blob playable a moment longer since the transport was never told;
  // openBundle() used to replace project.show.music without touching
  // musicUrl at all, so state.music kept showing the OLD blob under the
  // NEW name instead of the "re-pick the file" bar plan §3.7 promises).
  //
  // Clearing (file = null) now means "back to the built-in track", not
  // "silence", whenever this file has one: musicUrl going null is what
  // hands playback back to SIM.embeddedMusic (pushMusicToTransport), and
  // the project's music name goes back to the built-in track's rather than
  // to nothing - the show's music has not stopped being the show's music
  // just because the designer put their own copy away.
  function setMusicFile(file) {
    if (musicUrl) { URL.revokeObjectURL(musicUrl); musicUrl = null; }
    musicFile = file || null;
    musicUrl = file ? URL.createObjectURL(file) : null;
    project.show.music = file ? { name: file.name }
                              : (builtInMusicName() ? { name: builtInMusicName() } : null);
    pushMusicToTransport();
  }
  const app = {
    newProject() {
      setMusicFile(null);
      // adoptBuiltInName: setMusicFile above wrote the built-in name onto
      // the OLD project, which is about to be thrown away - the new one
      // needs it too, or "New project" would quietly be the one way to
      // lose the built-in track's name off the timeline.
      project = adoptBuiltInName(freshProject()); ui.item = null; ui.design = null; ui.cue = null; persist(); rebuild();
    },
    getProject() { return project; },
    // Not part of plan_designer_sim.md §2.3's frozen list, but a natural
    // companion to getProject() - used to put a project back exactly as it
    // was (runSelfTestSafely() above is the one caller today).
    setProject(p) { project = p; rebuild(); persist(); },
    getState() { return state; },
    render() { render(); },
    addFiles(list) {
      const saved = [], refused = [];
      for (const { name, text } of list) {
        const raw = String(name);
        // Refused outright, not silently renamed (adversarial review,
        // 2026-09-25): a dropped "../../../etc/foo_map.csv" or
        // "sub/dir_map.csv" used to become "sub_dir_map.csv" and get saved
        // as if the designer had typed that name (a real file dropped from
        // a folder never carries a path in its own `File.name`, only in the
        // webkitGetAsEntry() traversal this page already flattens before
        // calling here - so a name that still has one is not a plain
        // file). ".." only as a whole path segment (adversarial review
        // round 2 - F10), not merely a substring: a slash-free name like
        // "AZ271SP0002_color_a..b_grid.csv" cannot traverse anywhere - the
        // old check refused it anyway, with the generic "not a *_map.csv…"
        // reason, which does not even describe what was wrong with it. A
        // distinct, accurate message for this specific refusal too, rather
        // than reusing the unrelated-extension one.
        const hasBadPathSegment = raw.split(/[\\/]/).includes("..");
        if (raw.includes("/") || raw.includes("\\") || hasBadPathSegment) {
          refused.push({ name: raw, error: "file names must not contain / \\ or .." }); continue;
        }
        if (globalThis.SIM.look.kind(raw) === null) { refused.push({ name: raw, error: refuseReason(raw) }); continue; }
        project.files[raw] = text.replace(/\r\n?/g, "\n");
        saved.push(raw);
      }
      rebuild(); persist();
      return { saved, refused };
    },
    // Not on plan_designer_sim.md §2.3's frozen list, but the same kind of
    // natural companion as setProject(): addFiles() for ONE item, with each
    // file renamed onto it first (see renameOntoItem() above). Everything
    // that actually touches project.files still goes through addFiles(), so
    // the path-segment guard and the *_map/_color_…_grid check apply to the
    // RENAMED name too - a caller cannot smuggle a bad name past them by
    // coming in this way.
    addFilesToItem(itemKey, list) {
      const items = [...list];
      if (!state.items.some(i => i.item === itemKey)) {
        return { saved: [], renamed: [],
                 refused: items.map(f => ({ name: String(f.name), error: "no item of that name in this project" })) };
      }
      const renamed = [], out = [], refused = [];
      const takenBy = new Map();          // target name -> the picked file already going there
      for (const { name, text } of items) {
        const raw = String(name);
        const to = renameOntoItem(itemKey, raw);
        if (to === null) { refused.push({ name: raw, error: refuseReason(raw) }); continue; }
        // A garment's MAP is not interchangeable the way its designs are
        // (adversarial review F1): renaming AZ271SD1301_map.csv onto this
        // item replaced this garment's wiring with another garment's, threw
        // away the original text, and reported it as a success - the
        // hundreds of problems that followed were the only hint. A design
        // grid renamed across garments is the ordinary case and still is;
        // a map that belongs to a garment this project already has is not.
        const ownerOfMap = globalThis.SIM.look.kind(raw) === "map" ? globalThis.SIM.look.mapItem(raw) : null;
        if (ownerOfMap && ownerOfMap.toLowerCase() !== itemKey.toLowerCase()
            && state.items.some(i => i.item.toLowerCase() === ownerOfMap.toLowerCase())) {
          refused.push({ name: raw, error: "another garment's map - use the header's Add CSV for it" });
          continue;
        }
        // Two picked files that would land on the same name (F3): the first
        // wins and the second is refused, naming both. Letting them through
        // meant addFiles() silently kept whichever came last, with the toast
        // counting them both as saved.
        if (takenBy.has(to)) {
          refused.push({ name: raw, error: `would overwrite ${takenBy.get(to)} from this same pick` });
          continue;
        }
        takenBy.set(to, raw);
        if (to !== raw) renamed.push({ from: raw, to });
        out.push({ name: to, text });
      }
      // No rebuild()/persist() for a pick that saved nothing.
      if (!out.length) return { saved: [], renamed, refused };
      const result = app.addFiles(out);
      // A file that addFiles() itself refused was never renamed onto
      // anything, so it must not be reported as one that was.
      const stillRefused = new Set(result.refused.map(r => r.name));
      return { saved: result.saved, renamed: renamed.filter(r => !stillRefused.has(r.to)),
               refused: refused.concat(result.refused) };
    },
    removeFile(name) { delete project.files[name]; rebuild(); persist(); },
    setShow(patch) { Object.assign(project.show, patch); rebuild(); persist(); },
    setLabel(item, label) { project.show.labels[item] = { look: label.look || "", model: label.model || "" }; rebuild(); persist(); },
    addCue({ item, at, design, partial }) {
      // A string id, matching timeline.py:241's str(...) (model.js's clean()
      // ports that exactly - see sameId()'s comment above): generating a
      // number here was the root cause of EDIT CUE/selection/drag never
      // matching a real cue once state.show.cues came back through
      // SIM.buildState with everything stringified.
      const id = String((project.show.cues.reduce((m, c) => Math.max(m, Number(c.id) || 0), 0) || 0) + 1);
      project.show.cues.push({ id, item, at, design, partial: !!partial, refresh_s: null, transition: "design", sequence: "natural", span_s: 0 });
      rebuild(); persist(); return id;
    },
    updateCue(id, patch) {
      const cue = project.show.cues.find(c => sameId(c.id, id)); if (!cue) return;
      if ("end" in patch) {
        // "End" edits the NEXT cue's Start on the same item (or is a no-op on the last cue).
        const mine = project.show.cues.filter(c => c.item === cue.item).sort((a, b) => a.at - b.at);
        const idx = mine.findIndex(c => sameId(c.id, id));
        const next = mine[idx + 1];
        if (next) next.at = patch.end;
        delete patch.end;
      }
      Object.assign(cue, patch);
      rebuild(); persist();
    },
    deleteCue(id) { project.show.cues = project.show.cues.filter(c => !sameId(c.id, id)); if (sameId(ui.cue, id)) ui.cue = null; rebuild(); persist(); },
    setDesignTransition(designFile, sequence, span_s) { project.show.transitions[designFile] = { sequence, span_s }; rebuild(); persist(); },
    exportBundle() {
      const now = localIso();
      return {
        format: "epaper-show-bundle", version: 1, exported: now, app: "az27ss-simulator 1.0",
        show: { format: "epaper-show", version: 1, exported: now, workspace: "designer",
                duration: project.show.duration, refresh_s: project.show.refresh_s,
                cues: project.show.cues.map(({ id, item, at, design, partial, refresh_s, transition, sequence, span_s }) =>
                  ({ id, item, at, design, partial: !!partial, refresh_s: refresh_s ?? null, transition: transition || "design", sequence: sequence || "natural", span_s: span_s ?? 0 })),
                transitions: project.show.transitions || {}, labels: project.show.labels || {},
                boards: project.show.boards || {}, music: project.show.music ? { name: project.show.music.name } : null },
        files: Object.assign({}, project.files),
        music: project.show.music ? { name: project.show.music.name } : null,
      };
    },
    openBundle(obj) {
      if (!obj || typeof obj !== "object") return { ok: false, error: "Not a project file" };
      let show, files = null;
      if (obj.format === "epaper-show-bundle" && obj.version === 1) { show = obj.show; files = obj.files; }
      else if (obj.format === "epaper-show" && obj.version === 1) { show = obj; }
      else return { ok: false, error: `Unrecognised project file (format ${JSON.stringify(obj.format)}, version ${JSON.stringify(obj.version)})` };
      let saved = [];
      if (files && typeof files === "object") {
        const r = app.addFiles(Object.entries(files).map(([name, text]) => ({ name, text: String(text) })));
        saved = r.saved;
      }
      if (show && typeof show === "object") {
        // Clamped to the same bounds the typed fields enforce (adversarial
        // review round 2, 2026-09-25 - F2): an untrusted project file had no
        // bounds at all here. 5,000,000 s made the ruler draw ~83,000 ticks
        // (a render at 600 ms and climbing); 1e9 overflowed a Number
        // somewhere downstream into a RangeError that escaped openBundle
        // entirely, poisoning `project` with nothing to show for it (see
        // F2's other half in rebuild()/render() below).
        if ("duration" in show) {
          const d = Number(show.duration);
          if (Number.isFinite(d)) {
            const clamped = Math.max(1, Math.min(MAX_SHOW_DURATION_S, d));
            // Said out loud, not just applied silently (adversarial review
            // round 2, third pass - N3): a project file that asked for
            // something out of range got the clamp with no sign anything
            // had changed from what it actually said.
            if (clamped !== d) toast(`Show length clamped to ${globalThis.SIM.mmss.format(clamped)}.`);
            project.show.duration = clamped;
          }
        }
        if ("refresh_s" in show) {
          const r = Number(show.refresh_s);
          if (Number.isFinite(r)) {
            const clamped = Math.max(1, Math.min(60, r));
            if (clamped !== r) toast(`Default refresh time clamped to ${clamped.toFixed(1)} s.`);
            project.show.refresh_s = clamped;
          }
        }
        if ("cues" in show) {
          if (Array.isArray(show.cues)) {
            // Every cue id run through the same [A-Za-z0-9_-]{1,40} shape
            // the model itself generates (adversarial review round 2 - F1):
            // an id is interpolated into a data-cue attribute
            // (renderTracks/cueTable) and, unescaped before this fix, into a
            // querySelector string too - a hand-edited project file could
            // carry a quote-and-markup payload as a cue id and have it
            // execute the moment the Timeline tab rendered. esc()/
            // CSS.escape() at every use site closes the immediate hole;
            // this closes it at the source too, and de-duplicates while at
            // it (two cues sharing an id would otherwise silently alias
            // sameId() everywhere).
            const SAFE_CUE_ID = /^[A-Za-z0-9_-]{1,40}$/;
            const usedIds = new Set();
            let regeneratedAnId = false;
            let nextSpare = 1;
            const freshId = () => { let id; do { id = String(nextSpare++); } while (usedIds.has(id)); return id; };
            project.show.cues = show.cues.map(c => {
              const cue = Object.assign({}, c);
              const raw = cue.id === undefined || cue.id === null ? "" : String(cue.id);
              if (!SAFE_CUE_ID.test(raw) || usedIds.has(raw)) { cue.id = freshId(); regeneratedAnId = true; }
              else cue.id = raw;
              usedIds.add(cue.id);
              return cue;
            });
            if (regeneratedAnId) toast("This project had one or more invalid or duplicate cue ids - they were replaced.");
          } else {
            toast("Ignored an invalid project file: cues must be a list.");
          }
        }
        if ("transitions" in show) project.show.transitions = show.transitions || {};
        if ("labels" in show) project.show.labels = show.labels || {};
        if ("boards" in show) project.show.boards = show.boards || {};
        if ("music" in show) {
          // A bundle never carries the audio itself (plan §4.2: "music
          // name only"), so a name that changed means whatever musicUrl is
          // playing right now is for the WRONG file (or there is none) -
          // drop it and ask the designer to re-pick, exactly the "yellow
          // bar" musicControl() already shows for a reload with no bytes
          // (plan §3.7). An unchanged name (re-opening the same project)
          // keeps whatever is already loaded.
          const newMusic = show.music || null;
          const oldName = project.show.music && project.show.music.name;
          const newName = newMusic && newMusic.name;
          if (oldName !== newName) {
            setMusicFile(null);
            project.show.music = newMusic;
            // A bundle naming no music at all still leaves the built-in
            // track playing, so the project should say so - the same
            // thing newProject() does, and for the same reason.
            adoptBuiltInName(project);
            const embedded = builtInMusicName();
            // "Pick the file again to hear it" is only true when there is
            // nothing else to hear (adversarial review - F3): with a
            // built-in track it flatly contradicted the music line right
            // above it, which was already saying what would play.
            if (!newName) { /* nothing was named - nothing to warn about */ }
            else if (!embedded) toast(`Music: ${newName} - pick the file again to hear it (project files never include the audio itself).`);
            else if (newName !== embedded) toast(`Music: ${newName} - this file plays the built-in track ${embedded} instead.`);
          }
        }
      }
      rebuild(); persist();
      // project.show.cues.length always, not "show.cues truthy ? its length
      // : ..." (adversarial review round 2 - F10): show.cues could be
      // anything at all (a string, "nope".length is 4) - the actual applied
      // count is whatever project.show.cues ended up holding above.
      return { ok: true, saved, cues: project.show.cues.length };
    },
    pickMusic(file) {
      if (file.size > globalThis.SIM.transport.MAX_MUSIC) { toast(`${file.name} is too large - the limit is 64 MB`); return; }
      setMusicFile(file);
      rebuild(); persist();
    },
    clearMusic() {
      setMusicFile(null);
      rebuild(); persist();
    },
    // The "Back to the built-in track" button. Same code path as
    // clearMusic() on purpose - with a built-in track present, putting the
    // picked file away IS going back to it - but a separate name, because
    // the two mean different things to a caller and only one of them is
    // offered when there is no built-in track to return to.
    useBuiltInMusic() {
      setMusicFile(null);
      rebuild(); persist();
    },
    seek(sec) { ensureTransport().seek(sec); },
    play() { ensureTransport().play(); },
    pause() { ensureTransport().pause(); },
    stop() { ensureTransport().stop(); },
  };
  // A real read-write probe, not just "does localStorage exist" (Safari in
  // private browsing DOES expose window.localStorage - every call on it
  // throws instead): the only way to know autosave will actually work is to
  // try it once, up front, so the banner shows immediately rather than
  // waiting for the first debounced autosave to fail 800ms into the
  // session.
  function localStorageWorks() {
    try {
      const probe = "az27ss.probe";
      localStorage.setItem(probe, "1");
      localStorage.removeItem(probe);
      return true;
    } catch { return false; }
  }
  function boot() {
    loadUiPrefs();
    project = adoptBuiltInName(loadProject());
    if (!localStorageWorks()) {
      autosaveWarned = true;   // the debounced autosave's own toast would be redundant
      const warn = document.getElementById("autosave-warn");
      if (warn) warn.style.display = "inline";
    }
    globalThis.SIM.app = app;
    // Wrap buildState so state.music always reflects this session's live object
    // URL (SIM.buildState itself is pure and knows nothing about object URLs):
    // `url` is whatever pressing Play will actually produce - the picked file
    // if there is one, otherwise the built-in track.
    //
    // Exactly the two keys {name, url}, and no more: selftest.js's `state`
    // cases deepEqual this whole object against tests/goldens/model.json, and
    // its `state-digest` cases hash it, so a third key here would fail the
    // golden cross-check against Python for reasons that have nothing to do
    // with the model. Which of the two sources is playing, and whether there
    // is a built-in track at all, are facts about this SESSION, not about the
    // project - musicControl() reads them straight off the module instead.
    const rawBuildState = globalThis.SIM.buildState;
    globalThis.SIM.buildState = p => {
      const s = rawBuildState(p);
      s.music = { name: p.show.music?.name || null,
                  url: p === project ? (musicUrl || builtInMusicUrl()) : null };
      return s;
    };
    wireChrome();
    wireDropZone();
    wireGlobalDragHandlers();
    wireFps();
    rebuild();
    // The transport gets the built-in track at start-up, before anything is
    // played: the first Play is the user's own click, so the autoplay policy
    // has nothing to object to, and the audio is decoded and ready by then
    // rather than at the moment the designer expects sound.
    pushMusicToTransport();
    // #displaycheck also forces the Timeline tab open (adversarial review
    // round 2 - F4): the browser-run vocabulary test needs the real
    // rendered DOM - tracks, cue table, SHORTEST INTERVAL PER ITEM, the
    // dock - not just displayCheck()'s own synthetic dirty-string fixtures,
    // and the Timeline tab is where the operator-page vocabulary (unit/
    // board/socket/...) would show up if a display override ever came
    // unwired. Done here, after rebuild(), and not alongside the
    // displayCheck() trigger itself (registered earlier, so it can and does
    // run before `state` exists) because render() needs `state`.
    if (/displaycheck/i.test(location.hash)) { displayCheck(); ui.tab = "timeline"; ui.cue = null; render(); }
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot); else boot();
})();
