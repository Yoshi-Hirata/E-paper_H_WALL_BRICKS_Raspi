/* model.stub.js — Coder Q's throw-away stand-in for Coder P's real model.js /
 * state.js while both are built in parallel (plan_designer_sim.md §6,
 * "Q codes strictly against §2.3 with a stub buildState").
 *
 * This file is NOT part of the shipped build: tools/build_designer.py never
 * inlines it, and conductor/web/designer.html only loads it when
 * SIM.buildState is not already defined (see the tiny loader script at the
 * bottom of designer.html). Delete this file's <script> tag the day P's
 * model.js/state.js land on main; nothing else here should have to change,
 * because everything downstream of buildState() only reads SIM.* per the
 * frozen §2.3 shapes.
 *
 * It fakes just enough of SIM.fmt / SIM.mmss / SIM.look / SIM.buildState to
 * drive render.js/flicker.js/looks.js/transport.js/designer-app.js during
 * development. It does NOT reproduce Python's half-to-even rounding, exact
 * problem-string wording, or ordering edge cases - none of that is graded
 * against this file.
 */
(function () {
  "use strict";

  const fmt = {
    round(x, n) { const p = Math.pow(10, n || 0); return Math.round(x * p) / p; },
    roundInt(x) { return Math.round(x); },
    fixed(x, n) { return x.toFixed(n); },
    g(x) { return String(x); },
    canonical(v) { return JSON.stringify(v); },
    digest64(text) {
      // Not FNV-1a, just stable enough for stub round-trip checks.
      let h = 0n;
      for (let i = 0; i < text.length; i++) h = (h * 33n + BigInt(text.charCodeAt(i))) & 0xffffffffffffffffn;
      return h.toString(16).padStart(16, "0");
    },
  };

  const mmss = {
    parse(text) {
      const m = String(text).trim().match(/^\s*(\d{1,3})(?:[.:](\d{1,2}))?\s*$/);
      if (!m) return null;
      const min = Number(m[1]);
      let sec = 0;
      if (m[2] !== undefined) sec = m[2].length === 1 ? Number(m[2]) : Number(m[2]);
      if (sec >= 60) return null;
      return min * 60 + sec;
    },
    format(sec) {
      const n = fmt.roundInt(Math.max(0, sec));
      const m = Math.floor(n / 60), s = n % 60;
      return m + "." + String(s).padStart(2, "0");
    },
    human(sec) {
      const n = fmt.roundInt(Math.max(0, sec));
      const m = Math.floor(n / 60), s = n % 60;
      return (m ? m + " min " : "") + String(s).padStart(2, "0") + " s";
    },
  };

  const IS_GRID = /_color_.+grid/i;
  const IS_MAP = /_map$/i;
  const MAP_ITEM = /(.+?)_map/i;

  const look = {
    PALETTE: Array.from({ length: 16 }, (_, i) => ({ name: "colour " + i, rgb: [i * 16 % 256, (i * 47) % 256, (i * 91) % 256] })),
    ARRAY_LEN: 64, COLOR_COUNT: 16, MAX_BOARDS: 60,
    defaultShift(row) { return row % 2 ? 0.5 : 0; },
    kind(filename) {
      const stem = filename.replace(/\.csv$/i, "");
      if (!/\.csv$/i.test(filename)) return null;
      if (IS_GRID.test(stem)) return "grid";
      if (IS_MAP.test(stem)) return "map";
      return null;
    },
    nameParts(filename) {
      const stem = filename.replace(/\.csv$/i, "");
      const mapM = stem.match(MAP_ITEM);
      if (mapM) return { item: mapM[1], pattern: null, label: mapM[1] };
      const gridM = stem.match(/^(.+?)_color_(.+?)_grid/i);
      if (gridM) return { item: gridM[1], pattern: null, label: gridM[2] };
      return { item: stem, pattern: null, label: stem };
    },
    mapItem(filename) { return look.nameParts(filename).item; },
  };

  function parseCsv(text) {
    return text.replace(/\r\n?/g, "\n").split("\n").filter(l => l.length).map(l => l.split(","));
  }

  function parseMapCsv(text) {
    const rows = parseCsv(text);
    const header = rows[0].map(h => h.trim().toLowerCase());
    const idx = name => header.indexOf(name);
    const scales = [], shifts = {}, sidesSeen = [];
    for (let i = 1; i < rows.length; i++) {
      const r = rows[i];
      const side = r[idx("side")].trim();
      const row = Number(r[idx("row")]);
      const col = Number(r[idx("col")]);
      const boardNo = Number(r[idx("board_no")]);
      const socket = Number(r[idx("socket")]);
      if (!sidesSeen.includes(side)) sidesSeen.push(side);
      scales.push([side, row, col, boardNo, socket]);
      const si = idx("shift");
      if (si >= 0 && r[si] !== undefined && r[si] !== "") {
        const v = Number(r[si]);
        const key = side + "|" + row;
        if (!(key in shifts) && v !== look.defaultShift(row)) shifts[key] = v;
      }
    }
    return { scales, shifts, sides: sidesSeen };
  }

  function parseGridCsv(text) {
    const rows = parseCsv(text);
    const header = rows[0].map(h => h.trim());
    const cols = header.slice(3).map(Number);
    const colors = {}, undecided = [];
    for (let i = 1; i < rows.length; i++) {
      const r = rows[i];
      const side = r[0].trim(), row = Number(r[1]);
      for (let c = 0; c < cols.length; c++) {
        const cell = (r[3 + c] || "0").trim();
        const key = side + "|" + row + "|" + cols[c];
        if (cell === "-") undecided.push(key);
        else if (cell !== "0" && cell !== "") colors[key] = parseInt(cell, 16);
      }
    }
    return { colors, undecided };
  }

  function buildState(project) {
    const show = Object.assign({ duration: 600, refresh_s: 7, cues: [], transitions: {}, labels: {}, boards: {}, music: null }, project.show || {});
    const items = {};
    const orphans = [];
    const fileNames = Object.keys(project.files || {}).sort();

    for (const name of fileNames) {
      if (look.kind(name) !== "map") continue;
      const item = look.nameParts(name).item;
      let parsed, problems = [];
      try { parsed = parseMapCsv(project.files[name]); }
      catch (e) { parsed = { scales: [], shifts: {}, sides: [] }; problems = [String(e)]; }
      const label = show.labels[item] || {};
      items[item] = {
        item, unit: null, look: String(label.look || ""), model: String(label.model || ""),
        map: { name, scales: parsed.scales, shifts: parsed.shifts, sides: parsed.sides, warnings: [] },
        designs: [], problems, boards: [], sequences: { natural: parsed.scales.map(() => 0) },
      };
    }
    for (const name of fileNames) {
      if (look.kind(name) !== "grid") continue;
      const np = look.nameParts(name);
      const g = parseGridCsv(project.files[name]);
      const record = {
        name, pattern: np.pattern, label: np.label, problems: [], partial_problems: [],
        colors: g.colors, shifts: {}, undecided: g.undecided,
        transition: (show.transitions[name]) || { sequence: "natural", span_s: 0 },
      };
      if (items[np.item]) items[np.item].designs.push(record);
      else orphans.push(record);
    }
    for (const item of Object.values(items)) {
      item.designs.sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
    }
    const ordered = Object.values(items).sort((a, b) => {
      const an = a.look ? Number(a.look) : NaN, bn = b.look ? Number(b.look) : NaN;
      const aNum = Number.isFinite(an), bNum = Number.isFinite(bn);
      if (aNum && bNum && an !== bn) return an - bn;
      if (aNum !== bNum) return aNum ? -1 : 1;
      return a.item < b.item ? -1 : a.item > b.item ? 1 : 0;
    });

    const cues = (show.cues || []).map(c => Object.assign({}, c)).sort((a, b) => a.at - b.at);
    const byItem = {};
    for (const c of cues) (byItem[c.item] = byItem[c.item] || []).push(c);
    const minInterval = {};
    for (const item of ordered) {
      const mine = (byItem[item.item] || []).sort((a, b) => a.at - b.at);
      for (let i = 0; i < mine.length; i++) {
        const cue = mine[i];
        const refresh = cue.refresh_s != null ? cue.refresh_s : show.refresh_s;
        cue.sent = cue.at;
        cue.complete = cue.at + refresh + (cue.span_s || 0);
        const next = mine[i + 1];
        cue.end = next ? next.at : show.duration;
        cue.end_source = next ? "next" : "show";
        cue.refresh = refresh;
        cue.refresh_source = cue.refresh_s != null ? "cue" : "show";
        cue.problems = [];
      }
      minInterval["(" + item.item + ")"] = mine.length > 1 ? Math.min(...mine.slice(1).map((c, i) => c.at - mine[i].at)) : null;
    }

    return {
      show: { duration: show.duration, refresh_s: show.refresh_s, cues, warnings: [], min_interval: minInterval },
      history: { undo: 0, redo: 0 },
      units: [], items: ordered, orphans,
      // ids/labels match conductor/sequence.py's SEQUENCES/LABELS exactly (not
      // approximated): the designer's Transition dropdown reads these from
      // state.sequences the same way the operator page does.
      sequences: [{ id: "natural", label: "Socket order (P01 to P60)" }, { id: "center", label: "Centre outward" },
                  { id: "top_down", label: "Top to bottom" }, { id: "bottom_up", label: "Bottom to top" },
                  { id: "left_right", label: "Left to right (audience)" }, { id: "right_left", label: "Right to left (audience)" }],
      palette: look.PALETTE,
      music: project.music || { name: null, url: null },
    };
  }

  globalThis.SIM = Object.assign(globalThis.SIM || {}, {
    fmt, mmss, look,
    buildState,
    timeSweeps() {}, cleanTransitions(raw) { return raw || {}; },
    designTransition(entry) { return entry || { sequence: "natural", span_s: 0 }; },
  });
})();
