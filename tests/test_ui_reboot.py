"""Reboot mode: the REBOOT menu row and ui/rebooter.py.

The OS command is replaced by a fake, so the flow - confirm screen,
KEY1 *hold* to go, refusal reported and retryable, buttons dead once the
reboot is accepted - is covered without rebooting the test machine.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ui import render
from ui.app import App, Screen
from ui.config import HEIGHT, WIDTH
from ui.display import NullDisplay
from ui.inputs import ScriptedInput
from ui.patterns import PATTERNS
from ui.rebooter import FAILED, IDLE, REBOOT_COMMAND, REBOOTING, Rebooter
from tests.test_ui_app import FakeRunner
from tests.test_ui_runner import wait_until


class FakeOS:
    """Stands in for `sudo -n systemctl reboot`.

    `code`/`output` are what the command answers; `hang` raises the
    subprocess timeout; `release` holds the call so a test can look at
    the screen while it is in flight.
    """

    def __init__(self, code=0, output="", hang=False):
        self.code = code
        self.output = output
        self.hang = hang
        self.calls = []
        self.release = threading.Event()
        self.release.set()

    def __call__(self, args, timeout):
        self.calls.append(list(args))
        self.release.wait(5.0)
        if self.hang:
            raise subprocess.TimeoutExpired(args[0], timeout)
        return self.code, self.output


def make_rebooter(**kwargs):
    fake = FakeOS(**kwargs)
    return Rebooter(run=fake, timeout=1.0, echo_log=False), fake


def make_app(rebooter, events=()):
    runner = FakeRunner()
    app = App(NullDisplay(), ScriptedInput(events), runner,
              port_label="/dev/fake", rebooter=rebooter, host="radxa-03")
    return app, runner


def enter(app):
    app.select("reboot")
    app.handle("key1")


# ---- the rebooter ----

def test_menu_row_and_default_command():
    rebooter, _ = make_rebooter()
    assert rebooter.menu_entry.key == "reboot"
    assert rebooter.menu_entry.label == "REBOOT"
    assert rebooter.phase == IDLE
    # Passwordless sudo, no prompt: the same route the USB rebind takes.
    assert REBOOT_COMMAND == ["sudo", "-n", "systemctl", "reboot"]


def test_accepted_reboot_stays_in_rebooting():
    rebooter, fake = make_rebooter()
    rebooter.start()
    assert wait_until(lambda: any("going down" in line
                                  for line in rebooter.recent(10)))
    assert rebooter.phase == REBOOTING and rebooter.busy
    assert fake.calls == [REBOOT_COMMAND]
    assert rebooter.error is None


def test_refused_reboot_is_reported_and_can_be_reset():
    rebooter, _ = make_rebooter(code=1, output="sudo: a password is required")
    rebooter.start()
    assert wait_until(lambda: rebooter.phase == FAILED)
    assert "exit 1" in rebooter.error
    assert any("ERROR sudo: a password is required" in line
               for line in rebooter.recent(10))
    rebooter.reset()
    assert rebooter.phase == IDLE and rebooter.error is None


def test_hung_command_times_out_as_a_failure():
    rebooter, _ = make_rebooter(hang=True)
    rebooter.start()
    assert wait_until(lambda: rebooter.phase == FAILED)
    assert "timed out" in rebooter.error


def test_a_second_start_while_rebooting_is_ignored():
    rebooter, fake = make_rebooter()
    fake.release.clear()
    rebooter.start()
    rebooter.start()
    fake.release.set()
    rebooter.join(2.0)
    assert len(fake.calls) == 1


def test_run_command_survives_a_missing_binary(monkeypatch):
    from ui import rebooter as mod

    def missing(cmd, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(mod.subprocess, "run", missing)
    code, out = mod.run_command(["sudo", "-n", "x"], 5.0)
    assert code == 127 and "not installed" in out


def test_run_command_never_reads_stdin(monkeypatch):
    from ui import rebooter as mod

    seen = {}

    def fake_run(cmd, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    mod.run_command(["true"], 5.0)
    assert seen["stdin"] == subprocess.DEVNULL


# ---- the App ----

def test_reboot_row_exists_only_with_a_rebooter():
    plain = App(NullDisplay(), ScriptedInput(()), FakeRunner())
    assert [p.key for p in plain.patterns] == [p.key for p in PATTERNS]
    rebooter, _ = make_rebooter()
    app, _ = make_app(rebooter)
    assert app.patterns[-1].key == "reboot"


def test_key1_opens_the_confirm_screen_without_rebooting():
    rebooter, fake = make_rebooter()
    app, runner = make_app(rebooter)
    enter(app)
    assert app.screen is Screen.REBOOT
    assert runner.stops == 0 and runner.starts == []
    assert fake.calls == []
    app.handle("key2")
    assert app.screen is Screen.MENU


def test_a_plain_press_never_reboots():
    rebooter, fake = make_rebooter()
    app, _ = make_app(rebooter)
    enter(app)
    for event in ("key1", "press", "up", "down", "left", "right"):
        app.handle(event)
    rebooter.join(0.2)
    assert fake.calls == []
    assert app.screen is Screen.REBOOT and rebooter.phase == IDLE


def test_key1_hold_reboots_and_leaves_the_runner_alone():
    rebooter, fake = make_rebooter()
    app, runner = make_app(rebooter)
    enter(app)
    app.handle("key1_hold")
    assert wait_until(lambda: rebooter.phase == REBOOTING and fake.calls)
    assert fake.calls == [REBOOT_COMMAND]
    assert runner.stops == 0
    assert not app.quit


def test_buttons_are_dead_once_the_reboot_is_accepted():
    rebooter, fake = make_rebooter()
    app, runner = make_app(rebooter)
    enter(app)
    app.handle("key1_hold")
    assert wait_until(lambda: rebooter.busy and fake.calls)
    for event in ("key2", "key1", "down", "key1_hold", "press"):
        app.handle(event)
    assert app.screen is Screen.REBOOT
    assert len(fake.calls) == 1


def test_refusal_shows_on_screen_and_hold_retries():
    rebooter, fake = make_rebooter(code=1, output="sudo: a password is required")
    app, _ = make_app(rebooter)
    enter(app)
    app.handle("key1_hold")
    assert wait_until(lambda: rebooter.phase == FAILED)
    app.handle("key1_hold")                   # retry
    assert wait_until(lambda: len(fake.calls) == 2)
    assert wait_until(lambda: rebooter.phase == FAILED)
    app.handle("key2")
    assert app.screen is Screen.MENU and rebooter.phase == IDLE


def test_locked_unit_cannot_reboot():
    rebooter, fake = make_rebooter()
    app = App(NullDisplay(), ScriptedInput(()), FakeRunner(),
              rebooter=rebooter, locked=True)
    app.select("reboot")
    app.handle("key1")
    app.handle("key1_hold")
    assert app.screen is Screen.MENU
    assert fake.calls == []


def test_reboot_screen_redraws_as_the_log_grows():
    rebooter, _ = make_rebooter()
    app, _ = make_app(rebooter)
    enter(app)
    app.draw()
    before = app.display.frames
    rebooter.emit("something happened")
    app.tick(wait=0.0)
    assert app.display.frames == before + 1
    app.tick(wait=0.0)
    assert app.display.frames == before + 1


# ---- rendering ----

def test_reboot_screen_renders_every_phase():
    for phase in ("idle", "rebooting", "failed"):
        image = render.reboot_screen(
            phase, ["12:00:00 reboot: sudo -n systemctl reboot"],
            error="reboot refused (exit 1)" if phase == "failed" else None,
            host="radxa-03")
        assert image.size == (WIDTH, HEIGHT)
    assert render.reboot_screen("idle", [], locked=True).size == (WIDTH, HEIGHT)
