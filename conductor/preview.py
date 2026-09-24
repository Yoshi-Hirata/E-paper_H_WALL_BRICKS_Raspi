"""Draw a look as the garment wears it: one disc per scale, front and back.

Two views from the same geometry:

  design view  - every scale in its cue colour (the FW_260917 chart's
                 reference RGB; the e-paper's real pigments are duller),
                 for the designer to check a cue before the show.
  wiring view  - no design given: every scale tinted by its board and
                 labelled with its socket, for whoever plugs the scales
                 in and sets the DIP switches.

Rows are drawn hem first so each row overlaps the one below it, the way
the scales hang. The highest row is the neck and goes at the top.
"""

from __future__ import annotations

import colorsys

from PIL import Image, ImageDraw, ImageFont

from .look import PALETTE, Design, LookMap

BG = (245, 245, 247)
INK = (40, 40, 48)
EDGE = (120, 120, 130)
ROW_PITCH = 0.78           # rows overlap: pitch < one scale diameter
MARGIN = 1.2               # in scale widths


def _font(size: int):
    for path in ("C:/Windows/Fonts/segoeui.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def board_tint(index: int, count: int) -> "tuple[int, int, int]":
    """Evenly spaced hues, alternating lightness so neighbours in the
    numbering (usually neighbours on the garment) stay apart."""
    hue = index / max(count, 1)
    light = 0.62 if index % 2 else 0.48
    r, g, b = colorsys.hls_to_rgb(hue, light, 0.65)
    return int(r * 255), int(g * 255), int(b * 255)


def _text_color(rgb) -> "tuple[int, int, int]":
    luminance = 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]
    return (20, 20, 20) if luminance > 140 else (245, 245, 245)


def render(look_map: LookMap, design: "Design | None" = None,
           cell: int = 26, title: "str | None" = None) -> Image.Image:
    sides = look_map.sides
    rows = [s.row for s in look_map.scales]
    cols = [s.col for s in look_map.scales]
    top_row, low_row = max(rows), min(rows)
    low_col, span_cols = min(cols), max(cols) - min(cols) + 1.5

    side_w = int((span_cols + 2 * MARGIN) * cell)
    header = int(cell * 2.2)
    legend_h = int(cell * 2.4)
    body_h = int(((top_row - low_row) * ROW_PITCH + 1 + 2 * MARGIN) * cell)
    image = Image.new("RGB", (side_w * len(sides), header + body_h + legend_h),
                      BG)
    draw = ImageDraw.Draw(image)
    font_title, font_side = _font(int(cell * 0.7)), _font(int(cell * 0.55))
    font_tiny = _font(max(8, int(cell * 0.36)))

    heading = title or (f"{look_map.name}  +  {design.name}" if design
                        else f"{look_map.name}  (wiring: board / socket)")
    draw.text((int(cell * 0.6), int(cell * 0.3)), heading, font=font_title,
              fill=INK)

    ids = look_map.board_ids
    radius = cell * 0.5
    for index, side in enumerate(sides):
        x0 = index * side_w
        draw.text((x0 + int(MARGIN * cell), int(cell * 1.3)), side.upper(),
                  font=font_side, fill=EDGE)
        scales = sorted((s for s in look_map.scales if s.side == side),
                        key=lambda s: (s.row, s.col))      # hem first
        for scale in scales:
            shift = (design.shift(side, scale.row) if design
                     else look_map.shift(scale.side, scale.row))
            cx = x0 + (MARGIN + scale.col - low_col + shift + 0.5) * cell
            cy = header + (MARGIN + (top_row - scale.row) * ROW_PITCH
                           + 0.5) * cell
            if design is not None:
                code = design.colors.get(scale.position)
                fill = PALETTE[code][1] if code is not None else BG
            else:
                fill = board_tint(ids[scale.board_no] - 1, len(ids))
            draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius),
                         fill=fill, outline=EDGE)
            if design is None:
                label = str(scale.socket)
                draw.text((cx - font_tiny.getlength(label) / 2,
                           cy - cell * 0.25), label, font=font_tiny,
                          fill=_text_color(fill))

    # Legend: the colours this cue uses, or which tint is which board.
    y = header + body_h + int(cell * 0.4)
    x = int(cell * 0.6)
    if design is not None:
        entries = [(PALETTE[c][1], f"0x{c:02X} {PALETTE[c][0]}")
                   for c in sorted(set(design.colors.values()))]
    else:
        entries = [(board_tint(address - 1, len(ids)),
                    f"{board_no:03d}=ID{address}")
                   for board_no, address in ids.items()]
    swatch = int(cell * 0.7)
    for fill, text in entries:
        width = swatch + 6 + int(font_tiny.getlength(text)) + int(cell * 0.6)
        if x + width > image.width:
            x, y = int(cell * 0.6), y + swatch + 6
        draw.rectangle((x, y, x + swatch, y + swatch), fill=fill, outline=EDGE)
        draw.text((x + swatch + 6, y + 2), text, font=font_tiny, fill=INK)
        x += width
    return image
