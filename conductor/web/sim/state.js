/*
 * conductor/web/sim/state.js
 *
 * Classic script - a JS port, as it is on main commit cd22872
 * (2026-09-24), of conductor/server.py's Workspace.state() (plus its
 * small helpers _design_transition/_clean_transitions/_time_sweeps),
 * adapted to a pure function of an in-memory project (no disk, no
 * locking, no undo history): SIM.buildState(project) -> state.
 *
 * Differences from the server's state() (plan section 1.3), and only
 * these: state.units = [], every item.unit = null (the sim never
 * assigns units); no history/workspace/fleet keys; state.music =
 * {name, url} with url always null here (SIM.app overlays the live
 * object URL of the picked file); state.show.min_interval is keyed by
 * the Python fallback bus name "(<item>)" for every item, unchanged
 * from the server's own fallback - the goldens compare it as-is, the UI
 * rewrites it for display.
 *
 * Requires model.js to have run first (script order in designer.html:
 * model.js -> state.js -> ...).
 *
 * Owned by Coder P.
 */
(function () {
  "use strict";

  const look = SIM.look;
  const sequence = SIM.sequence;
  const timeline = SIM.timeline;
  const _int = SIM._internal;

  const LOOK_NO = /^look\s*0*(\d+)/i;   // Python's re.match anchors at the start only
  const DEFAULT_DURATION_S = timeline.DEFAULT_DURATION_S;
  const REFRESH_S = timeline.REFRESH_S;

  function stemOf(filename) {
    const base = String(filename).replace(/^.*[\\/]/, "");
    const dot = base.lastIndexOf(".");
    return dot > 0 ? base.slice(0, dot) : base;
  }

  // ---- conductor/server.py's _design_transition ----
  function designTransition(entry) {
    if (typeof entry !== "object" || entry === null || Array.isArray(entry)) entry = {};
    return {
      sequence: sequence.cleanSequence(entry.sequence !== undefined ? entry.sequence : "natural"),
      span_s: sequence.cleanSpan(entry.span_s !== undefined ? entry.span_s : 0.0),
    };
  }

  // ---- conductor/server.py's _clean_transitions ----
  function cleanTransitions(raw) {
    const result = Object.create(null);
    if (typeof raw !== "object" || raw === null || Array.isArray(raw)) return result;
    Object.keys(raw).forEach(design => {
      const entry = raw[design];
      if (typeof entry !== "object" || entry === null || Array.isArray(entry)) return;
      const seq = sequence.cleanSequence(entry.sequence !== undefined ? entry.sequence : "natural");
      const span = Math.min(sequence.MAX_DELAY_S,
        sequence.cleanSpan(entry.span_s !== undefined ? entry.span_s : 0.0));
      if (seq !== "natural" && span > 0) result[String(design)] = { sequence: seq, span_s: span };
    });
    return result;
  }

  // ---- conductor/server.py's _time_sweeps ----
  function timeSweeps(cues, maps) {
    cues.forEach(cue => {
      const map = maps[cue.item.toLowerCase()];
      const sweep = cue.sweep;
      if (sweep.sequence === "natural") {
        cue.span = 0.0;
      } else if (map) {
        cue.span = sequence.spanS(map, sweep.sequence, sweep.span_s);
      }
    });
  }

  // Python int(x): truncates an already-numeric x, parses a strict
  // whole-number string, else null (matching int()'s ValueError).
  function pyIntCoerce(v) {
    if (typeof v === "number") return Number.isFinite(v) ? Math.trunc(v) : null;
    if (typeof v === "string") return _int.pyIntStrict(v);
    return null;
  }

  // {old board_no in the map CSV: the number the garment really has},
  // for one item - Workspace._own_boards, restricted to one item and
  // taking `show` (project.show) rather than a loaded show.json.
  // Python's version is all-or-nothing: a dict comprehension that hits
  // one bad int() aborts entirely (its except clause returns {}), it
  // does not keep the entries that happened to convert.
  function ownBoardsFor(show, item) {
    const perItem = show && show.boards && typeof show.boards === "object"
      && !Array.isArray(show.boards) ? show.boards[item] : null;
    if (!perItem || typeof perItem !== "object" || Array.isArray(perItem)) return {};
    const result = {};
    for (const oldKey of Object.keys(perItem)) {
      const old = pyIntCoerce(oldKey);
      const neu = pyIntCoerce(perItem[oldKey]);
      if (old === null || neu === null) return {};
      result[old] = neu;
    }
    return result;
  }

  function buildState(project) {
    project = project || {};
    const files = project.files && typeof project.files === "object" ? project.files : {};
    const show = project.show && typeof project.show === "object" ? project.show : {};
    const names = Object.keys(files).sort();

    const labels = show.labels && typeof show.labels === "object" ? show.labels : {};
    // server.py's state() uses `show.get("transitions") or {}` RAW - no
    // _clean_transitions() there (that only runs on import_show). Every
    // consumer (designTransition() per design, timeline.resolve() per
    // cue via applyTransitions()) does its own per-entry cleaning, so
    // the show's own unclean entries (a zero span, a span over 30 s)
    // still show up in state.transitions exactly as authored.
    const transitions = show.transitions || {};

    const items = Object.create(null);      // lower(item) -> entry
    const maps = Object.create(null);       // lower(item) -> map

    function itemEntry(name) {
      const key = name.toLowerCase();
      if (items[key]) return items[key];
      let label = labels[name];
      if (typeof label !== "object" || label === null || Array.isArray(label)) {
        const m = LOOK_NO.exec(name);
        label = { look: m ? String(parseInt(m[1], 10)) : "", model: "" };
      }
      const entry = {
        item: name, unit: null, map: null,
        look: String(label.look || ""), model: String(label.model || ""),
        designs: [], problems: [],
      };
      items[key] = entry;
      return entry;
    }

    // ---- maps ----
    names.forEach(name => {
      if (look.kind(name) !== "map") return;
      const text = files[name];
      const stem = stemOf(name);
      const item = look.mapItem(name);
      const result = look.parseMap(text, { name, item });
      if (!result.ok) {
        const entry = itemEntry(stem.slice(0, -4));    // kind() confirmed "_map" (4 chars) at the end
        entry.map = { name, scales: [], sides: [], shifts: {} };
        entry.problems = entry.problems.concat(result.problems);
        return;
      }
      let map = result.map;
      map = look.renumber(map, ownBoardsFor(show, map.item || ""));
      const entry = itemEntry(map.item || stem);
      maps[entry.item.toLowerCase()] = map;
      const rowKeys = new Set(map.scales.map(s => `${s.side}|${s.row}`));
      const shiftsOut = {};
      rowKeys.forEach(k => {
        const sep = k.indexOf("|");
        const side = k.slice(0, sep);
        const row = Number(k.slice(sep + 1));
        const v = look.shiftAt(map, side, row);
        if (v !== look.defaultShift(row)) shiftsOut[k] = v;
      });
      entry.map = {
        name, sides: map.sides.slice(), warnings: map.warnings, shifts: shiftsOut,
        scales: map.scales.map(s => [s.side, s.row, s.col, s.board_no, s.socket]),
      };
    });

    // ---- designs ----
    const orphans = [];
    // The garments whose map the loop above already parsed - what tells
    // nameParts() where the item ends in an <item>_<名前>_HW.csv whose
    // design name has underscores of its own (server.py's _known_items()).
    const knownItems = Object.keys(maps).map(k => maps[k].item).filter(Boolean);
    names.forEach(name => {
      if (look.kind(name) !== "grid") return;
      const text = files[name];
      const parts = look.nameParts(name, knownItems);   // [item, pattern, label]
      const parseResult = look.parseDesign(text, { name, item: parts[0], pattern: parts[1] });
      let design = null;
      let problems = [];
      if (parseResult.ok) {
        design = parseResult.design;
        design.label = parts[2];
      } else {
        problems = parseResult.problems;
      }
      const item = (design && design.item) || stemOf(name);
      const lookMap = maps[item.toLowerCase()];
      const record = {
        name, pattern: design ? design.pattern : null,
        label: design ? design.label : parts[2],
        problems: problems.slice(), partial_problems: problems.slice(),
        colors: {}, shifts: {}, undecided: [],
        transition: designTransition(transitions[name]),
      };
      if (design) {
        record.colors = design.colors;
        record.shifts = design.shifts;
        record.undecided = _int.sortedPositionKeys(design.undecided).map(_int.posKeyOf);
        if (lookMap) {
          record.problems = look.check(lookMap, design, false);
          record.partial_problems = look.check(lookMap, design, true);
        }
      }
      if (!lookMap && !Object.prototype.hasOwnProperty.call(items, item.toLowerCase())) {
        if (!record.problems.length) record.problems = [`no ${item}_map.csv yet`];
        orphans.push(record);
      } else {
        itemEntry(item).designs.push(record);
      }
    });

    // ---- board addressing (never shared: the sim has no unit assignment) ----
    Object.keys(maps).forEach(key => {
      const map = maps[key];
      const entry = items[key];
      const ids = look.boardIds(map);
      entry.boards = look.dipSheet(map, ids);
      const was = {};
      const own = ownBoardsFor(show, entry.item);
      Object.keys(own).forEach(oldKey => { was[own[oldKey]] = Number(oldKey); });
      entry.boards.forEach(board => {
        board.source_no = Object.prototype.hasOwnProperty.call(was, board.board_no)
          ? was[board.board_no] : board.board_no;
      });
    });

    // ---- sweep sequences, for the page's own sweep preview ----
    Object.keys(maps).forEach(key => {
      const map = maps[key];
      const order = map.scales.map(s => s.key);
      items[key].sequences = {};
      sequence.SEQUENCES.forEach(name => {
        if (name === "natural") return;
        const ranked = sequence.ranksByKey(map, name);
        items[key].sequences[name] = order.map(p => ranked[p]);
      });
    });

    const ordered = Object.values(items).sort((a, b) => {
      const au = a.unit || "~", bu = b.unit || "~";
      if (au !== bu) return au < bu ? -1 : 1;
      const ai = a.item.toLowerCase(), bi = b.item.toLowerCase();
      return ai < bi ? -1 : ai > bi ? 1 : 0;
    });
    ordered.forEach(entry => {
      entry.designs.sort((a, b) => {
        const an = a.pattern === null, bn = b.pattern === null;
        if (an !== bn) return an ? 1 : -1;
        const av = a.pattern || 0, bv = b.pattern || 0;
        if (av !== bv) return av - bv;
        return a.name < b.name ? -1 : a.name > b.name ? 1 : 0;
      });
      if (!entry.boards) entry.boards = [];
    });

    const facts = Object.create(null);
    Object.keys(items).forEach(key => {
      if (!Object.prototype.hasOwnProperty.call(maps, key)) return;
      const entry = items[key];
      const designs = {};
      entry.designs.forEach(d => {
        designs[d.name] = { full: !d.problems.length, partial: !d.partial_problems.length };
      });
      facts[key] = { item: entry.item, unit: entry.unit, boards: entry.boards.length, designs };
    });

    // `float(show.get("duration", DEFAULT))`: the default only applies
    // when the KEY IS ABSENT - a present-but-junk value (a string that
    // isn't a number, null, an object) must throw, not silently fall
    // back to the default.
    function numberOr(key, defaultValue) {
      if (!Object.prototype.hasOwnProperty.call(show, key)) return defaultValue;
      const num = _int.toNumber(show[key]);
      if (num === null) throw new Error(`not a number: ${JSON.stringify(show[key])}`);
      return num;
    }
    const duration = numberOr("duration", DEFAULT_DURATION_S);
    const refresh = numberOr("refresh_s", REFRESH_S);

    const cues = timeline.clean(show.cues);
    timeline.applyTransitions(cues, transitions);
    timeSweeps(cues, maps);
    const validated = timeline.validate(cues, facts, duration, refresh);
    const cueProblems = validated.problems, warnings = validated.warnings;
    const cueEnds = timeline.ends(cues, refresh, duration);
    cues.forEach(cue => {
      const t = timeline.times(cue, refresh);
      cue.sent = t[0]; cue.complete = t[1];
      cue.refresh = timeline.effectiveRefresh(cue, refresh);
      cue.refresh_source = (typeof cue.refresh_s === "number" && !Number.isNaN(cue.refresh_s)) ? "cue" : "show";
      const end = cueEnds[cue.id];
      cue.end = end[0]; cue.end_source = end[1];
      cue.problems = cueProblems[cue.id];
    });

    const unitBoards = Object.create(null);
    Object.keys(facts).forEach(key => {
      const fact = facts[key];
      const name = fact.unit || `(${fact.item})`;
      unitBoards[name] = (unitBoards[name] || 0) + fact.boards;
    });
    const minInterval = {};
    Object.keys(unitBoards).forEach(u => { minInterval[u] = timeline.minInterval(unitBoards[u], refresh); });

    return {
      show: { duration, refresh_s: refresh, cues, warnings, min_interval: minInterval },
      units: [],
      items: ordered,
      orphans,
      sequences: sequence.SEQUENCES.map(name => ({ id: name, label: sequence.LABELS[name] })),
      palette: look.PALETTE.map(([n, rgb]) => ({ name: n, rgb: rgb.slice() })),
      transitions,
      music: { name: (show.music && typeof show.music === "object" && show.music.name) || null, url: null },
    };
  }

  globalThis.SIM = Object.assign(globalThis.SIM || {}, {
    buildState, timeSweeps, cleanTransitions, designTransition,
  });
})();
