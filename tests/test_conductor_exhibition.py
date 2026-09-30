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
import tarfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from conductor import __main__ as cli
from conductor.fleet import Fleet
from conductor.server import (LOOP_WAIT_S, WORKSPACE_TAR_MAX, Workspace,
                              check_loop_wait, loop_wait_of, make_server,
                              parse_conductor_address, reachable_urls)
from conductor.speaker import LATENCY_SAMPLES, Speaker
from tests.test_fleet import StubLink
from tests.test_look import GRID, MAP


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
                   speaker=False, speaker_lead_ms=None, speaker_factory=None):
        seen.update(workspace=workspace, port=port, host=host, speaker=speaker,
                    lead=speaker_lead_ms)
        return 0

    import conductor.server
    monkeypatch.setattr(conductor.server, "serve", fake_serve)
    assert cli.main(["serve"]) == 0
    assert seen["host"] == "127.0.0.1" and seen["speaker"] is False
    assert cli.main(["serve", "--host", "0.0.0.0", "--speaker",
                     "--speaker-lead-ms", "80", "--port", "8765",
                     "--workspace", "/home/radxa/exhibition"]) == 0
    assert seen == {"workspace": "/home/radxa/exhibition", "port": 8765,
                    "host": "0.0.0.0", "speaker": True, "lead": 80.0}


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

def test_loop_wait_is_ten_to_six_hundred_seconds_or_off():
    assert check_loop_wait(None) is None
    assert check_loop_wait(30) == 30.0
    assert check_loop_wait("６０") == 60.0             # NFKC, like the countdown
    assert check_loop_wait("12.34") == 12.3
    for bad in (9.9, 600.1, True, "abc", "", [], {}, "0x10", float("nan")):
        with pytest.raises(ValueError, match="10 to 600"):
            check_loop_wait(bad)
    assert loop_wait_of({}) is None
    assert loop_wait_of({"loop_wait_s": "junk"}) is None
    assert loop_wait_of({"loop_wait_s": 45}) == 45.0


def test_loop_is_stored_with_the_show_off_as_no_key_and_undoable(tmp_path):
    ws = _workspace(tmp_path / "ws", music=False)
    assert ws.loop_wait() is None and ws.loop_settings() is None
    assert "loop_wait_s" not in ws._load_show()
    ws.set_loop(45)
    assert ws.loop_wait() == 45.0
    assert ws.loop_settings() == (45.0, 11.0)          # the wait, the countdown
    ws.set_start_countdown(20)
    assert ws.loop_settings() == (45.0, 20.0)
    assert ws.state()["show"]["loop_wait_s"] == 45.0
    assert ws.state()["show"]["loop_default_s"] == LOOP_WAIT_S
    ws.set_loop(None)
    assert "loop_wait_s" not in ws._load_show()
    assert ws.undo() and ws.loop_wait() == 45.0
    assert ws.redo() and ws.loop_wait() is None
    with pytest.raises(ValueError):
        ws.set_loop(5)
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
    b.set_loop(30)
    without = {k: v for k, v in exported.items() if k != "loop_wait_s"}
    b.import_show(without)
    assert b.loop_wait() == 30.0
    with pytest.raises(ValueError):
        b.import_show(dict(exported, loop_wait_s=2))


def test_post_api_loop_answers_the_fleet_loop_object(tmp_path):
    ws = _workspace(tmp_path / "ws", music=False)
    fleet = Fleet({})
    server = make_server(ws.root, port=0, fleet=fleet)
    port = _serve(server)
    try:
        status, before, _ = _get(port, "/api/fleet")
        assert json.loads(before)["loop"] == {"on": False, "wait_s": 30,
                                              "next_in_s": None, "runs": 0,
                                              "problem": None}
        status, loop = _post(port, "/api/loop", {"on": True})
        assert status == 200 and loop["on"] and loop["wait_s"] == 30
        status, loop = _post(port, "/api/loop", {"on": True, "wait_s": 45})
        assert status == 200 and loop == {"on": True, "wait_s": 45,
                                          "next_in_s": None, "runs": 0,
                                          "problem": None}
        assert ws.loop_wait() == 45.0
        assert json.loads(_get(port, "/api/fleet")[1])["loop"]["on"] is True
        status, loop = _post(port, "/api/loop", {"on": False, "wait_s": 45})
        assert status == 200 and loop["on"] is False and loop["wait_s"] == 30
        assert ws.loop_wait() is None
        for bad in ({}, {"on": "yes"}, {"on": True, "wait_s": 3},
                    {"on": True, "wait_s": "x"}):
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
    assert pending["next_in_s"] == 5.0
    assert "radxa-02: not answering" in pending["problem"]
    said = [line for line in fleet.corrections if "cannot start again" in line]
    assert len(said) == 1
    clock.now += 5.0
    fleet._loop_tick()
    assert fleet.run["loops"] == 0 and fleet.loop_state()["next_in_s"] == 5.0
    assert len([l for l in fleet.corrections if "cannot start again" in l]) == 1
    fleet.links["radxa-02"].online = True
    clock.now += 5.0
    fleet._loop_tick()
    assert fleet.run["loops"] == 1 and fleet.loop_state() is None


def test_the_loop_keeps_the_runs_force_and_never_clears_between_runs():
    clock = Clock()
    fleet = _fleet(clock, lambda: (30.0, 11.0), clear_after_move_s=0.0)
    for show in fleet.shows.values():
        show["clear_after_show"] = True
    fleet.start_show(lead_s=1.0, force=True)
    assert fleet.run["clear_after_show"] is True
    clock.now = fleet.run["t0"] + 100.0
    fleet._loop_tick()
    # The END clear would go out CLEAR_AFTER_END_S past the end; with a
    # restart pending it stays where it is.
    clock.now += 10.0
    link = fleet.links["radxa-01"]
    assert fleet._clear_after_end(link, dict(fleet.run), fleet.shows["radxa-01"]) is False
    assert _posted(fleet, "/show/clear") == []
    clock.now += 20.0
    fleet._loop_tick()
    assert fleet.run["loops"] == 1 and fleet.run["force"] is True
    assert all(body["force"] for _, body in _posted(fleet, "/show/run"))
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
            self.position = 0.0
            self.state = 2 if (word in ("L", "LOAD") or self.plays_on_load) else 1
            self._say("@I ID3:fake")
            self._say(f"@P {self.state}")
        elif word == "P":
            if self.state == 0:
                self._say("@E No track loaded!")
                return
            if self.delay_s:
                time.sleep(self.delay_s)
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
    assert stage.speaker.status() == {"available": False, "error": None,
                                      "state": "idle", "track": None,
                                      "latency_ms": stage.speaker.status()["latency_ms"],
                                      "playing": False,
                                      "log": stage.speaker.status()["log"]}


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
    assert made["fleet"]["loop"] == {"on": False, "wait_s": 30, "next_in_s": None,
                                     "runs": 0, "problem": None}
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
        assert answer["problems"] == []
        assert b.loop_wait() == 60.0
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
    a = _workspace(tmp_path / "a", music=False)
    server = make_server(tmp_path / "b", port=0, fleet=Fleet({}), token="s3cret")
    port = _serve(server)
    try:
        packed = io.BytesIO()
        a.export_tar(packed)
        assert _get(port, "/api/workspace/export")[0] == 401
        assert _get(port, "/api/workspace/export", {"X-Show-Token": "wrong"})[0] == 401
        assert _get(port, "/api/workspace/export", {"X-Show-Token": "s3cret"})[0] == 200
        assert _post(port, "/api/workspace/import", packed.getvalue())[0] == 401
        assert _post(port, "/api/workspace/import", packed.getvalue(),
                     {"X-Show-Token": "s3cret"})[0] == 200
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
        assert answer["units"]["radxa-01"] == {"ok": True, "scheduled": True, "after_s": 20.0}
        assert answer["units"]["radxa-02"] == {"ok": False, "error": "a show is running on this unit"}
        assert answer["units"]["radxa-03"] == {"ok": False, "error": "offline"}
        assert answer["units"]["radxa-05"]["ok"]
        told = [name for name, path, _, _ in order if path == "/wifi/select"]
        assert told[-1] == "radxa-05" and set(told[:-1]) == {"radxa-01", "radxa-02"}
        assert all(body == {"profile": "AZ-Epaper", "after_s": 20.0} and learn is False
                   for _, _, body, learn in order)
        for bad in ({}, {"profile": ""}, {"profile": "x", "after_s": 1},
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
