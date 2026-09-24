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

from conductor.fleet import Fleet, UnitLink, default_units
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

    def post(self, path, body):
        self.posted.append((path, body))
        return {}

    def snapshot(self):
        return {"name": self.name, "online": True, "show": self.status["show"]}


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
    assert link.posted == [("/show/run", {"t0": 1005.0, "show": "showA"})]
    assert "confirmed" in fleet.corrections[-1]
    link.status["show"]["synced"] = True
    link.posted.clear(); fleet._corrected.clear()
    fleet._supervise(link)
    assert link.posted == []


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
                                 "show": "showA"})]


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
                                          "show": "showA"})]


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
