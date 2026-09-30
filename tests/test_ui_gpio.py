"""ui/gpio.py's ButtonWatcher: press, release, hold - driven by hand.

No GPIO character device here: `_open` is replaced by a fake line, and
the watcher's edge and hold logic is called directly (start=False), the
way its own select() loop would call it.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ui import gpio
from ui.boards import Line


class FakeLine:
    """A python-periphery GPIO stand-in: `level` is what the pin reads
    (buttons are active low - False is pressed)."""

    def __init__(self):
        self.level = True
        self.fd = 3
        self.closed = False

    def read(self):
        return self.level

    def read_event(self):
        return None

    def close(self):
        self.closed = True


def make_watcher(monkeypatch, hold_s=1.0):
    lines = {}
    monkeypatch.setattr(gpio, "_open", lambda line, direction, **kw:
                        lines.setdefault(line.line, FakeLine()))
    events = []
    watcher = gpio.ButtonWatcher({"key1": Line(0, 21), "key3": Line(0, 16),
                                  "key2": Line(0, 20)},
                                 events.append,
                                 hold_events={"key1": hold_s, "key3": hold_s},
                                 start=False)
    return watcher, events, {"key1": lines[21], "key3": lines[16],
                             "key2": lines[20]}


def test_a_short_press_reports_on_release_and_a_hold_once(monkeypatch):
    watcher, events, lines = make_watcher(monkeypatch)
    lines["key3"].level = False
    watcher._edge("key3", True, 10.0)
    watcher._check_holds(10.5)
    assert events == []                          # nothing until it is known
    lines["key3"].level = True
    watcher._edge("key3", False, 10.6)
    assert events == ["key3"]
    # Held past the threshold, still down: the hold, and the release
    # afterwards is silent.
    lines["key3"].level = False
    watcher._edge("key3", True, 20.0)
    watcher._check_holds(20.9)
    assert events == ["key3"]
    watcher._check_holds(21.0)
    assert events == ["key3", "key3_hold"]
    watcher._check_holds(21.5)
    assert events == ["key3", "key3_hold"]       # once
    lines["key3"].level = True
    watcher._edge("key3", False, 21.6)
    assert events == ["key3", "key3_hold"]
    # A button without a hold reports on the press.
    watcher._edge("key2", True, 30.0)
    assert events[-1] == "key2"


def test_a_30ms_tap_whose_release_edge_was_debounced_is_not_a_hold(monkeypatch):
    # The press edge arrives, the release 30 ms later is inside the
    # 50 ms debounce and is dropped: the watcher never hears a release.
    # A second later the line is plainly up - that is the release, and
    # the tap was a short press (review of 951e0b7, LOW-4: this used to
    # fire key3_hold - LOOP flipped unseen - or key1_hold on REBOOT).
    watcher, events, lines = make_watcher(monkeypatch)
    for name in ("key3", "key1"):
        lines[name].level = False
        watcher._edge(name, True, 100.0)
        lines[name].level = True                 # released 30 ms later, unheard
        watcher._check_holds(100.5)
        assert events == [] or events[-1] != f"{name}_hold"
        watcher._check_holds(101.0)
        assert events[-1] == name                # the short press it was
        assert f"{name}_hold" not in events
        watcher._check_holds(102.0)
        assert events.count(name) == 1           # and only once
    assert events == ["key3", "key1"]


def test_an_unreadable_line_still_trusts_the_edges(monkeypatch):
    watcher, events, lines = make_watcher(monkeypatch)

    def broken():
        raise OSError("gpio read failed")
    lines["key1"].read = broken
    watcher._edge("key1", True, 5.0)
    watcher._check_holds(6.0)
    assert events == ["key1_hold"]
    watcher.close()
    assert all(line.closed for line in lines.values())
