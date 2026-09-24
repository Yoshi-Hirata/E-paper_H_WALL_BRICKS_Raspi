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
        if (text.length > MAX_PROJECT_BYTES) {
          if (!autosaveWarned) { autosaveWarned = true; toast("This project is too large to autosave (" + fmtSize(text.length) + ") - use Save project… to keep a copy."); }
          return;
        }
        localStorage.setItem(PROJECT_KEY, text);
      } catch {}
    }, 800);
  }
  function persist() { scheduleAutosave(); saveUiPrefs(); }

  function fmtSize(bytes) {
    const mb = bytes / (1024 * 1024);
    return mb >= 1 ? mb.toFixed(1) + " MB" : Math.max(1, Math.round(bytes / 1024)) + " KB";
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
  function renderSidebar() {
    const ordered = state.items.slice();     // already LOOK-then-name ordered by buildState
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
        `<option value="${esc(s.id)}" ${s.id === tr.sequence ? "selected" : ""}>${esc(s.label)}</option>`).join("")}</select>
      <span ${tr.sequence === "natural" ? 'style="display:none"' : ""}><input type="text" data-tr-span="${esc(name)}" size="4" value="${tr.span_s}"> s</span>`;
  }
  function renderDesigns() {
    const root = $("#content");
    const item = state.items.find(i => i.item === ui.item);
    if (!item) { root.innerHTML = `<div class="empty">Drop the garment's map and design CSV files here (or use Add CSV above) to begin.</div>`; return; }
    if (!item.map || !item.map.scales.length) {
      root.innerHTML = `<div class="card"><h2>${esc(itemFull(item))}</h2>
        <ul class="problems">${item.problems.map(p => `<li>${esc(labelize(p))}</li>`).join("") || "<li>no map CSV</li>"}</ul></div>`;
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
              ? `<ul class="problems">${problems.slice(0, 60).map(p => `<li>${esc(labelize(p))}</li>`).join("")}</ul>`
              : `<div class="okline">✓ No problems — ${item.map.scales.length} scales${design ? ` / design ${Object.keys(design.colors).length} coloured` : ""}</div>`}
            ${kind === "partial" ? `<div class="warn">A partial design: some scales are left "-" or uncoloured and keep whatever they already show. Fine as a partial cue on the Timeline.</div>` : ""}
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
  function trackItems() { return state.items.filter(i => i.map && i.map.scales.length); }
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
              `<option value="${s.id}" ${s.id === (cue.sequence || "natural") ? "selected" : ""}>${esc(s.label)}</option>`).join("")}</select>
            <span ${(cue.sequence || "natural") === "natural" ? 'style="display:none"' : ""}><input type="text" id="cue-span" value="${cue.span_s ?? 0}" size="4" ${transitionMode !== "custom" ? "disabled" : ""}> s</span></label>
        </div><div class="why"></div></div>
      <div class="cue-computed">Picture complete at ${clockShort(cue.complete)} (Start + ${refr.value.toFixed(1)} s refresh${sweep.span_s > 0 ? ` + ${sweep.span_s.toFixed(1)} s sweep` : ""})</div>
      <div class="cue-row"><div class="lbl">End</div><div class="ctl">${mmssField("cue-end", cue.end, { disabled: isLast })}</div>
        <div class="why">${isLast ? `Shown until the end of the show (${clockShort(state.show.duration)})` : "Shown until the next cue of this garment starts"}</div></div>
      <div class="cue-design"><label>Design <select id="cue-design">${(item?.designs || []).map(d =>
        `<option value="${esc(d.name)}" ${d.name === cue.design ? "selected" : ""}>${esc(designLabel(d))}</option>`).join("")}</select></label>
        <label><input type="checkbox" id="cue-partial" ${cue.partial ? "checked" : ""}> partial</label></div>
      ${(cue.problems || []).length ? `<ul class="problems">${cue.problems.map(p => `<li>${esc(labelize(p))}</li>`).join("")}</ul>` : ""}
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
        return `<div class="cue-band ${cue.id === ui.cue ? "sel" : ""}" data-cue="${cue.id}" style="left:${pct(cue.at)};width:${bandW}"></div>
          <div class="cue-hold ${bad ? "bad" : ""} ${cue.id === ui.cue ? "sel" : ""}" data-cue="${cue.id}" data-drag="${cue.at <= 0 ? "0" : "1"}"
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
        const tr = c.transition === "custom" ? (SEQ_OPTIONS().find(s => s.id === (c.sequence || "natural"))?.label || c.sequence) : "design default";
        const status = (c.problems || []).length ? `<span style="color:var(--err)">${c.problems.length} problem${c.problems.length === 1 ? "" : "s"}</span>` : '<span class="okline">OK</span>';
        return `<tr class="pick ${c.id === ui.cue ? "hl" : ""}" data-cue="${c.id}">
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
          <div class="card"><h2>CUES</h2>${cueTable()}</div>
          ${minIntervalTable()}
        </div>
        <div class="card" id="tl-editor-card"><h2>EDIT CUE</h2><div id="cue-editor-body"></div></div>
      </div>` : `<div class="empty">Add the map and design CSV files on the Designs tab first.</div>`}`;
    if (items.length) {
      wireMmss("show-duration", sec => globalThis.SIM.app.setShow({ duration: sec }));
      $("#show-refresh").onchange = e => { const v = Number(e.target.value); if (v >= 1 && v <= 60) globalThis.SIM.app.setShow({ refresh_s: v }); else e.target.value = state.show.refresh_s.toFixed(1); };
      renderCueEditor(state.show.cues.find(c => c.id === ui.cue) || null);
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
      ui.cue = Number(el.dataset.cue); render();
    }));
    document.querySelectorAll("tr[data-cue]").forEach(tr => tr.addEventListener("click", () => { ui.cue = Number(tr.dataset.cue); render(); }));
    document.querySelectorAll('.cue-hold[data-drag="1"]').forEach(el => {
      el.addEventListener("pointerdown", e => {
        e.preventDefault(); e.stopPropagation();
        const track = el.closest(".tl-track");
        const box = track.getBoundingClientRect();
        const cue = state.show.cues.find(c => c.id === Number(el.dataset.cue));
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
    const goto = $("#goto-input"); if (goto && document.activeElement !== goto) goto.value = globalThis.SIM.mmss.format(t);
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
  function renderDock() {
    ensureTransport();
    const dock = $("#tl-dock");
    dock.style.display = ui.tab === "timeline" ? "block" : "none";
    if (ui.tab !== "timeline") return;
    dock.innerHTML = `<div class="tl-dock-head">
      <span class="tl-dock-title">THE LOOKS AT <b id="tp-time"></b></span>
      <button id="tp-play">${transport.playing ? "⏸ Pause" : "▶ Play"}</button>
      <button id="tp-stop">⏹ Stop</button>
      <span class="mmss" style="margin-left:6px"><input type="text" id="goto-input" size="6"><span class="mmss-badge">mm.ss</span></span>
      <button id="goto-go">Go to</button>
      <button id="sim-view" class="${ui.simView ? "on" : ""}" style="margin-left:auto">Simulator view</button>
    </div>
    <div class="thumbs" id="tl-thumbs"></div>`;
    $("#tp-play").onclick = () => transport.toggle();
    $("#tp-stop").onclick = () => transport.stop();
    $("#goto-go").onclick = () => { const sec = globalThis.SIM.mmss.parse($("#goto-input").value); if (sec !== null) transport.seek(sec); };
    $("#sim-view").onclick = () => { ui.simView = !ui.simView; persist(); render(); };
    THUMB_VIEW.fill = ui.simView;
    fullRedrawThumbs(transport.playhead);
    wirePlayheadDrag();
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
    const pad = n => String(n).padStart(2, "0");
    const name = `az27ss-${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}-${pad(now.getHours())}${pad(now.getMinutes())}.json`;
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
      const out = $("#selftest-result");
      if (typeof globalThis.__selftest !== "function") { out.textContent = "not available in this build yet"; out.className = ""; return; }
      const r = globalThis.__selftest();
      out.textContent = r.ok ? `OK - ${r.total} check(s) passed` : `${r.failed} of ${r.total} failed`;
      out.className = r.ok ? "ok" : "fail";
    };
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
    else { $("#tl-dock").style.display = "none"; if (ui.tab === "help") renderHelp(); else renderDesigns(); }
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
  const app = {
    newProject() { project = freshProject(); musicUrl = null; ui.item = null; ui.design = null; ui.cue = null; persist(); rebuild(); },
    getProject() { return project; },
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
      const id = (project.show.cues.reduce((m, c) => Math.max(m, c.id || 0), 0) || 0) + 1;
      project.show.cues.push({ id, item, at, design, partial: !!partial, refresh_s: null, transition: "design", sequence: "natural", span_s: 0 });
      rebuild(); persist(); return id;
    },
    updateCue(id, patch) {
      const cue = project.show.cues.find(c => c.id === id); if (!cue) return;
      if ("end" in patch) {
        // "End" edits the NEXT cue's Start on the same item (or is a no-op on the last cue).
        const mine = project.show.cues.filter(c => c.item === cue.item).sort((a, b) => a.at - b.at);
        const idx = mine.findIndex(c => c.id === id);
        const next = mine[idx + 1];
        if (next) next.at = patch.end;
        delete patch.end;
      }
      Object.assign(cue, patch);
      rebuild(); persist();
    },
    deleteCue(id) { project.show.cues = project.show.cues.filter(c => c.id !== id); if (ui.cue === id) ui.cue = null; rebuild(); persist(); },
    setDesignTransition(designFile, sequence, span_s) { project.show.transitions[designFile] = { sequence, span_s }; rebuild(); persist(); },
    exportBundle() {
      const now = new Date().toISOString().replace(/\.\d+Z$/, "");
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
        if ("music" in show) project.show.music = show.music || null;
      }
      rebuild(); persist();
      return { ok: true, saved, cues: (show && show.cues) ? show.cues.length : project.show.cues.length };
    },
    pickMusic(file) {
      if (file.size > globalThis.SIM.transport.MAX_MUSIC) { toast(`${file.name} is too large - the limit is 64 MB`); return; }
      if (musicUrl) URL.revokeObjectURL(musicUrl);
      musicUrl = URL.createObjectURL(file);
      project.show.music = { name: file.name };
      rebuild(); ensureTransport().setMusic(musicUrl); persist();
    },
    clearMusic() {
      if (musicUrl) URL.revokeObjectURL(musicUrl);
      musicUrl = null; project.show.music = null;
      rebuild(); ensureTransport().setMusic(null); persist();
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
