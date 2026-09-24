"""Standalone demos: ui/demos.py's store, the /demo/* endpoints, and the
menu row + DEMO screen that let the unit play one on its own clock.
"""

from __future__ import annotations

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
from ui.showplay import STOPPED, ShowPlayer

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
                              "loop": True,
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
