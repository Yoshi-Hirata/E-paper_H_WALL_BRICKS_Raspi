"""E-paper display control protocol (显示控制协议 V1.0).

Frame layout (little-endian for multi-byte fields):

    AA 55 | Dest | Src | DevType | Cmd | DataLen | GN GC CC CA SS | Data | CRC16 | 55 AA

CRC16 is Modbus (poly 0xA001 reflected, init 0xFFFF) computed over
Dest .. end of Data (10 + DataLen bytes), stored low byte first.
"""

from __future__ import annotations

from dataclasses import dataclass, field

FRAME_HEAD = b"\xaa\x55"
FRAME_TAIL = b"\x55\xaa"

ADDR_PC = 0x00
ADDR_BUS_MASTER = 0x01
ADDR_BROADCAST = 0xFF

DEV_WALL_BRICKS = 0x00
DEV_H_WALL_BRICKS = 0x01
DEV_NUMBER_BRAND = 0x03
DEV_SMALL_H = 0x04

CMD_RESET = 0x01
CMD_GET_VERSION = 0x02
CMD_SET_ADDRESS = 0x03
CMD_SAVE_CONFIG = 0x04
CMD_FACTORY_RESET = 0x05
CMD_SAVE_COLOR = 0x13
CMD_DELETE_SLOT = 0x14
CMD_CLEAR_ALL = 0x15
CMD_PLAY_START = 0x16
CMD_PLAY_STOP = 0x17
CMD_SET_AUTOPLAY = 0x19
CMD_SET_SLAVE_COUNT = 0x1A
CMD_SET_SLOT_CONFIG = 0x1B
CMD_SHOW_SINGLE = 0x1D

ACK_SUCCESS = 0x80
ACK_FAIL = 0x81
ACK_BUSY = 0x82
ACK_INVALID_CMD = 0x83
ACK_CRC_ERROR = 0x84
ACK_PARAM_ERROR = 0x85

ACK_NAMES = {
    ACK_SUCCESS: "ACK_SUCCESS",
    ACK_FAIL: "ACK_FAIL",
    ACK_BUSY: "ACK_BUSY",
    ACK_INVALID_CMD: "ACK_INVALID_CMD",
    ACK_CRC_ERROR: "ACK_CRC_ERROR",
    ACK_PARAM_ERROR: "ACK_PARAM_ERROR",
}

ERROR_CODES = {
    0x00: "no error",
    0x01: "segment number out of range",
    0x02: "chip address error",
    0x03: "group number error",
    0x04: "data length error",
    0x05: "invalid color value",
    0x06: "hardware error (storage r/w)",
    0x07: "chip count error",
    0x08: "group count error",
    0x0A: "parameter error (generic)",
}


def crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


@dataclass
class Frame:
    dest: int
    src: int
    dev_type: int
    cmd: int
    group_no: int = 1
    group_count: int = 1
    chip_count: int = 1
    chip_addr: int = 0
    seg_start: int = 0
    data: bytes = b""

    def encode(self) -> bytes:
        if len(self.data) > 0xFF:
            raise ValueError(f"data too long: {len(self.data)}")
        body = bytes(
            [
                self.dest,
                self.src,
                self.dev_type,
                self.cmd,
                len(self.data),
                self.group_no,
                self.group_count,
                self.chip_count,
                self.chip_addr,
                self.seg_start,
            ]
        ) + self.data
        crc = crc16_modbus(body)
        return FRAME_HEAD + body + bytes([crc & 0xFF, crc >> 8]) + FRAME_TAIL

    @property
    def is_ack(self) -> bool:
        return ACK_SUCCESS <= self.cmd <= ACK_PARAM_ERROR

    def describe(self) -> str:
        name = ACK_NAMES.get(self.cmd, f"cmd 0x{self.cmd:02X}")
        extra = ""
        if self.cmd == ACK_FAIL and self.data:
            code = self.data[0]
            extra = f" (error 0x{code:02X}: {ERROR_CODES.get(code, 'unknown')})"
        return f"{name} from 0x{self.src:02X}{extra}"


class FrameError(Exception):
    pass


MIN_FRAME_LEN = 16  # head(2) + body(10) + crc(2) + tail(2)


def decode(buf: bytes) -> tuple["Frame | None", bytes]:
    """Extract one frame from buf.

    Returns (frame, remaining). frame is None when buf holds no complete
    frame yet; bytes before a valid head are discarded in `remaining`.
    """
    start = buf.find(FRAME_HEAD)
    if start < 0:
        # Keep a trailing 0xAA in case 0x55 arrives next.
        return None, buf[-1:] if buf.endswith(b"\xaa") else b""
    buf = buf[start:]
    if len(buf) < MIN_FRAME_LEN:
        return None, buf
    data_len = buf[6]
    total = MIN_FRAME_LEN + data_len
    if len(buf) < total:
        return None, buf
    candidate = buf[:total]
    body = candidate[2 : 12 + data_len]
    crc_lo, crc_hi = candidate[12 + data_len], candidate[13 + data_len]
    tail = candidate[14 + data_len : total]
    crc = crc16_modbus(body)
    if tail != FRAME_TAIL or crc != (crc_hi << 8 | crc_lo):
        # Corrupt or false head: resync one byte past this head.
        frame, rest = decode(buf[2:])
        return frame, rest
    frame = Frame(
        dest=body[0],
        src=body[1],
        dev_type=body[2],
        cmd=body[3],
        group_no=body[5],
        group_count=body[6],
        chip_count=body[7],
        chip_addr=body[8],
        seg_start=body[9],
        data=bytes(body[10 : 10 + data_len]),
    )
    return frame, buf[total:]


def hexdump(data: bytes) -> str:
    return " ".join(f"{b:02X}" for b in data)
