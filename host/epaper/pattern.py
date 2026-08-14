"""Color array builder for H_WALL_BRICKS (DeviceType 0x01).

64-byte array, triangle number == array index (spec section 13):
  index 0  : 0x21 (COM start marker)
  index 63 : 0x21 (COM end marker)
  missing triangle numbers 17-22, 61, 62 : 0x37 (Hi-Z)
  remaining 54 indices : color code 0x00-0x05
"""

from __future__ import annotations

COLOR_WHITE = 0x00
COLOR_YELLOW = 0x01
COLOR_BLUE = 0x02
COLOR_RED = 0x03
COLOR_BLACK = 0x04
COLOR_GREEN = 0x05

COLOR_NAMES = {
    "white": COLOR_WHITE,
    "yellow": COLOR_YELLOW,
    "blue": COLOR_BLUE,
    "red": COLOR_RED,
    "black": COLOR_BLACK,
    "green": COLOR_GREEN,
}

COM_MARKER = 0x21
HI_Z = 0x37

# Triangle numbers on the production boards (2026-08): 1-16 and 23-60
# -> 54 triangles; 17-22 and 61-63 do not exist. Address_H_WALL_BRICKS
# .jpg shows the first-generation numbering, which is one higher
# throughout (2-17, 24-61) - subtract 1 to read it for these boards.
# Found on hardware: with the old numbering, each panel's segment 1
# stayed white because nothing ever wrote index 1.
VALID_TRIANGLES = frozenset(range(1, 17)) | frozenset(range(23, 61))
assert len(VALID_TRIANGLES) == 54


def build_hexagon_array(colors: dict[int, int] | None = None,
                        fill: int = COLOR_WHITE) -> bytes:
    """Build the 64-byte color array.

    colors maps triangle number -> color code; unlisted triangles get
    `fill`. Raises ValueError on unknown triangle numbers or color codes.
    """
    colors = colors or {}
    for tri, col in colors.items():
        if tri not in VALID_TRIANGLES:
            raise ValueError(f"triangle {tri} does not exist on this panel")
        if col not in (0x00, 0x01, 0x02, 0x03, 0x04, 0x05):
            raise ValueError(f"invalid color 0x{col:02X} for triangle {tri}")
    if fill not in (0x00, 0x01, 0x02, 0x03, 0x04, 0x05):
        raise ValueError(f"invalid fill color 0x{fill:02X}")

    arr = bytearray([HI_Z] * 64)
    arr[0] = COM_MARKER
    arr[63] = COM_MARKER
    for tri in VALID_TRIANGLES:
        arr[tri] = colors.get(tri, fill)
    return bytes(arr)
