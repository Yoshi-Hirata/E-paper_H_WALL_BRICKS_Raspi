"""POST /wifi/select: a Wi-Fi switch asked for over the agent, now or in
N seconds (ui/agent.py, ui/wifi.py's schedule()).

The fake nmcli is tests/test_ui_wifi.py's; nothing here touches the
network of the machine the tests run on, and every switch the timer
fires is the very `sudo -n nmcli --wait 45 con up <name>` those tests
check.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ui.agent import Agent
from ui.app import Screen
from ui.showplay import HOLDING, LOADED, RUNNING
from ui.wifi import (CONNECTING, DONE, IDLE, LOCKED, LOCKED_NOTE, PENDING_NOTE,
                     up_command)
from tests.test_ui_remote import call, make_session
from tests.test_ui_runner import wait_until
from tests.test_ui_wifi import (HOTSPOT, ROUTER, FakePlayer, enter, make_app,
                                make_wifi)


def make_agent(wifi, player=None):
    session, runner, _ = make_session()
    agent = Agent(session, port=0, host="127.0.0.1", wifi=wifi, player=player)
    agent.start()
    return agent, runner


def select(agent, **body):
    return call(agent, "/wifi/select", body)


# ---- now ----

def test_select_switches_now_with_the_same_argv_as_a_held_key1():
    wifi, fake = make_wifi()
    wifi.poll()
    agent, runner = make_agent(wifi)
    try:
        code, answer = select(agent, profile=HOTSPOT)
        assert code == 200
        assert answer["scheduled"] is False and answer["profile"] == HOTSPOT
        assert answer["phase"] in (CONNECTING, DONE)   # the fake is quick
        assert answer["wifi"]["pending"] is None
        assert wait_until(lambda: wifi.phase == DONE)
        assert fake.ups == [HOTSPOT]
        assert [c for c in fake.calls if c[:2] == ["sudo", "-n"]] == [up_command(HOTSPOT)]
        assert wifi.snapshot()["ssid"] == HOTSPOT
        code, status = call(agent, "/status")
        assert status["wifi"]["profile"] == HOTSPOT
        assert status["wifi"]["pending"] is None
    finally:
        agent.stop()
        runner.stop()


def test_bad_bodies_and_unknown_profiles_are_answered():
    wifi, fake = make_wifi()
    agent, runner = make_agent(wifi)
    try:
        # The list has not been read yet: not a 404, the name may be fine.
        code, answer = select(agent, profile=HOTSPOT)
        assert code == 409 and "not been read" in answer["error"]
        wifi.poll()
        code, answer = select(agent, profile="Guest")
        assert code == 404 and answer["error"] == "unknown profile: Guest"
        for body in ({}, {"profile": ""}, {"profile": 3},
                     {"profile": HOTSPOT, "after_s": -1},
                     {"profile": HOTSPOT, "after_s": 121},
                     {"profile": HOTSPOT, "after_s": "30"},
                     {"profile": HOTSPOT, "after_s": True}):
            code, answer = call(agent, "/wifi/select", body)
            assert code == 400, body
            assert answer["error"].startswith("bad request")
        assert fake.ups == []
        assert wifi.pending is None
    finally:
        agent.stop()
        runner.stop()


def test_refused_while_the_pc_show_runs_and_while_a_switch_is_in_flight():
    # The WIFI row's own predicate (App._wifi_locked, review of cae60f8
    # M1): running / holding / writing pictures, and a PC show uploaded
    # and waiting for START - each with its own words.
    cases = [(FakePlayer(state=RUNNING), "PC show running - WIFI locked"),
             (FakePlayer(state=HOLDING), "PC show running - WIFI locked"),
             (FakePlayer(state=LOADED, burn="burning"),
              "PC show running - WIFI locked"),
             (FakePlayer(state=LOADED, burn="burned"),
              "PC show loaded - WIFI locked")]
    for player, words in cases:
        wifi, fake = make_wifi()
        wifi.poll()
        agent, runner = make_agent(wifi, player=player)
        try:
            code, answer = select(agent, profile=HOTSPOT)
            assert code == 409
            assert answer["error"] == words
            code, answer = select(agent, profile=HOTSPOT, after_s=30)
            assert code == 409 and answer["error"] == words
            assert fake.ups == [] and wifi.pending is None
        finally:
            agent.stop()
            runner.stop()
    assert LOCKED_NOTE == "PC show running - WIFI locked"
    # A PC show LOADED with nothing written (a restart's restore of a
    # demo looks like this) does not lock it - the row's own rule.
    wifi, fake = make_wifi()
    wifi.poll()
    agent, runner = make_agent(wifi, player=FakePlayer(state=LOADED, burn="none"))
    try:
        fake.release.clear()
        code, answer = select(agent, profile=HOTSPOT)
        assert code == 200 and wifi.busy
        code, answer = select(agent, profile=ROUTER)
        assert code == 409 and answer["error"] == "a switch is in flight"
        code, answer = select(agent, profile=ROUTER, after_s=5)
        assert code == 409
        fake.release.set()
        assert wait_until(lambda: wifi.phase == DONE)
        assert fake.ups == [HOTSPOT]
    finally:
        agent.stop()
        runner.stop()


# ---- later ----

def test_a_deferred_select_answers_at_once_and_fires_on_time():
    wifi, fake = make_wifi()
    wifi.poll()
    agent, runner = make_agent(wifi)
    try:
        before = time.monotonic()
        code, answer = select(agent, profile=HOTSPOT, after_s=0.3)
        assert time.monotonic() - before < 0.25      # never waits for it
        assert code == 200
        assert answer == {"scheduled": True, "after_s": 0.3, "profile": HOTSPOT}
        assert fake.ups == []                         # not yet
        pending = wifi.pending_info()
        assert pending["profile"] == HOTSPOT and pending["in_s"] in (0, 1)
        code, status = call(agent, "/status")
        assert status["wifi"]["pending"]["profile"] == HOTSPOT
        assert status["wifi"]["profile"] == ROUTER    # still on the router
        assert wifi.status_text().startswith(f"switching to {HOTSPOT} in ")
        assert wait_until(lambda: wifi.phase == DONE)
        assert fake.ups == [HOTSPOT]
        assert [c for c in fake.calls if c[:2] == ["sudo", "-n"]] == [up_command(HOTSPOT)]
        assert wifi.pending is None
        code, status = call(agent, "/status")
        assert status["wifi"]["pending"] is None
        assert status["wifi"]["profile"] == HOTSPOT
        assert any("in 0.3 s" in line for line in wifi.recent(10))
    finally:
        agent.stop()
        runner.stop()


def test_cancel_takes_a_deferral_back_and_a_new_select_replaces_one():
    wifi, fake = make_wifi()
    wifi.poll()
    agent, runner = make_agent(wifi)
    try:
        code, answer = select(agent, cancel=True)
        assert code == 200 and answer["cancelled"] is False
        code, _ = select(agent, profile=HOTSPOT, after_s=0.2)
        assert code == 200 and wifi.pending is not None
        code, answer = select(agent, cancel=True)
        assert code == 200 and answer["cancelled"] is True
        assert answer["wifi"]["pending"] is None
        assert wifi.pending is None
        time.sleep(0.4)
        assert fake.ups == [] and wifi.phase == IDLE  # the timer never fired
        # Replaced: only the second one fires, once.
        code, _ = select(agent, profile=ROUTER, after_s=0.15)
        code, _ = select(agent, profile=HOTSPOT, after_s=0.3)
        assert wifi.pending_info()["profile"] == HOTSPOT
        assert wait_until(lambda: wifi.phase == DONE)
        time.sleep(0.3)
        assert fake.ups == [HOTSPOT]
        # A select "now" clears a pending one too.
        code, _ = select(agent, profile=ROUTER, after_s=60)
        assert wifi.pending is not None
        code, _ = select(agent, profile=ROUTER)
        assert wifi.pending is None
        assert wait_until(lambda: wifi.phase == DONE)
        assert fake.ups == [HOTSPOT, ROUTER]
    finally:
        agent.stop()
        runner.stop()


def test_the_lock_is_asked_again_when_the_timer_fires():
    player = FakePlayer(state=LOADED, burn="none")   # not locked now...
    wifi, fake = make_wifi()
    wifi.poll()
    agent, runner = make_agent(wifi, player=player)
    try:
        code, _ = select(agent, profile=HOTSPOT, after_s=0.2)
        assert code == 200
        player.state = RUNNING                    # ...but the PC took the unit
        time.sleep(0.5)
        assert fake.ups == []                     # skipped, never run
        assert wifi.pending is None
        assert wifi.phase == LOCKED
        assert wifi.status_text() == (f"PC show running - WIFI locked - "
                                      f"switch to {HOTSPOT} skipped")
        assert any("skipped" in line and "ERROR" in line
                   for line in wifi.recent(10))
        assert wifi.snapshot()["profile"] == ROUTER
    finally:
        agent.stop()
        runner.stop()


def test_a_profile_gone_by_fire_time_is_a_failure_not_a_crash():
    wifi, fake = make_wifi()
    wifi.poll()
    wifi.schedule(HOTSPOT, 0.1)
    del fake.profiles[HOTSPOT]
    wifi.poll()
    assert wait_until(lambda: wifi.phase == LOCKED)
    assert fake.ups == []
    assert wifi.status_text() == f"no such profile: {HOTSPOT} - switch skipped"


# ---- the WIFI screen while one is pending ----

def test_the_screen_counts_a_deferral_down_and_refuses_the_keys():
    wifi, fake = make_wifi()
    wifi.poll()
    app, runner = make_app(wifi)
    enter(app)
    wifi.schedule(HOTSPOT, 60.0)
    app.draw()
    key = app._display_key()
    assert wifi.status_text() == f"switching to {HOTSPOT} in 60 s"
    assert wifi.chosen().name == ROUTER
    for event in ("up", "down", "left", "right", "key1_hold", "key1", "press"):
        app.handle(event)
    assert wifi.chosen().name == ROUTER           # the cursor did not move
    assert wifi.phase == LOCKED and wifi.error == PENDING_NOTE
    assert wifi.status_text() == f"switch pending - switching to {HOTSPOT} in 60 s"
    assert app._display_key() != key
    wifi.join(0.2)
    assert fake.ups == []                         # nothing switched early
    # KEY2 goes back without cancelling; re-entering shows it still.
    app.handle("key2")
    assert app.screen is Screen.MENU
    assert wifi.pending is not None and wifi.phase == IDLE
    enter(app)
    assert wifi.status_text() == f"switching to {HOTSPOT} in 60 s"
    # The redraw key follows the seconds.
    wifi.pending = (HOTSPOT, wifi.pending[1] - 5.0)
    assert wifi.status_text() == f"switching to {HOTSPOT} in 55 s"
    assert wifi.cancel_pending() is True
    assert wifi.status_text() == ""
    assert wifi.cancel_pending() is False
    assert runner.stops == 0
