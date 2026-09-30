"""WIFI: the menu row and ui/wifi.py.

nmcli is replaced by a fake that models just enough of NetworkManager -
which wireless profiles exist, which one is up, what address it gives -
so the whole flow (the list, the cursor, KEY1 *held* to switch, a
refusal bringing the previous profile back, the lock while the PC
drives the unit, /status's cache) is covered without nmcli or sudo, and
without touching the network of the machine the tests run on.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ui import render
from ui.agent import Agent
from ui.app import App, Screen
from ui.config import HEIGHT, WIDTH
from ui.display import NullDisplay
from ui.inputs import ScriptedInput
from ui.showplay import ENDED, HOLDING, LOADED, RUNNING
from ui.wifi import (CONNECTING, DONE, FAILED, IDLE, LOCKED, LOCKED_NOTE,
                     WIFI_BLANK, Wifi, split_terse, up_command)
from tests.test_ui_app import FakeRunner
from tests.test_ui_runner import wait_until

ROUTER = "yoshihirock.net_5G"
HOTSPOT = "AZ-Epaper"
REFUSAL = ("Error: Connection activation failed: No suitable device found "
           "for this connection (device wlan0 not available because "
           "profile is not compatible with device).")


class FakeNmcli:
    """Stands in for nmcli and `sudo -n nmcli con up`.

    `active` is the profile up now (None: disconnected). `fail` names
    profiles whose `con up` is refused with REFUSAL - and, as
    NetworkManager does, leaves the device disconnected unless
    `keep_on_fail`; `hang` names profiles whose `con up` raises the
    subprocess timeout. `release` holds every `con up` so a test can
    look at the screen while a switch is in flight.
    """

    def __init__(self, active=ROUTER, fail=(), hang=(), keep_on_fail=False,
                 signal=72):
        self.profiles = {
            ROUTER: {"mode": "infrastructure", "ip": "192.168.51.103"},
            HOTSPOT: {"mode": "infrastructure", "ip": "192.168.4.13"},
        }
        self.active = active
        self.fail = set(fail)
        self.hang = set(hang)
        self.keep_on_fail = keep_on_fail
        self.signal = signal
        self.broken = None          # a message: every read fails with it
        self.calls = []
        self.release = threading.Event()
        self.release.set()
        self.hold_reads = threading.Event()   # cleared: reads wait too
        self.hold_reads.set()

    @property
    def ups(self):
        return [call[-1] for call in self.calls if call[:2] == ["sudo", "-n"]]

    def __call__(self, args, timeout):
        self.calls.append(list(args))
        if args[:2] == ["sudo", "-n"]:
            return self._up(args, timeout)
        assert args[0] == "nmcli" and args[1] == "-t"
        self.hold_reads.wait(5.0)
        if self.broken:
            return 8, self.broken
        if args[-2:] == ["con", "show"]:
            rows = [f"{name}:802-11-wireless:{'yes' if name == self.active else 'no'}:yes"
                    for name in self.profiles]
            rows.insert(1, "Wired connection 1:802-3-ethernet:no:yes")
            return 0, "\n".join(rows)
        if args[-3:-1] == ["con", "show"]:
            name = args[-1]
            if name not in self.profiles:
                return 10, f"Error: {name} - no such connection profile."
            return 0, (f"802-11-wireless.mode:{self.profiles[name]['mode']}\n"
                       f"802-11-wireless.ssid:{name}")
        if args[-2:] == ["dev", "show"]:
            wifi = self.profiles.get(self.active) if self.active else None
            return 0, "\n".join([
                "GENERAL.DEVICE:lo", "GENERAL.TYPE:loopback",
                "GENERAL.CONNECTION:--", "IP4.ADDRESS[1]:127.0.0.1/8", "",
                "GENERAL.DEVICE:wlan0", "GENERAL.TYPE:wifi",
                f"GENERAL.CONNECTION:{self.active or '--'}",
                f"IP4.ADDRESS[1]:{wifi['ip']}/24" if wifi else ""])
        if "wifi" in args:
            assert args[-2:] == ["--rescan", "no"]     # never a scan
            wifi = self.profiles.get(self.active) if self.active else None
            if wifi is None:
                return 0, "no:Neighbour:40"
            if wifi["mode"] == "ap":
                # Some builds list the hotspot's own AP as active at 100.
                return 0, f"yes:{self.active}:100"
            return 0, f"yes:{self.active}:{self.signal}\nno:Neighbour:40"
        raise AssertionError(f"unexpected nmcli call {args}")

    def _up(self, args, timeout):
        assert args[:7] == ["sudo", "-n", "nmcli", "--wait", "45", "con", "up"]
        name = args[-1]
        self.release.wait(5.0)
        if name in self.hang:
            raise subprocess.TimeoutExpired(args[0], timeout)
        if name in self.fail:
            if not self.keep_on_fail:
                self.active = None
            return 4, REFUSAL
        self.active = name
        return 0, "Connection successfully activated (D-Bus active path: /x/1)"


def make_wifi(**kwargs):
    fake = FakeNmcli(**kwargs)
    return Wifi(run=fake, poll_s=60.0, echo_log=False), fake


class FakePlayer:
    """Just what App._pc_show_wins() reads off a ShowPlayer."""

    def __init__(self, state=RUNNING, burn="burned", is_demo=False):
        self.show = {"id": "pc-show"}
        self.is_demo = is_demo
        self.state = state
        self.restored_running = False
        self.restored_id = None
        self.burn = burn

    def status(self):
        return {"state": self.state, "burn": {"state": self.burn}}


def make_app(wifi, player=None, locked=False):
    runner = FakeRunner()
    app = App(NullDisplay(), ScriptedInput(()), runner, port_label="/dev/fake",
              wifi=wifi, host="radxa-03", player=player, locked=locked)
    return app, runner


def enter(app):
    app.select("wifi")
    app.handle("key1")


# ---- reading ----

def test_split_terse_unescapes_colons_and_backslashes():
    assert split_terse("yes:my\\:ssid:70") == ["yes", "my:ssid", "70"]
    assert split_terse("a\\\\b:c") == ["a\\b", "c"]
    assert split_terse("") == [""]


def test_poll_lists_the_wireless_profiles_and_marks_the_active_one():
    wifi, fake = make_wifi()
    assert wifi.snapshot() == WIFI_BLANK
    assert wifi.current() == ("reading...", "")
    wifi.poll()
    # Wireless profiles only - the ethernet row is not offered.
    assert [p.name for p in wifi.profiles] == [ROUTER, HOTSPOT]
    assert [p.active for p in wifi.profiles] == [True, False]
    assert all(p.mode == "client" for p in wifi.profiles)
    assert wifi.rows() == [(ROUTER, "client", True), (HOTSPOT, "client", False)]
    assert wifi.snapshot() == {"ssid": ROUTER, "ip": "192.168.51.103",
                               "signal": 72, "mode": "client",
                               "profile": ROUTER}
    assert wifi.current() == (ROUTER, "IP 192.168.51.103  72%  client")
    assert wifi.status_text() == ""
    assert fake.ups == []                       # reading never switches
    assert not any(call[0] == "sudo" for call in fake.calls)


def test_a_hotspot_reads_as_one_and_never_has_a_signal():
    wifi, fake = make_wifi(active=HOTSPOT)
    fake.profiles[HOTSPOT] = {"mode": "ap", "ip": "10.42.0.1"}
    wifi.poll()
    # The fake lists the AP itself as active at 100, as some builds do;
    # a hotspot has no signal of its own (review of cae60f8, L1).
    assert wifi.snapshot() == {"ssid": HOTSPOT, "ip": "10.42.0.1",
                               "signal": None, "mode": "hotspot",
                               "profile": HOTSPOT}
    assert wifi.rows()[1] == (HOTSPOT, "hotspot", True)
    assert wifi.current() == (HOTSPOT, "IP 10.42.0.1  hotspot")


def test_disconnected_and_a_missing_nmcli_read_as_nulls():
    wifi, fake = make_wifi(active=None)
    wifi.poll()
    assert wifi.snapshot() == WIFI_BLANK
    assert wifi.current() == ("not connected", "")
    assert [p.active for p in wifi.profiles] == [False, False]

    absent = Wifi(run=lambda args, t: (127, "nmcli not installed"),
                  echo_log=False)
    absent.poll()                               # never raises
    assert absent.snapshot() == WIFI_BLANK and absent.profiles == []
    assert absent.current() == ("not connected", "nmcli: nmcli not installed")

    def hung(args, timeout):
        raise subprocess.TimeoutExpired(args[0], timeout)
    slow = Wifi(run=hung, echo_log=False)
    slow.poll()
    assert slow.snapshot() == WIFI_BLANK
    assert slow.read_state == "error" and "timed out" in slow.read_error


def test_a_transient_nmcli_error_keeps_the_last_good_cache():
    # Review of cae60f8, L2: a NetworkManager restart or a busy D-Bus must
    # not flicker /status and the screen to nulls.
    wifi, fake = make_wifi()
    wifi.poll()
    good = wifi.snapshot()
    fake.broken = "Error: NetworkManager is not running."
    wifi.poll()
    assert wifi.snapshot() == good
    assert wifi.rows() == [(ROUTER, "client", True), (HOTSPOT, "client", False)]
    assert wifi.read_state == "error"
    assert wifi.current() == (ROUTER, "IP 192.168.51.103  72%  client")
    assert wifi.status_text() == "nmcli: Error: NetworkManager is not running."
    fake.broken = None
    fake.active = HOTSPOT
    wifi.poll()
    assert wifi.snapshot()["ssid"] == HOTSPOT and wifi.status_text() == ""


def test_the_reader_thread_refreshes_the_cache_on_its_own():
    fake = FakeNmcli()
    wifi = Wifi(run=fake, poll_s=0.05, echo_log=False)
    try:
        wifi.start_reader()
        assert wait_until(lambda: wifi.snapshot()["ssid"] == ROUTER)
        fake.active = HOTSPOT                   # changed behind our back
        assert wait_until(lambda: wifi.snapshot()["ssid"] == HOTSPOT)
        assert fake.ups == []
    finally:
        wifi.close()


# ---- the menu row and the keys ----

def test_wifi_sits_just_before_board_info():
    from ui.boardinfo import BoardInfo
    from ui.puller import RepoPuller
    from ui.rebooter import Rebooter

    wifi, _ = make_wifi()
    app = App(NullDisplay(), ScriptedInput(()), FakeRunner(), puller=RepoPuller(),
              rebooter=Rebooter(), wifi=wifi, boardinfo=BoardInfo(None))
    keys = [p.key for p in app.patterns]
    assert keys[-4:] == ["pull", "reboot", "wifi", "boardinfo"], keys
    assert wifi.menu_entry.label == "WIFI"
    plain = App(NullDisplay(), ScriptedInput(()), FakeRunner())
    assert "wifi" not in [p.key for p in plain.patterns]


def test_entering_starts_on_the_active_profile_and_up_down_move():
    wifi, fake = make_wifi(active=HOTSPOT)
    wifi.poll()
    app, runner = make_app(wifi)
    enter(app)
    assert app.screen is Screen.WIFI
    assert wifi.chosen().name == HOTSPOT        # the cursor on what is up
    assert runner.stops == 0                    # the runner keeps the port
    app.handle("up")
    assert wifi.chosen().name == ROUTER
    app.handle("up")
    assert wifi.chosen().name == HOTSPOT        # wrapped
    app.handle("down")
    assert wifi.chosen().name == ROUTER
    assert fake.ups == []


def test_a_plain_key1_never_switches():
    wifi, fake = make_wifi()
    wifi.poll()
    app, _ = make_app(wifi)
    enter(app)
    app.handle("down")                          # AZ-Epaper chosen
    for event in ("key1", "press", "left", "right", "key1", "press"):
        app.handle(event)
    wifi.join(0.2)
    assert fake.ups == []
    assert app.screen is Screen.WIFI and wifi.phase == IDLE


def test_held_key1_switches_and_the_screen_shows_the_new_network():
    wifi, fake = make_wifi()
    wifi.poll()
    app, runner = make_app(wifi)
    enter(app)
    app.handle("down")
    app.handle("key1_hold")
    assert wait_until(lambda: wifi.phase == DONE)
    assert fake.ups == [HOTSPOT]
    assert [c for c in fake.calls if c[:2] == ["sudo", "-n"]] == [up_command(HOTSPOT)]
    assert up_command(HOTSPOT) == ["sudo", "-n", "nmcli", "--wait", "45",
                                   "con", "up", HOTSPOT]
    assert wifi.snapshot()["ssid"] == HOTSPOT
    assert wifi.snapshot()["ip"] == "192.168.4.13"
    assert wifi.rows()[1] == (HOTSPOT, "client", True)
    assert wifi.status_text() == f"on {HOTSPOT}"
    assert runner.stops == 0 and runner.starts == []
    app.handle("key2")
    assert app.screen is Screen.MENU and wifi.phase == IDLE


def test_while_connecting_only_key2_is_heard_and_the_switch_goes_on():
    wifi, fake = make_wifi()
    wifi.poll()
    app, _ = make_app(wifi)
    enter(app)
    app.handle("down")
    fake.release.clear()
    app.handle("key1_hold")
    assert wifi.busy and wifi.phase == CONNECTING
    assert wifi.status_text() == f"connecting to {HOTSPOT}…"
    for event in ("up", "down", "key1", "key1_hold", "press"):
        app.handle(event)
    assert wifi.chosen().name == HOTSPOT       # the cursor did not move
    assert app.screen is Screen.WIFI
    app.handle("key2")
    assert app.screen is Screen.MENU           # back, the switch still running
    assert wifi.busy
    fake.release.set()
    assert wait_until(lambda: wifi.phase == DONE)
    assert fake.ups == [HOTSPOT]


def test_a_failed_switch_brings_the_previous_profile_back():
    wifi, fake = make_wifi(fail=(HOTSPOT,))
    wifi.poll()
    app, _ = make_app(wifi)
    enter(app)
    app.handle("down")
    app.handle("key1_hold")
    assert wait_until(lambda: wifi.phase == FAILED)
    assert fake.ups == [HOTSPOT, ROUTER]
    assert fake.active == ROUTER
    assert wifi.restored is True
    assert wifi.error == REFUSAL.splitlines()[0]
    # The restore's outcome comes first, ahead of nmcli's long sentence,
    # so the screen's three lines can never cut it off (M3).
    text = wifi.status_text()
    assert text.startswith(f"ERROR back on {ROUTER}: Error: Connection "
                           "activation failed")
    assert wifi.snapshot()["ssid"] == ROUTER
    assert any("ERROR" in line for line in wifi.recent(10))
    # Moving the cursor reads the verdict away; a hold tries again.
    app.handle("up")
    assert wifi.phase == IDLE
    app.handle("down")
    app.handle("key1_hold")
    assert wait_until(lambda: len(fake.ups) == 4)
    assert wait_until(lambda: wifi.phase == FAILED)


def test_a_failure_networkmanager_recovered_by_itself_is_not_reconnected():
    wifi, fake = make_wifi(fail=(HOTSPOT,), keep_on_fail=True)
    wifi.poll()
    wifi.select(+1)
    wifi.switch()
    assert wait_until(lambda: wifi.phase == FAILED)
    assert fake.ups == [HOTSPOT]                # still on the router: left alone
    assert wifi.restored is True
    assert wifi.status_text().startswith(f"ERROR back on {ROUTER}: ")


def test_a_hung_nmcli_is_a_failure_and_a_failed_restore_is_said():
    wifi, fake = make_wifi(hang=(HOTSPOT,))
    wifi.up_timeout = 1.0
    wifi.poll()
    wifi.select(+1)
    wifi.switch()
    assert wait_until(lambda: wifi.phase == FAILED)
    assert "timed out" in wifi.error
    assert fake.ups == [HOTSPOT]                # the hang left the router up
    assert wifi.restored is True

    wifi, fake = make_wifi(fail=(HOTSPOT, ROUTER))
    wifi.poll()
    wifi.select(+1)
    wifi.switch()
    assert wait_until(lambda: wifi.phase == FAILED)
    assert fake.ups == [HOTSPOT, ROUTER]
    assert wifi.restored is False
    assert wifi.status_text().startswith(f"ERROR {ROUTER} NOT restored: Error")


def test_with_no_previous_network_the_failure_says_so():
    # Review of cae60f8, L2: a unit that was not on anything has nothing
    # to go back to - the text must not pretend otherwise.
    wifi, fake = make_wifi(active=None, fail=(HOTSPOT,))
    wifi.poll()
    wifi.select(+1)
    wifi.switch()
    assert wait_until(lambda: wifi.phase == FAILED)
    assert fake.ups == [HOTSPOT]
    assert wifi.previous is None and wifi.restored is None
    assert wifi.status_text().startswith("ERROR no previous network to go back to: ")


def test_the_switch_never_sticks_on_connecting(monkeypatch):
    # Review of cae60f8, L3: an exception nobody foresaw on the switch
    # thread, or a thread that cannot start, still ends in a verdict.
    wifi, fake = make_wifi()
    wifi.poll()
    wifi.select(+1)

    def boom(target, previous):
        raise RuntimeError("something nobody foresaw")
    monkeypatch.setattr(wifi, "_switch_body", boom)
    wifi.switch()
    assert wait_until(lambda: wifi.phase == FAILED)
    assert wifi.error == "something nobody foresaw"
    assert wifi.status_text() == (f"ERROR {ROUTER} not restored: "
                                  "something nobody foresaw")

    from ui import wifi as mod

    class NoThread:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("can't start new thread")
    wifi.reset()
    monkeypatch.setattr(mod.threading, "Thread", NoThread)
    wifi.switch()
    assert wifi.phase == FAILED and not wifi.busy
    assert wifi.error.startswith("could not start: ")


def test_holding_on_the_active_profile_does_nothing_to_the_link():
    wifi, fake = make_wifi()
    wifi.poll()
    app, _ = make_app(wifi)
    enter(app)
    app.handle("key1_hold")
    wifi.join(0.2)
    assert fake.ups == []
    assert wifi.phase == DONE and wifi.status_text() == f"on {ROUTER}"


def test_refused_while_the_pc_show_runs_holds_or_writes_pictures():
    for player in (FakePlayer(state=RUNNING), FakePlayer(state=HOLDING),
                   FakePlayer(state=LOADED, burn="burning")):
        wifi, fake = make_wifi()
        wifi.poll()
        app, runner = make_app(wifi, player=player)
        enter(app)
        app.handle("down")
        app.handle("key1_hold")
        wifi.join(0.2)
        assert fake.ups == []                   # said, not done
        assert wifi.phase == LOCKED
        assert wifi.status_text() == LOCKED_NOTE == "PC show running - WIFI locked"
        assert app.screen is Screen.WIFI
        assert runner.stops == 0
        app.handle("key2")
        assert app.screen is Screen.MENU and wifi.phase == IDLE
    # One case more than a demo row's KEY1 (review of cae60f8, M1): a PC
    # show LOADED with its pictures burned - uploaded, waiting for START.
    # A demo may supersede it; a network switch only takes the unit out
    # of the PC's reach with the show on it.
    wifi, fake = make_wifi()
    wifi.poll()
    app, _ = make_app(wifi, player=FakePlayer(state=LOADED, burn="burned"))
    enter(app)
    app.handle("down")
    app.handle("key1_hold")
    wifi.join(0.2)
    assert fake.ups == [] and wifi.phase == LOCKED
    assert wifi.status_text() == "PC show loaded - WIFI locked"
    # ...while a show restore() put back on the garment locks it as
    # "running" (the same as a demo row)...
    restored = FakePlayer(state=LOADED, burn="none")
    restored.restored_running = True
    restored.restored_id = "pc-show"
    wifi, fake = make_wifi()
    wifi.poll()
    app, _ = make_app(wifi, player=restored)
    enter(app)
    app.handle("down")
    app.handle("key1_hold")
    wifi.join(0.2)
    assert fake.ups == [] and wifi.phase == LOCKED
    assert wifi.status_text() == LOCKED_NOTE
    # ...and a LOADED show whose burn reads "none" (a restart's restore()
    # of a demo, radxa-05 2026-09-26), an ENDED or a STOPPED one, and a
    # demo's own LOADED+burned, do not lock the row.
    for player in (FakePlayer(state=LOADED, burn="none"),
                   FakePlayer(state=ENDED), FakePlayer(state="stopped"),
                   FakePlayer(state=LOADED, burn="burned", is_demo=True)):
        wifi, fake = make_wifi()
        wifi.poll()
        app, _ = make_app(wifi, player=player)
        enter(app)
        app.handle("down")
        app.handle("key1_hold")
        assert wait_until(lambda: wifi.phase == DONE)
        assert fake.ups == [HOTSPOT]


def test_a_failure_after_leaving_mid_switch_waits_on_the_screen():
    # Review of cae60f8, M3: KEY2 is allowed while CONNECTING, so the
    # verdict may land while the operator is on the menu - it must still
    # be there, restore outcome first, when WIFI is opened again.
    wifi, fake = make_wifi(fail=(HOTSPOT,))
    wifi.poll()
    app, _ = make_app(wifi)
    enter(app)
    app.handle("down")
    fake.release.clear()
    app.handle("key1_hold")
    assert wifi.busy
    app.handle("key2")
    assert app.screen is Screen.MENU
    enter(app)                                  # back while still in flight
    assert app.screen is Screen.WIFI and wifi.phase == CONNECTING
    assert wifi.status_text() == f"connecting to {HOTSPOT}…"
    app.handle("key2")
    fake.release.set()
    assert wait_until(lambda: wifi.phase == FAILED)
    assert app.screen is Screen.MENU
    enter(app)
    assert app.screen is Screen.WIFI
    assert wifi.phase == FAILED                 # not wiped by re-entry
    assert wifi.status_text().startswith(f"ERROR back on {ROUTER}: ")
    app.draw()
    app.handle("up")                            # read: the verdict clears
    assert wifi.phase == IDLE
    # A DONE verdict, by contrast, starts the screen clean next time.
    while wifi.chosen().name != HOTSPOT:
        app.handle("down")
    fake.fail.clear()
    app.handle("key1_hold")
    assert wait_until(lambda: wifi.phase == DONE)
    app.handle("key2")
    enter(app)
    assert wifi.phase == IDLE


def test_locked_unit_cannot_switch():
    wifi, fake = make_wifi()
    wifi.poll()
    app, _ = make_app(wifi, locked=True)
    app.select("wifi")
    app.handle("key1")
    app.handle("down")
    app.handle("key1_hold")
    wifi.join(0.2)
    assert app.screen is Screen.MENU
    assert fake.ups == []


def test_the_list_refresh_keeps_the_cursor_on_its_name():
    wifi, fake = make_wifi()
    wifi.poll()
    wifi.select(+1)
    assert wifi.chosen().name == HOTSPOT
    fake.profiles = {"Guest": {"mode": "infrastructure", "ip": "10.0.0.2"},
                     **fake.profiles}
    wifi.poll()
    assert wifi.chosen().name == HOTSPOT
    del fake.profiles[HOTSPOT]
    wifi.poll()
    assert wifi.chosen() is not None            # clamped, never off the list


# ---- /status ----

def test_status_serves_the_wifi_cache_and_never_asks_nmcli():
    from tests.test_ui_remote import call, make_session

    wifi, fake = make_wifi()
    session, runner, _ = make_session()
    agent = Agent(session, port=0, host="127.0.0.1", wifi=wifi)
    agent.start()
    try:
        code, status = call(agent, "/status")
        assert code == 200
        assert status["wifi"] == WIFI_BLANK     # nothing read yet: nulls
        assert fake.calls == []                 # /status asked nothing
        wifi.poll()
        before = len(fake.calls)
        code, status = call(agent, "/status")
        assert status["wifi"] == {"ssid": ROUTER, "ip": "192.168.51.103",
                                  "signal": 72, "mode": "client",
                                  "profile": ROUTER}
        assert len(fake.calls) == before
    finally:
        agent.stop()
        runner.stop()


def test_status_without_a_wifi_worker_still_has_the_field():
    from tests.test_ui_remote import call, make_session

    session, runner, _ = make_session()
    agent = Agent(session, port=0, host="127.0.0.1")
    agent.start()
    try:
        code, status = call(agent, "/status")
        assert code == 200
        assert status["wifi"] == {"ssid": None, "ip": None, "signal": None,
                                  "mode": None, "profile": None}
    finally:
        agent.stop()
        runner.stop()


# ---- the screen ----

def test_the_screen_renders_every_phase():
    rows = [(ROUTER, "client", True), (HOTSPOT, "hotspot", False)]
    for phase, status in ((IDLE, ""), (CONNECTING, f"connecting to {HOTSPOT}…"),
                          (DONE, f"on {HOTSPOT}"),
                          (FAILED, f"ERROR {REFUSAL} - back on {ROUTER}"),
                          (LOCKED, LOCKED_NOTE)):
        image = render.wifi_screen(ROUTER, "IP 192.168.51.103  72%  client",
                                   rows, 1, phase, status=status, host="radxa-03")
        assert image.size == (WIDTH, HEIGHT)
    empty = render.wifi_screen("not connected", "nmcli: nmcli not installed",
                               [], 0, IDLE, host="radxa-03")
    assert empty.size == (WIDTH, HEIGHT)
    many = [(f"a rather long network name {i}", "client", i == 3)
            for i in range(9)]
    assert render.wifi_screen("reading...", "", many, 8, IDLE).size == (WIDTH, HEIGHT)
    assert render.wifi_screen(ROUTER, "", rows, 0, IDLE,
                              locked=True).size == (WIDTH, HEIGHT)


def test_the_app_draws_wifi_and_redraws_when_the_cache_lands():
    wifi, fake = make_wifi()
    fake.hold_reads.clear()                     # the reader waits on nmcli
    app, _ = make_app(wifi)
    started = time.monotonic()
    enter(app)
    assert time.monotonic() - started < 0.5     # the LCD loop never waits
    app.draw()
    first = app._display_key()
    assert first[0] == "wifi" and wifi.current() == ("reading...", "")
    fake.hold_reads.set()
    assert wait_until(lambda: wifi.read_state == "read")   # refresh()'s read
    assert app._display_key() != first
    app.draw()
    frames = app.display.frames
    app.tick(wait=0.0)
    app.tick(wait=0.0)
    assert app.display.frames == frames         # nothing changed: no repaint
    wifi.select(+1)
    app.tick(wait=0.0)
    assert app.display.frames == frames + 1
