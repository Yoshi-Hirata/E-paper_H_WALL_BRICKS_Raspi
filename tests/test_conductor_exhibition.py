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
                   speaker_factory=None, passcode=None, adopt=False):
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

def test_loop_wait_is_ten_to_six_hundred_seconds_or_off():
    assert check_loop_wait(None) is None
    assert check_loop_wait(40) == 40.0
    assert check_loop_wait("６０") == 60.0             # NFKC, like the countdown
    assert check_loop_wait("42.34") == 42.3
    assert LOOP_WAIT_S == 45.0
    # Below 40 s the seam between two runs can leave the master without a
    # STOP for over 60 s (its idle STOP comes at L+15..41 and needs 5 s).
    for bad in (39.9, 30, 600.1, True, "abc", "", [], {}, "0x10", float("nan")):
        with pytest.raises(ValueError, match="40 to 600"):
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
    b.set_loop(50)
    without = {k: v for k, v in exported.items() if k != "loop_wait_s"}
    b.import_show(without)
    assert b.loop_wait() == 50.0
    with pytest.raises(ValueError):
        b.import_show(dict(exported, loop_wait_s=2))


def test_post_api_loop_answers_the_fleet_loop_object(tmp_path):
    ws = _workspace(tmp_path / "ws", music=False)
    fleet = Fleet({})
    server = make_server(ws.root, port=0, fleet=fleet)
    port = _serve(server)
    try:
        status, before, _ = _get(port, "/api/fleet")
        assert json.loads(before)["loop"] == {"on": False, "wait_s": 45,
                                              "next_in_s": None, "runs": 0,
                                              "problem": None}
        status, loop = _post(port, "/api/loop", {"on": True})
        assert status == 200 and loop["on"] and loop["wait_s"] == 45
        status, loop = _post(port, "/api/loop", {"on": True, "wait_s": 45})
        assert status == 200 and loop == {"on": True, "wait_s": 45,
                                          "next_in_s": None, "runs": 0,
                                          "problem": None}
        assert ws.loop_wait() == 45.0
        assert json.loads(_get(port, "/api/fleet")[1])["loop"]["on"] is True
        status, loop = _post(port, "/api/loop", {"on": False, "wait_s": 45})
        assert status == 200 and loop["on"] is False and loop["wait_s"] == 45
        assert ws.loop_wait() is None
        for bad in ({}, {"on": "yes"}, {"on": True, "wait_s": 3},
                    {"on": True, "wait_s": "x"}, {"on": True, "wait_s": 39}):
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
