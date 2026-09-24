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
  let autosaveWarned = false;
  let autosaveTimer = null;

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

  function loadProject() {
    try {
      const raw = localStorage.getItem(PROJECT_KEY);
      if (raw) { const p = JSON.parse(raw); if (p && p.files && p.show) return p; }
    } catch {}
    return cloneStarter();          // first ever open (empty localStorage), or a corrupt value
  }
  function scheduleAutosave() {
    clearTimeout(autosaveTimer);
    autosaveTimer = setTimeout(() => {
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

  function rebuild() {
    try { state = globalThis.SIM.buildState(project); }
    catch (e) { console.error("buildState failed", e); toast("Internal error building the project - see the console."); return; }
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

  // ---- display-only rewrite of the model's bus fallback name (plan §1.3/§3.6):
  // the model always calls an unassigned item's bus "(<item>)"; the UI shows
  // "LOOK n" (or the bare item name with no LOOK yet) instead, everywhere.
  function unitLabel(key) {
    const m = /^\((.+)\)$/.exec(key);
    if (!m) return key;
    const it = state.items.find(i => i.item.toLowerCase() === m[1].toLowerCase());
    return it && it.look ? "LOOK " + it.look : m[1];
  }
  function labelize(msg) {
    return String(msg).replace(/ on \(([^)]+)\)/g, (m, name) => {
      const it = state.items.find(i => i.item.toLowerCase() === name.toLowerCase());
      return " on " + (it && it.look ? "LOOK " + it.look : name);
    });
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
      .replace(/\bunits\b/gi, "garments").replace(/\bunit\b/gi, "garment")
      .replace(/\bboards\b/gi, "segments").replace(/\bboard\b/gi, "segment")
      .replace(/\bsockets\b/gi, "positions").replace(/\bsocket\b/gi, "position")
      .replace(/\bbus(es)?\b/gi, "shared line")
      .replace(/\bdip\b/gi, "").replace(/\bradxa\b/gi, "controller")
      .replace(/[ \t]{2,}/g, " ").trim();
  }
  const clean = msg => deJargon(labelize(msg));
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
    const banned = /\b(unit|units|radxa|bus|buses|board|boards|dip|socket|sockets)\b/i;
    const failures = [];
    for (const msg of dirty) {
      const got = deJargon(msg);
      if (banned.test(got)) failures.push(`deJargon(${JSON.stringify(msg)}) -> ${JSON.stringify(got)} still has banned vocabulary`);
    }
    const seqs = (globalThis.SIM.sequence && globalThis.SIM.sequence.LABELS) || {};
    Object.keys(seqs).forEach(id => {
      const got = seqLabel({ id, label: seqs[id] });
      if (banned.test(got)) failures.push(`seqLabel(${id}) -> ${JSON.stringify(got)} still has banned vocabulary`);
    });
    const result = { ok: failures.length === 0, total: dirty.length + Object.keys(seqs).length, failed: failures.length, failures };
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
  globalThis.__displaycheck = displayCheck;
  if (typeof document !== "undefined" && /#displaycheck/.test(location.hash)) {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", displayCheck);
    else displayCheck();
  }
  const itemName = i => !i ? "" : i.look ? "LOOK " + i.look : i.item;
  const itemFull = i => !i ? "" : [i.look ? "LOOK " + i.look : "", i.model].filter(Boolean).join(" · ") || i.item;
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
        <input class="lab look" data-label="look" value="${esc(item.look)}" placeholder="–" maxlength="12" title="LOOK number (the only ordering control)">
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
  function wireMmss(id, onChange) {
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
      else { input.value = input.defaultValue; paint(); }   // revert AND re-paint, so the red/bad state clears with it
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
    if (!item) { root.innerHTML = `<div class="empty">Drop the garment's map and design CSV files here (or use Add CSV above) to begin.</div>`; return; }
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
  const ctx = () => ({ cuesOf, palette: state.palette });

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
    const idx = cuesOnItem.findIndex(c => c.id === cue.id);
    const isLast = idx === cuesOnItem.length - 1;
    card.innerHTML = `<div class="cue-card">
      <div class="cue-head"><b>${esc(itemFull(item) || cue.item)}</b>
        <span><button id="cue-del" class="danger">Delete</button></span></div>
      <div class="cue-row"><div class="lbl">Start</div><div class="ctl">${mmssField("cue-start", cue.at, { disabled: isPreset })}</div>
        <div class="why">${isPreset ? "Preset: shown before the show starts" : "The e-paper starts refreshing here"}</div></div>
      <div class="cue-row"><div class="lbl">Refresh</div>
        <div class="ctl"><label class="rad"><input type="radio" name="cue-refresh-mode" id="cue-refresh-show" ${refr.source === "show" ? "checked" : ""}> show default (${state.show.refresh_s.toFixed(1)} s)</label>
          <label class="rad"><input type="radio" name="cue-refresh-mode" id="cue-refresh-cue" ${refr.source === "cue" ? "checked" : ""}> this cue</label>
          <input type="text" id="cue-refresh" value="${refr.value.toFixed(1)}" ${refr.source === "cue" ? "" : "disabled"}> s</div>
        <div class="why">How long this garment's e-paper takes to redraw</div></div>
      <div class="cue-row"><div class="lbl">Transition</div>
        <div class="ctl" style="flex-direction:column;align-items:flex-start;gap:6px">
          <label class="rad"><input type="radio" name="cue-transition" id="cue-transition-design" ${transitionMode === "design" ? "checked" : ""}> this design (every cue wearing it)
            ${design ? `<span style="display:inline-flex;gap:8px;align-items:center;margin-left:4px">${transitionControl(cue.design, designTr)}</span>` : ""}</label>
          <label class="rad"><input type="radio" name="cue-transition" id="cue-transition-custom" ${transitionMode === "custom" ? "checked" : ""}> this cue only
            <select id="cue-seq" ${transitionMode !== "custom" ? "disabled" : ""}>${SEQ_OPTIONS().map(s =>
              `<option value="${s.id}" ${s.id === (cue.sequence || "natural") ? "selected" : ""}>${esc(seqLabel(s))}</option>`).join("")}</select>
            <span ${(cue.sequence || "natural") === "natural" ? 'style="display:none"' : ""}><input type="text" id="cue-span" value="${cue.span_s ?? 0}" size="4" ${transitionMode !== "custom" ? "disabled" : ""}> s</span></label>
        </div><div class="why"></div></div>
      <div class="cue-computed">Picture complete at ${clockShort(cue.complete)} (Start + ${refr.value.toFixed(1)} s refresh${sweep.span_s > 0 ? ` + ${sweep.span_s.toFixed(1)} s sweep` : ""})</div>
      <div class="cue-row"><div class="lbl">End</div><div class="ctl">${mmssField("cue-end", cue.end, { disabled: isLast })}</div>
        <div class="why">${isLast ? `Shown until the end of the show (${clockShort(state.show.duration)})` : "Shown until the next cue of this garment starts"}</div></div>
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
        return `<div class="cue-band ${sameId(cue.id, ui.cue) ? "sel" : ""}" data-cue="${cue.id}" style="left:${pct(cue.at)};width:${bandW}"></div>
          <div class="cue-hold ${bad ? "bad" : ""} ${sameId(cue.id, ui.cue) ? "sel" : ""}" data-cue="${cue.id}" data-drag="${cue.at <= 0 ? "0" : "1"}"
            style="left:${holdX};width:${holdW}" title="${esc(designLabel(item.designs.find(d => d.name === cue.design) || { name: cue.design }))}">${cue.at <= 0 ? "PRESET · " : ""}${esc(designLabel(item.designs.find(d => d.name === cue.design) || { name: cue.design }))}</div>`;
      }).join("");
      return `<div class="tl-row"><div class="tl-name">${esc(itemName(item))}<small>${esc(item.model || "")}</small></div>
        <div class="tl-track" data-track="${esc(item.item)}">${bands}</div></div>`;
    }).join("");
    return `<div class="tl-ruler" id="ruler">${rulerTicks(D)}</div><div class="tl-wrap">${rows || `<div class="empty">No garments yet.</div>`}
      <div id="playhead"></div><div id="ph-head"><svg width="14" height="10"><polygon points="0,0 14,0 7,10" fill="var(--err)"/></svg><span id="ph-time"></span></div></div>`;
  }
  function rulerTicks(D) {
    const step = D > 900 ? 60 : D > 300 ? 30 : 10;
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
    return `<div class="card"><h2>SHORTEST INTERVAL PER GARMENT</h2><table><tbody>
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
        return `<tr class="pick ${sameId(c.id, ui.cue) ? "hl" : ""}" data-cue="${c.id}">
          <td>${clockShort(c.at)}</td><td>${clockShort(c.complete)}</td><td>${clockShort(c.end)}</td>
          <td>${esc(itemName(item))}</td><td>${esc(designLabel(item?.designs.find(d => d.name === c.design) || { name: c.design }))}${c.partial ? " (partial)" : ""}</td>
          <td>${esc(tr)}</td><td>${status}</td></tr>`;
      }).join("")}</tbody></table>`;
  }
  function musicControl() {
    const m = state.music || { name: null, url: null };
    if (!m.name) return `<div class="group"><span>Music</span><label class="filebtn">Pick file…<input id="music-pick" type="file" accept="audio/*"></label></div>`;
    const missing = !m.url;
    return `<div class="group"><span>Music</span><span class="meta">${esc(m.name)}${missing ? ' <span class="warn">(re-pick the file: it is not kept between reloads)</span>' : ""}</span>
      <label class="filebtn">Pick file…<input id="music-pick" type="file" accept="audio/*"></label>
      <button id="music-clear">Remove</button></div>`;
  }
  function renderTimelineTab() {
    ensureTransport();
    const root = $("#content");
    const items = trackItems();
    root.innerHTML = `<div class="toolbar">
        <div class="group"><span>Show length</span>${mmssField("show-duration", state.show.duration)}</div>
        <div class="group"><span>Default refresh time</span><input type="text" id="show-refresh" size="4" value="${state.show.refresh_s.toFixed(1)}"> s</div>
        <div class="group"><button id="save-project">Save project…</button><label class="filebtn">Open project…<input id="open-project" type="file" accept=".json"></label></div>
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
      wireMmss("show-duration", sec => globalThis.SIM.app.setShow({ duration: sec }));
      $("#show-refresh").onchange = e => { const v = Number(e.target.value); if (v >= 1 && v <= 60) globalThis.SIM.app.setShow({ refresh_s: v }); else e.target.value = state.show.refresh_s.toFixed(1); };
      renderCueEditor(state.show.cues.find(c => sameId(c.id, ui.cue)) || null);
      wireTrackEvents();
    }
    $("#save-project").onclick = saveProjectFile;
    $("#open-project").onchange = e => { if (e.target.files[0]) openProjectFile(e.target.files[0]); e.target.value = ""; };
    const mp = $("#music-pick"); if (mp) mp.onchange = e => { if (e.target.files[0]) globalThis.SIM.app.pickMusic(e.target.files[0]); e.target.value = ""; };
    const mc = $("#music-clear"); if (mc) mc.onclick = () => globalThis.SIM.app.clearMusic();
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
        const marker = document.querySelector(`.cue-hold[data-cue="${cueDrag.id}"]`);
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
        const id = globalThis.SIM.app.addCue({ item: item.item, at: t, design: design ? design.name : null, partial: false });
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
    wireMmss("goto-input", sec => transport.seek(sec));
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
      <h2>開き方</h2><p>このファイル（<code>az27ss-simulator.html</code> または <code>designer.html</code>）をダブルクリックするだけで開きます。インストールもサーバーも不要です。Windows は Edge か Chrome、macOS は Safari か Chrome を推奨します。</p>
      <h2>CSV の入れ方</h2><p>マップCSV（<code>*_map.csv</code>）とデザインCSV（<code>*_color_名前_grid.csv</code>）を、このページのどこにでもドラッグ＆ドロップしてください（フォルダごとも可）。ヘッダーの「Add CSV」ボタンでも選べます。同じ名前のファイルは上書きされます。</p>
      <h2>mm.ss の読み方</h2><p>開始・終了・ショー全体の長さなど「時刻」は分.秒（mm.ss）で入力します。例：<code>3.05</code> → 3分05秒。<code>3.5</code> のように秒が1桁でも「3分05秒」として読みます。入力欄の横に読み方がそのまま表示されます（例：「3 min 05 s」）。60秒以上は無効（赤色）になります。</p>
      <h2>遷移（トランジション）6種</h2><p>各デザインの塗り替え方向を選べます：既定（配線どおり、変更なし）、Top to bottom（上から下）、Bottom to top（下から上）、Left to right（左から右）、Right to left（右から左）、Centre outward（中心から外へ）。「秒」は最初の一列が変わってから最後の一列が変わるまでの時間です。</p>
      <h2>保存と受け渡し</h2><p>「Save project…」でこのブラウザ内のプロジェクト全体（CSVとタイムライン）を1つのJSONファイルに書き出します。「Open project…」で読み込みます。オペレーター側の「Load bundle…」に同じファイルを渡すと、ユニットの割り当てはそのままに、CSVとタイムラインだけが更新されます。</p>
      <h2>制限</h2><p>自動保存はブラウザに約4MBまで。音楽ファイルは64MBまで、名前だけ覚えていて再読み込み後は音源ファイルを選び直してください（プロジェクトファイルには音は含まれません）。保存ファイルは6MBを超えると警告、8MBを超えると保存を拒否します。</p>
      <h2>自己テスト</h2><p><button id="run-selftest">Run self-test</button> <span id="selftest-result"></span></p>
      <h2>連絡先</h2><p>不具合や質問は ${esc("y.hirata@r2-engineering.com")} まで。</p>
    </div>`;
    $("#run-selftest").onclick = () => {
      if (typeof globalThis.__selftest !== "function") { const out = $("#selftest-result"); out.textContent = "not available in this build yet"; out.className = ""; return; }
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
    const savedMusicUrl = musicUrl;
    const savedUi = Object.assign({}, ui);
    let result;
    try {
      result = globalThis.__selftest();
    } finally {
      Object.assign(ui, savedUi);
      app.setProject(savedProject);
      musicUrl = savedMusicUrl;
      ensureTransport().setMusic(savedMusicUrl);
    }
    return result;
  }

  // ==================================================================
  // CSV drops / Add CSV (plan §3.2)
  // ==================================================================
  function readFileAsText(file) { return new Promise((res, rej) => { const r = new FileReader(); r.onload = () => res(r.result); r.onerror = rej; r.readAsText(file); }); }
  async function addFilesFromBlobs(files) {
    const list = [];
    for (const f of files) { if (/\.csv$/i.test(f.name)) { try { list.push({ name: f.name, text: await readFileAsText(f) }); } catch {} } }
    if (!list.length) return;
    const result = globalThis.SIM.app.addFiles(list);
    const parts = [];
    if (result.saved.length) parts.push(`${result.saved.length} file(s) added`);
    if (result.refused.length) parts.push(`${result.refused.length} refused: ` + result.refused.map(r => `${r.name} (${r.error})`).join("; "));
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
  function render() {
    document.querySelectorAll("[data-tab]").forEach(b => b.classList.toggle("on", b.dataset.tab === ui.tab));
    renderSidebar();
    if (ui.tab === "timeline") renderTimelineTab();
    else { $("#tl-dock").style.display = "none"; syncDockHeight(); if (ui.tab === "help") renderHelp(); else renderDesigns(); }
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
  function refuseReason(name) { return `not a *_map.csv or *_color_NAME_grid.csv`; }
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
  function setMusicFile(file) {
    if (musicUrl) { URL.revokeObjectURL(musicUrl); musicUrl = null; }
    musicUrl = file ? URL.createObjectURL(file) : null;
    project.show.music = file ? { name: file.name } : null;
    ensureTransport().setMusic(musicUrl);
  }
  const app = {
    newProject() {
      setMusicFile(null);
      project = freshProject(); ui.item = null; ui.design = null; ui.cue = null; persist(); rebuild();
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
        const clean = String(name).replace(/[\\/]/g, "_");
        if (globalThis.SIM.look.kind(clean) === null) { refused.push({ name: clean, error: refuseReason(clean) }); continue; }
        project.files[clean] = text.replace(/\r\n?/g, "\n");
        saved.push(clean);
      }
      rebuild(); persist();
      return { saved, refused };
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
        if ("duration" in show) project.show.duration = Number(show.duration) || project.show.duration;
        if ("refresh_s" in show) project.show.refresh_s = Number(show.refresh_s) || project.show.refresh_s;
        if ("cues" in show && Array.isArray(show.cues)) project.show.cues = show.cues.map(c => Object.assign({}, c));
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
            if (newName) toast(`Music: ${newName} - pick the file again to hear it (project files never include the audio itself).`);
          }
        }
      }
      rebuild(); persist();
      return { ok: true, saved, cues: (show && show.cues) ? show.cues.length : project.show.cues.length };
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
    seek(sec) { ensureTransport().seek(sec); },
    play() { ensureTransport().play(); },
    pause() { ensureTransport().pause(); },
    stop() { ensureTransport().stop(); },
  };
  function boot() {
    loadUiPrefs();
    project = loadProject();
    globalThis.SIM.app = app;
    // Wrap buildState so state.music always reflects this session's live object
    // URL (SIM.buildState itself is pure and knows nothing about object URLs).
    const rawBuildState = globalThis.SIM.buildState;
    globalThis.SIM.buildState = p => { const s = rawBuildState(p); s.music = { name: p.show.music?.name || null, url: p === project ? musicUrl : null }; return s; };
    wireChrome();
    wireDropZone();
    wireGlobalDragHandlers();
    wireFps();
    rebuild();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot); else boot();
})();
