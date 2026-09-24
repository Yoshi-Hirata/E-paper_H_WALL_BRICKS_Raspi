"""UI state machine: menu -> running, driven by HAT events.

Controls (Waveshare 1.3inch LCD HAT):
  joystick up/down/left/right - choose a demo pattern
  KEY1                        - start; then pause; then resume
  KEY1 held ~1 s              - reset: start again from zero
  KEY2                        - back to the menu (stops a running demo)
  KEY3                        - blank the screen now

The last menu row, UPDATE FW, is a mode rather than a demo: it stops
the runner to free the port, scans for the USB-attached board's address
(UP/DOWN override it), and KEY1 flashes the bundled image
(ui/updater.py). While the
transfer runs every button is ignored - there is no safe "abort" of an
OTA in flight - and afterwards KEY2 simply returns to the menu: no
white repaint on the way out (removed 2026-09-17 at the operator's
request), so the rebooted board keeps playing whatever it wakes up
with until a demo is started or STANDBY is chosen.

FW VERSION asks every configured address its OTA state (0x29) and
lists what each answering board runs (ui/versions.py) - the runner is
stopped for the scan and stays stopped on the way out, no repaint.

GIT PULL, the last row, updates the checkout itself: `git pull
--ff-only` in the repo the service runs from (ui/puller.py), and when
the commit moved KEY1 exits the process - the service is Restart=always,
so systemd brings the UI back on the new code. Ten identical units
tell apart by the hostname in the top strip of every screen.

REBOOT restarts the whole unit (ui/rebooter.py). Its row only opens a
confirm screen, and the reboot needs KEY1 *held* - a plain press does
nothing there - so a double-tap on the menu cannot take a unit down.

The screen also blanks itself after BLANK_AFTER_S without input. Any
press wakes it and does nothing else - waking must never move the state
machine, or a blind press in a dark room could stop a running show.

No button quits. This runs as a service on a headless appliance, so a
button that ended the process would leave the screen dark until someone
SSHed in.

REMOTE is not a menu row: the show PC takes the unit over through
the agent (ui/agent.py, ui/remote.py) and the screen follows - what is
loaded, how many boards took it, the countdown to the fire time. KEY2
hands the unit back to its own menu; on a locked unit nothing does, so
a knock on stage cannot drop a garment out of the show.

A standalone demo (ui/demos.py) IS a menu row, one per show the PC has
written into this unit, sorted in right after STANDBY. KEY1 loads it
(ui/showplay.py's ShowPlayer, with demo=True so a reboot does not
resume it on its own) and the DEMO screen opens right away, on the
burn ui/remote.py's RemoteSession.burn() just started - it shows
"writing pictures n/N" (status.show.burn) while that runs, and KEY2
during it cancels the burn and returns to the menu, same as any other
time on this screen. Once burn.state is "burned" the App itself calls
run() - once, not on every tick - and the screen then follows a
running demo exactly like REMOTE follows a PC-driven show, because the
player fires its cues through the very same session a show PC would.
A burn that instead comes back "failed" with a board that is live but
still would not take the write shows "N boards failed - KEY2 menu" and
never runs (a board that is simply absent is not counted - the same
gap ShowPlayer.run() itself accepts). KEY2 releases the session (ending
the demo and any lingering "REMOTE" claim on it in one move, see
_stop_demo); KEY1 held restarts it from 0:00 (a fresh load - the burn
cache makes an unchanged file's re-burn cost nothing); ENDED with
`loop` set restarts it again after LOOP_GAP_S, a re-run only, never a
re-burn. A show PC still wins: /prepare and /show/load are refused
while the demo runs (ui/agent.py), so the operator presses STOP on the
Units tab, which is /show/stop and ends the demo the same way KEY2
does.

With `locked` set the buttons do nothing at all, except that they still
wake the screen; the UNLOCK_SEQUENCE frees them temporarily and the lock
returns by itself after RELOCK_AFTER_S of quiet.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum

from . import notify, render
from .config import (BLANK_AFTER_S, FRAME_INTERVAL_S, LOG_LINES,
                     RELOCK_AFTER_S, UNLOCK_SEQUENCE, UNLOCK_WINDOW_S,
                     WATCHDOG_PERIOD_S)
from .patterns import PATTERNS
from .remote import RemoteError
from .runner import DemoRunner
from .showplay import ENDED, STOPPED

# Standalone demos poll the store rather than being pushed a change from
# the agent's HTTP thread - the two run in the same process but talking
# across threads for a menu redraw would be one more lock to get wrong,
# and a couple of seconds' delay in a new row appearing costs nothing.
DEMO_POLL_S = 2.0
# ENDED, loop set: how long the last picture stays up before it plays
# again - long enough to read, short enough not to look stuck.
LOOP_GAP_S = 5.0


class Screen(Enum):
    MENU = "menu"
    RUNNING = "running"
    UPDATE = "update"
    VERSIONS = "versions"
    PULL = "pull"
    REBOOT = "reboot"
    REMOTE = "remote"
    DEMO = "demo"


@dataclass(frozen=True)
class DemoRow:
    """A stored demo as a menu row - duck-types enough of Pattern (key,
    label, detail) for menu_screen and _restart() to treat it as one."""
    key: str            # "demo:<slug>"
    label: str          # the name as written on the PC
    detail: str         # "show · N cues · m:ss [· loop]"
    slug: str
    loop: bool


class App:
    def __init__(self, display, inputs, runner: DemoRunner | None = None,
                 patterns=None, port_label: str | None = None,
                 locked: bool = False, blank_after: float = BLANK_AFTER_S,
                 relock_after: float = RELOCK_AFTER_S,
                 clock=time.monotonic, updater=None, puller=None,
                 host: str | None = None, versions=None, rebooter=None,
                 remote=None, player=None, demos=None):
        self.display = display
        self.inputs = inputs
        self.runner = runner or DemoRunner()
        self.patterns = list(patterns or PATTERNS)
        # The update and pull modes are menu rows only when their
        # workers are wired in, so a headless or test App keeps the
        # plain demo menu.
        self.updater = updater
        if updater is not None:
            self.patterns.append(updater.menu_entry)
        self.versions = versions
        if versions is not None:
            self.patterns.append(versions.menu_entry)
        self.puller = puller
        if puller is not None:
            self.patterns.append(puller.menu_entry)
        self.rebooter = rebooter
        if rebooter is not None:
            self.patterns.append(rebooter.menu_entry)
        # The show PC's session (ui/remote.py). It may not take the port
        # while something that must not be interrupted holds the unit.
        self.remote = remote
        self.show_status = None        # set by main: the show player's status
        if remote is not None:
            remote.busy = lambda: any(
                worker is not None and worker.busy
                for worker in (self.updater, self.versions, self.rebooter))
        # Standalone demos (ui/demos.py): `player` is the same ShowPlayer
        # the PC's shows run on, `demos` is the store menu rows are built
        # from. Either may be None (older callers, or --no-remote), and
        # the whole feature is then simply absent - no demo rows, no
        # DEMO screen.
        self.player = player
        self.demo_store = demos
        self._demo_rows: list[DemoRow] = []
        self._playing_demo: str | None = None
        self._demo_name = ""
        self._demo_loop = False
        self._demo_ended_at: float | None = None
        self._demo_show_id: str | None = None
        # Pre-burn (2026-09-25): _start_demo_show() only loads (which
        # starts the burn); _track_demo() calls run() itself, exactly
        # once, the first tick it sees the burn settle.
        self._demo_awaiting_run = False
        # Set instead, when that settling was "failed" with a board that
        # is live but still refused the write - shown on the DEMO screen
        # until KEY2 (_stop_demo clears it).
        self._demo_burn_error: str | None = None
        self._last_demo_poll = 0.0
        # A short-lived note on the MENU screen (e.g. a refused KEY1 on a
        # demo row while the PC's own show is loaded) - _standby_status()
        # shows it in place of the usual standby/port line until it times
        # out, same slot, no new screen needed.
        self._menu_note = ""
        self._menu_note_until = 0.0
        self.host = host
        self.selected = 0
        self.screen = Screen.MENU
        self.port_label = port_label
        self.quit = False
        # What the screen says once run() ends; a restart asked for from
        # the GIT PULL screen replaces the default "service ended".
        self.exit_message = ("stopped", "service ended")
        self.blanked = False
        self.locked = locked
        self.blank_after = blank_after
        self.relock_after = relock_after
        self._clock = clock
        # Only a device that started locked re-locks itself; unlocking an
        # unlocked device would be a surprise.
        self._locks_itself = locked
        self._unlock_progress: list[str] = []
        self._unlock_started = 0.0
        self._last_input = clock()
        self._last_pet = 0.0
        self._dirty = True
        self._drawn_key = None
        self._standby = False
        self.refresh_demos()           # initial rows, right after STANDBY

    # ---- state transitions ----

    def select(self, key: str) -> None:
        """Move the menu cursor to a pattern by key."""
        for index, pattern in enumerate(self.patterns):
            if pattern.key == key:
                self.selected = index
                self._dirty = True
                return
        raise KeyError(f"unknown pattern: {key}")

    def _try_unlock(self, event: str) -> None:
        now = self._clock()
        if self._unlock_progress and now - self._unlock_started > UNLOCK_WINDOW_S:
            self._unlock_progress.clear()
        if not self._unlock_progress:
            self._unlock_started = now
        expected = UNLOCK_SEQUENCE[len(self._unlock_progress)]
        if event == expected:
            self._unlock_progress.append(event)
            if len(self._unlock_progress) == len(UNLOCK_SEQUENCE):
                self._unlock_progress.clear()
                self.locked = False
                self._dirty = True
        else:
            self._unlock_progress.clear()

    def handle(self, event: str) -> None:
        now = self._clock()
        if self.blanked:
            # Any press wakes the screen and is consumed doing so, so a
            # blind press cannot also change what is running.
            self.blanked = False
            self.display.wake()
            self._dirty = True
            self._last_input = now
            return
        self._last_input = now

        if self.locked:
            self._try_unlock(event)
            return

        if event == "key3":
            self._blank()
            return

        if self.screen is Screen.UPDATE:
            self._handle_update(event)
            return
        if self.screen is Screen.PULL:
            self._handle_pull(event)
            return
        if self.screen is Screen.VERSIONS:
            self._handle_versions(event)
            return
        if self.screen is Screen.REBOOT:
            self._handle_reboot(event)
            return
        if self.screen is Screen.REMOTE:
            if event == "key2":
                self.remote.release()
                self._standby = False
                self.screen = Screen.MENU
                self._dirty = True
            return
        if self.screen is Screen.DEMO:
            if event == "key2":
                self._stop_demo()
            elif event == "key1_hold":
                # Restart from 0:00 - never _restart()/patterns[selected]:
                # if the playing demo's row was deleted from the PC while
                # it played, the cursor has since been clamped onto a
                # neighbour, and going through the menu would start THAT
                # one instead (a different demo, a pattern, even STANDBY -
                # whitening every panel on stage).
                if self._playing_demo is not None and not self._start_demo_show(
                        self._playing_demo):
                    self._stop_demo()
            return                      # KEY1 alone does nothing here

        if event == "key1_hold":
            # Reset: back to cycle 0 with the timer at zero, wherever we
            # were. Distinct from pause, which keeps both.
            self._restart()
            return

        if self.screen is Screen.MENU:
            if event in ("up", "left"):
                self.selected = (self.selected - 1) % len(self.patterns)
                self._dirty = True
            elif event in ("down", "right"):
                self.selected = (self.selected + 1) % len(self.patterns)
                self._dirty = True
            elif event in ("key1", "press"):
                self._restart()
        else:  # RUNNING
            if event in ("key1", "press"):
                # Pause and resume keep the cycle count and the timer; the
                # panels hold their image while paused.
                if self.runner.paused:
                    self.runner.resume()
                elif self.runner.running:
                    self.runner.pause()
                else:
                    self._restart()
                self._dirty = True
            elif event == "key2":
                self.runner.stop()
                self.screen = Screen.MENU
                self._dirty = True

    def _handle_update(self, event: str) -> None:
        updater = self.updater
        if updater.busy:
            return                  # nothing interrupts a flash in flight
        if updater.finished:
            if event in ("key1", "press"):
                updater.reset()     # back to the confirm screen
                updater.scan()
            elif event == "key2":
                self._leave_update()
            self._dirty = True
            return
        if event == "up":
            updater.select(-1)
        elif event == "down":
            updater.select(+1)
        elif event == "left":
            updater.select_image(-1)     # an older build, to go back to
        elif event == "right":
            updater.select_image(+1)
        elif event in ("key1", "press"):
            updater.start()
        elif event == "key2":
            self._leave_update()
        self._dirty = True

    def _handle_pull(self, event: str) -> None:
        puller = self.puller
        if puller.busy:
            return                  # a pull in flight is left alone
        if event in ("key1", "press"):
            if puller.changed:
                # The new code only runs in a new process; systemd
                # restarts the service when this one exits.
                self.exit_message = ("restarting",
                                     f"now at {puller.after.commit}, "
                                     "UI back in ~15 s")
                self.quit = True
            elif puller.finished:
                puller.reset()
            else:
                puller.start()
        elif event == "key2":
            self.screen = Screen.MENU
        self._dirty = True

    def _handle_reboot(self, event: str) -> None:
        rebooter = self.rebooter
        if rebooter.busy:
            return                  # accepted: the OS is on its way down
        if event == "key1_hold":
            # Only the hold reboots. It also retries after a refusal.
            rebooter.reset()
            rebooter.start()
        elif event == "key2":
            rebooter.reset()
            self.screen = Screen.MENU
        self._dirty = True

    def _handle_versions(self, event: str) -> None:
        versions = self.versions
        if versions.busy:
            return                  # the scan holds the port; let it finish
        if event in ("up", "left"):
            versions.scroll(-1, render.VERSION_ROWS)
        elif event in ("down", "right"):
            versions.scroll(+1, render.VERSION_ROWS)
        elif event in ("key1", "press"):
            versions.scan()
        elif event == "key2":
            # Same exit as UPDATE FW: straight back to the menu, no
            # white repaint (the scan only sent PLAY_STOP and 0x29, the
            # panels still hold whatever they showed before).
            self.screen = Screen.MENU
        self._dirty = True

    def _enter_versions(self) -> None:
        self.runner.stop()
        self._standby = False
        self.versions.scan()
        self.screen = Screen.VERSIONS
        self._dirty = True

    def _enter_reboot(self) -> None:
        # Like GIT PULL the runner keeps the port: backing out with KEY2
        # must leave a running demo exactly as it was.
        self.rebooter.reset()
        self.screen = Screen.REBOOT
        self._dirty = True

    def _enter_pull(self) -> None:
        # The runner keeps the port: a pull touches only the checkout,
        # and the restart that applies it stops everything anyway.
        self.puller.reset()
        self.screen = Screen.PULL
        self._dirty = True

    def _enter_update(self) -> None:
        # The runner owns the serial port (standby keeps it open to
        # watch the link), and the OTA needs it to itself.
        self.runner.stop()
        self._standby = False
        self.updater.reset()
        self.updater.scan()         # find the USB board's address
        self.screen = Screen.UPDATE
        self._dirty = True

    def _leave_update(self) -> None:
        # Straight back to the menu. The runner stays stopped and the
        # panels keep whatever the rebooted board is showing; the
        # operator picks STANDBY or a demo when ready. (The white
        # repaint that used to run here was dropped 2026-09-17: a full
        # e-paper refresh just to leave the screen was unwanted.)
        self.screen = Screen.MENU
        self._dirty = True

    # ---- standalone demos (ui/demos.py) ----

    def _start_demo_show(self, slug: str, name: "str | None" = None) -> bool:
        """Load the stored show fresh (KEY1, and KEY1-hold's restart - the
        operator may have just re-written this very slug); only starts
        the burn - _track_demo() calls run() itself once it settles.
        False if the load could not even start (a corrupt or
        since-deleted file - /demo/save validates, so this is mainly the
        "deleted from the PC while it played" case); the caller falls
        back to the menu rather than opening a screen with nothing to
        show.

        `name` (the row's label) is what /status.show.demo_name carries
        for the PC's Units tile; a restart that does not pass one again
        (KEY1-hold) keeps whatever _enter_demo() set the first time."""
        try:
            show = self.demo_store.load(slug)
            self.player.load(show, demo=True,
                             name=self._demo_name if name is None else name)
            self._demo_show_id = show["id"]
            self._demo_awaiting_run = True
            self._demo_burn_error = None
            return True
        except RemoteError:
            return False

    def _loop_demo_show(self) -> bool:
        """ENDED, loop set: run the already-loaded show again from 0:00 -
        no reload (so no re-burn either - the picture in every slot is
        already right), no re-write of the ~100-300 KB show file to disk
        every lap (run() starting a new top forgets the garment on its
        own, so nothing about a stale load would show)."""
        show = self.player.show if self.player else None
        if show is None or not self.player.is_demo:
            return False
        try:
            lead = self.player._lead(show["cues"][0]) + 1.0
            self.player.run(self._clock() + lead)
            return True
        except RemoteError:
            return False

    def _await_demo_burn(self) -> None:
        """One tick of waiting out _start_demo_show()'s burn: still
        "burning" is a no-op (the DEMO screen's hint reads the count
        straight off status.show.burn each frame), any other state ends
        the wait - run() once, and a refusal (a live board would not
        take the write) becomes the screen's error instead of trying
        again on its own."""
        player = self.player
        status = player.status()
        burn = (status or {}).get("burn")
        if burn is not None and burn["state"] == "burning":
            self._dirty = True
            return
        self._demo_awaiting_run = False
        try:
            lead = player._lead(player.show["cues"][0]) + 1.0
            player.run(self._clock() + lead)
        except RemoteError as exc:
            if burn is not None and burn["state"] == "failed":
                absent = player.session.runner.absent
                failed = {b for b, s in burn.get("failed", ())
                         if b not in absent}
                self._demo_burn_error = f"{len(failed)} boards failed - KEY2 menu"
            else:
                # Some other refusal (e.g. the unit went busy under us) -
                # say what it actually was rather than guess "boards".
                self._demo_burn_error = f"{exc} - KEY2 menu"
        self._dirty = True

    def _enter_demo(self, row: "DemoRow") -> None:
        if self.player is None or self.demo_store is None:
            return
        player = self.player
        if (player.show is not None and not player.is_demo
                and player.state != STOPPED):
            # A PC show is loaded (or ended and not yet let go of) - the
            # PC always wins; loading over it here would either fight a
            # show in progress or make the PC's own next /show/load see a
            # mismatched id and refuse itself pointlessly.
            self._note_on_menu("PC show loaded - use the PC")
            return
        if not self._start_demo_show(row.slug, row.label):
            return
        self._standby = False
        self._playing_demo = row.slug
        self._demo_name = row.label
        self._demo_loop = row.loop
        self._demo_ended_at = None
        self.screen = Screen.DEMO
        self._dirty = True

    def _note_on_menu(self, text: str, seconds: float = 3.0) -> None:
        self._menu_note = text
        self._menu_note_until = self._clock() + seconds
        self._dirty = True

    def _stop_demo(self) -> None:
        # release(), not player.stop(): the player fires its cues through
        # the very session a show PC would use, so ending the demo has to
        # also let go of that session (active -> False) or _follow_remote
        # would read it as the PC still owning the unit and bounce the
        # screen to REMOTE the moment it next ticks.
        if self.remote is not None:
            self.remote.release()
        elif self.player is not None:
            self.player.stop()
        # player.stop() (reached above through on_release, or directly)
        # itself calls session.cancel_burn() - KEY2 during "writing
        # pictures n/N" gives up on it the same way.
        self._playing_demo = None
        self._demo_ended_at = None
        self._demo_awaiting_run = False
        self._demo_burn_error = None
        self.screen = Screen.MENU
        self._dirty = True

    def _track_demo(self) -> None:
        """Loop a finished demo, or notice that the player no longer
        belongs to it - the PC took over (a mismatched id, or is_demo
        turned False under an id that happens to match, both possible
        the moment a demo sits ENDED and /show/load is no longer
        refused) or a reboot cleared it. Either way this demo must never
        fire into what is on the garment now - it is not ours any more."""
        if self._playing_demo is None or self.player is None:
            return
        player = self.player
        if not player.is_demo or (player.show or {}).get("id") != self._demo_show_id:
            self._playing_demo = None
            self._demo_ended_at = None
            self._demo_awaiting_run = False
            self._demo_burn_error = None
            if self.screen is Screen.DEMO:
                self.screen = Screen.MENU
            self._dirty = True
            return
        if self._demo_awaiting_run:
            self._await_demo_burn()
            return
        status = player.status()
        state = status["state"] if status else None
        if state == STOPPED:
            # /show/stop reached the player directly (the PC's STOP button
            # ends a demo the same way KEY2 does) without going through
            # this screen's own KEY2 - catch up so the LCD does not sit on
            # a dead DEMO screen.
            self._stop_demo()
        elif state == ENDED:
            if self._demo_ended_at is None:
                self._demo_ended_at = self._clock()
                self._dirty = True          # "ended" hint comes on
            elif (self._demo_loop
                  and self._clock() - self._demo_ended_at >= LOOP_GAP_S):
                self._demo_ended_at = None
                if not self._loop_demo_show():
                    if player.is_demo:
                        self._stop_demo()   # e.g. the session went busy
                    else:
                        # The PC took the player between the check above
                        # and the lap: releasing the session now would
                        # tear down ITS show. Just let go of the demo.
                        self._playing_demo = None
                        self._demo_ended_at = None
                        self.screen = Screen.MENU
                        self._dirty = True
        else:
            self._demo_ended_at = None

    def refresh_demos(self) -> None:
        """Rebuild the menu rows from the store: at startup, after every
        /demo/save or /demo/delete (polled from _idle_tasks - the agent's
        HTTP thread writes the files, it does not call back into here),
        right after STANDBY and ahead of every built-in pattern."""
        if self.demo_store is None:
            return
        rows = [self._demo_row(entry) for entry in self.demo_store.list()]
        if rows == self._demo_rows:
            return
        selected_key = (self.patterns[self.selected].key
                        if self.patterns else None)
        base = [p for p in self.patterns if p not in self._demo_rows]
        insert_at = next((i + 1 for i, p in enumerate(base)
                          if getattr(p, "key", None) == "standby"), 0)
        self.patterns = base[:insert_at] + rows + base[insert_at:]
        self._demo_rows = rows
        for index, pattern in enumerate(self.patterns):
            if pattern.key == selected_key:
                self.selected = index
                break
        else:
            self.selected = min(self.selected, len(self.patterns) - 1)
        self._dirty = True

    @staticmethod
    def _demo_row(entry: dict) -> "DemoRow":
        minutes, seconds = divmod(int(entry.get("duration") or 0), 60)
        detail = f"show · {entry['cues']} cues · {minutes}:{seconds:02d}"
        if entry.get("loop"):
            detail += " · loop"
        return DemoRow(key=f"demo:{entry['slug']}", label=entry["name"],
                      detail=detail, slug=entry["slug"],
                      loop=bool(entry.get("loop")))

    def _restart(self) -> None:
        if self.patterns[self.selected].key == "update":
            self._enter_update()
            return
        if self.patterns[self.selected].key == "pull":
            self._enter_pull()
            return
        if self.patterns[self.selected].key == "versions":
            self._enter_versions()
            return
        if self.patterns[self.selected].key == "reboot":
            self._enter_reboot()
            return
        if self.patterns[self.selected].key == "standby":
            # The top menu entry is not a looping demo. One shot of the
            # boot standby - every sector white, every board probed -
            # with the outcome reported on the menu.
            self.enter_standby()
            self.screen = Screen.MENU
            return
        row = self.patterns[self.selected]
        if isinstance(row, DemoRow):
            self._enter_demo(row)
            return
        self._standby = False
        self.runner.start(row)
        self.screen = Screen.RUNNING
        self._dirty = True

    def _blank(self) -> None:
        if not self.blanked:
            self.blanked = True
            self.display.sleep()

    # ---- drawing ----

    def enter_standby(self) -> None:
        """Silence the factory autoplay and white out the panels.

        Called once the link is up, before anyone touches a button, so
        the installation waits on white instead of on whatever vendor
        demo frame happened to be mid-play.
        """
        self._standby = True
        self.runner.standby()
        self._dirty = True

    def _standby_status(self) -> str:
        """Menu subtitle while the panels are being blanked, else ''.

        Doubles as the link-check report: standby probes every
        configured board, so the count of boards answering - and any
        error - is the state of the wall. A demo-row refusal's note
        (_note_on_menu) takes this same slot for a few seconds first.
        """
        if self._menu_note and self._clock() < self._menu_note_until:
            return self._menu_note
        if not self._standby:
            return ""
        if self.runner.error:
            return f"ERROR {self.runner.error}"
        expected = getattr(self.runner, "expected", len(self.runner.boards))
        boards = f"{len(self.runner.live)}/{expected}"
        if self.runner.standby_ready:
            return f"standby: white, boards {boards} OK"
        if self.runner.running:
            return f"standby: blanking + check {boards}..."
        return ""

    def frame(self):
        if self.screen is Screen.MENU:
            return render.menu_screen(self.patterns, self.selected,
                                      self.port_label, locked=self.locked,
                                      status=self._standby_status(),
                                      host=self.host)
        if self.screen is Screen.UPDATE:
            updater = self.updater
            return render.update_screen(
                updater.firmware_label, updater.size, updater.addr,
                updater.phase, updater.board_state, updater.done,
                updater.recent(LOG_LINES), error=updater.error,
                locked=self.locked, host=self.host,
                image_choice=updater.image_choice)
        if self.screen is Screen.VERSIONS:
            versions = self.versions
            return render.versions_screen(
                versions.rows, versions.status, versions.phase,
                versions.bundled, offset=versions.offset, locked=self.locked,
                host=self.host)
        if self.screen is Screen.PULL:
            puller = self.puller
            return render.pull_screen(
                puller.before.label,
                puller.after.label if puller.after else None,
                puller.phase, puller.recent(LOG_LINES), error=puller.error,
                changed=puller.changed, locked=self.locked, host=self.host)
        if self.screen is Screen.REMOTE:
            status = self._remote_status()
            return render.remote_screen(
                status, self.runner.recent(LOG_LINES), now=self._mono(),
                locked=self.locked, host=self.host)
        if self.screen is Screen.DEMO:
            status = self._remote_status()
            return render.remote_screen(
                status, self.runner.recent(LOG_LINES), now=self._mono(),
                locked=self.locked, host=self.host,
                title=f"DEMO {self._demo_name}".strip(),
                hint=self._demo_hint(status))
        if self.screen is Screen.REBOOT:
            rebooter = self.rebooter
            return render.reboot_screen(
                rebooter.phase, rebooter.recent(LOG_LINES),
                error=rebooter.error, locked=self.locked, host=self.host)
        pattern = self.runner.pattern
        return render.running_screen(
            pattern.label if pattern else "-",
            self.runner.elapsed,
            self.runner.cycle,
            self.runner.recent(LOG_LINES),
            error=self.runner.error,
            caption=self.runner.caption,
            stopping=not self.runner.running and self.runner.error is None,
            paused=self.runner.paused,
            locked=self.locked,
            host=self.host,
        )

    def draw(self) -> None:
        if self.blanked:
            return
        # Key first, then paint: a worker thread (scan, flash, pull) that
        # lands its result while the frame is being rendered must show
        # up on the next tick. Keying after the paint recorded that
        # newer state as "drawn" and the screen stayed on the old frame
        # (FW VERSION stuck on "01 answers...", radxa-01 2026-09-17).
        key = self._display_key()
        self.display.show(self.frame())
        self._dirty = False
        self._drawn_key = key

    def _display_key(self):
        """Everything the running screen actually shows.

        Packing a frame costs ~125 ms on a Pi Zero 2 W, so redrawing on
        every input poll would eat most of the CPU to paint identical
        pixels - the timer only has one-second resolution.
        """
        if self.screen is not Screen.RUNNING:
            # The menu shows the standby progress, so it has to repaint
            # when that changes.
            if self.screen is Screen.UPDATE:
                updater = self.updater
                return ("update", updater.phase, updater.addr,
                        updater.board_state, updater.done, updater.size,
                        tuple(updater.recent(LOG_LINES)), updater.error,
                        self.locked)
            if self.screen is Screen.VERSIONS:
                versions = self.versions
                return ("versions", versions.phase, tuple(versions.rows),
                        versions.status, versions.offset, self.locked)
            if self.screen is Screen.PULL:
                puller = self.puller
                return ("pull", puller.phase, puller.before, puller.after,
                        tuple(puller.recent(LOG_LINES)), puller.error,
                        self.locked)
            if self.screen is Screen.REMOTE:
                status = self._remote_status()
                fire_at = status["fire_at"]
                show = status.get("show") or {}
                # The countdown repaints once a second, not every poll.
                left = (None if fire_at is None or status["fired_at"]
                        else int(max(0.0, fire_at - self._mono())))
                return ("remote", status["phase"], status["cue"],
                        status["label"], len(status["saved"]),
                        len(status["failed"]), len(status["live"]),
                        status["error"], status["late_ms"], left,
                        show.get("state"), show.get("synced"),
                        None if show.get("now") is None else int(show["now"]),
                        tuple(self.runner.recent(LOG_LINES)), self.locked)
            if self.screen is Screen.DEMO:
                status = self._remote_status()
                fire_at = status["fire_at"]
                show = status.get("show") or {}
                burn = show.get("burn")
                left = (None if fire_at is None or status["fired_at"]
                        else int(max(0.0, fire_at - self._mono())))
                return ("demo", self._playing_demo, status["phase"],
                        len(status["saved"]), len(status["failed"]),
                        status["error"], left, show.get("state"),
                        None if show.get("now") is None else int(show["now"]),
                        None if burn is None else (burn["state"], burn["done"]),
                        self._demo_burn_error,
                        tuple(self.runner.recent(LOG_LINES)), self.locked)
            if self.screen is Screen.REBOOT:
                rebooter = self.rebooter
                return ("reboot", rebooter.phase,
                        tuple(rebooter.recent(LOG_LINES)), rebooter.error,
                        self.locked)
            return ("menu", self._standby_status())
        return (int(self.runner.elapsed), self.runner.cycle,
                self.runner.caption,
                tuple(self.runner.recent(LOG_LINES)),
                self.runner.error, self.runner.running, self.runner.paused)

    # ---- main loop ----

    def _remote_status(self) -> dict:
        status = self.remote.status()
        status["show"] = self.show_status() if self.show_status else None
        return status

    def _demo_hint(self, status: dict) -> str:
        if self.locked:
            return "buttons locked"
        show = status.get("show") or {}
        burn = show.get("burn")
        if burn is not None and burn["state"] == "burning":
            return f"writing pictures {burn['done']}/{burn['total']} - KEY2 cancel"
        if self._demo_burn_error:
            return self._demo_burn_error
        if show.get("state") == "ended":
            return ("looping soon - KEY2 menu" if self._demo_loop
                    else "ended - KEY2 menu")
        return "KEY2 stop  hold=restart  KEY3 off"

    @staticmethod
    def _mono() -> float:
        # Fire times are in time.monotonic() - the agent's clock - which
        # is not the injectable idle clock the tests drive.
        return time.monotonic()

    def _follow_remote(self) -> None:
        """The screen follows who drives the unit: REMOTE while the show
        PC has it, the menu again once it lets go."""
        remote = self.remote
        if remote is None:
            return
        if (self._playing_demo is not None and self.player is not None
                and self.player.is_demo):
            # A demo we started ourselves fires its cues through this
            # very session, so `remote.active` is true too - must not
            # read as the PC taking the unit over (that would bounce the
            # screen straight to REMOTE). _stop_demo()/_track_demo() are
            # the only ways off the DEMO screen while this holds.
            # `player.is_demo` is also checked (not just `_playing_demo`)
            # so that the moment the PC actually does take over - a
            # /show/load turns it False - this stops overriding at once,
            # instead of waiting for _track_demo()'s own next tick.
            return
        if remote.active and self.screen is not Screen.REMOTE:
            self.screen = Screen.REMOTE
            self._standby = False
            self._dirty = True
        elif not remote.active and self.screen is Screen.REMOTE:
            self.screen = Screen.MENU
            self._dirty = True

    def _idle_tasks(self) -> None:
        self._follow_remote()
        self._track_demo()
        now = self._clock()
        if (self.demo_store is not None
                and now - self._last_demo_poll >= DEMO_POLL_S):
            self._last_demo_poll = now
            self.refresh_demos()
        idle = now - self._last_input
        if not self.blanked and 0 < self.blank_after <= idle:
            self._blank()
        if (self._locks_itself and not self.locked
                and 0 < self.relock_after <= idle):
            self.locked = True
            self._unlock_progress.clear()
            self._dirty = True
        if now - self._last_pet >= WATCHDOG_PERIOD_S:
            self._last_pet = now
            notify.alive()

    def tick(self, wait: float = FRAME_INTERVAL_S) -> None:
        """Drain pending events, then redraw if anything visible changed."""
        event = self.inputs.get(timeout=wait)
        while event is not None:
            self.handle(event)
            if self.quit:
                return
            event = self.inputs.get()
        self._idle_tasks()
        if self.blanked:
            return
        if self._dirty or self._display_key() != self._drawn_key:
            self.draw()

    def run(self, max_ticks: int | None = None) -> None:
        self.draw()
        notify.ready()
        ticks = 0
        try:
            while not self.quit and (max_ticks is None or ticks < max_ticks):
                self.tick()
                ticks += 1
        finally:
            self.runner.stop()
            self.display.wake()
            self.display.show(render.message_screen(*self.exit_message,
                                                    host=self.host))
            time.sleep(0.2)
