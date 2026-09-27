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
    assert show["slot_capacity"] == 20    # a board has 20 slots total
    assert [c["id"] for c in show["cues"]] == ["q00", "q01"]
    preset, accent = show["cues"]
    assert (preset["slot"], accent["slot"]) == (1, 2)   # its own slot each,
                                                        # in send order
    # Top and skirt share the unit and the instant: one cue, one label.
    assert preset["sent"] == -1.0
    assert preset["label"] == "Look20-Skirt P01 + Look20-Top P01"
    assert bytes.fromhex(preset["boards"]["1"])[7] == 0x0A      # skirt 001-07
    assert bytes.fromhex(preset["boards"]["3"])[1] == 0x03      # top 017-01
    # The partial cue: "at" IS Start, sent at 0:20; touches board 3 socket
    # 1 only, every other board is written "refresh nothing".
    assert accent["sent"] == 20.0 and accent["label"] == "Look20-Top P02*"
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


def test_every_unit_cue_gets_its_own_slot_in_send_order(tmp_path):
    ws = workspace(tmp_path)
    ws.set_timeline(60, [cue("a", "Look20-Top", 0, P1),
                         cue("b", "Look20-Top", 6, P2, partial=True),
                         cue("c", "Look20-Top", 9, P1),
                         cue("d", "Look20-Top", 12, P2, partial=True)],
                    refresh=1.0)
    shows, problems = ws.compile_show()
    assert problems == []
    show = shows["radxa-02"]
    # Four moments (the preset, q00, included) -> four slots, 1..4, one
    # per cue in send order - nothing is rewritten once the show starts.
    assert [c["slot"] for c in show["cues"]] == [1, 2, 3, 4]


def test_a_show_with_a_problem_is_not_built(tmp_path):
    ws = workspace(tmp_path)
    ws.set_timeline(60, [cue("a", "Look20-Top", 0, P1),
                         cue("b", "Look20-Top", 20, P2)], refresh=1.0)
    shows, problems = ws.compile_show()
    assert shows == {} and any("partial cue" in p for p in problems)
    ws.assign("Look20-Top", None)
    ws.set_timeline(60, [cue("a", "Look20-Top", 0, P1)], refresh=1.0)
    assert any("not assigned" in p for p in ws.compile_show()[1])
    ws.set_timeline(60, [], refresh=1.0)
    assert any("has no cues" in p for p in ws.compile_show()[1])


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
                                 setup_s=0.6, setup_board_s=0.0,
                                 # The real 30 s window a STOP's clear waits
                                 # out, compressed like every other wait here.
                                 clear_after_stop_s=0.2)
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
    fleet = Fleet({n: u.address for n, u in units.items()}, poll_s=0.05,
                  clear_after_stop_s=0.2, clear_after_move_s=0.2)
    fleet.start()
    assert wait_until(lambda: all(len(l._samples) >= 3
                                  for l in fleet.links.values()))
    yield ws, fleet, units, tmp_path
    fleet.stop()
    for unit in units.values():
        unit.close()


def timeline(ws, second_at=4, duration=30):
    ws.set_timeline(duration, [
        cue("a", "Look22", 0, "Look22_color_pattern01_grid.csv"),
        cue("b", "Look20-Top", 0, P1), cue("c", "Look20-Skirt", 0, S1),
        cue("d", "Look22", second_at, "Look22_color_pattern02_grid.csv",
            partial=True),
        cue("e", "Look20-Top", second_at, P2, partial=True)], refresh=1.0)
    shows, problems = ws.compile_show()
    assert problems == []
    return shows


def burned(fleet, units, timeout=15):
    """Upload starts the burn on every unit; PRESET/START wait for it (the
    page shows "Pictures written on n / n units" first)."""
    return wait_until(lambda: fleet._burn_problems(list(units)) == [],
                      timeout=timeout)


def test_upload_preset_start_and_both_units_fire_together(stage):
    ws, fleet, units, _ = stage
    shows = timeline(ws, second_at=6)
    results = fleet.upload(shows)
    assert all(r["ok"] and r["show"] == shows[n]["id"]
               for n, r in results.items())
    assert burned(fleet, units)
    fleet.preset()
    assert wait_until(lambda: all(u.player.applied == "q00"
                                  for u in units.values()))
    assert all(len(u.shows) == 1 for u in units.values())

    fleet.start_show(lead_s=0.5)
    t0 = fleet.run["t0"]
    assert wait_until(lambda: all(u.player.applied == "q01"
                                  for u in units.values()), timeout=8)
    fired = [u.shows[1] for u in units.values()]
    assert max(fired) - min(fired) < 0.05                   # together
    # "at" IS Start: sent at 0:06 sharp, on the PC's clock.
    offsets = [fleet.links[n].offset for n in units]
    for stamp, offset in zip(fired, offsets):
        assert -TICK <= (stamp - offset) - (t0 + 6.0) < 0.06
    assert all(len(u.shows) == 2 for u in units.values())   # preset not redone
    snap = fleet.snapshot()
    assert snap["run"]["state"] == "running" and snap["run"]["now"] > 5
    assert snap["corrections"] == []


def test_a_finished_show_is_left_alone(stage):
    ws, fleet, units, _ = stage
    fleet.upload(timeline(ws, second_at=6, duration=6))
    assert burned(fleet, units)
    fleet.preset()
    assert wait_until(lambda: all(u.player.applied == "q00"
                                  for u in units.values()))
    fleet.start_show(lead_s=0.3)
    assert wait_until(lambda: all(u.player.state == "ended"
                                  for u in units.values()), timeout=10)
    time.sleep(3.5)                     # longer than the supervision's pause
    assert fleet.snapshot()["corrections"] == []
    assert all(u.player.state == "ended" for u in units.values())


def test_hold_resume_and_next_move_every_unit_alike(stage):
    ws, fleet, units, _ = stage
    fleet.upload(timeline(ws, second_at=20))
    assert burned(fleet, units)
    fleet.preset()
    assert wait_until(lambda: all(u.player.applied == "q00"
                                  for u in units.values()))
    fleet.start_show(lead_s=0.3)
    time.sleep(0.6)
    fleet.hold()
    assert all(u.player.state == "holding" for u in units.values())
    held_now = fleet.snapshot()["run"]["now"]
    time.sleep(0.5)
    assert fleet.snapshot()["run"]["now"] == held_now        # the clock stands
    resumed = time.perf_counter()
    fleet.resume()
    assert all(u.player.state == "running" for u in units.values())
    # resume() fixes T0 before it posts to the units, and the clock runs
    # from then on: under load those posts take real time (0.5 s seen),
    # so the slack is the time actually spent, not a flat 0.2 s.
    assert (abs(fleet.snapshot()["run"]["now"] - held_now)
            < 0.2 + (time.perf_counter() - resumed))

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
    assert burned(fleet, units)
    fleet.preset()
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
    assert -TICK <= (fired[0] - offset) - (t0 + 6.0) < 0.06


def test_a_running_show_refuses_loose_cues_but_not_the_panic_white(stage):
    ws, fleet, units, _ = stage
    fleet.upload(timeline(ws, second_at=20))
    assert burned(fleet, units)
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

# ---- "Clear pictures after the show", end to end ----
# 2026-09-27: the operator pressed STOP and unplugged the Radxa from a
# garment whose boards were still on battery. A minute later the master board
# restarted the factory autoplay and cycled slots 0-18 - it replayed the
# show's pictures on its own. The checkbox next to (3) START is the answer.

DELETE = 0x14


def deleted(unit):
    return sorted((f.dest, f.data[0]) for f in unit.bus.requested
                  if f.cmd == DELETE)


def test_the_checkbox_travels_into_every_show_file_without_moving_its_id(tmp_path):
    ws = workspace(tmp_path)
    ws.set_timeline(60, [cue("a", "Look20-Top", 0, P1),
                         cue("b", "Look20-Skirt", 0, S1)], refresh=1.0)
    plain = ws.compile_show()[0]["radxa-02"]
    assert "clear_after_show" not in plain      # absent = false, as before
    ws.set_clear_after_show(True)
    assert ws.state()["show"]["clear_after_show"] is True
    ticked = ws.compile_show()[0]["radxa-02"]
    assert ticked["clear_after_show"] is True
    # The pictures did not change, so the show did not: a fleet already
    # holding this timeline is not asked to take it all again the evening
    # of the show, and no tile reads "old version".
    assert ticked["id"] == plain["id"]
    # Undoable like any other edit of the show, and off writes no key.
    assert ws.undo() is True
    assert "clear_after_show" not in ws.compile_show()[0]["radxa-02"]


def test_stop_clears_every_unit_and_repaints_nothing(stage):
    ws, fleet, units, _ = stage
    ws.set_clear_after_show(True)
    fleet.upload(timeline(ws, second_at=6))
    assert burned(fleet, units)
    assert fleet.clear_wanted() is True
    fleet.start_show(lead_s=0.3)
    assert wait_until(lambda: all(u.player.applied == "q00"
                                  for u in units.values()), timeout=8)
    painted = {n: len(u.shows) for n, u in units.items()}
    fleet.stop_show()
    for name, unit in units.items():
        assert wait_until(lambda u=unit: u.session.clear_record()["state"]
                          == "cleared", timeout=15), name
        # Slots 1-18 on every board of that garment, and nothing else.
        boards = ws.compile_show()[0][name]["boards"]
        assert deleted(unit) == sorted((b, s) for b in boards
                                      for s in range(1, 19))
        # Nothing was repainted on the way out: the garments are still
        # showing the last look they were given (the operator's rule).
        assert len(unit.shows) == painted[name]
        assert unit.player.status()["burn"]["state"] == "cleared"
    # ...and START now refuses, on every unit, until the pictures go back.
    assert wait_until(lambda: all("cleared" in p for p in
                                  fleet._burn_problems(list(units)))
                      and fleet._burn_problems(list(units)), timeout=8)
    with pytest.raises(ValueError, match="pictures were cleared after the "
                                         "last show"):
        fleet.start_show(lead_s=0.3, force=True)
    # An Upload is the way back.
    fleet.upload(timeline(ws, second_at=6))
    assert burned(fleet, units)
    fleet.start_show(lead_s=0.3)
    assert fleet.run is not None


def test_the_end_of_the_show_clears_it_with_nobody_pressing_anything(stage):
    ws, fleet, units, _ = stage
    ws.set_clear_after_show(True)
    fleet.upload(timeline(ws, second_at=3, duration=4))
    assert burned(fleet, units)
    fleet.start_show(lead_s=0.3)
    assert wait_until(lambda: all(u.player.state == "ended"
                                  for u in units.values()), timeout=12)
    # Supervision notices the end (CLEAR_AFTER_END_S past the duration) and
    # asks every unit, without an operator in the loop.
    for name, unit in units.items():
        assert wait_until(lambda u=unit: u.session.clear_record()["state"]
                          == "cleared", timeout=20), name
    assert any("clearing the pictures" in line for line in fleet.corrections)


def test_a_start_inside_the_stop_window_keeps_the_pictures(stage):
    # The director's mid-show abort, taken back: STOP then START, and the
    # show runs again without a three-minute re-Upload (review, 2026-09-27).
    ws, fleet, units, _ = stage
    ws.set_clear_after_show(True)
    fleet.upload(timeline(ws, second_at=20, duration=60))
    assert burned(fleet, units)
    fleet.start_show(lead_s=0.3)
    assert wait_until(lambda: all(u.player.applied == "q00"
                                  for u in units.values()), timeout=8)
    fleet.stop_show()
    assert fleet.clear_armed_in_s() is not None
    fleet.start_show(lead_s=0.3)             # "no, carry on"
    assert fleet.clear_armed_in_s() is None
    time.sleep(1.0)                          # longer than the 0.2 s window
    for unit in units.values():
        assert unit.session.clear_record()["state"] == "none"
        assert deleted(unit) == []
        assert unit.player.status()["burn"]["state"] == "burned"
    assert fleet.run is not None              # ...and the show is running


def test_the_box_left_unticked_changes_nothing(stage):
    ws, fleet, units, _ = stage
    fleet.upload(timeline(ws, second_at=3, duration=4))
    assert burned(fleet, units)
    fleet.start_show(lead_s=0.3)
    assert wait_until(lambda: all(u.player.state == "ended"
                                  for u in units.values()), timeout=12)
    fleet.stop_show()
    time.sleep(1.0)
    for unit in units.values():
        assert unit.session.clear_record()["state"] == "none"
        assert deleted(unit) == []
        assert unit.player.status()["burn"]["state"] == "burned"
