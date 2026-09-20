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
    assert units["radxa-01"] == "192.168.50.101:8787"
    assert units["radxa-10"] == "192.168.50.110:8787"


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
    link._learn({"mono": 12.0}, 999.99, 1000.01)     # monotonic restarted
    assert len(link._samples) == 1 and abs(link.offset - -988.0) < 0.01


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
