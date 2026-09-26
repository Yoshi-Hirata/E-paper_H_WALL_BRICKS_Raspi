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
