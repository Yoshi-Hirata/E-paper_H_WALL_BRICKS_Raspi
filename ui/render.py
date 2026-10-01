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
WARN = (255, 190, 80)        # amber: worth a look, not a fault
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
        # Built-in patterns keep to <=14 chars by convention, but a demo's
        # name is only capped on the way in (ui/demos.py's MAX_NAME_LEN,
        # server-side too) - ellipsize rather than let a long one run
        # into the row's right edge or the selection highlight.
        draw.text((14, y), _ellipsize(patterns[index].label, FONT_M,
                                      WIDTH - 14 - 8), font=FONT_M,
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


def usb_board_line(usb_board: dict, shared: bool = False
                   ) -> "tuple[str, tuple]":
    """'SERIAL 5CF26F473930  TYPE 3930' and its tint: amber for a type
    other than the fleet's usual 324C, red when there is no serial.

    `shared` (the scan found several boards - the 485 is in): the serial
    is still only the board on the USB cable, while the target comes from
    the scan, so it is named as that and nothing more -
    'USB BOARD 5CF2…3930', dim (review of 59fbded, LOW-3)."""
    serial = usb_board.get("serial")
    family = usb_board.get("family")
    if not serial:
        return "SERIAL none - no USB serial", ERR
    if shared:
        short = serial if len(serial) <= 9 else f"{serial[:4]}…{serial[-4:]}"
        return f"USB BOARD {short}", DIM
    return (f"SERIAL {serial}  TYPE {family}",
            DIM if family == "324C" else WARN)


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
                  host: str | None = None,
                  image_choice: str = "",
                  usb_board: "dict | None" = None,
                  bus_shared: bool = False) -> Image.Image:
    """Firmware update: image, target board, transfer bar, log tail.

    `phase` is one of ui.updater's IDLE/FLASHING/VERIFYING/DONE/FAILED.
    `image_choice` ("2/3") says there are other builds: LEFT/RIGHT.
    `usb_board` (transport.usb_board_info, read at the scan) puts the USB
    board's SERIAL and TYPE on a line of its own under the bar, so the
    operator sees which board is about to be written before KEY1.
    """
    image, draw = _blank()
    status, color = _UPDATE_STATUS.get(phase, (phase.upper(), DIM))
    _header(draw, "FW UPDATE", status=status, status_color=color, host=host)

    draw.text((8, 32), "image", font=FONT_S, fill=DIM)
    choice = f"< {image_choice} >" if image_choice else ""
    choice_w = int(FONT_S.getlength(choice)) + 6 if choice else 0
    draw.text((56, 30), _ellipsize(firmware, FONT_M, WIDTH - 64 - choice_w),
              font=FONT_M, fill=FG)
    if choice:
        draw.text((WIDTH - 8 - choice_w + 6, 32), choice, font=FONT_S, fill=DIM)
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

    top = 108
    if usb_board is not None:
        text, tint = usb_board_line(usb_board, shared=bus_shared)
        draw.text((8, 106), _ellipsize(text, FONT_S, WIDTH - 16),
                  font=FONT_S, fill=tint)
        top = 122
    draw.line((8, top, WIDTH - 8, top), fill=BAR, width=1)
    y = top + 4
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
                  host: str | None = None, title: str = "REMOTE",
                  hint: "str | None" = None) -> Image.Image:
    """The unit under the show PC: what is loaded and when it fires.

    `status` is ui.remote.RemoteSession.status(); `now` is the monotonic
    clock its fire time is written in. A standalone demo (ui/app.py's
    Screen.DEMO) reuses this screen with its own `title` ("DEMO <name>")
    and `hint` (KEY1 does nothing, KEY2 stops it) - everything else about
    what is loaded and when it fires reads exactly the same.
    """
    image, draw = _blank()
    word, color = _REMOTE_STATUS.get(status["phase"],
                                     (status["phase"].upper(), DIM))
    _header(draw, title, status=word, status_color=color, host=host)

    label = status["label"] or status["cue"] or "waiting for a cue"
    draw.text((8, 30), _ellipsize(label, FONT_L, WIDTH - 16), font=FONT_L,
              fill=FG)
    show = status.get("show")
    if show:
        # The show's own clock and what comes next - what an operator
        # backstage wants to read off a garment at a glance.
        if show["state"] == "running" and show["now"] is not None:
            clock = format_elapsed(show["now"])[3:] if show["now"] >= 0 \
                else f"-{format_elapsed(-show['now'])[3:]}"
            line = f"SHOW {clock}"
            if show["next"]:
                line += f"  next in {show['next']['in_s']:.0f} s"
            if not show["synced"]:
                line += "  (unsynced)"
        else:
            line = f"SHOW {show['state'].upper()}  {show['cues']} cues"
        draw.text((8, 100), _ellipsize(line, FONT_S, WIDTH - 16),
                  font=FONT_S, fill=ACCENT)

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
        draw.text((8, 114), _ellipsize(f"ERROR {status['error']}", FONT_S,
                                       WIDTH - 16), font=FONT_S, fill=ERR)

    draw.line((8, 130, WIDTH - 8, 130), fill=BAR, width=1)
    y = 134
    for line in log_lines[-LOG_LINES:]:
        tint = ERR if "ERROR" in line else DIM
        draw.text((8, y), _ellipsize(line, FONT_S, WIDTH - 16),
                  font=FONT_S, fill=tint)
        y += 15

    _hint(draw, hint if hint is not None else
          ("buttons locked" if locked else "KEY2 local menu  KEY3 off"))
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


_TONE = {"": FG, "warn": WARN, "err": ERR}


def boardinfo_screen(lines: "list[tuple[str, str, str]]", reading: bool,
                     usb_line: str = "", locked: bool = False,
                     host: str | None = None) -> Image.Image:
    """BOARD INFO: the board on the USB cable (ui/boardinfo.py).

    `lines` is BoardInfo.lines(): (key, value, tone) - the key in the left
    column, the value wrapped to as many lines as it needs so it reads in
    full ("3930 (differs from most: 324C)"); a key of "" gives the value
    the whole width. `usb_line` is the small print above the hint strip.
    """
    image, draw = _blank()
    _header(draw, "BOARD INFO", status="READING" if reading else None,
            status_color=OK, host=host)
    top, bottom, key_w = 32, HEIGHT - 42, 62

    def layout(font, line_h, gap):
        placed, y = [], top
        for key, value, tone in lines:
            width = WIDTH - 16 - (key_w if key else 0)
            wrapped = _wrap(value, font, width)
            placed.append((y, key, wrapped, tone))
            y += line_h * len(wrapped) + gap
        return placed, y - gap

    # The values in the 15 px face when they fit (they do on the Radxa's
    # DejaVu with every text BoardInfo writes, at two lines apiece), the
    # 12 px face when a longer one would push the last row off the screen.
    font, line_h = FONT_M, 18
    placed, end = layout(font, line_h, 6)
    if end > bottom:
        font, line_h = FONT_S, 15
        placed, end = layout(font, line_h, 4)
    for y, key, wrapped, tone in placed:
        if key:
            draw.text((8, y + (3 if font is FONT_M else 0)), key,
                      font=FONT_S, fill=DIM)
        x = 8 + key_w if key else 8
        for line in wrapped:
            if y + line_h > bottom + 4:
                break
            draw.text((x, y), line, font=font, fill=_TONE.get(tone, FG))
            y += line_h
    if usb_line:
        draw.text((8, HEIGHT - 38), _ellipsize(usb_line, FONT_S, WIDTH - 16),
                  font=FONT_S, fill=DIM)
    if locked:
        hint = "buttons locked"
    elif reading:
        hint = "reading FW - please wait"
    else:
        hint = "KEY1 read again  KEY2 menu"
    _hint(draw, hint)
    return image


_WIFI_STATUS = {
    "idle": ("READY", ACCENT),
    "connecting": ("CONNECTING", OK),
    "done": ("DONE", OK),
    "failed": ("FAILED", ERR),
    "locked": ("LOCKED", WARN),
}
WIFI_ROWS = 5                # profile rows that fit between now and status


def wifi_screen(ssid: str, info: str, rows: "list[tuple[str, str, bool]]",
                choice: int, phase: str, status: str = "",
                locked: bool = False, host: str | None = None) -> Image.Image:
    """WIFI: the network now, the unit's wireless profiles (ui/wifi.py).

    `ssid`/`info` are Wifi.current() - what the unit is on and 'IP ...
    72%  client'; `rows` is Wifi.rows(): (name, mode, active), the active
    one marked and `choice` highlighted like a menu row; `phase` is one of
    ui.wifi's IDLE/CONNECTING/DONE/FAILED/LOCKED and `status` the line
    under the list (Wifi.status_text()), red when it starts with ERROR.
    """
    image, draw = _blank()
    word, color = _WIFI_STATUS.get(phase, (phase.upper(), DIM))
    _header(draw, "WIFI", status=word, status_color=color, host=host)

    connected = ssid not in ("not connected", "reading...")
    draw.text((8, 30), _ellipsize(ssid, FONT_M, WIDTH - 16), font=FONT_M,
              fill=FG if connected else DIM)
    draw.text((8, 50), _ellipsize(info, FONT_S, WIDTH - 16), font=FONT_S,
              fill=ERR if info.startswith("nmcli:") else DIM)
    draw.line((8, 68, WIDTH - 8, 68), fill=BAR, width=1)

    # The profiles, scrolled so the cursor stays on the screen.
    row_h = 20
    first = max(0, min(choice - WIFI_ROWS // 2, len(rows) - WIFI_ROWS))
    if not rows:
        draw.text((8, 76), "no wireless profiles", font=FONT_S, fill=DIM)
    for row, index in enumerate(range(first, min(first + WIFI_ROWS, len(rows)))):
        y = 74 + row * row_h
        name, mode, active = rows[index]
        chosen = index == choice
        if chosen:
            draw.rectangle((4, y - 2, WIDTH - 4, y + row_h - 4), fill=SELECT)
            draw.rectangle((4, y - 2, 7, y + row_h - 4), fill=ACCENT)
        draw.text((14, y), "●" if active else "", font=FONT_S,
                  fill=OK if active else DIM)
        tag = f"  {mode}" if mode and mode != "client" else ""
        tag_w = int(FONT_S.getlength(tag)) if tag else 0
        draw.text((28, y), _ellipsize(name, FONT_M, WIDTH - 28 - 8 - tag_w),
                  font=FONT_M, fill=FG if chosen else DIM)
        if tag:
            draw.text((WIDTH - 8 - tag_w, y + 2), tag, font=FONT_S,
                      fill=WARN if mode == "hotspot" else DIM)

    if status:
        if status.startswith("ERROR"):
            tint = ERR
        elif phase == "locked":
            tint = WARN
        elif phase == "done":
            tint = OK
        else:
            tint = DIM
        y = 176
        for line in _wrap(status, FONT_S, WIDTH - 16)[:3]:
            draw.text((8, y), line, font=FONT_S, fill=tint)
            y += 14

    if locked:
        hint = "buttons locked"
    elif phase == "connecting":
        hint = "connecting - KEY2 menu"
    else:
        hint = "hold KEY1 = switch  KEY2 back"
    _hint(draw, hint)
    return image


_EXHIBITION_STATUS = {
    "idle": ("READY", ACCENT),
    "sending": ("SENDING", OK),
    "done": ("DONE", OK),
    "failed": ("FAILED", ERR),
}


def exhibition_screen(available: "bool | None", show: "tuple[str, str]",
                      run: str, fleet: str, loop: str, speaker: str,
                      phase: str, status: str = "", active: bool = False,
                      locked: bool = False, host: str | None = None,
                      volume_keys: bool = False) -> Image.Image:
    """EXHIBITION: the Conductor on this unit, run from the HAT
    (ui/exhibition.py).

    `available` is whether a Conductor answers on 127.0.0.1 (None: not
    asked yet); `show` is Exhibition.show_lines() - the timeline's name
    and `N cues · m:ss`; `run`, `fleet`, `loop`, `speaker` are the four
    state lines (run_text() and friends); `phase` is IDLE/SENDING/DONE/
    FAILED and `status` the line under them (status_text(), red when it
    starts with ERROR). `active` picks the KEY1 hint: STOP while a run
    or its countdown exists, START otherwise; `volume_keys` adds the
    `< > volume` hint (Exhibition.volume_supported(): the Conductor has
    /api/speaker/volume - not merely a speaker).
    """
    image, draw = _blank()
    if available is False:
        word, color = "MISSING", ERR
    elif available is None:
        word, color = "CHECKING", DIM
    else:
        word, color = _EXHIBITION_STATUS.get(phase, (phase.upper(), DIM))
    _header(draw, "EXHIBITION", status=word, status_color=color, host=host)

    if not available:
        # Without a Conductor here there is nothing to run: say so and
        # offer the way back. (The row still reads "(no conductor)" on
        # the menu, so this is only reached on purpose.)
        title = ("checking for a conductor…" if available is None
                 else "no conductor on this unit")
        draw.text((8, 40), _ellipsize(title, FONT_M, WIDTH - 16),
                  font=FONT_M, fill=DIM if available is None else WARN)
        y = 66
        for line in _wrap("EXHIBITION needs the Conductor service running "
                          "here (127.0.0.1:8765). On radxa-05 it starts "
                          "with the unit; elsewhere run the show from "
                          "the PC.", FONT_S, WIDTH - 16)[:5]:
            draw.text((8, y), line, font=FONT_S, fill=DIM)
            y += 14
        if status:
            y += 6
            for line in _wrap(status, FONT_S, WIDTH - 16)[:2]:
                draw.text((8, y), line, font=FONT_S, fill=ERR)
                y += 14
        _hint(draw, "buttons locked" if locked else "KEY2 back")
        return image

    name, detail = show
    draw.text((8, 30), _ellipsize(name or "show", FONT_M, WIDTH - 16),
              font=FONT_M, fill=FG)
    draw.text((8, 50), _ellipsize(detail, FONT_S, WIDTH - 16), font=FONT_S,
              fill=WARN if "not uploaded" in detail else DIM)
    draw.line((8, 68, WIDTH - 8, 68), fill=BAR, width=1)

    # The run: bright while something is happening, quiet when idle.
    if run.startswith("countdown") or run.startswith("hold"):
        tint = WARN
    elif run.endswith("running") or run.startswith("next run"):
        tint = OK
    elif run.startswith("ended"):
        tint = ACCENT
    else:
        tint = DIM
    draw.text((8, 76), _ellipsize(run or "…", FONT_L, WIDTH - 16),
              font=FONT_L, fill=tint)

    online = fleet.split()[1] if fleet.startswith("units ") else ""
    seen, _, total = online.partition("/")
    if seen and total and seen == total and seen != "0":
        fleet_tint = DIM
    elif seen == "0":
        fleet_tint = ERR
    else:
        fleet_tint = WARN
    draw.text((8, 104), _ellipsize(fleet, FONT_S, WIDTH - 16), font=FONT_S,
              fill=fleet_tint)
    draw.text((8, 120), _ellipsize(loop, FONT_S, WIDTH - 16), font=FONT_S,
              fill=OK if loop == "LOOP on" else DIM)
    if speaker.startswith("speaker LOST"):
        speaker_tint = ERR          # the Bluetooth link is down (SPEAKER row)
    elif speaker.startswith("no speaker"):
        speaker_tint = WARN
    else:
        speaker_tint = DIM
    draw.text((8, 136), _ellipsize(speaker, FONT_S, WIDTH - 16), font=FONT_S,
              fill=speaker_tint)

    if status:
        if status.startswith("ERROR"):
            tint = ERR
        elif phase == "done":
            tint = OK
        else:
            tint = DIM
        y = 158
        for line in _wrap(status, FONT_S, WIDTH - 16)[:3]:
            draw.text((8, y), line, font=FONT_S, fill=tint)
            y += 14

    keys = "hold KEY3 = LOOP on/off"
    if volume_keys:
        keys = "hold KEY3 = LOOP   < > volume"
    draw.text((8, 204), _ellipsize(keys, FONT_S, WIDTH - 16), font=FONT_S,
              fill=DIM)
    if locked:
        hint = "buttons locked"
    elif phase == "sending":
        hint = "sending - KEY2 menu"
    elif run.startswith("next run"):
        # LOOP between runs: STOP cancels the restart the Conductor
        # has pending, which is the thing the operator must know.
        hint = "hold KEY1 = STOP (no next run)"
    elif active:
        hint = "hold KEY1 = STOP  KEY2 back"
    else:
        hint = "hold KEY1 = START  KEY2 back"
    _hint(draw, hint)
    return image


_SPEAKER_STATUS = {
    "READY": ACCENT, "BUSY": OK, "DONE": OK, "FAILED": ERR,
}
_SPEAKER_MODE_TEXT = {
    "checking": ("checking for a conductor…", ""),
    "missing": ("no conductor on this unit",
                "SPEAKER needs the Conductor service running here "
                "(127.0.0.1:8765), the one that plays the music."),
    "none": ("no speaker on this conductor",
             "The Conductor here runs without --speaker: it plays no "
             "music, so there is nothing to connect. Start it with "
             "--speaker to use this screen."),
    "old": ("speaker ?",
            "This Conductor reports a speaker but not its Bluetooth "
            "link: connect and pair need a newer Conductor (git pull "
            "on this unit)."),
}


def speaker_screen(mode: str, word: str, device: str, state: str,
                   detail: "tuple[str, str]", seen: str, status: str = "",
                   banner: str = "", instruction: str = "",
                   busy: bool = False, volume_keys: bool = False,
                   locked: bool = False, host: str | None = None
                   ) -> Image.Image:
    """SPEAKER: the Bluetooth speaker as the Conductor on this unit sees
    it, and the keys that connect / re-pair it (ui/speaker.py).

    `mode` is Speaker.mode() - "ok", "wired" (not a Bluetooth output:
    the volume alone, no holds), or why the screen is only a note
    (checking / missing / none / old); `word` is the header's READY /
    BUSY / DONE / FAILED; `device`, `state`, `detail` (text, tone) and
    `seen` are lines 1-3 (device_text() and friends - the state's tint
    follows its words: connected green, NOT CONNECTED red, connecting /
    reconnect / pairing amber); `status` is the verdict line (red when
    it starts with ERROR, amber for the "hold again" prompts); `banner`
    is `MUSIC LOST` or ""; `instruction` is the pairing instruction when
    it is due; `busy` picks the hint, `volume_keys` adds `< > volume`.
    """
    image, draw = _blank()
    if mode == "missing":
        color = ERR
        word = "MISSING"
    elif mode == "checking":
        color = DIM
        word = "CHECKING"
    elif mode == "none":
        color = WARN
        word = "NONE"
    else:
        color = _SPEAKER_STATUS.get(word, DIM)
    _header(draw, "SPEAKER", status=word, status_color=color, host=host)

    if mode in _SPEAKER_MODE_TEXT:
        title, body = _SPEAKER_MODE_TEXT[mode]
        draw.text((8, 40), _ellipsize(title, FONT_M, WIDTH - 16),
                  font=FONT_M, fill=DIM if mode == "checking" else WARN)
        y = 66
        for line in _wrap(body, FONT_S, WIDTH - 16)[:5]:
            draw.text((8, y), line, font=FONT_S, fill=DIM)
            y += 14
        if mode == "old" and state:
            # What the old Conductor does say (the EXHIBITION line).
            draw.text((8, y + 6), _ellipsize(state, FONT_S, WIDTH - 16),
                      font=FONT_S, fill=DIM)
            y += 20
        if status:
            y += 6
            tint = ERR if status.startswith(("ERROR", "no conductor")) else DIM
            for line in _wrap(status, FONT_S, WIDTH - 16)[:2]:
                draw.text((8, y), line, font=FONT_S, fill=tint)
                y += 14
        _hint(draw, "buttons locked" if locked else "KEY2 back")
        return image

    # Line 1: the device. Line 2: the state, large, in its colour.
    draw.text((8, 30), _ellipsize(device, FONT_M, WIDTH - 16), font=FONT_M,
              fill=DIM if device.startswith("no speaker") else FG)
    if state.startswith(("NOT CONNECTED", "pairing failed")) or "no sound" in state:
        tint = ERR                  # incl. "connected, no sound output"
    elif state.startswith(("connected", "vol ")):
        tint = OK
    elif state.startswith(("connecting", "pairing", "reconnect")):
        tint = WARN
    else:
        tint = DIM
    draw.text((8, 50), _ellipsize(state or "…", FONT_L, WIDTH - 16),
              font=FONT_L, fill=tint)
    text, tone = detail
    detail_tint = _TONE.get(tone, DIM) if tone else DIM

    if status:
        if status.startswith("ERROR"):
            status_tint = ERR
        elif (status.startswith(("show running", "connecting"))
                or status == "hold KEY3 again = pair"):
            status_tint = WARN
        elif word == "DONE":
            status_tint = OK
        else:
            status_tint = DIM
    if instruction:
        # Pairing: the instruction needs the room. The detail keeps one
        # line, the "last connected" line gives way to the verdict (two
        # lines - it is what asks for the second hold - both above the
        # MUSIC LOST strip at 122; review of 73c8fdc, LOW-6), and the
        # instruction takes up to four lines above the keys - DejaVu
        # 12 px on the Radxa is wider than the preview font, so three
        # may not do.
        if text:
            draw.text((8, 74), _ellipsize(text, FONT_S, WIDTH - 16),
                      font=FONT_S, fill=detail_tint)
        if status:
            y = 88
            for line in _wrap(status, FONT_S, WIDTH - 16)[:2]:
                draw.text((8, y), line, font=FONT_S, fill=status_tint)
                y += 14
    else:
        if text:
            y = 74
            for line in _wrap(text, FONT_S, WIDTH - 16)[:2]:
                draw.text((8, y), line, font=FONT_S, fill=detail_tint)
                y += 14
        draw.text((8, 102), _ellipsize(seen, FONT_S, WIDTH - 16), font=FONT_S,
                  fill=DIM)
    draw.line((8, 118, WIDTH - 8, 118), fill=BAR, width=1)

    if banner:
        # A red strip, black caps: readable from the wall, not the HAT.
        draw.rectangle((8, 122, WIDTH - 8, 142), fill=ERR)
        draw.text(((WIDTH - FONT_M.getlength(banner)) / 2, 124), banner,
                  font=FONT_M, fill=BG)

    if instruction:
        y = 146
        for line in _wrap(instruction, FONT_S, WIDTH - 16)[:4]:
            draw.text((8, y), line, font=FONT_S, fill=WARN)
            y += 14
    elif status:
        y = 146
        for line in _wrap(status, FONT_S, WIDTH - 16)[:3]:
            draw.text((8, y), line, font=FONT_S, fill=status_tint)
            y += 14

    keys = ("< > volume only" if mode == "wired"
            else "hold KEY1 connect · hold KEY3 pair")
    draw.text((8, 204), _ellipsize(keys, FONT_S, WIDTH - 16), font=FONT_S,
              fill=DIM)
    if locked:
        hint = "buttons locked"
    elif busy:
        hint = ("pairing - KEY2 menu" if state.startswith("pairing")
                else "sending - KEY2 menu")
    elif volume_keys:
        hint = "< > volume  KEY2 back"
    else:
        hint = "KEY2 back"
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
