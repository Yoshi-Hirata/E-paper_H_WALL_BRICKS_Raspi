import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

from epaper import transport
from epaper.transport import find_port


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
