"""UI state machine: menu -> running, driven by HAT events.

Controls (Waveshare 1.3inch LCD HAT):
  joystick up/down/left/right - choose a demo pattern
  KEY1                        - start; then pause; then resume
  KEY1 held ~1 s              - reset: start again from zero
  KEY2                        - back to the menu (stops a running demo)
  KEY3                        - blank the screen now

UPDATE FW (the first of the unit's own rows, after every demo) is a
mode rather than a demo: it stops
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

BOARD INFO (ui/boardinfo.py, the last row) names the board on the USB cable - the only
one a unit can name: its USB serial (the STM32 unique ID), its TYPE (the
serial's last four characters; most of the fleet reads 324C), what FW
VERSION's own scan says it runs, and this unit's flash record for it.
The serial, type and record are read without a byte on the wire; the FW
line is FW VERSION's scan (the runner is stopped for it, the same way),
and is not read at all while the PC drives the unit. KEY1 reads again,
KEY2 goes back to the menu.

GIT PULL updates the checkout itself: `git pull --ff-only` in the repo
the service runs from (ui/puller.py), and when the commit moved KEY1
exits the process - the service is Restart=always, so systemd brings
the UI back on the new code. Ten identical units tell apart by the
hostname in the top strip of every screen.

REBOOT restarts the whole unit (ui/rebooter.py). Its row only opens a
confirm screen, and the reboot needs KEY1 *held* - a plain press does
nothing there - so a double-tap on the menu cannot take a unit down.

WIFI (ui/wifi.py, the row before BOARD INFO) chooses which Wi-Fi
network the unit is on: the router's SSID at the venue, radxa-05's
AZ-Epaper hotspot at an exhibition without router or PC. The screen
shows the SSID, address, signal and mode now and lists the unit's
wireless NetworkManager profiles, the active one marked; UP/DOWN pick
one and KEY1 *held* - the REBOOT gesture, a plain press does nothing -
brings it up (`sudo -n nmcli con up`) on a thread of its own; a
refusal brings the previous profile back. KEY2 goes back to the menu,
also while a switch is in flight (it keeps running; a FAILED verdict
waits on the screen until it is read). Refused with "PC show running -
WIFI locked" whenever a demo row's KEY1 is refused (_pc_show_wins())
and with "PC show loaded - WIFI locked" for a PC show uploaded and
waiting for START (_wifi_locked()). The choice lasts until the next
reboot - autoconnect is never touched. The runner, the port and the
boards are never touched either; the row order is UPDATE FW, FW
VERSION, GIT PULL, REBOOT, WIFI, EXHIBITION, BOARD INFO. The show PC
(or the Conductor on radxa-05, before it leaves the network to become
the hotspot) can also ask for a switch over the agent - POST
/wifi/select, with a delay, under the same _wifi_locked() rule - and
while that deferral is pending the WIFI screen counts it down
("switching to AZ-Epaper in 25 s"); UP/DOWN and a held KEY1 are then
refused with "switch pending", and KEY2 goes back without cancelling
it (only /wifi/select {"cancel": true} does).

EXHIBITION (ui/exhibition.py, the row after WIFI) runs the show from
the unit that IS the Conductor - radxa-05 at an exhibition without a
PC. The row reads `EXHIBITION  (no conductor)` on a unit where nothing
answers on 127.0.0.1:8765 (a reader thread asks every ~30 s, ~5 s while
the screen is open - the HAT loop never waits on HTTP), and its screen
then only says so. With a Conductor: the timeline's name and length,
the run (`idle`, `countdown -0:11`, `0:00 / 10:54 running`, `hold`,
`ended`, `next run in 0:25` while LOOP waits), `units 7/7 online`,
`LOOP on/off`, `speaker ok / no speaker`. KEY1 *held* is START when
idle and STOP while a run or its countdown exists (the REBOOT gesture;
a plain press does nothing), KEY3 *held* toggles LOOP, LEFT/RIGHT are
the speaker's volume -5/+5 (plain presses; one request in flight,
presses meanwhile add up), UP/DOWN only read a verdict away (the
Conductor has one timeline; there is nothing to choose), and KEY2 goes
back to the menu - the show keeps running,
the Conductor owns it. Every command goes on a worker thread; the
screen reads `sending…` and then the Conductor's answer or its
refusal, verbatim. A held KEY3 anywhere else blanks the screen, as a
plain KEY3 does. On radxa-05 the local Conductor drives radxa-05's own
player too (fleet.json -> 127.0.0.1:8787), so a START from this screen
arms this unit's own session: wherever a Conductor answers locally,
_follow_remote() never leaves the EXHIBITION screen, shows EXHIBITION
instead of REMOTE (also moving over from a REMOTE reached before the
first probe answered), and KEY2 on REMOTE or EXHIBITION does not
release() the session (that would be player.stop - radxa-05 dropping
out of its own show; the Conductor's presence decides, not its up to
30 s old run); the operator is left on the menu until the session is
really let go of or the Conductor service stops, and on that menu the
rows that take the port (STANDBY, a pattern, FW VERSION, UPDATE FW)
are refused with a note (_remote_holds), as demo rows are.

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
written into this unit, sorted in right after STANDBY. KEY1 is refused
only while the PC is really in control of the unit - its show running or
held, its show restored onto the garment and waiting, or its pictures
being written right now (_pc_show_wins(), a 5 s note on the menu) - and
a PC show that is merely LOADED, STOPPED or ENDED is superseded, the
operator getting it back by Uploading again from the PC. KEY1 loads it
(ui/showplay.py's ShowPlayer, with demo=True and the row's slug and
loop flag) and the DEMO screen opens right away, on the burn
ui/remote.py's RemoteSession.burn() just started - it shows
"writing n/N  KEY2 cancel" (status.show.burn) while that runs, and KEY2
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

A demo survives a restart of the unit. ShowPlayer.restore() brings one
that was playing back playing (ui/showplay.py), before this App is even
built, so _adopt_restored_demo() picks it up at start-up: the DEMO
screen, the name, the loop, the cursor on its row. A showroom left
looping comes back by itself after a power cut, and KEY2 (or the PC's
STOP) is still what ends it.

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
from .exhibition import VOLUME_STEP
from .patterns import PATTERNS
from .remote import RemoteError
from .runner import DemoRunner
from .showplay import ENDED, HOLDING, LOADED, RUNNING, STOPPED

# The WIFI row's refusals (ui/wifi.py shows whichever it is given).
WIFI_LOCKED = "PC show running - WIFI locked"
WIFI_LOCKED_LOADED = "PC show loaded - WIFI locked"

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
    BOARDINFO = "boardinfo"
    PULL = "pull"
    REBOOT = "reboot"
    WIFI = "wifi"
    EXHIBITION = "exhibition"
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
                 remote=None, player=None, demos=None, boardinfo=None,
                 wifi=None, exhibition=None):
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
        # WIFI (ui/wifi.py) sits between REBOOT and BOARD INFO: a switch
        # of network holds no port and no board, so it is not in
        # remote.busy below either.
        self.wifi = wifi
        if wifi is not None:
            self.patterns.append(wifi.menu_entry)
        # EXHIBITION (ui/exhibition.py) follows WIFI: a client of the
        # Conductor on this unit, nothing on the port or the boards.
        self.exhibition = exhibition
        if exhibition is not None:
            self.patterns.append(exhibition.menu_entry)
        # BOARD INFO is the last row, so GIT PULL and REBOOT keep theirs.
        # It reads FW through `versions` (the same worker, so its scan
        # already counts in remote.busy below).
        self.boardinfo = boardinfo
        if boardinfo is not None:
            self.patterns.append(boardinfo.menu_entry)
        # The show PC's session (ui/remote.py). It may not take the port
        # while something that must not be interrupted holds the unit.
        self.remote = remote
        self.show_status = None        # set by main: the show player's status
        # KEY2 left REMOTE/EXHIBITION on a unit whose own Conductor has
        # this session armed: the follow leaves the menu alone until the
        # session is really released (_follow_remote).
        self._remote_dismissed = False
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
        self._adopt_restored_demo()    # ...and one already playing again

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
        if event == "key3_hold":
            # Only EXHIBITION gives the hold a meaning (LOOP on/off).
            # Anywhere else a KEY3 held too long still blanks the
            # screen - the short press was suppressed by the hold.
            if self.screen is Screen.EXHIBITION:
                self._handle_exhibition(event)
            else:
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
        if self.screen is Screen.BOARDINFO:
            self._handle_boardinfo(event)
            return
        if self.screen is Screen.REBOOT:
            self._handle_reboot(event)
            return
        if self.screen is Screen.WIFI:
            self._handle_wifi(event)
            return
        if self.screen is Screen.EXHIBITION:
            self._handle_exhibition(event)
            return
        if self.screen is Screen.REMOTE:
            if event == "key2":
                if self._local_conductor():
                    # The session is the local Conductor's and this unit
                    # may be in its run (the cache can be up to 30 s
                    # old, so the Conductor's presence decides, not its
                    # last reported run): KEY2 only leaves the screen -
                    # release() would be player.stop, radxa-05 dropping
                    # out of its own show. The follow does not bring it
                    # back until the session is really let go of.
                    self._remote_dismissed = True
                else:
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

    def _handle_wifi(self, event: str) -> None:
        wifi = self.wifi
        if wifi.busy:
            # The switch keeps running on its thread; only KEY2 (back to
            # the menu) is heard while it does.
            if event == "key2":
                self.screen = Screen.MENU
                self._dirty = True
            return
        if wifi.pending is not None:
            # A switch the Conductor (or the PC) asked for over
            # /wifi/select is counting down: the list is not for
            # choosing now, and KEY2 leaves it pending - only another
            # /wifi/select {"cancel": true} takes it back.
            if event in ("up", "down", "left", "right", "key1_hold"):
                wifi.refuse(wifi.PENDING_NOTE)
            elif event == "key2":
                wifi.reset()
                self.screen = Screen.MENU
            self._dirty = True
            return
        if event in ("up", "left"):
            wifi.select(-1)
        elif event in ("down", "right"):
            wifi.select(+1)
        elif event == "key1_hold":
            # Only the hold switches - a plain KEY1 does nothing here, a
            # knock must never move a unit off its network. Refused in
            # the states a demo row's KEY1 is refused, and one more
            # (_wifi_locked): the PC would lose this unit.
            note = self._wifi_locked()
            if note is not None:
                wifi.refuse(note)
            else:
                wifi.switch()
        elif event == "key2":
            wifi.reset()
            self.screen = Screen.MENU
        self._dirty = True

    def _handle_exhibition(self, event: str) -> None:
        exhibition = self.exhibition
        if not exhibition.available:
            # No Conductor here: the screen is a note, KEY2 the way out.
            if event == "key2":
                self._leave_exhibition()
            return
        if exhibition.busy:
            # The command completes on its thread; only KEY2 is heard.
            if event == "key2":
                self._leave_exhibition()
            return
        if event in ("up", "down"):
            # The Conductor has one timeline - nothing to choose between.
            # Moving reads a verdict away, as on the WIFI list.
            exhibition.reset()
        elif event in ("left", "right"):
            # The speaker's volume, 5 % a press - plain presses, as many
            # as wanted; presses during a request add up into the next.
            exhibition.adjust_volume(-VOLUME_STEP if event == "left"
                                     else VOLUME_STEP)
        elif event == "key1_hold":
            # The hold is START while idle and STOP while a run or its
            # countdown exists - the REBOOT gesture: a plain press does
            # nothing on a screen that can start a show on ten units.
            if exhibition.active:
                exhibition.stop()
            else:
                exhibition.start()
        elif event == "key3_hold":
            exhibition.toggle_loop()
        elif event == "key2":
            self._leave_exhibition()
        self._dirty = True

    def _leave_exhibition(self) -> None:
        """KEY2: back to the menu. The run is the Conductor's and goes
        on - nothing is released, and where the Conductor is local and
        has this unit's session armed, the follow is told not to bring
        the screen straight back (until the session is let go of)."""
        self.exhibition.close()
        if (self.remote is not None and self.remote.active
                and self._local_conductor()):
            self._remote_dismissed = True
        self.screen = Screen.MENU
        self._dirty = True

    def _wifi_locked(self) -> "str | None":
        """Why the WIFI switch is refused right now, or None
        (wifi_lock_note, for this App's player)."""
        return self.wifi_lock_note(self.player)

    @staticmethod
    def wifi_lock_note(player) -> "str | None":
        """Why a Wi-Fi switch must be refused for `player`'s unit, or None.
        One rule for the WIFI row's held KEY1 and the agent's POST
        /wifi/select (ui/agent.py asks this, now and again when a
        deferred switch fires).

        Everything that refuses a demo row's KEY1 (_pc_show_wins: the
        PC's show running or holding, restored onto the garment, its
        pictures being written) - and one case more: a PC show that is
        LOADED with its pictures burned, i.e. uploaded and waiting for
        START. A demo may supersede that (the PC uploads again), but a
        network switch does not supersede anything: it takes the unit
        out of the PC's reach with the show's pictures on it. Only a
        show load()ed since this boot counts (ShowPlayer.loaded_here):
        restore() brings every PC show a unit ever played back LOADED
        with its burn read from disk as "burned", and that must not
        lock every unit for good after a power cycle (review round 2,
        2026-09-30); a restored show the PC is still driving is
        _pc_show_wins()'s restored_running case above."""
        if player is None:
            return None
        note = App._pc_show_wins(player)
        if note is not None:
            return WIFI_LOCKED
        if (player.show is not None and not player.is_demo
                and player.state == LOADED and player.loaded_here):
            burn = (player.status() or {}).get("burn") or {}
            if burn.get("state") == "burned":
                return WIFI_LOCKED_LOADED
        return None

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

    def _handle_boardinfo(self, event: str) -> None:
        if self.boardinfo.busy:
            return                  # the FW read holds the port; let it finish
        if event in ("key1", "press"):
            self._read_boardinfo()
        elif event == "key2":
            # Like FW VERSION: straight back, no repaint, runner stopped.
            self.screen = Screen.MENU
        self._dirty = True

    def _pc_driving(self) -> bool:
        """Is the show PC in charge of this unit right now - its session
        active or on the port, or its show on the garment? Then BOARD INFO
        leaves the port alone and does not read FW."""
        remote = self.remote
        if remote is not None:
            if remote.active:
                return True
            owned = getattr(remote, "owned", None)
            if callable(owned) and owned():
                return True
        player = self.player
        if player is not None:
            if player.is_demo and self._playing_demo is not None:
                return True
            if self._pc_show_wins(player) is not None:
                return True
        return False

    def _read_boardinfo(self) -> None:
        read_fw = not self._pc_driving()
        if read_fw and self.versions is not None:
            # FW VERSION's scan needs the port to itself - stopped the same
            # way _enter_versions() stops it.
            self.runner.stop()
            self._standby = False
        self.boardinfo.read(read_fw)

    def _enter_boardinfo(self) -> None:
        self._read_boardinfo()
        self.screen = Screen.BOARDINFO
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

    def _enter_wifi(self) -> None:
        # The runner keeps the port too: a network switch touches nothing
        # on the serial side, and a running demo carries on through it.
        # The cache is asked to refresh (the reader's thread does the
        # reading) and the cursor starts on the profile in use. A FAILED
        # verdict from a switch the operator left mid-way is kept.
        self.wifi.reopen()
        self.wifi.refresh()
        self.wifi.select_active()
        self.screen = Screen.WIFI
        self._dirty = True

    def _enter_exhibition(self) -> None:
        # Nothing on the port either: the Conductor drives the units over
        # HTTP, this unit's own included. open() polls right away (on the
        # reader's thread) and reads the show's name once.
        self.exhibition.open()
        self.screen = Screen.EXHIBITION
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

    def _start_demo_show(self, slug: str, name: "str | None" = None,
                         loop: "bool | None" = None) -> bool:
        """Load the stored show fresh (KEY1, and KEY1-hold's restart - the
        operator may have just re-written this very slug); only starts
        the burn - _track_demo() calls run() itself once it settles.
        False if the load could not even start (a corrupt or
        since-deleted file - /demo/save validates, so this is mainly the
        "deleted from the PC while it played" case); the caller falls
        back to the menu rather than opening a screen with nothing to
        show.

        `name` (the row's label) is what /status.show.demo_name carries
        for the PC's Units tile, and `loop` is the row's own flag; a
        restart that passes neither again (KEY1-hold) keeps whatever
        _enter_demo() set the first time. Both go on disk with the slug,
        so that a restart of the UNIT can hand this same demo, looping as
        it was, back to this same screen (_adopt_restored_demo())."""
        try:
            show = self.demo_store.load(slug)
            self.player.load(show, demo=True, slug=slug,
                             name=self._demo_name if name is None else name,
                             loop=self._demo_loop if loop is None else loop)
            self._demo_show_id = show["id"]
            self._demo_awaiting_run = True
            self._demo_burn_error = None
            return True
        except RemoteError:
            return False

    def _adopt_restored_demo(self) -> None:
        """The unit restarted while it was playing one of its own demos.

        ShowPlayer.restore() runs before this App exists (ui/main.py) and
        brings a demo that was RUNNING back running, as a demo - so the
        screen has to pick it up too, or the LCD would sit on the menu
        while the garment plays, KEY2 would not stop it, and a `loop`
        demo would stop at its last cue. (restore() only ever comes back
        LOADED or RUNNING, never HOLDING - but the player's own thread
        runs between restore() and this App being built, so a lap whose
        remainder had already expired can read ENDED by now. That one is
        adopted too, and _track_demo() starts its next lap.)

        The slug names the menu row; a record written by the release
        before the slug existed has none, so the row whose stored show is
        this very show is looked up instead (DemoStore.list() carries
        `show_id`). Failing even that - the row was deleted from the PC
        while the unit was off - the demo is still adopted: it keeps
        playing and looping and KEY2 still ends it, only KEY1-hold has
        nothing to reload and drops back to the menu.

        `loop` is whatever was recorded with the demo, or the row's own
        flag when the record predates it (or the operator has since
        turned looping on): either one is a reason to play it again.

        A demo restored merely LOADED is left on the menu on purpose:
        nothing is on the garment, and KEY1 on its row starts it.
        """
        player = self.player
        if (player is None or not player.is_demo or player.show is None
                or not player.restored_running
                or player.state not in (RUNNING, ENDED)):
            return
        slug, show_id = player.demo_slug, player.show["id"]
        if not slug and self.demo_store is not None:
            try:
                slug = next((entry["slug"] for entry in self.demo_store.list()
                             if entry.get("show_id") == show_id), "")
            except OSError:
                slug = ""
        row = next((r for r in self._demo_rows if r.slug == slug), None)
        self._playing_demo = slug
        self._demo_name = player.demo_name or (row.label if row else "")
        self._demo_loop = player.demo_loop or bool(row and row.loop)
        self._demo_show_id = show_id
        self._demo_ended_at = None
        self._demo_awaiting_run = False
        self._demo_burn_error = None
        self._standby = False
        # The cursor sits on the row that is playing, so KEY2 lands back
        # on the menu at the demo the operator was looking at.
        if row is not None:
            self.select(row.key)
        self.screen = Screen.DEMO
        self._dirty = True

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
                absent = player.session.runner.absent_snapshot()
                failed = {b for b, s in burn.get("failed", ())
                         if b not in absent}
                self._demo_burn_error = f"{len(failed)} boards failed - KEY2 menu"
            elif (burn is not None and burn["state"] == "cancelled"
                    and burn.get("reason")):
                # The burn never finished (no board answered, the port
                # was taken): the reason alone fits the hint strip,
                # where the gate's whole sentence would be ellipsized.
                self._demo_burn_error = f"{burn['reason']} - KEY2 menu"
            else:
                # Some other refusal (e.g. the unit went busy under us) -
                # say what it actually was rather than guess "boards".
                self._demo_burn_error = f"{exc} - KEY2 menu"
        self._dirty = True

    def _enter_demo(self, row: "DemoRow") -> None:
        if self.player is None or self.demo_store is None:
            return
        player = self.player
        note = self._pc_show_wins(player)
        if note is not None:
            self._note_on_menu(note)
            return
        if not self._start_demo_show(row.slug, row.label, row.loop):
            return
        self._standby = False
        self._playing_demo = row.slug
        self._demo_name = row.label
        self._demo_loop = row.loop
        self._demo_ended_at = None
        self.screen = Screen.DEMO
        self._dirty = True

    @staticmethod
    def _pc_show_wins(player) -> "str | None":
        """Why a demo row's KEY1 must be refused right now, in the
        operator's words - or None, when the PC is not in charge of this
        unit and a demo may take it.

        The PC wins in three cases, and only these three:

          - its show is RUNNING or HOLDING: it is on the garment and
            being driven, and a demo would paint over a show in progress
          - restore() put its show back on the garment and it is waiting
            (held, or mid-show with a T0 the PC has yet to confirm) -
            still the PC's picture, though the state reads LOADED.
            `restored_running` is never cleared, so it counts only while
            it is still THIS show and that show has not since STOPPED or
            ENDED (a restored run that played itself out is finished, the
            same as any other ENDED show); load() and stop() drop the
            pairing outright
          - the pictures are being written right now: the conductor's
            own Upload is in flight, and KEY1 would cancel that burn

        A PC show that is merely LOADED, STOPPED or ENDED is none of
        those: a demo supersedes it, and the operator gets it back by
        Uploading again from the PC. Refusing those too is what left a
        unit that had restarted mid-demo - its demo restored as a plain
        LOADED PC show - answering KEY1 on every demo row with nothing
        but a note (radxa-05, 2026-09-26).
        """
        if player.show is None or player.is_demo:
            return None
        if player.state in (RUNNING, HOLDING):
            return "PC show running - stop it on the PC"
        if (player.restored_running
                and player.show.get("id") == player.restored_id
                and player.state not in (STOPPED, ENDED)):
            return "PC show running - stop it on the PC"
        burn = (player.status() or {}).get("burn") or {}
        if burn.get("state") == "burning":
            return "PC is writing pictures - wait"
        return None

    def _note_on_menu(self, text: str, seconds: float = 5.0) -> None:
        # 5 s, not 3: the operator presses KEY1 and looks at the wall,
        # not at the 1.3" screen - three seconds of subtitle went by
        # unread and the LCD read as "did nothing" (radxa-05, 2026-09-26).
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

    def _remote_holds(self) -> "str | None":
        """Why a row that takes the serial port (STANDBY, a pattern, FW
        VERSION, UPDATE FW) must be refused right now, in the operator's
        words - or None. Somebody else drives this unit through its
        session: the show PC, or on radxa-05 the local Conductor, whose
        session stays armed after a START (the menu is reachable then,
        _remote_dismissed) - a standby would white the garment, a
        pattern would paint over the show, a stopped runner would fail
        the next arm() (review of 13c8dcc, HIGH-2). Demo rows have their
        own gate (_pc_show_wins)."""
        remote = self.remote
        if remote is None or not (remote.active or self._pc_driving()):
            return None
        note = (self._pc_show_wins(self.player) if self.player is not None
                else None)
        if note is not None:
            return note
        if self._local_conductor():
            return "conductor holds this unit - see EXHIBITION"
        return "PC holds this unit - release it on the PC"

    def _restart(self) -> None:
        key = self.patterns[self.selected].key
        if key in ("update", "versions", "standby") or (
                key not in ("pull", "boardinfo", "reboot", "wifi", "exhibition")
                and not isinstance(self.patterns[self.selected], DemoRow)):
            note = self._remote_holds()
            if note is not None:
                self._note_on_menu(note)
                return
        if key == "update":
            self._enter_update()
            return
        if self.patterns[self.selected].key == "pull":
            self._enter_pull()
            return
        if self.patterns[self.selected].key == "versions":
            self._enter_versions()
            return
        if self.patterns[self.selected].key == "boardinfo":
            self._enter_boardinfo()
            return
        if self.patterns[self.selected].key == "reboot":
            self._enter_reboot()
            return
        if self.patterns[self.selected].key == "wifi":
            self._enter_wifi()
            return
        if self.patterns[self.selected].key == "exhibition":
            self._enter_exhibition()
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
                image_choice=updater.image_choice,
                usb_board=getattr(updater, "usb_board", None),
                bus_shared=bool(getattr(updater, "bus_shared", False)))
        if self.screen is Screen.VERSIONS:
            versions = self.versions
            return render.versions_screen(
                versions.rows, versions.status, versions.phase,
                versions.bundled, offset=versions.offset, locked=self.locked,
                host=self.host)
        if self.screen is Screen.BOARDINFO:
            info = self.boardinfo
            return render.boardinfo_screen(
                info.lines(), info.busy, usb_line=info.usb_line(),
                locked=self.locked, host=self.host)
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
        if self.screen is Screen.WIFI:
            wifi = self.wifi
            ssid, info = wifi.current()
            return render.wifi_screen(
                ssid, info, wifi.rows(), wifi.choice, wifi.phase,
                status=wifi.status_text(), locked=self.locked, host=self.host)
        if self.screen is Screen.EXHIBITION:
            ex = self.exhibition
            return render.exhibition_screen(
                ex.available, ex.show_lines(), ex.run_text(), ex.fleet_text(),
                ex.loop_text(), ex.speaker_text(), ex.phase,
                status=ex.status_text(), active=ex.active,
                locked=self.locked, host=self.host,
                volume_keys=ex.available and ex.speaker_available()
                and ex.volume_supported())
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
                        self.locked,
                        tuple(sorted((getattr(updater, "usb_board", None)
                                      or {}).items())),
                        bool(getattr(updater, "bus_shared", False)))
            if self.screen is Screen.VERSIONS:
                versions = self.versions
                return ("versions", versions.phase, tuple(versions.rows),
                        versions.status, versions.offset, self.locked)
            if self.screen is Screen.BOARDINFO:
                return ("boardinfo", self.boardinfo.key(),
                        self.boardinfo.busy, self.locked)
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
            if self.screen is Screen.WIFI:
                return ("wifi", self.wifi.key(), self.locked)
            if self.screen is Screen.EXHIBITION:
                return ("exhibition", self.exhibition.key(), self.locked)
            # The EXHIBITION row's label follows its cache ("(no
            # conductor)" or not), so the menu repaints when that flips.
            return ("menu", self._standby_status(),
                    None if self.exhibition is None
                    else self.exhibition.available)
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
            # Kept to the width of the other hints (ui/render.py's _hint
            # ellipsizes at 240 px / DejaVu 12 px, about 32 characters,
            # and "writing pictures 480/480 - KEY2 cancel" did not fit).
            return f"writing {burn['done']}/{burn['total']}  KEY2 cancel"
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
        local = self._local_conductor()
        if self.screen is Screen.EXHIBITION and local:
            # Never left by the follow. On radxa-05 the local Conductor
            # drives this very unit (fleet.json: radxa-05 -> 127.0.0.1),
            # so the START just pressed here arms this session and the
            # screen would otherwise flip to REMOTE the next tick - where
            # a held KEY1 cannot STOP and KEY2 would release() the unit
            # out of its own run (review of 951e0b7, HIGH-1). KEY2 is
            # the only way out. (A garment unit sitting on the "(no
            # conductor)" note is followed as any other screen.)
            return
        if not remote.active or not local:
            # Released - or the Conductor service here is gone: REMOTE
            # and its release() come back (review of 13c8dcc, LOW-1).
            self._remote_dismissed = False
        if not remote.active:
            if self.screen is Screen.REMOTE:
                self.screen = Screen.MENU
                self._dirty = True
            return
        if local:
            # The Conductor is on this unit: EXHIBITION is where its run
            # is read and stopped; REMOTE would only offer a KEY2 that
            # drops this unit out of it. REMOTE is still reached when
            # the session was armed before the first probe answered (a
            # UI restart, a timed-out probe): moved over as soon as the
            # Conductor is known (review of 13c8dcc, MED-1).
            if self.screen is Screen.REMOTE or (
                    self.screen is not Screen.EXHIBITION
                    and not self._remote_dismissed):
                self._enter_exhibition()
                self._standby = False
            return
        if self.screen is not Screen.REMOTE:
            self.screen = Screen.REMOTE
            self._standby = False
            self._dirty = True

    def _local_conductor(self) -> bool:
        """A Conductor answers on this unit (the EXHIBITION cache)."""
        return self.exhibition is not None and bool(self.exhibition.available)

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
