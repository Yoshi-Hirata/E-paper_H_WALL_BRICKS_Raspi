"""Standalone demos: ui/demos.py's store, the /demo/* endpoints, and the
menu row + DEMO screen that let the unit play one on its own clock.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ui.app as app_module
from ui.agent import Agent
from ui.app import App, DemoRow, Screen
from ui.demos import MAX_DEMOS, DemoStore, slugify
from ui.display import NullDisplay
from ui.inputs import ScriptedInput
from ui.remote import RemoteError
from ui.showplay import ENDED, LOADED, RUNNING, STOPPED, ShowPlayer

from tests.test_showplay import REFRESH, events, make_show, ordered_bus, show_times
from tests.test_ui_remote import call, make_session, shows, wait_until

ordered_bus = ordered_bus            # re-exported: keeps the autouse fixture


# ---- DemoStore ----

def test_save_list_load_delete(tmp_path):
    store = DemoStore(tmp_path)
    show = make_show()
    slug = store.save("DEMO PARIS", show)
    assert slug == "demo-paris"
    listed = store.list()
    assert len(listed) == 1
    entry = listed[0]
    assert entry["slug"] == slug and entry["name"] == "DEMO PARIS"
    assert entry["cues"] == 3 and entry["loop"] is False
    assert entry["duration"] == show["duration"]
    assert store.load(slug) == show
    store.delete(slug)
    assert store.list() == []


def test_slug_collision_with_a_different_name_gets_a_suffix(tmp_path):
    store = DemoStore(tmp_path)
    show = make_show()
    a = store.save("Demo Paris!", show)
    b = store.save("DEMO, PARIS", show)
    assert a == "demo-paris" and b == "demo-paris-2"
    # The same name again updates that one demo in place, not a 3rd slug.
    c = store.save("Demo Paris!", dict(show, name="v2"), loop=True)
    assert c == a
    entries = {e["slug"]: e for e in store.list()}
    assert len(entries) == 2 and entries[a]["loop"] is True


def test_slugify_falls_back_on_a_name_with_nothing_left():
    assert slugify("!!!") == "demo"
    assert slugify("  Demo   Paris!  ") == "demo-paris"


def test_a_21st_demo_is_refused_but_overwriting_one_at_the_cap_is_not(tmp_path):
    store = DemoStore(tmp_path)
    show = make_show()
    for n in range(MAX_DEMOS):
        store.save(f"demo{n}", show)
    with pytest.raises(RemoteError, match="demo store full"):
        store.save("one too many", show)
    assert len(store.list()) == MAX_DEMOS
    store.save("demo0", dict(show, name="v2"))       # fine: not a new slug
    assert len(store.list()) == MAX_DEMOS


def test_save_refuses_a_blank_name_or_a_bad_show(tmp_path):
    store = DemoStore(tmp_path)
    with pytest.raises(RemoteError):
        store.save("   ", make_show())
    with pytest.raises(RemoteError):
        store.save("ok", {"id": "x", "cues": []})
    assert store.list() == []


def test_loading_an_unknown_slug_is_refused(tmp_path):
    store = DemoStore(tmp_path)
    with pytest.raises(RemoteError):
        store.load("nope")


def test_a_slug_that_is_not_a_slug_is_refused_not_walked(tmp_path):
    # "/demo/delete" takes a slug straight from the HTTP body; ".." or a
    # leading "/" must never turn into a path outside `store.root`.
    store = DemoStore(tmp_path)
    for bad in ("../show-run", "../../etc/passwd", "", "-leading-hyphen",
               "UPPER", "a/b", "."):
        with pytest.raises(RemoteError):
            store.load(bad)
        with pytest.raises(RemoteError):
            store.delete(bad)
    assert not (tmp_path.parent / "show-run.json").exists()


def test_deleting_an_unknown_but_well_formed_slug_is_refused(tmp_path):
    store = DemoStore(tmp_path)
    with pytest.raises(RemoteError):
        store.delete("nope")          # not a silent {"ok": True}


# ---- the agent's /demo/* endpoints ----

@pytest.fixture
def rig(tmp_path):
    session, runner, bus = make_session()
    player = ShowPlayer(session, store=tmp_path / "player", save_s=0.01,
                        margin_s=0.1, grace_s=0.1, tick_s=0.02,
                        setup_s=0.05, setup_board_s=0.0)
    demos = DemoStore(tmp_path / "demos")
    agent = Agent(session, port=0, host="127.0.0.1", commit="abc1234",
                  name="radxa-03", player=player, demos=demos)
    agent.start()
    yield agent, session, runner, bus, player, demos
    agent.stop()
    player.close()
    runner.stop()


def test_demo_save_list_delete_over_http(rig):
    agent, session, runner, bus, player, demos = rig
    show = make_show()
    code, body = call(agent, "/demo/save",
                      {"name": "DEMO PARIS", "loop": True, "show": show})
    assert code == 200 and body["ok"] and body["slug"] == "demo-paris"
    assert body["demos"] == [{"slug": "demo-paris", "name": "DEMO PARIS",
                              "cues": 3, "duration": show["duration"],
                              "loop": True, "show_id": show["id"],
                              "saved_at": body["demos"][0]["saved_at"]}]
    assert call(agent, "/demo/list")[1]["demos"] == body["demos"]
    assert call(agent, "/status")[1]["demos"] == 1

    code, body = call(agent, "/demo/delete", {"slug": "demo-paris"})
    assert code == 200 and body["demos"] == []
    assert call(agent, "/status")[1]["demos"] == 0


def test_demo_save_is_refused_while_a_show_runs_or_holds(rig):
    agent, session, runner, bus, player, demos = rig
    show = make_show()
    player.load(show)
    player.run(time.monotonic() + 0.3)
    code, body = call(agent, "/demo/save", {"name": "x", "show": show})
    assert code == 409 and "stop it first" in body["error"]
    player.hold()
    code, body = call(agent, "/demo/save", {"name": "x", "show": show})
    assert code == 409
    player.stop()
    code, body = call(agent, "/demo/save", {"name": "x", "show": show})
    assert code == 200 and body["ok"]


def test_show_load_is_refused_while_a_demo_plays_not_while_a_pc_show_runs(rig):
    agent, session, runner, bus, player, demos = rig
    show = make_show()
    player.load(show, demo=True)
    player.run(time.monotonic() + 0.3)
    code, body = call(agent, "/show/load", show)
    assert code == 409 and "stop it first" in body["error"]
    player.stop()

    # A PC-driven show is not a demo: /show/load while it runs is left to
    # the conductor's own supervision (fleet.py resends a mismatched id
    # even mid-show), unchanged by this feature.
    player.load(show)
    player.run(time.monotonic() + 0.3)
    code, body = call(agent, "/show/load", show)
    assert code == 200
    player.stop()


def test_show_preset_and_run_are_also_refused_while_a_demo_plays(rig):
    # The demo's show id is exactly what a PC "START" would post to
    # /show/run - without this, the PC could quietly retime the demo
    # instead of being told to stop it first.
    agent, session, runner, bus, player, demos = rig
    show = make_show()
    player.load(show, demo=True)
    player.run(time.monotonic() + 0.3)
    code, body = call(agent, "/show/preset", {})
    assert code == 409 and "stop it first" in body["error"]
    code, body = call(agent, "/show/run", {"t0": time.monotonic() + 1,
                                           "show": show["id"]})
    assert code == 409 and "stop it first" in body["error"]
    # HOLD reaches the same guard (state HOLDING, not RUNNING).
    player.hold()
    code, body = call(agent, "/show/run", {"t0": time.monotonic() + 1})
    assert code == 409
    # /show/hold and /show/stop are how the PC takes the unit back, and
    # must not themselves be blocked by the guard they satisfy.
    assert call(agent, "/show/hold", {})[0] == 200
    assert call(agent, "/show/stop", {})[0] == 200
    player.stop()

    # A PC-driven show (is_demo False) is unaffected.
    player.load(show)
    player.run(time.monotonic() + 0.3)
    assert call(agent, "/show/run", {"t0": time.monotonic() + 1,
                                     "show": show["id"]})[0] == 200
    player.stop()


def test_status_clock_is_stamped_before_the_slow_demos_read(rig):
    # Agent.status() must sample the clock before demos.list() (disk) or
    # any other variable-cost work, or the /status round trip the PC
    # times its offset from stops being symmetric.
    agent, session, runner, bus, player, demos = rig
    original_list = demos.list

    def slow_list():
        time.sleep(0.2)
        return original_list()
    demos.list = slow_list
    try:
        before = time.monotonic()
        code, status = call(agent, "/status")
        after = time.monotonic()
        assert code == 200 and after - before >= 0.2   # the slow path ran
        assert status["clock"]["mono"] - before < 0.05  # stamped up front
    finally:
        demos.list = original_list


def test_demo_delete_over_http_refuses_a_bad_or_unknown_slug(rig):
    agent, session, runner, bus, player, demos = rig
    assert call(agent, "/demo/delete", {"slug": "../show-run"})[0] == 409
    assert call(agent, "/demo/delete", {"slug": "nope"})[0] == 409
    assert call(agent, "/demo/delete", {})[0] == 409       # no slug at all


def test_status_show_carries_the_demo_flag_and_name(rig):
    agent, session, runner, bus, player, demos = rig
    show = make_show()
    player.load(show)
    status = call(agent, "/status")[1]["show"]
    assert status["demo"] is False and status["demo_name"] == ""
    player.load(show, demo=True, name="DEMO PARIS")
    status = call(agent, "/status")[1]["show"]
    assert status["demo"] is True and status["demo_name"] == "DEMO PARIS"
    # A plain (PC) load, even of the same show, is not a demo and carries
    # no name - the name is what /demo/save wrote it under, not the
    # look's own name (show["name"]).
    player.load(show)
    status = call(agent, "/status")[1]["show"]
    assert status["demo"] is False and status["demo_name"] == ""


def test_demo_routes_without_a_store_are_not_found():
    session, runner, bus = make_session()
    agent = Agent(session, port=0, host="127.0.0.1")
    agent.start()
    try:
        assert call(agent, "/demo/list")[0] == 404
        code, body = call(agent, "/demo/save", {"name": "x", "show": {}})
        assert code == 409 and "no demo store" in body["error"]
        assert call(agent, "/status")[1]["demos"] == 0
    finally:
        agent.stop()
        runner.stop()


# ---- the menu and the DEMO screen ----

def make_app(tmp_path, session, runner, player_kwargs=None, **app_kwargs):
    kwargs = dict(save_s=0.01, margin_s=0.1, grace_s=0.1, tick_s=0.02,
                 setup_s=0.05, setup_board_s=0.0)
    kwargs.update(player_kwargs or {})
    player = ShowPlayer(session, store=tmp_path / "player", **kwargs)
    session.on_release = player.stop      # ui/main.py's own wiring
    demos = DemoStore(tmp_path / "demos")
    app = App(NullDisplay(), ScriptedInput(()), runner, remote=session,
             player=player, demos=demos, host="radxa-03", **app_kwargs)
    return app, player, demos


def test_menu_gains_a_row_after_save_and_loses_it_after_delete(tmp_path):
    session, runner, bus = make_session()
    app, player, demos = make_app(tmp_path, session, runner)
    try:
        assert not any(isinstance(p, DemoRow) for p in app.patterns)
        slug = demos.save("DEMO PARIS", make_show(), loop=True)
        app.refresh_demos()
        rows = [p for p in app.patterns if isinstance(p, DemoRow)]
        assert len(rows) == 1
        row = rows[0]
        assert row.slug == slug and row.label == "DEMO PARIS" and row.loop
        assert "3 cues" in row.detail and "loop" in row.detail
        # right after STANDBY, ahead of every built-in pattern
        assert app.patterns[0].key == "standby"
        assert app.patterns[1] is row

        demos.delete(slug)
        app.refresh_demos()
        assert not any(isinstance(p, DemoRow) for p in app.patterns)
    finally:
        player.close()
        runner.stop()


def test_key1_plays_it_and_key2_stops_it(tmp_path):
    session, runner, bus = make_session()
    app, player, demos = make_app(tmp_path, session, runner)
    try:
        show = make_show(sents=(-REFRESH, 0.6), duration=5)
        slug = demos.save("DEMO PARIS", show)
        app.refresh_demos()
        app.select(f"demo:{slug}")
        app.handle("key1")
        assert app.screen is Screen.DEMO
        t0 = player.t0
        assert t0 is not None
        assert player.is_demo and player.demo_name == "DEMO PARIS"

        assert wait_until(lambda: player.applied == "q00")
        assert events(bus) == [("save", 1, 1), ("save", 2, 1), ("show",)]
        assert wait_until(lambda: player.applied == "q01", timeout=4)
        assert 0 <= show_times(bus)[1] - (t0 + 0.6) < 0.05

        app.handle("key2")
        assert app.screen is Screen.MENU
        assert player.state == STOPPED
        time.sleep(0.2)
        assert len(shows(bus)) == 2          # nothing more fired after STOP
    finally:
        player.close()
        runner.stop()


def test_key1_held_restarts_from_zero(tmp_path):
    session, runner, bus = make_session()
    app, player, demos = make_app(tmp_path, session, runner)
    try:
        show = make_show(sents=(-REFRESH, 0.6), duration=5)
        slug = demos.save("DEMO PARIS", show)
        app.refresh_demos()
        app.select(f"demo:{slug}")
        app.handle("key1")
        assert wait_until(lambda: player.applied == "q00")
        first_t0 = player.t0

        app.handle("key1_hold")
        assert app.screen is Screen.DEMO
        assert player.t0 is not None and player.t0 != first_t0
        assert wait_until(lambda: player.applied == "q00")
    finally:
        player.close()
        runner.stop()


def test_key1_hold_restarts_the_playing_demo_not_a_row_the_cursor_clamped_onto(
        tmp_path):
    session, runner, bus = make_session()
    app, player, demos = make_app(tmp_path, session, runner)
    try:
        show_a = make_show(sents=(-REFRESH, 0.6), duration=5)
        show_b = make_show(sents=(-REFRESH, 0.9), duration=5)
        slug_b = demos.save("BBB", show_b)      # saved first: lists first
        slug_a = demos.save("AAA", show_a)      # saved second: lists second
        app.refresh_demos()
        app.select(f"demo:{slug_a}")
        app.handle("key1")
        assert wait_until(lambda: player.applied == "q00")
        assert player.show["id"] == show_a["id"]

        # AAA is deleted from the PC while it plays; the row it occupied
        # is gone, and refresh_demos()'s clamp (min(selected, len-1))
        # lands the cursor on whatever now sits at that same index - a
        # built-in pattern here, since BBB is the only demo left and
        # sorts ahead of it.
        demos.delete(slug_a)
        app.refresh_demos()
        clamped = app.patterns[app.selected]
        assert clamped.key not in (f"demo:{slug_a}", f"demo:{slug_b}")

        app.handle("key1_hold")
        # Must not silently start `clamped` (a built-in demo pattern, in
        # this case - whitening or looping colours across every panel):
        # AAA's own file is also gone, so the honest outcome is falling
        # back to the menu, never a pattern or another demo's show.
        assert app.screen is Screen.MENU
        assert runner.pattern is None           # never started `clamped`
        assert player.state == STOPPED
        assert player.show["id"] == show_a["id"]        # BBB never loaded
    finally:
        player.close()
        runner.stop()


def test_key1_is_refused_while_a_pc_show_is_loaded(tmp_path):
    session, runner, bus = make_session()
    app, player, demos = make_app(tmp_path, session, runner)
    try:
        pc_show = make_show(sents=(-REFRESH, 5.0), duration=30)
        player.load(pc_show)                    # an "Upload", not a demo
        slug = demos.save("DEMO PARIS", make_show())
        app.refresh_demos()
        app.select(f"demo:{slug}")

        app.handle("key1")
        assert app.screen is Screen.MENU        # never entered DEMO
        assert player.show["id"] == pc_show["id"]   # not overwritten
        assert not player.is_demo
        assert "PC show" in app._standby_status()

        # Once the PC lets go (STOPPED), the row works normally again.
        player.stop()
        app.handle("key1")
        assert app.screen is Screen.DEMO
    finally:
        player.close()
        runner.stop()


def _pump(app, predicate, timeout=3.0, interval=0.02) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        app.tick(wait=0)
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def test_loop_restarts_after_the_gap(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "LOOP_GAP_S", 0.2)
    session, runner, bus = make_session()
    app, player, demos = make_app(tmp_path, session, runner)
    try:
        # Two cues, not one: a one-cue show's only cue is both the first
        # and the last of every run, so its "already applied" check would
        # (correctly) skip repainting an unchanged picture - not what
        # this test is after, which is that a *loop* restarts at all.
        show = make_show(sents=(-REFRESH, 0.05), duration=0.1)
        slug = demos.save("LOOPY", show, loop=True)
        app.refresh_demos()
        app.select(f"demo:{slug}")
        app.handle("key1")
        assert _pump(app, lambda: len(shows(bus)) >= 1, timeout=3)
        assert _pump(app, lambda: len(shows(bus)) >= 2, timeout=3)
        assert app.screen is Screen.DEMO       # never left DEMO to loop
    finally:
        player.close()
        runner.stop()


def test_without_loop_it_stays_ended_until_key2(tmp_path):
    session, runner, bus = make_session()
    app, player, demos = make_app(tmp_path, session, runner)
    try:
        show = make_show(sents=(-REFRESH,), duration=0.2)
        slug = demos.save("ONESHOT", show, loop=False)
        app.refresh_demos()
        app.select(f"demo:{slug}")
        app.handle("key1")
        assert _pump(app, lambda: len(shows(bus)) >= 1, timeout=3)
        time.sleep(0.6)
        app.tick(wait=0)
        assert len(shows(bus)) == 1             # not repeated
        assert app.screen is Screen.DEMO
        app.handle("key2")
        assert app.screen is Screen.MENU
    finally:
        player.close()
        runner.stop()


def test_a_locked_unit_ignores_a_demo_row(tmp_path):
    session, runner, bus = make_session()
    app, player, demos = make_app(tmp_path, session, runner, locked=True)
    try:
        slug = demos.save("DEMO PARIS", make_show())
        app.refresh_demos()
        app.select(f"demo:{slug}")
        app.handle("key1")
        assert app.screen is Screen.MENU
        assert player.state != "running"
    finally:
        player.close()
        runner.stop()


def test_without_a_player_or_store_the_menu_has_no_demo_rows():
    runner = make_session()[1]
    app = App(NullDisplay(), ScriptedInput(()), runner, port_label="x")
    assert not any(isinstance(p, DemoRow) for p in app.patterns)
    app.refresh_demos()                # no-op: nothing crashes
    assert not any(isinstance(p, DemoRow) for p in app.patterns)
    runner.stop()


# ---- found in the stage-safety review (2026-09-24) ----

def test_a_pc_show_uploaded_over_an_ended_looping_demo_is_never_repainted(
        tmp_path, monkeypatch):
    """The scenario the review flagged as a BLOCKER: a looping demo ends,
    the operator walks away, the PC uploads and starts the real show
    while the demo sits ENDED (allowed - /show/load only refuses a demo
    that is RUNNING/HOLDING), the real show also ends, and LOOP_GAP_S
    later the demo must not fire over it - nor must the LCD have stayed
    away from Screen.REMOTE while the PC's show was live."""
    monkeypatch.setattr(app_module, "LOOP_GAP_S", 0.2)
    session, runner, bus = make_session()
    app, player, demos = make_app(tmp_path, session, runner)
    try:
        demo_show = make_show(sents=(-REFRESH,), duration=0.15)
        slug = demos.save("LOOPY", demo_show, loop=True)
        app.refresh_demos()
        app.select(f"demo:{slug}")
        app.handle("key1")
        assert _pump(app, lambda: player.status()["state"] == ENDED,
                     timeout=3)
        assert app.screen is Screen.DEMO

        # The PC takes over mid-ENDED, exactly as fleet.py's supervise()
        # or an operator's Upload+START would: /show/load then /show/run.
        pc_show = make_show(sents=(-REFRESH, 0.9), duration=5)
        player.load(pc_show)                    # demo=False: not ours
        player.run(time.monotonic() + 0.3)
        assert _pump(app, lambda: app.screen is Screen.REMOTE, timeout=2)
        assert app._playing_demo is None        # let go of, not just hidden

        marker = len(shows(bus))
        time.sleep(0.5)                         # LOOP_GAP_S and then some
        app.tick(wait=0)
        assert player.show["id"] == pc_show["id"]     # still the PC's show
        assert len(shows(bus)) >= marker              # the PC's own cue(s)
        # No extra fire came from the demo re-loading itself over this.
        assert player.is_demo is False
    finally:
        player.close()
        runner.stop()


def test_restore_of_a_demo_clears_is_demo_and_never_auto_resumes(tmp_path):
    session, runner, bus = make_session()
    player = ShowPlayer(session, store=tmp_path, save_s=0.01, margin_s=0.1,
                        grace_s=0.1, tick_s=0.02, setup_s=0.05,
                        setup_board_s=0.0)
    try:
        show = make_show(sents=(-REFRESH, 5.0), duration=30)
        player.load(show, demo=True)
        player.run(time.monotonic() + 0.2)
        assert wait_until(lambda: player.state == RUNNING)
        player.close()

        session2, runner2, _ = make_session()
        reborn = ShowPlayer(session2, store=tmp_path, tick_s=0.02)
        try:
            reborn.restore()
            assert reborn.state == LOADED and reborn.is_demo is False

            run = json.loads((tmp_path / "show-run.json")
                             .read_text(encoding="utf-8"))
            assert run["demo"] is False

            # A second reboot (now mid-nothing, since it never resumed)
            # does not take the demo branch again either.
            reborn.close()
            session3, runner3, _ = make_session()
            reborn2 = ShowPlayer(session3, store=tmp_path, tick_s=0.02)
            try:
                reborn2.restore()
                assert reborn2.state == LOADED and reborn2.is_demo is False
            finally:
                reborn2.close()
                runner3.stop()
        finally:
            runner2.stop()
    finally:
        player.close()
        runner.stop()


def test_a_foreign_json_file_in_the_store_is_skipped_not_fatal(tmp_path):
    """A `*.json` that is not an object (null, a list) must not raise out of
    list() - that path runs on every /status poll and every LCD tick."""
    store = DemoStore(tmp_path)
    (tmp_path / "junk.json").write_text("null", encoding="utf-8")
    (tmp_path / "other.json").write_text("[1, 2]", encoding="utf-8")
    assert store.list() == []
    assert store.list() == []          # remembered, not re-parsed each time
    assert store._unreadable == {"junk", "other"}


def test_a_save_whose_sidecar_fails_leaves_nothing_behind(tmp_path, monkeypatch):
    store = DemoStore(tmp_path)
    real_write = store._write
    calls = []

    def flaky(path, payload):
        calls.append(path.name)
        if path.name.endswith(".meta.json"):
            raise OSError(28, "No space left on device")
        real_write(path, payload)
    monkeypatch.setattr(store, "_write", flaky)
    with pytest.raises(RemoteError):
        store.save("DEMO PARIS", make_show())
    assert list(tmp_path.glob("*.json")) == []     # whole or not at all
    assert store.list() == []

