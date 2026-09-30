"""WIFI: which Wi-Fi network this unit is on, and switching it (2026-09-30).

At the venue the units sit on the router's SSID (yoshihirock.net_5G). An
exhibition without the router or a PC runs off radxa-05 instead: it
becomes a hotspot, SSID AZ-Epaper - on radxa-05 the NetworkManager
profile of that name IS the hotspot, on every other unit it is an
ordinary client profile with a static address. Moving a unit between
the two used to mean ssh, which from the wrong network is exactly what
one cannot do. The WIFI menu row does it from the LCD HAT.

The screen shows what the unit is on now (SSID, IPv4 address, signal,
client or hotspot) and lists the unit's wireless NetworkManager
profiles, the active one marked. UP/DOWN pick one; KEY1 *held* (the
REBOOT gesture - a plain press does nothing here, a knock must never
move a unit off its network) runs `sudo -n nmcli --wait 45 con up
<name>` on a thread of its own. Success shows the new SSID and address;
a refusal shows nmcli's first line and brings the previous profile back
up, so a wrong choice cannot strand a unit out of reach. Profiles are
never created, modified or deleted here, autoconnect is never touched,
nothing is written under /etc: the row only chooses among what the
setup left on the unit (radxa/README.md).

Everything shown comes from a cache. A daemon thread asks nmcli every
POLL_S - cheap D-Bus questions, no USB, and no scan (`dev wifi list
--rescan no`), so a unit mid-show never hops channels for this - and
/status's "wifi" is that same cache, never a read on the HTTP thread
or the LCD loop. Every nmcli call goes through one injectable runner,
REBOOT's run_command, so the tests run against a fake and never touch
the network of the machine they run on.
"""

from __future__ import annotations

import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass

from .config import LOG_HISTORY
from .rebooter import run_command
from .updater import MenuEntry

IDLE = "idle"                # the list: hold KEY1 switches to the chosen row
CONNECTING = "connecting"    # nmcli con up in flight
DONE = "done"                # switched (or already there)
FAILED = "failed"            # refused; the previous profile brought back
LOCKED = "locked"            # the PC drives the unit: nothing was done

WIRELESS = "802-11-wireless"
LOCKED_NOTE = "PC show running - WIFI locked"

LIST_COMMAND = ["nmcli", "-t", "-f", "NAME,TYPE,ACTIVE,AUTOCONNECT",
                "con", "show"]
# --rescan no: the cached scan results are enough for the signal figure,
# and a scan would take the radio off-channel while the PC drives the unit.
SCAN_COMMAND = ["nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL", "dev", "wifi",
                "list", "--rescan", "no"]
# Every device, not `dev show wlan0`: the block whose TYPE is wifi is the
# one, whatever the kernel named it.
DEVICE_COMMAND = ["nmcli", "-t", "-f",
                  "GENERAL.DEVICE,GENERAL.TYPE,GENERAL.CONNECTION,IP4.ADDRESS",
                  "dev", "show"]
PROFILE_FIELDS = "802-11-wireless.mode,802-11-wireless.ssid"

UP_WAIT_S = 45               # nmcli's own wait (its default is 90)
UP_TIMEOUT_S = 60.0          # ours, around it
READ_TIMEOUT_S = 10.0
POLL_S = 10.0
MAX_PROFILES = 8             # rows asked about per poll; the screen fits 5

# What /status serves when nothing has been read (or nmcli is missing).
WIFI_BLANK = {"ssid": None, "ip": None, "signal": None, "mode": None,
              "profile": None}

_MODES = {"infrastructure": "client", "": "client", "ap": "hotspot"}


def profile_command(name: str) -> list[str]:
    return ["nmcli", "-t", "-f", PROFILE_FIELDS, "con", "show", name]


def up_command(name: str) -> list[str]:
    return ["sudo", "-n", "nmcli", "--wait", str(UP_WAIT_S), "con", "up", name]


def split_terse(line: str) -> list[str]:
    """One `nmcli -t` line into its fields: ':' separates, '\\' escapes
    (an SSID may carry either)."""
    fields, current, escaped = [], [], False
    for ch in line:
        if escaped:
            current.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(ch)
    fields.append("".join(current))
    return fields


def _first_line(text: str) -> str:
    for line in str(text).splitlines():
        if line.strip():
            return line.strip()
    return ""


class WifiError(Exception):
    """nmcli refused or is not there; the message is its first line."""


@dataclass(frozen=True)
class Profile:
    """One wireless NetworkManager profile, as the list shows it."""
    name: str
    ssid: str
    mode: str            # "client" / "hotspot" (or nmcli's own word)
    active: bool
    autoconnect: bool


class Wifi:
    """State behind the WIFI screen and /status's "wifi".

    `run` is injectable (args, timeout) -> (code, output), like the
    Rebooter's, so the flow is testable without nmcli or sudo.
    """

    def __init__(self, run=run_command, poll_s: float = POLL_S,
                 read_timeout: float = READ_TIMEOUT_S,
                 up_timeout: float = UP_TIMEOUT_S, echo_log: bool = True):
        self._run = run
        self.poll_s = poll_s
        self.read_timeout = read_timeout
        self.up_timeout = up_timeout
        self._echo_log = echo_log

        # The cache: what the reader last saw. `state` is exactly what
        # /status serves; `profiles` is the list on the screen.
        self.state: dict = dict(WIFI_BLANK)
        self.profiles: "list[Profile]" = []
        self.read_state = "none"          # none / read / error
        self.read_error: "str | None" = None
        self.choice = 0

        self.phase = IDLE
        self.target: "Profile | None" = None
        self.previous: "str | None" = None
        self.error: "str | None" = None
        self.restored: "bool | None" = None   # FAILED: previous back up?
        self.log: deque = deque(maxlen=LOG_HISTORY)
        self._lock = threading.Lock()         # cache, log
        self._poll_lock = threading.Lock()    # one poll at a time
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._reader: "threading.Thread | None" = None
        self._thread: "threading.Thread | None" = None

    # ---- facts for the screen ----

    @property
    def menu_entry(self) -> MenuEntry:
        return MenuEntry("wifi", "WIFI", "choose this unit's Wi-Fi network")

    @property
    def busy(self) -> bool:
        """True while the switch runs. KEY2 still leaves the screen then;
        the switch keeps going."""
        return self.phase == CONNECTING

    def recent(self, count: int) -> list[str]:
        with self._lock:
            return list(self.log)[-count:]

    def emit(self, message: str, error: bool = False) -> None:
        first = _first_line(message)
        if error and not first.startswith("ERROR"):
            first = f"ERROR {first}"
        stamp = time.strftime("%H:%M:%S")
        with self._lock:
            self.log.append(f"{stamp} {first}")
        if self._echo_log:
            print(f"{stamp} {message.rstrip()}", flush=True)

    def snapshot(self) -> dict:
        """/status's "wifi": the cache, never a read."""
        with self._lock:
            return dict(self.state)

    def chosen(self) -> "Profile | None":
        with self._lock:
            if not self.profiles:
                return None
            return self.profiles[self.choice % len(self.profiles)]

    def rows(self) -> "list[tuple[str, str, bool]]":
        """(name, mode, active) per profile, for the screen."""
        with self._lock:
            return [(p.name, p.mode, p.active) for p in self.profiles]

    def current(self) -> "tuple[str, str]":
        """The two lines about now: the SSID (or why there is none) and
        'IP 192.168.51.103  72%  client', whichever parts are known."""
        state = self.snapshot()
        if state["ssid"] is None and state["profile"] is None:
            if self.read_state == "none":
                return "reading...", ""
            if self.read_state == "error":
                return "not connected", f"nmcli: {self.read_error}"
            return "not connected", ""
        parts = []
        if state["ip"]:
            parts.append(f"IP {state['ip']}")
        if state["signal"] is not None:
            parts.append(f"{state['signal']}%")
        if state["mode"]:
            parts.append(state["mode"])
        return state["ssid"] or state["profile"], "  ".join(parts)

    def status_text(self) -> str:
        """The line under the list: what the switch is doing or did."""
        target = self.target
        if self.phase == CONNECTING:
            return f"connecting to {target.ssid if target else '?'}…"
        if self.phase == DONE:
            return f"on {target.ssid}" if target else ""
        if self.phase == FAILED:
            text = f"ERROR {self.error or 'nmcli refused'}"
            if self.previous and self.restored is not None:
                text += (f" - back on {self.previous}" if self.restored
                         else f" - {self.previous} NOT restored")
            return text
        if self.phase == LOCKED:
            return self.error or LOCKED_NOTE
        if self.read_state == "error" and self.state["profile"] is not None:
            return f"nmcli: {self.read_error}"
        return ""

    def key(self) -> tuple:
        """Everything the screen shows, for the App's redraw check."""
        with self._lock:
            state = tuple(self.state[k] for k in WIFI_BLANK)
            profiles = tuple(self.profiles)
        return (self.phase, self.choice, profiles, state, self.error,
                self.restored, self.read_state, self.read_error,
                None if self.target is None else self.target.name)

    # ---- the list ----

    def select(self, step: int) -> None:
        if self.busy:
            return
        with self._lock:
            if self.profiles:
                self.choice = (self.choice + step) % len(self.profiles)
        if self.phase != IDLE:
            self.reset()                # a verdict is read; moving on

    def select_active(self) -> None:
        """The cursor on the profile in use, so a hold does nothing rash."""
        with self._lock:
            for index, profile in enumerate(self.profiles):
                if profile.active:
                    self.choice = index
                    return

    def _keep_choice(self, before: "list[Profile]") -> None:
        # Called with the lock held, after `profiles` was replaced: the
        # cursor stays on the same NAME, or clamps.
        if before and 0 <= self.choice < len(before):
            name = before[self.choice].name
            for index, profile in enumerate(self.profiles):
                if profile.name == name:
                    self.choice = index
                    return
        self.choice = min(self.choice, max(0, len(self.profiles) - 1))

    # ---- the reader ----

    def start_reader(self) -> None:
        """The daemon that fills the cache every poll_s (main wires it)."""
        if self._reader is not None:
            return
        self._reader = threading.Thread(target=self._loop, daemon=True,
                                        name="wifi-reader")
        self._reader.start()

    def refresh(self) -> None:
        """Read again soon (the screen was just opened)."""
        if self._reader is None:
            self.start_reader()         # its first poll is right away
        else:
            self._wake.set()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.poll()
            self._wake.wait(self.poll_s)
            self._wake.clear()

    def _ask(self, args: list[str]) -> str:
        try:
            code, output = self._run(args, self.read_timeout)
        except subprocess.TimeoutExpired:
            raise WifiError(f"{args[0]} timed out")
        if code != 0:
            raise WifiError(_first_line(output) or f"{args[0]} exit {code}")
        return output

    def poll(self) -> None:
        """One refresh of the cache, on the caller's thread (the reader's,
        normally; a test's). Never raises: what nmcli would not say is
        null, and the last error is kept for the screen."""
        with self._poll_lock:
            error = None
            profiles: "list[Profile]" = []
            try:
                profiles = self._read_profiles()
            except Exception as exc:        # noqa: BLE001 - shown, not raised
                error = _first_line(str(exc)) or exc.__class__.__name__
            state = dict(WIFI_BLANK)
            try:
                state = self._read_state(profiles)
            except Exception as exc:        # noqa: BLE001
                error = error or _first_line(str(exc)) or exc.__class__.__name__
            with self._lock:
                before = self.profiles
                self.profiles = profiles
                self.state = state
                self.read_error = error
                self.read_state = "error" if error else "read"
                self._keep_choice(before)

    def _read_profiles(self) -> "list[Profile]":
        found = []
        for line in self._ask(LIST_COMMAND).splitlines():
            fields = split_terse(line)
            if len(fields) < 4 or fields[1] != WIRELESS:
                continue
            found.append((fields[0], fields[2] == "yes", fields[3] == "yes"))
        profiles = []
        for name, active, autoconnect in found[:MAX_PROFILES]:
            ssid, mode = name, "client"
            try:
                for line in self._ask(profile_command(name)).splitlines():
                    fields = split_terse(line)
                    if len(fields) < 2:
                        continue
                    key, value = fields[0], ":".join(fields[1:])
                    if key == "802-11-wireless.mode":
                        mode = _MODES.get(value, value)
                    elif key == "802-11-wireless.ssid" and value:
                        ssid = value
            except WifiError:
                pass                        # the name is still a row
            profiles.append(Profile(name, ssid, mode, active, autoconnect))
        return profiles

    def _read_state(self, profiles: "list[Profile]") -> dict:
        state = dict(WIFI_BLANK)
        # The wifi device: its connection and IPv4 address. Blocks start
        # at each GENERAL.DEVICE, whether or not nmcli blank-lines them.
        blocks: "list[dict]" = []
        for line in self._ask(DEVICE_COMMAND).splitlines():
            fields = split_terse(line)
            if len(fields) < 2:
                continue
            key, value = fields[0], ":".join(fields[1:])
            if key == "GENERAL.DEVICE" or not blocks:
                blocks.append({})
            blocks[-1][key] = value
        for block in blocks:
            if block.get("GENERAL.TYPE") != "wifi":
                continue
            profile = block.get("GENERAL.CONNECTION") or ""
            state["profile"] = None if profile in ("", "--") else profile
            for key, value in block.items():
                if key.startswith("IP4.ADDRESS") and value:
                    state["ip"] = value.split("/")[0]
                    break
            break
        if state["profile"] is not None:
            for profile in profiles:
                if profile.name == state["profile"]:
                    state["mode"] = profile.mode
                    state["ssid"] = profile.ssid
                    break
        # What the radio says it is on, and how well - the hotspot has
        # no such row (and some builds refuse the question in AP mode),
        # which is why the SSID above came from the profile first.
        try:
            rows = self._ask(SCAN_COMMAND).splitlines()
        except WifiError:
            rows = []
        for line in rows:
            fields = split_terse(line)
            if len(fields) >= 3 and fields[0] == "yes":
                if fields[1]:
                    state["ssid"] = fields[1]
                try:
                    state["signal"] = int(fields[2])
                except ValueError:
                    pass
                break
        return state

    # ---- the switch ----

    def reset(self) -> None:
        """Back to the plain list (a verdict was read, or KEY2 left)."""
        if self.busy:
            return
        self.phase = IDLE
        self.error = None
        self.target = None
        self.restored = None

    def refuse(self, note: str = LOCKED_NOTE) -> None:
        """The App's answer while the PC drives the unit: said, not done."""
        if self.busy:
            return
        self.phase = LOCKED
        self.error = note
        self.target = None
        self.restored = None

    def switch(self) -> None:
        """Bring the chosen profile up on a thread of its own."""
        if self.busy:
            return
        target = self.chosen()
        if target is None:
            return
        self.target = target
        self.error = None
        self.restored = None
        if target.active:
            # `con up` on the active profile would drop and re-raise it
            # for nothing - say so instead.
            self.emit(f"wifi: already on {target.name}")
            self.phase = DONE
            return
        self.previous = self.snapshot()["profile"]
        self.phase = CONNECTING
        self._thread = threading.Thread(target=self._switch,
                                        args=(target, self.previous),
                                        daemon=True, name="wifi-switch")
        self._thread.start()

    def join(self, timeout: "float | None" = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _up(self, name: str) -> "tuple[int, str]":
        try:
            return self._run(up_command(name), self.up_timeout)
        except subprocess.TimeoutExpired:
            return 124, f"nmcli timed out after {self.up_timeout:.0f} s"
        except Exception as exc:            # noqa: BLE001 - a verdict, not a crash
            return 1, str(exc) or exc.__class__.__name__

    def _switch(self, target: Profile, previous: "str | None") -> None:
        self.emit(f"wifi: con up {target.name}")
        code, output = self._up(target.name)
        if code == 0:
            self.poll()                     # the new SSID/IP before DONE shows
            self.emit(f"wifi: on {target.ssid}")
            self.phase = DONE
            return
        self.error = _first_line(output) or f"nmcli exit {code}"
        self.emit(self.error, error=True)
        self.poll()
        if previous and previous != target.name:
            # A wrong choice must not strand the unit: the profile it
            # was on comes back. NetworkManager's own autoconnect may
            # have done that already - then `con up` again would only
            # drop and re-raise a link that is fine.
            if self.snapshot()["profile"] == previous:
                self.restored = True
                self.emit(f"wifi: still on {previous}")
            else:
                self.emit(f"wifi: back to {previous}")
                code, output = self._up(previous)
                self.restored = code == 0
                if self.restored:
                    self.emit(f"wifi: back on {previous}")
                else:
                    self.emit(f"{previous}: {_first_line(output) or code}",
                              error=True)
                self.poll()
        self.phase = FAILED
