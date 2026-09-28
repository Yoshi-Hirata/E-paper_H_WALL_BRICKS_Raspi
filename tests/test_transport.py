import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

from epaper import transport
from epaper.commands import get_version, stop
from epaper.protocol import Frame
from epaper.transport import Bus, find_port


class FakePort:
    def __init__(self, device, vid=None, pid=None):
        self.device = device
        self.vid = vid
        self.pid = pid


def _patch_ports(monkeypatch, ports):
    monkeypatch.setattr(transport.list_ports, "comports", lambda: ports)


def test_finds_board_by_vid_pid(monkeypatch):
    _patch_ports(monkeypatch, [
        FakePort("/dev/ttyS0"),
        FakePort("/dev/ttyACM0", vid=0x0483, pid=0x5740),
    ])
    assert find_port() == "/dev/ttyACM0"


def test_ignores_builtin_uart_when_board_absent(monkeypatch):
    # A Raspberry Pi always enumerates its on-board UART; picking it would
    # make every frame time out instead of reporting "no board".
    _patch_ports(monkeypatch, [FakePort("/dev/ttyS0"), FakePort("/dev/ttyAMA0")])
    assert find_port() is None


def test_falls_back_to_lone_usb_serial(monkeypatch):
    _patch_ports(monkeypatch, [FakePort("/dev/ttyS0"), FakePort("/dev/ttyACM0")])
    assert find_port() == "/dev/ttyACM0"


def test_no_guess_when_multiple_usb_serial(monkeypatch):
    _patch_ports(monkeypatch, [FakePort("/dev/ttyACM0"), FakePort("/dev/ttyUSB0")])
    assert find_port() is None


# ---- an answer only counts from the board that was asked ----

class FakeSerial:
    """Hands back whatever frames were queued, one read at a time."""

    def __init__(self, *args, **kwargs):
        self.written = b""
        self.queued: "list[bytes]" = []

    def write(self, data):
        self.written += data

    def flush(self):
        pass

    def read(self, size=1):
        return self.queued.pop(0) if self.queued else b""

    def reset_input_buffer(self):
        pass

    def close(self):
        pass


def _bus(monkeypatch) -> "tuple[Bus, FakeSerial]":
    port = FakeSerial()
    monkeypatch.setattr(transport.serial, "Serial", lambda *a, **k: port)
    monkeypatch.setattr(transport.time, "sleep", lambda s: None)
    bus = Bus("/dev/fake", verbose=False)
    port.written = b""
    return bus, port


def _ack(src: int, cmd: int = 0x80) -> bytes:
    return Frame(dest=0x00, src=src, dev_type=0xFF, cmd=cmd).encode()


def test_an_ack_from_another_board_is_not_this_requests_answer(monkeypatch):
    # A board that was repainting when an earlier command arrived answers
    # it seconds later, inside the read window of whatever is asked next.
    # Taking that frame would tell the caller board 1 is awake when it was
    # board 20 talking - which, for the runner's landing check, means a
    # second broadcast and a second repaint of the whole wall.
    bus, port = _bus(monkeypatch)
    port.queued = [_ack(20), _ack(1, 0x81)]
    ack = bus.request(get_version(1), retries=1, timeout=1.0)
    assert ack is not None and ack.src == 1 and ack.cmd == 0x81
    assert port.written == get_version(1).encode()      # asked once


def test_only_strays_inside_the_window_time_the_request_out(monkeypatch):
    bus, port = _bus(monkeypatch)
    port.queued = [_ack(20)]
    assert bus.request(get_version(1), retries=1, timeout=0.05) is None


def test_a_broadcast_takes_whichever_board_answers(monkeypatch):
    bus, port = _bus(monkeypatch)
    port.queued = [_ack(7)]
    ack = bus.request(stop(0xFF), retries=1, timeout=0.05)
    assert ack is not None and ack.src == 7


# ---- resetting the master's USB device (ui/runner.py's recovery) ----

def _linux_with_a_master(monkeypatch, *, node_writable=False, usbreset=True):
    """usb_reset() as it runs on a Radxa, without touching anything real:
    the platform, the device found through sysfs, the node's access, the
    usbreset tool and sudo itself are all stand-ins."""
    monkeypatch.setattr(transport.sys, "platform", "linux")
    monkeypatch.setattr(transport, "_usb_device", lambda port: {
        "vid": 0x0483, "pid": 0x5740, "busnum": 1, "devnum": 7,
        "sysfs": "/sys/bus/usb/devices/1-1"})
    monkeypatch.setattr(transport.os, "access",
                        lambda node, mode: node_writable)
    monkeypatch.setattr(transport.shutil, "which",
                        lambda name: "/usr/bin/usbreset" if usbreset else None)
    ran = []

    class Done:
        returncode, stdout, stderr = 0, "", ""

    def run(argv, **kwargs):
        ran.append((argv, kwargs.get("timeout")))
        return Done()
    monkeypatch.setattr(transport.subprocess, "run", run)
    return ran


def test_usbreset_is_given_the_bus_and_device_never_vid_pid(monkeypatch):
    """Review of 349dcdd, M1: VID:PID names every master of that type on the
    machine; BBB/DDD is exactly the one behind this unit's port."""
    ran = _linux_with_a_master(monkeypatch)
    assert transport.usb_reset("/dev/ttyACM0") == (True, "sudo usbreset")
    argv, _ = ran[0]
    assert argv == ["sudo", "-n", "/usr/bin/usbreset", "001/007"], argv
    assert not any(":" in part for part in argv[3:]), "a VID:PID went out"


def test_a_sudo_that_hangs_is_given_only_the_callers_time(monkeypatch):
    """Review M2: the caller passes what is left of its own budget, and a
    sudo that does not answer in it is killed and reported."""
    ran = _linux_with_a_master(monkeypatch)
    transport.usb_reset("/dev/ttyACM0", timeout=1.25)
    assert ran[0][1] == 1.25
    transport.usb_reset("/dev/ttyACM0", timeout=99.0)
    assert ran[1][1] == transport.USBRESET_TIMEOUT_S     # never above its own

    def hang(argv, **kwargs):
        raise transport.subprocess.TimeoutExpired(argv, kwargs["timeout"])
    monkeypatch.setattr(transport.subprocess, "run", hang)
    done, how = transport.usb_reset("/dev/ttyACM0", timeout=0.5)
    assert done is False and "no answer in 0.5 s" in how


def test_usb_reset_never_raises(monkeypatch):
    """Review of 6a2d136, N1: the caller has just closed its port - anything
    going wrong in here (comports(), sysfs, odd bytes from usbreset) must be
    an answer it can act on, never an exception."""
    _linux_with_a_master(monkeypatch)

    def boom(port):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
    monkeypatch.setattr(transport, "_usb_device", boom)
    done, how = transport.usb_reset("/dev/ttyACM0", timeout=1.0)
    assert done is False and how.startswith("usb reset raised:"), how


def test_usbreset_output_is_decoded_without_raising(monkeypatch):
    ran = _linux_with_a_master(monkeypatch)
    seen = {}

    def run(argv, **kwargs):
        seen.update(kwargs)
        return ran and None or type("Done", (), {"returncode": 0,
                                                 "stdout": "", "stderr": ""})()
    monkeypatch.setattr(transport.subprocess, "run", run)
    transport.usb_reset("/dev/ttyACM0")
    assert seen.get("errors") == "replace" and seen.get("text") is True


def test_nothing_is_reset_off_linux():
    if sys.platform.startswith("linux"):
        return                                       # the Radxas: see above
    assert transport.usb_reset("/dev/ttyACM0")[0] is False
    assert transport.usb_reset_available("/dev/ttyACM0")[0] is False


# ---- usb_board_info: who is on the USB cable (2026-09-28) ----

class InfoPort:
    def __init__(self, device, serial_number=None, vid=0x0483, pid=0x5740,
                 location=None, usb_device_path=None):
        self.device = device
        self.serial_number = serial_number
        self.vid = vid
        self.pid = pid
        self.location = location
        self.usb_device_path = usb_device_path


def test_usb_board_info_names_the_serial_and_its_family(monkeypatch):
    monkeypatch.setattr(transport.sys, "platform", "win32")   # no sysfs
    _patch_ports(monkeypatch, [
        InfoPort("/dev/ttyACM1", serial_number="48E8854C324C"),
        InfoPort("/dev/ttyACM0", serial_number="5CF26F473930"),
    ])
    info = transport.usb_board_info("/dev/ttyACM0")
    assert info == {"serial": "5CF26F473930", "family": "3930",  # THAT port
                    "vid_pid": "0483:5740", "bcd": None}
    assert transport.usb_board_info("/dev/ttyACM1")["family"] == "324C"


def test_usb_board_family_of_known_and_unknown_serials():
    assert transport.usb_board_family("48E8854C324C") == "324C"
    assert transport.usb_board_family("5cef71563930") == "3930"
    assert transport.usb_board_family("0123456789AB") == "other"
    assert transport.usb_board_family(None) is None
    assert transport.usb_board_family("") is None


def test_usb_board_info_without_a_serial_or_a_port(monkeypatch):
    monkeypatch.setattr(transport.sys, "platform", "win32")
    _patch_ports(monkeypatch, [InfoPort("/dev/ttyACM0", serial_number=None,
                                        vid=None, pid=None)])
    blank = {"serial": None, "family": None, "vid_pid": None, "bcd": None}
    assert transport.usb_board_info("/dev/ttyACM0") == blank
    assert transport.usb_board_info("/dev/ttyACM9") == blank   # not there
    assert transport.usb_board_info(None) == blank
    assert transport.usb_board_info("") == blank


def test_usb_board_info_falls_back_to_sysfs_on_linux(monkeypatch, tmp_path):
    (tmp_path / "serial").write_text("5CEF71563930\n")
    (tmp_path / "bcdDevice").write_text("0200\n")
    _patch_ports(monkeypatch, [InfoPort("/dev/ttyACM0", serial_number=None,
                                        usb_device_path=str(tmp_path))])
    monkeypatch.setattr(transport.sys, "platform", "linux")
    info = transport.usb_board_info("/dev/ttyACM0")
    assert info == {"serial": "5CEF71563930", "family": "3930",
                    "vid_pid": "0483:5740", "bcd": "0200"}


def test_usb_board_info_never_raises(monkeypatch):
    def boom():
        raise OSError("comports exploded")
    monkeypatch.setattr(transport.list_ports, "comports", boom)
    assert transport.usb_board_info("/dev/ttyACM0") == {
        "serial": None, "family": None, "vid_pid": None, "bcd": None}
