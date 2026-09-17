"""Repo update mode: the GIT PULL menu row and ui/puller.py.

git is replaced by a fake command runner, so the flow - read HEAD,
pull fast-forward only, report moved/unchanged/failed, restart on KEY1
by exiting the process for systemd to bring back - is covered without
a checkout or a network.
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
from ui.puller import DONE, FAILED, IDLE, PULLING, RepoPuller
from tests.test_ui_app import FakeRunner
from tests.test_ui_runner import wait_until


class FakeGit:
    """Answers the few git commands the puller issues.

    `pull` moves HEAD to `upstream` when that differs from `head`, and
    prints what git prints; `fail` makes it exit non-zero with that
    text instead; `hang` raises the subprocess timeout.
    """

    def __init__(self, head=("b5706ee", "Bundle FW_260917"),
                 upstream=None, fail=None, hang=False):
        self.head = head
        self.upstream = upstream or head
        self.fail = fail
        self.hang = hang
        self.calls = []
        self.release = threading.Event()
        self.release.set()

    def __call__(self, args, timeout):
        self.calls.append(list(args))
        if args[:2] == ["rev-parse", "--short"]:
            return 0, self.head[0] + "\n"
        if args[:1] == ["log"]:
            return 0, self.head[1] + "\n"
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return 0, "main\n"
        assert args[0] == "pull" and "--ff-only" in args
        self.release.wait(5.0)
        if self.hang:
            raise subprocess.TimeoutExpired("git", timeout)
        if self.fail:
            return 1, f"fatal: {self.fail}\n"
        if self.upstream == self.head:
            return 0, "Already up to date.\n"
        old, self.head = self.head, self.upstream
        return 0, (f"Updating {old[0]}..{self.head[0]}\nFast-forward\n"
                   " ui/app.py | 40 ++--\n")


def make_puller(**kwargs):
    git = FakeGit(**kwargs)
    return RepoPuller(Path("/nowhere"), run=git, timeout=1.0,
                      echo_log=False), git


def make_app(puller, events=()):
    runner = FakeRunner()
    app = App(NullDisplay(), ScriptedInput(events), runner,
              port_label="/dev/fake", puller=puller, host="radxa-03")
    return app, runner


# ---- the puller ----

def test_menu_row_names_the_current_commit():
    puller, _ = make_puller()
    assert puller.menu_entry.key == "pull"
    assert puller.menu_entry.label == "GIT PULL"
    assert puller.menu_entry.detail == "repo at b5706ee Bundle FW_260917"
    assert puller.phase == IDLE and not puller.changed


def test_pull_that_moves_head_reports_the_new_commit():
    puller, git = make_puller(upstream=("c0ffee1", "Add GIT PULL"))
    git.release.clear()
    puller.start()
    assert puller.busy
    git.release.set()
    assert wait_until(lambda: puller.phase == DONE)
    assert puller.changed
    assert puller.before.commit == "b5706ee"
    assert puller.after.label == "c0ffee1 Add GIT PULL"
    lines = puller.recent(10)
    assert any("Fast-forward" in line for line in lines)
    assert any("b5706ee -> c0ffee1" in line for line in lines)
    assert ["pull", "--ff-only"] in git.calls


def test_pull_with_nothing_new_is_not_a_change():
    puller, _ = make_puller()
    puller.start()
    assert wait_until(lambda: puller.phase == DONE)
    assert not puller.changed
    assert puller.after.commit == puller.before.commit
    assert any("up to date" in line for line in puller.recent(10))


def test_failed_pull_is_reported_not_raised():
    puller, _ = make_puller(fail="unable to access 'https://github.com/x'")
    puller.start()
    assert wait_until(lambda: puller.phase == FAILED)
    assert "exit 1" in puller.error
    assert puller.after is None and not puller.changed
    assert any("ERROR fatal: unable to access" in line
               for line in puller.recent(10))
    puller.reset()
    assert puller.phase == IDLE and puller.error is None


def test_hung_network_times_out_instead_of_waiting_forever():
    puller, _ = make_puller(hang=True)
    puller.start()
    assert wait_until(lambda: puller.phase == FAILED)
    assert "timed out" in puller.error


def test_not_a_checkout_fails_immediately():
    def no_git(args, timeout):
        return 128, "fatal: not a git repository"

    puller = RepoPuller(Path("/nowhere"), run=no_git, echo_log=False)
    assert puller.before.commit == "?"
    puller.start()
    assert wait_until(lambda: puller.phase == FAILED)
    assert "not a git checkout" in puller.error


def test_run_git_never_prompts_and_survives_a_missing_binary(monkeypatch):
    from ui import puller as puller_mod

    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"], seen["env"] = cmd, kwargs["env"]
        raise FileNotFoundError

    monkeypatch.setattr(puller_mod.subprocess, "run", fake_run)
    code, out = puller_mod.run_git(Path("/repo"), ["status"], 5.0)
    assert code == 127 and "not installed" in out
    assert seen["cmd"][:3] == ["git", "-C", str(Path("/repo"))]
    assert seen["env"]["GIT_TERMINAL_PROMPT"] == "0"


# ---- the App ----

def test_pull_row_exists_only_with_a_puller():
    plain = App(NullDisplay(), ScriptedInput(()), FakeRunner())
    assert [p.key for p in plain.patterns] == [p.key for p in PATTERNS]
    puller, _ = make_puller()
    app, _ = make_app(puller)
    assert app.patterns[-1].key == "pull"


def test_key1_enters_pull_screen_without_stopping_the_runner():
    puller, _ = make_puller()
    app, runner = make_app(puller)
    app.select("pull")
    app.handle("key1")
    assert app.screen is Screen.PULL
    assert runner.stops == 0
    assert puller.phase == IDLE
    app.handle("key2")
    assert app.screen is Screen.MENU


def test_key1_pulls_and_then_restarts_by_quitting():
    puller, _ = make_puller(upstream=("c0ffee1", "Add GIT PULL"))
    app, runner = make_app(puller)
    app.select("pull")
    app.handle("key1")                  # enter
    app.handle("key1")                  # pull
    assert wait_until(lambda: puller.phase == DONE)
    assert puller.changed and not app.quit
    app.handle("key1")                  # restart = exit for systemd
    assert app.quit
    assert app.exit_message[0] == "restarting"
    assert "c0ffee1" in app.exit_message[1]


def test_buttons_are_ignored_while_pulling():
    puller, git = make_puller(upstream=("c0ffee1", "Add GIT PULL"))
    git.release.clear()
    app, runner = make_app(puller)
    app.select("pull")
    app.handle("key1")
    app.handle("key1")
    assert wait_until(lambda: puller.phase == PULLING)
    for event in ("key2", "key1", "down", "key1_hold", "press"):
        app.handle(event)
    assert app.screen is Screen.PULL and not app.quit
    assert runner.starts == []
    git.release.set()
    assert wait_until(lambda: puller.phase == DONE)


def test_up_to_date_key1_offers_another_pull_not_a_restart():
    puller, _ = make_puller()
    app, _ = make_app(puller)
    app.select("pull")
    app.handle("key1")
    app.handle("key1")
    assert wait_until(lambda: puller.phase == DONE)
    app.handle("key1")
    assert not app.quit
    assert puller.phase == IDLE           # back to the confirm screen


def test_pull_screen_redraws_as_the_log_grows():
    puller, _ = make_puller()
    app, _ = make_app(puller)
    app.select("pull")
    app.handle("key1")
    app.draw()
    before = app.display.frames
    puller.emit("something happened")
    app.tick(wait=0.0)
    assert app.display.frames == before + 1
    app.tick(wait=0.0)
    assert app.display.frames == before + 1


def test_run_ends_on_the_restart_message():
    puller, _ = make_puller(upstream=("c0ffee1", "Add GIT PULL"))
    app, _ = make_app(puller)
    app.select("pull")
    app.handle("key1")                  # enter
    app.handle("key1")                  # pull
    assert wait_until(lambda: puller.phase == DONE)
    app.inputs = ScriptedInput(("key1",))   # restart
    app.run(max_ticks=5)
    assert app.quit
    assert app.display.frames >= 2       # the restart screen was shown


# ---- rendering ----

def test_pull_screen_renders_every_phase():
    for phase, changed in (("idle", False), ("pulling", False),
                           ("done", True), ("done", False), ("failed", False)):
        image = render.pull_screen("b5706ee Bundle", "c0ffee1 Add" if changed
                                   else None, phase, ["11:00:00 x"],
                                   error="boom" if phase == "failed" else None,
                                   changed=changed, host="radxa-03")
        assert image.size == (WIDTH, HEIGHT)


def test_hostname_shows_on_every_screen():
    menu = render.menu_screen(PATTERNS, 0, "/dev/x")
    named = render.menu_screen(PATTERNS, 0, "/dev/x", host="radxa-07")
    assert menu.tobytes() != named.tobytes()
    run = render.running_screen("WAVE", 1.0, 1, [])
    run_named = render.running_screen("WAVE", 1.0, 1, [], host="radxa-07")
    assert run.tobytes() != run_named.tobytes()
    upd = render.update_screen("fw", 10, 1, "idle", "", 0, [])
    upd_named = render.update_screen("fw", 10, 1, "idle", "", 0, [],
                                     host="radxa-07")
    assert upd.tobytes() != upd_named.tobytes()
    msg = render.message_screen("stopped")
    msg_named = render.message_screen("stopped", host="radxa-07")
    assert msg.tobytes() != msg_named.tobytes()
