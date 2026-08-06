"""Screen rendering: state in, 240x240 PIL image out.

Pure drawing code with no hardware or timing dependencies, so screens can
be snapshot-tested and previewed as PNGs long before the LCD arrives.
"""

from PIL import Image, ImageDraw, ImageFont

from .config import HEIGHT, LOG_LINES, WIDTH

# Palette for a 240x240 IPS panel read at arm's length. Pure black gives
# the most contrast the panel can produce; the secondary tone is kept
# bright enough to stay legible at 12px (~10:1 against the background,
# where the previous grey managed about 5:1).
BG = (0, 0, 0)
FG = (255, 255, 255)         # primary text
DIM = (176, 186, 200)        # secondary text: labels, log, hints
ACCENT = (120, 205, 255)     # headers
OK = (110, 235, 140)
ERR = (255, 105, 105)
BAR = (30, 33, 40)           # header/hint strips, distinct from the black
SELECT = (0, 82, 140)        # selected menu row

_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
)


def _font(size: int) -> ImageFont.ImageFont:
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


FONT_S = _font(12)
FONT_M = _font(15)
FONT_L = _font(19)
FONT_TIMER = _font(38)


def format_elapsed(seconds: float) -> str:
    total = max(0, int(seconds))
    return f"{total // 3600:02d}:{total // 60 % 60:02d}:{total % 60:02d}"


def _blank() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    image = Image.new("RGB", (WIDTH, HEIGHT), BG)
    return image, ImageDraw.Draw(image)


def _header(draw: ImageDraw.ImageDraw, text: str, color=ACCENT) -> None:
    draw.rectangle((0, 0, WIDTH, 24), fill=BAR)
    draw.text((8, 4), text, font=FONT_M, fill=color)


def _hint(draw: ImageDraw.ImageDraw, text: str) -> None:
    draw.rectangle((0, HEIGHT - 20, WIDTH, HEIGHT), fill=BAR)
    draw.text((8, HEIGHT - 18), text, font=FONT_S, fill=DIM)


def _ellipsize(text: str, font, max_width: int) -> str:
    if font.getlength(text) <= max_width:
        return text
    while text and font.getlength(text + "\u2026") > max_width:
        text = text[:-1]
    return text + "\u2026"


def menu_screen(patterns, selected: int, port: str | None = None,
                locked: bool = False) -> Image.Image:
    """Pattern chooser. Up/Down move, KEY1 starts."""
    image, draw = _blank()
    _header(draw, "E-PAPER DEMO")
    if locked:
        draw.text((WIDTH - 8 - FONT_S.getlength("LOCKED"), 6), "LOCKED",
                  font=FONT_S, fill=ACCENT)

    # Scroll the list so the cursor stays visible on the 240px screen.
    visible = 6
    first = max(0, min(selected - visible // 2, len(patterns) - visible))
    first = max(0, first)
    row_h = 26
    for row, index in enumerate(range(first, min(first + visible, len(patterns)))):
        y = 30 + row * row_h
        chosen = index == selected
        if chosen:
            draw.rectangle((4, y - 2, WIDTH - 4, y + row_h - 6), fill=SELECT)
            draw.rectangle((4, y - 2, 7, y + row_h - 6), fill=ACCENT)
        draw.text((14, y), patterns[index].label, font=FONT_M,
                  fill=FG if chosen else DIM)

    detail = patterns[selected].detail if patterns else ""
    draw.text((8, 188), _ellipsize(detail, FONT_S, WIDTH - 16),
              font=FONT_S, fill=DIM)
    draw.text((8, 202), f"port {port}" if port else "port: not found",
              font=FONT_S, fill=DIM if port else ERR)
    _hint(draw, "buttons locked" if locked
          else "UP/DOWN sel  KEY1 start  KEY3 off")
    return image


def running_screen(pattern_label: str, elapsed: float, cycle: int,
                   log_lines: list[str], error: str | None = None,
                   stopping: bool = False, paused: bool = False,
                   locked: bool = False) -> Image.Image:
    """Live view: elapsed timer, cycle counter and the tail of the log."""
    image, draw = _blank()
    if error:
        status, color = "ERROR", ERR
    elif paused:
        status, color = "PAUSED", ACCENT
    elif stopping:
        status, color = "STOPPING", DIM
    else:
        status, color = "RUNNING", OK
    _header(draw, _ellipsize(pattern_label, FONT_M, 150))
    draw.text((WIDTH - 8 - FONT_S.getlength(status), 6), status,
              font=FONT_S, fill=color)

    timer = format_elapsed(elapsed)
    draw.text(((WIDTH - FONT_TIMER.getlength(timer)) / 2, 30), timer,
              font=FONT_TIMER, fill=FG)
    label = f"cycle {cycle}"
    draw.text(((WIDTH - FONT_M.getlength(label)) / 2, 76), label,
              font=FONT_M, fill=DIM)

    draw.line((8, 100, WIDTH - 8, 100), fill=BAR, width=1)
    y = 106
    for line in log_lines[-LOG_LINES:]:
        tint = ERR if "ERROR" in line else DIM
        draw.text((8, y), _ellipsize(line, FONT_S, WIDTH - 16),
                  font=FONT_S, fill=tint)
        y += 15

    _hint(draw, "buttons locked" if locked
          else ("KEY1 resume  hold=reset" if paused
                else "KEY1 pause  hold=reset  KEY2 back"))
    return image


def message_screen(title: str, body: str = "", color=FG) -> Image.Image:
    """Splash / fatal error screen."""
    image, draw = _blank()
    _header(draw, "E-PAPER DEMO")
    draw.text((8, 90), _ellipsize(title, FONT_L, WIDTH - 16),
              font=FONT_L, fill=color)
    if body:
        draw.text((8, 120), _ellipsize(body, FONT_S, WIDTH - 16),
                  font=FONT_S, fill=DIM)
    return image
