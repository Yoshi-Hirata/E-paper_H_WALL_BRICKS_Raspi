"""Screen rendering: state in, 240x240 PIL image out.

Pure drawing code with no hardware or timing dependencies, so screens can
be snapshot-tested and previewed as PNGs long before the LCD arrives.
"""

from __future__ import annotations

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


def _header(draw: ImageDraw.ImageDraw, text: str, color=ACCENT,
            status: str | None = None, status_color=DIM,
            host: str | None = None) -> None:
    """Top strip: title left; on the right the status word and, before
    it, the unit's hostname so ten identical appliances tell apart."""
    draw.rectangle((0, 0, WIDTH, 24), fill=BAR)
    right = WIDTH - 8
    if status:
        right -= FONT_S.getlength(status)
        draw.text((right, 6), status, font=FONT_S, fill=status_color)
        right -= 10
    if host:
        right -= FONT_S.getlength(host)
        draw.text((right, 6), host, font=FONT_S, fill=DIM)
        right -= 10
    draw.text((8, 4), _ellipsize(text, FONT_M, right - 8), font=FONT_M,
              fill=color)


def _hint(draw: ImageDraw.ImageDraw, text: str) -> None:
    """Bottom strip. Hints are written to fit the Radxa's DejaVu 12px
    (wider than the Windows preview font); one that still does not fit
    is ellipsized rather than clipped at the edge."""
    draw.rectangle((0, HEIGHT - 20, WIDTH, HEIGHT), fill=BAR)
    draw.text((8, HEIGHT - 18), _ellipsize(text, FONT_S, WIDTH - 16),
              font=FONT_S, fill=DIM)


def _ellipsize(text: str, font, max_width: int) -> str:
    if font.getlength(text) <= max_width:
        return text
    while text and font.getlength(text + "\u2026") > max_width:
        text = text[:-1]
    return text + "\u2026"


def _wrap(text: str, font, max_width: int) -> list[str]:
    """Break `text` into lines that fit `max_width`, at spaces where
    possible; a single word wider than the line is cut mid-word rather
    than lost. The result reads in full where _ellipsize would end in
    an ellipsis."""
    lines: list[str] = []
    for paragraph in text.split("\n"):
        current = ""
        for word in paragraph.split(" "):
            candidate = f"{current} {word}" if current else word
            if font.getlength(candidate) <= max_width:
                current = candidate
                continue
            if current:
                lines.append(current)
            while word and font.getlength(word) > max_width:
                cut = len(word)
                while cut > 1 and font.getlength(word[:cut]) > max_width:
                    cut -= 1
                lines.append(word[:cut])
                word = word[cut:]
            current = word
        lines.append(current)
    return lines or [""]


def menu_screen(patterns, selected: int, port: str | None = None,
                locked: bool = False, status: str = "",
                host: str | None = None) -> Image.Image:
    """Pattern chooser. Up/Down move, KEY1 starts.

    The hostname is the title: on a wall of ten units it is the one
    thing an operator needs to read off the menu."""
    image, draw = _blank()
    _header(draw, host or "E-PAPER DEMO",
            status="LOCKED" if locked else None, status_color=ACCENT)

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
    # The standby status displaces the port line while the panels are
    # being blanked - both are one-line context, and there is room for one.
    if status:
        second, tint = status, (ERR if status.startswith("ERROR") else DIM)
    elif port:
        second, tint = f"port {port}", DIM
    else:
        second, tint = "port: not found", ERR
    draw.text((8, 202), _ellipsize(second, FONT_S, WIDTH - 16),
              font=FONT_S, fill=tint)
    _hint(draw, "buttons locked" if locked
          else "UP/DOWN sel  KEY1 start  KEY3 off")
    return image


def running_screen(pattern_label: str, elapsed: float, cycle: int,
                   log_lines: list[str], error: str | None = None,
                   stopping: bool = False, paused: bool = False,
                   locked: bool = False,
                   caption: str | None = None,
                   host: str | None = None) -> Image.Image:
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
    _header(draw, pattern_label, status=status, status_color=color,
            host=host)

    timer = format_elapsed(elapsed)
    draw.text(((WIDTH - FONT_TIMER.getlength(timer)) / 2, 30), timer,
              font=FONT_TIMER, fill=FG)
    # The pattern's caption (e.g. the color on the glass right now) earns
    # the bright type; the cycle count stays secondary next to it.
    label = f"cycle {cycle}"
    if caption:
        label += f" - {caption}"
    label = _ellipsize(label, FONT_M, WIDTH - 16)
    draw.text(((WIDTH - FONT_M.getlength(label)) / 2, 76), label,
              font=FONT_M, fill=FG if caption else DIM)

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


_UPDATE_STATUS = {
    "idle": ("READY", ACCENT),
    "flashing": ("FLASHING", OK),
    "verifying": ("VERIFY", OK),
    "done": ("DONE", OK),
    "failed": ("FAILED", ERR),
}


def update_screen(firmware: str, size: int, addr: int, phase: str,
                  board_state: str, done: int, log_lines: list[str],
                  error: str | None = None,
                  locked: bool = False,
                  host: str | None = None) -> Image.Image:
    """Firmware update: image, target board, transfer bar, log tail.

    `phase` is one of ui.updater's IDLE/FLASHING/VERIFYING/DONE/FAILED.
    """
    image, draw = _blank()
    status, color = _UPDATE_STATUS.get(phase, (phase.upper(), DIM))
    _header(draw, "FW UPDATE", status=status, status_color=color, host=host)

    draw.text((8, 32), "image", font=FONT_S, fill=DIM)
    draw.text((56, 30), _ellipsize(firmware, FONT_M, WIDTH - 64),
              font=FONT_M, fill=FG)
    draw.text((8, 52), "board", font=FONT_S, fill=DIM)
    draw.text((56, 46), f"{addr:02d}", font=FONT_L, fill=FG)
    tint = ERR if board_state.startswith(("ERROR", "no ")) else DIM
    draw.text((92, 52), _ellipsize(board_state, FONT_S, WIDTH - 100),
              font=FONT_S, fill=tint)

    # Transfer bar: the whole image, filled as chunks are acknowledged.
    top, bottom = 76, 90
    draw.rectangle((8, top, WIDTH - 8, bottom), outline=DIM, width=1)
    fill_w = int((WIDTH - 18) * (done / size)) if size else 0
    if fill_w > 0:
        draw.rectangle((9, top + 1, 9 + fill_w, bottom - 1),
                       fill=ERR if phase == "failed" else OK)
    pct = f"{done * 100 // size}%" if size else "-"
    label = f"{done}/{size} B  {pct}"
    draw.text((8, 92), label, font=FONT_S, fill=DIM)
    if error:
        draw.text((WIDTH - 8 - FONT_S.getlength("see log"), 92), "see log",
                  font=FONT_S, fill=ERR)

    draw.line((8, 108, WIDTH - 8, 108), fill=BAR, width=1)
    y = 112
    for line in log_lines[-LOG_LINES:]:
        tint = ERR if "ERROR" in line else DIM
        draw.text((8, y), _ellipsize(line, FONT_S, WIDTH - 16),
                  font=FONT_S, fill=tint)
        y += 15

    if locked:
        hint = "buttons locked"
    elif phase in ("flashing", "verifying"):
        hint = "updating - do not unplug"
    elif phase in ("done", "failed"):
        hint = "KEY1 again  KEY2 menu"
    else:
        hint = "UP/DOWN  KEY1 flash  KEY2 back"
    _hint(draw, hint)
    return image


_PULL_STATUS = {
    "idle": ("READY", ACCENT),
    "pulling": ("PULLING", OK),
    "failed": ("FAILED", ERR),
}


def pull_screen(before: str, after: str | None, phase: str,
                log_lines: list[str], error: str | None = None,
                changed: bool = False, locked: bool = False,
                host: str | None = None) -> Image.Image:
    """Repo update: the commit now, the commit after the pull, log tail.

    `phase` is one of ui.puller's IDLE/PULLING/DONE/FAILED; `changed`
    says whether the DONE pull moved HEAD (then KEY1 restarts the UI).
    """
    image, draw = _blank()
    if phase == "done":
        status, color = ("UPDATED", OK) if changed else ("UP TO DATE", DIM)
    else:
        status, color = _PULL_STATUS.get(phase, (phase.upper(), DIM))
    _header(draw, "GIT PULL", status=status, status_color=color, host=host)

    draw.text((8, 32), "now", font=FONT_S, fill=DIM)
    draw.text((44, 30), _ellipsize(before, FONT_M, WIDTH - 52),
              font=FONT_M, fill=FG)
    draw.text((8, 54), "new", font=FONT_S, fill=DIM)
    draw.text((44, 52), _ellipsize(after or "-", FONT_M, WIDTH - 52),
              font=FONT_M, fill=OK if changed else DIM)
    if error:
        draw.text((8, 76), _ellipsize(f"ERROR {error}", FONT_S, WIDTH - 16),
                  font=FONT_S, fill=ERR)
    elif changed:
        draw.text((8, 76), "restart the UI to run the new code",
                  font=FONT_S, fill=OK)

    draw.line((8, 94, WIDTH - 8, 94), fill=BAR, width=1)
    y = 98
    for line in log_lines[-LOG_LINES:]:
        tint = ERR if "ERROR" in line else DIM
        draw.text((8, y), _ellipsize(line, FONT_S, WIDTH - 16),
                  font=FONT_S, fill=tint)
        y += 15

    if locked:
        hint = "buttons locked"
    elif phase == "pulling":
        hint = "pulling - please wait"
    elif phase == "done" and changed:
        hint = "KEY1 restart UI  KEY2 menu"
    elif phase in ("done", "failed"):
        hint = "KEY1 pull again  KEY2 menu"
    else:
        hint = "KEY1 pull  KEY2 back"
    _hint(draw, hint)
    return image


_REMOTE_STATUS = {
    "preparing": ("LOADING", ACCENT),
    "ready": ("READY", OK),
    "armed": ("ARMED", OK),
    "fired": ("FIRED", OK),
    "failed": ("FAILED", ERR),
    "standby": ("STANDBY", DIM),
    "idle": ("IDLE", DIM),
}


def remote_screen(status: dict, log_lines: list[str], now: float = 0.0,
                  locked: bool = False,
                  host: str | None = None) -> Image.Image:
    """The unit under the show PC: what is loaded and when it fires.

    `status` is ui.remote.RemoteSession.status(); `now` is the monotonic
    clock its fire time is written in.
    """
    image, draw = _blank()
    word, color = _REMOTE_STATUS.get(status["phase"],
                                     (status["phase"].upper(), DIM))
    _header(draw, "REMOTE", status=word, status_color=color, host=host)

    label = status["label"] or status["cue"] or "waiting for a cue"
    draw.text((8, 30), _ellipsize(label, FONT_L, WIDTH - 16), font=FONT_L,
              fill=FG)

    wanted = len(status["boards"])
    if status["phase"] == "standby":
        line = ("white, boards " if status["standby_ready"]
                else "blanking, boards ") + f"{len(status['live'])}/{wanted}"
        tint = DIM
    elif status["phase"] == "preparing":
        line, tint = f"writing {wanted} boards...", DIM
    else:
        line = f"boards {len(status['saved'])}/{wanted} loaded"
        tint = ERR if status["failed"] else OK
        if status["failed"]:
            line += f"  ({len(status['failed'])} failed)"
    draw.text((8, 58), _ellipsize(line, FONT_M, WIDTH - 16), font=FONT_M,
              fill=tint)

    if status["late_ms"] is not None:
        when, tint = f"fired {status['late_ms']:+.0f} ms", OK
    elif status["fire_at"] is not None:
        left = max(0.0, status["fire_at"] - now)
        when, tint = f"fires in {left:.0f} s", ACCENT
    else:
        when, tint = "no fire time yet", DIM
    draw.text((8, 80), when, font=FONT_M, fill=tint)
    if status["error"]:
        draw.text((8, 100), _ellipsize(f"ERROR {status['error']}", FONT_S,
                                       WIDTH - 16), font=FONT_S, fill=ERR)

    draw.line((8, 118, WIDTH - 8, 118), fill=BAR, width=1)
    y = 122
    for line in log_lines[-LOG_LINES:]:
        tint = ERR if "ERROR" in line else DIM
        draw.text((8, y), _ellipsize(line, FONT_S, WIDTH - 16),
                  font=FONT_S, fill=tint)
        y += 15

    _hint(draw, "buttons locked" if locked else "KEY2 local menu  KEY3 off")
    return image


_REBOOT_STATUS = {
    "idle": ("CONFIRM", ACCENT),
    "rebooting": ("REBOOTING", OK),
    "failed": ("FAILED", ERR),
}


def reboot_screen(phase: str, log_lines: list[str],
                  error: str | None = None, locked: bool = False,
                  host: str | None = None) -> Image.Image:
    """Reboot confirm: which unit, what happens, KEY1 held to go.

    `phase` is one of ui.rebooter's IDLE/REBOOTING/FAILED. The hostname
    is repeated large because a reboot on the wrong one of ten identical
    units is the mistake this screen exists to prevent.
    """
    image, draw = _blank()
    status, color = _REBOOT_STATUS.get(phase, (phase.upper(), DIM))
    _header(draw, "REBOOT", status=status, status_color=color, host=host)

    draw.text((8, 34), _ellipsize(host or "this unit", FONT_L, WIDTH - 16),
              font=FONT_L, fill=FG)
    if phase == "rebooting":
        body, tint = "Going down now. The UI is back in about a minute.", OK
    elif phase == "failed":
        body, tint = f"Not rebooted: {error or 'unknown error'}", ERR
    else:
        body = ("Restart the whole unit? The panels are set to standby "
                "(white) when the UI comes back.")
        tint = DIM
    y = 66
    for line in _wrap(body, FONT_S, WIDTH - 16)[:4]:
        draw.text((8, y), line, font=FONT_S, fill=tint)
        y += 15

    draw.line((8, 132, WIDTH - 8, 132), fill=BAR, width=1)
    y = 136
    for line in log_lines[-LOG_LINES + 2:]:
        tint = ERR if "ERROR" in line else DIM
        draw.text((8, y), _ellipsize(line, FONT_S, WIDTH - 16),
                  font=FONT_S, fill=tint)
        y += 15

    if locked:
        hint = "buttons locked"
    elif phase == "rebooting":
        hint = "rebooting - please wait"
    elif phase == "failed":
        hint = "hold KEY1 retry  KEY2 menu"
    else:
        hint = "hold KEY1 = reboot  KEY2 back"
    _hint(draw, hint)
    return image


# Scroll step floor for the FW VERSION list: the fewest board rows that
# fit a page when every label wraps onto two lines. One-line labels fit
# ten, so the last page simply shows more.
VERSION_ROWS = 5
_VERSION_LINE_H = 15


def versions_screen(rows: list[tuple[int, str]], status: str, phase: str,
                    bundled: str, offset: int = 0, locked: bool = False,
                    host: str | None = None) -> Image.Image:
    """Firmware inventory: one row per answering board, addr + label.

    `phase` is one of ui.versions' IDLE/SCANNING/DONE; `rows` beyond
    VERSION_ROWS scroll with `offset`.
    """
    image, draw = _blank()
    if phase == "scanning":
        word, color = "SCANNING", OK
    elif phase == "done":
        word, color = "DONE", DIM
    else:
        word, color = "READY", ACCENT
    _header(draw, "FW VERSION", status=word, status_color=color, host=host)

    # Everything below flows: the status and every row wrap onto as
    # many lines as they need, so the verdict is readable in full.
    tint = ERR if status.startswith(("ERROR", "no ", "port ")) else DIM
    y = 30
    for line in _wrap(status, FONT_S, WIDTH - 16)[:2]:
        draw.text((8, y), line, font=FONT_S, fill=tint)
        y += 14
    draw.text((8, y), _ellipsize(f"bundled: {bundled}", FONT_S, WIDTH - 16),
              font=FONT_S, fill=DIM)
    y += 16
    draw.line((8, y, WIDTH - 8, y), fill=BAR, width=1)
    y += 4

    footer_h = 14
    bottom = HEIGHT - 22 - footer_h
    shown = 0
    for addr, label in rows[offset:]:
        lines = _wrap(label, FONT_S, WIDTH - 42)
        if y + _VERSION_LINE_H * len(lines) > bottom:
            break
        draw.text((8, y), f"{addr:02d}", font=FONT_S, fill=FG)
        # A board on the bundled image (or recorded as flashed with it)
        # is the good case; anything else is what the operator wants
        # to see.
        fill = OK if bundled in label else FG
        for line in lines:
            draw.text((34, y), line, font=FONT_S, fill=fill)
            y += _VERSION_LINE_H
        y += 2
        shown += 1
    if offset > 0 or offset + shown < len(rows):
        more = f"rows {offset + 1}-{offset + shown} of {len(rows)}"
        draw.text((WIDTH - 8 - FONT_S.getlength(more), HEIGHT - 22 - footer_h),
                  more, font=FONT_S, fill=DIM)

    if locked:
        hint = "buttons locked"
    elif phase == "scanning":
        hint = "scanning - please wait"
    else:
        hint = "UP/DOWN  KEY1 rescan  KEY2 menu"
    _hint(draw, hint)
    return image


def message_screen(title: str, body: str = "", color=FG,
                   host: str | None = None) -> Image.Image:
    """Splash / fatal error screen."""
    image, draw = _blank()
    _header(draw, host or "E-PAPER DEMO")
    draw.text((8, 90), _ellipsize(title, FONT_L, WIDTH - 16),
              font=FONT_L, fill=color)
    if body:
        draw.text((8, 120), _ellipsize(body, FONT_S, WIDTH - 16),
                  font=FONT_S, fill=DIM)
    return image
