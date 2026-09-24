/* looks.js — the LOOK thumbnail row, extracted from conductor/web/index.html
 * (as of commit 243bbb1): lookGroups() (1134-1155), garmentAspect() lives in
 * render.js instead (see render.js's header), renderThumbs() (1180-1204),
 * buildLookCache() (1205-1219), layoutLooks() (1226-1247), updateThumbColors()
 * (1264-1290). liveChip() (1298-1307) is dropped: it exists only for the
 * operator's live Units-tab row (reads a `fleet` global with no designer
 * equivalent) - the designer has one row (the Timeline simulator), always in
 * "changing / not" mode, never "unit offline"/"loading…".
 *
 * Parameterised per plan_designer_sim.md §2.3: `lookGroups(items)` takes the
 * item list directly instead of reading a `trackItems()` off a module-level
 * `state`; `renderThumbs(view, t, ctx)` / `updateThumbColors(t, view, ctx)` take
 * a `ctx = {cuesOf, palette}` bag instead of reading `state.show.cues` /
 * `state.palette` globally, and pass it straight through to SIM.flicker.wornAt.
 */
(function () {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const render = () => globalThis.SIM.render;
  const flicker = () => globalThis.SIM.flicker;

  // index.html:1134-1155
  function lookGroups(items) {
    const groups = new Map();
    for (const item of items) {
      const key = item.look ? "L" + item.look : "I" + item.item;
      if (!groups.has(key)) groups.set(key, { look: item.look || null, items: [] });
      groups.get(key).items.push(item);
    }
    const list = [...groups.values()];
    const stackRank = i => /top/i.test(i.model || "") ? 0 : /skirt|bottom/i.test(i.model || "") ? 2 : 1;
    for (const g of list) g.items.sort((a, b) => stackRank(a) - stackRank(b) || (a.item < b.item ? -1 : a.item > b.item ? 1 : 0));
    list.sort((a, b) => {
      const an = a.look !== null ? Number(a.look) : NaN, bn = b.look !== null ? Number(b.look) : NaN;
      const aNum = Number.isFinite(an), bNum = Number.isFinite(bn);
      if (aNum && bNum && an !== bn) return an - bn;
      if (aNum !== bNum) return aNum ? -1 : 1;
      return a.items[0].item < b.items[0].item ? -1 : a.items[0].item > b.items[0].item ? 1 : 0;
    });
    return list;
  }
  const esc = s => String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const itemName = i => !i ? "" : i.look ? "LOOK " + i.look : i.item;

  // index.html:1180-1204. ctx: {cuesOf, palette}
  function renderThumbs(view, t, ctx) {
    if (view.onPlayhead) view.onPlayhead(t);
    const box = $(view.sel);
    if (!box) return;
    const groups = lookGroups(view.items());
    if (!groups.length) { box.innerHTML = ""; box.style.display = "none"; view.cache = []; return; }
    box.style.display = "";
    box.innerHTML = groups.map(g => {
      return `<div class="look-cell">
        <div class="lk-head">${esc(g.look ? "LOOK " + g.look : itemName(g.items[0]))}<span class="chg" style="display:none"></span>
          <div class="sub"></div></div>
        ${g.items.map(item => { const worn = flicker().wornAt(item, t, { flicker: true, cuesOf: ctx.cuesOf, palette: ctx.palette }); return `<div class="lk-item">
          ${render().renderGarment(item, { cell: view.cell, mode: "design", labels: false, tag: true, colors: worn.colors, shifts: worn.shifts, palette: ctx.palette })}</div>`; }).join("")}
      </div>`; }).join("");
    buildLookCache(groups, view);
    layoutLooks(view);
    updateThumbColors(t, view, ctx);
  }
  // index.html:1205-1219
  function buildLookCache(groups, view) {
    const box = $(view.sel), cache = [];
    (box ? box.querySelectorAll(".look-cell") : []).forEach((cellEl, gi) => {
      const g = groups[gi];
      const items = [];
      cellEl.querySelectorAll(".lk-item").forEach((itemEl, ii) => {
        const item = g.items[ii];
        const circles = new Map();
        itemEl.querySelectorAll("g[data-k]").forEach(el => circles.set(el.dataset.k, el.querySelector("circle")));
        items.push({ item, svg: itemEl.querySelector("svg"), circles, colors: new Map(), ratio: render().garmentAspect(item) });
      });
      cache.push({ cellEl, sub: cellEl.querySelector(".sub"), chg: cellEl.querySelector(".chg"), items });
    });
    view.cache = cache;
  }
  // index.html:1226-1247
  function layoutLooks(view) {
    const box = $(view.sel);
    if (!box || !view.cache.length) return;
    const rect = box.getBoundingClientRect();
    const count = view.cache.length;
    const GAP = 10, CELL_PAD_X = 16, CELL_PAD_Y = 12, HEAD_H = view.head, STACK_GAP = 4;
    const containerWidth = box.clientWidth || rect.width;
    const cellWidth = Math.max(40, (containerWidth - GAP * (count - 1)) / count);
    const wBudget = Math.max(20, cellWidth - CELL_PAD_X);
    const availableHeight = view.fill ? Math.max(80, window.innerHeight - rect.top - 16) : view.height;
    box.style.height = availableHeight + "px";
    for (const cell of view.cache) {
      cell.cellEl.style.width = cellWidth + "px";
      const n = cell.items.length;
      const hBudget = Math.max(20, availableHeight - HEAD_H - CELL_PAD_Y - (n - 1) * STACK_GAP);
      const sumRatio = cell.items.reduce((s, it) => s + it.ratio, 0) || 1;
      const renderWidth = Math.max(16, Math.min(wBudget, hBudget / sumRatio));
      for (const it of cell.items) {
        if (it.svg) { it.svg.style.width = renderWidth + "px"; it.svg.style.maxWidth = "none"; it.svg.style.height = "auto"; }
      }
    }
  }
  // index.html:1264-1290, minus the live-fleet chip branch (see file header)
  function updateThumbColors(t, view, ctx) {
    for (const cell of view.cache) {
      let changing = false;
      const sub = [];
      for (const entry of cell.items) {
        const worn = flicker().wornAt(entry.item, t, { flicker: true, cuesOf: ctx.cuesOf, palette: ctx.palette });
        if (worn.changing) changing = true;
        sub.push(`${entry.item.model || entry.item.item}: ${worn.label}`);
        for (const [key, circleEl] of entry.circles) {
          const fill = render().fillFor(worn.colors[key], ctx.palette);
          if (entry.colors.get(key) !== fill) { entry.colors.set(key, fill); if (circleEl) circleEl.setAttribute("fill", fill); }
        }
      }
      if (cell.chg) {
        cell.chg.textContent = "refreshing…";
        cell.chg.style.display = changing ? "inline" : "none";
      }
      if (cell.sub) cell.sub.textContent = sub.join(" · ");
    }
  }

  globalThis.SIM = Object.assign(globalThis.SIM || {}, {
    looks: { lookGroups, renderThumbs, buildLookCache, layoutLooks, updateThumbColors },
  });
})();
