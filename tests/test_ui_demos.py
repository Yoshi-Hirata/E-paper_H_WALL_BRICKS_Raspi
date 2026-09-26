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
from ui.showplay import ENDED, HOLDING, LOADED, RUNNING, STOPPED, ShowPlayer

from tests.test_showplay import (REFRESH, events, make_show, ordered_bus,
                                 saved_pairs, show_times, wait_burned)
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
    assert wait_burned(player)
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
    assert wait_burned(player)
    player.run(time.monotonic() + 0.3)
    code, body = call(agent, "/show/load", show)
    assert code == 409 and "stop it first" in body["error"]
    player.stop()

    # A PC-driven show is not a demo: /show/load while it runs is left to
    # the conductor's own supervision (fleet.py resends a mismatched id
    # even mid-show), unchanged by this feature.
    player.load(show)
    assert wait_burned(player)
    player.run(time.monotonic() + 0.3)
    code, body = call(agent, "/show/load", show)
    assert code == 200
    player.stop()


def test_show_load_and_run_are_refused_while_a_demo_is_still_burning(tmp_path):
    # Review F7: a demo LOADED and still writing its pictures after KEY1
    # was not "playing" to the agent, so the PC's Upload could /show/load
    # over it and its START could retime it.
    session, runner, bus = make_session(_SlowSaveBus())
    player = ShowPlayer(session, store=tmp_path / "player", tick_s=0.02)
    demos = DemoStore(tmp_path / "demos")
    agent = Agent(session, port=0, host="127.0.0.1", commit="abc1234",
                  name="radxa-03", player=player, demos=demos)
    agent.start()
    try:
        demo = make_show(sents=tuple(n * 0.01 for n in range(18)), duration=1)
        player.load(demo, demo=True, name="DEMO PARIS")    # what KEY1 does
        assert player.state == LOADED
        assert player.status()["burn"]["state"] == "burning"
        code, body = call(agent, "/show/load", make_show())
        assert code == 409 and "stop it first" in body["error"]
        code, body = call(agent, "/show/run", {"t0": time.monotonic() + 1,
                                               "show": demo["id"]})
        assert code == 409 and "stop it first" in body["error"]
        code, body = call(agent, "/show/preset", {})
        assert code == 409 and "stop it first" in body["error"]
        assert player.is_demo and player.show["id"] == demo["id"]
        # /show/stop is how the PC takes the unit back - then it loads.
        code, status = call(agent, "/show/stop", {})
        assert code == 200 and status["show"]["burn"]["state"] == "cancelled"
        code, status = call(agent, "/show/load", make_show())
        assert code == 200 and status["show"]["demo"] is False
    finally:
        agent.stop()
        player.close()
        runner.stop()


def test_show_preset_and_run_are_also_refused_while_a_demo_plays(rig):
    # The demo's show id is exactly what a PC "START" would post to
    # /show/run - without this, the PC could quietly retime the demo
    # instead of being told to stop it first.
    agent, session, runner, bus, player, demos = rig
    show = make_show()
    player.load(show, demo=True)
    assert wait_burned(player)
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
    assert wait_burned(player)
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


def test_show_load_reply_already_shows_burning_for_the_new_show(rig):
    # The conductor's fleet-wide START gate polls status.show.burn right
    # after /show/load's own reply - it must already say "burning" (with
    # the total worked out) for THIS show, not "burned" from a previous
    # one or state that has not caught up yet. load() calls
    # RemoteSession.burn() synchronously (it only queues the write; the
    # write itself is what runs on the worker), so this is true by
    # construction, but is worth pinning down at the HTTP boundary.
    agent, session, runner, bus, player, demos = rig
    show_a = make_show()
    code, status = call(agent, "/show/load", show_a)
    assert code == 200
    assert status["show"]["id"] == show_a["id"]
    burn = status["show"]["burn"]
    assert burn is not None and burn["state"] in ("burning", "burned")
    assert burn["total"] == sum(len(c["boards"]) for c in show_a["cues"])
    assert wait_burned(player)

    # A second, different show: the reply's burn must belong to THIS
    # show, not linger as "burned" from show_a.
    show_b = make_show(sents=(-REFRESH, 0.5, 0.9, 1.3))
    code, status = call(agent, "/show/load", show_b)
    assert code == 200
    assert status["show"]["id"] == show_b["id"]
    burn = status["show"]["burn"]
    assert burn is not None
    assert burn["total"] == sum(len(c["boards"]) for c in show_b["cues"])


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
    app.show_status = player.status       # ui/main.py's own wiring too
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
        assert player.is_demo and player.demo_name == "DEMO PARIS"

        # KEY1 only starts the burn (both cues, full state, before any
        # trigger) - the App itself calls run() once it settles.
        assert _pump(app, lambda: (player.status().get("burn") or {})
                     .get("state") == "burned")
        assert events(bus) == [("save", 1, 1), ("save", 2, 1),
                               ("save", 1, 2), ("save", 2, 2)]
        assert _pump(app, lambda: player.applied == "q00")
        t0 = player.t0
        assert t0 is not None
        assert events(bus)[-1] == ("show", 1)
        assert _pump(app, lambda: player.applied == "q01", timeout=4)
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
        assert _pump(app, lambda: player.applied == "q00")
        first_t0 = player.t0

        app.handle("key1_hold")
        assert app.screen is Screen.DEMO
        # A fresh load (a new burn) - its content is unchanged, so the
        # cache skips every write, but run() still only follows once
        # _await_demo_burn() sees it settle.
        assert _pump(app, lambda: player.t0 is not None
                     and player.t0 != first_t0)
        assert _pump(app, lambda: player.applied == "q00")
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
        assert _pump(app, lambda: player.applied == "q00")
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


def test_key1_is_refused_only_while_the_pc_show_is_running_or_holding(tmp_path):
    # radxa-05, 2026-09-26: the guard used to refuse for any PC show that
    # was not STOPPED, so a unit whose restored demo had come back as a
    # plain LOADED "PC show" answered KEY1 on every demo row with a note
    # and nothing else. Only a show the PC is actually driving wins.
    session, runner, bus = make_session()
    app, player, demos = make_app(tmp_path, session, runner)
    try:
        pc_show = make_show(sents=(-REFRESH, 5.0), duration=30)
        slug = demos.save("DEMO PARIS", make_show())
        app.refresh_demos()
        app.select(f"demo:{slug}")

        player.load(pc_show)                    # an "Upload", not a demo
        assert wait_burned(player)
        player.run(time.monotonic() + 5)
        assert player.state == RUNNING
        app.handle("key1")
        assert app.screen is Screen.MENU        # never entered DEMO
        assert player.show["id"] == pc_show["id"]   # not overwritten
        assert not player.is_demo
        assert app._standby_status() == "PC show running - stop it on the PC"
        # The note is readable: the operator looks at the wall, not at
        # the 1.3" screen, so it stays up for 5 s rather than 3.
        assert app._menu_note_until - time.monotonic() > 4.0

        player.hold()                           # HOLDING is the PC's too
        assert player.state == HOLDING
        app.handle("key1")
        assert app.screen is Screen.MENU and not player.is_demo

        # ...but a PC show that is merely LOADED may be superseded: the
        # PC's own next /show/load (or the conductor's _supervise(), on
        # the id mismatch) puts it straight back.
        player.stop()
        player.load(pc_show)
        assert wait_burned(player)
        assert player.state == LOADED
        app.handle("key1")
        assert app.screen is Screen.DEMO
        assert player.is_demo and player.demo_slug == slug
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


# ---- pre-burn (2026-09-25): KEY1 burns first, then the App runs it ----

class _SlowSaveBus:
    """A FakeBus whose colour saves take a moment - long enough for the
    burn to still be "burning" the first few times something checks."""

    def __init__(self, delay=0.05):
        from tests.test_ui_runner import FakeBus

        self._bus = FakeBus()
        self._delay = delay

    def __getattr__(self, name):
        return getattr(self._bus, name)

    def request(self, frame, retries=3):
        if frame.cmd == 0x13:          # SAVE
            time.sleep(self._delay)
        return self._bus.request(frame, retries)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._bus.closed = True


def test_the_demo_screen_shows_the_burn_progress(tmp_path):
    session, runner, bus = make_session(_SlowSaveBus())
    app, player, demos = make_app(tmp_path, session, runner)
    try:
        show = make_show(sents=(-REFRESH, 0.6, 0.9), duration=5)
        slug = demos.save("DEMO PARIS", show)
        app.refresh_demos()
        app.select(f"demo:{slug}")
        app.handle("key1")
        assert app.screen is Screen.DEMO
        # Caught mid-burn at least once - "writing n/N  KEY2 cancel", not
        # the ordinary running hint, and never yet actually running.
        hint = lambda: app._demo_hint(app._remote_status())
        assert _pump(app, lambda: hint().startswith("writing "))
        assert len(hint()) <= 32               # the LCD's hint strip
        assert player.t0 is None
        assert _pump(app, lambda: player.t0 is not None)
    finally:
        player.close()
        runner.stop()


def test_key2_during_the_burn_cancels_it_and_returns_to_the_menu(tmp_path):
    session, runner, bus = make_session()
    app, player, demos = make_app(tmp_path, session, runner)
    try:
        # Many cues (many slots to write) so the burn is still running
        # when KEY2 lands, on a fake bus with no real per-frame delay.
        show = make_show(sents=tuple(n * 0.01 for n in range(18)),
                         duration=1.0)
        slug = demos.save("DEMO PARIS", show)
        app.refresh_demos()
        app.select(f"demo:{slug}")
        app.handle("key1")
        assert app.screen is Screen.DEMO
        app.handle("key2")
        assert app.screen is Screen.MENU
        assert not session.active            # release() let go of the unit
        assert wait_until(lambda: session.burn_status()["state"]
                          in ("cancelled", "burned"))
        assert player.t0 is None              # run() was never reached
    finally:
        player.close()
        runner.stop()


def test_a_burn_failure_on_a_live_board_shows_the_error_and_never_runs(
        tmp_path):
    from tests.test_showplay import RefusesSaveBus

    session, runner, bus = make_session(RefusesSaveBus(2))
    app, player, demos = make_app(tmp_path, session, runner)
    try:
        show = make_show(sents=(-REFRESH, 0.6), duration=5)
        slug = demos.save("DEMO PARIS", show)
        app.refresh_demos()
        app.select(f"demo:{slug}")
        app.handle("key1")
        assert app.screen is Screen.DEMO
        assert _pump(app, lambda: "boards failed" in app._demo_hint(
            app._remote_status()))
        assert player.t0 is None              # never ran
        assert app.screen is Screen.DEMO      # stays put until KEY2
        app.handle("key2")
        assert app.screen is Screen.MENU
    finally:
        player.close()
        runner.stop()


def test_a_burn_failure_on_only_absent_boards_still_runs(tmp_path):
    from tests.test_ui_remote import PickyBus

    session, runner, bus = make_session(PickyBus({2}))
    app, player, demos = make_app(tmp_path, session, runner)
    try:
        show = make_show(sents=(-REFRESH, 0.6), duration=5)
        slug = demos.save("DEMO PARIS", show)
        app.refresh_demos()
        app.select(f"demo:{slug}")
        app.handle("key1")
        assert _pump(app, lambda: player.applied == "q00")
        assert app._demo_burn_error is None
    finally:
        player.close()
        runner.stop()


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
        assert wait_burned(player)
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


def test_restore_of_a_running_demo_brings_it_back_running_as_a_demo(tmp_path):
    # radxa-05, 2026-09-26: the operator's standalone demo was playing
    # when the unit restarted (a USB re-plug). It came back as a plain
    # LOADED *PC* show - demo False - and from then on KEY1 on every demo
    # row was refused. A demo is restored as a demo, and one that was
    # running comes back running: that is what a showroom loop is for.
    session, runner, bus = make_session()
    player = ShowPlayer(session, store=tmp_path, save_s=0.01, margin_s=0.1,
                        grace_s=0.1, tick_s=0.02, setup_s=0.05,
                        setup_board_s=0.0)
    try:
        show = make_show(sents=(-REFRESH, 5.0), duration=30)
        player.load(show, demo=True, name="DEMO PARIS", slug="demo-paris")
        assert wait_burned(player)
        t0 = time.monotonic() - 1.0
        player.run(t0)
        assert wait_until(lambda: player.applied == "q00")
        player.close()                                  # the USB re-plug

        # What the PM verifies on the real unit, in show-run.json.
        run = json.loads((tmp_path / "show-run.json").read_text(encoding="utf-8"))
        assert run["demo"] is True and run["demo_name"] == "DEMO PARIS"
        assert run["demo_slug"] == "demo-paris"
        assert run["show"] == show["id"] and run["state"] == RUNNING

        session2, runner2, bus2 = make_session()
        reborn = ShowPlayer(session2, store=tmp_path, grace_s=0.1, tick_s=0.02)
        try:
            reborn.restore()
            assert reborn.state == RUNNING and reborn.is_demo is True
            assert reborn.demo_name == "DEMO PARIS"
            assert reborn.demo_slug == "demo-paris"
            assert reborn.restored_running          # ui/main.py: no standby
            status = reborn.status()
            assert status["demo"] is True and status["demo_name"] == "DEMO PARIS"
            assert status["demo_slug"] == "demo-paris"
            # The burn record names this very show, so nothing is written
            # again - the trigger alone puts the garment right.
            assert status["burn"] == {"done": 4, "total": 4, "failed": [],
                                      "state": "burned"}
            assert wait_until(lambda: reborn.applied == "q00", timeout=3)
            assert saved_pairs(bus2) == set()
            assert [e for e in events(bus2) if e[0] == "show"] == [("show", 1)]
            assert any("resumed the demo after a restart" in line
                       for line in runner2.recent(10))
        finally:
            reborn.close()
            runner2.stop()
    finally:
        player.close()
        runner.stop()


def test_restore_of_a_demo_that_was_only_loaded_comes_back_loaded(tmp_path):
    session, runner, bus = make_session()
    player = ShowPlayer(session, store=tmp_path, tick_s=0.02)
    try:
        show = make_show(sents=(-REFRESH, 5.0), duration=30)
        player.load(show, demo=True, name="DEMO PARIS", slug="demo-paris")
        assert wait_burned(player)
        player.close()                                  # never run

        session2, runner2, bus2 = make_session()
        reborn = ShowPlayer(session2, store=tmp_path, tick_s=0.02)
        try:
            reborn.restore()
            assert reborn.state == LOADED and reborn.is_demo is True
            assert reborn.demo_slug == "demo-paris"
            assert not reborn.restored_running    # nothing on the garment
            assert reborn.t0 is None
            time.sleep(0.2)
            assert show_times(bus2) == []         # and nothing fires
            # KEY1 is the way it starts again, and its burn is still good.
            reborn.run(time.monotonic() + 0.2)
            assert reborn.state == RUNNING and reborn.is_demo is True
            assert saved_pairs(bus2) == set()     # never re-burned
        finally:
            reborn.close()
            runner2.stop()
    finally:
        player.close()
        runner.stop()


def test_an_old_run_record_is_read_for_what_it_says_and_no_more(tmp_path):
    """Backward compatibility, both shapes of an older `show-run.json`:
    one written before demos existed (no `demo` key at all - a PC show,
    whatever is in show.json), and one written by the release before this
    fix (`demo` and `demo_name`, no `demo_slug` - still a demo, only the
    LCD cannot tell WHICH row it was)."""
    def reborn_from(record) -> "tuple[ShowPlayer, object]":
        (tmp_path / "show-run.json").write_text(json.dumps(record),
                                                encoding="utf-8")
        session, runner, _ = make_session()
        player = ShowPlayer(session, store=tmp_path, tick_s=0.02)
        player.restore()
        return player, runner

    session, runner, bus = make_session()
    player = ShowPlayer(session, store=tmp_path, tick_s=0.02)
    try:
        show = make_show(duration=30)
        player.load(show, demo=True, name="DEMO PARIS", slug="demo-paris")
        assert wait_burned(player)
        player.close()
        saved = json.loads((tmp_path / "show-run.json")
                           .read_text(encoding="utf-8"))
    finally:
        player.close()
        runner.stop()

    older = {k: v for k, v in saved.items()
             if k not in ("demo", "demo_name", "demo_slug")}
    reborn, runner2 = reborn_from(older)
    try:
        assert reborn.state == LOADED
        assert reborn.is_demo is False and reborn.demo_slug == ""
        assert reborn.status()["demo"] is False
    finally:
        reborn.close()
        runner2.stop()

    previous = {k: v for k, v in saved.items() if k != "demo_slug"}
    reborn, runner3 = reborn_from(previous)
    try:
        assert reborn.is_demo is True and reborn.demo_name == "DEMO PARIS"
        assert reborn.demo_slug == ""       # nothing to hand the LCD back
    finally:
        reborn.close()
        runner3.stop()


def test_a_run_record_naming_another_show_is_not_taken_as_a_demo(tmp_path):
    # load() writes show.json and show-run.json in that order: a power cut
    # between the two leaves a record that describes the show BEFORE this
    # one. It says nothing about this show file, so it is not believed -
    # a PC show, which the conductor may load over, rather than a demo it
    # would leave alone for ever.
    session, runner, bus = make_session()
    player = ShowPlayer(session, store=tmp_path, tick_s=0.02)
    try:
        show = make_show(duration=30)
        player.load(show, demo=True, name="DEMO PARIS", slug="demo-paris")
        assert wait_burned(player)
        player.close()
        run = json.loads((tmp_path / "show-run.json").read_text(encoding="utf-8"))
        run["show"] = "another-show"
        (tmp_path / "show-run.json").write_text(json.dumps(run), encoding="utf-8")

        session2, runner2, _ = make_session()
        reborn = ShowPlayer(session2, store=tmp_path, tick_s=0.02)
        try:
            reborn.restore()
            assert reborn.state == LOADED
            assert reborn.is_demo is False and reborn.demo_name == ""
        finally:
            reborn.close()
            runner2.stop()
    finally:
        player.close()
        runner.stop()


def test_the_conductor_still_sees_a_restored_demo_as_a_demo(tmp_path):
    """conductor/fleet.py reads only the status fields, so a restored
    demo has to LOOK like one: _playing_demo() is what keeps the fleet's
    supervision (and _adopt()) from driving a show nobody asked it to."""
    from conductor.fleet import Fleet      # the PC side, on the unit's JSON

    class _Link:
        def __init__(self, status):
            self.status = status

    session, runner, bus = make_session()
    player = ShowPlayer(session, store=tmp_path, save_s=0.01, margin_s=0.1,
                        grace_s=0.1, tick_s=0.02, setup_s=0.05,
                        setup_board_s=0.0)
    try:
        show = make_show(sents=(-REFRESH, 5.0), duration=30)
        player.load(show, demo=True, name="DEMO PARIS", slug="demo-paris")
        assert wait_burned(player)
        player.run(time.monotonic() - 1.0)
        assert wait_until(lambda: player.state == RUNNING)
        player.close()

        session2, runner2, _ = make_session()
        reborn = ShowPlayer(session2, store=tmp_path, grace_s=0.1, tick_s=0.02)
        try:
            reborn.restore()
            link = _Link({"show": reborn.status()})
            assert Fleet._playing_demo(link)
            assert Fleet._demo_excuse(link) == "playing a demo - press STOP first"
        finally:
            reborn.close()
            runner2.stop()
    finally:
        player.close()
        runner.stop()


def restarted_app(tmp_path, **app_kwargs):
    """What ui/main.py does after a restart, in order: a new player on the
    same store, restore(), and only then the App - which is why the App
    has to adopt what the player brought back."""
    session, runner, bus = make_session()
    player = ShowPlayer(session, store=tmp_path / "player", save_s=0.01,
                        margin_s=0.1, grace_s=0.1, tick_s=0.02,
                        setup_s=0.05, setup_board_s=0.0)
    session.on_release = player.stop
    player.restore()
    app = App(NullDisplay(), ScriptedInput(()), runner, remote=session,
              player=player, demos=DemoStore(tmp_path / "demos"),
              host="radxa-03", **app_kwargs)
    app.show_status = player.status
    return app, player, runner, bus


def test_key1_is_refused_over_a_pc_show_restore_put_back_on_the_garment(tmp_path):
    # Review 1: a PC show that was HOLDING when the unit restarted comes
    # back LOADED - but its picture is on the garment (restored_running,
    # which is why ui/main.py skips the standby white) and the PC is
    # coming back for it. The state alone does not say so.
    session, runner, bus = make_session()
    app, player, demos = make_app(tmp_path, session, runner)
    slug = demos.save("DEMO PARIS", make_show())
    pc_show = make_show(sents=(-REFRESH, 5.0), duration=30)
    try:
        player.load(pc_show)
        assert wait_burned(player)
        player.run(time.monotonic() - 1.0)
        assert wait_until(lambda: player.applied == "q00")
        player.hold()
    finally:
        player.close()
        runner.stop()

    app2, player2, runner2, _ = restarted_app(tmp_path)
    try:
        assert player2.state == LOADED and player2.restored_running
        assert player2.restored_id == pc_show["id"]
        app2.select(f"demo:{slug}")
        app2.handle("key1")
        assert app2.screen is Screen.MENU           # never entered DEMO
        assert not player2.is_demo
        assert player2.show["id"] == pc_show["id"]  # not painted over
        assert "PC show running" in app2._standby_status()

        # STOP from the PC is what lets go of the garment; then the row
        # works again, on the very same LOADED show.
        player2.stop()
        assert player2.restored_id is None
        app2.handle("key1")
        assert app2.screen is Screen.DEMO and player2.is_demo
    finally:
        player2.close()
        runner2.stop()


def test_key1_is_refused_while_the_pc_is_writing_its_pictures(tmp_path):
    # Review 2: the conductor's Upload is in flight. KEY1 would cancel
    # that burn (load() cancels whatever the session is writing), and the
    # PC has no way to know its Upload was thrown away.
    session, runner, bus = make_session(_SlowSaveBus())
    app, player, demos = make_app(tmp_path, session, runner)
    try:
        slug = demos.save("DEMO PARIS", make_show())
        app.refresh_demos()
        app.select(f"demo:{slug}")
        pc_show = make_show(sents=tuple(n * 0.01 for n in range(18)),
                            duration=5)
        player.load(pc_show)
        assert player.state == LOADED
        assert player.status()["burn"]["state"] == "burning"

        app.handle("key1")
        assert app.screen is Screen.MENU
        assert not player.is_demo and player.show["id"] == pc_show["id"]
        assert "writing pictures" in app._standby_status()
        assert player.status()["burn"]["state"] == "burning"   # not cancelled

        # Once it settles, that same LOADED show may be superseded.
        assert wait_burned(player, timeout=10)
        app.handle("key1")
        assert app.screen is Screen.DEMO and player.is_demo
    finally:
        player.close()
        runner.stop()


def test_a_record_from_before_the_slug_still_finds_its_row(tmp_path):
    # Review 3: the release before this fix wrote `demo` and `demo_name`
    # but no `demo_slug`. The row whose stored show is this very show is
    # the way back to it (DemoStore.list() carries show_id) - without it
    # the LCD would fall through to REMOTE and the loop would be lost.
    session, runner, bus = make_session()
    app, player, demos = make_app(tmp_path, session, runner)
    demo_show = make_show(sents=(-REFRESH, 5.0), duration=30)
    try:
        slug = demos.save("LOOPY", demo_show, loop=True)
        app.refresh_demos()
        app.select(f"demo:{slug}")
        app.handle("key1")
        assert _pump(app, lambda: player.state == RUNNING, timeout=3)
    finally:
        player.close()
        runner.stop()

    record = tmp_path / "player" / "show-run.json"
    run = json.loads(record.read_text(encoding="utf-8"))
    del run["demo_slug"], run["demo_loop"]          # the older shape
    record.write_text(json.dumps(run), encoding="utf-8")

    app2, player2, runner2, _ = restarted_app(tmp_path)
    try:
        assert player2.is_demo and not player2.demo_slug
        assert app2.screen is Screen.DEMO
        assert app2._playing_demo == slug           # found by show_id
        assert app2._demo_loop is True              # ...and its loop flag
        assert app2.patterns[app2.selected].key == f"demo:{slug}"
    finally:
        player2.close()
        runner2.stop()


def test_a_looping_demo_whose_row_was_deleted_still_loops_as_recorded(
        tmp_path, monkeypatch):
    # Review 4: `loop` is recorded with the demo, so an adopted lap does
    # not depend on a menu row that may be gone (deleted from the PC
    # while the unit was off).
    monkeypatch.setattr(app_module, "LOOP_GAP_S", 0.2)
    session, runner, bus = make_session()
    app, player, demos = make_app(tmp_path, session, runner)
    demo_show = make_show(sents=(-REFRESH, 0.3), duration=0.6)
    try:
        slug = demos.save("LOOPY", demo_show, loop=True)
        app.refresh_demos()
        app.select(f"demo:{slug}")
        app.handle("key1")
        assert _pump(app, lambda: player.state == RUNNING, timeout=3)
        assert json.loads((tmp_path / "player" / "show-run.json")
                          .read_text(encoding="utf-8"))["demo_loop"] is True
        demos.delete(slug)                          # gone from the PC
    finally:
        player.close()
        runner.stop()

    app2, player2, runner2, bus2 = restarted_app(tmp_path)
    try:
        assert app2.screen is Screen.DEMO
        assert app2._playing_demo == slug
        assert not any(isinstance(p, DemoRow) for p in app2.patterns)
        assert app2._demo_loop is True      # the record, not the row
        assert _pump(app2, lambda: player2.state == ENDED, timeout=8)
        ended_t0 = player2.t0
        assert _pump(app2, lambda: player2.state == RUNNING
                     and player2.t0 != ended_t0, timeout=8)
        assert saved_pairs(bus2) == set()           # a re-run, no re-burn
    finally:
        player2.close()
        runner2.stop()


def test_a_pc_load_cut_in_half_does_not_leave_the_demo_record_behind(tmp_path):
    # Review 5: a demo written FROM the show the PC runs carries the very
    # same id, so the run record is the only thing telling the two apart.
    # _persist() writes it BEFORE show.json for exactly this reason: a
    # power cut between the two can then only lose the show file (which
    # still holds the same bytes), never leave the demo's record paired
    # with the PC's fresh load.
    session, runner, bus = make_session()
    player = ShowPlayer(session, store=tmp_path, tick_s=0.02)
    show = make_show(duration=30)
    try:
        player.load(show, demo=True, name="DEMO PARIS", slug="demo-paris")
        assert wait_burned(player)
        real_write = player._write

        def cut_in_half(name, payload):
            if name == "show.json":
                raise OSError("the power went")
            return real_write(name, payload)

        player._write = cut_in_half
        player.load(show)                   # the PC's own /show/load
    finally:
        player.close()
        runner.stop()

    session2, runner2, _ = make_session()
    reborn = ShowPlayer(session2, store=tmp_path, tick_s=0.02)
    try:
        reborn.restore()
        assert reborn.show["id"] == show["id"]
        assert reborn.is_demo is False      # the PC's load is what stuck
        assert reborn.demo_name == "" and reborn.demo_slug == ""
    finally:
        reborn.close()
        runner2.stop()


def test_a_unit_that_restarted_mid_demo_gives_the_lcd_its_demo_back(
        tmp_path, monkeypatch):
    """ui/main.py restores the player and only then builds the App, so the
    App has to adopt a demo that came back running - or the LCD would sit
    on the menu while the garment plays, KEY2 would not stop it, and a
    `loop` demo would stop at its last cue."""
    monkeypatch.setattr(app_module, "LOOP_GAP_S", 0.2)
    session, runner, bus = make_session()
    app, player, demos = make_app(tmp_path, session, runner)
    try:
        demo_show = make_show(sents=(-REFRESH, 0.3), duration=0.6)
        slug = demos.save("LOOPY", demo_show, loop=True)
        app.refresh_demos()
        app.select(f"demo:{slug}")
        app.handle("key1")
        assert _pump(app, lambda: player.status()["state"] == RUNNING,
                     timeout=3)
        assert app.screen is Screen.DEMO
    finally:
        player.close()
        runner.stop()

    # The restart: a new player on the same store, restore(), then the App.
    app2, player2, runner2, bus2 = restarted_app(tmp_path)
    try:
        assert player2.is_demo and player2.state == RUNNING
        # Adopted: the DEMO screen, its loop flag and name, and the slug
        # KEY1-hold restarts from.
        assert app2.screen is Screen.DEMO
        assert app2._playing_demo == slug and app2._demo_loop is True
        assert app2._demo_name == "LOOPY"
        assert app2._demo_show_id == demo_show["id"]
        # Nothing is burned again - the pictures are still in their slots.
        assert saved_pairs(bus2) == set()
        # It plays out the lap it was restored into...
        assert _pump(app2, lambda: player2.state == ENDED, timeout=8)
        fired = len(show_times(bus2))
        assert fired >= 2
        ended_t0 = player2.t0
        # ...then loops on its own, as it did before the restart: a
        # re-run on the same T0 arithmetic, never a re-burn.
        assert _pump(app2, lambda: player2.state == RUNNING
                     and player2.t0 != ended_t0, timeout=8)
        assert _pump(app2, lambda: len(show_times(bus2)) > fired, timeout=8)
        assert saved_pairs(bus2) == set()
        # ...and KEY2 still ends it.
        app2.handle("key2")
        assert app2.screen is Screen.MENU and player2.state == STOPPED
    finally:
        player2.close()
        runner2.stop()


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

