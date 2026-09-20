"""The show end to end: CSVs + timeline -> show files -> units that run it.

conductor/showfile.py is checked on its own first; then a conductor
workspace, the fleet and two real agents with show players (FakeBus,
localhost) run a compressed show - refresh 1 s, a cue a few seconds in -
including a unit that restarts in the middle and is put right by the
PC's supervision without anyone asking.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conductor.fleet import Fleet
from conductor.server import Workspace
from conductor.showfile import blank, lay_over
from tests.test_look import GRID, MAP, SKIRT_GRID, SKIRT_MAP
from tests.test_ui_remote import SAVE, SHOW, make_session, wait_until
from ui.agent import Agent
from ui.showplay import ShowPlayer

# The fake units stamp their sends with time.monotonic(), which ticks every
# 15.6 ms on Windows: a stamp can read that much early. (The real units are
# Linux, nanosecond clocks; this slack is for the test bench only.)
TICK = 0.02

P1 = "Look20-Top_color_pattern01_grid.csv"
P2 = "Look20-Top_color_pattern02_grid.csv"
S1 = "Look20-Skirt_color_pattern01_grid.csv"
ACCENT = "side,row,shift,1,2,3\nfront,1,0.5,0x08,-,0\nfront,0,0,-,0,0\nback,0,0,0,-,0\n"


def workspace(tmp_path, refresh=1.0):
    ws = Workspace(tmp_path / "ws")
    ws.save("Look20-Top_map.csv", MAP)
    ws.save(P1, GRID)
    ws.save(P2, ACCENT)                      # partial: one scale turns pink
    ws.save("Look20-Skirt_map.csv", SKIRT_MAP)
    ws.save(S1, SKIRT_GRID)
    ws.assign("Look20-Top", "radxa-02")
    ws.assign("Look20-Skirt", "radxa-02")
    return ws


def cue(id_, item, at, design, **more):
    return dict({"id": id_, "item": item, "at": at, "design": design}, **more)


# ---- the show file ----

def test_a_show_file_holds_every_cue_resolved_for_the_unit(tmp_path):
    ws = workspace(tmp_path)
    ws.set_timeline(60, [cue("a", "Look20-Top", 0, P1),
                         cue("b", "Look20-Skirt", 0, S1),
                         cue("c", "Look20-Top", 20, P2, partial=True)],
                    refresh=1.0)
    shows, problems = ws.compile_show()
    assert problems == [] and list(shows) == ["radxa-02"]
    show = shows["radxa-02"]
    assert show["boards"] == [1, 2, 3, 4, 5] and show["refresh_s"] == 1.0
    assert [c["id"] for c in show["cues"]] == ["q00", "q01"]
    preset, accent = show["cues"]
    # Top and skirt share the unit and the instant: one cue, one label.
    assert preset["sent"] == -1.0
    assert preset["label"] == "Look20-Skirt P01 + Look20-Top P01"
    assert bytes.fromhex(preset["boards"]["1"])[7] == 0x0A      # skirt 001-07
    assert bytes.fromhex(preset["boards"]["3"])[1] == 0x03      # top 017-01
    # The partial cue: sent 1 s before 0:20, touches board 3 socket 1 only;
    # every other board is written "refresh nothing".
    assert accent["sent"] == 19.0 and accent["label"] == "Look20-Top P02*"
    change = bytes.fromhex(accent["boards"]["3"])
    assert change[1] == 0x08 and change[60] == 0xFF
    assert all(bytes.fromhex(accent["boards"][a]) == bytes(blank())
               for a in ("1", "2", "4", "5"))
    # ...and the state is that change laid over the preset.
    state = bytes.fromhex(accent["state"]["3"])
    assert state[1] == 0x08 and state[60] == 0x00               # white kept
    assert accent["state"]["1"] == preset["state"]["1"]
    assert len(show["id"]) == 10
    assert ws.compile_show()[0]["radxa-02"]["id"] == show["id"]  # stable


def test_a_show_with_a_problem_is_not_built(tmp_path):
    ws = workspace(tmp_path)
    ws.set_timeline(60, [cue("a", "Look20-Top", 0, P1),
                         cue("b", "Look20-Top", 20, P2)], refresh=1.0)
    shows, problems = ws.compile_show()
    assert shows == {} and any("一部更新" in p for p in problems)
    ws.assign("Look20-Top", None)
    ws.set_timeline(60, [cue("a", "Look20-Top", 0, P1)], refresh=1.0)
    assert any("未割当" in p for p in ws.compile_show()[1])
    ws.set_timeline(60, [], refresh=1.0)
    assert any("キューがありません" in p for p in ws.compile_show()[1])


def test_lay_over_keeps_what_the_change_leaves_alone():
    state, change = blank(), blank()
    state[5], change[6] = 0x02, 0x03
    lay_over(state, bytes(change))
    assert (state[5], state[6], state[7]) == (0x02, 0x03, 0xFF)


# ---- units running it ----

class ShowUnit:
    def __init__(self, name, store, port=0):
        self.session, self.runner, self.bus = make_session()
        self.player = ShowPlayer(self.session, store=store, save_s=0.02,
                                 margin_s=0.3, grace_s=0.2, tick_s=0.02,
                                 setup_s=0.6, setup_board_s=0.0)
        self.session.on_release = self.player.stop
        self.agent = Agent(self.session, port=port, host="127.0.0.1",
                           name=name, player=self.player)
        self.agent.start()
        self.address = f"127.0.0.1:{self.agent.port}"
        self.bus.stamps = []
        send = self.bus.send

        def stamped(frame):
            if frame.cmd == SHOW:
                self.bus.stamps.append(time.monotonic())
            return send(frame)
        self.bus.send = stamped

    def close(self):
        self.agent.stop()
        self.player.close()
        self.runner.stop()

    @property
    def shows(self):
        return self.bus.stamps


@pytest.fixture
def stage(tmp_path):
    ws = workspace(tmp_path)
    ws.save("Look22_map.csv", MAP)
    ws.save("Look22_color_pattern01_grid.csv", GRID)
    ws.save("Look22_color_pattern02_grid.csv", ACCENT)
    ws.assign("Look22", "radxa-01")
    units = {"radxa-01": ShowUnit("radxa-01", tmp_path / "u1"),
             "radxa-02": ShowUnit("radxa-02", tmp_path / "u2")}
    fleet = Fleet({n: u.address for n, u in units.items()}, poll_s=0.05)
    fleet.start()
    assert wait_until(lambda: all(len(l._samples) >= 3
                                  for l in fleet.links.values()))
    yield ws, fleet, units, tmp_path
    fleet.stop()
    for unit in units.values():
        unit.close()


def timeline(ws, second_at=4):
    ws.set_timeline(30, [
        cue("a", "Look22", 0, "Look22_color_pattern01_grid.csv"),
        cue("b", "Look20-Top", 0, P1), cue("c", "Look20-Skirt", 0, S1),
        cue("d", "Look22", second_at, "Look22_color_pattern02_grid.csv",
            partial=True),
        cue("e", "Look20-Top", second_at, P2, partial=True)], refresh=1.0)
    shows, problems = ws.compile_show()
    assert problems == []
    return shows


def test_upload_preset_start_and_both_units_fire_together(stage):
    ws, fleet, units, _ = stage
    shows = timeline(ws, second_at=6)
    results = fleet.upload(shows)
    assert all(r["ok"] and r["show"] == shows[n]["id"]
               for n, r in results.items())
    fleet.simple(list(shows), "/show/preset")
    assert wait_until(lambda: all(u.player.applied == "q00"
                                  for u in units.values()))
    assert all(len(u.shows) == 1 for u in units.values())

    fleet.start_show(lead_s=0.5)
    t0 = fleet.run["t0"]
    assert wait_until(lambda: all(u.player.applied == "q01"
                                  for u in units.values()), timeout=8)
    fired = [u.shows[1] for u in units.values()]
    assert max(fired) - min(fired) < 0.05                   # together
    # Sent 1 s (the refresh) before 0:06, on the PC's clock.
    offsets = [fleet.links[n].offset for n in units]
    for stamp, offset in zip(fired, offsets):
        assert -TICK <= (stamp - offset) - (t0 + 5.0) < 0.06
    assert all(len(u.shows) == 2 for u in units.values())   # preset not redone
    snap = fleet.snapshot()
    assert snap["run"]["state"] == "running" and snap["run"]["now"] > 5
    assert snap["corrections"] == []


def test_hold_resume_and_next_move_every_unit_alike(stage):
    ws, fleet, units, _ = stage
    fleet.upload(timeline(ws, second_at=20))
    fleet.simple(list(units), "/show/preset")
    assert wait_until(lambda: all(u.player.applied == "q00"
                                  for u in units.values()))
    fleet.start_show(lead_s=0.3)
    time.sleep(0.6)
    fleet.hold()
    assert all(u.player.state == "holding" for u in units.values())
    held_now = fleet.snapshot()["run"]["now"]
    time.sleep(0.5)
    assert fleet.snapshot()["run"]["now"] == held_now        # the clock stands
    fleet.resume()
    assert all(u.player.state == "running" for u in units.values())
    assert abs(fleet.snapshot()["run"]["now"] - held_now) < 0.2

    results = fleet.next_cue(lead_s=1.2)                    # 0:20's cue, now
    assert all(r["ok"] for r in results.values())
    assert wait_until(lambda: all(u.player.applied == "q01"
                                  for u in units.values()), timeout=6)
    fired = [u.shows[1] for u in units.values()]
    assert max(fired) - min(fired) < 0.05
    fleet.stop_show()
    assert all(u.player.state == "stopped" for u in units.values())
    assert fleet.snapshot()["run"] is None


def test_a_unit_that_restarts_is_put_right_without_being_asked(stage):
    ws, fleet, units, tmp_path = stage
    fleet.upload(timeline(ws, second_at=6))
    fleet.simple(list(units), "/show/preset")
    assert wait_until(lambda: all(u.player.applied == "q00"
                                  for u in units.values()))
    fleet.start_show(lead_s=0.3)
    t0 = fleet.run["t0"]
    time.sleep(0.8)

    # radxa-01 loses power: a new process, an empty disk, the same address.
    old = units["radxa-01"]
    port = old.agent.port
    old.close()
    # A machine that dies takes its TCP connections with it. Stopping the
    # server in-process does not - the kept-alive handler thread would go
    # on answering for the dead unit - so the test cuts the line itself.
    link = fleet.links["radxa-01"]
    link._poll_conn.sock.close()
    reborn = ShowUnit("radxa-01", tmp_path / "u1-new", port=port)
    units["radxa-01"] = reborn
    assert reborn.player.show is None

    assert wait_until(lambda: reborn.player.state == "running", timeout=6)
    assert reborn.player.show["id"] == fleet.shows["radxa-01"]["id"]
    assert any("radxa-01: show reloaded" in line
               for line in fleet.snapshot()["corrections"])
    # It does not know what its boards show: the preset goes out whole,
    # then the 0:06 cue fires with the unit that never stopped.
    assert wait_until(lambda: all(u.player.applied == "q01"
                                  for u in units.values()), timeout=10)
    fired = [units[n].shows[-1] for n in ("radxa-01", "radxa-02")]
    assert max(fired) - min(fired) < 0.05
    offset = fleet.links["radxa-01"].offset
    assert -TICK <= (fired[0] - offset) - (t0 + 5.0) < 0.06


def test_a_running_show_refuses_loose_cues_but_not_the_panic_white(stage):
    ws, fleet, units, _ = stage
    fleet.upload(timeline(ws, second_at=20))
    fleet.start_show(lead_s=0.2)
    assert wait_until(lambda: all(u.player.state == "running"
                                  for u in units.values()))
    blankhex = bytes(blank()).hex()
    refused = fleet.prepare({"radxa-01": {"cue": "x", "label": "", "dev_type": 3,
                                          "boards": {"1": blankhex}}})
    assert "show is running" in refused["radxa-01"]["error"]
    # PANIC white: stop the run, then standby - the order the API uses.
    fleet.stop_show()
    results = fleet.simple(list(units), "/standby")
    assert all(r["ok"] and r["phase"] == "standby" for r in results.values())
