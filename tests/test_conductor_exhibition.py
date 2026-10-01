"""EXHIBITION mode (2026-09-30): the Conductor headless on radxa-05.

Four things, each against fakes - no unit, no mpg123, no network beyond
localhost:

* `serve --host`: the bind address is a flag, 127.0.0.1 stays the default,
  the reachable URLs are printed;
* THE SHOW's Loop: show.json's loop_wait_s (validation, off = no key, undo,
  export/import, never in the revision), POST /api/loop and /api/fleet's
  `loop` object, and the fleet's own restart - armed when a run reaches its
  end, fired after the wait with the show's countdown, cancelled by STOP and
  by any move, retried when refused, and never clearing pictures between runs;
* the speaker: conductor/speaker.py against a fake `mpg123 -R` - LOADPAUSED
  and the latency measurement, the unpause LATENCY before T0, HOLD / SEEK /
  STOP / the end of the show, a missing binary, a process that dies;
* the workspace .tar: export, import (409 while a run is active, 413 over
  the limit, 401 without the fleet token, hostile member names refused,
  fleet.json kept) and the server-to-server send job.

The page's own half is in tests/test_conductor_exhibition_page.py.
"""
from __future__ import annotations

import io
import json
import queue
import tarfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from conductor import __main__ as cli
from conductor.fleet import Fleet, pc_clock
from conductor.server import (LOOP_WAIT_S, WORKSPACE_TAR_MAX, WORKSPACE_TAR_MEMBERS,
                              Workspace, check_loop_wait, loop_wait_of, make_server,
                              parse_conductor_address, reachable_urls)
from conductor.speaker import LATENCY_SAMPLES, Speaker
from tests.test_fleet import StubLink
from tests.test_look import GRID, MAP

PAGE_TEXT = (Path(__file__).resolve().parents[1] / "conductor" / "web"
             / "index.html").read_text(encoding="utf-8")


# ------------------------------------------------------------ helpers

def _serve(server):
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server.server_address[1]


def _get(port, path, headers=None):
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                     headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


def _post(port, path, body, headers=None):
    if isinstance(body, (dict, list)):
        data = json.dumps(body).encode()
        head = {"Content-Type": "application/json"}
    else:
        data = body
        head = {"Content-Type": "application/x-tar"}
    head.update(headers or {})
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                     data=data, headers=head, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"raw": raw}


def _workspace(root, music=True):
    ws = Workspace(root)
    ws.save("Look23_map.csv", MAP)
    ws.save("Look23_color_ivory_grid.csv", GRID)
    ws.save("Look23_color_scarlet_grid.csv", GRID)
    ws.assign("Look23", "radxa-01")
    ws.set_timeline(120, [
        {"id": "p", "item": "Look23", "at": 0, "design": "Look23_color_ivory_grid.csv"},
        {"id": "c", "item": "Look23", "at": 60, "design": "Look23_color_scarlet_grid.csv"},
    ])
    if music:
        ws.save_music("show.mp3", io.BytesIO(b"\xff\xfb" * 3000), 6000)
    return ws


class Clock:
    """A clock the tests move by hand."""

    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def _fleet(clock, settings=None, **kw):
    fleet = Fleet({}, clock=clock, loop_settings=settings, **kw)
    fleet.links = {"radxa-01": StubLink("radxa-01", "stopped"),
                   "radxa-02": StubLink("radxa-02", "stopped")}
    fleet.shows = {"radxa-01": {"id": "showA", "cues": [], "duration": 100.0},
                   "radxa-02": {"id": "showA", "cues": [], "duration": 100.0}}
    return fleet


def _posted(fleet, path):
    return [(name, body) for name, link in fleet.links.items()
            for p, body in link.posted if p == path]


# ------------------------------------------------------------ 1. --host

def test_serve_host_is_a_flag_and_localhost_stays_the_default(monkeypatch):
    seen = {}

    def fake_serve(workspace, port, open_browser=False, host="127.0.0.1",
                   speaker=False, speaker_lead_ms=None, speaker_output=None,
                   speaker_factory=None, passcode=None, adopt=False, label=None):
        seen.update(workspace=workspace, port=port, host=host, speaker=speaker,
                    lead=speaker_lead_ms, output=speaker_output, passcode=passcode)
        return 0

    import conductor.server
    monkeypatch.setattr(conductor.server, "serve", fake_serve)
    assert cli.main(["serve"]) == 0
    assert seen["host"] == "127.0.0.1" and seen["speaker"] is False
    assert cli.main(["serve", "--host", "0.0.0.0", "--speaker",
                     "--speaker-lead-ms", "80", "--speaker-output", "alsa",
                     "--passcode", "s3", "--port", "8765",
                     "--workspace", "/home/radxa/exhibition"]) == 0
    assert seen == {"workspace": "/home/radxa/exhibition", "port": 8765,
                    "host": "0.0.0.0", "speaker": True, "lead": 80.0,
                    "output": "alsa", "passcode": "s3"}


def test_make_server_binds_the_host_it_is_given(tmp_path):
    server = make_server(tmp_path, port=0, host="0.0.0.0")
    try:
        assert server.server_address[0] == "0.0.0.0"
    finally:
        server.server_close()
    server = make_server(tmp_path, port=0)
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()


def test_reachable_urls_name_every_address_when_bound_to_all():
    assert reachable_urls("127.0.0.1", 8765) == ["http://127.0.0.1:8765"]
    urls = reachable_urls("0.0.0.0", 8765)
    assert urls[0] == "http://127.0.0.1:8765"
    assert all(u.startswith("http://") and u.endswith(":8765") for u in urls)
    assert len(urls) == len(set(urls))


# ------------------------------------------------------------ 2. the Loop

def test_loop_wait_is_zero_to_six_hundred_seconds_or_off_above_the_timelines_floor():
    assert check_loop_wait(None) is None
    assert check_loop_wait(40) == 40.0
    assert check_loop_wait(0) == 0.0
    assert check_loop_wait("６０") == 60.0             # NFKC, like the countdown
    assert check_loop_wait("42.34") == 42.3
    assert LOOP_WAIT_S == 45.0
    for bad in (-0.1, 600.1, True, "abc", "", [], {}, "0x10", float("nan")):
        with pytest.raises(ValueError, match="0 to 600"):
            check_loop_wait(bad)
    # The floor is the timeline's (loop_floor_of): named with the reason.
    with pytest.raises(ValueError, match="30 to 600 seconds here \\(tail\\)"):
        check_loop_wait(20, floor=30, why="tail")
    assert check_loop_wait(30, floor=30) == 30.0
    assert loop_wait_of({}) is None
    assert loop_wait_of({"loop_wait_s": "junk"}) is None
    assert loop_wait_of({"loop_wait_s": 45}) == 45.0


def _show_with_tail(duration, last_cue_at):
    return {"duration": duration, "cues": [
        {"id": "p", "item": "Look23", "at": 0, "design": "Look23_color_ivory_grid.csv"},
        {"id": "l", "item": "Look23", "at": last_cue_at,
         "design": "Look23_color_scarlet_grid.csv"}]}


def test_the_loop_floor_is_the_seam_minus_the_timelines_tail():
    from conductor.server import LOOP_SEAM_S, loop_effective_wait, loop_floor_of

    assert LOOP_SEAM_S == 40.0
    # The exhibition show: last cue 9:37, music end 10:54 - a 77 s tail.
    floor, tail, why = loop_floor_of(_show_with_tail(654.0, 577.0))
    assert (floor, tail) == (0.0, 77.0) and "77 s before the end" in why
    # A show whose last cue is 10 s before its end needs 30 s.
    floor, tail, why = loop_floor_of(_show_with_tail(120.0, 110.0))
    assert (floor, tail) == (30.0, 10.0) and "40 s are needed" in why
    # Rounded UP to a second; a cue at the very end needs the whole seam.
    assert loop_floor_of(_show_with_tail(120.0, 110.5))[0] == 31.0
    assert loop_floor_of(_show_with_tail(120.0, 120.0))[0] == 40.0
    # No cue: nothing to keep away from.
    assert loop_floor_of({"duration": 60.0, "cues": []})[0] == 0.0
    # The restart uses the stored wait, or the floor when it is higher.
    assert loop_effective_wait(dict(_show_with_tail(120.0, 110.0), loop_wait_s=10)) == 30.0
    assert loop_effective_wait(dict(_show_with_tail(120.0, 110.0), loop_wait_s=50)) == 50.0
    assert loop_effective_wait(_show_with_tail(120.0, 110.0)) is None


def test_the_loop_wait_is_validated_against_this_timelines_floor(tmp_path):
    ws = _workspace(tmp_path / "ws", music=False)       # 120 s, last cue at 60: tail 60
    assert ws.loop_floor()[0] == 0.0
    ws.set_loop(0)                                        # "LOOP はゼロ秒で再開"
    assert ws.loop_wait() == 0.0 and ws.loop_settings() == (0.0, 11.0, False)
    assert ws.state()["show"]["loop_min_wait_s"] == 0.0
    assert ws.state()["show"]["loop_tail_s"] == 60.0
    # The last cue moves to 10 s before the end: the floor is 30 now. The
    # stored 0 is KEPT, the Timeline warns, the restart waits the floor.
    ws.set_timeline(120, _show_with_tail(120.0, 110.0)["cues"])
    assert ws.loop_floor()[0] == 30.0
    assert ws.loop_wait() == 0.0
    assert ws.loop_settings() == (30.0, 11.0, False)
    state = ws.state()
    assert state["show"]["loop_min_wait_s"] == 30.0
    assert any("Loop: the wait of 0 s is below the 30 s" in w for w in state["show"]["warnings"])
    # ...and a new wait below the floor is refused with the number and why.
    with pytest.raises(ValueError) as refused:
        ws.set_loop(20)
    assert "30 to 600 seconds here" in str(refused.value)
    assert "10 s before the end" in str(refused.value)
    ws.set_loop(30)
    assert ws.loop_wait() == 30.0
    assert not any("Loop: the wait" in w for w in ws.state()["show"]["warnings"])
    # /api/fleet's loop carries the floor; a plain "on" lifts the default to it.
    ws.set_loop(None)
    ws.set_timeline(120, _show_with_tail(120.0, 119.0)["cues"])      # floor 39 < default 45
    server = make_server(ws.root, port=0, fleet=Fleet({}))
    port = _serve(server)
    try:
        loop = json.loads(_get(port, "/api/fleet")[1])["loop"]
        assert loop["min_wait_s"] == 39 and loop["on"] is False
        status, loop = _post(port, "/api/loop", {"on": True})
        assert status == 200 and loop["wait_s"] == 45
        status, answer = _post(port, "/api/loop", {"on": True, "wait_s": 20})
        assert status == 400 and "39 to 600" in answer["error"]
    finally:
        server.shutdown()
        server.server_close()


def test_loop_is_stored_with_the_show_off_as_no_key_and_undoable(tmp_path):
    ws = _workspace(tmp_path / "ws", music=False)
    assert ws.loop_wait() is None and ws.loop_settings() is None
    assert "loop_wait_s" not in ws._load_show()
    ws.set_loop(45)
    assert ws.loop_wait() == 45.0
    assert ws.loop_settings() == (45.0, 11.0, False)   # the wait, the countdown, preset first (off)
    ws.set_start_countdown(20)
    assert ws.loop_settings() == (45.0, 20.0, False)
    assert ws.state()["show"]["loop_wait_s"] == 45.0
    assert ws.state()["show"]["loop_default_s"] == LOOP_WAIT_S
    ws.set_loop(None)
    assert "loop_wait_s" not in ws._load_show()
    assert ws.undo() and ws.loop_wait() == 45.0
    assert ws.redo() and ws.loop_wait() is None
    with pytest.raises(ValueError):
        ws.set_loop(-1)

    # Not a step when nothing changes.
    before = ws.state()["history"]
    ws.set_loop(None)
    assert ws.state()["history"] == before


def test_loop_never_changes_the_revision_or_the_show_id(tmp_path):
    ws = _workspace(tmp_path / "ws", music=False)
    rev = ws.revision()
    shows, _ = ws.compile_show()
    ws.set_loop(120)
    assert ws.revision() == rev
    assert ws.compile_show()[0]["radxa-01"]["id"] == shows["radxa-01"]["id"]


def test_loop_travels_in_the_show_file_and_null_means_off(tmp_path):
    a = _workspace(tmp_path / "a", music=False)
    a.set_loop(90)
    exported = a.export_show()
    assert exported["loop_wait_s"] == 90.0
    b = _workspace(tmp_path / "b", music=False)
    b.import_show(exported)
    assert b.loop_wait() == 90.0
    # null in the file turns it off; a file without the key leaves it alone.
    b.import_show(dict(exported, loop_wait_s=None))
    assert b.loop_wait() is None
    b.set_loop(50)
    without = {k: v for k, v in exported.items() if k != "loop_wait_s"}
    b.import_show(without)
    assert b.loop_wait() == 50.0
    with pytest.raises(ValueError):
        b.import_show(dict(exported, loop_wait_s=-2))


def test_post_api_loop_answers_the_fleet_loop_object(tmp_path):
    ws = _workspace(tmp_path / "ws", music=False)
    fleet = Fleet({})
    server = make_server(ws.root, port=0, fleet=fleet)
    port = _serve(server)
    try:
        status, before, _ = _get(port, "/api/fleet")
        assert json.loads(before)["loop"] == {"on": False, "wait_s": 45,
                                              "next_in_s": None, "runs": 0,
                                              "problem": None, "min_wait_s": 0,
                                              "retrying": False, "waiting": False,
                                              "retry_in_s": None, "stored_wait_s": None}
        status, loop = _post(port, "/api/loop", {"on": True})
        assert status == 200 and loop["on"] and loop["wait_s"] == 45
        status, loop = _post(port, "/api/loop", {"on": True, "wait_s": 45})
        assert status == 200 and loop == {"on": True, "wait_s": 45,
                                          "next_in_s": None, "runs": 0,
                                          "problem": None, "min_wait_s": 0,
                                          "retrying": False, "waiting": False,
                                          "retry_in_s": None, "stored_wait_s": 45}
        assert ws.loop_wait() == 45.0
        assert json.loads(_get(port, "/api/fleet")[1])["loop"]["on"] is True
        status, loop = _post(port, "/api/loop", {"on": False, "wait_s": 45})
        assert status == 200 and loop["on"] is False and loop["wait_s"] == 45
        assert ws.loop_wait() is None
        for bad in ({}, {"on": "yes"}, {"on": True, "wait_s": 601},
                    {"on": True, "wait_s": "x"}, {"on": True, "wait_s": -1}):
            status, answer = _post(port, "/api/loop", bad)
            assert status == 400, bad
        # The old body shape the page used first is not an endpoint.
        assert _post(port, "/api/show/loop", {"wait_s": 30})[0] == 404
    finally:
        server.shutdown()
        server.server_close()


def test_the_fleet_arms_the_loop_at_the_end_and_starts_again_after_the_wait():
    clock = Clock()
    settings = {"value": (30.0, 11.0)}
    fleet = _fleet(clock, lambda: settings["value"])
    fleet.start_show(lead_s=11.0)
    t0 = fleet.run["t0"]
    assert t0 == clock.now + 11.0 and fleet.run["loops"] == 0
    assert fleet.loop_state() is None
    clock.now = t0 + 99.0
    fleet._loop_tick()
    assert fleet.loop_state() is None, "armed before the end"
    clock.now = t0 + 100.0
    fleet._loop_tick()
    pending = fleet.loop_state()
    assert pending["next_in_s"] == 30.0 and pending["runs"] == 0
    assert pending["problem"] is None
    assert any("Loop: next run in 30 s" in line for line in fleet.corrections)
    for link in fleet.links.values():
        link.posted.clear()
    clock.now += 29.0
    fleet._loop_tick()
    assert fleet.loop_state()["next_in_s"] == 1.0
    assert _posted(fleet, "/show/run") == []
    clock.now += 1.0
    fleet._loop_tick()
    # A START as the button makes one: t0 = now + the show's countdown,
    # from 0:00, posted to every unit with the run's force.
    assert fleet.run["t0"] == clock.now + 11.0 and fleet.run["loops"] == 1
    assert fleet.run["state"] == "running" and fleet.run["force"] is False
    runs = _posted(fleet, "/show/run")
    assert sorted(name for name, _ in runs) == ["radxa-01", "radxa-02"]
    assert all(body["t0"] == fleet.run["t0"] + 5.0 for _, body in runs)
    assert fleet.loop_state() is None                 # nothing pending now
    assert any("Loop: run 1 started" in line for line in fleet.corrections)
    # ...and again at the next end: the count goes up.
    clock.now = fleet.run["t0"] + 100.0
    fleet._loop_tick()
    assert fleet.loop_state()["runs"] == 1
    clock.now += 30.0
    fleet._loop_tick()
    assert fleet.run["loops"] == 2
    assert fleet.snapshot()["loop"] is None           # the raw fleet: pending only


def test_the_loop_reads_the_wait_and_the_countdown_when_they_matter():
    clock = Clock()
    settings = {"value": None}
    fleet = _fleet(clock, lambda: settings["value"])
    fleet.start_show(lead_s=3.0)
    clock.now = fleet.run["t0"] + 100.0
    fleet._loop_tick()
    assert fleet.loop_state() is None, "a Loop that is off armed a restart"
    # Turned on while the show sits at its end: armed on the next tick.
    settings["value"] = (60.0, 5.0)
    fleet._loop_tick()
    assert fleet.loop_state()["next_in_s"] == 60.0
    # Turned off during the wait: no next run.
    settings["value"] = None
    clock.now += 60.0
    fleet._loop_tick()
    assert fleet.loop_state() is None and fleet.run["loops"] == 0
    assert any("Loop turned off" in line for line in fleet.corrections)
    # On again, with a countdown changed during the wait: the new one runs.
    settings["value"] = (60.0, 5.0)
    fleet._loop_tick()
    settings["value"] = (60.0, 20.0)
    clock.now += 60.0
    fleet._loop_tick()
    assert fleet.run["loops"] == 1 and fleet.run["t0"] == clock.now + 20.0


def test_stop_and_every_move_cancel_the_pending_loop():
    clock = Clock()
    fleet = _fleet(clock, lambda: (30.0, 11.0))
    fleet.start_show(lead_s=1.0)
    clock.now = fleet.run["t0"] + 100.0
    fleet._loop_tick()
    assert fleet.loop_state() is not None
    fleet.stop_show()
    assert fleet.loop_state() is None and fleet.run is None
    clock.now += 100.0
    fleet._loop_tick()
    assert fleet.run is None, "the loop restarted a stopped show"
    # A seek back into the show, a HOLD and a NEXT all take the wait back;
    # the loop arms again when the run reaches its end once more.
    fleet.start_show(lead_s=1.0)
    clock.now = fleet.run["t0"] + 100.0
    fleet._loop_tick()
    assert fleet.loop_state() is not None
    fleet.seek(50.0, lead_s=1.0)
    assert fleet.loop_state() is None
    clock.now = fleet.run["t0"] + 100.0
    fleet._loop_tick()
    assert fleet.loop_state() is not None
    fleet.hold()
    assert fleet.loop_state() is None
    clock.now += 40.0
    fleet._loop_tick()
    assert fleet.loop_state() is None and fleet.run["state"] == "holding"
    fleet.resume()                          # past the end: arms afresh
    fleet._loop_tick()
    assert fleet.loop_state()["next_in_s"] == 30.0
    # A START press starts the count of runs over.
    fleet.start_show(lead_s=1.0)
    assert fleet.run["loops"] == 0 and fleet.loop_state() is None


def test_a_refused_restart_is_said_once_and_retried_until_it_lands():
    clock = Clock()
    fleet = _fleet(clock, lambda: (30.0, 11.0), loop_retry_s=5.0)
    fleet.start_show(lead_s=1.0)
    clock.now = fleet.run["t0"] + 100.0
    fleet._loop_tick()
    # radxa-02 goes off the air between runs: START would refuse, so does
    # the loop - once in the corrections, and again in five seconds.
    fleet.links["radxa-02"].online = False
    clock.now += 30.0
    fleet._loop_tick()
    assert fleet.run["loops"] == 0
    pending = fleet.loop_state()
    # Refused and retried: NO countdown (a 5 s retry published as one read
    # "next run in 0:03" over and over on the LCD), the retry for diagnostics.
    assert pending["next_in_s"] is None and pending["retrying"] is True
    assert pending["waiting"] is True and pending["retry_in_s"] == 5.0
    assert "radxa-02: not answering" in pending["problem"]
    said = [line for line in fleet.corrections if "cannot start again" in line]
    assert len(said) == 1
    clock.now += 5.0
    fleet._loop_tick()
    assert fleet.run["loops"] == 0 and fleet.loop_state()["next_in_s"] is None
    assert fleet.loop_state()["retrying"] and fleet.loop_state()["retry_in_s"] == 5.0
    assert len([l for l in fleet.corrections if "cannot start again" in l]) == 1
    fleet.links["radxa-02"].online = True
    clock.now += 5.0
    fleet._loop_tick()
    assert fleet.run["loops"] == 1 and fleet.loop_state() is None


def test_the_loop_never_forces_and_never_clears_between_runs():
    clock = Clock()
    fleet = _fleet(clock, lambda: (30.0, 11.0), clear_after_move_s=0.0)
    for show in fleet.shows.values():
        show["clear_after_show"] = True
    fleet.start_show(lead_s=1.0, force=True)
    assert fleet.run["clear_after_show"] is True and fleet.run["force"] is True
    clock.now = fleet.run["t0"] + 100.0
    fleet._loop_tick()
    # The END clear would go out CLEAR_AFTER_END_S past the end; with a
    # restart pending it stays where it is.
    clock.now += 10.0
    link = fleet.links["radxa-01"]
    assert fleet._clear_after_end(link, dict(fleet.run), fleet.shows["radxa-01"]) is False
    assert _posted(fleet, "/show/clear") == []
    assert all(body["force"] for _, body in _posted(fleet, "/show/run"))
    for link in fleet.links.values():
        link.posted.clear()
    clock.now += 20.0
    fleet._loop_tick()
    # The restart NEVER forces: waving failed boards through is the
    # operator's own answer at the START press, not something 3 a.m. inherits.
    assert fleet.run["loops"] == 1 and fleet.run["force"] is False
    assert _posted(fleet, "/show/run") and \
        not any(body["force"] for _, body in _posted(fleet, "/show/run"))
    assert _posted(fleet, "/show/clear") == []
    # STOP is where the clear happens with a Loop on: armed as ever.
    fleet.stop_show()
    assert fleet.clear_armed_in_s() is not None


def test_the_loop_thread_runs_on_its_own_and_stops_with_the_fleet():
    clock = Clock()
    fleet = _fleet(clock, lambda: (10.0, 2.0), loop_tick_s=0.01)
    for link in fleet.links.values():
        link.poll = lambda: False           # start() polls too; nobody answers
    fleet.start()
    try:
        fleet.start_show(lead_s=1.0)
        clock.now = fleet.run["t0"] + 100.0
        deadline = time.monotonic() + 2.0
        while fleet.loop_state() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert fleet.loop_state() is not None, "the loop thread never armed"
        clock.now += 10.0
        while fleet.run["loops"] == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert fleet.run["loops"] == 1
    finally:
        fleet.stop()


def test_run_snapshot_is_the_speakers_view_of_the_run():
    clock = Clock()
    fleet = _fleet(clock)
    assert fleet.run_snapshot() == (None, 100.0)
    fleet.start_show(lead_s=2.0)
    run, duration = fleet.run_snapshot()
    assert run["t0"] == clock.now + 2.0 and duration == 100.0
    run["t0"] = 0                                   # a copy, not the run
    assert fleet.run["t0"] == clock.now + 2.0


# ------------------------------------------------------------ 3. the speaker

class FakeMpg123:
    """`mpg123 -R` as the speaker sees it: two pipes and the @P protocol.

    Commands arrive on stdin (write + flush), answers are read line by line
    from stdout. `delay_s` is how long a P takes to answer - the unpause
    latency the speaker measures. `plays_on_load` makes LOADPAUSED answer
    "@P 2" the way an older build might."""

    def __init__(self, delay_s=0.0, plays_on_load=False, load_error=None):
        self.delay_s, self.plays_on_load, self.load_error = delay_s, plays_on_load, load_error
        self.commands: "list[tuple[float, str]]" = []     # (perf_counter, text)
        self.state = 0
        self.loaded = None
        self.loads = 0                                     # how many LP/L so far
        self.at_eof = False                                # the file ran out
        self.volume = 100
        self.position = 0.0
        self.alive = True
        self.stdin = self
        self.stdout = self
        self._lines: "list[bytes]" = []
        self._cond = threading.Condition()
        self._buffer = b""
        self.clock = time.perf_counter

    # ---- the pipe the speaker writes ----
    def write(self, data: bytes):
        self._buffer += data
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            self._handle(line.decode("utf-8"))
        return len(data)

    def flush(self):
        pass

    def _say(self, text: str):
        with self._cond:
            self._lines.append((text + "\n").encode("utf-8"))
            self._cond.notify_all()

    def _handle(self, text: str):
        self.commands.append((self.clock(), text))
        word, _, arg = text.partition(" ")
        if word == "SILENCE":
            self._say("@silence")
        elif word in ("LP", "LOADPAUSED", "L", "LOAD"):
            if self.load_error:
                self._say(f"@E {self.load_error}")
                self._say("@P 0")
                return
            self.loaded = arg
            self.loads += 1
            self.position = 0.0
            self.at_eof = False
            self.state = 2 if (word in ("L", "LOAD") or self.plays_on_load) else 1
            self._say("@I ID3:fake")
            self._say(f"@P {self.state}")
        elif word == "P":
            if self.state == 0:
                self._say("@E No track loaded!")
                return
            if self.delay_s:
                time.sleep(self.delay_s)
            if getattr(self, "burst_on_next_p", False):
                # The track ends in the same instant as this P: the EOF's
                # own "@F ... 0" / "@P 1", then this P's "@P 2" / "@P 1"
                # against the ended file - three @P lines in one burst.
                self.burst_on_next_p = False
                self.at_eof = True
                self.state = 1
                self._say("@F 323 0 7.75 0.00")
                self._say("@P 1")
                self._say("@P 2")
                self._say("@P 1")
                return
            if self.at_eof:
                # Nothing left to play: "@P 2" and at once "@P 1" again.
                self.p_at_eof = getattr(self, "p_at_eof", 0) + 1
                self.state = 1
                self._say("@P 2")
                self._say("@F 323 1 7.75 0.00")
                self._say("@P 1")
                return
            self.state = 1 if self.state == 2 else 2
            self._say(f"@P {self.state}")
        elif word == "V":
            self.volume = float(arg)
            self._say(f"@V {self.volume:.6f}%")
        elif word == "J":
            if self.state == 0:
                self._say("@E No track loaded!")
                return
            self.position = float(arg.rstrip("s"))
            self._say("@J 0")
        elif word == "Q":
            self.alive = False
            with self._cond:
                self._cond.notify_all()
        else:
            self._say(f"@E Unknown command: {word}")

    # ---- the pipe the speaker reads ----
    def readline(self) -> bytes:
        with self._cond:
            while not self._lines and self.alive:
                self._cond.wait(0.05)
            if self._lines:
                return self._lines.pop(0)
            return b""

    # ---- the process ----
    def poll(self):
        return None if self.alive else 0

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        self.alive = False

    def kill(self):
        self.alive = False

    def since(self, index=0):
        return [text for _, text in self.commands[index:]]

    def end_of_track(self, style="p1"):
        """What mpg123 prints when the file runs out. Measured on radxa-05
        (1.26.4, `-R --keep-open`, 2026-09-30): the last frame line with 0
        frames left, then an UNSOLICITED "@P 1"; the file stays loaded but
        cannot be played on - a P answers "@P 2" and at once "@P 1", a
        JUMP 0s answers "@J 0" and changes nothing; only a new LOADPAUSED
        plays again. `style="p0"` is a build that says "@P 0" instead."""
        if style == "p0":
            self.state = 0
            self.loaded = None
            self._say("@P 0")
            return
        self.at_eof = True
        self.state = 1
        self._say("@F 323 0 7.75 0.00")
        self._say("@P 1")

    def late_state_line(self, state):
        """A stray "@P n" that arrives on its own (mpg123 prints one after a
        seek in some builds): the speaker must take it as the truth without
        mistaking it for the answer to its next command."""
        self.state = state
        self._say(f"@P {state}")


class Stage:
    """A track and a run the tests set by hand, plus a speaker on them."""

    def __init__(self, tmp_path, fake=None, **kw):
        self.track_path = tmp_path / "show.mp3"
        self.track_path.write_bytes(b"\xff\xfb" * 100)
        self.track = self.track_path
        self.run = None
        self.duration = 60.0
        self.fake = fake or FakeMpg123()
        self.spawned = 0
        self.speaker = Speaker(lambda: self.track, lambda: (self.run, self.duration),
                               factory=self._factory, tick_s=0.005,
                               track_check_s=0.01, retry_s=0.2, **kw)

    def _factory(self, argv):
        self.spawned += 1
        self.argv = argv
        assert argv[:2] == ["mpg123", "-R"]
        return self.fake

    def wait_for(self, predicate, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.005)
        return predicate()

    def wait_state(self, state, timeout=3.0):
        assert self.wait_for(lambda: self.speaker.state == state, timeout), \
            (state, self.speaker.state, self.speaker.error, self.fake.since())


@pytest.fixture
def stage(tmp_path):
    made = Stage(tmp_path, extra_lead_s=0.0)
    made.speaker.start()
    yield made
    made.speaker.stop()


def test_the_speaker_loads_paused_and_measures_the_unpause_round_trip(tmp_path):
    fake = FakeMpg123(delay_s=0.02)
    st = Stage(tmp_path, fake=fake, extra_lead_s=0.05)
    st.speaker.start()
    try:
        st.wait_state("loaded")
        sent = fake.since()
        assert st.argv[:3] == ["mpg123", "-R", "--keep-open"]
        assert sent[0] == "SILENCE"
        assert sent[1] == f"LP {st.track_path}"
        assert sent[2] == "V 0"
        assert sent[3:3 + 2 * LATENCY_SAMPLES] == ["P", "P"] * LATENCY_SAMPLES
        assert sent[3 + 2 * LATENCY_SAMPLES:] == ["V 100", "J 0s"]
        status = st.speaker.status()
        assert status["available"] and status["error"] is None
        assert status["track"] == "show.mp3" and status["state"] == "loaded"
        # ~20 ms of fake round trip plus the 50 ms allowance, never more than
        # the bound.
        assert 60 <= status["latency_ms"] <= 200, status
        assert fake.state == 1 and fake.volume == 100 and fake.position == 0.0
    finally:
        st.speaker.stop()
    assert fake.since()[-1] == "Q"


def test_a_build_that_plays_on_loadpaused_is_paused_at_once(tmp_path):
    fake = FakeMpg123(plays_on_load=True)
    st = Stage(tmp_path, fake=fake, extra_lead_s=0.0)
    st.speaker.start()
    try:
        st.wait_state("loaded")
        assert fake.since()[1:4] == [f"LP {st.track_path}", "P", "V 0"]
        assert fake.state == 1
    finally:
        st.speaker.stop()


def test_the_unpause_goes_out_latency_early_at_t0(stage):
    stage.wait_state("loaded")
    clock = stage.speaker._clock
    latency = stage.speaker._latency
    mark = len(stage.fake.commands)
    # A START from 0:00 with a 0.3 s countdown: armed, silent, then P.
    stage.run = {"t0": clock() + 0.3, "state": "running", "held_at": None}
    stage.wait_state("armed")
    assert stage.fake.state == 1
    stage.wait_state("playing", timeout=2.0)
    unpaused = [(at, text) for at, text in stage.fake.commands[mark:] if text == "P"]
    assert len(unpaused) == 1
    early = stage.run["t0"] - unpaused[0][0]
    assert abs(early - latency) < 0.05, (early, latency)
    assert stage.fake.position == 0.0 and stage.fake.state == 2
    status = stage.speaker.status()
    assert status["playing"] and status["state"] == "playing"


def test_hold_pauses_resume_jumps_and_stop_rewinds(stage):
    stage.wait_state("loaded")
    clock = stage.speaker._clock
    stage.run = {"t0": clock() - 10.0, "state": "running", "held_at": None}
    stage.wait_state("playing")
    mark = len(stage.fake.commands)
    # A START from a mark: JUMP to the position (+ latency), then P.
    jumps = [t for t in stage.fake.since() if t.startswith("J ")]
    assert jumps and 9.9 <= float(jumps[-1][2:-1]) <= 10.6, jumps
    # HOLD keeps the position.
    stage.run = {"t0": stage.run["t0"], "state": "holding", "held_at": clock()}
    stage.wait_state("paused")
    assert stage.fake.since(mark) == ["P"] and stage.fake.state == 1
    mark = len(stage.fake.commands)
    # RESUME: a new T0, a JUMP and an unpause.
    stage.run = {"t0": clock() - 12.0, "state": "running", "held_at": None}
    stage.wait_state("playing")
    sent = stage.fake.since(mark)
    assert sent[0].startswith("J 12.") or sent[0].startswith("J 11.9"), sent
    assert sent[1] == "P" and stage.fake.state == 2
    mark = len(stage.fake.commands)
    # A SEEK while playing: a JUMP alone.
    stage.run = {"t0": clock() - 30.0, "state": "running", "held_at": None}
    assert stage.wait_for(lambda: any(t.startswith("J 30") for t in stage.fake.since(mark)))
    assert "P" not in stage.fake.since(mark)
    mark = len(stage.fake.commands)
    # STOP: pause and back to the top, the track still loaded.
    stage.run = None
    stage.wait_state("loaded")
    assert stage.fake.since(mark) == ["P", "J 0s"]
    assert stage.fake.state == 1 and stage.fake.loaded is not None


def test_the_music_stops_at_the_end_of_the_show_not_of_the_track(stage):
    stage.wait_state("loaded")
    clock = stage.speaker._clock
    stage.duration = 0.4
    stage.run = {"t0": clock() - 0.1, "state": "running", "held_at": None}
    stage.wait_state("playing")
    stage.wait_state("ended", timeout=2.0)
    assert stage.fake.state == 1 and stage.fake.position == 0.0
    # The Loop's next run: a new T0 ahead - armed again from the top.
    stage.run = {"t0": clock() + 0.5, "state": "running", "held_at": None}
    stage.wait_state("armed")
    stage.wait_state("playing", timeout=2.0)


def test_no_mpg123_is_a_status_not_a_failure(tmp_path):
    def missing(argv):
        raise FileNotFoundError(2, "No such file or directory", "mpg123")

    st = Stage(tmp_path)
    st.speaker._factory = missing
    st.speaker.start()
    try:
        assert st.wait_for(lambda: st.speaker.status()["error"] is not None)
        status = st.speaker.status()
        assert status["available"] is False
        assert "mpg123 not found" in status["error"] and "apt install mpg123" in status["error"]
        # Said once, not once per tick.
        time.sleep(0.1)
        assert len([l for l in st.speaker.log if "not found" in l]) == 1
        # The run proceeds; nothing raises, nothing changes.
        st.run = {"t0": time.perf_counter(), "state": "running", "held_at": None}
        time.sleep(0.05)
        assert st.speaker.status()["error"] is not None
    finally:
        st.speaker.stop()


def test_a_dead_mpg123_is_reported_and_respawned(tmp_path):
    st = Stage(tmp_path, extra_lead_s=0.0)
    st.speaker.start()
    try:
        st.wait_state("loaded")
        first = st.fake
        first.alive = False                        # it died
        assert st.wait_for(lambda: "exited" in (st.speaker.status()["error"] or ""))
        assert st.speaker.status()["available"] is False
        st.fake = FakeMpg123()                     # the next spawn gets a new one
        assert st.wait_for(lambda: st.speaker.status()["error"] is None
                           and st.speaker.status()["state"] == "loaded", timeout=3.0)
        assert st.spawned == 2
    finally:
        st.speaker.stop()


def test_a_track_that_will_not_load_is_said_and_retried(tmp_path):
    fake = FakeMpg123(load_error="Error opening stream")
    st = Stage(tmp_path, fake=fake)
    st.speaker.start()
    try:
        assert st.wait_for(lambda: st.speaker.status()["error"] is not None)
        assert "could not load show.mp3" in st.speaker.status()["error"]
        fake.load_error = None
        st.track_path.write_bytes(b"\xff\xfb" * 120)     # a new file: tried again
        st.wait_state("loaded")
    finally:
        st.speaker.stop()


def test_no_track_is_idle_and_a_removed_track_lets_mpg123_go(stage):
    stage.wait_state("loaded")
    stage.track = None
    stage.wait_state("idle")
    assert stage.fake.since()[-1] == "Q"
    status = stage.speaker.status()
    assert status["available"] is False and status["error"] is None
    assert status["state"] == "idle" and status["track"] is None
    assert status["playing"] is False


def test_serve_wires_the_speaker_to_the_workspace_music(tmp_path, monkeypatch):
    """serve(): the track is the workspace's music file, the run is the
    fleet's, /api/fleet carries `speaker`, and the whole thing stops."""
    import conductor.server as srv

    ws = _workspace(tmp_path / "ws")
    fake = FakeMpg123()
    made = {}

    class Once(srv._Server):
        def serve_forever(self, poll_interval=0.5):
            made["handler"] = self.RequestHandlerClass
            made["port"] = self.server_address[1]
            # Let the speaker find the track, then ask /api/fleet.
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if made["handler"].speaker.status()["state"] == "loaded":
                    break
                time.sleep(0.01)
            threading.Thread(target=super().serve_forever, daemon=True).start()
            status, body = _get(made["port"], "/api/fleet")[:2]
            made["fleet"] = json.loads(body)
            self.shutdown()

    monkeypatch.setattr(srv, "_Server", Once)
    monkeypatch.setattr(srv, "already_serving", lambda port: False)
    assert srv.serve(ws.root, port=0, speaker=True, speaker_lead_ms=0,
                     speaker_factory=lambda argv: fake) == 0
    speaker = made["fleet"]["speaker"]
    assert speaker["available"] and speaker["track"] == "show.mp3"
    assert speaker["state"] == "loaded" and speaker["error"] is None
    assert made["fleet"]["loop"] == {"on": False, "wait_s": 45, "next_in_s": None,
                                     "runs": 0, "problem": None, "min_wait_s": 0,
                                     "retrying": False, "waiting": False,
                                     "retry_in_s": None, "stored_wait_s": None}
    assert fake.since()[1] == f"LP {ws.music / 'show.mp3'}"
    assert fake.since()[-1] == "Q"                  # stopped with the server


def test_without_speaker_the_fleet_says_null(tmp_path):
    server = make_server(tmp_path, port=0, fleet=Fleet({}))
    port = _serve(server)
    try:
        assert json.loads(_get(port, "/api/fleet")[1])["speaker"] is None
    finally:
        server.shutdown()
        server.server_close()


# ------------------------------------------------------------ 4. the workspace .tar

def test_export_tar_carries_the_workspace_and_never_fleet_json(tmp_path):
    ws = _workspace(tmp_path / "ws")
    (ws.root / "fleet.json").write_text('{"token": "secret"}', encoding="utf-8")
    ws.set_loop(45)
    out = io.BytesIO()
    counts = ws.export_tar(out)
    assert counts == {"files": 3, "music": "show.mp3", "show": True, "history": True}
    out.seek(0)
    with tarfile.open(fileobj=out, mode="r:") as tar:
        names = sorted(m.name for m in tar.getmembers())
        assert names == ["files/Look23_color_ivory_grid.csv",
                         "files/Look23_color_scarlet_grid.csv",
                         "files/Look23_map.csv", "history.json", "music/show.mp3",
                         "show.json"]
        assert all(m.isfile() and m.uname == "" and m.uid == 0 for m in tar.getmembers())
        show = json.loads(tar.extractfile("show.json").read())
        assert show["loop_wait_s"] == 45.0


def test_import_tar_replaces_the_workspace_whole_and_keeps_fleet_json(tmp_path):
    a = _workspace(tmp_path / "a")
    a.set_loop(45)
    packed = io.BytesIO()
    a.export_tar(packed)
    b = Workspace(tmp_path / "b")
    b.save("Old_map.csv", MAP)
    b.save_music("old.wav", io.BytesIO(b"RIFF" * 10), 40)
    (b.root / "fleet.json").write_text('{"units": {"radxa-01": "10.42.0.101:8787"}}',
                                       encoding="utf-8")
    b.mark_written("upload", "r1", units=["radxa-01"], all_units=["radxa-01"])
    tar_path = tmp_path / "ws.tar"
    tar_path.write_bytes(packed.getvalue())
    counts = b.import_tar(tar_path)
    assert counts == {"files": 3, "music": "show.mp3", "show": True, "history": True}
    assert sorted(p.name for p in b.files.iterdir()) == [
        "Look23_color_ivory_grid.csv", "Look23_color_scarlet_grid.csv", "Look23_map.csv"]
    assert sorted(p.name for p in b.music.iterdir()) == ["show.mp3"]
    assert b.loop_wait() == 45.0 and b.music_info()["name"] == "show.mp3"
    assert json.loads((b.root / "fleet.json").read_text()) == {
        "units": {"radxa-01": "10.42.0.101:8787"}}
    assert sorted(p.name for p in b.root.iterdir()) == [
        "files", "fleet.json", "history.json", "music", "show.json"]
    assert b.marks == {} and b.unit_marks == {} and b.compiled is None
    # The same show compiles to the same pictures on both sides (the id
    # itself carries the workspace's NAME - showfile.build - so a
    # "showdata" and an "exhibition" folder give two ids for one show).
    mine, theirs = b.compile_show()[0]["radxa-01"], a.compile_show()[0]["radxa-01"]
    assert mine["cues"] == theirs["cues"] and mine["duration"] == theirs["duration"]


def _tar_with(members):
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as tar:
        for name, data in members:
            info = tarfile.TarInfo(name)
            if data is None:
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
                continue
            if isinstance(data, tarfile.TarInfo):
                tar.addfile(data)
                continue
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return out.getvalue()


def test_import_tar_refuses_anything_that_is_not_a_workspace_file(tmp_path):
    b = _workspace(tmp_path / "b", music=False)
    before = sorted(p.name for p in b.files.iterdir())
    link = tarfile.TarInfo("files/evil_map.csv")
    link.type = tarfile.SYMTYPE
    link.linkname = "/etc/passwd"
    hostile = [
        [("../escape.json", b"{}")],
        [("/etc/passwd", b"x")],
        [("files/../../x_map.csv", b"x")],
        [("files/nested/Look_map.csv", b"x")],
        [("files/notacsv.txt", b"x")],
        [("files/a:b_map.csv", b"x")],
        [("music/track.exe", b"x")],
        [("music/../x.mp3", b"x")],
        [("other.json", b"{}")],
        [("secrets/", None)],
        [("show.json", b"not json")],
        [("history.json", b"{")],
        [("files/evil_map.csv", link)],
    ]
    for members in hostile:
        tar_path = tmp_path / "bad.tar"
        tar_path.write_bytes(_tar_with(members))
        with pytest.raises(ValueError):
            b.import_tar(tar_path)
        assert sorted(p.name for p in b.files.iterdir()) == before, members
        assert not any(p.name.startswith(".import") for p in b.root.iterdir())
    # fleet.json inside the tar is skipped, directory entries are fine.
    (b.root / "fleet.json").write_text('{"token": "mine"}', encoding="utf-8")
    tar_path = tmp_path / "ok.tar"
    tar_path.write_bytes(_tar_with([("files/", None), ("music/", None),
                                    ("fleet.json", b'{"token": "theirs"}'),
                                    ("show.json", b'{"duration": 30}'),
                                    ("files/X_map.csv", MAP.encode("utf-8"))]))
    counts = b.import_tar(tar_path)
    assert counts == {"files": 1, "music": None, "show": True, "history": False}
    assert (b.root / "fleet.json").read_text(encoding="utf-8") == '{"token": "mine"}'
    assert sorted(p.name for p in b.files.iterdir()) == ["X_map.csv"]
    assert not (b.root / "history.json").exists()
    assert b.state()["show"]["duration"] == 30.0


def test_import_over_http_round_trips_and_refuses_while_a_run_is_active(tmp_path):
    a = _workspace(tmp_path / "a")
    a.set_loop(60)
    fleet = Fleet({})
    server = make_server(tmp_path / "b", port=0, fleet=fleet)
    port = _serve(server)
    b = server.RequestHandlerClass.workspace
    try:
        packed = io.BytesIO()
        a.export_tar(packed)
        body = packed.getvalue()
        fleet.run = {"t0": 0.0, "state": "running", "held_at": None}
        status, answer = _post(port, "/api/workspace/import", body)
        assert status == 409 and "STOP" in answer["error"]
        assert list(b.files.iterdir()) == []
        fleet.run = None
        status, answer = _post(port, "/api/workspace/import", body)
        assert status == 200, answer
        assert answer["ok"] and answer["files"] == 3 and answer["music"] == "show.mp3"
        assert answer["cues"] == 2 and answer["history"] is True
        assert answer["shows"] == {"radxa-01": b.compile_show()[0]["radxa-01"]["id"]}
        # A PC (no --adopt) takes the workspace but not its Loop - said in the reply.
        assert answer["problems"] == ["the imported show had Loop on - turned off here (only the exhibition's Conductor keeps it)"]
        assert b.loop_wait() is None
        # Not a tar at all.
        status, answer = _post(port, "/api/workspace/import", b"hello")
        assert status == 400 and "not a workspace tar" in answer["error"]
        # Content-Length past the limit: refused before the body is read.
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/workspace/import", data=b"x",
            headers={"Content-Type": "application/x-tar",
                     "Content-Length": str(WORKSPACE_TAR_MAX + 1)}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                status = response.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        except OSError:
            status = 413                        # the connection was closed on us
        assert status == 413
        # The export endpoint answers the same bytes back.
        status, raw, headers = _get(port, "/api/workspace/export")
        assert status == 200 and headers["Content-Type"] == "application/x-tar"
        assert headers["Content-Disposition"].startswith('attachment; filename="b-workspace-')
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as tar:
            assert "music/show.mp3" in tar.getnames()
    finally:
        server.shutdown()
        server.server_close()


def test_the_fleet_token_gates_both_workspace_endpoints(tmp_path):
    """The 401 is answered BEFORE the body is wanted; the server drains a
    small body first (Handler._refuse_early), so the client sees the
    status and not a Windows connection reset - this asserts the status
    with a small body, repeatedly, the way the flake showed up."""
    a = _workspace(tmp_path / "a", music=False)
    server = make_server(tmp_path / "b", port=0, fleet=Fleet({}), token="s3cret")
    port = _serve(server)
    try:
        packed = io.BytesIO()
        a.export_tar(packed)
        body = packed.getvalue()
        assert len(body) < 64 * 1024                    # a small body, drained
        assert _get(port, "/api/workspace/export")[0] == 401
        assert _get(port, "/api/workspace/export", {"X-Show-Token": "wrong"})[0] == 401
        assert _get(port, "/api/workspace/export", {"X-Show-Token": "s3cret"})[0] == 200
        for _ in range(5):
            assert _post(port, "/api/workspace/import", body)[0] == 401
            assert _post(port, "/api/workspace/import", body,
                         {"X-Show-Token": "wrong"})[0] == 401
        assert _post(port, "/api/workspace/import", body,
                     {"X-Show-Token": "s3cret"})[0] == 200
    finally:
        server.shutdown()
        server.server_close()


def test_an_early_refusal_drains_a_small_body_and_closes_on_a_big_one(tmp_path):
    import http.client

    fleet = Fleet({})
    server = make_server(tmp_path / "b", port=0, fleet=fleet, token="s3cret")
    port = _serve(server)
    try:
        # Small: the whole body is read, the 401 arrives cleanly. (The
        # server speaks HTTP/1.0 and closes after every answer - there is
        # no keep-alive here; http.client simply reconnects for the GET.)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("POST", "/api/workspace/import", body=b"x" * 100000,
                     headers={"Content-Type": "application/x-tar"})
        response = conn.getresponse()
        assert response.status == 401 and response.version == 10
        response.read()
        conn.request("GET", "/api/state")
        assert conn.getresponse().status == 200
        conn.close()
        # The 409 to an AUTHENTICATED client drains the whole body however
        # big (a 5-30 MB Send over the hotspot takes longer than any short
        # grace): the client reads the 409's text, never a reset.
        from conductor.server import EARLY_DRAIN_MAX
        fleet.run = {"t0": 0.0, "state": "running", "held_at": None}
        try:
            for _ in range(2):
                status, answer = _post(port, "/api/workspace/import",
                                       b"x" * (EARLY_DRAIN_MAX * 3),
                                       {"X-Show-Token": "s3cret"})
                assert status == 409 and "STOP it first" in answer["error"]
        finally:
            fleet.run = None
        # Big (past EARLY_DRAIN_MAX), NOT authenticated: the status still
        # comes, with Connection: close, because the server goes on reading
        # for a bounded while after answering - a client that finishes
        # sending reads it instead of a reset. Twice, the way the flake showed.
        for _ in range(2):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("POST", "/api/workspace/import",
                         body=b"x" * (EARLY_DRAIN_MAX + 1),
                         headers={"Content-Type": "application/x-tar"})
            response = conn.getresponse()
            assert response.status == 401
            assert response.getheader("Connection", "").lower() == "close"
            response.read()
            conn.close()
    finally:
        server.shutdown()
        server.server_close()


def test_send_workspace_is_a_job_the_page_polls(tmp_path):
    a = _workspace(tmp_path / "a")
    (a.root / "fleet.json").write_text('{"token": "shared"}', encoding="utf-8")
    sender = make_server(a.root, port=0, fleet=Fleet({}), token="shared")
    receiver = make_server(tmp_path / "b", port=0, fleet=Fleet({}), token="shared")
    sp, rp = _serve(sender), _serve(receiver)
    try:
        status, job = _post(sp, "/api/workspace/send", {"to": f"127.0.0.1:{rp}"})
        assert status == 200 and job["ok"] and job["state"] in ("packing", "sending", "done")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            status, raw, _ = _get(sp, f"/api/workspace/send?job={job['job']}")
            job = json.loads(raw)
            if job["state"] in ("done", "failed"):
                break
            time.sleep(0.05)
        assert job["state"] == "done", job
        assert job["sent"] == job["total"] > 6000
        assert job["counts"]["music"] == "show.mp3"
        assert job["reply"]["files"] == 3 and job["reply"]["cues"] == 2
        assert job["reply"]["shows"]
        b = receiver.RequestHandlerClass.workspace
        assert b.music_info()["name"] == "show.mp3"
        # A receiver that is not there, and a bad address, are answers too.
        status, job = _post(sp, "/api/workspace/send", {"to": "127.0.0.1:9"})
        assert status == 200
        deadline = time.monotonic() + 10
        while job["state"] not in ("done", "failed") and time.monotonic() < deadline:
            time.sleep(0.05)
            job = json.loads(_get(sp, f"/api/workspace/send?job={job['job']}")[1])
        assert job["state"] == "failed" and job["error"]
        assert _post(sp, "/api/workspace/send", {"to": "not a host:port!"})[0] == 400
        assert _get(sp, "/api/workspace/send?job=nope")[0] == 404
    finally:
        sender.shutdown()
        sender.server_close()
        receiver.shutdown()
        receiver.server_close()


def test_conductor_addresses_are_read_generously():
    assert parse_conductor_address("radxa-05:8765") == ("radxa-05", 8765)
    assert parse_conductor_address(" radxa-05 ") == ("radxa-05", 8765)
    assert parse_conductor_address("http://10.42.0.1:8765/") == ("10.42.0.1", 8765)
    assert parse_conductor_address("ｒadxa-05：8766") == ("radxa-05", 8766)   # NFKC
    for bad in ("", None, "a b", "host:0", "host:99999", "http://", "a/b:1"):
        with pytest.raises(ValueError):
            parse_conductor_address(bad)


# ------------------------------------------------------------ 5. Wi-Fi

def test_wifi_select_tells_every_online_unit_and_this_host_last(tmp_path):
    fleet = Fleet({})
    order = []

    class Link(StubLink):
        def __init__(self, name, address):
            super().__init__(name, "stopped")
            self.address = address

        def post(self, path, body, learn=True, timeout=None):
            order.append((self.name, path, body, learn))
            if self.name == "radxa-02":
                raise RuntimeError("a show is running on this unit")
            return {"scheduled": True, "after_s": body["after_s"]}

    fleet.links = {"radxa-05": Link("radxa-05", "127.0.0.1:8787"),
                   "radxa-01": Link("radxa-01", "10.42.0.101:8787"),
                   "radxa-02": Link("radxa-02", "10.42.0.102:8787"),
                   "radxa-03": Link("radxa-03", "10.42.0.103:8787")}
    fleet.links["radxa-03"].online = False
    server = make_server(tmp_path, port=0, fleet=fleet)
    port = _serve(server)
    try:
        status, answer = _post(port, "/api/fleet/wifi_select",
                               {"profile": "AZ-Epaper", "after_s": 20})
        assert status == 200, answer
        assert answer["last"] == ["radxa-05"] and answer["after_s"] == 20.0
        assert answer["hotspot"] == "radxa-05"
        assert answer["units"]["radxa-01"] == {"ok": True, "scheduled": True, "after_s": 20.0}
        assert answer["units"]["radxa-02"] == {"ok": False, "error": "a show is running on this unit"}
        assert answer["units"]["radxa-03"] == {"ok": False, "error": "offline"}
        # Towards the hotspot: the hotspot unit gets the short lead (it has
        # to be up before the clients look for it), the clients the long one
        # - and the Conductor's own unit is still told LAST.
        assert answer["units"]["radxa-05"] == {"ok": True, "scheduled": True, "after_s": 5.0}
        told = [name for name, path, _, _ in order if path == "/wifi/select"]
        assert told[-1] == "radxa-05" and set(told[:-1]) == {"radxa-01", "radxa-02"}
        leads = {name: body["after_s"] for name, _, body, _ in order}
        assert leads == {"radxa-01": 20.0, "radxa-02": 20.0, "radxa-05": 5.0}
        assert all(body["profile"] == "AZ-Epaper" and learn is False
                   for _, _, body, learn in order)
        # Towards the router: the clients go first (5 s), the hotspot after
        # they have left (20 s).
        order.clear()
        status, answer = _post(port, "/api/fleet/wifi_select",
                               {"profile": "show-router", "after_s": 20})
        leads = {name: body["after_s"] for name, _, body, _ in order}
        assert leads == {"radxa-01": 5.0, "radxa-02": 5.0, "radxa-05": 20.0}
        assert [n for n, p, _, _ in order][-1] == "radxa-05"
        # after_s is clamped to what the unit takes (3-120).
        assert _post(port, "/api/fleet/wifi_select",
                     {"profile": "x", "after_s": 120})[0] == 200
        for bad in ({}, {"profile": ""}, {"profile": "x", "after_s": 1},
                    {"profile": "x", "after_s": 121},
                    {"profile": "x", "after_s": "soon"}):
            assert _post(port, "/api/fleet/wifi_select", bad)[0] == 400, bad
    finally:
        server.shutdown()
        server.server_close()


def test_the_units_wifi_reaches_the_tile():
    from conductor.fleet import UnitLink

    link = UnitLink("radxa-05", "127.0.0.1:8787")
    link.status = {"phase": "local", "wifi": {"ssid": "AZ-Epaper", "ip": "10.42.0.1",
                                              "signal": None, "mode": "hotspot",
                                              "profile": "AZ-Epaper"}}
    assert link.snapshot()["wifi"]["mode"] == "hotspot"
    link.status = {"phase": "local"}
    assert link.snapshot()["wifi"] is None


# ------------------------------------------------------------ review round (91275c6)

def test_the_speaker_keeps_mpg123s_own_play_state_and_never_doubles_a_p(tmp_path):
    """H1: P toggles, so it goes out only when mpg123's last "@P n" differs
    from what is wanted - a stray "@P" line is taken as the truth, and a
    late answer is never mistaken for the reply to the next command."""
    fake = FakeMpg123(delay_s=0.2)
    st = Stage(tmp_path, fake=fake, extra_lead_s=0.0)
    st.speaker.start()
    try:
        st.wait_state("loaded")
        # A slow P: the measured latency is the round trip (bounded).
        assert 180 <= st.speaker.status()["latency_ms"] <= 500
        clock = st.speaker._clock
        st.run = {"t0": clock() - 10.0, "state": "running", "held_at": None}
        st.wait_state("playing")
        mark = len(fake.commands)
        # A second move while already playing: a JUMP and no P at all.
        st.run = {"t0": clock() - 20.0, "state": "running", "held_at": None}
        assert st.wait_for(lambda: any(t.startswith("J 20") for t in fake.since(mark)))
        time.sleep(0.1)
        assert "P" not in fake.since(mark)
        # HOLD: one P. Then mpg123 says on its own that it is PLAYING (a
        # stray line): the truth is taken, and the RESUME sends no P at all.
        st.run = {"t0": st.run["t0"], "state": "holding", "held_at": clock()}
        st.wait_state("paused")
        assert fake.since(mark).count("P") == 1
        fake.late_state_line(2)
        assert st.wait_for(lambda: st.speaker.playing)
        mark = len(fake.commands)
        st.run = {"t0": clock() - 30.0, "state": "running", "held_at": None}
        st.wait_state("playing")
        time.sleep(0.05)
        assert "P" not in fake.since(mark) and fake.since(mark)[0].startswith("J 30")
    finally:
        st.speaker.stop()


@pytest.mark.parametrize("style", ["p1", "p0"])
def test_a_track_shorter_than_the_show_is_reloaded_for_the_next_run(tmp_path, style):
    """H1: the end of the file - the unsolicited "@P 1" mpg123 1.26.4 really
    prints (measured on radxa-05), or a "@P 0" - drops the loaded track; it
    is loaded again from the top with LP (never jumped: a JUMP does not
    make an ended file playable), THIS run stays silent without a P every
    tick, and the next run - the Loop's - has music."""
    st = Stage(tmp_path, extra_lead_s=0.0)
    st.speaker.start()
    try:
        st.wait_state("loaded")
        clock = st.speaker._clock
        st.duration = 60.0
        st.run = {"t0": clock() - 1.0, "state": "running", "held_at": None}
        st.wait_state("playing")
        assert st.fake.loads == 1
        mark = len(st.fake.commands)
        st.fake.end_of_track(style)                  # the file ran out at 0:30 of 1:00
        assert st.wait_for(lambda: st.fake.loads == 2 and st.speaker.state == "ended", 3.0)
        assert not st.fake.at_eof and st.fake.state == 1 and st.fake.position == 0.0
        # Between the EOF and the reload nothing was jumped and nothing
        # was toggled against the ended file; the reload is one LP.
        before_reload = st.fake.since(mark)[:st.fake.since(mark).index(f"LP {st.track_path}")]
        assert "P" not in before_reload and not any(t.startswith("J") for t in before_reload)
        time.sleep(0.2)
        assert st.fake.state == 1, "the same run was followed back into the track"
        assert getattr(st.fake, "p_at_eof", 0) == 0
        # The next run: armed and unpaused from the top.
        st.run = {"t0": clock() + 0.3, "state": "running", "held_at": None}
        st.wait_state("armed")
        st.wait_state("playing", timeout=2.0)
        assert st.fake.loads == 2 and st.fake.state == 2
    finally:
        st.speaker.stop()


def test_a_p_that_lands_on_the_ended_file_is_read_as_eof_not_retried(tmp_path):
    """H1 (2): the "@P 2" then "@P 1" a P gets at EOF must not become a P
    every 50 ms - the pair is the end of the file, and a reload follows."""
    st = Stage(tmp_path, extra_lead_s=0.0)
    st.speaker.start()
    try:
        st.wait_state("loaded")
        clock = st.speaker._clock
        # The file runs out while the speaker is paused on HOLD - so the
        # unsolicited line cannot be seen as such - and the RESUME's P then
        # meets the ended file.
        st.duration = 60.0
        st.run = {"t0": clock() - 1.0, "state": "running", "held_at": None}
        st.wait_state("playing")
        st.run = {"t0": st.run["t0"], "state": "holding", "held_at": clock()}
        st.wait_state("paused")
        st.fake.at_eof = True                        # ran out (no line: it was paused)
        st.run = {"t0": clock() - 2.0, "state": "running", "held_at": None}
        assert st.wait_for(lambda: st.fake.loads == 2, 3.0), st.fake.since()
        time.sleep(0.3)
        assert getattr(st.fake, "p_at_eof", 0) == 1, "P was retried against the ended file"
        assert st.speaker.state == "ended" and st.fake.state == 1
        assert st.speaker.error is None
    finally:
        st.speaker.stop()


def test_loop_run_two_has_music(tmp_path):
    """H1 end to end: a real Fleet with its loop thread and a Speaker on
    its run_snapshot(), a short show whose track ends early - the Loop's
    second run gets a fresh track and an unpause at its T0."""
    fake = FakeMpg123()
    track = tmp_path / "show.mp3"
    track.write_bytes(b"\xff\xfb" * 100)
    fleet = Fleet({}, loop_settings=lambda: (40.0, 0.3), loop_tick_s=0.01)
    fleet.links = {"radxa-01": StubLink("radxa-01", "stopped")}
    fleet.links["radxa-01"].poll = lambda: False
    fleet.shows = {"radxa-01": {"id": "showA", "cues": [], "duration": 0.6}}
    # The wait is the show's rule (40 s+); the test cannot wait that long, so
    # the fleet's clock runs 100x - the speaker keeps the same clock and only
    # cares about the order of events.
    base = time.perf_counter()
    fleet._clock = lambda: base + (time.perf_counter() - base) * 100.0
    speaker = Speaker(lambda: track, fleet.run_snapshot, clock=fleet._clock,
                      factory=lambda argv: fake, extra_lead_s=0.0, tick_s=0.002,
                      track_check_s=0.01)
    speaker.start()
    fleet.start()
    try:
        deadline = time.monotonic() + 3.0
        while speaker.state != "loaded" and time.monotonic() < deadline:
            time.sleep(0.005)
        assert speaker.state == "loaded", (speaker.state, speaker.error)
        fleet.start_show(lead_s=0.3)
        while fake.state != 2 and time.monotonic() < deadline:
            time.sleep(0.002)
        assert fake.state == 2, "run 1 never played"
        fake.end_of_track()
        while fleet.run["loops"] < 1 and time.monotonic() < deadline:
            time.sleep(0.002)
        assert fleet.run["loops"] == 1
        while fake.state != 2 and time.monotonic() < deadline:
            time.sleep(0.002)
        assert fake.loads == 2 and fake.state == 2, (fake.loads, fake.state, fake.since())
    finally:
        fleet.stop()
        speaker.stop()


def test_a_restarted_conductor_adopts_the_show_the_units_still_hold():
    """H2: fleet.shows is memory only. After a restart the workspace's
    compiled shows are offered; a unit reporting that id with its pictures
    burned is adopted on its first poll, one with something else is told
    to Upload, a member with no show (radxa-05) is simply not in it."""
    clock = Clock()
    compiled = {"radxa-01": {"id": "showA", "cues": [], "duration": 100.0},
                "radxa-02": {"id": "showA", "cues": [], "duration": 100.0},
                "radxa-03": {"id": "showB", "cues": [], "duration": 100.0}}
    fleet = Fleet({}, clock=clock, loop_settings=lambda: (40.0, 2.0))
    fleet.links = {n: StubLink(n, "loaded") for n in ("radxa-01", "radxa-02",
                                                       "radxa-03", "radxa-05")}
    fleet.links["radxa-01"].status["show"].update(id="showA", burn={"state": "burned"})
    fleet.links["radxa-02"].status["show"].update(id="showA", burn={"state": "none"})
    fleet.links["radxa-03"].status["show"].update(id="OLD", burn={"state": "burned"})
    fleet.links["radxa-05"].status = {"show": None}
    fleet.offer_shows(compiled)
    assert fleet.show_duration() == 0.0
    for link in fleet.links.values():
        fleet._supervise(link)
    assert fleet.shows == {"radxa-01": compiled["radxa-01"]}
    assert fleet.show_duration() == 100.0
    notes = "\n".join(fleet.corrections)
    assert "radxa-01: holds this show already (adopted after a restart" in notes
    assert "radxa-02: pictures none - Upload before START" in notes
    assert "radxa-03: another show - Upload before START" in notes
    assert "radxa-05" not in notes
    # Said once, not once per poll.
    for link in fleet.links.values():
        fleet._supervise(link)
    assert "\n".join(fleet.corrections).count("Upload before START") == 2
    # radxa-02 gets its Upload: the unit then reports burned, and is adopted.
    fleet.links["radxa-02"].status["show"]["burn"] = {"state": "burned"}
    fleet._supervise(fleet.links["radxa-02"])
    assert set(fleet.shows) == {"radxa-01", "radxa-02"}
    # The run the units were in is adopted too, and the Loop re-arms on it.
    for name in ("radxa-01", "radxa-02"):
        fleet.links[name].status["show"].update(state="running", t0=clock.now + 5.0 - 200.0,
                                                synced=True)
    snap = fleet.snapshot()
    assert snap["run"] and snap["run"]["adopted"]
    clock.now += 1.0
    fleet._loop_tick()
    assert fleet.loop_state() and fleet.loop_state()["next_in_s"] == 40.0


def test_adoption_survives_a_fleet_restart_end_to_end():
    """H2: the same compiled shows, a Fleet that uploaded and died, a new
    Fleet offered the same compile - START works without an Upload."""
    clock = Clock()
    compiled = {"radxa-01": {"id": "showA", "cues": [], "duration": 100.0}}
    first = Fleet({}, clock=clock)
    first.links = {"radxa-01": StubLink("radxa-01", "loaded")}
    first.upload(compiled)
    assert first.shows == compiled
    # ...the process dies. The unit still reports the show, burned.
    second = Fleet({}, clock=clock)
    second.links = {"radxa-01": StubLink("radxa-01", "loaded")}
    second.links["radxa-01"].status["show"].update(id="showA", burn={"state": "burned"})
    with pytest.raises(ValueError):
        second.start_show(lead_s=1.0)          # nothing known yet: refused
    second.offer_shows(compiled)
    second._supervise(second.links["radxa-01"])
    assert second.shows == compiled
    second.start_show(lead_s=1.0)
    assert second.run["t0"] == clock.now + 1.0


def test_an_import_forgets_what_the_units_hold_so_start_says_upload(tmp_path):
    """H3: right after an import, the units still hold the OLD show; START
    must not run its pictures under the new music."""
    a = _workspace(tmp_path / "a")
    fleet = Fleet({})
    fleet.links = {"radxa-01": StubLink("radxa-01", "loaded")}
    fleet.links["radxa-01"].status["show"].update(id="oldshow", burn={"state": "burned"})
    fleet.shows = {"radxa-01": {"id": "oldshow", "cues": [], "duration": 300.0}}
    fleet.start_at = 30.0
    fleet.offer_shows(dict(fleet.shows))
    server = make_server(tmp_path / "b", port=0, fleet=fleet)
    port = _serve(server)
    b = server.RequestHandlerClass.workspace
    b.unit_marks["upload"] = {"radxa-01": "r-old"}
    try:
        packed = io.BytesIO()
        a.export_tar(packed)
        status, answer = _post(port, "/api/workspace/import", packed.getvalue())
        assert status == 200, answer
        assert fleet.shows == {} and fleet.start_at == 0.0 and fleet._offered == {}
        assert b.unit_marks["upload"] == {"radxa-01": "before-import"}
        status, answer = _post(port, "/api/fleet/start", {"lead_s": 3})
        assert status == 200 and "Upload" in answer["note"]
        assert fleet.run is None
        # The unit's poll does not adopt the old show back either.
        fleet._supervise(fleet.links["radxa-01"])
        assert fleet.shows == {}
    finally:
        server.shutdown()
        server.server_close()


def test_a_stop_inside_the_restart_window_wins():
    """M1: STOP landing between the loop's settings read and start_show()
    must not be overtaken by the restart."""
    clock = Clock()
    calls = {"n": 0}
    fleet_box = {}

    def settings():
        calls["n"] += 1
        if calls["n"] == 2:                    # the read at fire time
            fleet_box["fleet"].stop_show()
        return (40.0, 11.0)

    fleet = _fleet(clock, settings)
    fleet_box["fleet"] = fleet
    fleet.start_show(lead_s=1.0)
    clock.now = fleet.run["t0"] + 100.0
    fleet._loop_tick()
    assert fleet.loop_state() is not None
    clock.now += 40.0
    fleet._loop_tick()
    assert fleet.run is None, "the loop restarted over the operator's STOP"
    assert _posted(fleet, "/show/stop")
    clock.now += 100.0
    fleet._loop_tick()
    assert fleet.run is None
    # ...and a move during the window is refused the same way.
    fleet2 = _fleet(clock, lambda: (40.0, 11.0))
    fleet2.start_show(lead_s=1.0)
    gen = fleet2._run_gen
    fleet2.seek(10.0, lead_s=1.0)
    with pytest.raises(ValueError, match="stopped or moved meanwhile"):
        fleet2.start_show(1.0, 0.0, loop=True, expect_gen=gen)


def test_after_sixty_seconds_the_loop_starts_without_the_unit_that_is_not_ready():
    """M6 (PM decision): one garment never freezes the exhibition."""
    clock = Clock()
    fleet = _fleet(clock, lambda: (40.0, 2.0), loop_retry_s=5.0)
    fleet.start_show(lead_s=1.0)
    clock.now = fleet.run["t0"] + 100.0
    fleet._loop_tick()
    fleet.links["radxa-02"].online = False
    clock.now += 40.0
    fleet._loop_tick()                                  # refused, retried
    assert fleet.run["loops"] == 0
    for _ in range(11):                                 # 55 s of retries
        clock.now += 5.0
        fleet._loop_tick()
    assert fleet.run["loops"] == 0
    clock.now += 5.0                                    # 60 s past the wait
    for link in fleet.links.values():
        link.posted.clear()
    fleet._loop_tick()
    assert fleet.run["loops"] == 1
    runs = _posted(fleet, "/show/run")
    assert [name for name, _ in runs] == ["radxa-01"]
    state = fleet.loop_state()
    assert state["next_in_s"] is None
    assert state["problem"].startswith("started without radxa-02 (not ready)")
    assert sum("started without radxa-02" in l for l in fleet.corrections) == 1
    # The one that was left out is still supervised (it holds the show), and
    # the next end arms the loop for everybody again.
    assert "radxa-02" in fleet.shows
    clock.now = fleet.run["t0"] + 100.0
    fleet._loop_tick()
    assert fleet.loop_state()["next_in_s"] == 40.0
    fleet.links["radxa-02"].online = True
    clock.now += 40.0
    for link in fleet.links.values():
        link.posted.clear()
    fleet._loop_tick()
    assert fleet.run["loops"] == 2
    assert sorted(name for name, _ in _posted(fleet, "/show/run")) == ["radxa-01", "radxa-02"]
    assert fleet.loop_state() is None


def test_a_start_during_the_loop_wait_needs_no_force(tmp_path):
    """M5: a run that has reached its end is not "already running"."""
    clock = Clock()
    fleet = _fleet(clock, lambda: (40.0, 2.0))
    server = make_server(tmp_path, port=0, fleet=fleet)
    port = _serve(server)
    try:
        fleet.start_show(lead_s=1.0)
        status, answer = _post(port, "/api/fleet/start", {"lead_s": 2})
        assert answer["note"] == "The show is already running."
        clock.now = fleet.run["t0"] + 100.0
        fleet._loop_tick()
        assert fleet.loop_state() is not None
        status, answer = _post(port, "/api/fleet/start", {"lead_s": 2})
        assert status == 200 and answer.get("from_s") == 0.0, answer
        assert fleet.run["loops"] == 0 and fleet.run["force"] is False
        assert fleet.loop_state() is None
    finally:
        server.shutdown()
        server.server_close()


def test_a_failed_swap_deletes_only_what_this_import_placed(tmp_path, monkeypatch):
    """M2: the rollback never touches an original the swap had not moved."""
    import conductor.server as srv

    b = _workspace(tmp_path / "b")
    a = _workspace(tmp_path / "a", music=False)
    a.save("Look24_map.csv", MAP)
    packed = io.BytesIO()
    a.export_tar(packed)
    tar_path = tmp_path / "ws.tar"
    tar_path.write_bytes(packed.getvalue())
    before = {p.name for p in b.files.iterdir()}
    show_before = (b.root / "show.json").read_bytes()
    real = srv.os.replace
    state = {"placed": 0}

    def flaky(src, dst):
        # The originals move aside first; then the staged ones move in -
        # fail on the second of those.
        if ".import-" in str(src) and ".import-old" not in str(src):
            state["placed"] += 1
            if state["placed"] == 2:
                raise OSError("disk full")
        return real(src, dst)

    monkeypatch.setattr(srv.os, "replace", flaky)
    with pytest.raises(OSError):
        b.import_tar(tar_path)
    monkeypatch.setattr(srv.os, "replace", real)
    assert {p.name for p in b.files.iterdir()} == before
    assert (b.root / "show.json").read_bytes() == show_before
    assert b.music_info()["name"] == "show.mp3"
    assert sorted(p.name for p in b.root.iterdir()) == ["files", "history.json",
                                                       "music", "show.json"]


def test_import_reads_plain_tars_only_member_by_member_with_a_cap(tmp_path):
    """M4."""
    import gzip

    b = _workspace(tmp_path / "b", music=False)
    plain = _tar_with([("show.json", b'{"duration": 30}')])
    gz = tmp_path / "ws.tar.gz"
    gz.write_bytes(gzip.compress(plain))
    with pytest.raises((ValueError, tarfile.TarError)):
        b.import_tar(gz)
    many = tmp_path / "many.tar"
    many.write_bytes(_tar_with([(f"files/L{i}_map.csv", b"x")
                                for i in range(WORKSPACE_TAR_MEMBERS + 1)]))
    with pytest.raises(ValueError, match="entries in the tar"):
        b.import_tar(many)
    assert not any(p.name.startswith(".import") for p in b.root.iterdir())
    # A bad name after good ones aborts before the swap.
    mixed = tmp_path / "mixed.tar"
    mixed.write_bytes(_tar_with([("show.json", b'{"duration": 30}'),
                                 ("files/A_map.csv", MAP.encode()),
                                 ("../escape", b"x")]))
    with pytest.raises(ValueError):
        b.import_tar(mixed)
    assert b.state()["show"]["duration"] == 120.0


def test_the_passcode_gates_other_hosts_and_never_loopback(tmp_path):
    """M3: every POST and the four material GETs need X-Passcode (or the
    cookie) from a client that is not this host; loopback is free; the
    page's first refusal is the JSON it asks on."""
    ws = _workspace(tmp_path / "ws")
    fleet = Fleet({})
    server = make_server(ws.root, port=0, fleet=fleet, passcode="open-sesame")
    port = _serve(server)
    handler = server.RequestHandlerClass
    try:
        # Loopback (the tests, and radxa-05's own LCD row): free.
        assert _post(port, "/api/loop", {"on": False})[0] == 200
        assert _get(port, "/api/music/file")[0] == 200
        # A client from another host: refused without the passcode.
        handler.local_hosts = ()
        status, answer = _post(port, "/api/loop", {"on": False})
        assert status == 401 and answer == {"error": "passcode required"}
        for path in ("/api/music/file", "/api/show/export", "/api/workspace/export",
                     "/api/simulator"):
            assert _get(port, path)[0] == 401, path
        # ...but the page itself, /api/state and /api/fleet stay open: the
        # page has to load before it can ask.
        for path in ("/", "/api/state", "/api/fleet"):
            assert _get(port, path)[0] == 200, path
        # The header, the cookie, a wrong one.
        assert _post(port, "/api/loop", {"on": False}, {"X-Passcode": "open-sesame"})[0] == 200
        assert _get(port, "/api/music/file", {"Cookie": "a=b; passcode=open-sesame"})[0] == 200
        assert _get(port, "/api/music/file", {"X-Passcode": "nope"})[0] == 401
        # A workspace import needs it too (on top of the token rule).
        packed = io.BytesIO()
        ws.export_tar(packed)
        assert _post(port, "/api/workspace/import", packed.getvalue())[0] == 401
        assert _post(port, "/api/workspace/import", packed.getvalue(),
                     {"X-Passcode": "open-sesame"})[0] == 200
        # A send from another host needs it as well (it is a POST).
        assert _post(port, "/api/workspace/send", {"to": "127.0.0.1:9"})[0] == 401
    finally:
        handler.local_hosts = ("127.0.0.1", "::1", "::ffff:127.0.0.1")
        server.shutdown()
        server.server_close()


def test_fleet_json_can_carry_the_passcode_and_the_hotspot(tmp_path):
    ws = Workspace(tmp_path / "ws")
    (ws.root / "fleet.json").write_text(
        '{"passcode": "pc", "hotspot": "radxa-07", "token": "t"}', encoding="utf-8")
    assert ws.fleet_option("passcode") == "pc"
    assert ws.fleet_option("hotspot", "radxa-05") == "radxa-07"
    assert ws.fleet_option("missing", "d") == "d"
    (ws.root / "fleet.json").write_text('{"passcode": ""}', encoding="utf-8")
    assert ws.fleet_option("passcode") is None


def test_send_probes_the_target_before_pushing_a_workspace_at_it(tmp_path):
    """M3: only a Conductor - one that answers /api/fleet - is sent to."""
    import http.server

    a = _workspace(tmp_path / "a", music=False)
    sender = make_server(a.root, port=0, fleet=Fleet({}))
    sp = _serve(sender)

    class NotAConductor(http.server.BaseHTTPRequestHandler):
        posted = False

        def log_message(self, *args):
            pass

        def do_GET(self):
            body = b'{"boards": []}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            NotAConductor.posted = True
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

    other = http.server.ThreadingHTTPServer(("127.0.0.1", 0), NotAConductor)
    op = other.server_address[1]
    threading.Thread(target=other.serve_forever, daemon=True).start()
    try:
        status, job = _post(sp, "/api/workspace/send", {"to": f"127.0.0.1:{op}"})
        deadline = time.monotonic() + 10
        while job["state"] not in ("done", "failed") and time.monotonic() < deadline:
            time.sleep(0.05)
            job = json.loads(_get(sp, f"/api/workspace/send?job={job['job']}")[1])
        assert job["state"] == "failed" and "is not a Conductor" in job["error"], job
        assert NotAConductor.posted is False
    finally:
        other.shutdown()
        other.server_close()
        sender.shutdown()
        sender.server_close()


def test_reachable_urls_and_local_ipv4s_never_resolve_a_name(monkeypatch):
    """L2."""
    import conductor.server as srv

    def boom(*args, **kwargs):
        raise AssertionError("DNS was asked")

    monkeypatch.setattr(srv.socket, "gethostbyname_ex", boom)
    monkeypatch.setattr(srv.socket, "getaddrinfo", boom)
    monkeypatch.setattr(srv.socket, "gethostbyname", boom)
    urls = srv.reachable_urls("0.0.0.0", 8765)
    assert urls[0] == "http://127.0.0.1:8765"
    assert all(not u.startswith("http://127.") for u in urls[1:])
    assert all(isinstance(a, str) and a.count(".") == 3 for a in srv.local_ipv4s())


# ------------------------------------------------------------ review round 2 (53b9b6b+92cef21)

def test_an_upload_withdraws_the_startup_offer():
    """MED-1: a garment taken out of the timeline still holds the old
    burned show; after an Upload it must not be adopted from it and sent
    a START."""
    clock = Clock()
    old = {"radxa-01": {"id": "showA", "cues": [], "duration": 100.0},
           "radxa-02": {"id": "showA", "cues": [], "duration": 100.0},
           "radxa-03": {"id": "showA", "cues": [], "duration": 100.0}}
    fleet = Fleet({}, clock=clock)
    fleet.links = {n: StubLink(n, "loaded") for n in old}
    for link in fleet.links.values():
        link.status["show"].update(id="showA", burn={"state": "burned"})
    fleet.offer_shows(old)
    # The new timeline has no radxa-03; the Upload writes 01 and 02.
    new = {"radxa-01": {"id": "showB", "cues": [], "duration": 90.0},
           "radxa-02": {"id": "showB", "cues": [], "duration": 90.0}}
    fleet.upload(new)
    assert fleet._offered == {}
    for link in fleet.links.values():
        link.status["show"].update(id="showB", burn={"state": "burned"})
    fleet.links["radxa-03"].status["show"].update(id="showA")
    for link in fleet.links.values():
        fleet._supervise(link)
    assert set(fleet.shows) == {"radxa-01", "radxa-02"}
    fleet.start_show(lead_s=1.0)
    assert sorted(name for name, _ in _posted(fleet, "/show/run")) == ["radxa-01", "radxa-02"]
    # Save on units drops the offer for units the compile no longer reaches.
    fleet2 = Fleet({}, clock=clock)
    fleet2.links = {n: StubLink(n, "loaded") for n in old}
    fleet2.offer_shows(old)
    fleet2.write_demo("DEMO", False, new)
    assert set(fleet2._offered) == {"radxa-01", "radxa-02"}


def test_an_offered_unit_not_adopted_yet_is_named_not_left_out():
    """MED-2: while the offer is open the gate, the grace and 'started
    without X' name a unit that has not come back; burn 'failed' adopts."""
    clock = Clock()
    compiled = {"radxa-01": {"id": "showA", "cues": [], "duration": 100.0},
                "radxa-02": {"id": "showA", "cues": [], "duration": 100.0}}
    fleet = Fleet({}, clock=clock, loop_settings=lambda: (40.0, 2.0), loop_retry_s=5.0)
    fleet.links = {n: StubLink(n, "loaded") for n in compiled}
    fleet.links["radxa-01"].status["show"].update(id="showA", burn={"state": "failed",
                                                                     "failed": [[3, 1]], "total": 4})
    fleet.links["radxa-02"].online = False          # still off after the restart
    fleet.offer_shows(compiled)
    fleet._supervise(fleet.links["radxa-01"])
    assert set(fleet.shows) == {"radxa-01"}          # 'failed' is adopted...
    assert sorted(fleet._targets()) == ["radxa-01", "radxa-02"]
    with pytest.raises(ValueError) as refused:        # ...and the gate decides
        fleet.start_show(lead_s=1.0)
    assert "radxa-01: 1 of 4 pictures not written" in str(refused.value)
    assert "radxa-02: not answering" in str(refused.value)
    fleet.start_show(lead_s=1.0, force=True, skip={"radxa-02"})
    # A run, and the Loop's restart: radxa-02 is named, never silent.
    clock.now = fleet.run["t0"] + 100.0
    fleet._loop_tick()
    clock.now += 40.0
    fleet._loop_tick()
    assert "radxa-02: not answering" in fleet.loop_state()["problem"]
    # A command aimed at it while the offer is open: it reports a show
    # (the old one), so it is posted `show: None` and runs what it holds -
    # main's behaviour; only a unit reporting NO show is refused by name
    # (test_a_mid_show_restart_with_a_changed_compile_still_resumes).
    fleet.links["radxa-02"].online = True
    results = fleet._send_run(["radxa-01", "radxa-02"])
    assert results["radxa-01"]["ok"] and results["radxa-02"]["ok"]
    assert [body["show"] for name, body in _posted(fleet, "/show/run") if name == "radxa-02"][-1] is None


def test_the_speakers_own_p_excuses_only_its_first_answer(tmp_path):
    """MED-3: the track ends in the same instant as the show-end pause -
    "@P 1" (EOF), then "@P 2" / "@P 1" from our P against the ended file.
    The first line answers our P; the last one is the EOF."""
    fake = FakeMpg123()
    st = Stage(tmp_path, fake=fake, extra_lead_s=0.0)
    st.speaker.start()
    try:
        st.wait_state("loaded")
        clock = st.speaker._clock
        st.duration = 0.3
        st.run = {"t0": clock() - 0.05, "state": "running", "held_at": None}
        st.wait_state("playing")
        # The show ends: the speaker's P (want PAUSED) meets the burst.
        fake.burst_on_next_p = True
        st.wait_state("ended", timeout=2.0)
        assert st.wait_for(lambda: fake.loads == 2, 3.0), fake.since()
        assert st.speaker.error is None
        # The next run has music from the top.
        st.run = {"t0": clock() + 0.3, "state": "running", "held_at": None}
        st.wait_state("armed")
        st.wait_state("playing", timeout=2.0)
        assert fake.state == 2 and fake.loads == 2
    finally:
        st.speaker.stop()


def test_a_unit_skipped_for_an_unchanged_reason_is_skipped_at_once():
    """MED-4: one 60 s grace per fault, not one per run; a new fault gets
    its own grace; a START press forgets."""
    clock = Clock()
    fleet = _fleet(clock, lambda: (40.0, 2.0), loop_retry_s=5.0)
    fleet.start_show(lead_s=1.0)
    fleet.links["radxa-02"].online = False
    clock.now = fleet.run["t0"] + 100.0
    fleet._loop_tick()
    clock.now += 40.0
    fleet._loop_tick()                                # refused: the grace starts
    clock.now += 60.0
    fleet._loop_tick()                                # the first grace ran out
    assert fleet.run["loops"] == 1 and fleet._loop_skipped == {
        "radxa-02": "radxa-02: not answering"}
    assert "joins this run as soon as it answers" in fleet.loop_state()["problem"]
    # Run 2 ends; radxa-02 is still off for the same reason: no second wait.
    clock.now = fleet.run["t0"] + 100.0
    fleet._loop_tick()
    clock.now += 40.0
    for link in fleet.links.values():
        link.posted.clear()
    fleet._loop_tick()
    assert fleet.run["loops"] == 2
    assert [name for name, _ in _posted(fleet, "/show/run")] == ["radxa-01"]
    # A different fault on it: the grace applies again.
    fleet.links["radxa-02"].online = True
    fleet.links["radxa-02"].status["show"]["burn"] = {"state": "none"}
    clock.now = fleet.run["t0"] + 100.0
    fleet._loop_tick()
    clock.now += 40.0
    fleet._loop_tick()
    assert fleet.run["loops"] == 2 and "Upload again" in fleet.loop_state()["problem"]
    clock.now += 60.0
    fleet._loop_tick()
    assert fleet.run["loops"] == 3
    # A START press starts with a clean slate.
    fleet.start_show(lead_s=1.0, skip={"radxa-02"})
    assert fleet._loop_skipped == {}


def test_a_conductor_open_to_other_hosts_refuses_without_a_real_passcode(tmp_path, capsys):
    """MED-6."""
    import conductor.server as srv

    ws = Workspace(tmp_path / "ws")
    monkey_free = {"already": lambda port: False}
    real = srv.already_serving
    srv.already_serving = monkey_free["already"]
    try:
        assert srv.serve(ws.root, port=0, host="0.0.0.0") == 2
        out = capsys.readouterr().out
        assert "refusing to serve on 0.0.0.0" in out and "no passcode" in out
        assert "fleet.json" in out and "--passcode" in out
        (ws.root / "fleet.json").write_text('{"passcode": "CHANGE-ME-2026"}', encoding="utf-8")
        assert srv.serve(ws.root, port=0, host="0.0.0.0") == 2
        out = capsys.readouterr().out
        assert "example value" in out and "CHANGE-ME-2026" in out
        assert srv.serve(ws.root, port=0, host="0.0.0.0",
                         passcode="CHANGE-ME-2026") == 2
        # Loopback needs none (the show PC as ever).
        assert srv.passcode_problem("127.0.0.1", None) is None
        assert srv.passcode_problem("localhost", "CHANGE-ME-2026") is None
        assert srv.passcode_problem("0.0.0.0", "my-own") is None
        assert srv.passcode_problem("10.42.0.1", "") is not None
    finally:
        srv.already_serving = real


def test_a_cli_passcode_that_differs_from_fleet_json_is_warned_about(tmp_path, capsys, monkeypatch):
    import conductor.server as srv

    ws = Workspace(tmp_path / "ws")
    (ws.root / "fleet.json").write_text('{"passcode": "stored"}', encoding="utf-8")
    seen = {}

    class Once(srv._Server):
        def serve_forever(self, poll_interval=0.5):
            seen["passcode"] = self.RequestHandlerClass.passcode

    monkeypatch.setattr(srv, "_Server", Once)
    monkeypatch.setattr(srv, "already_serving", lambda port: False)
    assert srv.serve(ws.root, port=0, host="0.0.0.0", passcode="typed") == 0
    out = capsys.readouterr().out
    assert "warning: --passcode and" in out and "differ" in out
    assert seen["passcode"] == "typed"
    assert srv.serve(ws.root, port=0, host="0.0.0.0") == 0
    assert "warning" not in capsys.readouterr().out and seen["passcode"] == "stored"


def test_the_service_file_carries_no_passcode():
    from pathlib import Path as _P
    service = (_P(__file__).resolve().parents[1] / "radxa" / "epaper-conductor.service").read_text(encoding="utf-8")
    assert "--passcode" not in service.split("ExecStart=")[1].split("\n")[0]
    assert "--host 0.0.0.0" in service and "--speaker-output pulse" in service
    assert "alsa" in service, "the USB-only fallback must stay documented"


def test_the_passcode_cookie_counts_for_the_music_file_only(tmp_path):
    ws = _workspace(tmp_path / "ws")
    server = make_server(ws.root, port=0, fleet=Fleet({}), passcode="pc")
    port = _serve(server)
    handler = server.RequestHandlerClass
    try:
        handler.local_hosts = ()
        cookie = {"Cookie": "passcode=pc"}
        assert _get(port, "/api/music/file", cookie)[0] == 200
        assert _get(port, "/api/show/export", cookie)[0] == 401
        assert _post(port, "/api/loop", {"on": False}, cookie)[0] == 401
        assert _post(port, "/api/loop", {"on": False}, {"X-Passcode": "pc"})[0] == 200
    finally:
        handler.local_hosts = ("127.0.0.1", "::1", "::ffff:127.0.0.1")
        server.shutdown()
        server.server_close()


def test_a_truncated_import_leaves_no_spool_behind(tmp_path):
    import http.client

    server = make_server(tmp_path / "b", port=0, fleet=Fleet({}))
    port = _serve(server)
    root = server.RequestHandlerClass.workspace.root
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.putrequest("POST", "/api/workspace/import")
        conn.putheader("Content-Type", "application/x-tar")
        conn.putheader("Content-Length", "100000")
        conn.endheaders()
        conn.send(b"x" * 1000)                       # ...and stops
        conn.close()
        # The server thread may not even have made the spool yet: give the
        # request time to be handled, then require the spool to be gone.
        time.sleep(0.5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and any(
                p.name.startswith(".import-") for p in root.iterdir()):
            time.sleep(0.05)
        assert not any(p.name.startswith(".import-") for p in root.iterdir())
    finally:
        server.shutdown()
        server.server_close()


# ------------------------------------------------------------ the host's loudness + final gate

class FakeAudio:
    """pactl / busctl as the speaker runs them: a default sink that the
    test can switch, a bluez transport whose fdN changes on reconnect."""

    def __init__(self, sink="alsa_output.usb-Foo.analog-stereo", transport_fd=None):
        self.sink = sink
        self.transport_fd = transport_fd            # None: not connected
        self.calls: "list[list[str]]" = []
        self.bluez_volume = None
        self.pulse_volumes: "dict[str, str]" = {}
        self.fail_busctl = False

    def __call__(self, argv, timeout=5.0):
        self.calls.append(list(argv))
        if argv[:2] == ["pactl", "info"]:
            return 0, f"Server Name: pulseaudio\nDefault Sink: {self.sink}\nDefault Source: x\n"
        if argv[:2] == ["pactl", "set-sink-volume"]:
            self.pulse_volumes[argv[2]] = argv[3]
            return 0, ""
        if argv[:3] == ["busctl", "--system", "tree"]:
            if self.fail_busctl:
                return 1, "Failed to introspect"
            lines = ["/org/bluez/hci0", "/org/bluez/hci0/dev_AC_BF_71_FA_8F_AB",
                     "/org/bluez/hci0/dev_AC_BF_71_FA_8F_AB/player0"]
            if self.transport_fd is not None:
                lines += [f"/org/bluez/hci0/dev_AC_BF_71_FA_8F_AB/sep1",
                          f"/org/bluez/hci0/dev_AC_BF_71_FA_8F_AB/sep1/fd{self.transport_fd}"]
            return 0, "\n".join(lines) + "\n"
        if argv[:3] == ["busctl", "--system", "set-property"]:
            if argv[4].endswith(f"/fd{self.transport_fd}"):
                self.bluez_volume = (argv[4], int(argv[-1]))
                return 0, ""
            return 1, "Unknown object"
        return 127, ""

    def bluez_sets(self):
        return [c for c in self.calls if c[:3] == ["busctl", "--system", "set-property"]]


def _volume_stage(tmp_path, audio, volume=70, saved=None, **kw):
    st = Stage(tmp_path, extra_lead_s=0.0)
    st.speaker = Speaker(lambda: st.track, lambda: (st.run, st.duration),
                         factory=st._factory, tick_s=0.005, track_check_s=0.01,
                         retry_s=0.2, extra_lead_s=0.0, runner=audio, volume=volume,
                         save_volume=(saved.append if saved is not None else None),
                         volume_check_s=kw.get("volume_check_s", 0.05))
    return st


def test_the_volume_goes_to_the_pulse_sink_for_a_usb_speaker(tmp_path):
    audio = FakeAudio()
    saved = []
    st = _volume_stage(tmp_path, audio, volume=70, saved=saved)
    st.speaker.start()
    try:
        assert st.wait_for(lambda: st.speaker.status()["applied"] == "pulse")
        assert audio.pulse_volumes == {"@DEFAULT_SINK@": "70%"}
        assert audio.bluez_sets() == []
        status = st.speaker.status()
        assert status["volume"] == 70 and status["volume_error"] is None
        # A change: persisted, applied, answered.
        answer = st.speaker.set_volume(55)
        assert answer == {"volume": 55, "applied": "pulse", "error": None}
        assert saved == [55] and audio.pulse_volumes["@DEFAULT_SINK@"] == "55%"
        # Clamped and rounded.
        assert st.speaker.set_volume(140)["volume"] == 100
        assert st.speaker.set_volume(-3)["volume"] == 0
        assert st.speaker.set_volume(42.6)["volume"] == 43
        # Not re-sent every check while nothing changed.
        n = len([c for c in audio.calls if c[:2] == ["pactl", "set-sink-volume"]])
        time.sleep(0.3)
        assert len([c for c in audio.calls if c[:2] == ["pactl", "set-sink-volume"]]) == n
    finally:
        st.speaker.stop()


def test_the_volume_goes_to_the_bluez_transport_and_follows_a_reconnect(tmp_path):
    audio = FakeAudio(sink="bluez_sink.AC_BF_71_FA_8F_AB.a2dp_sink", transport_fd=None)
    st = _volume_stage(tmp_path, audio, volume=90)
    st.speaker.start()
    try:
        # The speaker is not connected yet: said, retried, nothing applied.
        assert st.wait_for(lambda: "not found" in (st.speaker.status()["volume_error"] or ""))
        assert st.speaker.status()["applied"] is None
        # It connects: the AVRCP absolute volume, and the pulse sink at 100 %.
        audio.transport_fd = 3
        assert st.wait_for(lambda: st.speaker.status()["applied"] == "bluez")
        assert audio.bluez_volume == ("/org/bluez/hci0/dev_AC_BF_71_FA_8F_AB/sep1/fd3", 114)
        assert audio.pulse_volumes == {"bluez_sink.AC_BF_71_FA_8F_AB.a2dp_sink": "100%"}
        assert st.speaker.status()["volume_error"] is None
        # A reconnect: a new fdN, found on the next check, applied again.
        audio.transport_fd = 7
        assert st.wait_for(lambda: audio.bluez_volume and audio.bluez_volume[0].endswith("/fd7"))
        assert audio.bluez_volume[1] == 114
        # A change while connected: 0-100 -> 0-127.
        answer = st.speaker.set_volume(100)
        assert answer["applied"] == "bluez" and audio.bluez_volume[1] == 127
        assert st.speaker.set_volume(0)["volume"] == 0 and audio.bluez_volume[1] == 0
        # busctl failing is a status, retried.
        audio.fail_busctl = True
        st.speaker.set_volume(50)
        assert st.wait_for(lambda: "busctl tree" in (st.speaker.status()["volume_error"] or ""))
        audio.fail_busctl = False
        assert st.wait_for(lambda: st.speaker.status()["volume_error"] is None
                           and audio.bluez_volume[1] == 64)
    finally:
        st.speaker.stop()


def test_no_pactl_at_all_is_a_status(tmp_path):
    def missing(argv, timeout=5.0):
        return 127, ""
    st = _volume_stage(tmp_path, missing)
    st.speaker.start()
    try:
        assert st.wait_for(lambda: "pactl info" in (st.speaker.status()["volume_error"] or ""))
        assert st.speaker.status()["applied"] is None
        assert st.speaker.status()["error"] is None, "the volume must not fail the speaker"
    finally:
        st.speaker.stop()


def test_post_speaker_volume_and_the_fleet_report(tmp_path, monkeypatch):
    import conductor.server as srv

    ws = _workspace(tmp_path / "ws")
    (ws.root / "fleet.json").write_text('{"speaker_volume": 60}', encoding="utf-8")
    audio = FakeAudio()
    fake = FakeMpg123()
    made = {}

    class Once(srv._Server):
        def serve_forever(self, poll_interval=0.5):
            handler = self.RequestHandlerClass
            made["port"] = self.server_address[1]
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and handler.speaker.status()["applied"] is None:
                time.sleep(0.01)
            threading.Thread(target=super().serve_forever, daemon=True).start()
            made["fleet"] = json.loads(_get(made["port"], "/api/fleet")[1])
            made["set"] = _post(made["port"], "/api/speaker/volume", {"volume": 35})
            made["delta"] = _post(made["port"], "/api/speaker/volume", {"delta": -10})
            made["bad"] = [_post(made["port"], "/api/speaker/volume", b)[0]
                           for b in ({}, {"volume": 101}, {"volume": "x"}, {"delta": "y"})]
            made["file"] = json.loads((ws.root / "fleet.json").read_text(encoding="utf-8"))
            self.shutdown()

    monkeypatch.setattr(srv, "_Server", Once)
    monkeypatch.setattr(srv, "already_serving", lambda port: False)
    assert srv.serve(ws.root, port=0, speaker=True, speaker_lead_ms=0,
                     speaker_factory=lambda argv: fake, speaker_runner=audio) == 0
    speaker = made["fleet"]["speaker"]
    assert speaker["volume"] == 60 and speaker["applied"] == "pulse"
    assert speaker["volume_error"] is None
    assert made["set"] == (200, {"volume": 35, "applied": "pulse", "error": None})
    assert made["delta"] == (200, {"volume": 25, "applied": "pulse", "error": None})
    assert made["bad"] == [400, 400, 400, 400]
    assert made["file"] == {"speaker_volume": 25}
    assert audio.pulse_volumes["@DEFAULT_SINK@"] == "25%"


def test_volume_without_a_speaker_is_a_400(tmp_path):
    server = make_server(tmp_path, port=0, fleet=Fleet({}))
    port = _serve(server)
    try:
        status, answer = _post(port, "/api/speaker/volume", {"volume": 50})
        assert status == 400 and "no speaker" in answer["error"]
    finally:
        server.shutdown()
        server.server_close()


def test_the_page_has_the_speaker_volume_control():
    assert 'id="spk-vol"' in PAGE_TEXT and 'id="spk-up"' in PAGE_TEXT and 'id="spk-down"' in PAGE_TEXT
    assert 'api("/api/speaker/volume", body)' in PAGE_TEXT
    assert '{ delta: e.target.id === "spk-up" ? 5 : -5 }' in PAGE_TEXT
    assert '{ volume: Number(e.target.value) }' in PAGE_TEXT
    # Shown only with a speaker Conductor.
    assert 'hostvol.style.display = sp ? "" : "none"' in PAGE_TEXT


# ---- final gate: adoption is opt-in and keeps the gates honest ----

def test_the_pcs_default_start_never_offers_shows(tmp_path, monkeypatch):
    import conductor.server as srv

    ws = _workspace(tmp_path / "ws", music=False)
    seen = {}

    class Once(srv._Server):
        def serve_forever(self, poll_interval=0.5):
            seen["offered"] = dict(self.RequestHandlerClass.fleet._offered)

    monkeypatch.setattr(srv, "_Server", Once)
    monkeypatch.setattr(srv, "already_serving", lambda port: False)
    assert srv.serve(ws.root, port=0) == 0
    assert seen["offered"] == {}, "the show PC adopted shows without --adopt"
    assert srv.serve(ws.root, port=0, adopt=True) == 0
    assert set(seen["offered"]) == {"radxa-01"}
    (ws.root / "fleet.json").write_text('{"adopt": true}', encoding="utf-8")
    assert srv.serve(ws.root, port=0) == 0
    assert set(seen["offered"]) == {"radxa-01"}
    # ...and the CLI flag reaches serve().
    calls = {}
    monkeypatch.setattr(srv, "serve", lambda *a, **kw: calls.update(kw) or 0)
    assert cli.main(["serve", "--adopt"]) == 0
    assert calls["adopt"] is True
    assert cli.main(["serve"]) == 0
    assert calls["adopt"] is False


def test_an_edit_after_an_adopted_restart_is_refused_with_upload_again(tmp_path):
    """MED-A: the adopted unit is marked as holding the startup compile's
    revision, so an edit + START says Upload again instead of running the
    old pictures."""
    import conductor.server as srv

    ws = _workspace(tmp_path / "ws", music=False)
    compiled, _ = ws.compile_show()
    fleet = Fleet({})
    fleet.links = {"radxa-01": StubLink("radxa-01", "loaded")}
    fleet.links["radxa-01"].status["show"].update(id=compiled["radxa-01"]["id"],
                                                  burn={"state": "burned"})
    server = make_server(ws.root, port=0, fleet=fleet)
    port = _serve(server)
    ws = server.RequestHandlerClass.workspace
    try:
        srv.offer_startup_shows(fleet, ws)
        fleet._supervise(fleet.links["radxa-01"])
        assert set(fleet.shows) == {"radxa-01"}
        assert ws.unit_marks["upload"] == {"radxa-01": ws.revision()}
        assert ws.written_state()["uploaded"] == ws.revision()
        # Untouched: START runs.
        status, answer = _post(port, "/api/fleet/start", {"lead_s": 1})
        assert status == 200 and answer.get("from_s") == 0.0, answer
        fleet.stop_show()
        # Edited after the restart: refused, as on a fresh Upload.
        ws.set_timeline(90, [{"id": "p", "item": "Look23", "at": 0,
                              "design": "Look23_color_scarlet_grid.csv"}])
        status, answer = _post(port, "/api/fleet/start", {"lead_s": 1})
        assert status == 400 and "Upload again" in answer["error"], answer
        assert fleet.run is None
    finally:
        server.shutdown()
        server.server_close()


def test_a_mid_show_restart_with_a_changed_compile_still_resumes(tmp_path):
    """MED-B: the units run show X, the restarted conductor compiles Y and
    adopts nothing - RESUME / NEXT still post show:None (main's behaviour)
    to the units that report a show, never 'has not taken this show yet'."""
    clock = Clock()
    fleet = Fleet({}, clock=clock)
    fleet.links = {"radxa-01": StubLink("radxa-01", "running"),
                   "radxa-02": StubLink("radxa-02", "running"),
                   "radxa-05": StubLink("radxa-05", "loaded")}
    fleet.links["radxa-05"].status = {"show": None}     # control only
    for name in ("radxa-01", "radxa-02"):
        fleet.links[name].status["show"].update(id="X", t0=clock.now + 5.0 - 30.0,
                                                synced=True, burn={"state": "burned"})
    fleet.offer_shows({"radxa-01": {"id": "Y", "cues": [], "duration": 100.0},
                       "radxa-02": {"id": "Y", "cues": [], "duration": 100.0}})
    for link in fleet.links.values():
        fleet._supervise(link)
    assert fleet.shows == {}                            # Y is not what they hold
    snap = fleet.snapshot()
    assert snap["run"] and snap["run"]["adopted"]
    assert sorted(fleet._targets()) == ["radxa-01", "radxa-02"]
    fleet.hold()
    assert sorted(n for n, p in _posted(fleet, "/show/hold")) == ["radxa-01", "radxa-02"]
    results = fleet.resume()
    assert results == {"radxa-01": {"ok": True}, "radxa-02": {"ok": True}}, results
    runs = _posted(fleet, "/show/run")
    assert len(runs) == 2 and all(body["show"] is None for _, body in runs)
    # A unit reporting NO show at all is still refused by name (it holds
    # nothing to run), never posted a run naming nothing.
    fleet.links["radxa-03"] = StubLink("radxa-03", "loaded")
    fleet.links["radxa-03"].status = {"show": {}}
    fleet._offered["radxa-03"] = {"id": "Y", "cues": [], "duration": 100.0}
    results = fleet._send_run(["radxa-03"])
    assert results == {"radxa-03": {"ok": False, "error": "has not taken this show yet"}}


def test_the_started_without_hint_goes_once_the_unit_has_joined():
    clock = Clock()
    fleet = _fleet(clock, lambda: (40.0, 2.0), loop_retry_s=5.0)
    fleet.start_show(lead_s=1.0)
    fleet.links["radxa-02"].online = False
    clock.now = fleet.run["t0"] + 100.0
    fleet._loop_tick()
    clock.now += 40.0
    fleet._loop_tick()
    clock.now += 60.0
    fleet._loop_tick()
    assert fleet.loop_state()["problem"].startswith("started without radxa-02")
    # Back, and running this show (supervision's "started late"): the hint goes.
    fleet.links["radxa-02"].online = True
    fleet.links["radxa-02"].status["show"].update(state="running", id="showA")
    assert fleet.loop_state() is None


def test_the_exhibition_service_files_do_not_gate_the_boot():
    from pathlib import Path as _P
    radxa = _P(__file__).resolve().parents[1] / "radxa"
    net = (radxa / "epaper-exhibition-net.service").read_text(encoding="utf-8")
    assert "Type=simple" in net
    assert not any(line.startswith("Type=oneshot") for line in net.splitlines())
    assert not any(line.startswith("RemainAfterExit") for line in net.splitlines())
    assert "TimeoutStartSec=infinity" in net
    assert "Before=" not in net
    conductor = (radxa / "epaper-conductor.service").read_text(encoding="utf-8")
    assert "epaper-exhibition-net" not in conductor.split("After=")[1].split("\n")[0]
    assert "Environment=XDG_RUNTIME_DIR=/run/user/1000" in conductor
    assert "Environment=PULSE_SERVER=unix:/run/user/1000/pulse/native" in conductor
    exec_line = conductor.split("ExecStart=")[1].split("\n")[0]
    assert "--adopt" in exec_line and "--speaker-output pulse" in exec_line
    script = (radxa / "exhibition-net.sh").read_text(encoding="utf-8")
    assert "grep -q" not in script and "$SECONDS" in script
    assert '$4 == "activated"' in script


# ------------------------------------------------------------ last round (8495976)

def test_a_volume_change_during_an_apply_is_never_lost(tmp_path):
    """1a: a set_volume landing while a tick is inside pactl must not be
    marked done by that tick - the next tick applies the new value."""
    audio = FakeAudio()
    slow = {"s": 0.0}
    real = audio.__call__

    def slow_runner(argv, timeout=5.0):
        if argv[:2] == ["pactl", "set-sink-volume"] and slow["s"]:
            time.sleep(slow["s"])
        return real(argv, timeout)

    st = _volume_stage(tmp_path, slow_runner, volume=70)
    st.speaker.start()
    try:
        assert st.wait_for(lambda: st.speaker.status()["applied"] == "pulse")
        slow["s"] = 0.3
        first = {}
        t = threading.Thread(target=lambda: first.update(st.speaker.set_volume(80)))
        t.start()
        time.sleep(0.1)                               # the 80 is inside pactl now
        second = st.speaker.set_volume(90)
        t.join(3)
        assert second["volume"] == 90 and second["applied"] == "pulse"
        assert st.wait_for(lambda: audio.pulse_volumes["@DEFAULT_SINK@"] == "90%", 2.0), \
            audio.pulse_volumes
        assert st.speaker.status()["volume"] == 90
    finally:
        st.speaker.stop()


def test_a_failing_pactl_is_retried_on_the_interval_not_in_a_hot_loop(tmp_path):
    """1b: pulse down (every boot until user@1000 is up) must cost one
    pactl per VOLUME_CHECK_S, not twenty a second."""
    calls = []

    def down(argv, timeout=5.0):
        calls.append(list(argv))
        return 1, "Connection refused"

    st = _volume_stage(tmp_path, down, volume_check_s=0.1)
    st.speaker.start()
    try:
        assert st.wait_for(lambda: st.speaker.status()["volume_error"] is not None)
        n0 = len(calls)
        time.sleep(0.55)
        n = len(calls) - n0
        assert 3 <= n <= 8, f"{n} pactl calls in 0.55 s at a 0.1 s interval"
        # A change while it is down is tried at once, then on the interval.
        st.speaker.set_volume(40)
        assert st.speaker.status()["volume"] == 40
    finally:
        st.speaker.stop()


def test_the_volume_runs_on_its_own_thread_not_the_music_thread(tmp_path):
    """1c: a pactl that hangs must not delay the 0:00 unpause."""
    gate = threading.Event()

    def hanging(argv, timeout=5.0):
        if argv[:2] == ["pactl", "info"]:
            gate.wait(2.0)                            # "pulse is slow"
        return 1, ""

    st = _volume_stage(tmp_path, hanging)
    st.speaker.start()
    try:
        st.wait_state("loaded")
        clock = st.speaker._clock
        st.run = {"t0": clock() + 0.2, "state": "running", "held_at": None}
        st.wait_state("armed")
        st.wait_state("playing", timeout=1.0)         # well inside pactl's hang
        assert st.speaker._volume_thread is not None and st.speaker._volume_thread.is_alive()
    finally:
        gate.set()
        st.speaker.stop()


def test_the_adopted_run_widening_stops_once_this_conductor_has_uploaded():
    """3: restart mid-show, force Upload of a smaller timeline, HOLD/RESUME
    must not post show:None to a unit outside that timeline."""
    clock = Clock()
    fleet = Fleet({}, clock=clock)
    fleet.links = {n: StubLink(n, "running") for n in ("radxa-01", "radxa-02", "radxa-03")}
    for link in fleet.links.values():
        link.status["show"].update(id="X", t0=clock.now + 5.0 - 30.0, synced=True,
                                   burn={"state": "burned"})
    snap = fleet.snapshot()
    assert snap["run"]["adopted"]
    assert sorted(fleet._targets()) == ["radxa-01", "radxa-02", "radxa-03"]
    # The operator uploads a timeline for 01 and 02 only, under the run.
    fleet.upload({"radxa-01": {"id": "Y", "cues": [], "duration": 100.0},
                  "radxa-02": {"id": "Y", "cues": [], "duration": 100.0}}, force=True)
    assert sorted(fleet._targets()) == ["radxa-01", "radxa-02"]
    for link in fleet.links.values():
        link.posted.clear()
    fleet.hold()
    fleet.resume()
    assert not [n for n, p in fleet.links["radxa-03"].posted]
    assert sorted(n for n, _ in _posted(fleet, "/show/run")) == ["radxa-01", "radxa-02"]


def test_an_imported_loop_is_kept_only_by_the_exhibitions_conductor(tmp_path):
    """4: an export from radxa-05 must never make the PC restart shows."""
    a = _workspace(tmp_path / "a", music=False)
    a.set_loop(90)
    show_file = a.export_show()
    packed = io.BytesIO()
    a.export_tar(packed)
    # The PC (no --adopt): the Loop is turned off, said in the reply and the corrections.
    fleet = Fleet({})
    server = make_server(tmp_path / "pc", port=0, fleet=fleet)
    port = _serve(server)
    pc = server.RequestHandlerClass.workspace
    try:
        status, answer = _post(port, "/api/show/import", show_file)
        assert status == 200 and pc.loop_wait() is None
        assert any("Loop on - turned off here" in w for w in answer["warnings"])
        assert any("Loop on - turned off here" in c for c in fleet.corrections)
        status, answer = _post(port, "/api/workspace/import", packed.getvalue())
        assert status == 200 and pc.loop_wait() is None
        assert any("Loop on - turned off here" in p for p in answer["problems"])
    finally:
        server.shutdown()
        server.server_close()
    # The exhibition's Conductor (--adopt): kept.
    server = make_server(tmp_path / "ex", port=0, fleet=Fleet({}), adopt=True)
    port = _serve(server)
    ex = server.RequestHandlerClass.workspace
    try:
        status, answer = _post(port, "/api/show/import", show_file)
        assert status == 200 and ex.loop_wait() == 90.0
        assert not any("turned off" in w for w in answer["warnings"])
        ex.set_loop(None)
        status, answer = _post(port, "/api/workspace/import", packed.getvalue())
        assert status == 200 and ex.loop_wait() == 90.0
    finally:
        server.shutdown()
        server.server_close()


def test_set_fleet_option_keeps_the_mode_and_never_overwrites_a_broken_file(tmp_path):
    """5."""
    import os as _os
    import stat as _stat

    ws = Workspace(tmp_path / "ws")
    path = ws.root / "fleet.json"
    path.write_text('{"passcode": "pc", "speaker_volume": 70}', encoding="utf-8")
    _os.chmod(path, 0o600)
    ws.set_fleet_option("speaker_volume", 40)
    assert json.loads(path.read_text(encoding="utf-8")) == {"passcode": "pc", "speaker_volume": 40}
    if _os.name != "nt":
        assert _stat.S_IMODE(_os.stat(path).st_mode) == 0o600
    path.write_text('{"passcode": "pc", "speaker_volume": 4', encoding="utf-8")   # truncated
    with pytest.raises(RuntimeError, match="does not parse"):
        ws.set_fleet_option("speaker_volume", 50)
    assert path.read_text(encoding="utf-8") == '{"passcode": "pc", "speaker_volume": 4'
    path.write_text('[1, 2]', encoding="utf-8")
    with pytest.raises(RuntimeError, match="not an object"):
        ws.set_fleet_option("speaker_volume", 50)
    path.unlink()
    ws.set_fleet_option("speaker_volume", 55)
    assert json.loads(path.read_text(encoding="utf-8")) == {"speaker_volume": 55}


def test_volume_rejects_nan_and_inf_and_a_broken_fleet_json_is_a_500(tmp_path, monkeypatch):
    import conductor.server as srv

    ws = _workspace(tmp_path / "ws")
    (ws.root / "fleet.json").write_text('{"speaker_volume": 1e9}', encoding="utf-8")
    audio = FakeAudio()
    made = {}

    class Once(srv._Server):
        def serve_forever(self, poll_interval=0.5):
            handler = self.RequestHandlerClass
            port = self.server_address[1]
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and handler.speaker.status()["applied"] is None:
                time.sleep(0.01)
            threading.Thread(target=super().serve_forever, daemon=True).start()
            made["start_volume"] = handler.speaker.status()["volume"]
            made["bad"] = [_post(port, "/api/speaker/volume", b)
                           for b in ({"volume": float("nan")}, {"volume": float("inf")},
                                     {"delta": float("nan")}, {"delta": float("-inf")})]
            (ws.root / "fleet.json").write_text('{"speaker_volume": 4', encoding="utf-8")
            made["broken"] = _post(port, "/api/speaker/volume", {"volume": 30})
            made["file"] = (ws.root / "fleet.json").read_text(encoding="utf-8")
            self.shutdown()

    monkeypatch.setattr(srv, "_Server", Once)
    monkeypatch.setattr(srv, "already_serving", lambda port: False)
    assert srv.serve(ws.root, port=0, speaker=True, speaker_lead_ms=0,
                     speaker_factory=lambda argv: FakeMpg123(), speaker_runner=audio) == 0
    assert made["start_volume"] == 100, "an absurd stored volume must clamp, not crash"
    assert [s for s, _ in made["bad"]] == [400, 400, 400, 400]
    assert made["broken"][0] == 500 and "does not parse" in made["broken"][1]["error"]
    assert made["file"] == '{"speaker_volume": 4', "a broken fleet.json was overwritten"


def test_the_conductor_service_waits_for_the_user_manager():
    from pathlib import Path as _P
    conductor = (_P(__file__).resolve().parents[1] / "radxa" / "epaper-conductor.service").read_text(encoding="utf-8")
    after = conductor.split("\nAfter=")[1].split("\n")[0]
    wants = conductor.split("\nWants=")[1].split("\n")[0]
    assert "user@1000.service" in after and "user@1000.service" in wants


# ------------------------------------------------------------ the Bluetooth speaker's connection

MAC = "AC:BF:71:FA:8F:AB"
BLUEZ_SINK = "bluez_sink.AC_BF_71_FA_8F_AB.a2dp_sink"
USB_SINK = "alsa_output.usb-Foo.analog-stereo"


class FakeBluetooth:
    """bluetoothctl + pactl as the connection thread runs them (the volume
    calls go on to a FakeAudio): a Bose that the test connects, drops,
    hides from BlueZ, or puts in pairing mode."""

    def __init__(self, paired=True, connected=True, known=True,
                 name="Bose Flex SoundLink"):
        self.audio = FakeAudio(sink=USB_SINK, transport_fd=3 if connected else None)
        self.paired, self.connected, self.known, self.name = paired, connected, known, name
        self.trusted = paired
        self.pairing_mode = False          # the classic address shows in a scan
        self.connect_answer = "Connection successful"
        self.sink_lag = 0                  # listings after a connect without the sink
        self.sink_index = "1"              # the bluez sink's pulse index (new one per rebirth)
        self.no_sink = False               # connected, but pulse never makes the sink
        self.sink_inputs: "dict[str, str]" = {}   # mpg123's streams: input -> sink index
        self.calls: "list[list[str]]" = []
        self.threads: "list[tuple[str, str]]" = []
        self.raising = False               # bluetoothctl raises (a broken host)
        self.gate: "threading.Event | None" = None   # connect waits on it

    def _ansi(self, text):
        return f"\x1b[0;94m[bluetooth]\x1b[0m# {text}"

    def __call__(self, argv, timeout=5.0):
        self.calls.append(list(argv))
        self.threads.append((argv[0] + " " + " ".join(argv[1:3]), threading.current_thread().name))
        if argv[0] == "bluetoothctl":
            if self.raising:
                raise OSError("bluetoothctl exploded")
            if argv[1] == "paired-devices":
                return 0, (f"Device {MAC} {self.name}\n" if self.paired else "")
            if argv[1] == "info":
                if argv[2] != MAC or not self.known:
                    return 1, f"Device {argv[2]} not available\n"
                yes_no = lambda b: "yes" if b else "no"
                return 0, (f"Device {MAC} (public)\n\tName: {self.name}\n\tAlias: {self.name}\n"
                           f"\tClass: 0x00240418\n\tPaired: {yes_no(self.paired)}\n"
                           f"\tTrusted: {yes_no(self.trusted)}\n\tBlocked: no\n"
                           f"\tConnected: {yes_no(self.connected)}\n"
                           "\tUUID: Audio Sink                (0000110b-0000-1000-8000-00805f9b34fb)\n"
                           "\tUUID: A/V Remote Control        (0000110e-0000-1000-8000-00805f9b34fb)\n")
            if argv[1] == "connect":
                if self.gate is not None:
                    self.gate.wait(3.0)
                if self.connect_answer == "Connection successful" and self.known:
                    self.connected = True
                    self.audio.transport_fd = 3
                    return 0, f"Attempting to connect to {MAC}\n{self._ansi('Connection successful')}\n"
                return 1, f"Attempting to connect to {MAC}\n{self._ansi(self.connect_answer)}\n"
            if argv[1] == "disconnect":
                self.connected = False
                self.audio.transport_fd = None
                return 0, f"Attempting to disconnect from {MAC}\nSuccessful disconnected\n"
            if argv[1] == "devices":
                return 0, (f"Device {MAC} {self.name}\n" if self.pairing_mode or self.known else
                           "Device 11:22:33:44:55:66 LE-Bose Flex SoundLink\n")
            return 1, f"Invalid command {argv[1]}"
        if argv[:4] == ["pactl", "list", "short", "sinks"]:
            lines = [f"0\t{USB_SINK}\tmodule-alsa-card.c\ts16le 2ch 48000Hz\tSUSPENDED"]
            if self.connected and self.sink_lag <= 0 and not self.no_sink:
                lines.append(f"{self.sink_index}\t{BLUEZ_SINK}\tmodule-bluez5-device.c\t"
                             "s16le 2ch 44100Hz\tRUNNING")
            elif self.connected:
                self.sink_lag -= 1
            return 0, "\n".join(lines) + "\n"
        if argv[:4] == ["pactl", "list", "short", "sink-inputs"]:
            return 0, "".join(f"{i}\t{s}\t40\tprotocol-native.c\ts16le 2ch 44100Hz\n"
                              for i, s in self.sink_inputs.items())
        if argv[:2] == ["pactl", "set-default-sink"]:
            self.audio.sink = argv[2]
            return 0, ""
        if argv[:2] == ["pactl", "move-sink-input"]:
            self.sink_inputs[argv[2]] = self.sink_index if argv[3] == BLUEZ_SINK else "0"
            return 0, ""
        return self.audio(argv, timeout)

    def bluetoothctl(self, what):
        return [c for c in self.calls if c[:2] == ["bluetoothctl", what]]

    def pactl(self, what):
        return [c for c in self.calls if c[:2] == ["pactl", what]]


class FakeBtSession:
    """An interactive bluetoothctl on two pipes: the commands the re-pair
    flow writes, the lines a Bose answers with (ANSI prompts included)."""

    def __init__(self, bt: FakeBluetooth, pair_ok=True, connect_ok=True, mute=()):
        self.bt, self.pair_ok, self.connect_ok = bt, pair_ok, connect_ok
        self.mute = mute                   # commands bluetoothctl never answers
        self.commands: "list[str]" = []
        self._out = queue.Queue()
        self._buffer = b""
        self.returncode = None
        self.stdin = self.stdout = self
        self.scanning = False

    def write(self, data):
        self._buffer += data
        while b"\n" in self._buffer:
            line, _, self._buffer = self._buffer.partition(b"\n")
            self._handle(line.decode())

    def flush(self):
        pass

    def readline(self):
        return self._out.get()

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            raise TimeoutError("still running")      # a quit that was never honoured
        return self.returncode

    def terminate(self):
        self.returncode = -15
        self._out.put(b"")

    def close(self):                       # the pipes (both are this object)
        self.closed = True
        self._out.put(b"")

    def _say(self, text):
        self.said = getattr(self, "said", []) + [text]
        self._out.put(f"\x1b[0;94m[bluetooth]\x1b[0m# {text}\n".encode())

    def _handle(self, command):
        self.commands.append(command)
        bt = self.bt
        if command.split()[0] in self.mute:
            # bluetoothctl says nothing on this pipe - but BlueZ may still
            # have done it (a silent, real pairing): `info` tells.
            if command == f"pair {MAC}" and self.pair_ok and bt.pairing_mode:
                bt.known = bt.paired = True
            return
        if command == f"remove {MAC}":
            bt.known = bt.paired = bt.trusted = bt.connected = False
            bt.audio.transport_fd = None
            self._say(f"[DEL] Device {MAC} {bt.name}")
            self._say("Device has been removed")
        elif command == "scan on":
            self.scanning = True
            self._say("Discovery started")
            self._say("[CHG] Controller 00:11:22:33:44:55 Discovering: yes")
            self._say("[NEW] Device 11:22:33:44:55:66 LE-Bose Flex SoundLink")
            if bt.pairing_mode:
                self._say(f"[NEW] Device {MAC} {bt.name}")
        elif command == f"pair {MAC}":
            self._say(f"Attempting to pair with {MAC}")
            if self.pair_ok and bt.pairing_mode:
                bt.known = bt.paired = True
                self._say(f"[CHG] Device {MAC} Paired: yes")
                self._say("Pairing successful")
            else:
                self._say("Failed to pair: org.bluez.Error.AuthenticationFailed")
        elif command == f"trust {MAC}":
            bt.trusted = True
            self._say(f"Changing {MAC} trust succeeded")
        elif command == f"connect {MAC}":
            self._say(f"Attempting to connect to {MAC}")
            if self.connect_ok:
                bt.connected = True
                bt.audio.transport_fd = 5
                self._say("Connection successful")
            else:
                self._say("Failed to connect: org.bluez.Error.Failed")
        elif command == "scan off":
            self.scanning = False
            self._say("Discovery stopped")
        elif command == "quit":
            self.returncode = 0
            self._out.put(b"")


def _bt_speaker(bt, clock=None, run=None, session=None, **kw):
    """A speaker with nothing to play (no track) and a Bluetooth speaker
    to watch, on fast intervals unless the test says otherwise."""
    sessions = []

    def session_factory(argv):
        assert argv == ["bluetoothctl"]
        made = FakeBtSession(bt, **(session or {}))
        sessions.append(made)
        return made

    options = dict(output="pulse", runner=bt, session_factory=session_factory,
                   connection_check_s=0.05, connection_check_run_s=0.02,
                   reconnect_first_s=0.1, reconnect_every_s=0.2, reconnect_backoff_s=1.0,
                   pair_scan_s=1.0, pair_poll_s=0.05, pair_wait_s=1.0,
                   pair_connect_wait_s=1.0, volume_check_s=0.05, tick_s=0.005)
    options.update(kw)
    speaker = Speaker(lambda: None, run or (lambda: (None, 0.0)),
                      clock=clock or pc_clock, **options)
    speaker._test_sessions = sessions
    return speaker


def _wait(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def test_the_paired_audio_sink_is_found_and_published_as_connected():
    bt = FakeBluetooth()
    bt.sink_inputs = {"35": "0"}                  # mpg123's stream, on the USB sink
    sp = _bt_speaker(bt)
    sp.start()
    try:
        assert _wait(lambda: sp.status()["connection"] == "connected")
        status = sp.status()
        assert status["bluetooth"] is True
        device = status["device"]
        assert device["mac"] == MAC and device["name"] == "Bose Flex SoundLink"
        assert device["paired"] and device["trusted"] and device["connected"]
        assert device["sink_present"] is True and device["last_error"] is None
        assert abs(device["last_connected_at"] - time.time()) < 5
        assert status["reconnect"] == {"attempts": 0, "next_in_s": None, "last_error": None}
        assert status["pairing"] is None
        assert any("found among the paired devices" in line for line in sp.log)
        # Found once: paired-devices is not asked again while the MAC is known.
        n = len(bt.bluetoothctl("paired-devices"))
        time.sleep(0.2)
        assert len(bt.bluetoothctl("paired-devices")) == n == 1
        # The sound was routed to it: default sink, and the stream moved.
        assert _wait(lambda: bt.audio.sink == BLUEZ_SINK and bt.sink_inputs == {"35": "1"})
        assert bt.pactl("set-default-sink") == [["pactl", "set-default-sink", BLUEZ_SINK]]
        assert bt.pactl("move-sink-input") == [["pactl", "move-sink-input", "35", BLUEZ_SINK]]
        # ...once, not on every check.
        time.sleep(0.2)
        assert len(bt.pactl("move-sink-input")) == 1
        # And the volume thread followed to the bluez transport.
        assert _wait(lambda: sp.status()["applied"] == "bluez")
    finally:
        sp.stop()


def test_a_configured_mac_wins_and_an_unknown_one_is_no_device():
    bt = FakeBluetooth(known=False, paired=False)
    sp = _bt_speaker(bt, speaker_mac="ac-bf-71-fa-8f-ab")     # cleaned to bluetoothctl's form
    sp.start()
    try:
        assert _wait(lambda: sp.status()["device"] is not None)
        status = sp.status()
        assert status["connection"] == "no_device"
        assert status["device"]["mac"] == MAC and status["device"]["paired"] is False
        assert "not paired: Re-pair" in status["device"]["last_error"]
        assert bt.bluetoothctl("paired-devices") == [], "a configured MAC is never discovered"
        # Nothing is tried against a device BlueZ does not know.
        time.sleep(0.3)
        assert bt.bluetoothctl("connect") == []
        assert status["reconnect"]["next_in_s"] is None
        # Someone pairs it by hand: known on the next check.
        bt.known = bt.paired = bt.trusted = True
        bt.connected = False
        assert _wait(lambda: sp.status()["connection"] == "disconnected")
    finally:
        sp.stop()


def test_nothing_paired_is_no_device_and_looked_for_again():
    bt = FakeBluetooth(paired=False, connected=False, known=False)
    sp = _bt_speaker(bt)
    sp.start()
    try:
        assert _wait(lambda: len(bt.bluetoothctl("paired-devices")) >= 2)
        status = sp.status()
        assert status["connection"] == "no_device" and status["device"] is None
        # A wired speaker on pulse with nothing paired: not a Bluetooth
        # Conductor at all as far as the page and the LCD are concerned.
        assert status["bluetooth"] is False
        bt.known = bt.paired = bt.trusted = bt.connected = True
        assert _wait(lambda: sp.status()["connection"] == "connected")
        assert sp.status()["bluetooth"] is True
    finally:
        sp.stop()


def test_a_drop_is_reconnected_after_five_then_thirty_seconds_with_the_backoff():
    """The timing, on a clock the test moves: 5 s after the drop, then
    every 30 s, 120 s once three connects in a row were refused; on
    success the stream is moved back and `reconnected after N s` said."""
    clock = Clock(1000.0)
    bt = FakeBluetooth()
    bt.sink_inputs = {"35": "1"}                  # mpg123 playing into the Bose
    sp = _bt_speaker(bt, clock=clock, connection_check_s=10.0, connection_check_run_s=2.0,
                     reconnect_first_s=5.0, reconnect_every_s=30.0, reconnect_backoff_s=120.0)
    sp.start()
    try:
        assert _wait(lambda: sp.status()["connection"] == "connected")
        # The Bose goes away; pulse rescues the stream onto the USB sink.
        bt.connected = False
        bt.audio.transport_fd = None
        bt.sink_inputs = {"35": "0"}
        time.sleep(0.3)
        assert sp.status()["connection"] == "connected", "checked every 10 s, not sooner"
        clock.now += 10
        assert _wait(lambda: sp.status()["connection"] == "disconnected")
        status = sp.status()
        assert status["device"]["connected"] is False and status["device"]["sink_present"] is False
        assert status["reconnect"] == {"attempts": 0, "next_in_s": 5.0, "last_error": None}
        assert bt.bluetoothctl("connect") == []
        # 5 s: the first attempt - refused.
        bt.connect_answer = "Failed to connect: org.bluez.Error.Failed"
        clock.now += 5
        assert _wait(lambda: len(bt.bluetoothctl("connect")) == 1)
        assert _wait(lambda: sp.status()["connection"] == "disconnected")
        rc = sp.status()["reconnect"]
        assert rc["attempts"] == 1 and rc["next_in_s"] == 30.0
        assert rc["last_error"] == "Failed to connect: org.bluez.Error.Failed"
        assert bt.bluetoothctl("connect")[0] == ["bluetoothctl", "connect", MAC]
        # 30 s, 30 s: the second and third - the backoff kicks in.
        clock.now += 30
        assert _wait(lambda: len(bt.bluetoothctl("connect")) == 2)
        assert _wait(lambda: sp.status()["reconnect"]["next_in_s"] == 30.0)
        clock.now += 30
        assert _wait(lambda: len(bt.bluetoothctl("connect")) == 3)
        assert _wait(lambda: sp.status()["reconnect"]["next_in_s"] == 120.0)
        time.sleep(0.2)
        assert len(bt.bluetoothctl("connect")) == 3
        # Three failures, two log lines: the same error at the same interval
        # is not said again; the change to 120 s is.
        failed = [l for l in sp.log if "connect AC:BF:71:FA:8F:AB failed" in l]
        assert len(failed) == 2 and failed[0].endswith("next try in 30 s") \
            and failed[1].endswith("next try in 120 s"), failed
        # 120 s: the speaker is back - connected, routed, said.
        bt.connect_answer = "Connection successful"
        bt.sink_lag = 1                           # pulse makes the sink a moment later
        clock.now += 120
        assert _wait(lambda: len(bt.bluetoothctl("connect")) == 4)
        assert _wait(lambda: sp.status()["connection"] == "connected")
        status = sp.status()
        assert status["device"]["sink_present"] is False, "the sink is not there yet"
        assert status["reconnect"]["attempts"] == 0 and status["reconnect"]["next_in_s"] is None
        # 5 + 30 + 30 + 120, counted from the check that saw the drop.
        assert any("reconnected after 185 s" in line for line in sp.log), sp.log
        assert bt.sink_inputs == {"35": "0"}, "nothing to route to yet"
        # Connected without a sink is looked at again soon (the run interval).
        clock.now += 2
        assert _wait(lambda: sp.status()["device"]["sink_present"] is True)
        assert _wait(lambda: bt.sink_inputs == {"35": "1"}), bt.sink_inputs
        assert bt.audio.sink == BLUEZ_SINK
        assert any("sound routed to" in line and "1 stream(s) moved" in line for line in sp.log)
    finally:
        sp.stop()


def test_the_check_runs_every_two_seconds_during_a_run():
    clock = Clock(1000.0)
    bt = FakeBluetooth()
    run = {"r": None}
    sp = _bt_speaker(bt, clock=clock, run=lambda: (run["r"], 60.0),
                     connection_check_s=10.0, connection_check_run_s=2.0)
    sp.start()
    try:
        assert _wait(lambda: sp.status()["connection"] == "connected")
        n = len(bt.bluetoothctl("info"))          # discovery's + the first check's
        clock.now += 2.5
        time.sleep(0.3)
        assert len(bt.bluetoothctl("info")) == n, "idle: every 10 s"
        run["r"] = {"t0": clock.now, "state": "running", "held_at": None}
        clock.now += 8                            # the 10 s check, which reads the run
        assert _wait(lambda: len(bt.bluetoothctl("info")) == n + 1)
        clock.now += 2.5
        assert _wait(lambda: len(bt.bluetoothctl("info")) == n + 2), "a run: every 2 s"
    finally:
        sp.stop()


def test_connect_is_a_request_the_connection_thread_serves_never_the_caller():
    """The HTTP side only queues: request_connect answers at once while
    bluetoothctl connect is stuck, and every tool runs on the speaker's
    own threads - none on the caller's."""
    bt = FakeBluetooth(connected=False)
    bt.gate = threading.Event()
    sp = _bt_speaker(bt, reconnect_first_s=100.0)             # no automatic attempt here
    sp.start()
    try:
        assert _wait(lambda: sp.status()["connection"] == "disconnected")
        assert sp.status()["reconnect"]["next_in_s"] > 90
        t0 = time.monotonic()
        status, answer = sp.request_connect()
        assert time.monotonic() - t0 < 0.2
        assert (status, answer) == (200, {"ok": True, "connection": "connecting", "error": None})
        assert _wait(lambda: len(bt.bluetoothctl("connect")) == 1)
        time.sleep(0.1)
        assert sp.status()["connection"] == "connecting", "stuck inside bluetoothctl connect"
        # A second Connect while one is on its way is the same answer, not a second attempt.
        assert sp.request_connect() == (200, {"ok": True, "connection": "connecting", "error": None})
        bt.gate.set()
        assert _wait(lambda: sp.status()["connection"] == "connected")
        assert len(bt.bluetoothctl("connect")) == 1
        assert sp.status()["reconnect"]["attempts"] == 0, "connected: the count starts over"
        mine = threading.current_thread().name
        assert all(name != mine for _, name in bt.threads), bt.threads
        conn = sp._conn_thread.name
        assert all(name == conn for tool, name in bt.threads if tool.startswith("bluetoothctl")), bt.threads
        assert all(name == conn for tool, name in bt.threads
                   if tool.startswith("pactl list") or tool.startswith("pactl move")
                   or tool.startswith("pactl set-default-sink")), bt.threads
        # Connected: a Connect is still honoured (one more bluetoothctl connect).
        assert sp.request_connect()[0] == 200
        assert _wait(lambda: len(bt.bluetoothctl("connect")) == 2)
    finally:
        bt.gate.set()
        sp.stop()


def test_re_pair_runs_the_whole_cure_in_one_bluetoothctl_session():
    bt = FakeBluetooth(connected=False)
    bt.connect_answer = "Failed to connect: org.bluez.Error.Failed"
    sp = _bt_speaker(bt, reconnect_first_s=100.0, pairing_shown_s=0.4)
    sp.start()
    try:
        assert _wait(lambda: sp.status()["connection"] == "disconnected")
        status, answer = sp.request_pair()
        assert status == 200 and answer == {"ok": True, "connection": "pairing", "error": None}
        assert _wait(lambda: sp._test_sessions and "scan on" in sp._test_sessions[0].commands)
        pairing = sp.status()["pairing"]
        assert pairing["phase"] == "scanning" and "pairing mode" in pairing["note"]
        assert abs(pairing["started_at"] - time.time()) < 5
        # While it scans: a second Re-pair and a Connect are refused, 409.
        assert sp.request_pair() == (409, {"ok": False, "connection": "pairing",
                                           "error": "re-pairing is already in progress"})
        status, answer = sp.request_connect()
        assert status == 409 and "in progress" in answer["error"]
        assert bt.bluetoothctl("connect") == [], "no automatic connect during a re-pair"
        # The operator puts the Bose in pairing mode: found by the poll.
        time.sleep(0.15)
        bt.pairing_mode = True
        assert _wait(lambda: sp.status()["pairing"]["phase"] == "done")
        assert _wait(lambda: sp.status()["connection"] == "connected")
        session = sp._test_sessions[0]
        assert session.commands == [f"remove {MAC}", "scan on", f"pair {MAC}", f"trust {MAC}",
                                    f"connect {MAC}", "scan off", "quit"]
        assert len(sp._test_sessions) == 1
        device = sp.status()["device"]
        assert device["paired"] and device["trusted"] and device["connected"]
        phases = [line.split("re-pair ")[1].split(" - ")[0] for line in sp.log if "re-pair " in line]
        assert phases == [f"{MAC}: scanning", f"{MAC}: pairing", f"{MAC}: connecting", f"{MAC}: done"]
        assert sp.status()["reconnect"]["attempts"] == 0
        # The finished re-pair stays on show for PAIRING_SHOWN_S, then it is old news.
        assert sp.status()["pairing"]["phase"] == "done"
        assert _wait(lambda: sp.status()["pairing"] is None, 2.0)
        assert sp.status()["connection"] == "connected"
    finally:
        sp.stop()


def test_re_pair_is_refused_during_a_run_unless_forced_and_needs_a_mac():
    bt = FakeBluetooth(paired=False, connected=False, known=False)
    run = {"r": {"t0": 0.0, "state": "running", "held_at": None}}
    sp = _bt_speaker(bt, run=lambda: (run["r"], 60.0))
    sp.start()
    try:
        assert _wait(lambda: sp.status()["connection"] == "no_device")
        status, answer = sp.request_pair()
        assert status == 409 and answer["ok"] is False
        assert answer["error"] == ('show running - pairing drops the audio; '
                                   'send {"force": true} to pair anyway')
        run["r"] = None
        status, answer = sp.request_pair()
        assert status == 400 and "give its MAC" in answer["error"]
        assert sp.request_pair("not-a-mac")[0] == 400
        assert sp.request_connect()[0] == 400
        # With the MAC and force, during a run: accepted, the Bose appears.
        run["r"] = {"t0": 0.0, "state": "running", "held_at": None}
        bt.pairing_mode = True
        status, answer = sp.request_pair(MAC.lower(), force=True)
        assert status == 200 and answer["connection"] == "pairing"
        assert _wait(lambda: sp.status()["connection"] == "connected")
        assert sp.status()["device"]["mac"] == MAC
    finally:
        sp.stop()


def test_a_re_pair_that_finds_nothing_fails_and_the_automatic_connect_goes_on():
    bt = FakeBluetooth(connected=False)
    sp = _bt_speaker(bt, pair_scan_s=0.3, reconnect_first_s=100.0, reconnect_every_s=0.2)
    sp.start()
    try:
        assert _wait(lambda: sp.status()["connection"] == "disconnected")
        assert sp.request_pair()[0] == 200
        assert _wait(lambda: sp.status()["pairing"]["phase"] == "failed", 3.0)
        pairing = sp.status()["pairing"]
        assert "did not appear" in pairing["note"] and "pairing mode" in pairing["note"]
        assert _wait(lambda: sp._test_sessions[0].commands[-1] == "quit")
        assert "pair" not in " ".join(sp._test_sessions[0].commands[2:])
        # The old pairing is gone (remove ran): honestly no_device - but the
        # address stays on show, so the next Re-pair needs no retyping, and
        # nothing is tried against a device BlueZ no longer knows.
        assert _wait(lambda: sp.status()["connection"] == "no_device")
        device = sp.status()["device"]
        assert device["mac"] == MAC and device["paired"] is False
        assert "Re-pair" in device["last_error"]
        time.sleep(0.3)
        assert bt.bluetoothctl("connect") == []
        assert sp.request_pair()[0] == 200, "the MAC is remembered"
        bt.pairing_mode = True
        assert _wait(lambda: sp.status()["connection"] == "connected")
        assert len(sp._test_sessions) == 2
    finally:
        sp.stop()


def test_a_pair_that_is_refused_by_the_speaker_is_a_failed_phase():
    bt = FakeBluetooth(connected=False)
    bt.pairing_mode = True
    sp = _bt_speaker(bt, reconnect_first_s=100.0, session={"pair_ok": False})
    sp.start()
    try:
        assert _wait(lambda: sp.status()["connection"] == "disconnected")
        sp.request_pair()
        assert _wait(lambda: sp.status()["pairing"]["phase"] == "failed")
        assert "AuthenticationFailed" in sp.status()["pairing"]["note"]
        assert sp.status()["reconnect"]["last_error"].startswith("pair: ")
    finally:
        sp.stop()


def test_a_silent_bluetoothctl_cannot_hang_the_re_pair():
    """Every phase is bounded: a `pair` (or `connect`) that never answers
    is a failed phase after its wait, with the wait in the note, and the
    session is quit. The `quit` of a mute session is a close that falls
    back to terminate()."""
    bt = FakeBluetooth(connected=False)
    bt.pairing_mode = True
    sp = _bt_speaker(bt, reconnect_first_s=100.0, pair_wait_s=0.3,
                     session={"mute": ("pair", "quit"), "pair_ok": False})
    sp.start()
    try:
        assert _wait(lambda: sp.status()["connection"] == "disconnected")
        t0 = time.monotonic()
        sp.request_pair()
        assert _wait(lambda: (sp.status()["pairing"] or {}).get("phase") == "failed", 3.0)
        assert time.monotonic() - t0 < 2.5
        assert sp.status()["pairing"]["note"] == "pair: no answer in 0 s"
        assert _wait(lambda: sp._test_sessions[0].commands[-1] == "quit"
                     and getattr(sp._test_sessions[0], "closed", False))
        assert sp._test_sessions[0].returncode == -15, "a quit never honoured is a terminate"
        assert sp._conn_thread.is_alive()
    finally:
        sp.stop()


def test_a_silent_pair_that_bluez_did_anyway_goes_on_by_its_info():
    """LOW: bluetoothctl said nothing on the pipe, but `info` shows
    Paired: yes - the flow trusts and connects instead of failing."""
    bt = FakeBluetooth(connected=False)
    bt.pairing_mode = True
    saved = []
    sp = _bt_speaker(bt, reconnect_first_s=100.0, pair_wait_s=0.3,
                     session={"mute": ("pair",)}, save_mac=saved.append)
    sp.start()
    try:
        assert _wait(lambda: sp.status()["connection"] == "disconnected")
        sp.request_pair()
        assert _wait(lambda: (sp.status()["pairing"] or {}).get("phase") == "done", 3.0)
        assert _wait(lambda: sp.status()["connection"] == "connected")
        assert f"trust {MAC}" in sp._test_sessions[0].commands
        # The address that worked is saved to fleet.json (it was discovered,
        # not configured): a lost pairing is redone from the page as it is.
        assert saved == [MAC]
        # A second re-pair with the same (now configured) MAC saves nothing new.
        bt.connected = False
        sp.request_pair()
        assert _wait(lambda: len(sp._test_sessions) == 2 and sp.status()["connection"] == "connected")
        assert saved == [MAC]
    finally:
        sp.stop()


def test_a_device_bluez_still_lists_after_remove_is_not_paired_until_it_advertises():
    """LOW: the remove's result is honoured - a cached device makes the
    `devices` poll meaningless, so only a [NEW]/[CHG] line from the scan
    counts, and the note says so."""
    bt = FakeBluetooth(connected=False)
    sp = _bt_speaker(bt, reconnect_first_s=100.0, pair_scan_s=0.6,
                     session={"mute": ("remove",)})
    sp.start()
    try:
        assert _wait(lambda: sp.status()["connection"] == "disconnected")
        sp.request_pair()
        # (the muted remove is given its 3 s bound first)
        assert _wait(lambda: "still lists" in (sp.status()["pairing"] or {}).get("note", ""), 6.0)
        # `devices` lists it (cached) - but it is not advertising: no pair.
        assert _wait(lambda: (sp.status()["pairing"] or {}).get("phase") == "failed", 3.0)
        assert "did not appear" in sp.status()["pairing"]["note"]
        assert f"pair {MAC}" not in sp._test_sessions[0].commands
        # Now it advertises (the [NEW] line in the session): paired.
        bt.pairing_mode = True
        assert sp.request_pair()[0] == 200
        assert _wait(lambda: (sp.status()["pairing"] or {}).get("phase") == "done", 6.0), \
            (sp.status()["pairing"], [(s.commands, getattr(s, "said", None)) for s in sp._test_sessions])
    finally:
        sp.stop()


def test_bluetoothctl_dying_mid_scan_is_a_failed_phase_not_a_hot_loop():
    bt = FakeBluetooth(connected=False)
    sp = _bt_speaker(bt, reconnect_first_s=100.0, pair_scan_s=2.0, pair_poll_s=0.2)
    sp.start()
    try:
        assert _wait(lambda: sp.status()["connection"] == "disconnected")
        sp.request_pair()
        assert _wait(lambda: sp._test_sessions and "scan on" in sp._test_sessions[0].commands)
        n = len(bt.bluetoothctl("devices"))
        sp._test_sessions[0].terminate()               # bluetoothctl dies
        assert _wait(lambda: (sp.status()["pairing"] or {}).get("phase") == "failed", 3.0)
        assert "exited during the scan" in sp.status()["pairing"]["note"]
        assert len(bt.bluetoothctl("devices")) - n <= 2, "no hot loop on a dead pipe"
    finally:
        sp.stop()


def test_any_exception_inside_a_re_pair_still_ends_it():
    bt = FakeBluetooth(connected=False)

    def exploding_factory(argv):
        raise ZeroDivisionError("boom")

    sp = _bt_speaker(bt, reconnect_first_s=100.0, pairing_shown_s=0.3,
                     session_factory=exploding_factory)
    sp.start()
    try:
        assert _wait(lambda: sp.status()["connection"] == "disconnected")
        sp.request_pair()
        assert _wait(lambda: (sp.status()["pairing"] or {}).get("phase") == "failed")
        assert sp.status()["pairing"]["note"] == "boom"
        assert _wait(lambda: sp.status()["pairing"] is None, 2.0), "ended: it clears"
        assert sp._conn_thread.is_alive()
    finally:
        sp.stop()


def test_a_quick_drop_bluez_healed_itself_is_still_routed_by_the_new_sink_index():
    """HIGH: the Bose drops and BlueZ reconnects it between two checks -
    `Connected: yes` never flickered, but the recreated sink has a new
    pulse index and mpg123's stream sits on the fallback sink. The index
    is what the routing remembers; and once per connected check the
    streams are verified and a strayed one moved."""
    bt = FakeBluetooth()
    bt.sink_inputs = {"35": "0"}
    sp = _bt_speaker(bt)
    sp.start()
    try:
        assert _wait(lambda: bt.sink_inputs == {"35": "1"} and bt.audio.sink == BLUEZ_SINK)
        n_default, n_moves = len(bt.pactl("set-default-sink")), len(bt.pactl("move-sink-input"))
        # The quick drop: new sink index, the stream rescued to USB, the
        # default sink fallen back - and Connected: yes throughout.
        bt.sink_index = "7"
        bt.sink_inputs = {"35": "0"}
        bt.audio.sink = USB_SINK
        assert _wait(lambda: bt.sink_inputs == {"35": "7"}), bt.sink_inputs
        assert bt.audio.sink == BLUEZ_SINK
        assert len(bt.pactl("set-default-sink")) == n_default + 1
        assert len(bt.pactl("move-sink-input")) == n_moves + 1
        assert sp.status()["connection"] == "connected"
        # A stream that strays with the SAME sink: moved by the per-check
        # verification, without touching the default sink.
        n_default = len(bt.pactl("set-default-sink"))
        bt.sink_inputs["36"] = "0"
        assert _wait(lambda: bt.sink_inputs == {"35": "7", "36": "7"}), bt.sink_inputs
        assert len(bt.pactl("set-default-sink")) == n_default
        # Nothing to move: nothing moved, nothing said, but still verified.
        n_moves = len(bt.pactl("move-sink-input"))
        n_lists = len([c for c in bt.calls if c[:4] == ["pactl", "list", "short", "sink-inputs"]])
        time.sleep(0.25)
        assert len(bt.pactl("move-sink-input")) == n_moves
        assert len([c for c in bt.calls if c[:4] == ["pactl", "list", "short", "sink-inputs"]]) > n_lists
        assert len([l for l in sp.log if "sound routed" in l]) == 3
    finally:
        sp.stop()


def test_connected_without_a_sink_for_twenty_seconds_is_no_sink_and_the_link_is_cycled():
    """MED-1: bluetoothctl says connected, PulseAudio never makes the A2DP
    sink - after NO_SINK_S it is as good as lost: `no_sink`, said once,
    one disconnect + connect, then the usual schedule."""
    clock = Clock(1000.0)
    bt = FakeBluetooth()
    bt.no_sink = True
    sp = _bt_speaker(bt, clock=clock, connection_check_s=10.0, connection_check_run_s=2.0,
                     reconnect_every_s=30.0, no_sink_s=20.0)
    sp.start()
    try:
        assert _wait(lambda: sp.status()["connection"] == "connected")
        assert sp.status()["device"]["sink_present"] is False
        for _ in range(9):                            # 18 s: still "connected" (rechecked every 2 s)
            clock.now += 2
            time.sleep(0.05)
        assert sp.status()["connection"] == "connected"
        assert bt.bluetoothctl("disconnect") == []
        clock.now += 2                                # 20 s
        assert _wait(lambda: sp.status()["connection"] == "no_sink")
        assert sp.status()["device"]["last_error"] == "connected, no PulseAudio sink"
        assert _wait(lambda: len(bt.bluetoothctl("disconnect")) == 1 and len(bt.bluetoothctl("connect")) == 1)
        assert bt.calls.index(["bluetoothctl", "disconnect", MAC]) < bt.calls.index(["bluetoothctl", "connect", MAC])
        assert _wait(lambda: sp.status()["connection"] == "no_sink")
        assert sp.status()["reconnect"]["next_in_s"] == 30.0, sp.status()["reconnect"]
        said = [l for l in sp.log if "no sink for it after 20 s" in l]
        assert len(said) == 1
        # Not cycled again before the interval.
        clock.now += 10
        time.sleep(0.3)
        assert len(bt.bluetoothctl("disconnect")) == 1
        clock.now += 20
        assert _wait(lambda: len(bt.bluetoothctl("disconnect")) == 2)
        assert len([l for l in sp.log if "no sink for it" in l]) == 1, "said once"
        # The sink finally appears: connected, routed, the cycle forgotten.
        bt.no_sink = False
        clock.now += 2
        assert _wait(lambda: sp.status()["connection"] == "connected"
                     and sp.status()["device"]["sink_present"] is True)
        assert sp.status()["device"]["last_error"] is None
        assert sp.status()["reconnect"]["next_in_s"] is None
    finally:
        sp.stop()


def test_the_connection_thread_survives_a_raising_runner():
    bt = FakeBluetooth()
    bt.raising = True
    sp = _bt_speaker(bt)
    sp.start()
    try:
        assert _wait(lambda: any("connection: OSError" in line for line in sp.log))
        time.sleep(0.2)
        assert sp._conn_thread.is_alive()
        assert sp.status()["connection"] == "no_device" and sp.status()["device"] is None
        assert sp.status()["error"] is None, "the connection never fails the speaker"
        assert len([l for l in sp.log if "connection: OSError" in l]) == 1, "said once"
        bt.raising = False
        assert _wait(lambda: sp.status()["connection"] == "connected")
        # A raise in the middle of a manual connect: back to what the check says.
        bt.connected = False
        assert _wait(lambda: sp.status()["connection"] == "disconnected")
        bt.raising = True
        sp.request_connect()
        assert _wait(lambda: len([l for l in sp.log if "connection: OSError" in l]) == 2)
        assert sp.status()["connection"] in ("disconnected", "no_device")
        bt.raising = False
        assert _wait(lambda: sp.status()["connection"] == "connected")
    finally:
        sp.stop()


def test_without_pulse_output_nothing_watches_bluetooth():
    bt = FakeBluetooth()
    sp = _bt_speaker(bt, output=None)
    sp.start()
    try:
        time.sleep(0.2)
        status = sp.status()
        assert status["bluetooth"] is False and status["connection"] == "no_device"
        assert status["device"] is None and status["pairing"] is None
        assert status["reconnect"] == {"attempts": 0, "next_in_s": None, "last_error": None}
        assert bt.bluetoothctl("info") == [] and sp._conn_thread is None
        assert sp.request_connect()[0] == 400 and sp.request_pair()[0] == 400
        assert "--speaker-output pulse" in sp.request_connect()[1]["error"]
    finally:
        sp.stop()
    # Asked for explicitly, it watches whatever the output.
    sp = _bt_speaker(bt, output="alsa", bluetooth=True)
    sp.start()
    try:
        assert _wait(lambda: sp.status()["connection"] == "connected")
    finally:
        sp.stop()


def test_a_routing_failure_is_said_and_tried_again():
    bt = FakeBluetooth()
    bt.sink_inputs = {"35": "0"}
    real = bt.__call__
    broken = {"on": True}

    def runner(argv, timeout=5.0):
        if broken["on"] and argv[:2] == ["pactl", "move-sink-input"]:
            bt.calls.append(list(argv))
            return 1, "Failure: No such entity"
        return real(argv, timeout)

    sp = _bt_speaker(bt, runner=runner)
    sp.start()
    try:
        assert _wait(lambda: "route to" in ((sp.status()["device"] or {}).get("last_error") or ""))
        assert "move-sink-input 35" in sp.status()["device"]["last_error"]
        assert sp.status()["connection"] == "connected"
        n = len(bt.pactl("move-sink-input"))
        assert _wait(lambda: len(bt.pactl("move-sink-input")) > n + 2), "retried on the interval"
        # ...and said once, not once per retry.
        assert len([l for l in sp.log if "could not route" in l]) == 1, sp.log
        broken["on"] = False
        assert _wait(lambda: bt.sink_inputs == {"35": "1"})
        assert _wait(lambda: sp.status()["device"]["last_error"] is None)
    finally:
        sp.stop()


def test_speaker_connect_and_pair_over_http_and_the_fleet_report(tmp_path, monkeypatch):
    import conductor.server as srv

    ws = _workspace(tmp_path / "ws", music=False)
    (ws.root / "fleet.json").write_text(json.dumps({"speaker_mac": MAC.lower()}), encoding="utf-8")
    bt = FakeBluetooth(connected=False)
    sessions = []
    made = {}

    class Once(srv._Server):
        def serve_forever(self, poll_interval=0.5):
            handler = self.RequestHandlerClass
            port = self.server_address[1]
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and handler.speaker.status()["device"] is None:
                time.sleep(0.01)
            # Fast intervals for the test; the thread reads them each tick.
            handler.speaker._reconnect_first_s = 100.0
            handler.speaker._pair_scan_s = handler.speaker._pair_wait_s = 1.0
            handler.speaker._pair_connect_wait_s = 1.0
            handler.speaker._pair_poll_s = 0.05
            handler.speaker._conn_check_s = 0.05
            threading.Thread(target=super().serve_forever, daemon=True).start()
            made["fleet"] = json.loads(_get(port, "/api/fleet")[1])
            made["bad_mac"] = _post(port, "/api/speaker/pair", {"mac": "nope"})
            made["mac_type"] = _post(port, "/api/speaker/pair", {"mac": 5})[0]
            made["connect"] = _post(port, "/api/speaker/connect", {})
            _wait(lambda: handler.speaker.status()["connection"] == "connected")
            made["after_connect"] = json.loads(_get(port, "/api/fleet")[1])["speaker"]
            made["pair"] = _post(port, "/api/speaker/pair", {})
            # The speaker is not in pairing mode yet: the scan waits, and
            # meanwhile a second Re-pair and a Connect are refused.
            made["pair_again"] = _post(port, "/api/speaker/pair", {})
            made["connect_while_pairing"] = _post(port, "/api/speaker/connect", {})
            made["while_pairing"] = json.loads(_get(port, "/api/fleet")[1])["speaker"]
            bt.pairing_mode = True
            _wait(lambda: (handler.speaker.status()["pairing"] or {}).get("phase") == "done", 5.0)
            _wait(lambda: handler.speaker.status()["connection"] == "connected")
            made["after_pair"] = json.loads(_get(port, "/api/fleet")[1])["speaker"]
            self.shutdown()

    monkeypatch.setattr(srv, "_Server", Once)
    monkeypatch.setattr(srv, "already_serving", lambda port: False)

    def session_factory(argv):
        session = FakeBtSession(bt)
        sessions.append(session)
        return session

    assert srv.serve(ws.root, port=0, speaker=True, speaker_lead_ms=0,
                     speaker_output="pulse", speaker_factory=lambda argv: FakeMpg123(),
                     speaker_runner=bt, speaker_session_factory=session_factory) == 0
    speaker = made["fleet"]["speaker"]
    assert speaker["bluetooth"] is True and speaker["connection"] == "disconnected"
    assert speaker["device"]["mac"] == MAC and speaker["device"]["name"] == "Bose Flex SoundLink"
    assert set(speaker["device"]) == {"mac", "name", "paired", "trusted", "connected",
                                      "sink_present", "last_connected_at", "last_error"}
    assert set(speaker["reconnect"]) == {"attempts", "next_in_s", "last_error"}
    assert speaker["pairing"] is None
    assert bt.bluetoothctl("paired-devices") == [], "fleet.json's speaker_mac was used"
    assert made["bad_mac"] == (400, {"ok": False, "connection": "disconnected",
                                     "error": "mac: not a Bluetooth address ('nope')"})
    assert made["mac_type"] == 400
    assert made["connect"] == (200, {"ok": True, "connection": "connecting", "error": None})
    assert made["after_connect"]["connection"] == "connected"
    assert made["pair"] == (200, {"ok": True, "connection": "pairing", "error": None})
    assert made["pair_again"][0] == 409 and made["connect_while_pairing"][0] == 409
    assert made["pair_again"][1]["ok"] is False and made["pair_again"][1]["connection"] == "pairing"
    busy = made["while_pairing"]
    assert busy["connection"] == "pairing" and busy["pairing"]["phase"] == "scanning"
    after = made["after_pair"]
    assert after["connection"] == "connected" and after["pairing"]["phase"] == "done"
    assert set(after["pairing"]) == {"phase", "note", "started_at"}
    assert sessions and sessions[0].commands[:2] == [f"remove {MAC}", "scan on"]


def test_speaker_connect_without_a_speaker_is_a_400(tmp_path):
    server = make_server(tmp_path, port=0, fleet=Fleet({}))
    port = _serve(server)
    try:
        status, answer = _post(port, "/api/speaker/connect", {})
        assert status == 400 and answer["ok"] is False and "no speaker" in answer["error"]
        assert answer["connection"] == "no_device"
        assert _post(port, "/api/speaker/pair", {"force": True})[0] == 400
    finally:
        server.shutdown()
        server.server_close()


def _page_block(start_marker):
    start = PAGE_TEXT.index(start_marker)
    return PAGE_TEXT[start:PAGE_TEXT.index("\n}\n", start)]


def test_the_page_has_the_speaker_connection_line_and_buttons():
    assert 'id="spk-conn-text"' in PAGE_TEXT and 'id="spk-connect"' in PAGE_TEXT
    assert 'id="spk-pair"' in PAGE_TEXT
    assert 'api("/api/speaker/connect", {})' in PAGE_TEXT
    assert 'api("/api/speaker/pair", body)' in PAGE_TEXT
    pair = _page_block("async function speakerPair()")
    assert "confirm(SPEAKER_PAIR_QUESTION)" in pair, "a re-pair drops the sound: ask first"
    assert "body.force = true" in pair and "fleet.run" in pair
    start = PAGE_TEXT.index("const SPEAKER_PAIR_QUESTION = ")
    question = PAGE_TEXT[start:PAGE_TEXT.index(";\n", start)]
    assert "pairing mode" in question and "Bluetooth OFF" in question
    # Shown only where the host watches a Bluetooth speaker; the volume slider stays.
    assert 'conn.style.display = sp && sp.bluetooth ? "" : "none"' in PAGE_TEXT
    assert 'hostvol.style.display = sp ? "" : "none"' in PAGE_TEXT
    text = _page_block("function speakerConnectionText(sp)")
    for word in ("· connected", "· connecting…", "· not connected", "pairing: ", "retry in"):
        assert word in text, word
    assert ': "ok"' in text and 'tone: "err"' in text and 'tone: "busy"' in text
    assert 'if (e.target.id === "spk-connect") { await speakerConnect(); return; }' in PAGE_TEXT
    assert 'if (e.target.id === "spk-pair") { await speakerPair(); return; }' in PAGE_TEXT


# ------------------------------------------------------------ the operator's 'start anyway', carried per unit

def _failed_burn(pairs, total=16, reason=None):
    burn = {"state": "failed", "failed": [list(p) for p in pairs], "total": total}
    if reason:
        burn["reason"] = reason
    return burn


def test_a_forced_start_is_carried_into_the_restart_for_the_same_failure():
    """radxa-10, 2026-10-01: one dead board the operator waved through must
    not stop the loop after run 1 - the restart forces THAT unit again
    while its failure is unchanged, and nobody else."""
    clock = Clock()
    fleet = _fleet(clock, lambda: (40.0, 2.0), loop_retry_s=5.0)
    fleet.links["radxa-02"].status["show"]["burn"] = _failed_burn([(7, 1), (7, 2)])
    with pytest.raises(ValueError, match="radxa-02: 2 of 16 pictures not written"):
        fleet.start_show(lead_s=1.0)
    fleet.start_show(lead_s=1.0, force=True)              # the operator's 'start anyway'
    assert fleet._waved == {"radxa-02": (((7, 1), (7, 2)), None, 16, "showA")}
    assert all(body["force"] for _, body in _posted(fleet, "/show/run"))
    # The run ends; the loop restarts: radxa-02 forced, radxa-01 not.
    clock.now = fleet.run["t0"] + 100.0
    fleet._loop_tick()
    clock.now += 40.0
    for link in fleet.links.values():
        link.posted.clear()
    fleet._loop_tick()
    assert fleet.run["loops"] == 1 and fleet.run["force"] is False
    assert fleet.run["forced"] == ["radxa-02"]
    forces = {name: body["force"] for name, body in _posted(fleet, "/show/run")}
    assert forces == {"radxa-01": False, "radxa-02": True}
    assert fleet.loop_state() is None
    said = [l for l in fleet.corrections if "started again with the operator's 'start anyway'" in l]
    assert len(said) == 1 and "radxa-02" in said[0] and "same 1 board" in said[0]
    # ...and again at the next end, as long as nothing changed - said ONCE,
    # not on every restart (the corrections hold 20 lines).
    clock.now = fleet.run["t0"] + 100.0
    fleet._loop_tick()
    clock.now += 40.0
    fleet._loop_tick()
    assert fleet.run["loops"] == 2 and fleet.run["forced"] == ["radxa-02"]
    assert len([l for l in fleet.corrections if "start anyway" in l]) == 1


def test_a_failure_that_changed_is_not_forced_and_takes_the_grace_path():
    clock = Clock()
    fleet = _fleet(clock, lambda: (40.0, 2.0), loop_retry_s=5.0)
    fleet.links["radxa-02"].status["show"]["burn"] = _failed_burn([(7, 1)])
    fleet.start_show(lead_s=1.0, force=True)
    # A second board dies during the run: more failed pairs than waved.
    fleet.links["radxa-02"].status["show"]["burn"] = _failed_burn([(7, 1), (9, 1)])
    clock.now = fleet.run["t0"] + 100.0
    fleet._loop_tick()
    clock.now += 40.0
    fleet._loop_tick()
    assert fleet.run["loops"] == 0
    assert "radxa-02: 2 of 16 pictures not written" in fleet.loop_state()["problem"]
    clock.now += 60.0
    for link in fleet.links.values():
        link.posted.clear()
    fleet._loop_tick()
    assert fleet.run["loops"] == 1 and fleet.run["forced"] == []
    assert [name for name, _ in _posted(fleet, "/show/run")] == ["radxa-01"]
    assert fleet.loop_state()["problem"].startswith("started without radxa-02")
    assert not any("start anyway" in l for l in fleet.corrections)
    # A reason that changed counts as changed too.
    fleet2 = _fleet(clock, lambda: (40.0, 2.0))
    fleet2.links["radxa-02"].status["show"]["burn"] = _failed_burn(
        [], reason="none of its 16 boards answered")
    fleet2.start_show(lead_s=1.0, force=True)
    assert fleet2._waved == {"radxa-02": ((), "none of its 16 boards answered", 16, "showA")}
    fleet2.links["radxa-02"].status["show"]["burn"] = _failed_burn(
        [], reason="the port was taken")
    assert fleet2._still_waved() == set()
    fleet2.links["radxa-02"].status["show"]["burn"] = _failed_burn(
        [], reason="none of its 16 boards answered")
    assert fleet2._still_waved() == {"radxa-02"}


def test_a_plain_start_never_lets_the_loop_force():
    clock = Clock()
    fleet = _fleet(clock, lambda: (40.0, 2.0), loop_retry_s=5.0)
    fleet.start_show(lead_s=1.0)
    assert fleet._waved == {}
    # A board fails after the START: the restart does not force it.
    fleet.links["radxa-02"].status["show"]["burn"] = _failed_burn([(3, 1)])
    clock.now = fleet.run["t0"] + 100.0
    fleet._loop_tick()
    clock.now += 40.0
    fleet._loop_tick()
    assert fleet.run["loops"] == 0 and "radxa-02" in fleet.loop_state()["problem"]
    # A forced START, then a plain one: the plain one forgets the waving.
    fleet.start_show(lead_s=1.0, force=True)
    assert fleet._waved == {"radxa-02": (((3, 1),), None, 16, "showA")}
    fleet.links["radxa-02"].status["show"]["burn"] = {"state": "burned"}
    fleet.start_show(lead_s=1.0)
    assert fleet._waved == {}


# ------------------------------------------------------------ review of a9898aa

def test_with_the_loop_on_the_units_copy_does_not_clear_itself_but_stop_still_clears(tmp_path):
    ws = _workspace(tmp_path / "ws", music=False)
    ws.set_clear_after_show(True)
    plain, _ = ws.compile_show()
    assert plain["radxa-01"]["clear_after_show"] is True
    assert "conductor_clear_after_show" not in plain["radxa-01"]
    rev = ws.revision()
    ws.set_loop(0)
    looped, _ = ws.compile_show()
    unit_copy = looped["radxa-01"]
    assert "clear_after_show" not in unit_copy, "the unit would clear at its own ENDED"
    assert unit_copy["conductor_clear_after_show"] is True
    # Same id, same revision: no picture rewritten, no "changed since".
    assert unit_copy["id"] == plain["radxa-01"]["id"] and ws.revision() == rev
    # The fleet still knows the operator wants the clear - on STOP.
    clock = Clock()
    fleet = Fleet({}, clock=clock, loop_settings=ws.loop_settings)
    fleet.links = {"radxa-01": StubLink("radxa-01", "loaded")}
    fleet.links["radxa-01"].status["show"].update(id=unit_copy["id"], burn={"state": "burned"})
    fleet.upload(looped)
    assert fleet.clear_wanted() is True
    fleet.start_show(lead_s=1.0)
    assert fleet.run["clear_after_show"] is True
    fleet.stop_show()
    assert fleet.clear_armed_in_s() is not None


def test_turning_the_loop_on_warns_when_the_units_hold_a_self_clearing_copy(tmp_path):
    ws = _workspace(tmp_path / "ws", music=False)
    ws.set_clear_after_show(True)
    shows, _ = ws.compile_show()                      # uploaded BEFORE the Loop
    fleet = Fleet({})
    fleet.shows = dict(shows)
    server = make_server(ws.root, port=0, fleet=fleet)
    port = _serve(server)
    try:
        status, loop = _post(port, "/api/loop", {"on": True, "wait_s": 0})
        assert status == 200 and loop["on"]
        assert "clears its own pictures" in loop["note"]
        assert any("Loop on:" in c for c in fleet.corrections)
        # After an Upload of the looped compile, no note.
        fleet.shows = dict(ws.compile_show()[0])
        status, loop = _post(port, "/api/loop", {"on": True, "wait_s": 0})
        assert status == 200 and "note" not in loop
    finally:
        server.shutdown()
        server.server_close()


def test_a_plain_on_lifts_a_stored_wait_below_the_floor_and_wait_s_is_effective(tmp_path):
    """MED-2."""
    ws = _workspace(tmp_path / "ws", music=False)
    ws.set_loop(0)
    ws.set_timeline(120, _show_with_tail(120.0, 110.0)["cues"])     # floor 30 now
    server = make_server(ws.root, port=0, fleet=Fleet({}))
    port = _serve(server)
    try:
        loop = json.loads(_get(port, "/api/fleet")[1])["loop"]
        assert loop["wait_s"] == 30 and loop["stored_wait_s"] == 0 and loop["min_wait_s"] == 30
        status, loop = _post(port, "/api/loop", {"on": True})      # no 400
        assert status == 200 and loop["wait_s"] == 30 and loop["stored_wait_s"] == 30
        assert ws.loop_wait() == 30.0
        status, loop = _post(port, "/api/loop", {"on": False})
        assert status == 200 and loop["on"] is False and loop["stored_wait_s"] is None
        assert loop["wait_s"] == 45                                   # the default, above the floor
    finally:
        server.shutdown()
        server.server_close()


def test_a_waved_unit_offline_at_the_restart_rejoins_the_loop_run_forced():
    """MED-1 (review of ef61d26): offline at the restart it has no
    signature, is left out after the grace, and comes back mid-run -
    supervision's /show/run must carry the operator's force then, or the
    unit is refused for the whole run."""
    clock = Clock()
    fleet = _fleet(clock, lambda: (40.0, 2.0), loop_retry_s=5.0)
    fleet.links["radxa-02"].status["show"]["burn"] = _failed_burn([(7, 1)])
    fleet.start_show(lead_s=1.0, force=True)
    fleet.links["radxa-02"].online = False               # unplugged between runs
    clock.now = fleet.run["t0"] + 100.0
    fleet._loop_tick()
    clock.now += 40.0
    fleet._loop_tick()                                   # refused: not answering
    clock.now += 60.0
    fleet._loop_tick()                                   # started without it
    assert fleet.run["loops"] == 1 and fleet.run["forced"] == []
    # Back, 10 s into the run, with the SAME failure: the rejoin forces.
    fleet.links["radxa-02"].online = True
    fleet.links["radxa-02"].status["show"].update(state="loaded", t0=None)
    fleet.links["radxa-02"].posted.clear()
    fleet._corrected.clear()
    clock.now = fleet.run["t0"] + 10.0
    fleet._supervise(fleet.links["radxa-02"])
    runs = [body for path, body in fleet.links["radxa-02"].posted if path == "/show/run"]
    assert runs and runs[-1]["force"] is True, fleet.links["radxa-02"].posted
    # ...a DIFFERENT failure, or a plain-START run: no force on the rejoin.
    fleet.links["radxa-02"].status["show"]["burn"] = _failed_burn([(7, 1), (8, 1)])
    fleet.links["radxa-02"].posted.clear()
    fleet._corrected.clear()
    fleet._supervise(fleet.links["radxa-02"])
    runs = [body for path, body in fleet.links["radxa-02"].posted if path == "/show/run"]
    assert runs and runs[-1]["force"] is False
    assert fleet._rejoin_force({"force": False, "forced": [], "loops": 0}, "radxa-02") is False
    assert fleet._rejoin_force({"force": False, "forced": ["radxa-02"], "loops": 0}, "radxa-02") is True


def test_a_rescue_upload_of_another_compile_is_not_carried():
    """LOW-1: the signature carries the show id."""
    clock = Clock()
    fleet = _fleet(clock, lambda: (40.0, 2.0))
    fleet.links["radxa-02"].status["show"]["burn"] = _failed_burn([(7, 1)])
    fleet.start_show(lead_s=1.0, force=True)
    assert fleet._still_waved() == {"radxa-02"}
    fleet.upload({"radxa-02": {"id": "showB", "cues": [], "duration": 100.0}}, force=True,
                 only=["radxa-02"])
    fleet.links["radxa-02"].status["show"].update(id="showB")
    assert fleet._still_waved() == set()


# ------------------------------------------------------------ PRESET -> countdown -> 0:00

CUE0 = {"id": "q00", "at": 0, "sent": -8.0, "refresh_s": 8.0, "span": 0.0}


def _preset_fleet(clock, settings=None, paint_s=8.0, **kw):
    fleet = Fleet({}, clock=clock, loop_settings=settings, **kw)
    fleet.links = {"radxa-01": StubLink("radxa-01", "loaded"),
                   "radxa-02": StubLink("radxa-02", "loaded")}
    for link in fleet.links.values():
        link.status["show"].update(id="showA", burn={"state": "burned"},
                                   applied=None, dirty=False)
    cue = dict(CUE0, refresh_s=paint_s)
    fleet.shows = {n: {"id": "showA", "cues": [cue], "duration": 100.0}
                   for n in fleet.links}
    return fleet


def _paint(fleet, *names):
    for name in names:
        fleet.links[name].status["show"]["applied"] = "q00"


def test_start_with_preset_first_goes_preset_countdown_zero():
    clock = Clock()
    fleet = _preset_fleet(clock)
    results = fleet.start_show(lead_s=3.0, preset_first=True)
    # (1) the existing preset went to every target, nothing else yet.
    assert results == {"radxa-01": {"ok": True, "phase": None, "preset": True},
                       "radxa-02": {"ok": True, "phase": None, "preset": True}}
    assert _posted(fleet, "/show/run") == [] and fleet.run is None
    snap = fleet.snapshot()
    assert snap["run"]["state"] == "preset" and snap["run"]["phase"] == "preset"
    assert snap["run"]["now"] == 0.0 and snap["run"]["t0"] is None
    assert snap["preset"]["waiting_for"] == ["radxa-01", "radxa-02"]
    assert snap["preset"]["cap_s"] == 45.0 and snap["start_at"] == 0.0
    # (2) the units report the 0:00 cue applied; the start waits the paint
    #     time (refresh 8 s) + 0.5 s from the LAST one.
    clock.now += 1.0
    _paint(fleet, "radxa-01")
    fleet._staging_tick()
    clock.now += 2.0
    _paint(fleet, "radxa-02")
    fleet._staging_tick()
    assert fleet.snapshot()["preset"]["painted"] == ["radxa-01", "radxa-02"]
    clock.now += 8.0                                  # 01 done, 02 at 8.0 of 8.5
    fleet._staging_tick()
    assert fleet.run is None
    clock.now += 0.6
    fleet._staging_tick()
    # (3) the start, with the countdown as its lead; one preset per unit,
    #     no second paint of the 0:00 look from this side.
    assert fleet.run["state"] == "running" and fleet.run["t0"] == clock.now + 3.0
    assert fleet.snapshot()["preset"] is None and fleet.snapshot()["run"]["state"] == "running"
    runs = _posted(fleet, "/show/run")
    assert sorted(n for n, _ in runs) == ["radxa-01", "radxa-02"]
    assert [n for n, _ in _posted(fleet, "/show/preset")].count("radxa-01") == 1
    assert any("preset painted, countdown running" in c for c in fleet.corrections)


def test_a_unit_that_never_paints_is_waited_for_45_s_then_named():
    clock = Clock()
    fleet = _preset_fleet(clock)
    fleet.start_show(lead_s=11.0, preset_first=True)
    _paint(fleet, "radxa-01")
    for _ in range(44):
        clock.now += 1.0
        fleet._staging_tick()
    assert fleet.run is None and fleet.snapshot()["preset"]["waiting_for"] == ["radxa-02"]
    clock.now += 1.5
    fleet._staging_tick()
    assert fleet.run["t0"] == clock.now + 11.0
    assert any("radxa-02 had not painted the 0:00 look after 45 s - starting anyway" in c
               for c in fleet.corrections)
    assert sorted(n for n, _ in _posted(fleet, "/show/run")) == ["radxa-01", "radxa-02"]


def test_stop_during_the_preset_cancels_the_start():
    clock = Clock()
    fleet = _preset_fleet(clock)
    fleet.start_show(lead_s=3.0, preset_first=True)
    fleet.stop_show()
    assert fleet.preset_state() is None and fleet.snapshot()["run"] is None
    _paint(fleet, "radxa-01", "radxa-02")
    clock.now += 60.0
    fleet._staging_tick()
    assert fleet.run is None and _posted(fleet, "/show/run") == []
    # The next START: both garments show the look that (cancelled) stage
    # put up, painted long ago - no second preset, the countdown at once.
    fleet.start_show(lead_s=3.0, preset_first=True)
    assert fleet.preset_state() is None and fleet.run["t0"] == clock.now + 3.0
    assert len(_posted(fleet, "/show/preset")) == 2


def test_with_the_flag_off_start_is_exactly_as_before():
    clock = Clock()
    fleet = _preset_fleet(clock)
    results = fleet.start_show(lead_s=3.0)
    assert fleet.run["t0"] == clock.now + 3.0 and fleet.preset_state() is None
    assert _posted(fleet, "/show/preset") == []
    assert sorted(results) == ["radxa-01", "radxa-02"] and all(r["ok"] for r in results.values())
    # A start from a mark never presets, flag or no flag.
    fleet.stop_show()
    fleet.start_show(lead_s=3.0, at=30.0, preset_first=True)
    assert fleet.run["t0"] == clock.now + 3.0 - 30.0 and fleet.preset_state() is None


def test_the_loop_restart_presets_first_when_the_show_says_so():
    clock = Clock()
    fleet = _preset_fleet(clock, settings=lambda: (0.0, 3.0, True), loop_retry_s=5.0)
    fleet.start_show(lead_s=3.0)
    clock.now = fleet.run["t0"] + 100.0
    fleet._loop_tick()                                   # end -> wait 0 -> arms
    assert fleet.loop_state() is not None
    fleet._loop_tick()                                   # the wait is up: preset goes out
    assert fleet.preset_state() is not None and fleet.preset_state()["loop"] is True
    assert fleet.loop_state() is None                    # no "next run in" during the preset
    assert fleet.snapshot()["run"]["state"] == "preset"
    assert sorted(n for n, _ in _posted(fleet, "/show/preset")) == ["radxa-01", "radxa-02"]
    _paint(fleet, "radxa-01", "radxa-02")
    fleet._loop_tick()                                   # the staging tick sees them painted
    clock.now += 8.6
    fleet._loop_tick()                                   # ...and starts after the paint time
    assert fleet.run["loops"] == 1 and fleet.run["t0"] == clock.now + 3.0
    # A 2-tuple from an older settings callable still works: no preset.
    fleet2 = _preset_fleet(clock, settings=lambda: (0.0, 3.0))
    fleet2.start_show(lead_s=3.0)
    clock.now = fleet2.run["t0"] + 100.0
    fleet2._loop_tick()
    fleet2._loop_tick()
    assert fleet2.run["loops"] == 1 and fleet2.preset_state() is None


def test_a_waved_unit_is_preset_with_force_too():
    clock = Clock()
    fleet = _preset_fleet(clock)
    fleet.links["radxa-02"].status["show"]["burn"] = _failed_burn([(7, 1)])
    with pytest.raises(ValueError):
        fleet.start_show(lead_s=3.0, preset_first=True)
    fleet.start_show(lead_s=3.0, force=True, preset_first=True)
    assert {n: b["force"] for n, b in _posted(fleet, "/show/preset")} == {
        "radxa-01": True, "radxa-02": True}
    _paint(fleet, "radxa-01", "radxa-02")
    fleet._staging_tick()
    clock.now += 8.6
    fleet._staging_tick()
    assert fleet.run["force"] is True and fleet._waved


def test_preset_before_start_is_a_show_setting_off_by_default(tmp_path):
    # OFF unless the show says true (review of d64e6e0, HIGH-2): the PC's
    # show files have no key and keep (2) preset -> (3) START -> countdown.
    ws = _workspace(tmp_path / "ws", music=False)
    assert ws.preset_before_start() is False
    assert "preset_before_start" not in ws._load_show()
    assert ws.state()["show"]["preset_before_start"] is False
    rev = ws.revision()
    ws.set_preset_before_start(True)
    assert ws.preset_before_start() is True and ws._load_show()["preset_before_start"] is True
    assert ws.revision() == rev                          # never a "changed since"
    assert ws.undo() and ws.preset_before_start() is False
    assert "preset_before_start" not in ws._load_show()
    ws.set_preset_before_start(True)
    ws.set_preset_before_start(False)
    assert "preset_before_start" not in ws._load_show()  # off is no key
    ws.set_preset_before_start(True)
    exported = ws.export_show()
    assert exported["preset_before_start"] is True
    b = _workspace(tmp_path / "b", music=False)
    b.import_show(exported)
    assert b.preset_before_start() is True and b._load_show()["preset_before_start"] is True
    # A file saying false turns it off - and leaves no key (LOW-6).
    b.import_show(dict(exported, preset_before_start=False))
    assert b.preset_before_start() is False and "preset_before_start" not in b._load_show()
    # A show.json an older build wrote `false` into still reads off.
    legacy = dict(ws._load_show(), preset_before_start=False)
    from conductor.server import preset_before_start_of
    assert preset_before_start_of(legacy) is False
    assert preset_before_start_of({}) is False
    with pytest.raises(ValueError):
        ws.set_preset_before_start("yes")
    # ...and the loop settings carry it as the third member.
    ws.set_loop(0)
    assert ws.loop_settings() == (0.0, 11.0, True)
    ws.set_preset_before_start(False)
    assert ws.loop_settings() == (0.0, 11.0, False)


def test_start_over_http_is_staged_only_from_zero_and_only_with_the_flag(tmp_path):
    ws = _workspace(tmp_path / "ws", music=False)
    clock = Clock()
    fleet = _preset_fleet(clock)
    server = make_server(ws.root, port=0, fleet=fleet)
    port = _serve(server)
    try:
        # The default (no key): the START of main, byte for byte.
        status, answer = _post(port, "/api/fleet/start", {"lead_s": 3})
        assert status == 200 and "staged" not in answer and fleet.run is not None
        assert _posted(fleet, "/show/preset") == []
        fleet.stop_show()
        # Ticked on (the exhibition): staged.
        _post(port, "/api/show/preset_before", {"on": True})
        status, answer = _post(port, "/api/fleet/start", {"lead_s": 3})
        assert status == 200 and answer["staged"] is True, answer
        assert "Preset first" in answer["note"] and fleet.run is None
        assert fleet.preset_state() is not None
        status, again = _post(port, "/api/fleet/start", {"lead_s": 3})
        assert "preset first" in again["note"] and fleet.preset_state() is not None
        assert _post(port, "/api/fleet/stop", {})[0] == 200
        assert fleet.preset_state() is None
        # From a mark: never staged.
        fleet.start_at = 30.0
        status, answer = _post(port, "/api/fleet/start", {"lead_s": 3, "manual": True})
        assert status == 200 and "staged" not in answer and fleet.run is not None
        assert answer["note"] == "Started from 0:30."
    finally:
        server.shutdown()
        server.server_close()


def test_the_page_shows_the_preset_stage():
    assert 'id="show-preset-before"' in PAGE_TEXT
    assert 'api("/api/show/preset_before", { on })' in PAGE_TEXT
    assert 'if (run.state === "preset") return 0;' in PAGE_TEXT
    assert 'if (run.state === "preset") return "loaded";' in PAGE_TEXT
    assert 'word = `PRESET… ${painted}/${total} painted`' in PAGE_TEXT
    # N is the units waited for: a refused one is counted out, and said.
    assert "const waited = (p.targets || []).filter(n => !refused.includes(n));" in PAGE_TEXT
    assert '(refused.length ? ` (${refused.length} refused)` : "")' in PAGE_TEXT
    assert 'total = typeof p.waited === "number" ? p.waited : waited.length;' in PAGE_TEXT
    # ...and a staged START that went on without some garments says so once.
    assert "function notePresetLeftOut()" in PAGE_TEXT
    assert "The show started without the 0:00 look on ${names.join" in PAGE_TEXT
    assert 'fleet.run.state !== "preset" && !runIsOver()' in PAGE_TEXT


# ---- review of d64e6e0: the PRESET stage's fixes ----

class RefusingPresetLink(StubLink):
    """A unit that answers everything but refuses /show/preset (409)."""

    def __init__(self, name, show_state, why="the show is running"):
        super().__init__(name, show_state)
        self.why = why

    def post(self, path, body, learn=True, timeout=None):
        if path == "/show/preset":
            self.posted.append((path, body))
            raise RuntimeError(self.why)
        return super().post(path, body, learn, timeout)


def _start_after_paint(fleet, clock, *names):
    """Paint `names` and tick past their 8 s paint + 0.5 s settle."""
    _paint(fleet, *names)
    fleet._staging_tick()
    clock.now += 8.6
    fleet._staging_tick()


def test_a_staged_start_after_a_stop_still_starts():
    # HIGH-1: STOP sets _stopped; the stage must clear it like START does,
    # or every staged START after any STOP is dropped at its first tick.
    clock = Clock()
    fleet = _preset_fleet(clock)
    fleet.start_show(lead_s=3.0)                         # an ordinary START...
    fleet.stop_show()                                    # ...and a STOP
    assert fleet._stopped
    fleet.start_show(lead_s=3.0, preset_first=True)
    assert fleet.preset_state() is not None and not fleet._stopped
    _start_after_paint(fleet, clock, "radxa-01", "radxa-02")
    assert fleet.run is not None and fleet.run["t0"] == clock.now + 3.0
    # ...and again after a second STOP (the run moved the garments on).
    fleet.stop_show()
    for link in fleet.links.values():
        link.status["show"]["applied"] = "q09"
    fleet.start_show(lead_s=3.0, preset_first=True)
    assert fleet.preset_state() is not None
    _start_after_paint(fleet, clock, "radxa-01", "radxa-02")
    assert fleet.run is not None and fleet.run["t0"] == clock.now + 3.0
    assert len(_posted(fleet, "/show/preset")) == 4      # two per unit, one per START


def test_a_manual_preset_already_painted_starts_the_countdown_at_once():
    # HIGH-2 (b): the PC's (2) Show preset -> (3) START with the flag on is
    # the old timing exactly - no second /show/preset, no stage.
    clock = Clock()
    fleet = _preset_fleet(clock)
    fleet.preset()
    assert len(_posted(fleet, "/show/preset")) == 2
    clock.now += 4.0
    _paint(fleet, "radxa-01", "radxa-02")
    clock.now += 6.0                                     # 10 s since (2): > 8 + 0.5
    results = fleet.start_show(lead_s=11.0, preset_first=True)
    assert fleet.preset_state() is None and fleet.run["t0"] == clock.now + 11.0
    assert not any(r.get("preset") for r in results.values())
    assert len(_posted(fleet, "/show/preset")) == 2      # no second paint
    assert any("already shows the 0:00 look" in c for c in fleet.corrections)
    # The run moves the garments on: the next staged START presets again.
    fleet.stop_show()
    fleet.start_show(lead_s=11.0, preset_first=True)
    assert fleet.preset_state() is not None
    assert len(_posted(fleet, "/show/preset")) == 4


def test_start_pressed_right_after_a_manual_preset_waits_without_a_second_preset():
    # (3) two seconds after (2): the look is still going up. The stage waits
    # for it (no second /show/preset - that would paint it twice).
    clock = Clock()
    fleet = _preset_fleet(clock)
    fleet.preset()
    clock.now += 2.0
    results = fleet.start_show(lead_s=11.0, preset_first=True)
    assert results == {n: {"ok": True, "preset": True, "already": True}
                       for n in ("radxa-01", "radxa-02")}
    assert fleet.preset_state()["already"] == ["radxa-01", "radxa-02"]
    assert len(_posted(fleet, "/show/preset")) == 2
    _start_after_paint(fleet, clock, "radxa-01", "radxa-02")
    assert fleet.run["t0"] == clock.now + 11.0
    assert len(_posted(fleet, "/show/preset")) == 2


def test_a_mixed_fleet_presets_only_the_units_not_showing_the_look():
    clock = Clock()
    fleet = _preset_fleet(clock)
    fleet.preset()
    clock.now += 10.0
    _paint(fleet, "radxa-01")                            # 02 shows something else
    fleet.links["radxa-02"].status["show"]["applied"] = "q09"
    results = fleet.start_show(lead_s=11.0, preset_first=True)
    assert results["radxa-01"] == {"ok": True, "preset": True, "already": True}
    assert results["radxa-02"]["preset"] is True and "already" not in results["radxa-02"]
    assert sorted(n for n, _ in _posted(fleet, "/show/preset")) == [
        "radxa-01", "radxa-02", "radxa-02"]
    assert fleet.preset_state()["painted"] == ["radxa-01"]
    _start_after_paint(fleet, clock, "radxa-02")
    assert fleet.run["t0"] == clock.now + 11.0


def test_a_status_from_before_the_preset_reply_is_not_counted():
    # LOW-2: a poll already in flight reports the look from before.
    clock = Clock()
    fleet = _preset_fleet(clock)
    for link in fleet.links.values():
        link.status_sent = clock.now - 1.0               # asked before the preset
    _paint(fleet, "radxa-01", "radxa-02")                # ...says q00 (the old look)
    fleet.start_show(lead_s=3.0, preset_first=True)
    clock.now += 9.0
    fleet._staging_tick()
    assert fleet.preset_state()["painted"] == []
    for link in fleet.links.values():
        link.status_sent = clock.now                     # a poll after the reply
    _start_after_paint(fleet, clock)
    assert fleet.run is not None


def test_a_refused_preset_is_not_waited_for_but_gets_the_start():
    # MED-1: the unit that refused is named, left out of the wait, and still
    # sent /show/run - the others start after their paint, not at 45 s.
    clock = Clock()
    fleet = _preset_fleet(clock)
    fleet.links["radxa-02"] = RefusingPresetLink("radxa-02", "loaded", "timed out")
    fleet.links["radxa-02"].status["show"].update(id="showA", burn={"state": "burned"},
                                                  applied=None, dirty=False)
    results = fleet.start_show(lead_s=3.0, preset_first=True)
    assert results["radxa-02"] == {"ok": False, "error": "timed out", "preset": True}
    state = fleet.preset_state()
    assert state["refused"] == {"radxa-02": "timed out"} and state["retrying"] == []
    assert state["waiting_for"] == ["radxa-01"] and state["waited"] == 1
    assert any("radxa-02 refused the preset (timed out) - not waited for" in c
               for c in fleet.corrections)
    _start_after_paint(fleet, clock, "radxa-01")
    assert fleet.run is not None and fleet.run["t0"] == clock.now + 3.0
    assert sorted(n for n, _ in _posted(fleet, "/show/run")) == ["radxa-01", "radxa-02"]


def test_a_forced_start_over_a_running_show_is_not_staged():
    # MED-1: the units would refuse a preset while running - START at once.
    clock = Clock()
    fleet = _preset_fleet(clock)
    fleet.start_show(lead_s=3.0)
    clock.now += 20.0                                    # mid-show
    fleet.start_show(lead_s=11.0, force=True, preset_first=True)
    assert fleet.preset_state() is None and fleet.run["t0"] == clock.now + 11.0
    assert _posted(fleet, "/show/preset") == []
    # A run that has reached its END is not running: that START is staged.
    clock.now = fleet.run["t0"] + 100.0
    fleet.start_show(lead_s=11.0, preset_first=True)
    assert fleet.preset_state() is not None


def test_a_loop_stage_starts_without_a_unit_that_went_offline_during_it():
    # MED-2: the stage's end works out who is ready THEN - not the burn gate
    # refusing "not answering" and the whole loop re-arming wait + 60 s.
    clock = Clock()
    fleet = _preset_fleet(clock, settings=lambda: (0.0, 3.0, True), loop_retry_s=5.0)
    fleet.start_show(lead_s=3.0)
    clock.now = fleet.run["t0"] + 100.0
    fleet._loop_tick()                                   # end -> arms the wait (0 s)
    fleet._loop_tick()                                   # the preset goes out
    assert fleet.preset_state()["loop"] is True
    _paint(fleet, "radxa-01")
    fleet.links["radxa-02"].online = False               # unplugged mid-stage
    for _ in range(46):
        clock.now += 1.0
        fleet._loop_tick()
    assert fleet.run["loops"] == 1 and fleet.preset_state() is None
    assert [n for n, _ in _posted(fleet, "/show/run")].count("radxa-02") == 1   # run 0 only
    assert fleet._loop_skipped == {"radxa-02": "radxa-02: not answering"}
    assert "started without radxa-02" in fleet.loop_state()["problem"]
    assert any("Loop: run 1 started without radxa-02 (not ready: radxa-02: not answering)"
               in c for c in fleet.corrections)


def test_an_operator_stage_starts_without_a_unit_that_went_offline_during_it():
    clock = Clock()
    fleet = _preset_fleet(clock)
    fleet.start_show(lead_s=3.0, preset_first=True)
    fleet.links["radxa-02"].online = False
    _start_after_paint(fleet, clock, "radxa-01")
    clock.now += 45.0
    fleet._staging_tick()
    assert fleet.run is not None
    assert [n for n, _ in _posted(fleet, "/show/run")] == ["radxa-01"]
    assert any("countdown running without radxa-02 (not ready: radxa-02: not answering)"
               in c for c in fleet.corrections)


def test_a_seek_during_the_stage_says_so_once():
    # LOW-3
    clock = Clock()
    fleet = _preset_fleet(clock)
    fleet.start_show(lead_s=3.0, preset_first=True)
    fleet.seek(30.0)
    assert fleet.preset_state() is None
    assert len([c for c in fleet.corrections if "preset stage was called off" in c]) == 1
    _start_after_paint(fleet, clock, "radxa-01", "radxa-02")
    assert fleet.run is None
    assert len([c for c in fleet.corrections if "preset stage was called off" in c]) == 1


def test_two_starts_together_do_not_both_stage():
    # LOW-7: the check-and-set is under the run lock, before the posts.
    clock = Clock()
    fleet = _preset_fleet(clock)
    gate = threading.Event()
    entered = threading.Event()
    link = fleet.links["radxa-01"]
    real_post = link.post

    def slow_post(path, body, learn=True, timeout=None):
        entered.set()
        gate.wait(5)
        return real_post(path, body, learn, timeout)
    link.post = slow_post
    first = threading.Thread(target=fleet.start_show,
                             kwargs={"lead_s": 3.0, "preset_first": True})
    first.start()
    try:
        assert entered.wait(5)
        with pytest.raises(ValueError, match="preset first"):
            fleet.start_show(lead_s=3.0, preset_first=True)
    finally:
        gate.set()
        first.join(5)
    assert len(_posted(fleet, "/show/preset")) == 2      # one stage's worth


def test_the_end_of_show_clear_waits_for_a_loop_stage():
    # During a Loop restart's PRESET stage the ended run is still `run` and
    # the Loop wait is over: the end's clear must not empty the slots the
    # countdown's 0:00 is about to read.
    clock = Clock()
    fleet = _preset_fleet(clock, settings=lambda: (0.0, 3.0, True))
    for show in fleet.shows.values():
        show["clear_after_show"] = True
    fleet.start_show(lead_s=3.0)
    clock.now = fleet.run["t0"] + 100.0
    fleet._loop_tick()
    fleet._loop_tick()
    assert fleet.preset_state() is not None
    clock.now += 10.0
    link = fleet.links["radxa-01"]
    assert fleet._clear_after_end(link, dict(fleet.run), fleet.shows["radxa-01"]) is False
    assert _posted(fleet, "/show/clear") == []


def test_server_gates_refuse_during_the_stage(tmp_path):
    # LOW-4: Upload, demo save, Clear pictures and the tar import refuse
    # while a START is presetting, as they do during a run.
    ws = _workspace(tmp_path / "ws", music=False)
    ws.set_preset_before_start(True)
    clock = Clock()
    fleet = _preset_fleet(clock)
    server = make_server(ws.root, port=0, fleet=fleet)
    port = _serve(server)
    try:
        status, answer = _post(port, "/api/fleet/start", {"lead_s": 3})
        assert answer.get("staged") is True, answer
        for path, body in (("/api/fleet/upload", {"force": True}),
                           ("/api/fleet/write_demo", {"name": "demo", "loop": False}),
                           ("/api/fleet/clear_pictures", {})):
            status, answer = _post(port, path, body)
            assert status == 400 and answer["error"] == (
                "the show is starting (preset first) - STOP it first"), (path, answer)
        packed = io.BytesIO()
        ws.export_tar(packed)
        status, answer = _post(port, "/api/workspace/import", packed.getvalue())
        assert status == 409 and "preset first" in answer["error"]
        assert fleet.preset_state() is not None
    finally:
        server.shutdown()
        server.server_close()


class StillRunningLink(StubLink):
    """A unit whose last run has not flipped to ENDED yet: it refuses
    /show/preset with "the show is running" `refusals` times, then takes it
    (None: for ever)."""

    def __init__(self, name, refusals):
        super().__init__(name, "running")
        self.refusals = refusals

    def post(self, path, body, learn=True, timeout=None):
        if path == "/show/preset":
            self.posted.append((path, body))
            if self.refusals is None or self.refusals > 0:
                if self.refusals:
                    self.refusals -= 1
                raise RuntimeError("the show is running")
            self.status["show"]["state"] = "ended"
            return {}
        return super().post(path, body, learn, timeout)


def _still_running_fleet(clock, refusals):
    fleet = _preset_fleet(clock, settings=lambda: (0.0, 3.0, True))
    link = StillRunningLink("radxa-02", refusals)
    link.status["show"].update(id="showA", burn={"state": "burned"},
                               applied="q09", dirty=False)
    fleet.links["radxa-02"] = link
    return fleet


def test_a_unit_not_ended_yet_is_asked_again_and_waited_for():
    # Loop wait 0: the restart's preset lands before radxa-02 has turned
    # ENDED. It is asked again every 0.5 s - not started without its look.
    clock = Clock()
    fleet = _still_running_fleet(clock, refusals=2)
    results = fleet.start_show(lead_s=3.0, preset_first=True)
    # Not an error in the START's reply: it is being asked again.
    assert results["radxa-02"] == {"ok": True, "preset": True, "retrying": True,
                                   "why": "the show is running"}
    state = fleet.preset_state()
    assert state["waited"] == 2
    assert state["refused"] == {} and state["retrying"] == ["radxa-02"]
    assert "radxa-02" in state["waiting_for"]
    _paint(fleet, "radxa-01")
    for _ in range(4):                                   # 0.5 s, 1.0 s, ...
        clock.now += 0.5
        fleet._staging_tick()
    presets = [n for n, _ in _posted(fleet, "/show/preset")]
    assert presets.count("radxa-02") == 3                # 2 refused, then taken
    assert fleet.preset_state()["retrying"] == [] and fleet.preset_state()["refused"] == {}
    clock.now += 9.0                                     # 01 long painted, 02 not yet
    fleet._staging_tick()
    assert fleet.run is None and fleet.preset_state()["waiting_for"] == ["radxa-02"]
    _start_after_paint(fleet, clock, "radxa-02")
    assert fleet.run is not None and fleet.run["t0"] == clock.now + 3.0
    assert not any("refused the preset" in c for c in fleet.corrections)
    assert "preset_left_out" not in fleet.run


def test_a_unit_that_keeps_refusing_is_left_out_of_the_wait_after_10_s():
    clock = Clock()
    fleet = _still_running_fleet(clock, refusals=None)
    fleet.start_show(lead_s=3.0, preset_first=True)
    _paint(fleet, "radxa-01")
    for _ in range(19):                                  # 9.5 s: still asking
        clock.now += 0.5
        fleet._staging_tick()
    assert fleet.preset_state()["retrying"] == ["radxa-02"] and fleet.run is None
    clock.now += 0.6                                     # past 10 s
    fleet._staging_tick()
    # Refused now - counted out of the wait, so the start goes in this tick
    # (radxa-01 painted long ago).
    assert fleet.preset_state() is None and fleet.run is not None
    assert any("radxa-02 refused the preset (the show is running, still after 10 s)"
               " - not waited for" in c for c in fleet.corrections)
    assert "radxa-02" in [n for n, _ in _posted(fleet, "/show/run")]
    left = fleet.snapshot()["run"]["preset_left_out"]
    assert left == {"at": fleet.run["t0"], "units": {
        "radxa-02": "refused the preset: the show is running, still after 10 s"}}


def test_a_loose_test_look_after_the_preset_is_preset_again():
    # MED-A: (2) preset, then a loose cue fired / prepared on a garment: its
    # 0:00 look is no longer vouched for, so (3) presets it again.
    clock = Clock()
    fleet = _preset_fleet(clock)
    fleet.preset()
    clock.now += 10.0
    _paint(fleet, "radxa-01", "radxa-02")
    fleet.fire({"radxa-01": "test"})
    fleet.start_show(lead_s=11.0, preset_first=True)
    assert fleet.preset_state()["already"] == ["radxa-02"]
    assert [n for n, _ in _posted(fleet, "/show/preset")].count("radxa-01") == 2
    fleet.stop_show()
    fleet._preset_at = {"radxa-01": clock.now - 20.0, "radxa-02": clock.now - 20.0}
    fleet.prepare({"radxa-02": {"cue": "t", "boards": {}}})
    assert "radxa-02" not in fleet._preset_at and "radxa-01" in fleet._preset_at


def test_no_run_correction_during_a_stage_over_an_ended_run():
    # MED-B: the ended run is still `run` during a Loop restart's preset; a
    # unit coming back must not be sent /show/run on its old T0 (it would
    # fire the LAST cue over the 0:00 preset).
    clock = Clock()
    fleet = _preset_fleet(clock, settings=lambda: (0.0, 3.0, True))
    fleet.start_show(lead_s=3.0)
    clock.now = fleet.run["t0"] + 101.0                  # just past the end
    fleet._loop_tick()
    fleet._loop_tick()
    assert fleet.preset_state() is not None and fleet.run is not None
    link = fleet.links["radxa-02"]
    link.status["show"].update(state="stopped", t0=None)  # came back, not running
    link.posted.clear()
    fleet._corrected.clear()
    fleet._supervise(link)
    assert [p for p, _ in link.posted if p == "/show/run"] == []
    # Without a stage the same unit IS put back into the run (the control).
    fleet.stop_show()
    fleet.start_show(lead_s=3.0)
    clock.now += 5.0
    link.status["show"].update(state="stopped", t0=None)
    link.posted.clear()
    fleet._corrected.clear()
    fleet._supervise(link)
    assert [p for p, _ in link.posted if p == "/show/run"] == ["/show/run"]


def test_an_inflight_preset_never_reported_is_sent_again_once():
    # LOW: (3) right after (2), and radxa-02 never reports the look: when the
    # in-flight window (8 + 0.5 + 5 s) is over it is preset again - once -
    # instead of being waited for until 45 s.
    clock = Clock()
    fleet = _preset_fleet(clock)
    fleet.preset()
    clock.now += 2.0
    fleet.start_show(lead_s=11.0, preset_first=True)
    _paint(fleet, "radxa-01")
    clock.now += 11.0                                    # 13 s since (2): not yet
    fleet._staging_tick()
    assert [n for n, _ in _posted(fleet, "/show/preset")].count("radxa-02") == 1
    clock.now += 0.6                                     # 13.6 s: window over
    fleet._staging_tick()
    assert [n for n, _ in _posted(fleet, "/show/preset")].count("radxa-02") == 2
    assert any("radxa-02 never reported the 0:00 look" in c for c in fleet.corrections)
    clock.now += 1.0
    fleet._staging_tick()
    assert [n for n, _ in _posted(fleet, "/show/preset")].count("radxa-02") == 2
    _start_after_paint(fleet, clock, "radxa-02")
    assert fleet.run is not None and fleet.run["t0"] == clock.now + 11.0
