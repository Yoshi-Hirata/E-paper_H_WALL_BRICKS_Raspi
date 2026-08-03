"""Frame builders for color-test commands.

Deliberately excluded (destructive / risky): 0x15 clear-all, 0x03 set
address, 0x05 factory reset.
"""

import struct

from .protocol import (
    ADDR_PC,
    CMD_DELETE_SLOT,
    CMD_SAVE_COLOR,
    CMD_SET_SLOT_CONFIG,
    CMD_SHOW_SINGLE,
    CMD_PLAY_STOP,
    DEV_H_WALL_BRICKS,
    Frame,
)

MODE_ALL_AT_ONCE = 0x02      # recommended per spec 6.4
DIR_NATURAL = 0x0A           # recommended per spec 6.5
FLAG_LAST_FRAME = 0x01

TEST_SLOT = 19  # dedicated test slot, least likely to hold user data


def _frame(dest: int, cmd: int, data: bytes, group_count: int) -> Frame:
    return Frame(
        dest=dest,
        src=ADDR_PC,
        dev_type=DEV_H_WALL_BRICKS,
        cmd=cmd,
        group_no=dest if dest != 0xFF else 1,
        group_count=group_count,
        chip_count=1,
        chip_addr=0,
        data=data,
    )


def stop(dest: int, group_count: int = 2) -> Frame:
    return _frame(dest, CMD_PLAY_STOP, b"", group_count)


def slot_config(dest: int, slot: int, mode: int = MODE_ALL_AT_ONCE,
                direction: int = DIR_NATURAL, start_delay: int = 0,
                switch_delay: int = 0, pipeline: int = 1,
                group_count: int = 2) -> Frame:
    if not 0 <= slot <= 19:
        raise ValueError(f"slot {slot} out of range 0-19")
    data = struct.pack("<BBBHHB", slot, mode, direction,
                       start_delay, switch_delay, pipeline)
    return _frame(dest, CMD_SET_SLOT_CONFIG, data, group_count)


def save_color(dest: int, slot: int, array64: bytes,
               group_count: int = 2) -> Frame:
    """Single-chip device: one 66-byte frame with the LAST_FRAME flag.

    The spec's 69-byte last-frame format (7.1.2, trailing
    mode/direction/pipeline) is silently dropped by the firmware, which
    enforces DataLen <= 66 (spec 2.2). Verified on hardware 2026-07-23.
    Play parameters go through slot_config (0x1B) instead.
    """
    if not 0 <= slot <= 19:
        raise ValueError(f"slot {slot} out of range 0-19")
    if len(array64) != 64:
        raise ValueError(f"color array must be 64 bytes, got {len(array64)}")
    data = bytes([slot, FLAG_LAST_FRAME]) + array64
    assert len(data) == 66
    return _frame(dest, CMD_SAVE_COLOR, data, group_count)


def show_single(dest: int, slot: int, group_count: int = 2) -> Frame:
    if not 0 <= slot <= 19:
        raise ValueError(f"slot {slot} out of range 0-19")
    return _frame(dest, CMD_SHOW_SINGLE, bytes([slot]), group_count)


def delete_slot(dest: int, slot: int, group_count: int = 2) -> Frame:
    if not 0 <= slot <= 19:
        raise ValueError(f"slot {slot} out of range 0-19")
    return _frame(dest, CMD_DELETE_SLOT, bytes([slot]), group_count)
