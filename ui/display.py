"""Display backends.

`ST7789Display` talks to the LCD HAT; `PngDisplay` and `NullDisplay` let
the whole UI run (and be reviewed) on a machine with no panel attached.
Hardware libraries are imported lazily so this module stays importable
anywhere.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from .config import (HEIGHT, PIN_BL, PIN_DC, PIN_RST, SPI_BUS, SPI_DEVICE,
                     SPI_HZ, WIDTH)


class Display:
    """Backend interface: push a 240x240 RGB image, then clean up."""

    def show(self, image: Image.Image) -> None:  # pragma: no cover - iface
        raise NotImplementedError

    def sleep(self) -> None:
        """Blank the screen but stay usable - the demo keeps running."""
        self.asleep = True

    def wake(self) -> None:
        self.asleep = False

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class NullDisplay(Display):
    """Discards frames; counts them so tests can assert on redraws."""

    def __init__(self):
        self.frames = 0
        self.last: Image.Image | None = None
        self.asleep = False

    def show(self, image: Image.Image) -> None:
        self.frames += 1
        self.last = image


class PngDisplay(Display):
    """Writes each frame to a PNG - preview the UI without hardware.

    Always rewrites `latest.png`; with `keep_history` it also numbers the
    frames so an interaction can be reviewed step by step.
    """

    def __init__(self, directory: str | Path = "frames",
                 keep_history: bool = False):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.keep_history = keep_history
        self.frames = 0

    def show(self, image: Image.Image) -> None:
        image.save(self.directory / "latest.png")
        if self.keep_history:
            image.save(self.directory / f"frame_{self.frames:04d}.png")
        self.frames += 1


class ST7789Display(Display):
    """Waveshare 1.3inch LCD HAT (ST7789VW, 240x240, SPI0 + DC/RST/BL).

    MADCTL defaults to 0x70, the orientation Waveshare's own demo uses for
    this HAT (buttons on the left edge). Pass a different value if the
    image ends up rotated or mirrored on your unit.
    """

    def __init__(self, madctl: int = 0x70, spi_hz: int = SPI_HZ):
        import spidev
        from gpiozero import DigitalOutputDevice

        self._spi = None
        self._dc = self._rst = self._bl = None
        self.asleep = False
        try:
            self._spi = spidev.SpiDev()
            self._spi.open(SPI_BUS, SPI_DEVICE)
            self._spi.max_speed_hz = spi_hz
            self._spi.mode = 0

            self._dc = DigitalOutputDevice(PIN_DC)
            self._rst = DigitalOutputDevice(PIN_RST)
            self._bl = DigitalOutputDevice(PIN_BL)
            self._madctl = madctl
            self._init_panel()
            self._bl.on()
        except BaseException:
            # Another process may hold these lines (a second Waveshare HAT
            # shares GPIO 24/25 and SPI0 CE0). Release whatever we did take
            # so the caller can fall back without leaking the SPI handle.
            self._release()
            raise

    def _release(self) -> None:
        for device in (self._dc, self._rst, self._bl):
            if device is not None:
                try:
                    device.close()
                except Exception:
                    pass
        self._dc = self._rst = self._bl = None
        if self._spi is not None:
            try:
                self._spi.close()
            except Exception:
                pass
            self._spi = None

    # ---- low level ----

    def _command(self, cmd: int, *data: int) -> None:
        self._dc.off()
        self._spi.writebytes([cmd])
        if data:
            self._dc.on()
            self._spi.writebytes(list(data))

    def _reset(self) -> None:
        import time
        self._rst.on()
        time.sleep(0.01)
        self._rst.off()
        time.sleep(0.01)
        self._rst.on()
        time.sleep(0.12)

    def _init_panel(self) -> None:
        import time
        self._reset()
        self._command(0x36, self._madctl)
        self._command(0x3A, 0x05)              # 16-bit/pixel (RGB565)
        self._command(0xB2, 0x0C, 0x0C, 0x00, 0x33, 0x33)
        self._command(0xB7, 0x35)
        self._command(0xBB, 0x19)
        self._command(0xC0, 0x2C)
        self._command(0xC2, 0x01)
        self._command(0xC3, 0x12)
        self._command(0xC4, 0x20)
        self._command(0xC6, 0x0F)
        self._command(0xD0, 0xA4, 0xA1)
        self._command(0xE0, 0xD0, 0x04, 0x0D, 0x11, 0x13, 0x2B, 0x3F,
                      0x54, 0x4C, 0x18, 0x0D, 0x0B, 0x1F, 0x23)
        self._command(0xE1, 0xD0, 0x04, 0x0C, 0x11, 0x13, 0x2C, 0x3F,
                      0x44, 0x51, 0x2F, 0x1F, 0x1F, 0x20, 0x23)
        self._command(0x21)                    # inversion on (IPS panel)
        self._command(0x11)                    # sleep out
        time.sleep(0.12)
        self._command(0x29)                    # display on

    def _window(self, x0: int, y0: int, x1: int, y1: int) -> None:
        self._command(0x2A, x0 >> 8, x0 & 0xFF, x1 >> 8, x1 & 0xFF)
        self._command(0x2B, y0 >> 8, y0 & 0xFF, y1 >> 8, y1 & 0xFF)
        self._command(0x2C)

    # ---- frame push ----

    def show(self, image: Image.Image) -> None:
        if image.size != (WIDTH, HEIGHT):
            image = image.resize((WIDTH, HEIGHT))
        self._window(0, 0, WIDTH - 1, HEIGHT - 1)
        self._dc.on()
        payload = to_rgb565(image)
        if hasattr(self._spi, "writebytes2"):
            # Chunks internally and is measurably faster than slicing here.
            self._spi.writebytes2(payload)
        else:
            # spidev's transfer buffer defaults to 4 KiB; stay under it.
            for start in range(0, len(payload), 4096):
                self._spi.writebytes(payload[start:start + 4096])

    def sleep(self) -> None:
        if self._bl is not None:
            self._bl.off()
        if self._spi is not None:
            self._command(0x28)      # display off
        self.asleep = True

    def wake(self) -> None:
        if self._spi is not None:
            self._command(0x29)      # display on
        if self._bl is not None:
            self._bl.on()
        self.asleep = False

    def close(self) -> None:
        try:
            self.sleep()
        finally:
            self._release()


# Per-channel bit twiddling for RGB888 -> big-endian RGB565, expressed as
# byte translation tables so the work happens in C. A per-pixel Python
# loop needs ~390 ms for a 240x240 frame on a Pi Zero 2 W, which alone
# exceeds the UI's redraw interval; this runs in a small fraction of it.
# High byte: RRRRRGGG   Low byte: GGGBBBBB
_R_HI = bytes(i & 0xF8 for i in range(256))
_G_HI = bytes(i >> 5 for i in range(256))
_G_LO = bytes((i & 0x1C) << 3 for i in range(256))
_B_LO = bytes(i >> 3 for i in range(256))


def to_rgb565(image: Image.Image) -> bytes:
    """Pack an RGB image into big-endian RGB565, the ST7789 wire format."""
    if image.mode != "RGB":
        image = image.convert("RGB")
    pixels = image.tobytes()          # RGB888, 3 bytes per pixel
    red, green, blue = pixels[0::3], pixels[1::3], pixels[2::3]
    out = bytearray(len(pixels) // 3 * 2)
    out[0::2] = bytes(map(int.__or__, red.translate(_R_HI),
                          green.translate(_G_HI)))
    out[1::2] = bytes(map(int.__or__, green.translate(_G_LO),
                          blue.translate(_B_LO)))
    return bytes(out)


def make_display(kind: str = "auto", **kwargs) -> Display:
    """kind: auto | lcd | png | null.

    "auto" uses the LCD when the hardware libraries and SPI device are
    present, and falls back to PNG output otherwise - so the same command
    works before and after the HAT is fitted.
    """
    if kind == "null":
        return NullDisplay()
    if kind == "png":
        return PngDisplay(**kwargs)
    if kind == "lcd":
        return ST7789Display()
    if kind != "auto":
        raise ValueError(f"unknown display kind: {kind}")
    try:
        return ST7789Display()
    except Exception:
        return PngDisplay(**kwargs)
