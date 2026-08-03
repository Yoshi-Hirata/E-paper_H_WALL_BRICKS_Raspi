"""Serial transport: send a frame, wait for ACK with timeout/retry."""

import time

import serial
from serial.tools import list_ports

from .protocol import Frame, decode, hexdump

BAUDRATE = 115200
ACK_TIMEOUT_S = 0.5
MAX_RETRIES = 3

# STM32 USB CDC (board's virtual COM port)
KNOWN_VID_PID = {(0x0483, 0x5740)}


def find_port() -> str | None:
    ports = list(list_ports.comports())
    for p in ports:
        if (p.vid, p.pid) in KNOWN_VID_PID:
            return p.device
    if len(ports) == 1:
        return ports[0].device
    return None


class Bus:
    def __init__(self, port: str, verbose: bool = True):
        self.ser = serial.Serial(port, BAUDRATE, timeout=0.05)
        # USB CDC drops or corrupts bytes written immediately after open
        # (DTR toggle); settle, then send padding bytes the firmware's
        # frame parser discards, so any loss hits the padding instead of
        # the first real frame.
        time.sleep(0.2)
        self.ser.write(b"\x00" * 8)
        self.ser.flush()
        time.sleep(0.1)
        self.ser.reset_input_buffer()
        self.verbose = verbose
        self._rx = b""

    def close(self):
        self.ser.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def send(self, frame: Frame):
        raw = frame.encode()
        if self.verbose:
            print(f"TX> {hexdump(raw)}")
        self.ser.reset_input_buffer()
        self._rx = b""
        self.ser.write(raw)
        self.ser.flush()

    def recv(self, timeout: float = ACK_TIMEOUT_S) -> Frame | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            chunk = self.ser.read(64)
            if chunk:
                if self.verbose:
                    print(f"RX< {hexdump(chunk)}")
                self._rx += chunk
                frame, self._rx = decode(self._rx)
                if frame:
                    return frame
        return None

    def request(self, frame: Frame, retries: int = MAX_RETRIES) -> Frame | None:
        """Send and wait for ACK. Returns None on timeout after retries."""
        for attempt in range(1, retries + 1):
            self.send(frame)
            ack = self.recv()
            if ack:
                return ack
            if self.verbose and attempt < retries:
                print(f"-- timeout, retry {attempt}/{retries - 1}")
        return None
