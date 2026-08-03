import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image

from ui import render
from ui.config import HEIGHT, WIDTH
from ui.display import PngDisplay, to_rgb565
from ui.patterns import PATTERNS


def test_format_elapsed_rolls_over_into_hours():
    assert render.format_elapsed(0) == "00:00:00"
    assert render.format_elapsed(59.9) == "00:00:59"
    assert render.format_elapsed(61) == "00:01:01"
    assert render.format_elapsed(3725) == "01:02:05"
    assert render.format_elapsed(-5) == "00:00:00"   # clock skew must not crash


def test_screens_render_at_panel_resolution():
    for image in (
        render.menu_screen(PATTERNS, 0, "/dev/ttyACM0"),
        render.running_screen("WAVE", 12.0, 3, ["09:00:00 start"]),
        render.message_screen("stopped"),
    ):
        assert image.size == (WIDTH, HEIGHT)
        assert image.mode == "RGB"


def test_menu_scrolls_to_keep_the_selection_visible():
    # A selection past the visible window must change what is drawn,
    # otherwise the cursor would run off the bottom of the screen.
    first = render.menu_screen(PATTERNS, 0, None)
    last = render.menu_screen(PATTERNS, len(PATTERNS) - 1, None)
    assert first.tobytes() != last.tobytes()


def test_long_log_lines_are_truncated_not_wrapped():
    long_line = "09:00:00 " + "x" * 200
    image = render.running_screen("WAVE", 1.0, 1, [long_line])
    assert image.size == (WIDTH, HEIGHT)


def test_error_state_paints_differently():
    ok = render.running_screen("WAVE", 5.0, 1, ["09:00:00 cycle 1 shown"])
    bad = render.running_screen("WAVE", 5.0, 1, ["09:00:00 ERROR no ACK"],
                                error="no ACK")
    assert ok.tobytes() != bad.tobytes()


def test_rgb565_packs_two_big_endian_bytes_per_pixel():
    image = Image.new("RGB", (2, 1))
    image.putpixel((0, 0), (255, 0, 0))
    image.putpixel((1, 0), (0, 0, 255))
    assert to_rgb565(image) == b"\xf8\x00\x00\x1f"


def test_png_backend_writes_latest_and_history(tmp_path):
    display = PngDisplay(tmp_path, keep_history=True)
    display.show(render.message_screen("a"))
    display.show(render.message_screen("b"))
    assert (tmp_path / "latest.png").exists()
    assert (tmp_path / "frame_0000.png").exists()
    assert (tmp_path / "frame_0001.png").exists()
    assert display.frames == 2
