/* render.js — the garment drawing code, extracted from conductor/web/index.html
 * (as of commit 243bbb1): PITCH/MARGIN (index.html:413), tint() (index.html:514-516),
 * shiftOf() (index.html:517-520), fillFor()/rgb() (index.html:431-436),
 * garmentAspect() (index.html:1159-1168), renderGarment() (index.html:525-569).
 *
 * Kept verbatim except the parameterisations plan_designer_sim.md §2.3 calls for:
 * the operator page reads three things off module-level globals that the designer
 * has no equivalent of - `ui.view` ("outside"/"inside"), `ui.board` (the board
 * highlighted by a click on the DIP table, which the designer does not have), and
 * `state.palette` (via the free function rgb()). Here they arrive as opts.view,
 * opts.board and opts.palette instead; fillFor() takes the palette explicitly for
 * the same reason. Everything else - the maths, the loop structure, the SVG
 * string building - is unchanged line for line.
 */
(function () {
  "use strict";

  const PITCH = 0.78, MARGIN = 1.2;

  const esc = s => String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // index.html:514-516
  function tint(index, count) {
    return `hsl(${Math.round(360 * index / Math.max(count, 1))} 62% ${index % 2 ? 62 : 46}%)`;
  }
  // index.html:517-520
  function shiftOf(shifts, side, row) {
    const s = shifts?.[`${side}|${row}`];
    return s !== undefined ? s : (row % 2 ? 0.5 : 0);
  }
  // index.html:431-436, palette now explicit instead of the global `state`
  function fillFor(code, palette) {
    if (code === undefined) return "var(--garment)";
    if (typeof code === "string" && code[0] === "#") return code;
    return `rgb(${palette[code].rgb.join(",")})`;
  }
  // index.html:1159-1168
  function garmentAspect(item) {
    const scales = item.map.scales;
    const rows = scales.map(s => s[1]), cols = scales.map(s => s[2]);
    const top = Math.max(...rows), low = Math.min(...rows);
    const lowCol = Math.min(...cols), span = Math.max(...cols) - lowCol + 1.5;
    const margin = 0.4;
    const w = (span + 2 * margin) * item.map.sides.length;
    const h = (top - low) * PITCH + 1 + 2 * margin;
    return h / w;
  }
  // index.html:525-569. opts: {cell, mode, colors, undecided, shifts, labels, tag,
  // view, board, palette} - the last three replace ui.view/ui.board/state.palette.
  function renderGarment(item, opts) {
    const cell = opts.cell, scales = item.map.scales;
    const shifts = opts.shifts ?? item.map.shifts;
    const rows = scales.map(s => s[1]), cols = scales.map(s => s[2]);
    const top = Math.max(...rows), low = Math.min(...rows);
    const lowCol = Math.min(...cols), span = Math.max(...cols) - lowCol + 1.5;
    const margin = opts.labels ? MARGIN : 0.4;
    const sideW = (span + 2 * margin) * cell;
    const head = opts.labels ? 26 : 0;
    const bodyH = ((top - low) * PITCH + 1 + 2 * margin) * cell;
    const order = item.boards.map(b => b.board_no);
    const sides = item.map.sides;
    const view = opts.view || "outside";
    const board = opts.board ?? null;
    const palette = opts.palette;
    const W = sideW * sides.length, H = bodyH + head;
    let svg = `<svg xmlns="http://www.w3.org/2000/svg" style="width:100%;max-width:${opts.labels ? W + "px" : "100%"};height:auto"
      viewBox="0 0 ${W} ${H}" font-family="Segoe UI, sans-serif">`;
    sides.forEach((side, index) => {
      const x0 = index * sideW;
      if (opts.labels) {
        const label = { front: "FRONT", back: "BACK" }[side] || side.toUpperCase();
        svg += `<text x="${x0 + margin * cell}" y="17" font-size="12" fill="var(--dim)">${esc(label)}</text>`;
      }
      const here = scales.filter(s => s[0] === side).sort((a, b) => a[1] - b[1] || a[2] - b[2]);
      for (const [sd, row, col, boardNo, socket] of here) {
        let cx = (margin + col - lowCol + shiftOf(shifts, side, row) + 0.5) * cell;
        if (view === "outside") cx = sideW - cx;            // the files are drawn from the inside
        const cy = head + (margin + (top - row) * PITCH + 0.5) * cell;
        const key = `${side}|${row}|${col}`;
        let fill, text = "";
        if (opts.mode === "design") {
          const code = opts.colors[key];
          fill = fillFor(code, palette);
          if (code === undefined && opts.labels) text = opts.undecided?.includes(key) ? "-" : "?";
        } else { fill = tint(order.indexOf(boardNo), order.length); text = socket; }
        const dim = board !== null && opts.labels && board !== boardNo ? ` opacity=".18"` : "";
        const tagged = opts.labels || opts.tag;
        svg += `<g${dim}${tagged ? ` data-k="${key}" data-b="${boardNo}" data-s="${socket}"` : ""}>
          <circle cx="${(x0 + cx).toFixed(1)}" cy="${cy.toFixed(1)}" r="${cell / 2}" fill="${fill}" stroke="#7d7d88" stroke-width="${opts.labels ? .7 : .3}"/>` +
          (text !== "" ? `<text x="${(x0 + cx).toFixed(1)}" y="${(cy + 3).toFixed(1)}" font-size="8.5" text-anchor="middle"
            fill="${opts.mode === "design" ? "var(--err)" : "#fff"}" pointer-events="none">${text}</text>` : "") + `</g>`;
      }
    });
    return svg + "</svg>";
  }

  globalThis.SIM = Object.assign(globalThis.SIM || {}, {
    render: { PITCH, MARGIN, shiftOf, tint, fillFor, garmentAspect, renderGarment },
  });
})();
