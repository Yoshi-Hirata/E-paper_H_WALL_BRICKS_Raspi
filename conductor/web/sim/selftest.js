/*
 * conductor/web/sim/selftest.js
 *
 * window.__selftest() runs the whole cross-check (SIM.GOLDENS, written
 * by tools/make_goldens.py from the SAME fixtures and conductor/*.py)
 * against this page's own SIM implementation, synchronously, and
 * writes a <pre id="selftest-out"> the pytest side reads back:
 *
 *   <pre id="selftest-out" data-ok="true" data-total="N" data-failed="0">
 *
 * Auto-runs when the URL ends in "#selftest" (also wired to a "Run
 * self-test" button in Q's Help tab). Requires model.js, state.js and
 * goldens.js to have already run.
 *
 * The app smoke case (SIM.app: starter files -> a few cues ->
 * exportBundle -> newProject -> openBundle -> equal digests) only runs
 * when SIM.app exists - Q's designer-app.js is a separate file, and
 * P's own branch/tests must pass standalone before it lands (plan
 * section 6: "keeps your branch testable standalone").
 *
 * Owned by Coder P.
 */
(function () {
  "use strict";

  function deepEqual(a, b) {
    if (a === b) return true;
    if (typeof a === "number" && typeof b === "number" && Number.isNaN(a) && Number.isNaN(b)) return true;
    if (a === null || b === null || a === undefined || b === undefined) return a === b;
    if (typeof a !== typeof b) return false;
    if (Array.isArray(a) || Array.isArray(b)) {
      if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false;
      for (let i = 0; i < a.length; i++) if (!deepEqual(a[i], b[i])) return false;
      return true;
    }
    if (typeof a === "object") {
      const ak = Object.keys(a).sort();
      const bk = Object.keys(b).sort();
      if (ak.length !== bk.length) return false;
      for (let i = 0; i < ak.length; i++) if (ak[i] !== bk[i]) return false;
      for (const k of ak) if (!deepEqual(a[k], b[k])) return false;
      return true;
    }
    return false;
  }

  function short(v) {
    try {
      const s = JSON.stringify(v);
      return s.length > 300 ? s.slice(0, 300) + "..." : s;
    } catch (e) {
      return String(v);
    }
  }

  function mapSummary(map) {
    return {
      name: map.name, item: map.item, warnings: map.warnings, shifts: map.shifts,
      sides: map.sides, boardNos: map.boardNos,
      scales: map.scales.map(s => [s.side, s.row, s.col, s.board_no, s.socket, s.label]),
    };
  }

  function designSummary(design) {
    return {
      name: design.name, item: design.item, pattern: design.pattern, label: design.label,
      colors: design.colors, shifts: design.shifts, undecided: design.undecided.slice().sort(),
      cols: design.cols,
    };
  }

  function reconstructCue(raw) {
    const cleaned = SIM.timeline.clean([{
      id: raw.id, item: raw.item, at: raw.at, design: raw.design,
      partial: raw.partial, refresh_s: raw.refresh_s,
    }])[0];
    if (raw.sweep !== null && raw.sweep !== undefined) cleaned.sweep = raw.sweep;
    if (raw.span !== null && raw.span !== undefined) cleaned.span = raw.span;
    return cleaned;
  }

  const RUNNERS = {
    canonical(c) {
      let got;
      try {
        got = SIM.fmt.canonical(c.value);
      } catch (e) {
        return `canonical(${short(c.value)}) threw ${e && e.message ? e.message : e}`;
      }
      if (got !== c.expectCanonical) {
        return `canonical(${short(c.value)}) got ${short(got)} want ${short(c.expectCanonical)}`;
      }
      const digest = SIM.fmt.digest64(got);
      return digest === c.expectDigest ? null
        : `digest64(canonical(${short(c.value)})) got ${digest} want ${c.expectDigest}`;
    },

    fmt(c) {
      const fmt = SIM.fmt;
      let got;
      if (c.op === "fixed") got = fmt.fixed(c.x, c.n);
      else if (c.op === "round") got = fmt.round(c.x, c.n);
      else if (c.op === "roundInt") got = fmt.roundInt(c.x);
      else if (c.op === "g") got = fmt.g(c.x);
      else return `fmt: unknown op ${c.op}`;
      return deepEqual(got, c.expect) ? null : `fmt.${c.op}(${c.x},${c.n}) got ${short(got)} want ${short(c.expect)}`;
    },

    clock(c) {
      const tl = SIM.timeline;
      if (c.op === "parseClock") {
        let got, threw = false;
        try { got = tl.parseClock(c.input); } catch (e) { threw = true; }
        if (c.error) return threw ? null : `parseClock(${short(c.input)}) should have thrown, got ${short(got)}`;
        if (threw) return `parseClock(${short(c.input)}) threw unexpectedly`;
        return deepEqual(got, c.expect) ? null : `parseClock(${short(c.input)}) got ${short(got)} want ${short(c.expect)}`;
      }
      if (c.op === "formatClock") {
        const got = tl.formatClock(c.input);
        return deepEqual(got, c.expect) ? null : `formatClock(${c.input}) got ${short(got)} want ${short(c.expect)}`;
      }
      return `clock: unknown op ${c.op}`;
    },

    mmss(c) {
      const mmss = SIM.mmss;
      let got;
      if (c.op === "parse") got = mmss.parse(c.input);
      else if (c.op === "format") got = mmss.format(c.input);
      else if (c.op === "human") got = mmss.human(c.input);
      else return `mmss: unknown op ${c.op}`;
      return deepEqual(got, c.expect) ? null : `mmss.${c.op}(${short(c.input)}) got ${short(got)} want ${short(c.expect)}`;
    },

    // look.kind()/nameParts(): the file-name grammar, including the
    // production site's own <item>_<配色案名>_HW.csv (2026-09-26). Both
    // sides must read a dropped file's name the same way, or the
    // simulator and the Conductor disagree on which garment it belongs to.
    names(c) {
      const got = { kind: SIM.look.kind(c.filename),
                    parts: SIM.look.nameParts(c.filename, c.items || undefined) };
      return deepEqual(got, c.expect) ? null
        : `names ${c.filename} got ${short(got)} want ${short(c.expect)}`;
    },

    map(c, fixtures) {
      const text = fixtures[c.fixture];
      if (text === undefined) return `map ${c.fixture}: fixture text missing`;
      const result = SIM.look.parseMap(text, c.opts);
      const got = result.ok ? { ok: true, map: mapSummary(result.map) } : { ok: false, problems: result.problems };
      return deepEqual(got, c.expect) ? null : `map ${c.fixture} got ${short(got)} want ${short(c.expect)}`;
    },

    design(c, fixtures) {
      const text = fixtures[c.fixture];
      if (text === undefined) return `design ${c.fixture}: fixture text missing`;
      const result = SIM.look.parseDesign(text, c.opts);
      if (result.ok) result.design.label = SIM.look.nameParts(c.fixture)[2];   // as Design.from_csv/state.js set it
      const got = result.ok ? { ok: true, design: designSummary(result.design) } : { ok: false, problems: result.problems };
      return deepEqual(got, c.expect) ? null : `design ${c.fixture} got ${short(got)} want ${short(c.expect)}`;
    },

    check(c, fixtures) {
      const mapText = fixtures[c.mapFixture];
      const designText = fixtures[c.designFixture];
      if (mapText === undefined || designText === undefined) return `check ${c.mapFixture}/${c.designFixture}: fixture text missing`;
      const mapResult = SIM.look.parseMap(mapText, c.mapOpts);
      const designResult = SIM.look.parseDesign(designText, c.designOpts);
      if (!mapResult.ok || !designResult.ok) return `check ${c.mapFixture}/${c.designFixture}: fixture did not parse`;
      const map = mapResult.map, design = designResult.design;
      const results = c.calls.map(partial => SIM.look.check(map, design, partial));
      const got = { results, warningsAfter: map.warnings };
      return deepEqual(got, c.expect) ? null : `check ${c.mapFixture}/${c.designFixture} got ${short(got)} want ${short(c.expect)}`;
    },

    ranks(c, fixtures) {
      const text = fixtures[c.mapFixture];
      if (text === undefined) return `ranks ${c.mapFixture}: fixture text missing`;
      const mapResult = SIM.look.parseMap(text, c.opts || { name: c.mapFixture });
      if (!mapResult.ok) return `ranks ${c.mapFixture}: fixture did not parse`;
      const map = mapResult.map;
      const ranked = SIM.sequence.ranksByKey(map, c.sequence);
      const spanS = SIM.sequence.spanS(map, c.sequence, c.span);
      const got = { ranks: ranked, spanS };
      return deepEqual(got, c.expect) ? null : `ranks ${c.mapFixture}/${c.sequence}/${c.span} got ${short(got)} want ${short(c.expect)}`;
    },

    timeline(c) {
      const tl = SIM.timeline;
      const cues = c.cues.map(reconstructCue);
      const v = tl.validate(cues, c.items, c.duration, c.refresh, c.gap);
      const ends = tl.ends(cues, c.refresh, c.duration);
      const unitBoards = {};
      Object.keys(c.items).forEach(k => {
        const fact = c.items[k];
        const bus = fact.unit || `(${fact.item})`;
        unitBoards[bus] = (unitBoards[bus] || 0) + fact.boards;
      });
      const outCues = cues.map(cue => {
        const t = tl.times(cue, c.refresh);
        const end = ends[cue.id];
        return {
          id: cue.id, sent: t[0], complete: t[1],
          refresh: tl.effectiveRefresh(cue, c.refresh),
          refresh_source: (typeof cue.refresh_s === "number" && !Number.isNaN(cue.refresh_s)) ? "cue" : "show",
          end: end[0], end_source: end[1],
        };
      });
      const minInterval = {};
      Object.keys(unitBoards).forEach(u => { minInterval[u] = tl.minInterval(unitBoards[u], c.refresh); });
      const got = { problems: v.problems, warnings: v.warnings, cues: outCues, minInterval };
      return deepEqual(got, c.expect) ? null : `timeline ${c.name} got ${short(got)} want ${short(c.expect)}`;
    },

    state(c) {
      const got = SIM.buildState(c.project);
      return deepEqual(got, c.expect) ? null : `state ${c.name} got ${short(got)} want ${short(c.expect)}`;
    },

    "state-digest": function (c) {
      const state = SIM.buildState(c.project);
      const got = SIM.fmt.digest64(SIM.fmt.canonical(state));
      return got === c.expect ? null : `state-digest ${c.name} got ${got} want ${c.expect}`;
    },
  };

  function runAppSmoke(failures) {
    if (typeof SIM.app === "undefined") return;      // Q's module: optional here
    try {
      SIM.app.newProject();
      const starter = (globalThis.SIM.STARTER && globalThis.SIM.STARTER.files) || {};
      const added = SIM.app.addFiles(Object.keys(starter).map(name => ({ name, text: starter[name] })));
      if (added && added.refused && added.refused.length) {
        failures.push(`app smoke: starter files refused: ${short(added.refused)}`);
      }
      const before = SIM.app.getState ? SIM.app.getState() : SIM.app.getProject();
      const bundle = SIM.app.exportBundle();
      SIM.app.newProject();
      const opened = SIM.app.openBundle(bundle);
      if (!opened || opened.ok === false) {
        failures.push(`app smoke: openBundle failed: ${short(opened)}`);
        return;
      }
      const after = SIM.app.getState ? SIM.app.getState() : SIM.app.getProject();
      const d1 = SIM.fmt.digest64(SIM.fmt.canonical(before));
      const d2 = SIM.fmt.digest64(SIM.fmt.canonical(after));
      if (d1 !== d2) failures.push(`app smoke: export/import round trip changed the state digest`);
    } catch (e) {
      failures.push(`app smoke: threw ${e && e.message ? e.message : e}`);
    }
  }

  function selftest() {
    const golden = (globalThis.SIM && globalThis.SIM.GOLDENS) || null;
    const failures = [];
    let total = 0;
    if (!golden) {
      failures.push("SIM.GOLDENS is missing - load goldens.js before selftest.js");
    } else {
      const fixtures = golden.fixtureText || {};
      golden.cases.forEach(c => {
        const runner = RUNNERS[c.kind];
        total += 1;
        if (!runner) { failures.push(`unknown golden case kind ${c.kind}`); return; }
        let message;
        try {
          message = runner(c, fixtures);
        } catch (e) {
          message = `${c.kind} ${c.name || c.fixture || ""}: threw ${e && e.message ? e.message : e}`;
        }
        if (message) failures.push(message);
      });
    }
    runAppSmoke(failures);
    total += 1;      // the app smoke case itself counts as one, run or skipped
    const result = { ok: failures.length === 0, total, failed: failures.length, failures: failures.slice(0, 40) };
    try {
      let pre = document.getElementById("selftest-out");
      if (!pre) {
        pre = document.createElement("pre");
        pre.id = "selftest-out";
        document.body.appendChild(pre);
      }
      pre.setAttribute("data-ok", String(result.ok));
      pre.setAttribute("data-total", String(result.total));
      pre.setAttribute("data-failed", String(result.failed));
      pre.textContent = JSON.stringify(result, null, 1);
    } catch (e) {
      // no DOM (e.g. a non-browser JS engine running this file directly)
    }
    return result;
  }

  globalThis.__selftest = selftest;

  if (typeof document !== "undefined") {
    const run = () => { if (location.hash === "#selftest") selftest(); };
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", run);
    } else {
      run();
    }
  }
})();
