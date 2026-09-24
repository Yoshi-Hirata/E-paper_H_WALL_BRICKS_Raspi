/* flicker.js — the e-paper refresh flicker simulator, extracted from
 * conductor/web/index.html (as of commit 243bbb1), lines 706-852:
 * JITTER_MAX/REFRESH_TINT/REFRESH_PHASE_A/REFRESH_PALETTE/REFRESH_PHASE_C
 * (711-716), hash32 (721-726), hexOfCode/hexToRgb/TINT_STEPS/tintedHex
 * (727-748), refreshModelFor (749-768), refreshPhaseColor (769-788),
 * applySweptColors/applyFlatColors (789-824), wornAt (825-852).
 *
 * Kept verbatim except the parameterisation plan_designer_sim.md §2.3 names for
 * wornAt: `opts {flicker, cuesOf, palette}`. The operator page reads the show's
 * cues and palette off two module-level globals (`cuesOf(item)`, itself reading
 * `state.show.cues`, and `state.palette` via hexOfCode/rgb); the designer has no
 * such globals, so both travel down as parameters from wornAt's own opts, threaded
 * through every helper that used to read them off `state` directly (maxRankOf's
 * cache, hexOfCode, tintedHex, refreshPhaseColor, applySweptColors/applyFlatColors).
 * `maxRankCache`/`refreshModelCache` stay as this module's own private caches
 * (SIM.flicker.clearCaches() replaces the operator page's per-refresh() clearing).
 */
(function () {
  "use strict";

  const JITTER_MAX = 0.35;
  const REFRESH_TINT = [0xb8, 0xa0, 0x30];
  const REFRESH_PHASE_A = ["#2b2a4e", "#3a2f5a"];
  const REFRESH_PALETTE = ["#2b2a4e", "#3a2f5a", "#9a9aa0", "#c9c9cc", "#e6e3d8",
                            "#d8cf7a", "#b8b83a", "#7a5a3a", "#8a4a3a", "#2f4a7a"];
  const REFRESH_PHASE_C = ["#c9c9cc", "#e6e3d8", "#d8cf7a", "#b8b83a", "#7a5a3a"];
  const TINT_STEPS = 8;

  const maxRankCache = new Map();
  const refreshModelCache = new Map();
  const tintedHexCache = new Map();

  function clearCaches() {
    maxRankCache.clear();
    refreshModelCache.clear();
    tintedHexCache.clear();
  }

  function hash32(a, b) {
    let h = Math.imul(a ^ 0x9e3779b1, 0x85ebca6b) ^ Math.imul(b ^ 0x27d4eb2f, 0xc2b2ae35);
    h = Math.imul(h ^ (h >>> 15), 0x2c1b3c6d);
    h = Math.imul(h ^ (h >>> 12), 0x297a2d39);
    return (h ^ (h >>> 15)) >>> 0;
  }
  function hexOfCode(code, palette) {
    const [r, g, b] = palette[code].rgb;
    return "#" + [r, g, b].map(v => Math.round(v).toString(16).padStart(2, "0")).join("");
  }
  function hexToRgb(hex) { return [parseInt(hex.slice(1, 3), 16), parseInt(hex.slice(3, 5), 16), parseInt(hex.slice(5, 7), 16)]; }
  function tintedHex(targetCode, w, palette) {
    const step = Math.max(0, Math.min(TINT_STEPS, Math.round(w * TINT_STEPS)));
    const key = targetCode + "|" + step;
    let out = tintedHexCache.get(key);
    if (out === undefined) {
      const target = hexToRgb(hexOfCode(targetCode, palette));
      const ww = step / TINT_STEPS;
      const mixed = target.map((v, i) => Math.round(v * (1 - ww) + REFRESH_TINT[i] * ww));
      out = "#" + mixed.map(v => v.toString(16).padStart(2, "0")).join("");
      tintedHexCache.set(key, out);
    }
    return out;
  }
  function maxRankOf(item, sequence, ranks) {
    const key = item.item + "|" + sequence;
    let m = maxRankCache.get(key);
    if (m === undefined) { m = Math.max(...ranks, 0); maxRankCache.set(key, m); }
    return m;
  }
  function refreshModelFor(item, cue, sweep) {
    const key = item.item + "|" + cue.id;
    let m = refreshModelCache.get(key);
    if (m) return m;
    const ranks = item.sequences?.[sweep.sequence];
    const maxRank = ranks ? maxRankOf(item, sweep.sequence, ranks) : 0;
    const n = item.map.scales.length;
    const delay = new Float64Array(n), jitter = new Float64Array(n);
    for (let i = 0; i < n; i++) {
      delay[i] = ranks && maxRank > 0 ? ranks[i] * sweep.span_s / maxRank : 0;
      jitter[i] = (hash32(i, 0xa5a5) % 1000) / 1000 * JITTER_MAX;
    }
    m = { delay, jitter };
    refreshModelCache.set(key, m);
    return m;
  }
  function refreshPhaseColor(n, tau, frac, targetCode, palette) {
    const bucket = Math.floor(tau / 0.25);
    const h = hash32(n, bucket);
    if (frac < 0.13) return REFRESH_PHASE_A[h % REFRESH_PHASE_A.length];
    if (frac < 0.50) return REFRESH_PALETTE[h % REFRESH_PALETTE.length];
    if (frac < 0.72) return REFRESH_PHASE_C[h % REFRESH_PHASE_C.length];
    if (frac < 0.95) {
      if (h % 6 === 0) return REFRESH_PHASE_C[hash32(n, bucket + 1) % REFRESH_PHASE_C.length];
      const w = 0.45 * (1 - (frac - 0.72) / 0.23);
      return tintedHex(targetCode, w, palette);
    }
    return targetCode;
  }
  const sweepOf = cue => cue.sweep || { sequence: "natural", span_s: 0, source: "design" };
  function refreshOfFor(cue, showRefreshS) {
    if (cue.refresh !== undefined) return { value: cue.refresh, source: cue.refresh_source || (cue.refresh_s != null ? "cue" : "show") };
    return cue.refresh_s != null ? { value: cue.refresh_s, source: "cue" } : { value: showRefreshS, source: "show" };
  }
  function applySweptColors(item, cue, design, t, out, palette) {
    const sweep = sweepOf(cue);
    const refresh = cue.refresh ?? cue.refresh_s ?? 7;
    const model = refreshModelFor(item, cue, sweep);
    item.map.scales.forEach((s, n) => {
      const key = `${s[0]}|${s[1]}|${s[2]}`;
      const targetCode = design.colors[key];
      if (targetCode === undefined) return;
      const tau = t - (cue.sent + model.delay[n] + model.jitter[n]);
      if (tau < 0) return;
      out[key] = tau >= refresh ? targetCode : refreshPhaseColor(n, tau, tau / refresh, targetCode, palette);
    });
  }
  function applyFlatColors(item, cue, design, t, out) {
    const sweep = sweepOf(cue);
    const refresh = cue.refresh ?? cue.refresh_s ?? 7;
    const model = refreshModelFor(item, cue, sweep);
    item.map.scales.forEach((s, n) => {
      const key = `${s[0]}|${s[1]}|${s[2]}`;
      const targetCode = design.colors[key];
      if (targetCode === undefined) return;
      if (t >= cue.sent + model.delay[n] + refresh) out[key] = targetCode;
    });
  }
  const designLabel = d => d.label || d.name;
  // opts: {flicker (default true), cuesOf(item)->cues, palette}
  function wornAt(item, t, opts) {
    const flicker = !opts || opts.flicker !== false;
    const cues = (opts && opts.cuesOf) ? opts.cuesOf(item) : [];
    const palette = opts && opts.palette;
    let colors = {}, shifts = null, label = "(as before the show)", changing = false;
    for (const cue of cues) {
      const design = item.designs.find(d => d.name === cue.design);
      if (cue.sent <= t && t < cue.complete) changing = true;
      if (!design) continue;
      if (cue.complete > t) {
        if (cue.sent <= t) {
          if (flicker) applySweptColors(item, cue, design, t, colors, palette);
          else applyFlatColors(item, cue, design, t, colors);
        }
        continue;
      }
      colors = cue.partial ? { ...colors, ...design.colors } : { ...design.colors };
      shifts = design.shifts; label = designLabel(design) + (cue.partial ? " (partial)" : "");
    }
    return { colors, shifts, label, changing };
  }

  globalThis.SIM = Object.assign(globalThis.SIM || {}, {
    flicker: {
      JITTER_MAX, REFRESH_TINT, REFRESH_PHASE_A, REFRESH_PALETTE, REFRESH_PHASE_C, TINT_STEPS,
      hash32, refreshPhaseColor, refreshModelFor, wornAt, clearCaches,
    },
  });
})();
