"""Per-board pin maps, so the rest of the UI never names a GPIO number.

The LCD HAT plugs into a 40-pin header, and the *physical* pin positions
are the same on every board that copies that header - what differs is
which SoC line each position is wired to. So the profiles below are
written physical-pin-first: the comment is the constant, the numbers are
the translation.

Adding a board means adding a profile here and nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Line:
    """One GPIO line: which character device, and which line on it."""

    chip: int
    line: int

    @property
    def device(self) -> str:
        return f"/dev/gpiochip{self.chip}"


@dataclass(frozen=True)
class BoardProfile:
    key: str
    label: str
    spi_bus: int
    spi_device: int
    dc: Line
    rst: Line
    bl: Line
    buttons: "dict[str, Line]"
    # Transitional: the Pi has been running on gpiozero for weeks and is
    # the show machine, so it keeps that path until the portable one is
    # verified there too. New boards use "periphery".
    gpio_backend: str = "periphery"


# Raspberry Pi: BCM numbering is the line number on gpiochip0.
PI_ZERO_2W = BoardProfile(
    key="pi-zero-2w",
    label="Raspberry Pi Zero 2 W",
    spi_bus=0, spi_device=0,          # /dev/spidev0.0, header pins 19/21/23/24
    dc=Line(0, 25),                   # pin 22, BCM25
    rst=Line(0, 27),                  # pin 13, BCM27
    bl=Line(0, 24),                   # pin 18, BCM24
    buttons={
        "up": Line(0, 6),             # pin 31, BCM6
        "down": Line(0, 19),          # pin 35, BCM19
        "left": Line(0, 5),           # pin 29, BCM5
        "right": Line(0, 26),         # pin 37, BCM26
        "press": Line(0, 13),         # pin 33, BCM13
        "key1": Line(0, 21),          # pin 40, BCM21
        "key2": Line(0, 20),          # pin 38, BCM20
        "key3": Line(0, 16),          # pin 36, BCM16
    },
    gpio_backend="gpiozero",
)

# Radxa Cubie A7Z (Allwinner A733). Line numbers follow the vendor
# formula, confirmed against the hardware: gpiochip0 reports 352 lines
# = 11 banks (PA..PK) and gpiochip1 reports 64 = 2 banks (PL, PM), so
# the bank index restarts at PL on chip 1.
#   chip0 line = 32 * bank(PA=0..PK=10) + n
#   chip1 line = 32 * bank(PL=0, PM=1)  + n
# The lines are unnamed in this kernel, so they must be addressed by
# number - looking them up by name is not an option here.
CUBIE_A7Z = BoardProfile(
    key="cubie-a7z",
    label="Radxa Cubie A7Z",
    spi_bus=1, spi_device=0,          # SPI1 sits on the same header pins
    dc=Line(1, 5),                    # pin 22, PL5
    rst=Line(1, 6),                   # pin 13, PL6
    bl=Line(0, 313),                  # pin 18, PJ25 = 32*9 + 25
    buttons={
        "up": Line(0, 35),            # pin 31, PB3
        "down": Line(0, 38),          # pin 35, PB6
        "left": Line(0, 34),          # pin 29, PB2
        "right": Line(1, 36),         # pin 37, PM4 = 32*1 + 4
        "press": Line(1, 35),         # pin 33, PM3
        "key1": Line(0, 39),          # pin 40, PB7
        "key2": Line(0, 40),          # pin 38, PB8
        "key3": Line(0, 36),          # pin 36, PB4
    },
)

PROFILES = {p.key: p for p in (PI_ZERO_2W, CUBIE_A7Z)}

# Matched against /proc/device-tree/compatible, most specific first.
_SIGNATURES = (
    ("radxa,cubie-a7z", CUBIE_A7Z),
    ("raspberrypi", PI_ZERO_2W),
)


def _compatible() -> str:
    try:
        raw = Path("/proc/device-tree/compatible").read_bytes()
    except OSError:
        return ""
    return raw.decode("ascii", "ignore").replace("\0", " ").lower()


def detect_board(default: BoardProfile = PI_ZERO_2W) -> BoardProfile:
    """Identify the board from its device tree, falling back to the Pi."""
    compatible = _compatible()
    for signature, profile in _SIGNATURES:
        if signature in compatible:
            return profile
    return default


BOARD = detect_board()
