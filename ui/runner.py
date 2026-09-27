"""Background demo runner: drives the panels and reports progress.

Built for show operation, where the failure that matters is a frozen
panel waiting for a human. Nothing here is fatal - see docs/RELIABILITY.md
for the reasoning. The layers, outermost last:

  command  exponential backoff, long enough to outwait a repaint (9.8 s
           measured) and a busy board
  board    a board that does not answer is skipped, not fatal: the wall
           is built for 20 boards but must run with whatever subset is
           powered. Absent boards are re-probed periodically and join
           the show when they appear
  cycle    give up on at most one cycle, re-initialise, try the next one
  bus      close and reopen the port - via find_port, since a USB
           re-enumeration can rename the device - and wait for it to come
           back if it is gone
  thread   the worker only ever exits when asked to stop

The command sequence itself mirrors host/wave_demo.py, which is the one
verified against the real boards.

Standby (all-white idle) uses the same machinery, but instead of looping
it paints once and then watches the link, repainting whenever the boards
come back from a power cycle running their factory demo.
"""

from __future__ import annotations

import random
import struct
import sys
import threading
import time
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

from epaper.commands import (TEST_SLOT, clear_pipeline, delete_slot,
                             get_version, save_color, save_pipeline,
                             show_single, slot_config, stop)
from epaper.protocol import (ACK_INVALID_CMD, ACK_SUCCESS, ADDR_BUS_MASTER,
                             DEV_NUMBER_BRAND)

NO_DELAY = 0xFFFF          # in a show file's table: no delay for this socket
FRAME_S = 0.01             # conductor/sequence.py's FRAME_S: one table frame
# "No sweep anywhere" as a table: what a cue that carries no delay table
# for a (board, slot) means (review finding F5, 2026-09-25) - the slot's
# pipeline is cleared (0x25) once and remembered in _delays_sent, so a
# re-upload that took every sweep off the timeline does not leave the
# previous upload's tables in the boards.
NO_TABLE = struct.pack(">64H", *([NO_DELAY] * 64))
from epaper.transport import RESYNC_PAD_BYTES, Bus, find_port

from .config import LOG_HISTORY
from .patterns import DEFAULT_PALETTE, Pattern
from .remote import READY          # ui/remote.py imports nothing of ours

# A garment carries up to 60 boards, addressed 1..n by rank. Without a
# list from the show PC the runner explores: it probes upwards from 1 and
# stops once EXPLORE_GAP addresses in a row past the last board that
# answered stay silent - so a 21-board garment costs ~27 probes, not 60,
# and a dead board in the middle (16 of 21) does not end the search.
# Until the first board answers it keeps going to 60: the dead ones may
# be the low addresses (2026-09-22: 1-11 and 19-20 off, 12-18 and 21 on).
# A list that arrives later (a show's burn, a /prepare) ends the
# exploring for as long as it is in force, and nothing outside it is
# touched again - see _apply_job_boards().
MAX_BOARD_ID = 60
DEFAULT_BOARDS = list(range(1, MAX_BOARD_ID + 1))
EXPLORE_GAP = 6
ACK_SUCCESS, ACK_BUSY = 0x80, 0x82
# The show broadcast goes out exactly once. It is unacknowledged, and it
# is tempting to repeat it as insurance - but a board does NOT discard
# commands that arrive during its repaint: they queue in its receive
# buffer and run afterwards, so every extra copy is another full 16 s
# repaint. Measured on the production boards 2026-08-14: three copies
# made board 1 repaint twice (34 s) and board 20 finish at 35 s, while a
# single copy had both boards repainting in step (deaf 0.7-17.1 s and
# 1.2-17.3 s) - the whole "lag between boards" was this. In the pattern
# loop a genuinely lost frame costs one cycle and the next one repairs
# it; a show's cue has no next cycle, which is what the LANDING CHECK
# below is for - it sends a second copy only once it has evidence that
# the first one was NOT taken, so the repeat costs a repaint exactly
# when a repaint is what is missing.
SHOW_REPEATS = 1
SHOW_GAP_S = 0.15
# ---- the landing check (radxa-04, LOOK26 rehearsal, 2026-09-26) ----
# A show cue's broadcast is one unacknowledged frame. On a unit with a
# noisy USB link (kernel "usb1-port1: disabled by hub (EMI?)", ttyACM0 ->
# ttyACM1 mid-show) the last cue was recorded as fired and the boards
# never changed: nothing on the wire says a broadcast was dropped.
#
# So after the broadcast the runner asks ONE board a read-only question
# (CMD_GET_VERSION, 0x02 - it stores nothing and plays nothing) and reads
# the SILENCE, not the answer: a board repainting its e-paper answers
# nothing until it is done (deaf 0.7-17.1 s, measured 2026-08-14), so
# silence or ACK_BUSY means the frame landed. A board that answers is
# idle, which means it never got the show - and only then is the
# broadcast sent again, once. Never twice: two copies would be the
# 2026-08-14 double repaint, and an unconfirmed cue is reported instead.
#
# VERIFY_AFTER_S is measured from the moment the WITNESS board starts
# repainting (its own sweep delay), so it lands inside that deaf window.
# 1.5 s, not 1.0: the same 2026-08-14 run has the SLOWER of the two
# boards still answering at +1.2 s, so a second is not clear of the
# onset on every board - and asking before a board has gone deaf is the
# one reading that costs the wall a needless repaint. Anything up to the
# repaint's own length (16 s) is equally safe on the other side.
# Re-measure per board type before lowering it: docs/DEVELOPMENT.md
# section 6 says how.
VERIFY_AFTER_S = 1.5
VERIFY_READ_S = 0.3       # the witness answers in ms or not at all
# ...and the question is asked twice before silence is believed. The
# link this exists for eats frames in BOTH directions: a get_version
# that never reached the board reads exactly like a board that is deaf
# and busy repainting, which would report the lost cue as landed - the
# one outcome that must not be green. Two silences, 0.6 s in all.
VERIFY_TRIES = 2
# A sweep start further off than this is not waited for. The table is
# 16-bit frames, so a corrupt entry (0xFFFE) reads as 655 s, and the
# check would sit on the port for eleven minutes with no reprobe and no
# guard STOP behind it. A cue that says how long its sweep is caps it at
# that - a socket starting after the LAST scale is a broken table by
# definition - and anything else at conductor/sequence.py's MAX_DELAY_S.
VERIFY_MAX_DELAY_S = 30.0
# How late the NEXT cue may be made by repairing the last one. A board
# does not discard what arrives while it repaints - it queues it and
# runs it afterwards (the 2026-08-14 measurement) - so a repair that is
# still drawing when the next trigger goes out does not double-paint the
# same slot: it delays the next picture by what is left of it. Two
# seconds of that is worth a garment showing the right look; beyond it
# the repair would eat the cue after it, and the honest answer is to
# leave the loss on the tile in red instead (landed "idle-not-repaired").
REPAIR_LATE_S = 2.0
# WHICH board may be asked. Only one board of a garment is on the USB
# cable - the RS-485 master, address 1 (ADDR_BUS_MASTER; the production
# wall is ID:1 on USB, docs/SPECIFICATION.md 5.7, and every garment is
# addressed 1..n by rank, so 1 is always the one the unit talks to
# directly). Everything else is reached through that board's relay, and
# a relayed query is not known to be safe: 0x29 over the relay is never
# answered AND wedges the master's USB CDC until a power cycle (5.7,
# reproduced on radxa-01 2026-09-17). 0x02 has never been tried that
# way. A wedged master mid-show is worse than the lost frame this check
# exists to catch, so the default asks the USB board and nobody else.
#
# "any" is the earliest-starting live board (the strongest witness,
# since it is the most surely repainting) and is for AFTER a bench test
# shows a relayed 0x02 is harmless - the procedure is in
# docs/DEVELOPMENT.md section 6.
WITNESS_USB = "usb"
WITNESS_ANY = "any"
USB_BOARD = ADDR_BUS_MASTER
LINK_POLL_S = 2.0         # how often standby checks the panel link
LINK_GUARD_S = 60.0       # how often standby re-suppresses the autoplay
# ...and the same backstop while the show PC drives the unit. REMOTE had
# only one 0x17: the guard after each fire. Between two cues that is
# nothing at all - 38 s of silence between q02 (6:37) and q03 (7:45) in
# the 2026-09-27 rehearsals - and the tops garment's master board (16
# boards, reproduced on radxa-04 and radxa-05, never on the skirt)
# restarted its factory autoplay in that gap (SPECIFICATION 5.4; the
# same master was seen running the autoplay on its own with the USB out
# the day before). A board repainting is deaf and stops servicing USB,
# so the next cue's write BLOCKED for 40-400 ms and the cue never
# showed. So standby's 60 s heartbeat now runs in REMOTE too - but only
# while the worker is idle and only when it cannot land inside a
# repaint: see _remote_guard_clear().
# 15 s, not standby's 60: the silences that lost cues were 11-38 s long
# once the show was about two minutes old, so a minute between stops
# cannot be relied on to cover them. The frame costs 8 bytes and the
# rules below mean it only ever goes out when nothing else would touch
# the bus anyway.
REMOTE_GUARD_S = 15.0     # how often remote re-suppresses the autoplay
# How near a cue's trigger may be when the periodic stop goes out. A
# broadcast 0x17 is one 8-byte frame, but the board takes it, and a
# trigger landing in that window is exactly the collision this whole
# change is about - so the heartbeat stands aside for the cue, never
# the other way round, and picks up on the next pass.
REMOTE_GUARD_HOLD_S = 5.0
# A broadcast write that takes longer than this is worth saying out
# loud. On a healthy port `bus.send()` is a memcpy into the CDC's
# buffer - well under a millisecond. The lost cues of 2026-09-27 were
# all writes that BLOCKED for 40-400 ms because the master board had
# stopped servicing USB mid-repaint, and until now the only trace of it
# was the fire's own lateness. Timed here, the log and /status say
# plainly "the board was busy", which is what tells an autoplay problem
# apart from a slow Radxa or a late command from the PC.
STALL_LOG_MS = 50.0
# ---- recovering a bus that accepts frames and executes none ----
# LOOK28 (radxa-07, AZ271SD1307, 22 boards), 2026-09-28, 4 of 4 runs:
# 10-30 s after the LAST cue's repaint the bus went bad. Once the kernel
# said `usb usb1-port1: disabled by hub (EMI?), re-enabling`, the master
# re-enumerated ttyACM0 -> ttyACM1, the runner got EIO, reopened, and all
# was well. THREE times it stayed enumerated and every later write blocked
# 359 ms - the heartbeat said `bus stalled 359 ms on stop` for ever - while
# frames were ACCEPTED AND NOT EXECUTED: the operator's "Show preset"
# wrote slot 1 (+125 ms) and not one panel refreshed. Restarting the
# unit's UI service cleared it every time.
#
# What that restart does, in order: a port reopen (the DTR toggle), the
# padding bytes, a broadcast STOP, the probe sweep, the slot configs, a
# white paint. Two hypotheses, and the recovery tries both cheapest first
# because the padding-alone test could not be run from the show PC:
#   A  the transient desynced the master's USB frame parser, so every
#      later frame is read misaligned and dropped after a ~360 ms
#      inter-byte timeout - which is also why the CDC only drains every
#      ~360 ms. Cure: padding (transport.RESYNC_PAD_BYTES) and enough
#      quiet behind it for that timeout to expire (PAD_SETTLE_S).
#   B  the master needs the reopen itself, or the setup sweep. Cure: the
#      reopen, and the one board that is actually on the USB cable asked
#      again (_recover_sweep()).
# A write this fast is a healthy port: on the real unit a clean broadcast
# is +1..2 ms, so 100 ms is far above the noise and far below the 359 ms
# this exists for.
RECOVERED_MS = 100.0
# ...and how slow a heartbeat STOP has to be to count towards a recovery.
# 200 ms, not STALL_LOG_MS: a board busy repainting blocks 40-400 ms and
# that is NORMAL (the 2026-09-27 rehearsals) - it is the never-ending
# 225-390 ms of the degraded state this must recognise, and two in a row
# is what tells them apart, since a repaint ends and this does not.
STALL_RECOVER_MS = 200.0
# How much clear air the automatic recovery wants. It reopens the port and
# may hold it for seconds, so it never runs with a cue anywhere near: a
# cue nearer than this and it stands aside altogether, for the next
# heartbeat to find. The whole reason this is automatic at all is the
# stretch AFTER the last cue, which is where 4 of 4 LOOK28 runs went bad.
RECOVER_QUIET_S = 60.0
RECOVER_BACKOFF_S = 60.0   # between attempts
# ...and then it stops and says so. A bus that is still 359 ms after
# three of these is not something another attempt will fix, and a worker
# quietly reopening the port for ever while the operator tries to work
# is worse than a tile that says "restart this unit".
RECOVER_MAX_ATTEMPTS = 3
# The quiet after the padding, before the STOP that measures the result.
# The padding only helps if the firmware's own inter-byte timeout gets to
# expire behind it - measured at ~360 ms on radxa-07 - so a STOP sent
# straight after would arrive at a parser still mid-frame and prove
# nothing. 0.45 s is that timeout with a quarter over it, and no more than
# that: the pre-cue check (PRECHECK_S) has two seconds for the whole of
# itself, and this is its largest single wait.
PAD_SETTLE_S = 0.45
# The opt-in fire-time re-send (--resend-on-stall, off by default).
# When a CUE's own broadcast stalls there is no waiting for the idle
# recovery: the picture is missing NOW, and the whole of the cheap half
# of the recovery (padding, then the same frame again) costs about as
# long as the stall did. The reopen path is never taken here - 5-10 s
# inside a cue is a worse fault than the one being fixed.
#
# Off by default, and this is the reason: if the stalled frame WAS
# executed after all, the re-send restarts the repaint and the garment
# paints the same slot twice (the 2026-08-14 double repaint). On
# radxa-07, 2026-09-28, stalled frames were never executed - the preset
# wrote slot 1 and no panel moved - but that is one degradation on one
# unit, not a proof about every one, so the operator turns this on
# knowing the trade. docs/SPECIFICATION.md 4.12 has both halves.
RESEND_STALL_MS = STALL_RECOVER_MS
# ---- the fast paths: 1-1.5 s, and the next cue on time ----
# The idle recovery above is for the stretch after the last cue, where
# seconds cost nothing. A cue that is about to fire cannot wait for it, so
# the same two cures are also run on a clock that fits inside the gap
# before a trigger: no probe sweep, nothing acknowledged that need not be.
#
# When: PRECHECK_S before every armed cue, one timed broadcast STOP. A
# healthy port answers in 1-2 ms and the whole check is that one frame.
#
# 1.5 s, and the two numbers below say why it cannot be more. The check may
# not begin until the previous cue's repaint is certainly over
# (PRECHECK_AFTER_FIRE_S, 9.5 s) and this show's cues are at least 11 s
# apart, so the whole window it can live in is those 1.5 s. Asking for two
# would mean skipping the check before every cue of an 11 s show, which is
# every cue there is.
PRECHECK_S = 1.5
# ...and the repaint rule itself. A board repainting is deaf and stops
# servicing USB, so a check inside that window measures the repaint and not
# the port - it would read every healthy unit as degraded - and a 0x17 into
# it is the one frame that must not go. 9.5 s covers the first-generation
# panel's 9.8 s refresh as measured, less the 0.3 s the frame itself costs.
# A cue closer than this to the last one simply skips the check and fires.
PRECHECK_AFTER_FIRE_S = 9.5
# What each step of the check is allowed to cost, so it can be decided
# BEFORE the step whether there is time for it. Measured against the fake
# bus of tests/test_ui_remote.py with every write blocking 0.36 s, plus the
# 0.3 s of settle a real serial open costs:
#   the padding   PAD_SETTLE_S + the STOP that checks it   ~0.85 s
#   the reopen    the open, the master's config, the STOP  ~0.70 s
# A step there is no time for is not begun: the cue's own instant outranks
# it, and the idle recovery picks the unit up after the cue anyway.
PAD_BUDGET_S = 0.85
FAST_REOPEN_BUDGET_S = 0.70
# The read window the fast reopen gives the master's own config frame. A
# board that is going to answer does so in milliseconds; this is not the
# place for the retry ladder, because the whole path has to fit in the
# time before a trigger.
FAST_REOPEN_READ_S = 0.15
# ---- noticing a USB re-enumeration at once ----
# The kernel's `usb usb1-port1: disabled by hub (EMI?), re-enabling` takes
# the device node away and brings it back under a new name in about 0.45 s.
# Until now the unit found out at its NEXT WRITE - up to 20 s later, in the
# 2026-09-28 runs - and then spent reopen_delay (2 s) plus a full setup
# sweep getting back, about 5 s in all. Watched at PORT_POLL_S the loss is
# seen inside a fifth of a second, and the way back is find_port() every
# PORT_RETRY_S and then the fast reopen: about a second, with no cue missed.
PORT_POLL_S = 0.2
PORT_RETRY_S = 0.1
# How long the node is waited for before this is a real unplug rather than a
# re-enumeration, and the ordinary reopen ladder takes over.
PORT_BACK_WAIT_S = 3.0
PROBE_SWEEPS = 3          # setup passes over the board list
PROBE_SWEEP_DELAY_S = 3.0 # between passes, so a repaint can finish meanwhile
REPROBE_INTERVAL_S = 60.0 # how often absent boards get another chance
FIRE_SPIN_S = 0.02        # the last stretch before a timed show is polled
ARM_GRACE_S = 0.25        # how long a fresh worker waits for a cue's fire time
# A probe of a board that is not there costs a serial timeout - about
# 1.5 s, and up to 2.5 s across the setup's sweeps (radxa-01,
# 2026-09-25). A cue's trigger falling inside one is a late cue on
# stage (a unit that restarted mid-show fired its next cue 4 s late,
# behind the start-up probe of six absent boards), so no probe is begun
# while a trigger is due within this, and the probing sweep waits the
# trigger out and sends it first instead.
PROBE_HOLD_S = 3.0
# How long a new start waits for a worker that did not end within stop()'s
# timeout. One request into a wedged CDC can block for ~7.5 s (three
# tries of 2 s write timeout + 0.5 s read), and a save retries that.
OLD_WORKER_PATIENCE_S = 12.0
# The guard STOP (a broadcast 0x17 after a fire, so a finished slot does
# not run on into the factory autoplay - SPECIFICATION 5.4) used to be a
# flat guard_delay seconds after every fire. That 12 s is one refresh
# (conductor/timeline.py's REFRESH_S = 7 s, the first-generation panel)
# plus 5 s of slack. A swept cue is not finished at refresh: its last
# scale only STARTS at the sweep's span, so it completes at refresh +
# span - and a span may be up to sequence.MAX_DELAY_S = 30 s, which put
# the STOP squarely inside the sweep and killed the change half-drawn
# (adversarial review, 2026-09-26; latent, because the spans tried on
# hardware so far are 3 s and the wall is a 7 s refresh). The margin
# over the picture's completion is kept at whatever guard_delay has over
# this refresh, so a test that compresses guard_delay compresses it too.
GUARD_REFRESH_S = 7.0
# ...and a ceiling on what a cue can talk the guard into. The honest
# worst case is sequence.SPAN_HARD_MAX_S (120 s) over a 60 s refresh
# (timeline.REFRESH_RANGE_S), so nothing real reaches this; it is here
# so a show file or a /prepare body with a wild number cannot switch the
# guard off altogether and hand the wall back to the factory autoplay.
GUARD_MAX_S = 200.0


def device_token(port: str):
    """Identity of the panel's USB device node, or None if it is gone.

    Unplugging the cable removes the node, and board 0x01 comes back from
    the replug running its factory demo - the board cannot tell us, so
    this is how it is noticed. Presence alone is not enough: a quick
    replug can come and go between polls. A re-enumeration creates a new
    devtmpfs node, so the inode and its creation time change even when
    the name does not.

    Names that are not POSIX device paths (COM3) have no node to look at;
    they report a constant, and a failed command has to speak instead.
    """
    if not port.startswith("/dev/"):
        return "opaque"
    try:
        info = Path(port).stat()
    except OSError:
        return None
    return (info.st_ino, info.st_ctime_ns)


class DemoRunner:
    """Runs one pattern in a worker thread until stopped.

    `open_bus` is injectable so tests can drive a fake transport; the
    retry timings are parameters so they can be compressed in tests.
    """

    def __init__(self, boards: list[int] | None = None,
                 interval: float = 60.0, guard_delay: float = 30.0,
                 slot: int = TEST_SLOT, port: str | None = None,
                 palette: list[int] | None = None,
                 open_bus=None, seed: int | None = None,
                 echo_log: bool = True,
                 command_attempts: int = 8, save_attempts: int = 3,
                 retry_delays: tuple[float, ...] = (1, 2, 4, 8, 12),
                 busy_delay: float = 1.0, busy_attempts: int = 5,
                 reopen_after_failures: int = 3, reopen_delay: float = 2.0,
                 port_wait: float = 10.0,
                 show_repeats: int = SHOW_REPEATS,
                 show_gap: float = SHOW_GAP_S,
                 verify_fire: bool = False,
                 verify_after: float = VERIFY_AFTER_S,
                 verify_read: float = VERIFY_READ_S,
                 verify_witness: str = WITNESS_USB,
                 resend_on_stall: bool = False,
                 precheck: float = PRECHECK_S,
                 precheck_after_fire: float = PRECHECK_AFTER_FIRE_S,
                 port_poll: float = PORT_POLL_S,
                 port_back_wait: float = PORT_BACK_WAIT_S,
                 recover_quiet: float = RECOVER_QUIET_S,
                 recover_backoff: float = RECOVER_BACKOFF_S,
                 recover_attempts: int = RECOVER_MAX_ATTEMPTS,
                 pad_settle: float = PAD_SETTLE_S,
                 link_poll: float = LINK_POLL_S,
                 link_guard: float = LINK_GUARD_S,
                 remote_guard: float = REMOTE_GUARD_S,
                 remote_guard_hold: float = REMOTE_GUARD_HOLD_S,
                 link_token=device_token,
                 probe_sweeps: int = PROBE_SWEEPS,
                 probe_sweep_delay: float = PROBE_SWEEP_DELAY_S,
                 reprobe_interval: float = REPROBE_INTERVAL_S):
        self.explore = not boards            # no list given: find them
        self.boards = list(boards) if boards else list(DEFAULT_BOARDS)
        # What this unit does on its own, restored by _start() when the
        # show PC hands the port back (KEY2 / release): a job's board
        # list only rules while the show does - see _apply_job_boards().
        self._own_explore = self.explore
        self._own_boards = list(self.boards)
        # Where the list in force came from while it is not this unit's
        # own: "show" (a show file's garment list, ui/remote.py's
        # set_boards()) or "job" (the boards of one /prepare or one
        # burn). None means the unit's own list rules. `boards_source`
        # reports it, and the PC's tile marks a unit whose list is not
        # the show's.
        self._boards_given: "str | None" = None
        # The group count the worker on the port is carrying, None when
        # nobody is on it - see _take_groups().
        self._groups: "int | None" = None
        self.interval = interval
        self.guard_delay = guard_delay
        self.slot = slot           # the LOCAL pattern loop's working slot
        self._pattern_slot = slot  # ...restored by _start() after standby
        self.port = port
        self.palette = palette or list(DEFAULT_PALETTE)
        self._open_bus = open_bus or (lambda p: Bus(p, verbose=False))
        self._seed = seed
        # Mirror the on-screen log to stdout so the same progress shows up
        # in `journalctl -u epaper-ui` (the LCD is the only other view).
        self._echo_log = echo_log

        self.command_attempts = command_attempts
        # Each save retry writes the boards' flash, so it retries less.
        self.save_attempts = save_attempts
        self.retry_delays = retry_delays
        self.busy_delay = busy_delay
        self.busy_attempts = busy_attempts
        self.reopen_after_failures = reopen_after_failures
        self.reopen_delay = reopen_delay
        self.port_wait = port_wait
        self.show_repeats = show_repeats
        self.show_gap = show_gap
        # The landing check after a cue's broadcast (see VERIFY_AFTER_S).
        # Off means the old behaviour exactly: one frame, no question
        # asked - which is also what every non-cue path still does.
        self.verify_fire = verify_fire
        self.verify_after = verify_after
        self.verify_read = verify_read
        # Anything but an explicit "any" is the safe policy: a witness
        # behind the RS-485 relay is a risk nobody has measured yet
        # (see WITNESS_USB).
        self.verify_witness = (WITNESS_ANY if verify_witness == WITNESS_ANY
                               else WITNESS_USB)
        # The opt-in fire-time re-send (RESEND_STALL_MS). Off means
        # exactly today's behaviour: the stall is timed, logged and
        # counted, and the frame is not sent again.
        self.resend_on_stall = bool(resend_on_stall)
        # The pre-cue health check (PRECHECK_S). 0 switches it off.
        self.precheck_s = precheck
        self.precheck_after_fire = precheck_after_fire
        # Watching for a USB re-enumeration (PORT_POLL_S). 0 switches it off.
        self.port_poll = port_poll
        self.port_back_wait = port_back_wait
        self.recover_quiet = recover_quiet
        self.recover_backoff = recover_backoff
        self.recover_attempts_max = recover_attempts
        self.pad_settle = pad_settle
        self.link_poll = link_poll
        self.link_guard = link_guard
        self.remote_guard = remote_guard
        # How near a cue may be when the heartbeat goes out. A parameter
        # only so a test can compress it along with everything else.
        self.remote_guard_hold = remote_guard_hold
        self._link_token = link_token
        self.probe_sweeps = probe_sweeps
        self.probe_sweep_delay = probe_sweep_delay
        self.reprobe_interval = reprobe_interval

        # Which of self.boards are actually answering. `live` keeps the
        # bus order of self.boards; `absent` boards cost one quick probe
        # per reprobe interval and nothing else.
        self.live: list[int] = []
        self.absent: set[int] = set()
        # Per (board, slot): slot 0x1B config already sent, and the
        # delay table (docs/FW_REQUEST_SEGMENT_DELAY.md) already there -
        # so an unchanged one is not written again. A board's reboot
        # wipes neither in flash (docs/MERIS_REPLY_3SLOT.pdf: 0x13/0x1F/
        # 0x1B persist across power cycles), but a board that dropped
        # out and reappeared is re-verified with a write anyway rather
        # than trusted blind - see _forget_board().
        self._cfg_done: set[tuple[int, int]] = set()
        self._delays_sent: dict[tuple[int, int], bytes] = {}
        # Per (board, slot): the (array, table) last burned there, so a
        # re-Upload of an unchanged show writes nothing (ui/showplay.py's
        # ShowPlayer.load() -> RemoteSession.burn() -> _run_burn()).
        self._burn_cache: dict[tuple[int, int], tuple] = {}
        self._next_reprobe = 0.0
        self.no_sweep: set[int] = set()

        self.log: deque[str] = deque(maxlen=LOG_HISTORY)
        self.pattern: Pattern | None = None
        # The session feeding cues while the show PC drives this unit
        # (ui/remote.py); None whenever the unit runs its own patterns.
        self.remote = None
        self._remote_dev_type = DEV_NUMBER_BRAND
        self.caption: str | None = None   # what the current cycle shows
        self._once = False
        self.standby_ready = False
        # A trigger sent from inside the probing (see PROBE_HOLD_S) owes
        # the same guard stop as any other; _run_remote() picks this up.
        self._guard_owed: "float | None" = None
        self._firing = False       # inside _fire_at(): never re-enter it
        # When the LAST show broadcast of the current cue went out - the
        # first one, or the landing check's re-send. The guard STOP is
        # measured from this, not from the fire time.
        self._last_show_at: "float | None" = None
        # The REMOTE heartbeat (REMOTE_GUARD_S): when the next periodic
        # broadcast 0x17 is owed, whether the one log line has been
        # written, and how many have gone out (reported in /status as
        # remote_guard_sent).
        self._remote_guard_due: "float | None" = None
        self._remote_guard_said = False
        self.remote_guard_sent = 0
        # The instant the last picture is certainly finished, worked out
        # WHEN IT WAS SENT from that cue's own span and refresh. No
        # heartbeat before it. Captured rather than recomputed: the
        # session carries the NEXT cue's numbers moments after a fire
        # (ui/showplay.py arms it as soon as it sees FIRED), and a
        # shorter one would otherwise shrink the previous cue's sweep
        # out from under the rule.
        self._guard_floor: "float | None" = None
        self._guard_hold_s = 0.0   # the wait that floor was made from
        # The last broadcast write that blocked (STALL_LOG_MS), with how
        # many have blocked in this worker: {"ms", "frame", "at",
        # "count"}, reported in /status as bus_stall. None until one
        # does.
        self.bus_stall: "dict | None" = None
        # The last recovery of a bus that was accepting frames and
        # executing none (see RECOVERED_MS): {"at" (wall clock), "by"
        # ("padding" | "reopen" | None), "before_ms", "after_ms",
        # "count"}, reported in /status as bus_recovery. None until one
        # has been run in this worker. `by` None with recovered true is
        # "there was nothing wrong" - what POST /bus/recover answers on
        # a healthy unit.
        self.bus_recovery: "dict | None" = None
        # The last fire-time re-send (resend_on_stall): {"cue", "at",
        # "before_ms", "after_ms", "count"}, /status's `resend`.
        self.resend: "dict | None" = None
        # The last pre-cue health check (PRECHECK_S): {"cue", "at",
        # "before_ms", "by", "after_ms"}, /status's `precheck`.
        self.precheck: "dict | None" = None
        self._prechecked: "str | None" = None   # the cue already checked
        self._next_port_poll = 0.0             # PORT_POLL_S rate limit
        # How many heartbeat STOPs in a row have blocked >= the
        # threshold; two is the trigger (STALL_RECOVER_MS).
        self._stall_streak = 0
        self._recover_tries = 0            # per worker (RECOVER_MAX_ATTEMPTS)
        self._recover_next = 0.0           # monotonic: the backoff
        self._recover_said_enough = False  # the "leaving it to you" line
        self._recovering = False           # never re-entered
        # A reopen invalidates what the boards were told: the caches go,
        # and the full sweep is owed to _run_remote()'s own setup, where
        # it yields to every cue (_fire_before_probing()) instead of
        # running on the recovery's clock.
        self._setup_owed = False
        self._probing = False              # inside _setup(): no recovery
        self.cycle = 0
        self.failures = 0          # cycles abandoned since the demo started
        self.started_at: float | None = None
        self.error: str | None = None
        self._thread: threading.Thread | None = None
        # A worker that outlived stop()'s join. While it lives the stop
        # flag stays set and nothing new is started: two workers on one
        # bus means two broadcast shows for one cue.
        self._lingering: threading.Thread | None = None
        self._control = threading.RLock()   # start / start_remote / stop
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._elapsed_base = 0.0
        self._lock = threading.Lock()

    # ---- state ----

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def paused(self) -> bool:
        return self._pause.is_set()

    @property
    def elapsed(self) -> float:
        """Time spent demoing, with paused stretches not counted."""
        if self.started_at is None:
            return self._elapsed_base
        return self._elapsed_base + time.monotonic() - self.started_at

    def recent(self, count: int) -> list[str]:
        with self._lock:
            return list(self.log)[-count:]

    def emit(self, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        line = f"{stamp} {message}"
        with self._lock:
            self.log.append(line)
        if self._echo_log:
            print(line, flush=True)

    # ---- control ----

    def standby(self) -> None:
        """Silence the factory autoplay and leave every panel white.

        Runs as soon as the link is up, so the installation always starts
        from a known blank state instead of whatever vendor demo frame
        happened to be on the glass. One-shot: it paints once and the
        worker finishes, leaving the panels holding white - always into
        slot 0, since that is what the master autoplays at power-up
        (docs/MERIS_REPLY_3SLOT.pdf): whatever comes back from an
        unplug/replug shows the same white, not a stale look.
        """
        from .patterns import STANDBY

        self.start(STANDBY, once=True, slot=0)

    def _old_worker_gone(self) -> bool:
        """True once no earlier worker can touch the bus any more."""
        old = self._lingering
        if old is not None and old.is_alive():
            old.join(timeout=OLD_WORKER_PATIENCE_S)
        if old is not None and old.is_alive():
            self.error = "bus busy: the previous worker has not finished"
            self.emit("ERROR previous worker still on the bus, not starting")
            return False
        self._lingering = None
        return True

    def start_remote(self, session) -> bool:
        """Hand the port to the show PC's cues (ui/remote.py).

        Same worker thread, different loop: instead of drawing a pattern
        every interval it saves what the session hands it and sends the
        show at the instant the session names. False if the bus is still
        held by a worker that would not stop.
        """
        with self._control:
            return self._start_remote(session)

    def _start_remote(self, session) -> bool:
        if self.running:
            self.stop()
        if not self._old_worker_gone():
            return False
        self.pattern = None
        self.remote = session
        self.cycle = 0
        self.caption = None
        self.failures = 0
        self.error = None
        self._cfg_done = set()
        self._next_reprobe = 0.0
        self._stop.clear()
        self._pause.clear()
        self._elapsed_base = 0.0
        self.standby_ready = False
        self.started_at = time.monotonic()
        self.emit("start REMOTE")
        self._thread = threading.Thread(target=self._run_remote,
                                        args=(session,), daemon=True)
        self._thread.start()
        return True

    def start(self, pattern: Pattern, once: bool = False,
              slot: "int | None" = None) -> bool:
        with self._control:
            return self._start(pattern, once, slot)

    def _start(self, pattern: Pattern, once: bool = False,
               slot: "int | None" = None) -> bool:
        if self.running:
            self.stop()
        self.pattern = pattern          # what the screen names, either way
        if not self._old_worker_gone():
            return False
        self.remote = None
        self._once = once
        self.pattern = pattern
        # standby() asks for slot 0 (docs/MERIS_REPLY_3SLOT.pdf: the
        # master's power-up autoplay is slot 0, so that is what "white
        # and quiet" has to mean); every other pattern uses the runner's
        # own configured slot, same as always.
        self.slot = self._pattern_slot if slot is None else slot
        self.cycle = 0
        self.caption = None
        self.failures = 0
        self.error = None
        self.take_own_boards()
        # live/absent survive across starts on purpose: the wall does not
        # change because a different pattern was picked, and re-sweeping
        # eighteen empty sockets would hold the first frame for half a
        # minute every time KEY1 is pressed.
        self._cfg_done = set()
        self._next_reprobe = 0.0
        self._stop.clear()
        self._pause.clear()
        self._elapsed_base = 0.0
        self.standby_ready = False
        self.started_at = time.monotonic()
        self.emit(f"start {pattern.label}")
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout: float = 5.0) -> None:
        with self._control:
            self._stop_locked(timeout)

    def _stop_locked(self, timeout: float) -> None:
        self._stop.set()
        self._pause.clear()          # let a paused worker notice the stop
        if self.remote is not None:
            self.remote.wake()       # ...and one waiting for a cue
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=timeout)
            if thread.is_alive():
                # Still inside a bus request. It will see the stop flag
                # when that returns - as long as nobody clears the flag,
                # which is what _old_worker_gone() guards.
                self._lingering = thread
        self.remote = None
        self.started_at = None
        self._elapsed_base = 0.0
        # Nobody is sending frames any more, so /status goes back to
        # reporting what the list in force would give (_take_groups()).
        self._groups = None

    def pause(self) -> None:
        """Hold between cycles, keeping the timer and the pattern position.

        The panels simply keep whatever they are showing - nothing is
        playing on them, so there is nothing to stop.
        """
        if not self.running or self.paused:
            return
        self._elapsed_base = self.elapsed
        self.started_at = None       # freeze the timer
        self._pause.set()
        self.emit("paused")

    def resume(self) -> None:
        if not self.paused:
            return
        self.started_at = time.monotonic()
        self._pause.clear()
        self.emit("resumed")

    # ---- retry helpers ----

    def _hold_while_paused(self) -> bool:
        """Block until resumed; False if a stop was requested meanwhile."""
        while self._pause.is_set():
            if self._stop.wait(0.05):
                return False
        return True

    def _sleep(self, seconds: float) -> bool:
        """Interruptible sleep; False if a stop was requested.

        A pause is honoured before and after the wait, so the demo halts
        between cycles rather than mid-command.
        """
        if not self._hold_while_paused():
            return False
        if self._stop.wait(seconds):
            return False
        return self._hold_while_paused()

    def _backoff(self, attempt: int) -> float:
        return self.retry_delays[min(attempt, len(self.retry_delays)) - 1]

    def _request(self, bus, frame, label: str, attempts: int | None = None,
                 quiet: bool = False, bus_retries: int | None = None) -> bool:
        """Send until acknowledged, backing off between tries.

        The backoff has to be able to outwait a full repaint: a board is
        deaf for ~16 s while its e-paper redraws (production boards;
        9.8 s on the first generation), and that is a normal event, not
        a fault.

        `quiet` is for probes of boards that may simply not be there:
        no per-attempt log lines and no self.error, because a socket
        that is empty on purpose is bookkeeping, not a fault.
        """
        attempts = attempts or self.command_attempts
        busy_seen = 0
        for attempt in range(1, attempts + 1):
            if bus_retries is None:
                ack = bus.request(frame)
            else:
                ack = bus.request(frame, retries=bus_retries)
            if ack is not None and ack.src != frame.dest:
                # Board 20's relayed ACKs can arrive many seconds late,
                # landing in the receive window of whatever was asked
                # next - seen on hardware vouching for an empty socket.
                # An answer from the wrong board is no answer.
                ack = None
            if ack is not None and ack.cmd == ACK_SUCCESS:
                if attempt > 1 and not quiet:
                    self.emit(f"{label} ok after {attempt} tries")
                return True
            if ack is not None and ack.cmd == ACK_BUSY:
                busy_seen += 1
                if busy_seen > self.busy_attempts:
                    break
                if not self._sleep(self.busy_delay):
                    return False
                continue
            if attempt == 1 and not quiet:
                reason = "no ACK" if ack is None else f"NAK 0x{ack.cmd:02X}"
                self.emit(f"{label} {reason}, retrying")
            if attempt < attempts and not self._sleep(self._backoff(attempt)):
                return False
        if not quiet:
            self.error = f"{label}: gave up after {attempts}"
            self.emit(f"ERROR {label} gave up")
        return False

    # ---- board bookkeeping ----

    @staticmethod
    def _fmt_boards(boards) -> str:
        """Compress a board list for the LCD log: [2..19] -> "2-19"."""
        boards = sorted(boards)
        runs, start, prev = [], boards[0], boards[0]
        for b in boards[1:] + [None]:
            if b is not None and b == prev + 1:
                prev = b
                continue
            runs.append(str(start) if start == prev else f"{start}-{prev}")
            start = prev = b
        return ",".join(runs)

    def _active_dev_type(self) -> int:
        """Wire format of the pattern drawing this cycle (see Pattern)."""
        if self.pattern is None:
            return self._remote_dev_type        # a cue from the show PC
        active, _ = self.pattern.resolve(self.cycle)
        return active.dev_type

    def _trigger_due_soon(self) -> bool:
        """True when a show cue's trigger is due within the cost of one
        probe (PROBE_HOLD_S) - the probing must not start another one."""
        session = self.remote
        if session is None:
            return False
        due = session.due()
        return due is not None and due[1] - time.monotonic() <= PROBE_HOLD_S

    def _fire_before_probing(self, bus, groups: int) -> None:
        """Called between probes: a trigger due within one probe's cost
        is waited out precisely and sent first (_fire_at() spins the
        last 20 ms), and the probing goes on straight after. Nothing has
        to be probed or configured for a broadcast "show slot N" - the
        picture is already in the slot."""
        session = self.remote
        if session is None or self._firing or not self._trigger_due_soon():
            return
        due = session.due()             # re-read: it may have just fired
        if due is not None and self._fire_at(bus, groups, session, *due):
            self._guard_owed = self._guard_after_fire(session)

    def _wait_probing(self, bus, groups: int, seconds: float) -> bool:
        """A wait inside the probing that still lets a cue through: the
        same 50 ms tick _fire_at() uses, with _fire_before_probing() on
        every one of them. A flat sleep here put a cue landing in the
        gap between two sweeps 1.5 s late on the real unit (R2, review
        round 3). False if the worker was told to stop meanwhile."""
        end = time.monotonic() + seconds
        while True:
            self._fire_before_probing(bus, groups)
            # A sweep of a wall with absent boards runs for tens of
            # seconds; the heartbeat is owed in that time too.
            self._remote_guard_tick(bus, groups)
            left = end - time.monotonic()
            if left <= 0:
                return True
            if not self._sleep(min(left, 0.05)):
                return False

    def _probe(self, bus, board: int, groups: int) -> bool:
        """One quick chance for a board to answer: silence it, set the slot.

        A single short request, because during discovery most probes hit
        sockets that are empty on purpose - the full backoff ladder is
        reserved for boards that are known to be there.
        """
        if not self._request(bus, stop(board, groups), f"probe @{board:02d}",
                             attempts=1, quiet=True, bus_retries=1):
            return False
        # Two tries, not the full ladder (which climbs to 12 s between
        # retries): a board that answered the stop and then will not take
        # a slot config is treated as absent and reprobed later, rather
        # than holding the sweep - and a cue - for half a minute (R8,
        # review round 3).
        ok = self._request(bus, slot_config(board, self.slot,
                                            group_count=groups,
                                            dev_type=self._active_dev_type()),
                           f"cfg @{board:02d}",
                           attempts=min(2, self.command_attempts))
        if ok:
            self._cfg_done.add((board, self.slot))
        return ok

    def _forget_board(self, board: int) -> None:
        """A board that failed to take data may have rebooted - which,
        even though 0x13/0x1F/0x1B all persist across a power cycle
        (docs/MERIS_REPLY_3SLOT.pdf), is reason enough to re-verify every
        one of its slots with a write rather than trust the caches."""
        self._cfg_done = {k for k in self._cfg_done if k[0] != board}
        self._delays_sent = {k: v for k, v in self._delays_sent.items()
                             if k[0] != board}
        self._burn_cache = {k: v for k, v in self._burn_cache.items()
                            if k[0] != board}

    def _forget_burned(self, board: int, slot: int) -> None:
        """A manual /prepare (always slot 19) just wrote over what a
        burn might have put there - the cache no longer knows what the
        board holds, so the next burn must not skip it."""
        self._burn_cache.pop((board, slot), None)

    def _forget_slot(self, board: int, slot: int) -> None:
        """The slot has just been DELETED on this board (0x14): its
        picture, its 0x1B config and its delay table are all gone with
        it, so nothing here may go on claiming the board holds them -
        the next Upload has to write all three again."""
        self._burn_cache.pop((board, slot), None)
        self._cfg_done.discard((board, slot))
        self._delays_sent.pop((board, slot), None)

    def absent_snapshot(self) -> "set[int]":
        """A copy of `absent` for other threads (ui/showplay.py's burn
        gate, ui/app.py's DEMO screen): the worker changes the set in
        place, and iterating a live set from another thread can raise
        "set changed size during iteration" mid-show (review F10)."""
        with self._lock:
            return set(self.absent)

    def _drop(self, board: int) -> None:
        """Stop bothering an unreachable board until a reprobe finds it."""
        if board in self.live:
            self.live.remove(board)
        with self._lock:
            self.absent.add(board)
        self._forget_board(board)
        self.emit(f"board {board} dropped, will reprobe")

    @property
    def expected(self) -> int:
        """How many boards the wall is taken to have: the highest that
        ever answered when exploring, the length of the list otherwise."""
        if self.explore:
            return max(self.live) if self.live else 0
        return len(self.boards)

    @property
    def reported_boards(self) -> "list[int]":
        """The board list as told to the LCD and the show PC: when
        exploring, 1 up to the highest board that answers."""
        if self.explore:
            return [b for b in self.boards if b <= self.expected]
        return list(self.boards)

    def _group_count(self) -> int:
        """The group_count every frame carries: the board list's length,
        or its highest address when the list has gaps. It follows the
        list, so a show's own list changes it too (see
        _apply_job_boards())."""
        return max(len(self.boards), max(self.boards))

    def _take_groups(self) -> int:
        """The group count this worker carries from here on, remembered
        so /status can report what is really on the wire rather than
        what the list would give: an exploring worker keeps the count it
        opened the port with while _setup() trims the list under it."""
        self._groups = self._group_count()
        return self._groups

    @property
    def group_count(self) -> int:
        """What /status reports: the number the worker's frames are
        actually carrying, or - with no worker on the port - what the
        list in force would give the next one."""
        return self._group_count() if self._groups is None else self._groups

    @property
    def boards_source(self) -> str:
        """Where the list in force came from, for /status and the PC's
        tile: a show file's own garment list ("show"), one job's boards
        ("job" - a manual /prepare, or a burn on a unit holding no show
        list), this unit's discovery ("explore"), or the --boards it was
        started with ("fixed").

        The rehearsal failure this exists for was invisible from the
        Conductor: every picture was written, and what was wrong was
        which sockets the unit believed in (radxa-04, 2026-09-26)."""
        if self._boards_given is not None:
            return self._boards_given
        return "explore" if self.explore else "fixed"

    def take_own_boards(self) -> bool:
        """Back to the list this unit was started with; True if it moved.

        A show PC's list rules only while the show PC does. This is the
        one way back: _start() calls it when the unit's own menu takes
        the port (KEY1, standby), and ui/remote.py's release() calls it
        when the PC simply lets go (KEY2 to MENU, with no pattern
        started - which used to leave a bench unit reporting the show's
        16 boards for ever; review, 2026-09-27). Safe to call at any
        time: it only ever restores what the constructor was given.
        """
        if self._boards_given is None:
            return False
        self._boards_given = None
        self.explore = self._own_explore
        self.boards = list(self._own_boards)
        back = ("exploring again" if self.explore
                else f"back to boards {self._fmt_boards(self.boards)}")
        self.emit(f"the show's board list is released, {back}")
        return True

    def _apply_job_boards(self, wanted: "list[int]",
                          source: str = "job") -> bool:
        """Adopt the show PC's board list as THE list; True if it changed.

        `source` is where the list came from, for /status: "show" is the
        show file's own garment list (ui/remote.py's set_boards()),
        "job" the boards of one /prepare or one burn. A job that brings
        the very same list a show already set does not take the show's
        name off it.

        The PC knows the garment, so while its list is in force nothing
        outside it is live, absent or explored: a socket the show does
        not name is not probed at all. radxa-04, 2026-09-26 rehearsals -
        it came up in STANDBY, its discovery logged "board 17-22 absent,
        skipping" (the explore looks EXPLORE_GAP past the last live
        board), and those six stayed in `absent` once the show's own
        list of 1-16 arrived. _reprobe() then spent ~15 s of every
        minute (six serial timeouts) probing sockets the show knew were
        empty, and the cues whose trigger fell inside one of those
        probes went out +492 / +246 / +73 ms late. Traffic to boards
        that do not exist is bus noise inside a show.

        What is already known about a listed socket stays known: a board
        the standby sweep found empty gets one probe in the setup below,
        not three (29 s for 15 empty sockets on the bench, 2026-09-21).
        """
        wanted = sorted(wanted)
        keep = set(wanted)
        known = set(self.boards) | set(self.live) | set(self.absent)
        dropped = sorted(known - keep)
        changed = wanted != sorted(self.boards) or bool(dropped)
        was_live = set(self.live)
        with self._lock:
            self.absent = {b for b in self.absent if b in keep}
        self.live = [b for b in wanted if b in was_live]
        self.boards = wanted
        self.explore = False            # the show PC knows the garment
        if source == "show" or changed or self._boards_given is None:
            self._boards_given = source
        if changed:
            note = (f" ({self._fmt_boards(dropped)} dropped, not probed)"
                    if dropped else "")
            self.emit(f"boards {self._fmt_boards(wanted)} from the show{note}")
        return changed

    def _reprobe(self, bus, groups: int) -> bool:
        """Give absent boards a quick chance to join; True if any did.

        This is how a board powered on after the show started still gets
        into it: one short probe per board per interval, so eighteen
        empty sockets cost about nine seconds a minute and a board that
        appears is drawing within a cycle.

        Only boards on the list, ever: while a show's list is in force
        that is the show's list, and a socket outside it is not probed
        even if it once answered (see _apply_job_boards()).
        """
        stray = self.absent - set(self.boards)
        if stray:
            with self._lock:
                self.absent -= stray
        if not self.absent or time.monotonic() < self._next_reprobe:
            return False
        self._next_reprobe = time.monotonic() + self.reprobe_interval
        # Not while a cue is nearly due: a pass over six absent boards
        # is 15 s, and _fire_at() calls this from inside its own wait.
        # What is left unprobed stays absent for the next interval.
        joined = [board for board in sorted(self.absent)
                  if not self._trigger_due_soon()
                  and not self._stop.is_set()
                  and self._probe(bus, board, groups)]
        if not joined:
            return False
        with self._lock:
            self.absent -= set(joined)
        keep = set(self.live) | set(joined)
        self.live = [b for b in self.boards if b in keep]
        if self.explore:                    # look a little past the newcomer
            horizon = min(MAX_BOARD_ID, max(self.live) + EXPLORE_GAP)
            beyond = [b for b in DEFAULT_BOARDS if b > max(self.boards)
                      and b <= horizon]
            self.boards += beyond
            with self._lock:
                self.absent |= set(beyond)
        self.emit(f"board {self._fmt_boards(joined)} joined "
                  f"({len(self.live)}/{self.expected})")
        return True

    # ---- one cycle ----

    def _setup(self, bus, groups: int) -> bool:
        """Silence playback and (re)configure every board that answers.

        A board that stays silent is skipped, not fatal: the wall is
        built for 20 boards but runs with whatever subset is powered.
        The probing sweeps the list a few times, because a present board
        is deaf for the 9.8 s of a repaint and the factory autoplay is
        repainting at power-on - one pass would misread it as absent.
        Stragglers go to `absent`, where the periodic reprobe picks
        them up if they ever appear.
        """
        self._probing = True
        try:
            return self._setup_locked(bus, groups)
        finally:
            self._probing = False

    def _setup_locked(self, bus, groups: int) -> bool:
        """_setup()'s body, with `_probing` held: no automatic bus
        recovery may run inside a sweep that is already asking every
        board (and would reopen the port under it)."""
        bus.send(stop(0xFF, groups))
        # This IS the "one broadcast stop when the worker takes the
        # port" the REMOTE loop owes at start-up - it is not sent twice,
        # and the heartbeat's first interval runs from here.
        self._sent_broadcast_stop()
        if not self._wait_probing(bus, groups, 0.3):
            return False
        if self.explore:
            self.boards = list(DEFAULT_BOARDS)      # the search starts over
        # Cheap to redo, so always re-verified on a bus reopen - unlike
        # _burn_cache, which is deliberately NOT cleared here: a burn is
        # expensive (docs/MERIS_REPLY_3SLOT.pdf says the write itself
        # persists across a power cycle), so only a board that actually
        # dropped out (_drop(), below) loses its place in that cache.
        self._cfg_done = set()
        self._delays_sent = {}
        known_absent = {b for b in self.boards if b in self.absent}
        pending = list(self.boards)
        found: list[int] = []
        for sweep in range(self.probe_sweeps):
            if not pending:
                break
            if sweep and not self._wait_probing(bus, groups,
                                                self.probe_sweep_delay):
                return False
            still = []
            # Until a board answers the whole range is searched: the
            # low addresses can be the ones that are dead (a power feed
            # off, a cable out) while the rest of the garment is fine.
            reach = MAX_BOARD_ID if self.explore and sweep == 0 else None
            for board in pending:
                if reach is not None and board > reach:
                    break                       # nothing for a while: the end
                self._fire_before_probing(bus, groups)
                if self._probe(bus, board, groups):
                    found.append(board)
                    if reach is not None:
                        reach = board + EXPLORE_GAP
                else:
                    still.append(board)
            if reach is not None:
                # Only what lies within reach is worth the extra sweeps
                # (a board mid-repaint); the rest is the empty end of the bus.
                self.boards = [b for b in self.boards
                               if b <= min(reach, MAX_BOARD_ID)]
            # The extra sweeps exist to catch a fitted board that is deaf
            # mid-repaint. A board already known absent was not fitted a
            # moment ago, so it gets one pass here and the periodic
            # reprobe later - not three sweeps holding up the first frame.
            pending = [b for b in still if b not in known_absent]
        self.absent = set(self.boards) - set(found)
        self.live = [b for b in self.boards if b in set(found)]
        self._next_reprobe = time.monotonic() + self.reprobe_interval
        if not self.live:
            self.error = "no boards answering"
            self.emit("ERROR no boards answering")
            return False
        if pending:
            self.emit(f"board {self._fmt_boards(pending)} absent, skipping")
        self.emit(f"panels online: {len(self.live)}/{self.expected}")
        return True

    def _cycle(self, bus, groups: int, rng: random.Random) -> bool:
        self._reprobe(bus, groups)
        # A playlist hands back whichever pattern owns this cycle; a plain
        # pattern hands back itself.
        active, local_cycle = self.pattern.resolve(self.cycle)
        self.caption = active.caption(local_cycle) if active.caption else None
        frame = active(local_cycle, self.boards, self.palette, rng)
        updated = 0
        skipped = False
        for board in list(self.live):
            if self._stop.is_set():
                return False
            if not self._request(bus, stop(board, groups), f"stop @{board:02d}"):
                # Unreachable after the full ladder: out of the loop, so
                # one dead board cannot freeze the other nineteen.
                self._drop(board)
                skipped = True
                continue
            if (board, self.slot) not in self._cfg_done and not self._request(
                    bus, slot_config(board, self.slot, group_count=groups,
                                     dev_type=active.dev_type),
                    f"cfg @{board:02d}"):
                self._drop(board)
                skipped = True
                continue
            self._cfg_done.add((board, self.slot))
            arr = active.array(frame[board])
            if not self._request(bus, save_color(board, self.slot, arr, groups,
                                                 dev_type=active.dev_type),
                                 f"save @{board:02d}", self.save_attempts):
                # Answering but not taking data - it may have rebooted, so
                # every one of its slots needs re-verifying before the
                # next try (_forget_board()).
                self._forget_board(board)
                skipped = True
                continue
            updated += 1
        if updated == 0:
            return False

        # One broadcast show keeps the panels in step. Never repeat it as
        # insurance: boards queue commands received mid-repaint and play
        # them back afterwards, so each extra copy is another full repaint
        # (see SHOW_REPEATS).
        for _ in range(self.show_repeats):
            bus.send(show_single(0xFF, self.slot, groups,
                                 dev_type=active.dev_type))
            time.sleep(self.show_gap)

        self.cycle += 1
        if skipped:
            self.failures += 1
        label = "" if active is self.pattern else f" {active.label}"
        note = f" {self.caption}" if self.caption else ""
        self.emit(f"cycle {self.cycle}{label}{note} shown "
                  f"({updated}/{self.expected})")
        return True

    def _settle(self, bus, groups: int) -> None:
        """Let the repaint finish, then stop playback and leave it there.

        Only for the one-shot standby paint. Showing a slot starts the
        board playing, and once its 9.8 s repaint is done it would run on
        into the factory autoplay - which is exactly the thing standby
        exists to silence. The looping demo gets the same treatment from
        _wait_next after every cycle.
        """
        if not self._sleep(self.guard_delay):
            return
        bus.send(stop(0xFF, groups))
        for board in list(self.live):
            self._request(bus, stop(board, groups), f"stop @{board:02d}")

    def _watch_link(self, bus, groups: int, port: str) -> bool:
        """Sit on white until the panel link changes. False to stop.

        Standby is not a one-shot job: a board that loses power - an
        unplugged USB cable is enough - comes back playing the factory
        demo, and nothing on the board says so. Watching the device node
        is the cue to paint white again once it returns. The caller
        reopens the bus, since a replug can rename the port.

        A broadcast stop goes out every link_guard seconds as well. It
        costs one 8-byte frame and it is the backstop for a reboot this
        cannot see - it silences an autoplay that started unnoticed,
        which is what actually ruins the look of the wall.

        Absent boards are reprobed on the same clock: one powered on
        during standby comes up playing its factory demo, so finding it
        is treated like a link change and the white gets repainted.
        """
        token = self._link_token(port)
        next_guard = time.monotonic() + self.link_guard
        while not self._stop.is_set():
            if not self._sleep(self.link_poll):
                return False
            if self._link_token(port) != token:
                self.standby_ready = False
                self.emit("panel link changed, will re-blank")
                return True
            if self.link_guard > 0 and time.monotonic() >= next_guard:
                next_guard = time.monotonic() + self.link_guard
                bus.send(stop(0xFF, groups))
            if self._reprobe(bus, groups):
                self.standby_ready = False
                self.emit("new board joined, will re-blank")
                return True
        return False

    def _wait_next(self, bus, groups: int) -> bool:
        active, _ = self.pattern.resolve(max(self.cycle - 1, 0))
        interval = active.interval or self.interval
        if 0 < self.guard_delay < interval:
            if not self._sleep(self.guard_delay):
                return False
            bus.send(stop(0xFF, groups))    # suppress the factory autoplay
            return self._sleep(interval - self.guard_delay)
        return self._sleep(interval)

    # ---- remote cues and burns (ui/remote.py) ----

    def _save_one(self, bus, groups: int, slot: int, dev_type: int,
                 board: int, array: bytes,
                 table: "bytes | None" = None,
                 span_s: "float | None" = None) -> bool:
        """Write one board's picture and its delay table into `slot`. No
        per-board stop first: 0x13 is pure storage and never needs the
        board silenced (docs/MERIS_REPLY_3SLOT.pdf) - unlike _cycle()'s
        live pattern loop, which still silences before it draws.

        No `table` means "no sweep": the slot's pipeline is cleared
        (NO_TABLE -> 0x25) unless _save_delays() remembers it already
        is, so a slot never keeps a sweep from a previous show or a
        previous manual cue (review finding F5, 2026-09-25).

        `span_s` is only what the sweep was asked for, for the log line
        _save_delays() writes; nothing is timed by it here."""
        if board not in self.live:
            return False
        if table is None:
            table = NO_TABLE
        if (board, slot) not in self._cfg_done and not self._request(
                bus, slot_config(board, slot, group_count=groups,
                                 dev_type=dev_type), f"cfg @{board:02d}"):
            self._drop(board)
            return False
        self._cfg_done.add((board, slot))
        if not self._save_delays(bus, groups, board, table, dev_type, slot,
                                 span_s=span_s):
            self._forget_board(board)
            return False
        if not self._request(bus, save_color(board, slot, array, groups,
                                             dev_type=dev_type),
                             f"save @{board:02d}", self.save_attempts):
            self._forget_board(board)
            return False
        return True

    def _save_cue(self, bus, groups: int, job: dict) -> "tuple[list, list]":
        """Write one manual cue's arrays to its slot; (saved, failed) -
        the Designs tab and a demo row's one-shot preview (always slot
        19, DEFAULT_SLOT)."""
        dev_type, slot = job["dev_type"], job.get("slot", TEST_SLOT)
        delays = job.get("delays") or {}
        saved, failed = [], []
        for board in sorted(job["boards"]):
            if self._stop.is_set():
                break
            if self._save_one(bus, groups, slot, dev_type, board,
                              job["boards"][board], delays.get(board),
                              span_s=job.get("span_s")):
                saved.append(board)
                self._forget_burned(board, slot)   # the burn cache is stale now
            else:
                failed.append(board)
        return saved, failed

    def _save_delays(self, bus, groups: int, board: int, table: bytes,
                     dev_type: int, slot: "int | None" = None,
                     span_s: "float | None" = None) -> bool:
        """Give a board the sweep's delay table unless it already holds
        it (in this `slot`). The show file's table is 64 sockets of
        uint16, big-endian, already in the board's own unit - 10 ms
        frames (NO_DELAY: no delay given); the board takes it as V1.4's
        0x1F, and a table with no delays at all is 0x25 - forget the
        sweep. A board whose firmware does not know the commands is
        remembered and left alone: the picture still goes out, in
        socket order.

        `span_s`, when the caller knows it, is the span the sweep was
        ASKED for - only for the log line below."""
        slot = self.slot if slot is None else slot
        if board in self.no_sweep or self._delays_sent.get((board, slot)) == table:
            return True
        values = struct.unpack(">64H", table)
        timed = [v for v in values if v != NO_DELAY]
        if not timed:
            frames = [clear_pipeline(board, slot, groups, dev_type=dev_type)]
            label = f"sweep off @{board:02d}"
        else:
            # A socket with no scale on it takes the LAST frame of this
            # board's sweep, not frame 0. The table has an entry for all
            # 64 sockets and the board is handed all of them; correct
            # firmware ignores the ones it has no segment for, so this
            # is the same bytes-on-the-glass either way - but frame 0
            # means "start with the very first scale", and if a board
            # ever did act on an unused socket that is a flash at the
            # wrong end of the garment at T0, where the last frame is
            # invisible (adversarial review, 2026-09-26).
            unused = max(timed)
            frames = list(save_pipeline(
                board, slot,
                [unused if v == NO_DELAY else v for v in values],
                groups, dev_type=dev_type))
            label = f"sweep @{board:02d}"
        for frame in frames:
            ack = bus.request(frame)
            if (ack is not None and ack.src == board
                    and ack.cmd == ACK_INVALID_CMD):
                self.no_sweep.add(board)
                self.emit(f"board {board}: firmware without sweeps "
                          f"(0x{frame.cmd:02X} refused) - update it to V1.4")
                return True
            if not (ack is not None and ack.src == board
                    and ack.cmd == ACK_SUCCESS
                    or self._request(bus, frame, label, self.save_attempts)):
                return False
        self._delays_sent[(board, slot)] = table
        # Said once per table, so the operator can see on the unit's
        # log that the sweep really reached the board (a cached table
        # is not sent again and not announced again).
        if not timed:
            self.emit(f"board {board}: sweep cleared")
        else:
            last = max(timed) * FRAME_S
            line = (f"board {board}: sweep table saved, {len(timed)} sockets,"
                    f" last starts +{last:.2f} s")
            # A garment's scales are spread over several boards, so this
            # board's own last start is normally BELOW the span the
            # sweep was asked for - the scales that finish it sit on
            # another board. Saying the span too is what turns "+2.44 s"
            # from something to double-check into something to read past
            # (radxa-01, 2026-09-26).
            if span_s is not None and last < float(span_s) - FRAME_S / 2:
                line += (f" of a {float(span_s):.2f} s span - the farthest"
                         f" scales are on other boards")
            self.emit(line)
        return True

    def _run_burn(self, bus, groups: int, session, burn_job: dict,
                  began: "float | None" = None) -> None:
        """Write every cue of a show into its own slot, in order,
        skipping whatever the cache already knows is there
        (ui/showplay.py's ShowPlayer.load()). Interruptible: a newer
        burn() or a cancel_burn() bumps the session's burn epoch, and
        this notices between boards, not just between cues, so
        /show/stop or a fresh /show/load never waits for the whole
        thing to finish - an interrupted burn ends "cancelled" with its
        reason, never "failed" (review round 2, 2026-09-25).

        `began` is when the worker took the job, so the time this
        reports is the one the operator waited - the probing before the
        first write is most of it on a wall with absent boards (the
        caller's _setup(), 22 s for 14 of them on radxa-01)."""
        epoch, dev_type = burn_job["epoch"], burn_job["dev_type"]
        done, failed = 0, []
        began = time.monotonic() if began is None else began
        pairs = [(cue, board) for cue in burn_job["cues"]
                 for board in sorted(cue["boards"])]
        self._probe_burn_boards(bus, groups, burn_job)
        probe_s = time.monotonic() - began
        for n, (cue, board) in enumerate(pairs):
            slot = cue["slot"]
            # A burn owns the port for minutes. Nothing here schedules a
            # show (ShowPlayer waits for the burn before it arms
            # anything), but an operator's own /prepare + /fire can be
            # armed while one runs, and a cue is a cue (R3, round 3).
            self._fire_before_probing(bus, groups)
            if self._stop.is_set() or not session.burn_current(epoch):
                # Taken off the port (KEY1 on a pattern, KEY2, a shutdown)
                # or superseded (a newer burn(), a cancel_burn()). The
                # burn is CANCELLED with its reason, not "failed": what
                # was never reached is not a board that refused a write,
                # and a "failed" would have the PC offer a force this
                # unit refuses (review finding F4 + round 2, 2026-09-25).
                # For a superseded burn this is a no-op (its epoch has
                # moved on) and the newer state stands.
                left = len(pairs) - n
                why = "the port was taken" if self._stop.is_set() else "stopped"
                session.burn_cancelled(epoch, f"interrupted: {why}")
                self.emit(f"burn interrupted: {done - len(failed)}/{len(pairs)}"
                          f" boards written, {left} left")
                return
            array = cue["boards"][board]
            # No table in the cue = "no sweep" (NO_TABLE, cleared once):
            # the same key whether the show file says so or says nothing.
            table = (cue.get("delays") or {}).get(board)
            if table is None:
                table = NO_TABLE
            if board not in self.live:
                failed.append((board, slot))
            elif self._burn_cache.get((board, slot)) == (array, table):
                pass                     # unchanged: nothing to write
            elif self._save_one(bus, groups, slot, dev_type, board,
                                array, table, span_s=cue.get("span_s")):
                self._burn_cache[(board, slot)] = (array, table)
            else:
                failed.append((board, slot))
            done += 1
            session.burn_progress(epoch, done, failed)
        session.burn_finished(epoch, failed, self._all_absent(pairs, failed))
        # Everything in one line, because the unit's log on the tile is
        # six entries deep and the "absent ... skipped" line above can
        # have scrolled off by the time a long burn ends.
        absent = [b for b in self.boards if b in self.absent]
        self.emit(f"burn done: {done - len(failed)}/{done} in "
                  f"{time.monotonic() - began:.1f} s (probe {probe_s:.1f} s, "
                  f"{len(self.live)} live boards, {len(absent)} absent"
                  + (f": {self._fmt_boards(absent)})" if absent else ")"))

    def _all_absent(self, pairs, failed) -> "str | None":
        """"none of its 16 boards answered", when that is what this burn
        found - the show's whole garment is dark (its feed is off, or it
        is not plugged in). The PC words that case its own way and lets
        the operator start the other units past it, which is why the
        unit says it rather than leaving the PC to guess from a list of
        pairs that happens to be complete: a wall whose boards all
        answered and all refused the write looks the same in `failed`
        and is NOT this."""
        if not failed or len(failed) != len(pairs):
            return None
        boards = sorted({board for board, _ in failed})
        if not all(board in self.absent for board in boards):
            return None
        return f"none of its {len(boards)} boards answered"

    def _probe_burn_boards(self, bus, groups: int, burn_job: dict) -> None:
        """Give every board this burn names, and that nothing is known
        about yet, one short probe before the first slot is written.

        Measured on radxa-01 (2026-09-25): a board that is not there
        costs about 1.5 s of serial timeout. Without this pass those
        timeouts landed inside the first slot's writes (14 unknown
        boards of a 16-board garment = 22 s on the first slot, 0.3 s on
        each of the next), so the PC's "writing 0/64" sat still for 22 s
        and then raced - and nothing said why. Paid once here instead,
        with the absent boards named in the log; a board that appears
        later is picked up by the periodic reprobe as always.
        """
        wanted = sorted({b for cue in burn_job["cues"]
                         for b in cue["boards"]})
        unknown = [b for b in wanted
                   if b not in self.live and b not in self.absent]
        for board in unknown:
            if self._stop.is_set():
                return
            self._fire_before_probing(bus, groups)
            if self._probe(bus, board, groups):
                if board not in self.live:
                    self.live = [b for b in self.boards if b in
                                 set(self.live) | {board}]
            else:
                with self._lock:
                    self.absent.add(board)
        gone = [b for b in wanted if b in self.absent]
        if gone:
            self.emit(f"{len(gone)} board{'' if len(gone) == 1 else 's'} "
                      f"absent ({self._fmt_boards(gone)}) - skipped")

    # ---- taking the pictures back out of the slots (0x14) ----

    def _clear_ready(self, guard_due: "float | None") -> bool:
        """True when a queued clear may start: no guard STOP is still
        owed and the last cue's guard floor has passed.

        That floor is the same one the heartbeat waits for (see
        _remote_guard_clear()) - a cue's own refresh plus its sweep plus
        the margin, measured from its LAST broadcast. A 0x14 landing
        inside a repaint is the one thing a clear must never do, so it
        waits for exactly the instant that says the glass is finished,
        and the owed guard STOP goes out first (the loop below sends it,
        then comes round to this).
        """
        if guard_due is not None or self._guard_owed is not None:
            return False
        floor = self._guard_floor
        return floor is None or time.monotonic() >= floor

    def _run_clear(self, bus, groups: int, session, clear_job: dict,
                   began: "float | None" = None) -> None:
        """Delete the show's pictures from their slots, acked, one
        (board, slot) at a time - the same retry ladder as a save.

        Why: after the show on 2026-09-27 the operator pressed STOP and
        unplugged the Radxa from a garment whose boards were still on
        battery. About a minute later the master board restarted the
        FACTORY AUTOPLAY and cycled slots 0-18 - it replayed the show's
        pictures on its own, on the floor, with nothing left able to stop
        it. So the pictures must not be in the slots any more by the time
        the garment is unplugged.

        Nothing here REPAINTS anything: no 0x1D, no standby, slot 0 is
        never shown. The garment goes on holding the last look it was
        given for as long as it has power - the operator's rule
        (2026-09-27): "as long as the Radxa stays connected the garment
        keeps showing its last design". Only the slots the autoplay
        would cycle through are emptied.

        Slot 0 (the standby white) and slot 19 (the manual / demo
        one-shot) are left alone, and 0x15 (全消去) is FORBIDDEN and
        never sent - docs/SPECIFICATION.md 2.4 and host/epaper/commands.py,
        which does not even build the frame.

        Interruptible between slots: a START cancels the clear
        (ui/showplay.py's run()), and this finishes the slot it is on,
        says so and stops. What is already deleted stays deleted, so the
        burn record reads "cleared partially" and both gates ask for an
        Upload - never a half-cleared garment quietly started again.
        """
        epoch, slots = clear_job["epoch"], clear_job["slots"]
        began = time.monotonic() if began is None else began
        pairs = [(board, slot) for board in self.boards for slot in slots]
        session.clear_started(epoch, len(pairs))
        live = len(self.live)
        self.emit(f"clear: slots {self._fmt_boards(slots)} on {live} "
                  f"board{'' if live == 1 else 's'}...")
        done, failed = 0, []
        for n, (board, slot) in enumerate(pairs):
            if self._stop.is_set() or not session.clear_current(epoch):
                # Superseded (a START, a fresh Upload) or taken off the
                # port. A superseded clear's state is the canceller's to
                # write - clear_cancelled() no-ops on a stale epoch - so
                # only the port case reports itself here.
                why = ("the port was taken" if self._stop.is_set()
                       else "a new run started")
                session.clear_cancelled(epoch, f"interrupted: {why}")
                self.emit(f"clear interrupted: {done - len(failed)}/"
                          f"{len(pairs)} slots deleted, {len(pairs) - n} left")
                return
            if board not in self.live:
                failed.append((board, slot))
            elif self._request(bus, delete_slot(board, slot, groups),
                               f"delete @{board:02d} slot {slot}",
                               self.save_attempts):
                self._forget_slot(board, slot)
            else:
                failed.append((board, slot))
                self._forget_board(board)
            done += 1
            session.clear_progress(epoch, done, failed)
        session.clear_finished(epoch, failed)
        if failed:
            hurt = sorted({board for board, _ in failed})
            self.emit(f"clear failed: {len(failed)} slots on boards "
                      f"{self._fmt_boards(hurt)}")
        self.emit(f"clear done: {done - len(failed)}/{done} in "
                  f"{time.monotonic() - began:.1f} s")

    def _guard_for(self, session) -> float:
        """Seconds after a fire before the guard STOP may go out.

        The flat `guard_delay` is a refresh plus a margin (see
        GUARD_REFRESH_S). When the cue says how long it actually needs -
        its refresh and its sweep's span, carried by /prepare and by the
        show file's cues - the same margin is measured from the picture's
        real completion instead, so the STOP can never land inside a
        sweep. A cue that says nothing (an older conductor's body, a
        show file from before cues carried a span) keeps the flat delay,
        and the flat delay is also the floor: this only ever waits
        longer than it used to, never less.
        """
        span_s = getattr(session, "span_s", None)
        if span_s is None:
            return self.guard_delay
        refresh_s = getattr(session, "refresh_s", None)
        if refresh_s is None:
            refresh_s = GUARD_REFRESH_S
        margin = max(0.0, self.guard_delay - GUARD_REFRESH_S)
        return max(self.guard_delay,
                   min(GUARD_MAX_S,
                       float(refresh_s) + float(span_s) + margin))

    def _guard_after_fire(self, session) -> float:
        """When the guard STOP may go out after a cue.

        Measured from the LAST broadcast of that cue, not from its fire
        time: the landing check's re-send restarts the picture, so the
        guard has to move with it or a re-sent cue gets its 0x17 while
        it is still drawing."""
        base = self._last_show_at
        if base is None:
            base = time.monotonic()
        return base + self._guard_for(session)

    # ---- did the broadcast land? (see VERIFY_AFTER_S) ----

    def _sweep_start(self, board: int, slot: int) -> "float | None":
        """Seconds after a show broadcast before this board's FIRST
        socket starts repainting `slot`, or None if nothing here knows.

        What is known comes from the writes this runner made: the table
        it last sent (_delays_sent) or, when a burn skipped the write
        because the board already held it, the burn cache. A board whose
        firmware has no sweeps (no_sweep) ignores tables altogether and
        always starts at once.
        """
        if board in self.no_sweep:
            return 0.0
        table = self._delays_sent.get((board, slot))
        if table is None:
            cached = self._burn_cache.get((board, slot))
            table = cached[1] if cached else None
        if table is None:
            return None
        timed = [v for v in struct.unpack(">64H", table) if v != NO_DELAY]
        return min(timed) * FRAME_S if timed else 0.0

    def _witness(self, session, slot: int) -> "tuple[int, float] | str":
        """(board, delay) - who to ask after the broadcast, and when -
        or a string saying why nothing may be asked at all.

        Under the default `usb` policy there is exactly one candidate,
        the board on the USB cable (see WITNESS_USB): a relayed question
        could wedge that board's CDC, and a wedged master mid-show is
        worse than the lost frame this is looking for. Under `any` the
        witness is the LIVE board that starts repainting EARLIEST, which
        is the strongest witness - by the time the question goes out it
        is the one most surely inside its deaf window.

        `delay` is that board's OWN first-socket start. On a sweep that
        begins away from it the question is asked seconds after the fire
        rather than one second after it; that is the price of asking a
        board whose answer means something.

        A reason instead of a board means "do not ask": a swept cue
        whose tables this runner never wrote (a burn done before a
        restart, say) gives no honest instant to ask at, and asking too
        early reads a board that has not begun as "idle" - which costs
        the wall a second repaint, worse than not checking.
        """
        live = list(self.live)
        if not live:
            return "no live board"
        if self.verify_witness == WITNESS_USB:
            if USB_BOARD not in live:
                return "usb board absent"
            candidates = [USB_BOARD]
        else:
            candidates = live
        known = [(start, board) for start, board in
                 ((self._sweep_start(b, slot), b) for b in candidates)
                 if start is not None]
        if known:
            start, board = min(known)
            span_s = getattr(session, "span_s", None)
            cap = (float(span_s) + FRAME_S
                   if isinstance(span_s, (int, float))
                   else VERIFY_MAX_DELAY_S)
            if not start <= cap:        # ...and a NaN cap fails it too
                # A table nobody can have meant (see VERIFY_MAX_DELAY_S).
                # Not checked rather than waited out: the worker owes the
                # wall a guard STOP and a reprobe long before then.
                return f"sweep start {start:.1f} s too late to check"
            return board, start
        if getattr(session, "span_s", None):
            return f"no sweep table known for slot {slot}"
        return candidates[0], 0.0

    def _yield_to(self, session, until: float,
                  horizon: float = 0.0) -> "str | None":
        """What outranks the landing check right now, or None.

        A cue owns the port before anything else: a trigger at or before
        `until` takes the window (the next broadcast answers "did the
        last one land" anyway). `horizon` covers the work that would
        follow - the question itself takes verify_read x VERIFY_TRIES,
        and starting one just before a trigger would hold that trigger
        up by the whole of it. A cue armed beyond the horizon does not
        count: on a show the next cue is armed the moment this one
        applies, and yielding to that would mean never checking
        anything.
        """
        if self._stop.is_set():
            return "stopped"
        due = session.due()
        if due is not None and due[1] <= max(until,
                                             time.monotonic() + horizon):
            return f"cue {due[0]} is due"
        return None

    def _verify_wait(self, session, until: float) -> "str | None":
        """Wait for the instant to ask; a reason to give up, or None."""
        horizon = self.verify_read * VERIFY_TRIES
        while True:
            give_up = self._yield_to(session, until, horizon)
            if give_up is not None:
                return give_up
            left = until - time.monotonic()
            if left <= 0:
                return None
            self._stop.wait(min(left, 0.05))

    def _repair_blocked(self, session, span_s: float,
                        refresh_s: float) -> "str | None":
        """Why the lost cue may NOT be broadcast again, or None.

        The boards queue what arrives mid-repaint and run it afterwards,
        so the cost of repairing is not a double repaint of this slot -
        it is the NEXT cue going out however much of this repair is
        still running when its trigger comes. The repair is worth that
        up to REPAIR_LATE_S: a one-second sweep found lost 2.5 s into a
        nine-second gap finishes 1.5 s into the next cue's own start and
        goes out; a seven-second sweep found lost at 8.5 s would finish
        11.5 s late and does not.

        `span_s` / `refresh_s` are the REPAIRED cue's own (taken when it
        fired) - the session may already be holding the next cue's.
        """
        if self._stop.is_set():
            return "stopped"
        due = session.due()
        if due is None:
            return None
        now = time.monotonic()
        if due[1] <= now:
            return f"cue {due[0]} is due"
        if (now + refresh_s + span_s) - due[1] > REPAIR_LATE_S:
            return f"cue {due[0]} due in {due[1] - now:.1f} s"
        return None

    def _ask_witness(self, bus, groups: int, board: int, dev_type: int,
                     cue_id: str) -> str:
        """A short read-only question. "idle", "busy" or "deaf".

        "idle" - the board answered, so it is listening, so it is not
        repainting, so the show frame never reached it. Any answer means
        that: production firmware refuses 0x02 with ACK_FAIL 0x0A
        (docs/SPECIFICATION.md 5.5), and a refusal is an answer. Only
        ACK_BUSY is read as "working", and silence as "deaf".

        Asked VERIFY_TRIES times before silence is believed (the whole
        window is verify_read x VERIFY_TRIES, 0.6 s by default). The link
        this check exists for drops frames on the way OUT as well: a
        question that never arrived looks exactly like a board too busy
        to answer, and that reading would report the lost cue as landed.

        A frame from another board is a late answer to an earlier
        question (host/epaper/transport.py drops those, this is the
        backstop) - it says nothing about this board, so it is read as
        deaf: the cautious way round, since the cost of a wrong "idle"
        is a second repaint on the glass.
        """
        ack = bus.request(get_version(board, groups, dev_type=dev_type),
                          retries=VERIFY_TRIES, timeout=self.verify_read)
        if ack is None:
            return "deaf"
        if ack.src != board:
            self.emit(f"cue {cue_id} stray reply from "
                      f"0x{ack.src:02X} ignored")
            return "deaf"
        if ack.cmd == ACK_BUSY:
            return "busy"
        return "idle"

    def _cue_snapshot(self, session) -> dict:
        """What the landing check must know about the cue going out now,
        read while the session still holds it (see _fire_at()).

        `span_s` and `refresh_s` size the repair budget; `gen` is the
        session's glass generation, which is what tells a heal of this
        very fire from a NEW fire of the same cue id after a STOP/START
        or a demo's next lap (ui/remote.py's glass_gen).
        """
        span_s = getattr(session, "span_s", None)
        refresh_s = getattr(session, "refresh_s", None)
        return {
            "span_s": (float(span_s) if isinstance(span_s, (int, float))
                       else 0.0),
            "refresh_s": (float(refresh_s)
                          if isinstance(refresh_s, (int, float))
                          else GUARD_REFRESH_S),
            "gen": getattr(session, "glass_gen", None),
        }

    def _verify_landing(self, bus, groups: int, session, cue_id: str,
                        slot: int, dev_type: int, sent_at: float,
                        fired: dict) -> None:
        """Check that the cue's broadcast landed; re-send it once if not.

        At most two broadcasts per FIRE: the one _fire_at() already sent
        and, only against evidence that no board took it, one more.
        Everything else is a log line. A cue can be fired more than once
        - ui/showplay.py re-arms the same cue id to heal a board that
        joined late - and a heal of a cue this already confirmed is not
        checked again (below), so the healed fire adds exactly one frame.
        """
        if not self.verify_fire:
            return
        cue_span, cue_refresh = fired["span_s"], fired["refresh_s"]
        landed = getattr(session, "verify", None) or {}
        if (landed.get("cue") == cue_id
                and landed.get("landed") in ("deaf", "busy")
                and fired["gen"] is not None
                and getattr(session, "verify_gen", None) == fired["gen"]):
            # The same cue broadcast again for a board that joined late
            # (ui/showplay.py's heal): it was confirmed on the glass once
            # already, and this frame is for a board that was not even
            # there to be asked about. Checking again would only spend
            # port time and risk a re-send nothing needs - so a heal of a
            # confirmed cue adds exactly one broadcast. (If some other
            # cue was checked in between, this does not match and the
            # heal is checked like anything else: harmless, since a heal
            # repaints and the witness then reads deaf.)
            self.emit(f"cue {cue_id} landed already, not checked again")
            return
        chosen = self._witness(session, slot)
        if isinstance(chosen, str):
            self.emit(f"cue {cue_id} verify skipped: {chosen}")
            self._record_verify(session, cue_id, "skipped", False, None,
                                fired["gen"])
            return
        board, delay = chosen
        resent = False
        while True:
            ask_at = sent_at + delay + self.verify_after
            give_up = self._verify_wait(session, ask_at)
            if give_up is not None:
                self.emit(f"cue {cue_id} verify skipped: {give_up}")
                self._record_verify(session, cue_id, "skipped", resent, board,
                                    fired["gen"])
                return
            # THE INVARIANT: never ask before the witness has had its own
            # sweep start plus the whole deaf window. A board that has not
            # begun repainting answers, an answer means "re-send", and a
            # re-send on a frame that did land is the 2026-08-14 double
            # repaint of the entire wall. _verify_wait() is the only way
            # in and cannot return early, so this is unreachable - and it
            # stays, because a future change to the timing that breaks it
            # must skip the check rather than repaint the garment twice.
            checked = time.monotonic() - sent_at
            if checked < delay + self.verify_after - 0.001:
                self.emit(f"cue {cue_id} verify skipped: asked "
                          f"{delay + self.verify_after - checked:.2f} s early")
                self._record_verify(session, cue_id, "skipped", resent, board,
                                    fired["gen"])
                return
            state = self._ask_witness(bus, groups, board, dev_type, cue_id)
            if state != "idle":
                self.emit(f"cue {cue_id} landed (@{board:02d} {state}, "
                          f"checked +{checked:.1f} s)")
                self._record_verify(session, cue_id, state, resent, board,
                                    fired["gen"])
                return
            if resent:
                # Two broadcasts out and the board still says it is idle.
                # A third is not insurance, it is the 2026-08-14 double
                # repaint; the operator is told instead.
                self.emit(f"cue {cue_id} re-send unconfirmed @{board:02d} "
                          f"(checked +{checked:.1f} s)")
                self._record_verify(session, cue_id, "idle-after-resend",
                                    True, board, fired["gen"])
                return
            # The ask itself took up to verify_read x VERIFY_TRIES, and
            # what may happen in that window - a stop, the next cue
            # coming due - outranks repairing the last one. A loss found
            # and NOT repaired is still a loss: it is recorded red, never
            # as "nothing to see" (review round 2).
            hold = self._repair_blocked(session, cue_span, cue_refresh)
            if hold is not None:
                self.emit(f"cue {cue_id} not applied at @{board:02d} "
                          f"(checked +{checked:.1f} s), not re-sent: {hold}")
                self._record_verify(session, cue_id, "idle-not-repaired",
                                    resent, board, fired["gen"])
                return
            self._send_timed(bus, show_single(0xFF, slot, groups,
                                              dev_type=dev_type),
                             f"show slot {slot}")
            again = time.monotonic()
            self.emit(f"cue {cue_id} not applied at @{board:02d} (checked "
                      f"+{checked:.1f} s), re-sent "
                      f"+{(again - sent_at) * 1000:.0f} ms")
            self._last_show_at = sent_at = again
            # The picture restarts here, so the heartbeat's floor moves
            # with it - the same wait this cue was given when it first
            # went out, not one worked out from whatever the session
            # holds by now.
            self._guard_floor = again + self._guard_hold_s
            resent = True

    def _record_verify(self, session, cue_id: str, landed: str,
                       resent: bool, board: "int | None",
                       gen: "int | None" = None) -> None:
        """Tell the session, for /status and the show PC's tile. A
        session too old to know about landing checks simply is not
        told, and one that does not carry a generation is told without
        one (which then never matches a heal - the safe way round)."""
        verified = getattr(session, "verified", None)
        if verified is None:
            return
        try:
            verified(cue_id, landed, resent=resent, witness=board, gen=gen)
        except TypeError:
            verified(cue_id, landed, resent=resent, witness=board)

    def _send_timed(self, bus, frame, what: str) -> float:
        """Put one broadcast on the bus and time the write itself.

        A healthy CDC takes a copy and returns in well under a
        millisecond. A board that is repainting stops servicing USB and
        the write BLOCKS instead - 40-400 ms in the 2026-09-27
        rehearsals, and the cue that was written into that window never
        appeared. The lateness in the fire's own log line says something
        went slowly; only the write's own clock says it was the BOARD,
        which is what separates an autoplay that restarted from a busy
        Radxa or a command the PC sent late.

        The stall is recorded whether or not the write then succeeded,
        and the exception (if any) goes on to the caller untouched.

        A show broadcast that goes out CLEANLY takes the mark down
        again (`ms` back to None, the count kept): a 60 ms stall in the
        second minute is worth a look then, not an amber mark on the
        tile through the encore.

        Returns how long the write took, in milliseconds - the number
        the heartbeat's recovery trigger and the fire-time re-send both
        decide on. A write that RAISED does not return it, and the stall
        is recorded all the same.
        """
        began = time.perf_counter()
        sent = False
        try:
            bus.send(frame)
            sent = True
        finally:
            took_ms = (time.perf_counter() - began) * 1000.0
            previous = self.bus_stall or {}
            if took_ms >= STALL_LOG_MS:
                self.bus_stall = {"ms": round(took_ms, 1), "frame": what,
                                  # Wall clock, so the unit can say how
                                  # long ago it was (ui/remote.py's
                                  # status() turns it into ago_s).
                                  "at": time.time(),
                                  "count": int(previous.get("count", 0)) + 1}
                self.emit(f"bus stalled {took_ms:.0f} ms on {what}")
            elif sent and previous.get("ms") and what.startswith("show"):
                self.bus_stall = {"ms": None, "frame": None, "at": None,
                                  "count": int(previous.get("count", 0))}
        return took_ms

    # ---- recovering a bus that accepts frames and executes none ----

    def _pad(self, bus) -> bool:
        """The resync padding, if this transport can write it at all.

        False for a bus that cannot (an old fake, a transport without
        the helper): the reopen path is then the whole recovery, which
        is the honest answer rather than a step silently skipped.
        """
        pad = getattr(bus, "pad", None)
        if pad is None:
            return False
        try:
            pad(RESYNC_PAD_BYTES)
        except Exception as exc:        # noqa: BLE001 - the STOP is the verdict
            self.emit(f"bus recovery: the padding did not go out ({exc})")
            return False
        return True

    def _timed_stop(self, bus, groups: int) -> float:
        """One broadcast STOP, timed, through the usual stall path.

        A write that raises is not an exception here: the point of the
        frame is the CLOCK, and a write that timed out took its whole
        write timeout, which is exactly the "still bad" answer. So the
        elapsed time is returned either way and the caller compares it
        with RECOVERED_MS like any other.
        """
        began = time.perf_counter()
        try:
            took_ms = self._send_timed(bus, stop(0xFF, groups), "stop")
        except Exception as exc:        # noqa: BLE001 - measured, not raised
            took_ms = (time.perf_counter() - began) * 1000.0
            self.emit(f"bus recovery: the stop did not go out ({exc})")
        self._sent_broadcast_stop()
        return took_ms

    def _fast_reopen(self, bus, groups: int,
                     port: "str | None" = None) -> float:
        """Close the port and open it again, and NOTHING else. Seconds
        matter here: this runs before a cue and after a re-enumeration.

        What _setup() does that this does too, and only this: the
        broadcast STOP, and the master on the USB cable configured again.
        There is no slave-count frame to re-send - `group_count` rides in
        every frame's header (host/epaper/commands.py), so the board list
        in force is carried by the very next frame and needs nothing of
        its own. The PER-BOARD configs and the probe sweep are skipped
        outright: a 22-board garment with absent boards spends tens of
        seconds in that sweep (16 s with six of them, 2026-09-25), and
        `live` / `absent` catching up matters far less than the next cue
        being on time. `_setup_owed` hands the sweep to _run_remote(),
        which runs it when the unit is idle and yields to every cue.

        Measured bound: the open itself is transport.Bus's 0.3 s of
        settle, the broadcast STOP is unacknowledged, and the master's
        config gets ONE try with a FAST_REOPEN_READ_S window - about
        0.5 s in all, 1.2 s taken as the budget. A write that itself
        times out adds up to transport.WRITE_TIMEOUT_S on top, which is
        the case the caller's own timed STOP then reports.

        Returns how long it took, in seconds.
        """
        began = time.perf_counter()
        if port is None:
            port = self.port or find_port() or getattr(bus, "port", None)
        opened = bus.reopen(port)
        if opened and opened != port:
            self.emit(f"port {opened}")
        # The boards are owed their sweep from here on, whatever happens
        # below: as far as they are concerned this is a new port.
        self._setup_owed = True
        try:
            bus.send(stop(0xFF, groups))
            self._sent_broadcast_stop()
            bus.request(slot_config(USB_BOARD, self.slot, group_count=groups,
                                    dev_type=self._active_dev_type()),
                        retries=1, timeout=FAST_REOPEN_READ_S)
        except Exception:           # noqa: BLE001 - the timed STOP is the verdict
            pass
        return time.perf_counter() - began

    def _recovery_done(self, by: "str | None", before_ms: float,
                       after_ms: float, recovered: bool) -> dict:
        """Record and announce one recovery; the dict is what the agent's
        POST /bus/recover answers with."""
        previous = self.bus_recovery or {}
        self.bus_recovery = {"at": time.time(), "by": by,
                             "before_ms": round(before_ms, 1),
                             "after_ms": round(after_ms, 1),
                             # How many recoveries have been RUN in this
                             # worker, successful or not - not how many
                             # worked.
                             "count": int(previous.get("count", 0)) + 1}
        if recovered:
            # The all-clear the stall mark already has a shape for: the
            # count of what happened is kept, the amber `ms` goes.
            stalled = self.bus_stall or {}
            self.bus_stall = {"ms": None, "frame": None, "at": None,
                              "count": int(stalled.get("count", 0))}
        span = f"({before_ms:.0f} → {after_ms:.0f} ms)"
        if recovered and by is None:
            self.emit(f"bus is clear, nothing to recover ({after_ms:.0f} ms)")
        elif recovered:
            self.emit(f"bus recovered by {by} {span}")
        else:
            self.emit(f"bus recovery failed {span}")
        return {"recovered": recovered, "by": by,
                "before_ms": self.bus_recovery["before_ms"],
                "after_ms": self.bus_recovery["after_ms"]}

    def _recover_bus(self, bus, groups: int) -> dict:
        """Get a bus that accepts frames and executes none working again.

        Cheapest first, because the padding-alone test could not be run
        from the show PC (see RECOVERED_MS):

          0  what is wrong. The stall already recorded, or - with none -
             one timed STOP, which on a healthy unit is the whole call:
             "recovered, by nothing".
          1  the resync padding, then PAD_SETTLE_S of quiet so the
             firmware's own inter-byte timeout expires behind it, then a
             timed STOP. Under RECOVERED_MS and the parser was the
             problem: "by padding".
          2  the FAST reopen (_fast_reopen(): the DTR toggle and the
             padding of a fresh open, which is the one thing a service
             restart does that nothing else does, plus the master's own
             config - no probe sweep), and a timed STOP. "by reopen", or
             "failed" and the operator is told to restart the unit.

        Bounded, and that is a requirement, not an accident - a cue must
        never wait on this. Typically well under a second; the worst case
        with every wait taken at its limit is step 1's padding write,
        0.5 s of settle and a STOP whose write timeout is 2 s
        (transport.WRITE_TIMEOUT_S) - about 2.6 s - and step 2's ~1.2 s
        of fast reopen plus a 2 s STOP, so about 6 s in all. NOTHING
        here paints, and _setup() is deliberately not called: it sweeps
        the whole board list, which is not bounded at all.

        Never re-entered: it is reached from _remote_guard_tick(), which
        _setup() and _fire_at() both call.
        """
        if self._recovering:
            return {"recovered": False, "by": None,
                    "before_ms": None, "after_ms": None}
        self._recovering = True
        try:
            before = (self.bus_stall or {}).get("ms")
            if before is None or before < RECOVERED_MS:
                # Nothing recorded, or a stall too small to be this
                # state (one is recorded from STALL_LOG_MS = 50 ms, and
                # a board busy repainting is a normal 40-400 ms). Ask
                # the port itself instead of padding on a guess.
                before = self._timed_stop(bus, groups)
                if before < RECOVERED_MS:
                    return self._recovery_done(None, before, before, True)
            if self._pad(bus):
                self._sleep(self.pad_settle)
                after = self._timed_stop(bus, groups)
                if after < RECOVERED_MS:
                    return self._recovery_done("padding", before, after, True)
            try:
                self._fast_reopen(bus, groups)
            except Exception as exc:    # noqa: BLE001 - said, never raised
                self.emit(f"bus recovery: the port would not reopen ({exc})")
                return self._recovery_done(None, before, before, False)
            after = self._timed_stop(bus, groups)
            if after < RECOVERED_MS:
                return self._recovery_done("reopen", before, after, True)
            return self._recovery_done(None, before, after, False)
        finally:
            self._recovering = False

    def _resend_on_stall(self, bus, frame, cue_id: str,
                         took_ms: float) -> None:
        """The opt-in fire-time re-send (--resend-on-stall, off by default).

        A cue whose own broadcast BLOCKED is the picture that is missing
        now, and the cheap half of the recovery - the padding, then the
        same frame again - costs about as long as the stall did. Exactly
        once, and never the reopen path: 5-10 s of that inside a cue is a
        worse fault than the one being fixed. A re-send that still stalls
        is reported and not tried a third time; the idle recovery picks
        the unit up afterwards, on the heartbeat, with the quiet rules
        that make a reopen safe.

        Off by default because of what it costs when the reading is
        wrong: if the stalled frame WAS executed, this restarts the
        repaint and the garment paints the same slot twice (see
        RESEND_STALL_MS).
        """
        if not self.resend_on_stall or took_ms < RESEND_STALL_MS:
            return
        self._pad(bus)
        try:
            after_ms = self._send_timed(bus, frame, "show re-send")
        except Exception as exc:        # noqa: BLE001 - said, never raised
            self.emit(f"cue {cue_id} re-send did not go out ({exc})")
            return
        previous = self.resend or {}
        self.resend = {"cue": cue_id, "at": time.time(),
                       "before_ms": round(took_ms, 1),
                       "after_ms": round(after_ms, 1),
                       "count": int(previous.get("count", 0)) + 1}
        if after_ms < RESEND_STALL_MS:
            self.emit(f"cue {cue_id} re-sent after {took_ms:.0f} ms stall "
                      f"(took {after_ms:.0f} ms)")
        else:
            self.emit(f"cue {cue_id} re-send still stalled "
                      f"({after_ms:.0f} ms)")

    def _precheck(self, bus, groups: int, cue_id: str,
                  deadline: "float | None" = None) -> None:
        """PRECHECK_S before a cue: is the port going to take the frame?

        One timed broadcast STOP. On a healthy unit that is 1-2 ms and the
        whole check; on the 2026-09-28 unit it was 359 ms, and the cue
        after it was the one that never appeared. So a stall here is put
        right IMMEDIATELY, on a clock that fits in the gap: the padding
        (~0.5 s), and behind it the fast reopen (~1.2 s), which from
        T-2.0 s still leaves the trigger its own instant.

        Skipped while the PREVIOUS cue's repaint may still be running
        (PRECHECK_AFTER_FIRE_S): a board repainting is deaf and stops
        servicing USB, so the measurement would be of the repaint and
        every healthy unit would read as degraded - and a 0x17 into that
        window is the one frame that must not go.

        The cue is NEVER delayed by the verdict, and that is what
        `deadline` (the trigger's own instant) is for: a step there is no
        time left for is not begun at all - the chain says so and the
        trigger goes out on time, and the idle recovery picks the unit up
        after the cue. So the worst this can do to a cue is whatever the
        LAST step it began overran by, and each one is budgeted
        (PAD_BUDGET_S, FAST_REOPEN_BUDGET_S).
        """
        if self.precheck_s <= 0 or self._prechecked == cue_id:
            return
        last = self._last_show_at
        if last is not None and time.monotonic() - last < self.precheck_after_fire:
            self._prechecked = cue_id       # asked and answered: not now
            return
        self._prechecked = cue_id

        def time_for(budget: float) -> bool:
            return deadline is None or time.monotonic() + budget <= deadline

        before = self._timed_stop(bus, groups)
        steps = []
        after = before
        if before >= STALL_RECOVER_MS:
            if not time_for(PAD_BUDGET_S):
                steps.append("no time before the cue")
            elif self._pad(bus):
                steps.append("padding")
                self._sleep(self.pad_settle)
                after = self._timed_stop(bus, groups)
                if after >= STALL_RECOVER_MS:
                    if not time_for(FAST_REOPEN_BUDGET_S):
                        steps.append("no time to reopen")
                    else:
                        steps.append("reopen")
                        try:
                            self._fast_reopen(bus, groups)
                        except Exception as exc:    # noqa: BLE001 - said, not raised
                            self.emit(f"precheck {cue_id}: the port would "
                                      f"not reopen ({exc})")
                        else:
                            after = self._timed_stop(bus, groups)
        # `by` names the CURE, so only a step that is one and only when
        # the port really did come back: "no time before the cue" is a
        # reason there is no cure, not one.
        cured = steps and steps[-1] in ("padding", "reopen")
        self.precheck = {"cue": cue_id, "at": time.time(),
                         "before_ms": round(before, 1),
                         "by": (steps[-1] if cured
                                and after < STALL_RECOVER_MS else None),
                         "after_ms": round(after, 1)}
        if not steps:
            self.emit(f"precheck {cue_id}: bus ok ({before:.0f} ms)")
            return
        chain = "".join(f" → {step}" for step in steps)
        end = (f"ok ({after:.0f} ms)" if after < STALL_RECOVER_MS
               else f"still stalled ({after:.0f} ms)")
        self.emit(f"precheck {cue_id}: stalled {before:.0f} ms{chain} "
                  f"→ {end}")

    # ---- a USB re-enumeration, noticed at once ----

    def _port_watch(self, bus, groups: int) -> bool:
        """Poll the device node; if it has gone, wait for it and reopen.

        The kernel takes the node away and brings it back under a new name
        in about half a second (see PORT_POLL_S). Until this existed the
        unit found out at its next write - up to 20 s later - and then
        waited reopen_delay and swept every board, five seconds in all.

        True when a loss was found AND dealt with, so the caller knows the
        port under it has been replaced. Rate-limited to PORT_POLL_S: this
        is called from every 50 ms tick of the worker's waits.
        """
        if self.port_poll <= 0:
            return False
        now = time.monotonic()
        if now < self._next_port_poll:
            return False
        self._next_port_poll = now + self.port_poll
        port = getattr(bus, "port", None) or self.port
        if not port or self._link_token(port) is not None:
            return False
        began = now
        found = None
        while time.monotonic() - began < self.port_back_wait:
            candidate = self.port or find_port()
            if candidate and self._link_token(candidate) is not None:
                found = candidate
                break
            if not self._sleep(PORT_RETRY_S):
                return False
        if found is None:
            # Not a re-enumeration: a cable out, or a board with no power.
            # The ordinary ladder (reopen_delay, port_wait, a full setup)
            # is the right answer to that, so this says so and stands down.
            self.emit(f"port gone for {self.port_back_wait:g} s, waiting")
            return False
        back = time.monotonic() - began
        try:
            self._fast_reopen(bus, groups, port=found)
        except Exception as exc:        # noqa: BLE001 - said, never raised
            self.emit(f"port lost → {found} back in {back:.1f} s, "
                      f"but it would not open ({exc})")
            return False
        after = self._timed_stop(bus, groups)
        state = (f"bus ok ({after:.0f} ms)" if after < RECOVERED_MS
                 else f"bus still stalled ({after:.0f} ms)")
        self.emit(f"port lost → {found} back in {back:.1f} s, {state}")
        self._next_port_poll = time.monotonic() + self.port_poll
        return True

    def _recover_quiet(self, now: float) -> bool:
        """True when nothing is going to want the bus for recover_quiet.

        The recovery is bounded but it is seconds long and it reopens the
        port, so it stands aside for everything: a cue armed within the
        quiet window, a prepare / burn / clear queued, a show being
        played or held (a run between two far-apart cues is exactly when
        this must NOT happen, however wide the gap looks), and the
        probing sweep, which is already asking every board anyway.
        """
        session = self.remote
        if session is None or self._firing or self._probing:
            return False
        if session.playing():
            return False
        due = session.due()
        if due is not None and due[1] - now < self.recover_quiet:
            return False
        return not session.pending_job()

    def _maybe_recover(self, bus, groups: int, took_ms: float) -> None:
        """The automatic trigger, on the heartbeat's own STOP.

        Two heartbeats in a row that each blocked >= STALL_RECOVER_MS.
        Two, because one is a board repainting and that ends by itself;
        this state does not, and 15 s apart (REMOTE_GUARD_S) two of them
        is half a minute of a bus that is going nowhere.
        """
        if took_ms < STALL_RECOVER_MS:
            self._stall_streak = 0
            return
        self._stall_streak += 1
        if self._stall_streak < 2 or self._recovering:
            return
        if self._recover_tries >= self.recover_attempts_max:
            if not self._recover_said_enough:
                self._recover_said_enough = True
                self.emit(f"bus recovery: {self._recover_tries} attempts, "
                          f"leaving it to the operator")
            return
        now = time.monotonic()
        if now < self._recover_next or not self._recover_quiet(now):
            return
        self._recover_tries += 1
        self._recover_next = now + self.recover_backoff
        self._stall_streak = 0
        self._recover_bus(bus, groups)

    def _sent_broadcast_stop(self) -> None:
        """Note that a broadcast 0x17 just went out, whoever sent it.

        The REMOTE heartbeat counts from the LAST stop the boards heard,
        not from its own last send: the setup sweep's opening stop and
        the guard stop after a fire each silence the autoplay just as
        well, and a heartbeat right behind one is a frame for nothing.
        """
        self._remote_guard_due = (None if self.remote_guard <= 0 else
                                  time.monotonic() + self.remote_guard)

    def _remote_guard_clear(self, session, now: float) -> bool:
        """True when the periodic idle STOP can safely go out.

        Three ways it could do harm, and one rule against each:

        * inside the repaint a cue just started - `_guard_floor`, the
          same wait the post-fire guard uses, worked out from that
          cue's own span and refresh at the moment it was broadcast
          (and pushed forward again by a landing check's re-send);
        * on top of a trigger about to go out - REMOTE_GUARD_HOLD_S of
          clearance before the next cue, which is also what keeps this
          out of the last stretch of _fire_at()'s wait;
        * in the middle of the writes of a prepare or a burn - those
          hold the bus themselves, so the heartbeat waits for the queue
          to be empty.

        `_firing` is deliberately NOT a rule here. One worker thread
        owns the bus, so the only places this is reached are that
        thread's own waits, where the bus is free by definition - and
        _fire_at()'s wait is where a running show spends nearly all of
        its time. The landing check needs no rule either: it runs
        inside the floor above, seconds after a broadcast that set it
        tens of seconds ahead.
        """
        if self._guard_floor is not None and now < self._guard_floor:
            return False
        due = session.due()
        if due is not None and due[1] - now < self.remote_guard_hold:
            return False
        return not session.pending_job()

    def _remote_guard_tick(self, bus, groups: int) -> None:
        """Send the idle autoplay guard if one is due and clear to go.

        Called from every wait the remote worker makes. The idle loop is
        NOT enough on its own: ui/showplay.py arms the next cue the
        moment the current one applies, so `session.due()` is never None
        during a show and the worker sits inside _fire_at()'s wait for
        the whole stretch between two cues - which is exactly the 11-38 s
        of silence that lost cues on 2026-09-27 (review, same day).
        """
        session = self.remote
        if session is None or self._remote_guard_due is None:
            return
        now = time.monotonic()
        if now < self._remote_guard_due:
            return
        if not self._remote_guard_clear(session, now):
            return
        if not self._remote_guard_said:
            self._remote_guard_said = True
            self.emit("remote guard: stop every "
                      f"{self.remote_guard:g} s while idle")
        self.remote_guard_sent += 1
        took_ms = 0.0
        try:
            took_ms = self._send_timed(bus, stop(0xFF, groups), "stop")
        finally:
            self._sent_broadcast_stop()
        # This heartbeat is the one frame that goes out when NOTHING else
        # would touch the bus, which makes it the one honest sample of
        # the port's own health - and the place a recovery can run
        # without taking the bus from anybody.
        self._maybe_recover(bus, groups, took_ms)

    def _fire_at(self, bus, groups: int, session, cue_id: str, at: float,
                 slot: int, dev_type: int) -> bool:
        """Send the one broadcast "show slot" at monotonic time `at`.

        Sleeps most of the way and polls the last FIRE_SPIN_S, so the
        frame leaves within a millisecond or two of the instant; a time
        already past (a late command) fires at once and the lateness is
        what the session reports. Nothing is written here - the picture
        is already burned into `slot`.

        A show's cues can now be far apart (nothing to write ahead of
        time any more, so ui/showplay.py arms the next one the moment
        the current one applies, however far off its own instant is) -
        this call is where the runner would otherwise sit for that whole
        stretch, so it keeps reprobing (cheap: _reprobe() only touches
        the bus once every reprobe_interval) rather than only doing so
        between cues.

        It returns a second or so AFTER the broadcast, not at it: the
        landing check (_verify_landing()) holds the port for that long
        to find out whether the frame was taken. Nothing else may use
        the bus meanwhile - no reprobe in particular, which is why that
        wait is _verify_wait() and not the loop above.
        """
        self._firing = True
        try:
            while True:
                remaining = at - time.monotonic()
                if remaining <= 0:
                    break
                if (self._stop.is_set()
                        or session.due() != (cue_id, at, slot, dev_type)):
                    return False                # stopped, cancelled or moved
                if remaining > FIRE_SPIN_S:
                    # A re-enumeration mid-wait: found in a fifth of a
                    # second and put right in about one, rather than at
                    # this cue's own write - which is where the 2026-09-27
                    # lost cues were found, far too late to save them.
                    # Never in the final spin: nothing may stand between
                    # the last 20 ms and the frame.
                    self._port_watch(bus, groups)
                    if remaining <= self.precheck_s:
                        # ...and the one frame that says whether THIS cue's
                        # broadcast is going to be taken at all. `at` is
                        # handed over as the deadline: no step of it is
                        # begun that would run past the trigger.
                        self._precheck(bus, groups, cue_id, deadline=at)
                    self._reprobe(bus, groups)
                    # This wait, not the idle loop, is where a running
                    # show spends the stretch between two cues - so the
                    # idle autoplay guard has to go out from here too.
                    # It stands aside for the last REMOTE_GUARD_HOLD_S
                    # before `at`, which keeps it well clear of the spin
                    # below (_remote_guard_clear()).
                    self._remote_guard_tick(bus, groups)
                    self._stop.wait(min(remaining - FIRE_SPIN_S, 0.05))
                else:
                    time.sleep(0.0005)
            frame = show_single(0xFF, slot, groups, dev_type=dev_type)
            took_ms = self._send_timed(bus, frame, f"show slot {slot}")
            self._resend_on_stall(bus, frame, cue_id, took_ms)
            sent_at = self._last_show_at = time.monotonic()
            # What the landing check needs to know about THIS cue, read
            # before the session is told it fired: ui/showplay.py arms
            # the next cue as soon as it sees FIRED, and from then on the
            # session carries that cue's span and refresh (review round
            # 3 - the repair budget was being sized from the wrong cue).
            fired = self._cue_snapshot(session)
            # No heartbeat until THIS cue's picture is certainly done.
            # Read here, while the session still holds this cue: a
            # moment later it carries the NEXT one's span, and a shorter
            # one would shrink this sweep out from under the rule
            # (review, 2026-09-27). Not from the snapshot above - that
            # fills defaults in for the repair budget, and a cue that
            # said nothing must keep the FLAT guard, not a 7 s refresh.
            self._guard_hold_s = self._guard_for(session)
            self._guard_floor = sent_at + self._guard_hold_s
            # The time the PC is told is this FIRST send, whatever the
            # landing check does afterwards: "how late was the cue" is
            # about when the picture was asked for, and a re-send is a
            # repair of that same cue, not a later one.
            session.fired(cue_id, sent_at)
        finally:
            # Released only once the session has been told: until then
            # due() still names this cue, and a re-entrant call would
            # send the broadcast a second time - which costs the boards
            # a whole extra repaint (see SHOW_REPEATS).
            self._firing = False
        self.cycle += 1
        self.emit(f"cue {cue_id} fired slot {slot} "
                  f"{(sent_at - at) * 1000:+.0f} ms")
        self._verify_landing(bus, groups, session, cue_id, slot, dev_type,
                             sent_at, fired)
        return True

    def _run_remote(self, session) -> None:
        guard_due = None
        # One heartbeat clock per worker, armed by the first broadcast
        # 0x17 this worker sends (_setup()'s own first move).
        self._remote_guard_due = None
        self._remote_guard_said = False
        self.remote_guard_sent = 0
        self._guard_floor = None
        self.bus_stall = None
        self.bus_recovery = self.resend = self.precheck = None
        self._prechecked = None
        self._next_port_poll = 0.0
        self._stall_streak = self._recover_tries = 0
        self._recover_next = 0.0
        self._recover_said_enough = False
        self._setup_owed = False
        while not self._stop.is_set():
            port = self.port or find_port()
            if not port:
                if self.error != "no serial port":
                    self.emit("no serial port, waiting")
                self.error = "no serial port"
                session.failed_with("no serial port")
                if not self._sleep(self.port_wait):
                    break
                continue
            try:
                with self._open_bus(port) as bus:
                    self.emit(f"port {port}")
                    needs_setup = True
                    groups = self._take_groups()
                    while not self._stop.is_set():
                        if self._setup_owed:
                            # A recovery reopened the port. The boards
                            # know nothing of the new one, so the sweep
                            # and the slot configs are owed - here, where
                            # they yield to every cue, and not on the
                            # recovery's own clock.
                            self._setup_owed = False
                            needs_setup = True
                        # `groups` is taken again ONLY where the list is
                        # replaced (below), never per pass: an exploring
                        # setup trims the list as it goes, and following
                        # that mid-session would have the boards it
                        # already slot_configured with one group count
                        # served frames with another - and a board that
                        # joined later configured differently from its
                        # neighbours (review, 2026-09-27).
                        #
                        # The show file's own garment list, handed over
                        # with no job to write (ui/remote.py's
                        # set_boards(), called by ui/showplay.py). This
                        # is the only thing that carries the list for a
                        # show RESUMED after a restart: restore() never
                        # re-burns, so no job ever comes. Applied here,
                        # before the setup sweep and before any overdue
                        # cue's fire, so both already follow it.
                        listed = session.take_boards()
                        if listed:
                            if self._apply_job_boards(listed, source="show"):
                                needs_setup = True
                            groups = self._take_groups()
                        # A job's own board list is applied BEFORE setup
                        # runs, so the very first prepare() (still holding
                        # the runner's construction-time board list) does
                        # not pay for a setup sweep of the wrong list and
                        # then a second one right after adjusting it.
                        job = session.take_job()
                        if job is not None:
                            # The save's own stops cover it - both the
                            # guard this loop holds and one a fire from
                            # inside the probing left owed.
                            guard_due = self._guard_owed = None
                            wanted = sorted(job["boards"])
                            if self._apply_job_boards(wanted):
                                needs_setup = True
                            groups = self._take_groups()
                            if needs_setup:
                                if not self._setup(bus, groups):
                                    session.failed_with(self.error
                                                        or "setup failed")
                                    break
                                needs_setup = False
                                self.error = None
                            began = time.monotonic()
                            saved, failed = self._save_cue(bus, groups, job)
                            took = time.monotonic() - began
                            session.prepared(job["cue_id"], saved, failed, took)
                            self.emit(f"cue {job['cue_id']} saved "
                                      f"{len(saved)}/{len(wanted)} in {took:.1f} s")
                            continue

                        # A burn's own board list (every board any of its
                        # cues names) is applied the same way, and for the
                        # same reason: the runner may still be holding its
                        # construction-time list (or a previous show's).
                        burn_job = session.take_burn_job()
                        if burn_job is not None:
                            # The clock the operator is watching starts
                            # HERE, not at the first write: the setup
                            # sweep below is what a wall with absent
                            # boards spends most of an Upload on.
                            burn_began = time.monotonic()
                            guard_due = self._guard_owed = None
                            wanted = sorted({b for cue in burn_job["cues"]
                                            for b in cue["boards"]})
                            if wanted:
                                # The show file's own list wins over the
                                # burn's union of cue boards: the union
                                # is a subset of it by construction, and
                                # the subset would quietly drop boards
                                # the garment does have (review,
                                # 2026-09-27). A cue naming a board
                                # OUTSIDE it is a show file at odds with
                                # itself - the picture still has to go
                                # somewhere, so the list widens, says so,
                                # and stops calling itself the show's.
                                from_show = self.boards_source == "show"
                                outside = [b for b in wanted
                                           if b not in self.boards]
                                if from_show and outside:
                                    self.emit("the burn names board "
                                              f"{self._fmt_boards(outside)} "
                                              "outside the show's own list")
                                    wanted = sorted(set(self.boards) | set(wanted))
                                if outside or not from_show:
                                    if self._apply_job_boards(wanted):
                                        needs_setup = True
                                    groups = self._take_groups()
                            if needs_setup:
                                if not self._setup(bus, groups):
                                    if self.error != "no boards answering":
                                        # The setup itself was cut short
                                        # (a stop, a dead port): nothing
                                        # was tried and nothing says what
                                        # would have been written, so the
                                        # burn is CANCELLED with the
                                        # reason - never a "failed" over
                                        # pairs nobody reached, which the
                                        # PC would offer to force past
                                        # (review round 2, 2026-09-25).
                                        session.burn_cancelled(
                                            burn_job["epoch"],
                                            self.error or "setup failed")
                                        session.failed_with(self.error
                                                            or "setup failed")
                                        break
                                    # Every board of this show was
                                    # probed and none answered - a
                                    # garment whose feed is off, or one
                                    # that is not plugged in. That is a
                                    # KNOWN GAP, not an unknown one: the
                                    # burn below walks its whole list and
                                    # puts every pair down as absent, so
                                    # the operator can start the other
                                    # nine units past it (the conductor
                                    # asks first). needs_setup stays on,
                                    # so a feed switched on later is
                                    # found by the next pass.
                                    self.emit("no boards answering: the "
                                              "burn writes nothing")
                                else:
                                    needs_setup = False
                                    self.error = None
                            self._run_burn(bus, groups, session, burn_job,
                                           began=burn_began)
                            continue

                        # The show is over and its pictures are to come
                        # back out of the slots (ui/showplay.py's
                        # clear_after_show). Taken only once no guard STOP
                        # is owed and the last cue's guard floor has
                        # passed: a 0x14 must never land inside a repaint.
                        # Until then the job simply stays queued and this
                        # loop goes on doing its guard work - the wait
                        # below is shortened to that floor, so the clear
                        # starts at it rather than a poll later.
                        clear_job = (session.take_clear_job()
                                     if self._clear_ready(guard_due) else None)
                        if clear_job is not None:
                            clear_began = time.monotonic()
                            if needs_setup:
                                if not self._setup(bus, groups):
                                    if self.error != "no boards answering":
                                        session.clear_cancelled(
                                            clear_job["epoch"],
                                            self.error or "setup failed")
                                        session.failed_with(self.error
                                                            or "setup failed")
                                        break
                                    # Every board probed, none answered:
                                    # a garment with no power cannot
                                    # autoplay either, so this is worth
                                    # saying and not worth failing over -
                                    # the walk below puts every pair down
                                    # as absent and the PC shows the list.
                                    self.emit("no boards answering: the "
                                              "clear deletes nothing")
                                else:
                                    needs_setup = False
                                    self.error = None
                            self._run_clear(bus, groups, session, clear_job,
                                            began=clear_began)
                            continue

                        # POST /bus/recover: the operator's own button,
                        # run HERE because the worker thread owns the
                        # port - the HTTP thread only waits for the
                        # answer. The endpoint refuses it while a run is
                        # in flight or a cue is near (ui/remote.py's
                        # recover_bus()), so by the time it is queued the
                        # bus is nobody else's.
                        recover_job = session.take_recover_job()
                        if recover_job is not None:
                            session.recovered(recover_job,
                                              self._recover_bus(bus, groups))
                            continue
                        if (needs_setup and session.phase == READY
                                and session.due() is None):
                            # ui/showplay.py's _send() arms the cue and
                            # sets its time in two steps (arm() may have
                            # to take the port first - which is what
                            # started this worker), so a moment's
                            # patience here is what lets the branch
                            # below see the time at all.
                            session.wait(ARM_GRACE_S)
                        late = session.due()
                        if (needs_setup and late is not None
                                and late[1] <= time.monotonic()):
                            # A unit that restarted in the middle of the
                            # show: its cue's trigger is already overdue,
                            # and a broadcast "show slot N" needs no
                            # board probed or configured (the picture is
                            # burned into the slot and the 0x1B with it).
                            # So it goes out BEFORE the probing sweep -
                            # 16 s of it with 6 absent boards on the real
                            # unit (2026-09-25), which on stage is the
                            # garment sitting on the wrong picture.
                            if self._fire_at(bus, groups, session, *late):
                                guard_due = self._guard_after_fire(session)
                        if needs_setup:
                            # The one broadcast 0x17 for this port session
                            # (docs/MERIS_REPLY_3SLOT.pdf: one is enough
                            # after power-up) - _setup()'s own first move.
                            # Only reached with no job or burn pending: a
                            # due fire needs it just as much, but there is
                            # no board list of its own to adjust first.
                            if not self._setup(bus, groups):
                                session.failed_with(self.error
                                                    or "setup failed")
                                break           # reopen the bus and retry
                            needs_setup = False
                            self.error = None

                        due = session.due()
                        if due is not None:
                            if self._fire_at(bus, groups, session, *due):
                                guard_due = self._guard_after_fire(session)
                            continue
                        now = time.monotonic()
                        if self._guard_owed is not None:
                            # The LATEST fire's guard, not the earliest.
                            # Both are absolute deadlines, and an older
                            # fire's deadline is meaningless once a newer
                            # cue has gone out: a unit restarting
                            # mid-show fires its overdue cue before the
                            # probing sweep (guard at t+15), the next cue
                            # comes due inside that sweep and
                            # _fire_before_probing() sends it (guard at
                            # t+26), and taking the smaller of the two
                            # put a broadcast 0x17 five seconds into the
                            # second cue's own sweep (review, 2026-09-26).
                            guard_due = (self._guard_owed if guard_due is None
                                         else max(guard_due, self._guard_owed))
                            self._guard_owed = None
                        if guard_due is not None and now >= guard_due:
                            # As after every demo cycle: a shown slot runs
                            # on into the factory autoplay unless stopped.
                            guard_due = None
                            self._send_timed(bus, stop(0xFF, groups), "stop")
                            self._sent_broadcast_stop()
                        else:
                            # The heartbeat (REMOTE_GUARD_S), for a
                            # worker with no cue in hand at all: before
                            # START, after the last cue, on HOLD.
                            self._remote_guard_tick(bus, groups)
                        if self._reprobe(bus, groups):
                            pass                # joined boards take the next cue
                        # A re-enumeration while the unit is idle - which is
                        # where 4 of 4 LOOK28 runs went bad, 10-30 s after
                        # the last cue. Seen at once and put right in about
                        # a second, instead of at the next write.
                        self._port_watch(bus, groups)
                        wait = self.link_poll
                        if self.port_poll > 0:
                            # ...which means the idle wait cannot be longer
                            # than one poll of it.
                            wait = min(wait, self.port_poll)
                        if guard_due is not None:
                            wait = min(wait, max(0.0, guard_due - now))
                        if (session.clear_pending()
                                and self._guard_floor is not None
                                and self._guard_floor > now):
                            # A clear held back only by the guard floor
                            # starts AT it, not at the next poll: the
                            # operator is standing over the garment
                            # waiting to unplug it.
                            wait = min(wait, self._guard_floor - now)
                        if (self._remote_guard_due is not None
                                and self._remote_guard_due > now):
                            # Only while it is still ahead: a heartbeat
                            # held back by the rules above would
                            # otherwise shrink this wait to zero and
                            # spin the loop until the block cleared.
                            wait = min(wait,
                                       max(0.0, self._remote_guard_due - now))
                        session.wait(wait)
            except Exception as exc:        # unplugged, permissions, ...
                self.error = str(exc)
                self.emit(f"ERROR bus {exc}")
                session.failed_with(str(exc))
            if not self._stop.is_set() and not self._sleep(self.reopen_delay):
                break
        pending = session.take_burn_job()
        if pending is not None:
            # Queued after the last take_burn_job() and never started:
            # nothing of it is written, nothing was even tried, and
            # nobody else would ever say so - "cancelled", not a
            # "failed" the PC would offer to force past (review round 2).
            session.burn_cancelled(pending["epoch"],
                                   "the worker was stopped first")
            self.emit("burn never started: the worker was stopped first")
        waiting = session.take_clear_job()
        if waiting is not None:
            # Queued and never started, so not one slot was deleted: the
            # pictures are exactly as they were and cancel_clear()'s own
            # "nothing happened" reading is the honest one. Said out
            # loud, because the operator is waiting for "pictures
            # cleared" before unplugging the garment.
            session.clear_cancelled(waiting["epoch"],
                                    "the worker was stopped first")
            self.emit("clear never started: the worker was stopped first")
        self.emit("stopped")

    # ---- worker ----

    def _run(self) -> None:
        rng = random.Random(self._seed)
        groups = self._take_groups()
        while not self._stop.is_set():
            port = self.port or find_port()
            if not port:
                # Say it once. Standby waits for the port from boot, so a
                # host with no panels attached would otherwise write this
                # line every port_wait seconds for as long as it is up.
                if self.error != "no serial port":
                    self.emit("no serial port, waiting")
                self.error = "no serial port"
                if not self._sleep(self.port_wait):
                    break
                continue
            try:
                with self._open_bus(port) as bus:
                    self.emit(f"port {port}")
                    consecutive = 0
                    needs_setup = True
                    while not self._stop.is_set():
                        if not self._hold_while_paused():
                            break
                        # `groups` is the one this worker started with and
                        # stays that way: the exploring setup trims the
                        # list while it probes, and a group count that
                        # followed it would leave the boards configured
                        # in the first sweep disagreeing with every frame
                        # after it (review, 2026-09-27). A list that is
                        # REPLACED - a show's - is a different matter and
                        # is taken again where it happens (_run_remote).
                        if needs_setup and not self._setup(bus, groups):
                            consecutive += 1
                        elif self._cycle(bus, groups, rng):
                            consecutive = 0
                            needs_setup = False
                            self.error = None
                            if self._once:
                                self._settle(bus, groups)
                                self.standby_ready = True
                                self.emit("standby ready")
                                if not self._watch_link(bus, groups, port):
                                    return          # asked to stop
                                break               # link changed: repaint
                            if not self._wait_next(bus, groups):
                                break
                            continue
                        else:
                            consecutive += 1
                        # This cycle is lost; the panels keep the previous
                        # image. Re-initialise next time in case a board
                        # rebooted while it was unreachable.
                        self.failures += 1
                        needs_setup = True
                        if consecutive >= self.reopen_after_failures:
                            self.emit("reopening the bus")
                            break
                        if not self._sleep(self.reopen_delay):
                            break
            except Exception as exc:        # unplugged, permissions, ...
                self.error = str(exc)
                self.emit(f"ERROR bus {exc}")
            if not self._stop.is_set() and not self._sleep(self.reopen_delay):
                break
        self.emit("stopped")
