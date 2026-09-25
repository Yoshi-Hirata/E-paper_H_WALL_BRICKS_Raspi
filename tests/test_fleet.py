"""conductor/fleet.py against real agents: clock offsets and firing together.

Three units' agents (ui/agent.py) run in this process on localhost, each
with its own runner on a FakeBus, so the whole path is exercised - the
PC's poll and offset measurement, the two-step cue over HTTP, and the
unit's timed broadcast.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conductor.fleet import (DEMO_LIST_EVERY_S, SUPERVISE_EVERY_S, TIMEOUT_S,
                             Fleet, UnitLink, default_units)
from conductor.server import Workspace, make_server
from tests.test_look import GRID, MAP, SKIRT_GRID, SKIRT_MAP
from tests.test_ui_remote import SHOW, make_session, wait_until
from ui.agent import Agent

# The PC's clock in these tests is 500 s behind the units', so a fire
# time that was not converted per unit would be 500 s away, not 0.3 s.
PC_BEHIND_S = 500.0


def pc_clock() -> float:
    return time.monotonic() - PC_BEHIND_S


class Unit:
    def __init__(self, name):
        self.session, self.runner, self.bus = make_session()
        self.agent = Agent(self.session, port=0, host="127.0.0.1", name=name)
        self.agent.start()
        self.address = f"127.0.0.1:{self.agent.port}"

    def close(self):
        self.agent.stop()
        self.runner.stop()

    @property
    def shows(self):
        return [f for f in self.bus.sent if f.cmd == SHOW]


@pytest.fixture
def units():
    made = {name: Unit(name) for name in ("radxa-01", "radxa-02", "radxa-03")}
    yield made
    for unit in made.values():
        unit.close()


@pytest.fixture
def fleet(units):
    addresses = {name: unit.address for name, unit in units.items()}
    addresses["radxa-09"] = "127.0.0.1:9"           # nothing listens there
    fleet = Fleet(addresses, poll_s=0.03, clock=pc_clock)
    fleet.start()
    assert wait_until(lambda: all(fleet.links[n].offset is not None
                                  and len(fleet.links[n]._samples) >= 3
                                  for n in units))
    yield fleet
    fleet.stop()


def array(color):
    return bytes([0xFE] + [color] * 60 + [0xFF, 0xFF, 0xFE]).hex()


def payload(cue, color, boards=(1, 2)):
    return {"cue": cue, "label": f"test {color}", "dev_type": 3,
            "boards": {str(b): array(color) for b in boards}}


def test_default_addresses_are_the_ones_firstboot_assigns():
    units = default_units()
    assert len(units) == 10
    assert units["radxa-01"] == "192.168.51.101:8787"
    assert units["radxa-10"] == "192.168.51.110:8787"


def test_polling_finds_the_units_and_measures_their_clocks(fleet, units):
    snap = {u["name"]: u for u in fleet.snapshot()["units"]}
    for name in units:
        assert snap[name]["online"] and snap[name]["host"] == name
        assert snap[name]["phase"] == "local"
        assert snap[name]["sync_ms"] < 50
        # unit clock = PC clock + 500 s, measured to a few milliseconds
        assert abs(fleet.links[name].offset - PC_BEHIND_S) < 0.05
    # A refused or timed-out connection takes its time to say so.
    assert wait_until(lambda: fleet.links["radxa-09"].error is not None)
    gone = fleet.links["radxa-09"].snapshot()
    assert not gone["online"] and gone["error"] and gone["phase"] is None


def test_prepare_then_fire_lands_on_every_unit_at_one_instant(fleet, units):
    results = fleet.prepare({name: payload("c1", 3) for name in units})
    assert all(r["ok"] for r in results.values())
    assert wait_until(lambda: all(u.session.phase == "ready"
                                  for u in units.values()))
    assert all(u.shows == [] for u in units.values())

    results = fleet.fire({name: "c1" for name in units}, lead_s=0.4)
    assert all(r["ok"] for r in results.values())
    assert wait_until(lambda: all(u.session.phase == "fired"
                                  for u in units.values()))
    fired = [u.session.status()["fired_at"] for u in units.values()]
    late = [u.session.status()["late_ms"] for u in units.values()]
    assert all(len(u.shows) == 1 for u in units.values())
    assert all(0 <= ms < 60 for ms in late), late       # on time, never early
    assert max(fired) - min(fired) < 0.06               # together
    assert wait_until(lambda: all(
        u["late_ms"] is not None for u in fleet.snapshot()["units"]
        if u["name"] in units))


def test_an_unreachable_unit_does_not_hold_up_the_others(fleet, units):
    cues = {name: payload("c2", 4) for name in list(units) + ["radxa-09"]}
    results = fleet.prepare(cues)
    assert not results["radxa-09"]["ok"] and results["radxa-01"]["ok"]
    assert wait_until(lambda: all(u.session.phase == "ready"
                                  for u in units.values()))
    results = fleet.fire({name: "c2" for name in cues}, lead_s=0.2)
    assert "clock not measured" in results["radxa-09"]["error"]
    assert wait_until(lambda: all(u.session.phase == "fired"
                                  for u in units.values()))


def test_unit_side_refusals_come_back_per_unit(fleet, units):
    results = fleet.fire({"radxa-01": "never-loaded"}, lead_s=0.2)
    assert not results["radxa-01"]["ok"]
    assert "not the one loaded" in results["radxa-01"]["error"]
    results = fleet.simple(["radxa-01", "radxa-02"], "/standby")
    assert all(r["ok"] and r["phase"] == "standby" for r in results.values())
    results = fleet.simple(["radxa-01", "radxa-02"], "/release")
    assert all(r["phase"] == "local" for r in results.values())


def test_a_rebooted_unit_starts_its_clock_measurement_over():
    link = UnitLink("radxa-01", "127.0.0.1:9", clock=lambda: 1000.0)
    for n in range(5):
        link._learn({"mono": 5000.0 + n * 0.001}, 1000.0 - 0.004, 1000.0 + 0.004)
    assert len(link._samples) == 5 and abs(link.offset - 4000.0) < 0.01
    link._learn({"mono": 12.0}, 999.99, 1000.01)     # monotonic restarted?
    # One sample far off may be a stalled packet: the history is kept...
    assert len(link._samples) == 5 and abs(link.offset - 4000.0) < 0.01
    link._learn({"mono": 12.5}, 1000.49, 1000.51)    # ...a second agrees:
    assert len(link._samples) == 2 and abs(link.offset - -988.0) < 0.01


def test_a_lone_wild_sample_does_not_cost_the_clock_history():
    link = UnitLink("radxa-01", "127.0.0.1:9", clock=lambda: 1000.0)
    for n in range(5):
        link._learn({"mono": 5000.0}, 1000.0 - 0.004, 1000.0 + 0.004)
    link._learn({"mono": 5003.0}, 999.0, 1000.004)   # a 1 s stall on the way
    link._learn({"mono": 5000.0}, 1000.0 - 0.004, 1000.0 + 0.004)
    assert len(link._samples) == 6 and abs(link.offset - 4000.0) < 0.01
    link._learn({"mono": 4990.0}, 999.0, 1001.0)     # two wild ones that do
    link._learn({"mono": 5010.0}, 999.0, 1001.0)     # not agree: both dropped
    assert abs(link.offset - 4000.0) < 0.01


def test_the_shortest_round_trip_is_the_one_believed():
    link = UnitLink("radxa-01", "127.0.0.1:9", clock=lambda: 0.0)
    link._learn({"mono": 100.050}, 0.0, 0.200)       # slow, lopsided: +99.95
    link._learn({"mono": 100.102}, 0.100, 0.104)     # quick: +100.000
    link._learn({"mono": 100.300}, 0.150, 0.350)     # slow again
    assert abs(link.offset - 100.0) < 1e-6
    assert link.snapshot()["sync_ms"] == 2.0         # half of 4 ms


# ---- through the conductor's HTTP API, from the CSVs ----

def test_compile_units_addresses_a_shared_unit_as_one_bus(tmp_path):
    ws = Workspace(tmp_path)
    for name, text in (("Look20-Top_map.csv", MAP),
                       ("Look20-Top_color_pattern01_grid.csv", GRID),
                       ("Look20-Skirt_map.csv", SKIRT_MAP),
                       ("Look20-Skirt_color_pattern01_grid.csv", SKIRT_GRID)):
        ws.save(name, text)
    ws.assign("Look20-Top", "radxa-02")
    ws.assign("Look20-Skirt", "radxa-02")
    payloads, problems = ws.compile_units(
        {"Look20-Top": "Look20-Top_color_pattern01_grid.csv",
         "Look20-Skirt": "Look20-Skirt_color_pattern01_grid.csv"}, "c9")
    assert problems == [] and list(payloads) == ["radxa-02"]
    body = payloads["radxa-02"]
    assert sorted(body["boards"], key=int) == ["1", "2", "3", "4", "5"]
    assert bytes.fromhex(body["boards"]["1"])[7] == 0x0A        # skirt, board 001
    assert bytes.fromhex(body["boards"]["3"])[1] == 0x03        # top, board 017
    assert "Look20-Skirt P01" in body["label"] and "Look20-Top P01" in body["label"]
    # Only the top chosen: its boards keep the unit-wide addresses 3-5.
    payloads, _ = ws.compile_units(
        {"Look20-Top": "Look20-Top_color_pattern01_grid.csv"}, "c10")
    assert sorted(payloads["radxa-02"]["boards"], key=int) == ["3", "4", "5"]


def test_compile_units_reports_what_cannot_be_sent(tmp_path):
    ws = Workspace(tmp_path)
    ws.save("Look22_map.csv", MAP)
    ws.save("Look22_color_pattern01_grid.csv", GRID)
    payloads, problems = ws.compile_units(
        {"Look22": "Look22_color_pattern01_grid.csv", "Look99": "x.csv"}, "c1")
    assert payloads == {}
    assert any("not assigned" in p for p in problems) and any("Look99" in p for p in problems)


def test_prepare_and_fire_through_the_conductor_api(tmp_path, fleet, units):
    ws = Workspace(tmp_path)
    ws.save("Look22_map.csv", MAP)
    ws.save("Look22_color_pattern01_grid.csv", GRID)
    ws.assign("Look22", "radxa-02")
    server = make_server(tmp_path, port=0, fleet=fleet)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def call(path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}", data=data,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read())

    try:
        listed = {u["name"] for u in call("/api/fleet")["units"]}
        assert {"radxa-01", "radxa-02", "radxa-03", "radxa-09"} == listed
        result = call("/api/fleet/prepare",
                      {"choices": {"Look22": "Look22_color_pattern01_grid.csv"}})
        assert result["problems"] == [] and result["units"]["radxa-02"]["ok"]
        unit = units["radxa-02"]
        assert wait_until(lambda: unit.session.phase == "ready")
        assert unit.session.status()["label"] == "Look22 P01"
        assert unit.runner.boards == [1, 2, 3]
        assert call("/api/fleet")["prepared"] == {"radxa-02": result["cue"]}
        fired = call("/api/fleet/fire", {"lead_s": 0.5})
        assert fired["units"]["radxa-02"]["ok"]
        assert wait_until(lambda: unit.session.phase == "fired")
        assert 0 <= unit.session.status()["late_ms"] < 60
        assert units["radxa-01"].shows == []             # not part of the cue
        released = call("/api/fleet/release", {"units": ["radxa-02"]})
        assert released["units"]["radxa-02"]["phase"] == "local"
    finally:
        server.shutdown()
        server.server_close()


# ---- what the operator stopped stays stopped ----

class StubLink:
    """A unit as the fleet sees it, without a network."""

    def __init__(self, name, show_state, offset=5.0):
        self.name, self.offset, self.online = name, offset, True
        self.status = {"show": {"id": "showA", "state": show_state, "t0": 1005.0,
                                "synced": True}}
        self.posted = []
        self.demos = []           # what a GET /demo/list would answer

    def post(self, path, body, learn=True, timeout=None):
        self.posted.append((path, body))
        self.learned = learn      # last call's `learn`, for the demo tests
        return {}

    def get(self, path, learn=True, timeout=None):
        self.learned = learn
        if path == "/demo/list":
            return {"demos": self.demos}
        return {}

    def snapshot(self):
        return {"name": self.name, "online": True, "show": self.status["show"]}


class FailingLink(StubLink):
    """A unit that is offline (or refuses) for every command."""

    def post(self, path, body, learn=True, timeout=None):
        raise RuntimeError("timed out")

    def get(self, path, learn=True, timeout=None):
        raise RuntimeError("timed out")


def test_a_stopped_show_is_not_adopted_back_from_a_unit_that_missed_the_stop():
    fleet = Fleet({}, clock=lambda: 1100.0)
    fleet.links = {"radxa-01": StubLink("radxa-01", "stopped"),
                   "radxa-02": StubLink("radxa-02", "running")}   # missed it
    fleet.shows = {"radxa-01": {"id": "showA", "cues": [], "duration": 600},
                   "radxa-02": {"id": "showA", "cues": [], "duration": 600}}
    fleet.start_show(lead_s=1.0)
    fleet.stop_show()
    for link in fleet.links.values():
        link.posted.clear()
    fleet._corrected.clear()
    assert fleet.snapshot()["run"] is None          # not resurrected
    for link in fleet.links.values():
        fleet._supervise(link)
    assert fleet.links["radxa-01"].posted == []     # the stopped unit is left
    assert fleet.links["radxa-02"].posted == [("/show/stop", {})]
    assert any("missed STOP" in line for line in fleet.corrections)


def test_a_fresh_conductor_adopts_a_running_show_once():
    fleet = Fleet({}, clock=lambda: 1100.0)
    fleet.links = {"radxa-01": StubLink("radxa-01", "running")}
    run = fleet.snapshot()["run"]
    assert run["adopted"] and run["state"] == "running"
    assert abs(run["t0"] - 1000.0) < 1e-6           # unit T0 - its offset
    fleet.stop_show()
    assert fleet.snapshot()["run"] is None          # and never again


def test_a_unit_that_missed_the_stop_is_told_once_not_fought():
    fleet = Fleet({}, clock=lambda: 1100.0)
    link = StubLink("radxa-02", "running")
    fleet.links = {"radxa-02": link}
    fleet.shows = {"radxa-02": {"id": "showA", "cues": [], "duration": 600}}
    fleet.start_show(lead_s=1.0)
    fleet.stop_show()
    link.posted.clear()
    for _ in range(3):
        fleet._corrected.clear()
        fleet._supervise(link)
    assert link.posted == [("/show/stop", {})]      # once; then it is theirs


def test_a_restored_unit_is_told_its_t0_even_when_it_is_close_enough():
    fleet = Fleet({}, clock=lambda: 1100.0)
    link = StubLink("radxa-02", "running")
    link.status["show"]["synced"] = False           # running on its disk's T0
    fleet.links = {"radxa-02": link}
    fleet.shows = {"radxa-02": {"id": "showA", "cues": [], "duration": 600}}
    fleet.run = {"t0": 1000.0, "state": "running", "held_at": None}
    fleet._supervise(link)
    assert link.posted == [("/show/run", {"t0": 1005.0, "show": "showA",
                                          "force": False})]
    assert "confirmed" in fleet.corrections[-1]
    link.status["show"]["synced"] = True
    link.posted.clear(); fleet._corrected.clear()
    fleet._supervise(link)
    assert link.posted == []


# ---- the pre-burn design: every picture written at Upload, not live ----

def test_start_refuses_while_a_unit_is_still_burning_its_pictures():
    fleet = Fleet({}, clock=lambda: 1100.0)
    link = StubLink("radxa-04", "stopped")
    link.status["show"]["burn"] = {"done": 12, "total": 48, "failed": [],
                                   "state": "burning"}
    fleet.links = {"radxa-04": link}
    fleet.shows = {"radxa-04": {"id": "showA", "cues": [], "duration": 600}}
    with pytest.raises(ValueError, match="radxa-04: still writing 12/48"):
        fleet.start_show(lead_s=1.0)
    assert fleet.run is None                        # never armed


def test_start_refuses_while_a_unit_failed_to_burn():
    fleet = Fleet({}, clock=lambda: 1100.0)
    link = StubLink("radxa-05", "stopped")
    link.status["show"]["burn"] = {"done": 40, "total": 48,
                                   "failed": [[3, 5], [7, 5]], "state": "failed"}
    fleet.links = {"radxa-05": link}
    fleet.shows = {"radxa-05": {"id": "showA", "cues": [], "duration": 600}}
    # Pairs, not boards: one board of a 12-board garment losing every
    # cue is 18 pictures, not "1 board" (review round 2).
    with pytest.raises(ValueError, match=r"radxa-05: 2 of 48 pictures not "
                                         r"written on boards 3, 7"):
        fleet.start_show(lead_s=1.0)
    # `force` waves through the failed boards - but never a unit still
    # burning, offline, or holding some other show (see the tests below).
    fleet.start_show(lead_s=1.0, force=True)
    assert fleet.run is not None


def test_start_ignores_an_offline_units_stale_burned_report():
    # A unit that answered "burned" once, then went offline, must not
    # wave START through on that stale word - it might have rebooted and
    # lost everything since (found in review).
    fleet = Fleet({}, clock=lambda: 1100.0)
    link = StubLink("radxa-11", "stopped")
    link.status["show"]["burn"] = {"done": 48, "total": 48, "failed": [],
                                   "state": "burned"}
    link.online = False
    fleet.links = {"radxa-11": link}
    fleet.shows = {"radxa-11": {"id": "showA", "cues": [], "duration": 600}}
    with pytest.raises(ValueError, match="radxa-11: not answering"):
        fleet.start_show(lead_s=1.0, force=True)   # not even force helps


def test_start_refuses_a_unit_that_has_not_taken_this_show_yet():
    # The unit is online and says "burned" - but about a DIFFERENT show
    # (an earlier upload, or one from a previous conductor session): its
    # burn report is not about what is about to run (found in review).
    fleet = Fleet({}, clock=lambda: 1100.0)
    link = StubLink("radxa-12", "stopped")
    link.status["show"]["id"] = "showOLD"
    link.status["show"]["burn"] = {"done": 48, "total": 48, "failed": [],
                                   "state": "burned"}
    fleet.links = {"radxa-12": link}
    fleet.shows = {"radxa-12": {"id": "showNEW", "cues": [], "duration": 600}}
    with pytest.raises(ValueError, match="radxa-12: has not taken this show yet"):
        fleet.start_show(lead_s=1.0, force=True)


def test_start_lists_every_burning_unit_together():
    fleet = Fleet({}, clock=lambda: 1100.0)
    a = StubLink("radxa-13", "stopped")
    a.status["show"]["burn"] = {"done": 1, "total": 10, "failed": [],
                                "state": "burning"}
    b = StubLink("radxa-14", "stopped")
    b.status["show"]["burn"] = {"done": 5, "total": 10, "failed": [],
                                "state": "burning"}
    fleet.links = {"radxa-13": a, "radxa-14": b}
    fleet.shows = {"radxa-13": {"id": "showA", "cues": [], "duration": 600},
                   "radxa-14": {"id": "showA", "cues": [], "duration": 600}}
    with pytest.raises(ValueError, match="radxa-13: still writing 1/10; "
                                         "radxa-14: still writing 5/10"):
        fleet.start_show(lead_s=1.0)


def test_force_never_waves_through_a_unit_still_burning():
    fleet = Fleet({}, clock=lambda: 1100.0)
    link = StubLink("radxa-15", "stopped")
    link.status["show"]["burn"] = {"done": 1, "total": 10, "failed": [],
                                   "state": "burning"}
    fleet.links = {"radxa-15": link}
    fleet.shows = {"radxa-15": {"id": "showA", "cues": [], "duration": 600}}
    with pytest.raises(ValueError, match="still writing"):
        fleet.start_show(lead_s=1.0, force=True)


def test_a_unit_burning_its_own_demo_says_so():
    fleet = Fleet({}, clock=lambda: 1100.0)
    link = StubLink("radxa-16", "stopped")
    link.status["show"]["demo"] = True
    link.status["show"]["burn"] = {"done": 3, "total": 10, "failed": [],
                                   "state": "burning"}
    fleet.links = {"radxa-16": link}
    fleet.shows = {"radxa-16": {"id": "showA", "cues": [], "duration": 600}}
    with pytest.raises(ValueError, match=r"radxa-16: writing its demo "
                                         r"pictures \(3/10\) - wait or STOP it"):
        fleet.start_show(lead_s=1.0)


def test_start_runs_once_every_unit_has_burned():
    fleet = Fleet({}, clock=lambda: 1100.0)
    link = StubLink("radxa-06", "stopped")
    link.status["show"]["burn"] = {"done": 48, "total": 48, "failed": [],
                                   "state": "burned"}
    fleet.links = {"radxa-06": link}
    fleet.shows = {"radxa-06": {"id": "showA", "cues": [], "duration": 600}}
    fleet.start_show(lead_s=1.0)
    assert fleet.run is not None
    # An old agent that says nothing about burning at all is not held up.
    fleet.run = None
    del link.status["show"]["burn"]
    fleet.start_show(lead_s=1.0)
    assert fleet.run is not None


def test_preset_waits_for_the_burn_like_start_does():
    fleet = Fleet({}, clock=lambda: 1100.0)
    link = StubLink("radxa-04", "stopped")
    link.status["show"]["burn"] = {"done": 12, "total": 48, "failed": [],
                                   "state": "burning"}
    fleet.links = {"radxa-04": link}
    fleet.shows = {"radxa-04": {"id": "showA", "cues": [], "duration": 600}}
    with pytest.raises(ValueError, match="radxa-04: still writing 12/48"):
        fleet.preset()
    assert not any(path == "/show/preset" for path, _ in link.posted)
    link.status["show"]["burn"]["state"] = "failed"
    link.status["show"]["burn"]["failed"] = [[3, 1]]
    with pytest.raises(ValueError, match="radxa-04: 1 of 48 pictures not "
                                         "written on board 3"):
        fleet.preset()
    link.status["show"]["burn"] = {"done": 48, "total": 48, "failed": [],
                                   "state": "burned"}
    fleet.preset()
    assert any(path == "/show/preset" for path, _ in link.posted)


def test_preset_force_waves_through_a_failed_burn_and_reaches_the_unit():
    # One absent board is the common case: its burn "failed", and the
    # operator, asked by the page, shows the 0:00 look anyway. The unit's
    # own gate needs to hear the same `force` (review F3).
    fleet = Fleet({}, clock=lambda: 1100.0)
    link = StubLink("radxa-04", "stopped")
    link.status["show"]["burn"] = {"done": 46, "total": 48,
                                   "failed": [[3, 1], [3, 2]], "state": "failed"}
    fleet.links = {"radxa-04": link}
    fleet.shows = {"radxa-04": {"id": "showA", "cues": [], "duration": 600}}
    with pytest.raises(ValueError, match="radxa-04: 2 of 48 pictures not "
                                         "written on board 3"):
        fleet.preset()
    assert link.posted == []
    results = fleet.preset(force=True)
    assert results["radxa-04"]["ok"]
    assert link.posted == [("/show/preset", {"force": True})]
    # Without force the unit is told so, explicitly.
    link.posted.clear()
    link.status["show"]["burn"] = {"done": 48, "total": 48, "failed": [],
                                   "state": "burned"}
    fleet.preset()
    assert link.posted == [("/show/preset", {"force": False})]


def test_preset_force_never_waves_through_burning_cancelled_or_none():
    fleet = Fleet({}, clock=lambda: 1100.0)
    link = StubLink("radxa-04", "stopped")
    fleet.links = {"radxa-04": link}
    fleet.shows = {"radxa-04": {"id": "showA", "cues": [], "duration": 600}}
    for burn, words in [
            ({"done": 1, "total": 9, "failed": [], "state": "burning"},
             "still writing 1/9"),
            ({"done": 4, "total": 9, "failed": [], "state": "cancelled"},
             r"pictures not written \(cancelled\) - Upload again"),
            ({"done": 4, "total": 9, "failed": [], "state": "cancelled",
              "reason": "no boards answering"},
             r"pictures not written \(cancelled: no boards answering\) "
             r"- Upload again"),
            ({"done": 0, "total": 0, "failed": [], "state": "none"},
             "pictures not written since it restarted - Upload again"),
            (None, "pictures not written - Upload again")]:
        link.status["show"]["burn"] = burn
        with pytest.raises(ValueError, match=f"radxa-04: {words}"):
            fleet.preset(force=True)
        with pytest.raises(ValueError, match=f"radxa-04: {words}"):
            fleet.start_show(lead_s=1.0, force=True)
    assert link.posted == [] and fleet.run is None


# ---- review F1: a burn that is not "burned" is not "no opinion" ----

def test_a_new_agent_saying_burn_null_blocks_start_an_old_agent_without_the_key_does_not():
    # `burn: null` from an agent that DOES report burns means "nothing is
    # written" (a load that could not start its burn) - the opposite of
    # an old agent that has no "burn" key at all, which START must not
    # hold up. The two used to be indistinguishable (review F1).
    fleet = Fleet({}, clock=lambda: 1100.0)
    link = StubLink("radxa-04", "stopped")
    fleet.links = {"radxa-04": link}
    fleet.shows = {"radxa-04": {"id": "showA", "cues": [], "duration": 600}}
    link.status["show"]["burn"] = None
    with pytest.raises(ValueError, match="radxa-04: pictures not written - Upload again"):
        fleet.start_show(lead_s=1.0, force=True)
    assert fleet.run is None
    del link.status["show"]["burn"]
    fleet.start_show(lead_s=1.0)
    assert fleet.run is not None


def test_a_cancelled_or_lost_burn_blocks_start_with_its_own_words():
    fleet = Fleet({}, clock=lambda: 1100.0)
    link = StubLink("radxa-04", "stopped")
    fleet.links = {"radxa-04": link}
    fleet.shows = {"radxa-04": {"id": "showA", "cues": [], "duration": 600}}
    # STOP during the burn: the slots hold the previous show's pictures.
    link.status["show"]["burn"] = {"done": 12, "total": 48, "failed": [],
                                   "state": "cancelled"}
    with pytest.raises(ValueError, match=r"radxa-04: pictures not written "
                                         r"\(cancelled\) - Upload again"):
        fleet.start_show(lead_s=1.0, force=True)
    # The unit restarted after the Upload: nothing says the burn finished.
    link.status["show"]["burn"] = {"done": 0, "total": 0, "failed": [],
                                   "state": "none"}
    with pytest.raises(ValueError, match="radxa-04: pictures not written "
                                         "since it restarted - Upload again"):
        fleet.start_show(lead_s=1.0, force=True)
    # A state this conductor does not know is not waved through either.
    link.status["show"]["burn"] = {"state": "verifying"}
    with pytest.raises(ValueError, match=r"pictures not written \(verifying\)"):
        fleet.start_show(lead_s=1.0, force=True)
    assert fleet.run is None and link.posted == []


def test_the_snapshot_counts_a_null_cancelled_or_lost_burn_as_not_written():
    fleet = Fleet({}, clock=lambda: 1100.0)
    links = {}
    for name, burn in [("radxa-01", {"done": 2, "total": 2, "failed": [],
                                     "state": "burned"}),
                       ("radxa-02", None),
                       ("radxa-03", {"done": 1, "total": 2, "failed": [],
                                     "state": "cancelled"}),
                       ("radxa-04", {"done": 0, "total": 0, "failed": [],
                                     "state": "none"})]:
        links[name] = StubLink(name, "stopped")
        links[name].status["show"]["burn"] = burn
    old = StubLink("radxa-05", "stopped")           # no "burn" key: says nothing
    assert "burn" not in old.status["show"]
    links["radxa-05"] = old
    fleet.links = links
    fleet.shows = {n: {"id": "showA", "cues": [], "duration": 600} for n in links}
    assert fleet.snapshot()["burn"] == {"burned": 1, "total": 4}


def test_a_partly_failed_burn_is_counted_in_pictures_and_names_three_boards():
    fleet = Fleet({}, clock=lambda: 1100.0)
    link = StubLink("radxa-04", "stopped")
    fleet.links = {"radxa-04": link}
    fleet.shows = {"radxa-04": {"id": "showA", "cues": [], "duration": 600}}
    link.status["show"]["burn"] = {
        "done": 12, "total": 12, "state": "failed",
        "failed": [[b, slot] for b in (1, 2, 3) for slot in (1, 2, 3)]
        + [[9, 1]]}
    with pytest.raises(ValueError, match="radxa-04: 10 of 12 pictures not "
                                         r"written on boards 1, 2, 3 \+1 more"):
        fleet.start_show(lead_s=1.0)
    fleet.start_show(lead_s=1.0, force=True)        # the genuine "anyway"
    assert fleet.run is not None


def test_a_garment_that_answered_on_no_board_is_named_that_way_and_forceable():
    # A feed switched off must not hold the other nine units out of the
    # show: the unit reports a fully walked burn whose every pair is
    # absent, and START says so in the unit's own words and goes ahead
    # once the operator has answered the page's question (2026-09-25).
    fleet = Fleet({}, clock=lambda: 1100.0)
    link = StubLink("radxa-07", "stopped")
    fleet.links = {"radxa-07": link}
    fleet.shows = {"radxa-07": {"id": "showA", "cues": [], "duration": 600}}
    link.status["show"]["burn"] = {
        "done": 48, "total": 48, "state": "failed",
        "reason": "none of its 16 boards answered",
        "failed": [[b, s] for b in range(1, 17) for s in range(1, 4)]}
    with pytest.raises(ValueError,
                       match="radxa-07: none of its 16 boards answered"):
        fleet.start_show(lead_s=1.0)
    fleet.start_show(lead_s=1.0, force=True)
    assert fleet.run is not None and link.posted[-1][1]["force"] is True


def test_a_forced_upload_under_a_running_show_leaves_force_on_the_run():
    # R4 (review round 3): the rescue Upload is pointless if supervision
    # then posts force false and the rescued unit - whose re-burn failed
    # on a live board - is refused and never rejoins.
    fleet = Fleet({}, clock=lambda: 1100.0)
    link = StubLink("radxa-01", "stopped")
    fleet.links = {"radxa-01": link}
    shows = {"radxa-01": {"id": "showA", "cues": [], "duration": 600}}
    fleet.shows = dict(shows)
    fleet.start_show(lead_s=1.0)                    # no force needed then
    assert fleet.run["force"] is False
    fleet.upload(shows)                             # (not under a run: no-op)
    assert fleet.run["force"] is False
    fleet.upload(shows, force=True)                 # the page's confirm
    assert fleet.run["force"] is True


def test_an_adopted_run_carries_force_so_supervision_is_not_refused():
    # A conductor restarted mid-show builds the run from what the units
    # are already playing: that show passed the burn gate when it was
    # started, so its /show/run must not go back out with force false
    # and be refused by a unit whose burn failed on a board (round 2).
    fleet = Fleet({}, clock=lambda: 1100.0)
    link = StubLink("radxa-01", "running")
    link.status["show"].update({"state": "running", "synced": True,
                                "t0": 1000.0})
    link.offset = 0.0
    fleet.links = {"radxa-01": link}
    fleet.snapshot()                                # _adopt() runs here
    assert fleet.run and fleet.run["adopted"] and fleet.run["force"] is True


def test_the_tile_carries_the_last_refusal_this_unit_gave_supervision():
    fleet = Fleet({}, clock=lambda: 1100.0)
    link = StubLink("radxa-01", "stopped")
    fleet.links = {"radxa-01": link}
    tile = next(u for u in fleet.snapshot()["units"] if u["name"] == "radxa-01")
    assert tile["refused"] is None
    fleet._refused["radxa-01"] = ("run", "still writing 12/48")
    tile = next(u for u in fleet.snapshot()["units"] if u["name"] == "radxa-01")
    assert tile["refused"] == "run refused: still writing 12/48"


# ---- review F2: START's force reaches the units, for the whole run ----

def test_start_force_is_posted_to_the_unit_and_kept_for_the_run():
    fleet = Fleet({}, clock=lambda: 1100.0)
    link = StubLink("radxa-05", "stopped")
    link.status["show"]["burn"] = {"done": 40, "total": 48,
                                   "failed": [[3, 5]], "state": "failed"}
    fleet.links = {"radxa-05": link}
    fleet.shows = {"radxa-05": {"id": "showA", "cues": [], "duration": 600}}
    fleet.start_show(lead_s=1.0, force=True)
    assert fleet.run["force"] is True
    assert link.posted == [("/show/run", {"t0": 1101.0 + link.offset,
                                          "show": "showA", "force": True})]
    # SEEK/RESUME/NEXT of this run: the unit's gate hears the same force.
    link.posted.clear()
    fleet.seek(30.0, lead_s=1.0)
    assert link.posted[-1][1]["force"] is True
    # ...and so does the supervision's own /show/run.
    link.posted.clear()
    fleet._corrected.clear()
    link.status["show"].update(state="running", t0=None, synced=True)
    fleet._supervise(link)
    assert link.posted == [("/show/run", {"t0": fleet.run["t0"] + link.offset,
                                          "show": "showA", "force": True})]


def test_start_without_force_posts_force_false():
    fleet = Fleet({}, clock=lambda: 1100.0)
    link = StubLink("radxa-06", "stopped")
    link.status["show"]["burn"] = {"done": 48, "total": 48, "failed": [],
                                   "state": "burned"}
    fleet.links = {"radxa-06": link}
    fleet.shows = {"radxa-06": {"id": "showA", "cues": [], "duration": 600}}
    fleet.start_show(lead_s=1.0)
    assert fleet.run["force"] is False
    assert link.posted[0][1]["force"] is False


def test_force_never_waves_through_burning_even_at_the_unit():
    # The fleet-level gate stops a burning unit before anything is
    # posted: no /show/run with force ever reaches it (review F2).
    fleet = Fleet({}, clock=lambda: 1100.0)
    link = StubLink("radxa-15", "stopped")
    link.status["show"]["burn"] = {"done": 1, "total": 10, "failed": [],
                                   "state": "burning"}
    fleet.links = {"radxa-15": link}
    fleet.shows = {"radxa-15": {"id": "showA", "cues": [], "duration": 600}}
    with pytest.raises(ValueError, match="still writing"):
        fleet.start_show(lead_s=1.0, force=True)
    assert link.posted == [] and fleet.run is None


# ---- review F6: a refused supervision command is not hammered ----

class RefusingRunLink(StubLink):
    """A unit whose /show/run is refused (409 - a live board did not
    take the burn), every time."""

    def post(self, path, body, learn=True, timeout=None):
        super().post(path, body, learn, timeout)
        if path == "/show/run":
            raise RuntimeError("board 7 did not take the burn - reload the show")
        return {}


def test_supervise_records_a_refused_run_once_and_waits_before_retrying():
    now = [1100.0]
    fleet = Fleet({}, clock=lambda: now[0])
    link = RefusingRunLink("radxa-04", "stopped")
    link.status["show"]["t0"] = None
    fleet.links = {"radxa-04": link}
    fleet.shows = {"radxa-04": {"id": "showA", "cues": [], "duration": 600}}
    fleet.run = {"t0": 1000.0, "state": "running", "held_at": None}
    fleet._supervise(link)                          # refused - no exception out
    assert [p for p, _ in link.posted] == ["/show/run"]
    assert fleet.corrections[-1].endswith(
        "radxa-04: run refused: board 7 did not take the burn - reload the show")
    # The next polls, within SUPERVISE_EVERY_S: nothing is posted again.
    now[0] += 0.5
    fleet._supervise(link)
    now[0] += 0.5
    fleet._supervise(link)
    assert [p for p, _ in link.posted] == ["/show/run"]
    # After the pause it tries once more - and the standing refusal is
    # not written down a second time.
    said = len(fleet.corrections)
    now[0] += SUPERVISE_EVERY_S
    fleet._supervise(link)
    assert [p for p, _ in link.posted] == ["/show/run", "/show/run"]
    assert len(fleet.corrections) == said


def test_supervise_forgets_a_refusal_once_the_unit_takes_the_command():
    now = [1100.0]
    fleet = Fleet({}, clock=lambda: now[0])
    link = RefusingRunLink("radxa-04", "stopped")
    link.status["show"]["t0"] = None
    fleet.links = {"radxa-04": link}
    fleet.shows = {"radxa-04": {"id": "showA", "cues": [], "duration": 600}}
    fleet.run = {"t0": 1000.0, "state": "running", "held_at": None}
    fleet._supervise(link)
    assert "run refused" in fleet.corrections[-1]
    # The operator reloaded the show; the unit now takes /show/run.
    link.post = lambda path, body, learn=True, timeout=None: (
        link.posted.append((path, body)) or {})
    now[0] += SUPERVISE_EVERY_S
    fleet._supervise(link)
    assert fleet.corrections[-1].endswith("radxa-04: started late")
    assert "radxa-04" not in fleet._refused
    # Refused again later: said again, because it is news again.
    link.post = RefusingRunLink.post.__get__(link)
    now[0] += SUPERVISE_EVERY_S
    link.status["show"]["t0"] = None
    fleet._supervise(link)
    assert "run refused" in fleet.corrections[-1]


# ---- review F7: a unit burning its own demo is left alone ----

def burning_demo_link(name="radxa-16"):
    link = StubLink(name, "loaded")
    link.status["show"].update(demo=True, demo_name="PARIS", t0=None)
    link.status["show"]["burn"] = {"done": 3, "total": 10, "failed": [],
                                   "state": "burning"}
    return link


def test_a_unit_burning_its_demo_counts_as_playing_it():
    link = burning_demo_link()
    assert Fleet._playing_demo(link)
    link.status["show"]["burn"]["state"] = "burned"      # burned, not started
    assert not Fleet._playing_demo(link)
    link.status["show"]["demo"] = False                  # the fleet's own show
    link.status["show"]["burn"]["state"] = "burning"
    assert not Fleet._playing_demo(link)


def test_upload_leaves_a_unit_burning_its_demo_alone_and_says_why():
    fleet = Fleet({}, clock=lambda: 1100.0)
    busy, idle = burning_demo_link("radxa-16"), StubLink("radxa-17", "stopped")
    fleet.links = {"radxa-16": busy, "radxa-17": idle}
    shows = {n: {"id": "showB", "cues": [], "duration": 60} for n in fleet.links}
    results = fleet.upload(shows)
    assert results["radxa-16"] == {
        "ok": False, "error": "writing its demo pictures (3/10) - wait or STOP it"}
    assert results["radxa-17"]["ok"]
    assert busy.posted == [] and idle.posted[0][0] == "/show/load"


def test_supervise_leaves_a_unit_burning_its_demo_alone():
    fleet = Fleet({}, clock=lambda: 1100.0)
    link = burning_demo_link()
    link.status["show"]["id"] = "someOtherShow"     # would otherwise be reloaded
    fleet.links = {"radxa-16": link}
    fleet.shows = {"radxa-16": {"id": "showA", "cues": [], "duration": 600}}
    fleet.run = {"t0": 700.0, "state": "running", "held_at": None}
    fleet._supervise(link)
    assert link.posted == []
    assert fleet.corrections[-1].endswith(
        "radxa-16: writing its demo pictures, left alone")


def test_the_snapshot_counts_how_many_units_have_burned():
    fleet = Fleet({}, clock=lambda: 1100.0)
    burning = StubLink("radxa-07", "stopped")
    burning.status["show"]["burn"] = {"done": 1, "total": 2, "failed": [],
                                      "state": "burning"}
    burned = StubLink("radxa-08", "stopped")
    burned.status["show"]["burn"] = {"done": 2, "total": 2, "failed": [],
                                     "state": "burned"}
    fleet.links = {"radxa-07": burning, "radxa-08": burned}
    fleet.shows = {"radxa-07": {"id": "showA", "cues": [], "duration": 600},
                   "radxa-08": {"id": "showA", "cues": [], "duration": 600}}
    assert fleet.snapshot()["burn"] == {"burned": 1, "total": 2}


# ---- SEEK: moving the show's position by hand ----

def test_seek_while_running_moves_t0_for_every_unit_alike():
    fleet = Fleet({}, clock=lambda: 1000.0)
    fleet.links = {"radxa-01": StubLink("radxa-01", "running"),
                   "radxa-02": StubLink("radxa-02", "running")}
    fleet.shows = {"radxa-01": {"id": "showA", "cues": [], "duration": 600},
                   "radxa-02": {"id": "showA", "cues": [], "duration": 600}}
    fleet.run = {"t0": 700.0, "state": "running", "held_at": None}
    for link in fleet.links.values():
        link.posted.clear()
    mode, results = fleet.seek(180.0, lead_s=3.0)
    assert mode == "running"
    assert all(r["ok"] for r in results.values())
    assert fleet.run["t0"] == 1000.0 + 3.0 - 180.0
    for link in fleet.links.values():
        assert link.posted == [("/show/run",
                                {"t0": fleet.run["t0"] + link.offset,
                                 "show": "showA", "force": False})]


def test_seeking_backwards_lands_the_units_inside_the_show():
    fleet = Fleet({}, clock=lambda: 1000.0)
    link = StubLink("radxa-01", "running")
    fleet.links = {"radxa-01": link}
    fleet.shows = {"radxa-01": {"id": "showA", "cues": [], "duration": 600}}
    fleet.run = {"t0": 400.0, "state": "running", "held_at": None}   # 600 s in
    link.posted.clear()
    mode, results = fleet.seek(100.0, lead_s=3.0)
    assert mode == "running" and results["radxa-01"]["ok"]
    # lead_s from now the show reads exactly 100 s.
    assert (1000.0 + 3.0) - fleet.run["t0"] == pytest.approx(100.0)
    assert fleet.run["t0"] < 1000.0                  # landed in the past


def test_seeking_to_the_top_is_a_start_from_the_top():
    fleet = Fleet({}, clock=lambda: 1000.0)
    link = StubLink("radxa-01", "running")
    fleet.links = {"radxa-01": link}
    fleet.shows = {"radxa-01": {"id": "showA", "cues": [], "duration": 600}}
    fleet.run = {"t0": 400.0, "state": "running", "held_at": None}   # 600 s in
    mode, results = fleet.seek(0.0, lead_s=3.0)
    assert mode == "running" and results["radxa-01"]["ok"]
    assert fleet.run["t0"] == 1003.0
    assert fleet.run["t0"] > 1000.0                  # T0 ahead of now: a restart


def test_seek_while_holding_moves_the_position_and_stays_on_hold():
    fleet = Fleet({}, clock=lambda: 1000.0)
    link = StubLink("radxa-01", "running")
    fleet.links = {"radxa-01": link}
    fleet.shows = {"radxa-01": {"id": "showA", "cues": [], "duration": 600}}
    fleet.run = {"t0": 700.0, "state": "holding", "held_at": 950.0}  # 250 s in
    mode, results = fleet.seek(100.0)
    assert mode == "holding" and results == {}
    assert link.posted == []                         # nothing sent while holding
    assert fleet.run["t0"] == 850.0 and fleet.run["state"] == "holding"
    fleet._clock = lambda: 1200.0                     # time passes; then RESUME
    results = fleet.resume()
    assert results["radxa-01"]["ok"]
    assert fleet.run["t0"] == 1100.0                  # continues from where SEEK left it
    assert link.posted == [("/show/run", {"t0": 1100.0 + link.offset,
                                          "show": "showA", "force": False})]


def test_seek_without_a_run_only_says_where_start_begins():
    fleet = Fleet({}, clock=lambda: 1000.0)
    link = StubLink("radxa-01", "running")
    fleet.links = {"radxa-01": link}
    fleet.shows = {"radxa-01": {"id": "showA", "cues": [], "duration": 600}}
    fleet.run = None
    mode, results = fleet.seek(180.0)
    assert mode == "start_at" and results == {}
    assert link.posted == []
    assert fleet.start_at == 180.0
    # The caller resolves the position - here, the server always reads
    # fleet.start_at itself and passes it on explicitly (round 2 fix).
    started = fleet.start_show(lead_s=2.0, at=fleet.start_at)
    assert started["radxa-01"]["ok"]
    assert fleet.run["t0"] == 1000.0 + 2.0 - 180.0
    assert fleet.start_at == 0.0                      # forgotten once used


def test_stop_forgets_where_start_would_begin():
    fleet = Fleet({}, clock=lambda: 1000.0)
    fleet.links = {"radxa-01": StubLink("radxa-01", "running")}
    fleet.shows = {"radxa-01": {"id": "showA", "cues": [], "duration": 600}}
    fleet.seek(180.0)
    assert fleet.start_at == 180.0
    fleet.stop_show()
    assert fleet.start_at == 0.0


def test_seek_outside_the_show_is_refused():
    fleet = Fleet({}, clock=lambda: 1000.0)
    fleet.shows = {"radxa-01": {"id": "showA", "cues": [], "duration": 600}}
    with pytest.raises(ValueError, match=r"0:00 to 10:00"):
        fleet.seek(601.0)
    with pytest.raises(ValueError):
        fleet.seek(-1.0)
    mode, results = fleet.seek(600.0)                 # the top boundary is fine
    assert mode == "start_at" and fleet.start_at == 600.0


def test_a_seek_within_rounding_of_the_end_is_clamped_not_refused():
    # duration can carry more precision than the 0.1 s a seek is rounded
    # to (found in review): the exact end must not be refused just
    # because 719.96 rounds up to 720.0.
    fleet = Fleet({}, clock=lambda: 1000.0)
    fleet.shows = {"radxa-01": {"id": "showA", "cues": [], "duration": 719.96}}
    mode, _ = fleet.seek(719.96)
    assert mode == "start_at" and fleet.start_at == 719.96
    mode, _ = fleet.seek(-0.04)                       # the same, at the top
    assert mode == "start_at" and fleet.start_at == 0.0


def test_show_duration_is_the_longest_uploaded_show():
    fleet = Fleet({})
    assert fleet.show_duration() == 0.0
    fleet.shows = {"radxa-01": {"id": "a", "cues": [], "duration": 300},
                   "radxa-02": {"id": "b", "cues": [], "duration": 720}}
    assert fleet.show_duration() == 720.0
    assert isinstance(fleet.show_duration(), float)   # even from int inputs


def test_start_show_range_checks_whichever_position_it_is_given():
    # The blocker found in review: START never validated a remembered
    # start_at (only an explicit from_s) - the open door was skipped
    # validation, not a missing message.
    fleet = Fleet({}, clock=lambda: 1000.0)
    fleet.links = {"radxa-01": StubLink("radxa-01", "running")}
    fleet.shows = {"radxa-01": {"id": "showA", "cues": [], "duration": 600}}
    with pytest.raises(ValueError, match=r"0:00 to 10:00"):
        fleet.start_show(lead_s=1.0, at=601.0)
    assert fleet.run is None                          # refused before any mutation


def test_start_show_with_no_position_begins_exactly_lead_seconds_from_now():
    fleet = Fleet({}, clock=lambda: 1000.0)
    fleet.links = {"radxa-01": StubLink("radxa-01", "running")}
    fleet.shows = {"radxa-01": {"id": "showA", "cues": [], "duration": 600}}
    fleet.start_show(lead_s=3.0)
    assert fleet.run["t0"] == 1003.0


def test_uploading_a_new_show_forgets_a_remembered_start_position():
    fleet = Fleet({})
    fleet.start_at = 180.0
    fleet.upload({})
    assert fleet.start_at == 0.0


def test_adopting_a_run_forgets_a_remembered_start_position():
    fleet = Fleet({}, clock=lambda: 1100.0)
    fleet.start_at = 45.0
    fleet.links = {"radxa-01": StubLink("radxa-01", "running")}
    run = fleet.snapshot()["run"]
    assert run["adopted"]
    assert fleet.start_at == 0.0


def test_snapshot_start_at_is_zero_while_a_run_exists():
    fleet = Fleet({}, clock=lambda: 1000.0)
    fleet.start_at = 42.0                    # a leftover from before this run
    fleet.run = {"t0": 700.0, "state": "running", "held_at": None}
    assert fleet.snapshot()["start_at"] == 0.0
    fleet.run = None
    assert fleet.snapshot()["start_at"] == 42.0


def test_seeking_while_running_does_not_touch_the_remembered_start_position():
    fleet = Fleet({}, clock=lambda: 1000.0)
    fleet.links = {"radxa-01": StubLink("radxa-01", "running")}
    fleet.shows = {"radxa-01": {"id": "showA", "cues": [], "duration": 600}}
    fleet.run = {"t0": 700.0, "state": "running", "held_at": None}
    fleet.seek(200.0, lead_s=1.0)
    assert fleet.start_at == 0.0


# ---- races: an operator's next command lands mid-flight ----

def test_a_seek_racing_a_stop_does_not_crash_or_post(monkeypatch):
    # seek() releases _run_lock before _send_run() reads self.run again;
    # a concurrent STOP in that gap used to surface as a 400 'NoneType'
    # object is not subscriptable (found in review).
    fleet = Fleet({}, clock=lambda: 1000.0)
    link = StubLink("radxa-01", "running")
    fleet.links = {"radxa-01": link}
    fleet.shows = {"radxa-01": {"id": "showA", "cues": [], "duration": 600}}
    fleet.run = {"t0": 700.0, "state": "running", "held_at": None}
    real_targets = fleet._targets

    def targets_then_stop():
        names = real_targets()
        fleet.run = None                     # a concurrent STOP lands here
        return names
    monkeypatch.setattr(fleet, "_targets", targets_then_stop)
    mode, results = fleet.seek(50.0, lead_s=1.0)
    assert mode == "running" and results == {}
    assert link.posted == []


def test_a_seek_racing_a_hold_does_not_send_run_after_hold(monkeypatch):
    # Same gap, a concurrent HOLD instead: without the re-check, /show/run
    # could land after /show/hold and the unit would run while the
    # conductor believes it holds (found in review).
    fleet = Fleet({}, clock=lambda: 1000.0)
    link = StubLink("radxa-01", "running")
    fleet.links = {"radxa-01": link}
    fleet.shows = {"radxa-01": {"id": "showA", "cues": [], "duration": 600}}
    fleet.run = {"t0": 700.0, "state": "running", "held_at": None}
    real_targets = fleet._targets
    raced = []

    def targets_then_hold():
        if not raced:
            raced.append(True)
            fleet.hold()                     # a concurrent HOLD lands here
        return real_targets()
    monkeypatch.setattr(fleet, "_targets", targets_then_hold)
    mode, results = fleet.seek(50.0, lead_s=1.0)
    assert mode == "running" and results == {}
    assert link.posted == [("/show/hold", {})]         # only HOLD's own post landed


class RacingLink(StubLink):
    """A unit whose `post` performs a concurrent SEEK the first time it
    is called, simulating one landing between _supervise()'s copy of
    `run` and the point where it would post the (by then stale) T0."""

    def __init__(self, name, fleet):
        super().__init__(name, "running")
        self._fleet = fleet
        self._raced = False

    def post(self, path, body):
        if not self._raced and path == "/show/load":
            self._raced = True
            self._fleet.seek(200.0, lead_s=1.0)        # the concurrent operator
        return super().post(path, body)


def test_supervise_does_not_post_a_stale_t0_when_a_seek_lands_mid_check():
    fleet = Fleet({}, clock=lambda: 1000.0)
    link = RacingLink("radxa-01", fleet)
    fleet.links = {"radxa-01": link}
    # A different id than the StubLink's own ("showA") forces the
    # "show reloaded" /show/load post that RacingLink hooks.
    fleet.shows = {"radxa-01": {"id": "freshId", "cues": [], "duration": 600}}
    fleet.run = {"t0": 700.0, "state": "running", "held_at": None}
    fleet._supervise(link)
    run_posts = [body for path, body in link.posted if path == "/show/run"]
    assert len(run_posts) == 1                # supervise's own attempt was dropped
    assert run_posts[0]["t0"] == 806.0         # only the seek's own (new) T0 landed


def test_a_seek_is_not_held_up_by_an_offline_unit(fleet, units):
    fleet.shows = {name: {"id": "showA", "cues": [], "duration": 600}
                   for name in list(units) + ["radxa-09"]}
    fleet.run = {"t0": fleet._clock() - 60.0, "state": "running", "held_at": None}
    mode, results = fleet.seek(30.0, lead_s=0.5)
    assert mode == "running"
    assert set(results) == set(fleet.shows)
    assert not results["radxa-09"]["ok"]
    assert "clock not measured" in results["radxa-09"]["error"]


# ---- the standalone demo: a named copy of the show in a unit's own menu ----

def test_write_demo_posts_each_unit_its_own_show():
    fleet = Fleet({}, clock=lambda: 1000.0)
    a, b = StubLink("radxa-01", "stopped"), StubLink("radxa-02", "stopped")
    fleet.links = {"radxa-01": a, "radxa-02": b}
    shows = {"radxa-01": {"id": "showA", "cues": [], "duration": 60},
             "radxa-02": {"id": "showB", "cues": [], "duration": 90}}
    results = fleet.write_demo("DEMO PARIS", True, shows)
    assert all(r["ok"] for r in results.values())
    assert a.posted == [("/demo/save", {"name": "DEMO PARIS", "loop": True,
                                        "show": shows["radxa-01"]})]
    assert b.posted == [("/demo/save", {"name": "DEMO PARIS", "loop": True,
                                        "show": shows["radxa-02"]})]
    # An eMMC write is not a network sample, and must not be timed as one.
    assert a.learned is False and b.learned is False
    # Independent of the run this conductor drives: neither is touched.
    assert fleet.shows == {} and fleet.run is None


def test_write_demo_does_not_touch_a_live_run():
    fleet = Fleet({}, clock=lambda: 1000.0)
    a = StubLink("radxa-01", "stopped")
    fleet.links = {"radxa-01": a}
    fleet.shows = {"radxa-01": {"id": "liveShow", "cues": [], "duration": 300}}
    fleet.run = {"t0": 500.0, "state": "running", "held_at": None}
    fleet.start_at = 42.0
    demo_shows = {"radxa-01": {"id": "demoShow", "cues": [], "duration": 60}}
    fleet.write_demo("DEMO", False, demo_shows)
    assert fleet.shows == {"radxa-01": {"id": "liveShow", "cues": [], "duration": 300}}
    assert fleet.run == {"t0": 500.0, "state": "running", "held_at": None}
    assert fleet.start_at == 42.0


def test_write_demo_surfaces_a_409_refusal_per_unit():
    class Refusing(StubLink):
        def post(self, path, body, learn=True, timeout=None):
            raise RuntimeError("HTTP 409: a show is running - stop it first")
    fleet = Fleet({}, clock=lambda: 1000.0)
    fleet.links = {"radxa-01": Refusing("radxa-01", "stopped")}
    shows = {"radxa-01": {"id": "showA", "cues": [], "duration": 60}}
    results = fleet.write_demo("DEMO", False, shows)
    assert not results["radxa-01"]["ok"]
    assert "a show is running - stop it first" in results["radxa-01"]["error"]


def test_write_demo_reports_an_offline_unit_as_failed():
    fleet = Fleet({}, clock=lambda: 1000.0)
    ok, gone = StubLink("radxa-01", "stopped"), FailingLink("radxa-09", "stopped")
    fleet.links = {"radxa-01": ok, "radxa-09": gone}
    shows = {"radxa-01": {"id": "showA", "cues": [], "duration": 60},
             "radxa-09": {"id": "showA", "cues": [], "duration": 60}}
    results = fleet.write_demo("DEMO", False, shows)
    assert results["radxa-01"]["ok"]
    assert not results["radxa-09"]["ok"]
    assert "timed out" in results["radxa-09"]["error"]


def test_write_demo_with_no_shows_posts_nothing():
    # The server's own "whole show or not at all" rule (compile_show()
    # returns {} while the timeline has a problem): write_demo is simply
    # never called with anything to post, and does nothing on its own.
    fleet = Fleet({})
    a = StubLink("radxa-01", "stopped")
    fleet.links = {"radxa-01": a}
    assert fleet.write_demo("DEMO", False, {}) == {}
    assert a.posted == []


def test_list_demos_gathers_online_units_and_marks_the_rest_failed():
    fleet = Fleet({}, clock=lambda: 1000.0)
    online = StubLink("radxa-01", "stopped")
    online.demos = [{"slug": "demo-paris", "name": "DEMO PARIS", "cues": 5,
                     "duration": 60, "loop": False, "saved_at": 1}]
    offline = StubLink("radxa-02", "stopped")
    offline.online = False
    fleet.links = {"radxa-01": online, "radxa-02": offline}
    results = fleet.list_demos()
    assert results["radxa-01"] == {"ok": True, "demos": online.demos}
    assert not results["radxa-02"]["ok"]
    assert "offline" in results["radxa-02"]["error"]


def test_delete_demo_posts_to_every_online_unit():
    fleet = Fleet({}, clock=lambda: 1000.0)
    a, b = StubLink("radxa-01", "stopped"), StubLink("radxa-02", "stopped")
    fleet.links = {"radxa-01": a, "radxa-02": b}
    results = fleet.delete_demo("demo-paris")
    assert a.posted == [("/demo/delete", {"slug": "demo-paris"})]
    assert b.posted == [("/demo/delete", {"slug": "demo-paris"})]
    assert all(r["ok"] for r in results.values())
    assert a.learned is False        # a delete's timing is not a network sample


def test_delete_demo_skips_an_offline_unit_instead_of_posting():
    fleet = Fleet({}, clock=lambda: 1000.0)
    online, gone = StubLink("radxa-01", "stopped"), StubLink("radxa-09", "stopped")
    gone.online = False
    fleet.links = {"radxa-01": online, "radxa-09": gone}
    results = fleet.delete_demo("demo-paris")
    assert online.posted == [("/demo/delete", {"slug": "demo-paris"})]
    assert gone.posted == []                         # never attempted
    assert results["radxa-01"]["ok"]
    assert not results["radxa-09"]["ok"] and results["radxa-09"]["error"] == "offline"


def test_delete_demo_reports_an_unknown_slug_as_failed():
    # /demo/delete answers 409/404 with {"error": "no such demo: ..."} for
    # a slug that is not there; UnitLink.post() already turns any non-200
    # into a RuntimeError (see _exchange), so _each() reports it as a
    # per-unit failure with no code of its own needed here.
    class UnknownSlugLink(StubLink):
        def post(self, path, body, learn=True, timeout=None):
            raise RuntimeError(f"no such demo: {body['slug']}")
    fleet = Fleet({}, clock=lambda: 1000.0)
    fleet.links = {"radxa-01": UnknownSlugLink("radxa-01", "stopped")}
    results = fleet.delete_demo("ghost")
    assert not results["radxa-01"]["ok"]
    assert "no such demo: ghost" in results["radxa-01"]["error"]


# ---- a unit playing its own demo is not the fleet's show ----

def test_a_unit_playing_a_demo_is_not_adopted():
    fleet = Fleet({}, clock=lambda: 1100.0)
    link = StubLink("radxa-01", "running")
    link.status["show"]["demo"] = True
    fleet.links = {"radxa-01": link}
    assert fleet.snapshot()["run"] is None      # not mistaken for the fleet's own run


def test_a_unit_that_finished_its_demo_can_still_be_adopted():
    # state is "running" but demo is false: a real fleet-driven run, and
    # adoption must not have been blanket-disabled by the demo check.
    fleet = Fleet({}, clock=lambda: 1100.0)
    link = StubLink("radxa-01", "running")
    link.status["show"]["demo"] = False
    fleet.links = {"radxa-01": link}
    assert fleet.snapshot()["run"] is not None


def test_supervise_reloads_a_reborn_unit_but_runs_it_only_once_burned():
    fleet = Fleet({}, clock=lambda: 1100.0)
    link = StubLink("radxa-01", "stopped")
    link.status["show"]["id"] = "someOtherShow"
    fleet.links = {"radxa-01": link}
    fleet.shows = {"radxa-01": {"id": "showA", "cues": [], "duration": 600}}
    fleet.run = {"t0": 1000.0, "state": "running", "held_at": None}
    fleet._supervise(link)
    assert [path for path, _ in link.posted] == ["/show/load"]   # no run yet
    assert any("radxa-01: show reloaded" in line for line in fleet.corrections)
    # The unit now holds the show and is writing its pictures: left alone.
    link.posted.clear()
    fleet._corrected.clear()
    link.status["show"]["id"] = "showA"
    link.status["show"]["burn"] = {"done": 3, "total": 9, "failed": [],
                                   "state": "burning"}
    said = len(fleet.corrections)
    fleet._supervise(link)
    assert link.posted == [] and len(fleet.corrections) == said
    # Burned: the run goes out on the shared T0 (+ this link's offset).
    link.status["show"]["burn"]["state"] = "burned"
    link.status["show"]["t0"] = None                # never ran since its reboot
    fleet._supervise(link)
    assert [path for path, _ in link.posted] == ["/show/run"]
    assert any("radxa-01: started late" in line for line in fleet.corrections)


def test_supervise_leaves_a_unit_playing_a_demo_alone_once_per_episode():
    fleet = Fleet({}, clock=lambda: 1100.0)
    link = StubLink("radxa-02", "running")
    link.status["show"]["demo"] = True
    link.status["show"]["id"] = "someOtherShow"     # would normally read "show reloaded"
    fleet.links = {"radxa-02": link}
    fleet.shows = {"radxa-02": {"id": "showA", "cues": [], "duration": 600}}
    fleet.run = {"t0": 700.0, "state": "running", "held_at": None}
    fleet._supervise(link)
    assert link.posted == []                        # neither /show/load nor /show/run
    assert any("playing a demo, left alone" in line for line in fleet.corrections)
    said = len(fleet.corrections)
    fleet._supervise(link)                          # the same episode, again and again
    fleet._supervise(link)
    assert link.posted == [] and len(fleet.corrections) == said     # not repeated


def test_supervise_announces_a_second_demo_episode_again():
    # A stepped clock: SUPERVISE_EVERY_S throttles one correction per unit
    # per real interval, and each call below must land in a fresh one, or
    # the second and third calls would be swallowed by that limiter
    # rather than by the thing under test.
    now = [1100.0]
    fleet = Fleet({}, clock=lambda: now[0])
    link = StubLink("radxa-02", "running")
    link.status["show"]["demo"] = True
    fleet.links = {"radxa-02": link}
    fleet.shows = {"radxa-02": {"id": "showA", "cues": [], "duration": 600}}
    fleet.run = {"t0": 700.0, "state": "running", "held_at": None}
    fleet._supervise(link)
    assert len(fleet.corrections) == 1
    now[0] += SUPERVISE_EVERY_S + 1
    link.status["show"] = dict(link.status["show"], demo=False, state="stopped",
                               id="showA")           # KEY2: back to the menu
    fleet._supervise(link)                           # supervised normally now
    assert len(fleet.corrections) == 2               # a normal correction landed
    now[0] += SUPERVISE_EVERY_S + 1
    link.status["show"] = dict(link.status["show"], demo=True, state="running")
    fleet._supervise(link)                           # a new episode
    assert len(fleet.corrections) == 3
    assert fleet.corrections[-1].endswith("radxa-02: playing a demo, left alone")


def test_supervise_corrects_normally_once_a_demo_has_ended():
    # state "ended" (played through) is not "running"/"holding" any more -
    # the unit is supervised exactly as if it had never played a demo.
    fleet = Fleet({}, clock=lambda: 1100.0)
    link = StubLink("radxa-02", "running")
    link.status["show"] = dict(link.status["show"], demo=True, state="ended",
                               id="someOtherShow")
    fleet.links = {"radxa-02": link}
    fleet.shows = {"radxa-02": {"id": "showA", "cues": [], "duration": 600}}
    fleet.run = {"t0": 700.0, "state": "running", "held_at": None}
    fleet._supervise(link)
    assert ("/show/load", fleet.shows["radxa-02"]) in link.posted


def test_send_run_skips_a_unit_playing_a_demo():
    fleet = Fleet({}, clock=lambda: 1000.0)
    link = StubLink("radxa-01", "running")
    link.status["show"]["demo"] = True
    fleet.links = {"radxa-01": link}
    fleet.shows = {"radxa-01": {"id": "showA", "cues": [], "duration": 600}}
    fleet.run = {"t0": 700.0, "state": "running", "held_at": None}
    results = fleet._send_run(["radxa-01"])
    assert not results["radxa-01"]["ok"]
    assert results["radxa-01"]["error"] == "playing a demo - press STOP first"
    assert link.posted == []


def test_start_show_reports_a_demo_unit_as_not_started():
    fleet = Fleet({}, clock=lambda: 1000.0)
    link = StubLink("radxa-01", "running")
    link.status["show"]["demo"] = True
    fleet.links = {"radxa-01": link}
    fleet.shows = {"radxa-01": {"id": "showA", "cues": [], "duration": 600}}
    results = fleet.start_show(lead_s=1.0)
    assert not results["radxa-01"]["ok"]
    assert "playing a demo" in results["radxa-01"]["error"]


def test_a_demo_started_after_the_stop_is_not_a_missed_stop():
    """Rehearsal: STOP on the PC, then KEY1 on a demo row at the unit. The
    catch-up for units that missed the STOP must not kill that demo (it
    did, once per unit and STOP - the second try survived)."""
    fleet = Fleet({}, clock=lambda: 1100.0)
    link = StubLink("radxa-02", "running")
    fleet.links = {"radxa-02": link}
    fleet.shows = {"radxa-02": {"id": "showA", "cues": [], "duration": 600}}
    fleet.start_show(lead_s=1.0)
    fleet.stop_show()
    link.posted.clear()
    link.status["show"].update({"demo": True, "demo_name": "DEMO PARIS"})
    for _ in range(3):
        fleet._corrected.clear()
        fleet._supervise(link)
    assert link.posted == []                        # left alone
    link.status["show"].update({"demo": False})     # now a real missed STOP
    fleet._corrected.clear()
    fleet._supervise(link)
    assert link.posted == [("/show/stop", {})]


def test_upload_refuses_a_unit_playing_a_demo_like_start_does():
    fleet = Fleet({}, clock=lambda: 1100.0)
    busy = StubLink("radxa-02", "running")
    busy.status["show"].update({"demo": True, "demo_name": "DEMO PARIS"})
    idle = StubLink("radxa-03", "loaded")
    fleet.links = {"radxa-02": busy, "radxa-03": idle}
    results = fleet.upload({"radxa-02": {"id": "s2", "cues": [], "duration": 60},
                            "radxa-03": {"id": "s3", "cues": [], "duration": 60}})
    assert results["radxa-02"]["ok"] is False
    assert "playing a demo" in results["radxa-02"]["error"]
    assert results["radxa-03"]["ok"] is True
    assert busy.posted == [] and idle.posted[0][0] == "/show/load"



# ---- the demo cache behind the tiles (fleet.demos_of / _poll_demos) ----

def _demo_entry(slug, name, show_id, **extra):
    return dict({"slug": slug, "name": name, "cues": 4, "duration": 90.0,
                 "loop": False, "saved_at": 1.0, "show_id": show_id}, **extra)


def test_the_poll_asks_each_unit_for_its_demos_at_most_every_ten_seconds():
    # Every tile shows what its unit holds, and ten tiles repainting once
    # a second must not become ten requests a second: the poll loop
    # refreshes the list per unit every DEMO_LIST_EVERY_S, the snapshot
    # only reads the cache.
    now = [1000.0]
    fleet = Fleet({}, clock=lambda: now[0])
    link = StubLink("radxa-01", "stopped")
    link.demos = [_demo_entry("demo-paris", "DEMO PARIS", "showA")]
    fleet.links = {"radxa-01": link}

    fleet._poll_demos(link)
    assert fleet.demos_of("radxa-01")[0]["name"] == "DEMO PARIS"
    assert link.learned is False          # an eMMC read is not a clock sample
    link.demos = [_demo_entry("demo-paris", "RENAMED", "showA")]
    now[0] += DEMO_LIST_EVERY_S - 0.1
    fleet._poll_demos(link)
    assert fleet.demos_of("radxa-01")[0]["name"] == "DEMO PARIS"   # not asked again
    now[0] += 0.2
    fleet._poll_demos(link)
    assert fleet.demos_of("radxa-01")[0]["name"] == "RENAMED"


def test_a_unit_that_cannot_list_its_demos_is_not_known_rather_than_empty():
    # An older agent (404 on /demo/list) or a unit that dropped the
    # connection must never read as "no demo stored" - the tile says
    # nothing rather than something false. And it is not asked again
    # until the ten seconds are up: a 404 that comes back every time
    # would otherwise be asked for on every pass, for the whole night
    # (found in review).
    now = [1000.0]
    fleet = Fleet({}, clock=lambda: now[0])
    link = FailingLink("radxa-01", "stopped")
    fleet.links = {"radxa-01": link}
    fleet._poll_demos(link)
    assert fleet.demos_of("radxa-01") is None
    assert fleet.snapshot()["units"][0]["demos"] is None
    link.demos = [_demo_entry("demo-a", "DEMO A", "showA")]
    link.get = StubLink.get.__get__(link, StubLink)      # the unit comes back
    now[0] += DEMO_LIST_EVERY_S - 0.1
    fleet._poll_demos(link)
    assert fleet.demos_of("radxa-01") is None            # still not asked
    now[0] += 0.2
    fleet._poll_demos(link)
    assert [d["name"] for d in fleet.demos_of("radxa-01")] == ["DEMO A"]


def test_listing_the_demos_never_waits_on_a_units_own_poll():
    # /status is that unit's clock measurement and the supervision's
    # heartbeat; an eMMC listing (or a unit that has stopped answering)
    # must not stretch its 2 s cadence. The listings run on one thread of
    # their own, and the poll loop does not touch them.
    import inspect
    source = inspect.getsource(Fleet._poll_loop)
    assert "_poll_demos" not in source and "demo" not in source
    assert "_poll_demos" in inspect.getsource(Fleet._demo_loop)
    # Its own short timeout, and only for a unit that is answering at all.
    fleet = Fleet({}, clock=lambda: 1000.0)
    asked = []

    class Timed(StubLink):
        def get(self, path, learn=True, timeout=None):
            asked.append((path, timeout))
            return StubLink.get(self, path, learn, timeout)

    offline = Timed("radxa-02", "stopped")
    offline.online = False
    fleet.links = {"radxa-01": Timed("radxa-01", "stopped"),
                   "radxa-02": offline}
    for link in fleet.links.values():
        if link.online:
            fleet._poll_demos(link)
    assert asked == [("/demo/list", TIMEOUT_S)]


def test_the_snapshot_says_which_stored_demo_is_the_show_that_was_uploaded():
    # "current" is the whole point of the chips: True only when the demo
    # was written from the show this conductor last uploaded to THAT unit,
    # False when it is older, None when there is nothing to compare
    # against (nothing uploaded, or a demo with no show_id of its own).
    fleet = Fleet({}, clock=lambda: 1000.0)
    link = StubLink("radxa-01", "stopped")
    link.demos = [_demo_entry("demo-a", "DEMO A", "showA"),
                  _demo_entry("demo-b", "DEMO B", "older-hash"),
                  _demo_entry("demo-c", "DEMO C", None)]
    fleet.links = {"radxa-01": link}
    fleet._poll_demos(link)
    assert [d["current"] for d in fleet.demos_of("radxa-01")] == [None] * 3
    fleet.shows = {"radxa-01": {"id": "showA", "cues": [], "duration": 60}}
    demos = fleet.snapshot()["units"][0]["demos"]
    assert [d["current"] for d in demos] == [True, False, None]
    assert demos[0] == {"slug": "demo-a", "name": "DEMO A", "cues": 4,
                        "duration": 90.0, "loop": False, "show_id": "showA",
                        "current": True}


def test_a_write_and_a_delete_update_the_cache_from_their_own_answer():
    # /demo/save and /demo/delete both answer with the unit's whole menu:
    # the tiles are right at once, without waiting out the ten seconds or
    # spending a request of their own.
    class MenuLink(StubLink):
        def post(self, path, body, learn=True, timeout=None):
            StubLink.post(self, path, body, learn, timeout)
            if path == "/demo/save":
                self.demos = [_demo_entry("demo-paris", body["name"], "showA")]
            elif path == "/demo/delete":
                self.demos = []
            return {"slug": "demo-paris", "demos": self.demos}

    fleet = Fleet({}, clock=lambda: 1000.0)
    link = MenuLink("radxa-01", "stopped")
    fleet.links = {"radxa-01": link}
    fleet.write_demo("DEMO PARIS", False,
                     {"radxa-01": {"id": "showA", "cues": [], "duration": 60}})
    assert [d["name"] for d in fleet.demos_of("radxa-01")] == ["DEMO PARIS"]
    fleet.delete_demo("demo-paris")
    assert fleet.demos_of("radxa-01") == []


def test_a_write_whose_answer_carries_no_menu_leaves_the_cache_to_the_poll():
    # An agent that answers /demo/save without the list (or a unit that
    # failed): "not known", and asked again on the next poll rather than
    # ten seconds later - never a stale menu shown as the truth.
    now = [1000.0]
    fleet = Fleet({}, clock=lambda: now[0])
    link = StubLink("radxa-01", "stopped")          # post() answers {}
    link.demos = [_demo_entry("demo-old", "DEMO OLD", "showA")]
    fleet.links = {"radxa-01": link}
    fleet._poll_demos(link)
    fleet.write_demo("DEMO NEW", False,
                     {"radxa-01": {"id": "showA", "cues": [], "duration": 60}})
    assert fleet.demos_of("radxa-01") is None
    link.demos = [_demo_entry("demo-new", "DEMO NEW", "showA")]
    fleet._poll_demos(link)
    assert [d["name"] for d in fleet.demos_of("radxa-01")] == ["DEMO NEW"]


def test_listing_the_demos_on_demand_also_fills_the_cache_the_tiles_read():
    # Refresh in the demo table and the tiles must not disagree about
    # what a unit holds a second later.
    fleet = Fleet({}, clock=lambda: 1000.0)
    link = StubLink("radxa-01", "stopped")
    link.demos = [_demo_entry("demo-a", "DEMO A", "showA")]
    fleet.links = {"radxa-01": link}
    assert fleet.demos_of("radxa-01") is None
    fleet.list_demos()
    assert [d["name"] for d in fleet.demos_of("radxa-01")] == ["DEMO A"]
