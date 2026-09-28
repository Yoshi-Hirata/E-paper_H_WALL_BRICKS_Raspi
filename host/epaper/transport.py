"""Serial transport: send a frame, wait for ACK with timeout/retry."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

import serial
from serial.tools import list_ports

from .protocol import ADDR_BROADCAST, MIN_FRAME_LEN, Frame, decode, hexdump

BAUDRATE = 115200
ACK_TIMEOUT_S = 0.5
WRITE_TIMEOUT_S = 2.0
MAX_RETRIES = 3
# Zero bytes the firmware's frame parser throws away (a frame opens
# AA 55): what an open writes so the bytes lost to the DTR toggle are
# these and not the first real frame's head.
PAD_BYTES = 8
# ...and what a RESYNC writes, which is a different job: a parser that
# latched onto the middle of a frame has to be pushed out of it, so this
# is a whole minimum frame's worth rather than the eight an open needs
# (ui/runner.py's _recover_bus(); the 2026-09-28 LOOK28 state, where
# every write blocked 359 ms and nothing was executed).
RESYNC_PAD_BYTES = MIN_FRAME_LEN

# STM32 USB CDC (board's virtual COM port)
KNOWN_VID_PID = {(0x0483, 0x5740)}


def _is_usb_serial(device: str) -> bool:
    """True for USB CDC / USB-serial devices, not the Pi's built-in UART.

    On a Raspberry Pi the on-board UART (/dev/ttyS0, /dev/ttyAMA0) is
    always enumerated, so the "only one port -> use it" fallback would
    happily pick it and then time out on every frame.
    """
    name = device.rsplit("/", 1)[-1]
    return name.startswith(("ttyACM", "ttyUSB", "COM"))


def port_serial(device: str | None) -> str | None:
    """USB serial number of the board behind `device` (the STM32 unique
    ID, e.g. 48EC7570324C), or None for an unknown or non-USB port."""
    if not device:
        return None
    for p in list_ports.comports():
        if p.device == device:
            return p.serial_number or None
    return None


def find_port() -> str | None:
    ports = list(list_ports.comports())
    for p in ports:
        if (p.vid, p.pid) in KNOWN_VID_PID:
            return p.device
    usb = [p for p in ports if _is_usb_serial(p.device)]
    if len(usb) == 1:
        return usb[0].device
    return None


# ---- resetting the master's USB device (2026-09-28, radxa-07) ----
# A master in the degraded state accepts every frame, executes none and
# answers nothing, and neither padding, a port reopen, a STOP nor a probe
# sweep brings it back. A USB device reset does, in 0.3 s: on LOOK28
# `sudo -n usbreset 0483:5740` (kernel: "usb 1-1: reset full-speed USB
# device") had the node back as ttyACM0, `panels online: 22/22` eight seconds
# later and the burn intact. The "restart cured it" of the day before were
# Radxa REBOOTS - a USB power cycle, i.e. the same thing, much slower.
USBDEVFS_RESET = (ord("U") << 8) | 20         # _IO('U', 20), linux/usbdevice_fs.h
USBRESET_TIMEOUT_S = 5.0


def _usb_device(port: str) -> "dict | None":
    """The USB device behind the serial `port`: {"vid", "pid", "busnum",
    "devnum", "sysfs"}, or None when `port` is not a USB serial port that
    is there right now.

    Only ever the device behind THIS port - never a search by VID:PID
    across the machine: the test suite runs on the Radxas too, and must not
    be able to reset a real master that happens to be plugged in.
    """
    for info in list_ports.comports():
        if info.device != port or info.vid is None:
            continue
        sysfs = getattr(info, "usb_device_path", None)
        if not sysfs and info.location:
            # "1-1:1.0" is the interface; the device is the part before ":".
            sysfs = "/sys/bus/usb/devices/" + info.location.split(":", 1)[0]
        if not sysfs:
            return None
        try:
            with open(os.path.join(sysfs, "busnum")) as f:
                busnum = int(f.read().strip())
            with open(os.path.join(sysfs, "devnum")) as f:
                devnum = int(f.read().strip())
        except (OSError, ValueError):
            return None
        return {"vid": info.vid, "pid": info.pid, "busnum": busnum,
                "devnum": devnum, "sysfs": sysfs}
    return None


def usb_reset(port: str) -> "tuple[bool, str]":
    """Reset the USB device behind the serial `port`, as a re-plug would.

    Returns (done, how): how it was done ("ioctl" / "sudo usbreset"), or why
    it could not be. Two means, the first that works:

      a  the USBDEVFS_RESET ioctl on /dev/bus/usb/BBB/DDD, if this process
         may write that node - on the Radxas it is root:root crw-rw-r--, so
         normally it may not;
      b  `sudo -n usbreset VID:PID` (the service user has NOPASSWD sudo and
         /usr/bin/usbreset exists), bounded at USBRESET_TIMEOUT_S.

    The port should be CLOSED first: the node goes away with the reset and
    comes back - possibly under another name - about 0.3-0.45 s later.
    Anything but Linux answers "unsupported" and touches nothing.
    """
    if not sys.platform.startswith("linux"):
        return False, f"unsupported on {sys.platform}"
    dev = _usb_device(port) if port else None
    if dev is None:
        return False, f"no USB device behind {port}"
    node = f"/dev/bus/usb/{dev['busnum']:03d}/{dev['devnum']:03d}"
    tried = []
    if os.access(node, os.W_OK):
        try:
            import fcntl

            fd = os.open(node, os.O_WRONLY)
            try:
                fcntl.ioctl(fd, USBDEVFS_RESET, 0)
            finally:
                os.close(fd)
            return True, "ioctl"
        except OSError as exc:
            tried.append(f"ioctl: {exc}")
    else:
        tried.append(f"ioctl: {node} not writable")
    tool = shutil.which("usbreset")
    if tool is None:
        tried.append("usbreset: not installed")
        return False, "; ".join(tried)
    ident = f"{dev['vid']:04x}:{dev['pid']:04x}"
    try:
        done = subprocess.run(["sudo", "-n", tool, ident], capture_output=True,
                              text=True, timeout=USBRESET_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as exc:
        tried.append(f"sudo usbreset: {exc}")
        return False, "; ".join(tried)
    if done.returncode == 0:
        return True, "sudo usbreset"
    said = (done.stderr or done.stdout or "").strip().splitlines()
    tried.append(f"sudo usbreset exited {done.returncode}"
                 + (f": {said[-1]}" if said else ""))
    return False, "; ".join(tried)


class Bus:
    def __init__(self, port: str, verbose: bool = True):
        self.verbose = verbose
        self._rx = b""
        self.ser = None
        self._open(port)

    def _open(self, port: str) -> None:
        # write_timeout: a wedged board CDC stops draining USB, and a
        # write into that state blocks forever (frames are <100 bytes,
        # so any real send completes in milliseconds). Fail fast with
        # SerialTimeoutException instead of hanging the caller; the
        # 2026-09-11 hang also preceded the host OS going down.
        self.ser = serial.Serial(port, BAUDRATE, timeout=0.05,
                                 write_timeout=WRITE_TIMEOUT_S)
        # USB CDC drops or corrupts bytes written immediately after open
        # (DTR toggle); settle, then send padding bytes the firmware's
        # frame parser discards, so any loss hits the padding instead of
        # the first real frame.
        time.sleep(0.2)
        self.pad()
        time.sleep(0.1)
        self.ser.reset_input_buffer()
        self._rx = b""

    @property
    def port(self) -> str:
        """The device name this bus is on - kept after a close(), which is
        what a reopen falls back to when find_port() comes up empty."""
        return self.ser.port

    def pad(self, count: int = PAD_BYTES) -> None:
        """Write `count` zero bytes: nothing the firmware can read as a
        frame, so they are discarded wherever its parser happens to be.

        An open sends them because the DTR toggle eats the first bytes;
        a recovery sends RESYNC_PAD_BYTES of them because a parser stuck
        mid-frame has to be pushed past the end of whatever it thinks it
        is reading (see RESYNC_PAD_BYTES).
        """
        self.ser.write(b"\x00" * count)
        self.ser.flush()

    def reopen(self, port: str | None = None) -> str:
        """Close this port and open it again IN PLACE, keeping the object.

        A reopen is the DTR toggle and the padding of a fresh open - the
        one thing a restart of the unit's service does that nothing else
        does, and the cure for a master whose USB CDC has stopped
        executing what it accepts (ui/runner.py's _recover_bus()).

        In place, because the callers hold this object several frames
        deep: the remote worker's `bus` is a local of _run_remote() and
        the recovery is reached from inside _fire_at()'s own wait, where
        no rebinding could ever reach it. `port` may differ from the
        current one - a USB re-enumeration renames ttyACM0 to ttyACM1 -
        and defaults to the name already in use. Returns the name opened.
        """
        try:
            self.ser.close()
        except Exception:               # noqa: BLE001 - it is going anyway
            pass
        port = port or self.port
        self._open(port)
        return port

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

    def request(self, frame: Frame, retries: int = MAX_RETRIES,
                timeout: float = ACK_TIMEOUT_S) -> Frame | None:
        """Send and wait for THIS frame's ACK; None on timeout after retries.

        Only an answer from the board the frame was addressed to counts.
        A board that was deaf when a command arrived does not discard it:
        it answers after its repaint, up to seconds later, and that frame
        lands in the read window of whatever was asked next. Taking it as
        the answer to the new question attributes a board's "yes" to a
        different board - which ui/runner.py's landing check made easy to
        hit, since it deliberately asks a board that is expected to be
        deaf. A frame from anyone else is logged as a stray and the wait
        goes on with what is left of the timeout.

        `timeout` is the read window per try (the landing check uses a
        short one; a board that is going to answer does so in
        milliseconds, and the whole point of the question is the silence).
        """
        for attempt in range(1, retries + 1):
            self.send(frame)
            deadline = time.monotonic() + timeout
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                ack = self.recv(left)
                if ack is None:
                    break
                if frame.dest != ADDR_BROADCAST and ack.src != frame.dest:
                    if self.verbose:
                        print(f"-- stray reply from 0x{ack.src:02X} "
                              f"(0x{ack.cmd:02X}), ignored")
                    continue
                return ack
            if self.verbose and attempt < retries:
                print(f"-- timeout, retry {attempt}/{retries - 1}")
        return None
