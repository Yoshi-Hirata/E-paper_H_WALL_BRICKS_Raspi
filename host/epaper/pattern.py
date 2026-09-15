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


# --- Protocol V1.1 (16-color firmware, OTA'd 2026-08-28) ---
# Markers changed with the new firmware: 0xFE array start/end (was 0x21),
# 0xFF pad/no-refresh (was 0x37). Colors widened to 0x00-0x0F. The GEN
# (COMMON, 0x06) device type addresses segments 1-60 directly, no holes:
# per the vendor's 2026-08-26 README, indices 1-60 map to boards P1-P60
# and 0/61/62/63 are padding. Verified on boards 1 and 20 on 2026-08-28.
MARKER_V11 = 0xFE
HI_Z_V11 = 0xFF
COLOR_COUNT_16 = 16
SEGMENTS_GEN = frozenset(range(1, 61))

# V1.1 LUT (spec 5.1.1). Note the shift against the old 6-color table:
# 0x05 is now turquoise and green moved to 0x06 - anything still sending
# the old COLOR_NAMES["green"] to a new-firmware board shows turquoise.
COLOR_NAMES_16 = {
    "white": 0x00,
    "yellow": 0x01,
    "blue": 0x02,
    "red": 0x03,
    "black": 0x04,
    "turquoise": 0x05,
    "green": 0x06,
    "almond": 0x07,
    "pink": 0x08,
    "skyblue": 0x09,
    "orange": 0x0A,
    "yellowgreen": 0x0B,
    "olivegray": 0x0C,
    "brown": 0x0D,
    "darkbrown": 0x0E,
    "smokeblue": 0x0F,
}

# English display names in LUT order, translated from the datasheet's
# palette table (5.1.1 调色板): index == color code.
COLOR_LABELS_16 = [
    "White",        # 0x00 白色
    "Yellow",       # 0x01 黄色
    "Blue",         # 0x02 蓝色
    "Red",          # 0x03 红色
    "Black",        # 0x04 黑色
    "Turquoise",    # 0x05 青绿色
    "Green",        # 0x06 绿色
    "Almond",       # 0x07 杏仁色
    "Light Pink",   # 0x08 浅粉色
    "Sky Blue",     # 0x09 天蓝色
    "Orange",       # 0x0A 橙色
    "Yellow Green", # 0x0B 黄绿色
    "Olive Gray",   # 0x0C 橄榄灰
    "Brown",        # 0x0D 棕色
    "Dark Brown",   # 0x0E 深棕色
    "Smoke Blue",   # 0x0F 烟雾蓝
]

# Reference RGB per code, straight from the same table (参考色 (RGB)).
# For previews and the LCD, not for the boards - the e-paper's real
# pigments differ (0x06 and 0x07-0x0E render noticeably off the table).
COLOR_RGB_16 = [
    (255, 255, 255),  # 0x00 white
    (255, 255, 0),    # 0x01 yellow
    (0, 0, 255),      # 0x02 blue
    (255, 0, 0),      # 0x03 red
    (0, 0, 0),        # 0x04 black
    (64, 224, 208),   # 0x05 turquoise
    (0, 200, 0),      # 0x06 green
    (235, 210, 180),  # 0x07 almond
    (255, 209, 220),  # 0x08 light pink
    (135, 206, 235),  # 0x09 sky blue
    (255, 140, 0),    # 0x0A orange
    (154, 205, 50),   # 0x0B yellow green
    (112, 116, 85),   # 0x0C olive gray
    (139, 69, 19),    # 0x0D brown
    (92, 51, 23),     # 0x0E dark brown
    (110, 130, 150),  # 0x0F smoke blue
]


def build_gen_array(colors: dict[int, int] | None = None,
                    fill: int = COLOR_WHITE) -> bytes:
    """64-byte GEN (0x06) array: segment number == array index 1-60.

    colors maps segment -> color 0x00-0x0F; unlisted segments get `fill`.
    """
    colors = colors or {}
    for seg, col in colors.items():
        if seg not in SEGMENTS_GEN:
            raise ValueError(f"segment {seg} out of range 1-60")
        if not 0 <= col < COLOR_COUNT_16:
            raise ValueError(f"invalid color 0x{col:02X} for segment {seg}")
    if not 0 <= fill < COLOR_COUNT_16:
        raise ValueError(f"invalid fill color 0x{fill:02X}")

    arr = bytearray([HI_Z_V11] * 64)
    arr[0] = arr[63] = MARKER_V11
    for seg in SEGMENTS_GEN:
        arr[seg] = colors.get(seg, fill)
    return bytes(arr)


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
