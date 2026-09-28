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

import errno
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
from epaper.transport import ACK_TIMEOUT_S, USBRESET_TIMEOUT_S, Bus, find_port
from epaper.transport import usb_reset as usb_reset_default
from epaper.transport import usb_reset_available as usb_reset_check_default

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
# LOOK28 (radxa-07, AZ271SD1307, 22 boards), 2026-09-28. The bus goes bad
# 10-30 s after the last cue's repaint, in one of two ways:
#
#  * the kernel says `usb usb1-port1: disabled by hub (EMI?), re-enabling`
#    and the master re-enumerates (ttyACM0 -> ttyACM1 in 0.44 s). The port
#    watcher below finds that at once (PORT_POLL_S); the only trouble on the
#    unit was that udev had not yet given the new node its group, so the
#    first open answered EACCES - hence the open retries (OPEN_RETRY_S).
#  * the master stays enumerated and DEGRADES: every write blocks and no
#    frame is executed. Measured on the unit the same day: the first
#    heartbeat after the last cue's floor blocked 393 ms and every one
#    after it 358 ms; a preset written 8 s after a "recovery" blocked 272 ms
#    and no panel changed; a /standby worker (port reopen + STOP + probe
#    sweep) found "no boards answering" for 100 s - a degraded master
#    relays nothing and does not answer 0x02 itself.
#
# What does NOT cure the second: padding, a port reopen, a STOP, a probe
# sweep - all tried on the unit. What DOES: a USB device reset. `sudo -n
# usbreset 0483:5740` took 0.3 s, the node came back, and 8 s later the unit
# said `panels online: 22/22` with the burn intact. The "a restart cured it"
# of the day before were Radxa REBOOTS - a USB power cycle, the same cure.
# So the cure is transport.usb_reset(), and nothing else is offered as one.
#
# And how a false "recovered" came about, because it must not come about
# again: the blocking grows with the IDLE GAP before a write - 61 ms at
# 0.5 s after the previous write, 272 ms at 8 s, 358 ms at 15 s. The old
# padding path measured its STOP half a second after the padding and read
# 61 ms: "bus recovered by padding (512 -> 61 ms)", three times, each one
# false. So a recovery is PROVEN, never just timed (_prove_recovered()):
# the master must ACK a unicast STOP - a degraded master answers nothing -
# AND a broadcast STOP sent after PROOF_GAP_S of silence must take under
# RECOVERED_MS.
#
# Why a unicast STOP and not 0x02 (radxa-07, 2026-09-28 12:40, main
# 621669d): this firmware does not answer a 0x02 sent over the bus AT ALL,
# healthy or not - the pre-cue check read "master silent (1 ms)" on a
# healthy master and reset its USB 8 s before a cue, the idle proof could
# never succeed, and Recover bus could never say "already clear". The
# unicast STOP with its ACK is what the probe sweep itself asks every board
# (_probe()), and what a healthy master demonstrably answers.
#
# A write this fast is a healthy port: on the real unit a clean broadcast
# is +1..2 ms, so 100 ms is far above the noise and far below the 358 ms
# of the degraded state.
RECOVERED_MS = 100.0
# ...and how slow a STOP has to be to count as a stall worth recovering
# from. 200 ms, not STALL_LOG_MS: a board busy repainting blocks 40-400 ms
# and that is NORMAL (the 2026-09-27 rehearsals) - it is the never-ending
# 225-390 ms of the degraded state this must recognise, and two heartbeats
# in a row is what tells them apart, since a repaint ends and this does not.
STALL_RECOVER_MS = 200.0
# How much clear air the automatic recovery wants. It resets the USB device
# and may hold the port for seconds, so it never runs with a cue anywhere
# near: a cue nearer than this and it stands aside, for the next heartbeat.
RECOVER_QUIET_S = 60.0
RECOVER_BACKOFF_S = 60.0   # between attempts
# ...and then it stops and says so. A bus that three USB resets did not
# bring back is a unit to reboot or a cable to re-plug, not a fourth try.
RECOVER_MAX_ATTEMPTS = 3
# The proof's idle gap. Three seconds is well past the half second at which
# a degraded master still read 61 ms, and short enough to be paid on every
# recovery: a degraded master blocks ~270 ms by then and a healthy one 1-2.
PROOF_GAP_S = 3.0
# Asking the master (a unicast STOP, _master_answers()): the probe's own read
# window per try (transport.ACK_TIMEOUT_S, as _probe() waits), and two tries
# on the idle path - a healthy master ACKs in milliseconds, and a frame the
# degraded state ate gets its second chance before the verdict is "silent".
MASTER_ASK_S = ACK_TIMEOUT_S
MASTER_ASK_TRIES = 2
# Who is asked (_health_candidates()): the board that last answered, the
# lowest live board, the highest - at most this many, each once.
HEALTH_CANDIDATES_MAX = 3
# The pre-cue check's SECOND ask, after one miss behind a STOP under
# STALL_LOG_MS: a short read window (a board that answers does so in ms),
# and what it may cost at most - its write, behind a STOP that took under
# STALL_LOG_MS, allowed twice that; the window; and the transport's read
# overshoot (transport.Bus.recv() polls ser.read() with a 0.05 s timeout,
# so a silence is known up to READ_OVERSHOOT_S after the window closes).
# It is only asked if the USB reset step still fits behind it; otherwise
# the one miss gets the reset. 0.35 s (final gate on 05cc86a, LOW-1: at
# 0.3 a second ask that just fit could leave the reset without time).
READ_OVERSHOOT_S = 0.05
HEALTH_SECOND_ASK_S = 0.2
HEALTH_SECOND_ASK_COST_S = (2 * STALL_LOG_MS / 1000.0 + HEALTH_SECOND_ASK_S
                            + READ_OVERSHOOT_S)
# After a USB reset the node goes away and comes back, possibly under a new
# name - 0.44 s on the unit. It is waited for this long, polled every
# RETRY_POLL_S; and an open that answers EACCES (udev has not set the
# node's group yet) or ENOENT is retried for OPEN_RETRY_S.
USB_NODE_WAIT_S = 3.0
OPEN_RETRY_S = 2.0
# 50 ms, not 100: the node was back 0.44 s after the reset on the unit, and a
# 100 ms poll noticed it at 0.5 - time the fire-time re-send cannot spare.
RETRY_POLL_S = 0.05
# The fire-time re-send (--resend-on-stall; OFF by default - PM, after the
# review of 349dcdd). A CUE's own show frame blocking at least this long is
# one of TWO states, and they need opposite handling:
#
#  * DEGRADED (radxa-07, 2026-09-28 Run 2): the frame is accepted and never
#    executed, and the master answers nothing - the cure is a USB reset and
#    the same frame once more;
#  * SLOW but working (LOOK23, 2026-09-28 17:29): every picture appeared,
#    each ~0.36 s late - a re-send there paints the slot twice.
#
# So the stall alone decides nothing - and at fire time there is NO second
# signal left to ask (2026-09-28 12:40, radxa-07): the 0x02 it used is never
# answered on this firmware, and the unicast STOP that replaced it everywhere
# else cannot be sent here. Right after a show frame a working master is
# repainting and deaf (silence would read as "degraded" - the double paint
# again), and a STOP to a board whose sweep delay has not run out yet
# cancels its picture. So _resend_on_stall() says the stall and re-sends
# nothing, even with the flag on; the pre-cue check and the idle recovery are
# where the cure runs.
#
# The budget the re-send kept, measured on the fake with the unit's own
# timings: the re-sent picture went 1.80 s late (1.88 / 1.92 s with EACCES).
RESEND_STALL_MS = STALL_RECOVER_MS
FIRE_RESEND_BUDGET_S = 2.5
# ---- the pre-cue check: the next cue on time ----
# The idle recovery is for the stretch after the last cue, where seconds
# cost nothing. Before a cue it runs on a clock that fits inside the gap
# before the trigger.
#
# When: PRECHECK_S before every armed cue, one timed broadcast STOP and one
# unicast STOP to the master, which ACKs it (both; the broadcast alone could
# read fast if a heartbeat had just gone out - see PROOF_GAP_S). A healthy
# port answers both in milliseconds and that is the whole check - the whole
# healthy wire at ~T-8.5 is those two frames. The USB reset follows ONLY a
# broadcast STOP that blocked STALL_RECOVER_MS or more, whose write failed,
# or a master that did not ACK.
#
# Two rules bound it, and both are the rules the heartbeat already lives by
# (review of be1c0b5, F1/F3):
#
# * NOTHING in the last REMOTE_GUARD_HOLD_S before a trigger but the trigger
#   itself. The check is not even begun with less than that plus one
#   degraded write, DEGRADED_WRITE_S, left (5.4 s), and each step after the
#   first has to be done before T - REMOTE_GUARD_HOLD_S or it is not begun.
#   A cue armed with less lead than that simply gets no check.
# * not inside the previous cue's repaint - `_guard_floor`, the same floor
#   the heartbeat and the guard STOP use. So in a burst of cues closer
#   together than that floor the check is skipped, which is intended.
#
# The budget, stated at its worst (final gate on 05cc86a, LOW-3). The check
# begins at T-8.5 plus the wait's tick lag (<= 50 ms): T-8.45. Every ask's
# read overshoots its window by up to READ_OVERSHOOT_S (transport.Bus.recv()
# polls with a 0.05 s timeout). The USB reset step - the reset 0.3, the
# node back ~0.45, the open 0.3, the STOP straight after it (review M4) 0.4,
# and the question that proves it (0.4 + 0.5 + 0.05) - is
# USB_RESET_BUDGET_S = 2.4 s, and it is begun only if it ends by T-5.0.
#
#  * STOP stalled (>= 0.2 s, 0.4 at worst): no question, straight to the
#    reset - begun by T-8.05, done by T-5.65.
#  * STOP under 0.2 s but not under 50 ms: one question, 0.4 + 0.5 + 0.05 =
#    0.95 at worst, ending by T-7.30 - and then the reset does NOT fit at
#    every limit at once (T-4.90): "no time for a usb reset". On the
#    degraded master the unit showed (61 ms writes behind a write) the
#    question ends by T-7.78 and the reset by T-5.38.
#  * STOP under 50 ms: question 1, its write behind a fast STOP allowed 0.1,
#    0.65 at worst, ends by T-7.75. Question 2 (HEALTH_SECOND_ASK_COST_S
#    0.35) only if the reset still fits behind it - so the reset is begun by
#    T-7.40 and done by T-5.0 at every limit; if question 2 does not fit,
#    the one miss gets the reset at once (done by T-5.35).
#
# Each step is asked again against the clock before it is begun, and the
# reset itself is bounded by T-5.0 whatever sudo does (review M2). With the
# 3 s proof gap it would not fit, so the check proves by one board's ACK
# alone ("proof-lite"); the idle recovery proves with both.
#
# WHICH CUES GET ONE, in practice: the previous cue's guard floor has to be
# past at T-8.5, and at the unit's --guard-delay 30 that floor is 30-38 s
# after the fire for this show's refresh and span. So only a cue 38.5-46.5 s
# or more after the one before it is checked - 14 of the 36 cue-to-cue gaps
# of showdata/show.json, and NONE of the finale burst (11-28 s gaps).
PRECHECK_S = 8.5
# One degraded write, for the per-frame question (_frame_refusal()): may
# THIS frame still go before the hold?
DEGRADED_WRITE_S = 0.4
MASTER_ASK_COST_S = DEGRADED_WRITE_S + MASTER_ASK_S
USB_RESET_BUDGET_S = 2.4
# A probe sweep that finds no boards answering on an open port resets the
# master's USB and sweeps again (_setup_usb_reset()) - at most this many
# times per worker, recover_backoff apart, and then the old reopen loop.
SETUP_RESETS_MAX = 3
# How long the probe sweep a reset or a reopen owes waits after it before it
# may start at all (re-review of e139a33). A recovery is what the operator
# presses right before ② Show preset, and the preset follows within a
# second or two - a sweep begun at once was sweeping when the preset fired.
OWED_SETTLE_S = 20.0
# The read window the port watcher's reopen gives the master's own config
# frame. A board that is going to answer does so in milliseconds.
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


class _SweepYielded(Exception):
    """An owed probe sweep standing down for a cue that fired inside it
    (DemoRunner._setup()). Private: it never leaves _setup()."""


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
                 port_watch: bool = True,
                 auto_recover: bool = True,
                 port_poll: float = PORT_POLL_S,
                 port_back_wait: float = PORT_BACK_WAIT_S,
                 recover_quiet: float = RECOVER_QUIET_S,
                 recover_backoff: float = RECOVER_BACKOFF_S,
                 recover_attempts: int = RECOVER_MAX_ATTEMPTS,
                 owed_settle: float = OWED_SETTLE_S,
                 proof_gap: float = PROOF_GAP_S,
                 usb_reset=None,
                 usb_reset_check=None,
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
        # The fire-time re-send after a USB reset (RESEND_STALL_MS). OFF by
        # default (PM, after the review of 349dcdd); ui/main.py's
        # --resend-on-stall turns it on. Off, a stalled cue is timed, logged
        # and counted, and not re-sent.
        self.resend_on_stall = bool(resend_on_stall)
        # The three kill switches (ui/main.py's --precheck SECONDS,
        # --no-port-watch, --no-auto-recover), each reported in /status.
        # The pre-cue health check (PRECHECK_S): 0 switches it off.
        self.precheck_s = max(0.0, float(precheck or 0.0))
        # Watching for a USB re-enumeration (PORT_POLL_S), in the idle loop
        # and in the wait before a cue alike.
        self.port_watch = bool(port_watch)
        # The idle recovery on two stalled heartbeats (_maybe_recover()).
        # POST /bus/recover - the operator's own button - is not affected.
        self.auto_recover = bool(auto_recover)
        self.port_poll = port_poll
        self.port_back_wait = port_back_wait
        self.recover_quiet = recover_quiet
        self.recover_backoff = recover_backoff
        self.recover_attempts_max = recover_attempts
        self.owed_settle = owed_settle     # OWED_SETTLE_S; a knob for tests
        self.proof_gap = proof_gap         # PROOF_GAP_S; a knob for tests
        # The one cure (transport.usb_reset); injectable, so the tests never
        # reach a real USB device - they run on the Radxas too.
        self._usb_reset = usb_reset or usb_reset_default
        # Whether a reset is possible on this unit AT ALL (review L2):
        # asked once, when a worker first has the port, and reported in
        # /status as usb_reset_ok - the tile marks a unit where it is not.
        # None until asked.
        self._usb_reset_check = usb_reset_check or usb_reset_check_default
        self.usb_reset_ok: "bool | None" = None
        self.usb_reset_why: "str | None" = None
        # Set when a port could not be reopened after an abandoned reset:
        # the watcher keeps trying on its own poll (_port_watch()).
        self._needs_reopen = False
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
        # ("usb_reset" | None), "before_ms", "after_ms", "count"},
        # reported in /status as bus_recovery. None until one has been run
        # in this worker. `by` None with recovered true is "there was
        # nothing wrong" - what POST /bus/recover answers on a healthy unit.
        self.bus_recovery: "dict | None" = None
        # The last fire-time re-send (resend_on_stall): {"cue", "at",
        # "before_ms", "after_ms", "late_s", "by", "count"}, /status's
        # `resend`.
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
        self._stop_failed = None           # the last timed STOP's raise
        self._sweep_pending: "list[int]" = []
        self._health_sticky: "int | None" = None   # last board to answer
        self._health_trail: "list[tuple[int, bool]]" = []
        self._reset_setup_resets()
        # A reopen invalidates what the boards were told: the caches go,
        # and the full sweep is owed to _run_remote()'s own setup, where
        # it yields to every cue (_fire_before_probing()) instead of
        # running on the recovery's clock.
        self._setup_owed = False
        self._owed_at = 0.0                # when the reopen that owed it ran
        # An owed sweep that is running may be stood down by a cue firing
        # from inside it (_fire_before_probing()) - see _setup().
        self._setup_yields = False
        self._fired_in_setup = False
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
            # An owed sweep stands down the moment that sent a cue.
            self._yield_to_a_fired_cue()
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

        An OWED sweep (one a fast reopen left behind, `_setup_yields`) stands
        down the moment a cue fires from inside it (re-review of e139a33):
        probing on would put a unicast 0x17 into every board through that
        cue's repaint. It stops before the next probe, puts back what it had
        reset (the board list an exploring sweep starts over from, the slot
        and table caches) so the unit reads exactly as it did before, and
        is owed again - from now, so OWED_SETTLE_S and the usual gate (that
        cue's guard floor, a minute of quiet) both apply before it retries.
        A start-up sweep never yields: there is nothing to fall back on.
        """
        self._probing = True
        self._fired_in_setup = False
        saved = (list(self.boards), set(self._cfg_done),
                 dict(self._delays_sent))
        try:
            return self._setup_locked(bus, groups)
        except _SweepYielded:
            self.boards, self._cfg_done, self._delays_sent = saved
            self._setup_owed = True
            self._owed_at = time.monotonic()
            self.emit("probe sweep stood aside for the cue, still owed")
            return True
        finally:
            self._probing = False
            self._setup_yields = False

    def _yield_to_a_fired_cue(self) -> None:
        """Inside an owed sweep: stop here if a cue has just fired."""
        if self._setup_yields and self._fired_in_setup:
            raise _SweepYielded()

    def _setup_locked(self, bus, groups: int) -> bool:
        """_setup()'s body, with `_probing` held: no automatic bus
        recovery may run inside a sweep that is already asking every
        board (and would reopen the port under it).

        A sweep that finds no boards answering on an open port is followed
        by a USB reset of the master and one more sweep (_setup_usb_reset()
        says when): `no boards answering → usb reset → panels online: 22/22`
        or `… → usb reset → still no boards`, and then the verdict as ever.
        """
        absent_before = set(self.absent)
        if not self._sweep_boards(bus, groups):
            return False
        if self._sweep_found_nothing():
            reset = self._setup_usb_reset(bus)
            if reset is not None:
                done, how = reset
                if not done:
                    self.emit(f"no boards answering → usb reset → failed "
                              f"({how})")
                else:
                    # The same sweep again, from what was known before the
                    # first one (not three passes cut to one because the
                    # dead master made every board look absent).
                    with self._lock:
                        self.absent = {b for b in absent_before
                                       if b in self.boards}
                    if not self._sweep_boards(bus, groups):
                        return False
                    self._setup_owed = False    # this is the sweep it owed
                    if self._sweep_found_nothing():
                        self.emit("no boards answering → usb reset → still "
                                  "no boards")
                    else:
                        self.emit(f"no boards answering → usb reset → panels "
                                  f"online: {len(self.live)}/{self.expected}")
                        return self._sweep_verdict(said_online=True)
        return self._sweep_verdict()

    def _sweep_boards(self, bus, groups: int) -> bool:
        """One probe sweep: the opening broadcast STOP, then every board on
        the list PROBE_SWEEPS times. Sets live / absent; False only if it
        was cut short (a stop)."""
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
                self._yield_to_a_fired_cue()
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
        self._sweep_pending = pending
        return True

    def _sweep_found_nothing(self) -> bool:
        """The sweep's "no boards answering": not one board answered. Only
        that (review of 9a8c045, M1): "board 1 listed but silent" is also a
        garment whose board 1 is absent or at another DIP address, and
        reading it as a dead master reset a healthy unit's USB three times
        per worker. A degraded master relays nothing, so it empties the
        whole sweep anyway."""
        return not self.live

    def _sweep_verdict(self, said_online: bool = False) -> bool:
        if not self.live:
            self.error = "no boards answering"
            self.emit("ERROR no boards answering")
            return False
        if self._sweep_pending:
            self.emit(f"board {self._fmt_boards(self._sweep_pending)} "
                      f"absent, skipping")
        if not said_online:
            self.emit(f"panels online: {len(self.live)}/{self.expected}")
        return True

    def _reset_setup_resets(self) -> None:
        """A new worker gets SETUP_RESETS_MAX setup-path resets again, and
        starts its health questions from the sweep's list (no sticky board
        carried over from another worker)."""
        self._health_sticky = None
        self._setup_resets = 0
        self._setup_reset_next = 0.0
        self._setup_resets_said = False

    def _setup_usb_reset(self, bus) -> "tuple[bool, str] | None":
        """A sweep found no boards answering on a port that is open: reset
        the master's USB and let the caller sweep again (PM, radxa-07
        2026-09-28 12:45 - after the show the owed sweep met a degraded
        master, found nothing, and the unit went round "no boards answering
        -> reopen the port -> sweep" for as long as nobody ran usbreset by
        hand; one `sudo usbreset` cured it in 5 s). A reopen never cures
        that state, so the old loop alone never ends.

        Returns None when no reset was begun - and then the old behaviour
        follows exactly as before. Begun only:
        * with --auto-recover (the default) and a reset means this unit has
          (`usb_reset_ok`) - --no-auto-recover turns every automatic cure
          off, this one too;
        * at most SETUP_RESETS_MAX times per worker, recover_backoff apart;
        * not inside the last picture's repaint (`_guard_floor` - boards
          repainting are deaf, which is not this state);
        * not if the port is closed, and with a cue armed only if the whole
          reset step fits before its hold (USB_RESET_BUDGET_S); the reset is
          bounded by the hold and the port reopened by the trigger.
        """
        if not self.auto_recover or self.usb_reset_ok is not True:
            return None
        if self._stop.is_set() or self._recovering or self._bus_closed(bus):
            return None
        now = time.monotonic()
        if self._setup_resets >= SETUP_RESETS_MAX:
            if not self._setup_resets_said:
                self._setup_resets_said = True
                self.emit(f"no boards answering: {self._setup_resets} usb "
                          f"resets already, leaving it to the port reopen")
            return None
        if now < self._setup_reset_next:
            return None
        if self._guard_floor is not None and now < self._guard_floor:
            return None
        give_up_at = reopen_by = None
        session = self.remote
        due = session.due() if session is not None else None
        if due is not None:
            give_up_at = due[1] - self.remote_guard_hold
            reopen_by = due[1] - FIRE_SPIN_S
            if now + USB_RESET_BUDGET_S > give_up_at:
                self.emit("no boards answering → no time for a usb reset "
                          "before the cue")
                return None
        self._setup_resets += 1
        self._setup_reset_next = now + self.recover_backoff
        self._recovering = True
        try:
            return self._usb_reset_reopen(bus, give_up_at=give_up_at,
                                          reopen_by=reopen_by)
        except Exception as exc:        # noqa: BLE001 - see _survived()
            self._survived(bus, "usb reset", exc)
            return False, f"raised: {exc or exc.__class__.__name__}"
        finally:
            self._recovering = False

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

    def _send_timed(self, bus, frame, what: str,
                    record_from_ms: float = STALL_LOG_MS) -> float:
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
            if took_ms >= max(STALL_LOG_MS, record_from_ms):
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

    @staticmethod
    def _node_not_ready(exc: BaseException) -> bool:
        """An open refused because the node is not usable YET: EACCES while
        udev has not given a freshly enumerated node its group (radxa-07,
        2026-09-28: "[Errno 13] could not open port /dev/ttyACM1: Permission
        denied", 0.44 s after the re-enumeration), or ENOENT while it is not
        there at all. pyserial raises SerialException with errno set."""
        code = getattr(exc, "errno", None)
        if code in (errno.EACCES, errno.ENOENT):
            return True
        text = str(exc)
        return "Permission denied" in text or "No such file" in text

    def _open_retrying(self, bus, port: str,
                       give_up_at: "float | None" = None) -> str:
        """bus.reopen(port), retried every RETRY_POLL_S for up to
        OPEN_RETRY_S while the node is not ready (_node_not_ready()) - never
        past `give_up_at`. Returns the port opened; any other error, or the
        last refusal, is raised to the caller."""
        limit = time.monotonic() + OPEN_RETRY_S
        if give_up_at is not None:
            limit = min(limit, give_up_at)
        refused = 0
        while True:
            try:
                opened = bus.reopen(port)
            except Exception as exc:    # noqa: BLE001 - classified, re-raised
                if (not self._node_not_ready(exc)
                        or time.monotonic() + RETRY_POLL_S > limit):
                    raise
                refused += 1
                if not self._sleep(RETRY_POLL_S):
                    raise
                continue
            if refused:
                self.emit(f"port {opened or port} opened after {refused} "
                          f"refusal{'' if refused == 1 else 's'} (not ready)")
            return opened

    def _master_answers(self, bus, groups: int,
                        tries: int = MASTER_ASK_TRIES) -> bool:
        """Does the bus answer? The idle form of the health question: each
        try goes to the NEXT distinct candidate (_health_candidates(), up to
        HEALTH_CANDIDATES_MAX boards, MASTER_ASK_S each) and never to the
        same board twice - a board that dropped off mid-show is one silence,
        not the verdict (gate on f9efd43, MED-2). With one candidate only it
        is asked `tries` times, as before. True at the first answer.

        The question is a unicast STOP and its ACK: exactly the probe
        sweep's own first question (_probe(): stop(board), one send). NOT
        0x02: this firmware never answers a 0x02 sent over the bus, healthy
        or not (radxa-07, 2026-09-28 12:40 - see RECOVERED_MS). A degraded
        master relays nothing and answers nothing, so silence from every
        candidate is the degraded state; any one answering is the bus alive.
        A STOP to an idle board changes nothing on the glass; callers keep
        this out of a picture all the same.
        """
        self._health_trail = []
        candidates = self._health_candidates()
        if len(candidates) == 1:
            candidates = candidates * max(1, tries)
        for board in candidates[:max(HEALTH_CANDIDATES_MAX, tries)]:
            if self._ask_board(bus, groups, board, MASTER_ASK_S):
                return True
        return False

    def _ask_board(self, bus, groups: int, board: int, window: float) -> bool:
        """One unicast STOP to `board`, `window` seconds for its answer (an
        ACK, or a BUSY: it is there, merely working). The answer or the
        silence goes on `_health_trail` for the words (_health_words()), and
        a board that answers becomes the one asked first next time
        (`_health_sticky`)."""
        try:
            ack = bus.request(stop(board, groups), retries=1, timeout=window)
        except Exception:               # noqa: BLE001 - silence, as far as we know
            ack = None
        answered = ack is not None and ack.src == board
        self._health_trail.append((board, answered))
        if answered:
            self._health_sticky = board
        return answered

    def _health_candidates(self) -> "list[int]":
        """Who the health question goes to, in order, distinct (gate on
        f9efd43, the reviewer's design):

          1  the board that LAST answered one (`_health_sticky`, this
             worker's own memory) - on LOOK28 with the front body's 485
             cable out, boards 1-11 were dead and the healthy master was
             among 12-22, and nothing takes a board off `live` mid-show -
             while it is itself still live;
          2  the lowest live board (review of 9a8c045, M1 - address 1 is not
             always there, and not always the USB board);
          3  the highest live board - likely the other harness segment.

        Nothing live and nothing remembered: address 1. A relayed unicast
        STOP is what the sweep itself sends, and a degraded master relays
        nothing, so silence from all of them still means degraded."""
        live = sorted(b for b in self.live if isinstance(b, int))
        order = []
        sticky = self._health_sticky
        if sticky is not None and sticky in live:
            # Preferred only while it is still live (final gate on 05cc86a,
            # LOW-2): a sweep that has since lost it has the last word.
            order.append(sticky)
        if live:
            order += [live[0], live[-1]]
        elif not order:
            order.append(USB_BOARD)
        return list(dict.fromkeys(order))

    def _health_words(self) -> str:
        """What the last health question found, board by board: "board 2
        answers", "board 1 silent, board 12 answers", "board 1 silent", or -
        several silent - "no board answers (board 1, 12 silent)". The
        master's own address is not known (on LOOK28 it is not board 1), so
        only the generic sentence speaks of no board at all."""
        trail = getattr(self, "_health_trail", [])
        if not trail:
            return "not asked"
        boards = list(dict.fromkeys(b for b, _ in trail))
        answered = [b for b, ok in trail if ok]
        if answered:
            silent = [b for b in boards if b != answered[0]]
            said = "".join(f"board {b} silent, " for b in silent)
            return f"{said}board {answered[0]} answers"
        if len(boards) == 1:
            return f"board {boards[0]} silent"
        return ("no board answers (board "
                + ", ".join(str(b) for b in boards) + " silent)")

    def _usb_reset_reopen(self, bus, give_up_at: "float | None" = None,
                          reopen_by: "float | None" = None
                          ) -> "tuple[bool, str]":
        """The cure: close the port, reset the master's USB device, wait for
        its node to come back (a new node, possibly under a new name) and
        open it. Returns (reopened, what happened) - "ioctl" / "sudo
        usbreset", or why not.

        BOUNDED by `give_up_at` (review of 349dcdd, M2): the reset itself is
        given only what is left of it (transport.usb_reset(timeout=...)), so
        a sudo that hangs cannot hold a cue up, and neither can the wait for
        the node or the open's retries.

        The port is NEVER left closed. A reset that could not be done, did
        not finish, or whose node did not come back in time is followed by
        one more attempt to open the port as it is, bounded by `reopen_by`
        (the trigger, before a cue - a port opened late is still the only
        way the cue goes out at all) - and if even that fails the port is
        handed to the watcher explicitly (`_needs_reopen`), which keeps
        trying on its own poll.

        No frame goes out here: the reset is on the USB side and the open's
        own zero padding is not a frame. A reset owes the probe sweep
        exactly as a reopen does (OWED_SETTLE_S, _owed_setup_clear()).
        """
        port = getattr(bus, "port", None) or self.port or self._safe_find_port()
        old = self._safe_token(port)
        try:
            bus.close()
        except Exception:               # noqa: BLE001 - closing is best effort
            pass
        timeout = USBRESET_TIMEOUT_S
        if give_up_at is not None:
            timeout = min(timeout, max(0.1, give_up_at - time.monotonic()))
        try:
            done, how = self._usb_reset(port, timeout=timeout)
        except Exception as exc:        # noqa: BLE001 - an answer, not a raise
            # The port was closed a moment ago: whatever the reset did, the
            # one thing that must follow is the reopen below (review N1 - an
            # uncaught raise here failed the session and lost the next cue).
            done, how = False, f"usb reset raised: {exc or exc.__class__.__name__}"
        if not done:
            if not how.startswith("usb reset raised"):
                how = f"usb reset: {how}"
            return False, self._reopen_after_failure(bus, port, reopen_by, how)
        # The node goes and comes back - 0.44 s on the unit. Wait for a NEW
        # one (the token changes with every enumeration), under whatever
        # name it takes; if none is seen in time, try the name we have.
        limit = time.monotonic() + USB_NODE_WAIT_S
        if give_up_at is not None:
            limit = min(limit, give_up_at)
        found = None
        while time.monotonic() < limit:
            candidate = self.port or self._safe_find_port()
            token = self._safe_token(candidate)
            if token is not None and token != old:
                found = candidate
                break
            if not self._sleep(RETRY_POLL_S):
                break
        found = found or self.port or self._safe_find_port() or port
        # Owed from here on, whether or not the open below works: the
        # boards' view of this port is gone either way.
        self._setup_owed = True
        self._owed_at = time.monotonic()
        try:
            opened = self._open_retrying(bus, found, give_up_at)
        except Exception as exc:        # noqa: BLE001 - said, never raised
            return False, self._reopen_after_failure(
                bus, found, reopen_by,
                f"reset by {how}, but the port would not open ({exc})")
        if opened and opened != port:
            self.emit(f"port {opened}")
        return True, how

    def _safe_find_port(self) -> "str | None":
        """find_port() that answers None instead of raising - comports()
        can fail, and a recovery must not (review N1)."""
        try:
            return find_port()
        except Exception:               # noqa: BLE001 - "not found", for now
            return None

    def _safe_token(self, port: "str | None"):
        """The node's identity (link_token), or None - never a raise."""
        if not port:
            return None
        try:
            return self._link_token(port)
        except Exception:               # noqa: BLE001 - "not there", for now
            return None

    @staticmethod
    def _bus_closed(bus) -> bool:
        """True only when the bus says its port is closed (transport.Bus's
        pyserial object); a transport that cannot say is taken as open."""
        ser = getattr(bus, "ser", None)
        return ser is not None and getattr(ser, "is_open", True) is False

    def _survived(self, bus, what: str, exc: BaseException) -> None:
        """A recovery path raised anyway: say it, and hand a port it may
        have left closed to the watcher (_needs_reopen). A recovery attempt
        must never fail the session or lose a cue (review of 6a2d136, N1)."""
        self.emit(f"{what} raised: {exc or exc.__class__.__name__}")
        if self._bus_closed(bus):
            self._needs_reopen = True

    def _reopen_after_failure(self, bus, port: "str | None",
                              reopen_by: "float | None", why: str) -> str:
        """After a reset that was abandoned: open the port again as it is,
        once more, bounded by `reopen_by` - never leave it closed. If even
        that fails the watcher is told (`_needs_reopen`) and keeps trying on
        its own poll. Returns `why`, with what became of the port."""
        port = port or self.port or self._safe_find_port()
        if port:
            try:
                self._open_retrying(bus, port, reopen_by)
                self._needs_reopen = False
                return why
            except Exception:           # noqa: BLE001 - handed on, below
                pass
        self._needs_reopen = True
        return f"{why}; the port is left to the watcher"

    def _quiet_gap(self, seconds: float) -> "str | None":
        """The proof's silence, cut short the moment anything wants the bus:
        a cue armed, or a prepare / burn / clear queued (review L1 - a START
        right after "Recover bus" must never be late because of a proof).
        Returns None when the full gap was waited, else why it ended."""
        end = time.monotonic() + seconds
        session = self.remote
        while True:
            if session is not None:
                if session.due() is not None:
                    return "proof cut short by a cue"
                if session.pending_job():
                    return "proof cut short by a job"
            left = end - time.monotonic()
            if left <= 0:
                return None
            if not self._sleep(min(left, RETRY_POLL_S)):
                return "stopped"

    def _prove_recovered(self, bus, groups: int
                         ) -> "tuple[bool, float | None, str]":
        """PROOF that the bus is back - both, never just a timing:

          1  the master ACKs a unicast STOP (_master_answers(); a
             degraded one answers nothing), and
          2  a broadcast STOP sent after PROOF_GAP_S of silence takes under
             RECOVERED_MS. The gap is the point: on the degraded master a
             write half a second after another read 61 ms - the false
             "recovered by padding (512 -> 61 ms)" of 2026-09-28 - while one
             after a real pause blocked 272-358 ms.

        Both assume the board on the USB cable is bus address 1
        (ADDR_BUS_MASTER, SPECIFICATION 4.5): a master at another address
        never ACKs the STOP to address 1 and reads as silent.

        If a cue or a job comes up during the gap (_quiet_gap()), the proof
        ends there with the verdict so far - the master answered - rather
        than make the cue wait: "master answers, proof cut short by a cue".

        Returns (proven, the gapped STOP's ms or None, what it found).
        """
        if not self._master_answers(bus, groups):
            return False, None, self._health_words()
        who = self._health_words()
        cut = self._quiet_gap(self.proof_gap)
        if cut == "stopped":
            return False, None, "stopped"
        if cut:
            return True, None, f"{who}, {cut}"
        after = self._timed_stop(bus, groups)
        if self._stop_failed is not None:
            return False, after, (f"{who}, but the stop after "
                                  f"{self.proof_gap:g} s: write failed: "
                                  f"{self._stop_failed}")
        if after >= RECOVERED_MS:
            return False, after, (f"{who}, but a stop after "
                                  f"{self.proof_gap:g} s took {after:.0f} ms")
        return True, after, who

    def _timed_stop(self, bus, groups: int,
                    record_from_ms: float = STALL_LOG_MS) -> float:
        """One broadcast STOP, timed, through the usual stall path.

        A write that raises is not an exception here: the point of the
        frame is the CLOCK, and a write that timed out took its whole
        write timeout, which is exactly the "still bad" answer. So the
        elapsed time is returned either way and the caller compares it
        with RECOVERED_MS like any other.

        `record_from_ms`: the smallest block that goes on the record
        (bus_stall, and so the tile). The pre-cue check passes
        STALL_RECOVER_MS - a 60 ms block right before a cue is not worth an
        amber mark, only the state it exists to catch is (review F1(d)).

        A write that raised leaves its reason in `self._stop_failed` (None
        after one that went out): a failed write can be fast, and a fast
        time must never read as "bus ok" or "clear" (final gate, LOW).
        """
        began = time.perf_counter()
        self._stop_failed = None
        try:
            took_ms = self._send_timed(bus, stop(0xFF, groups), "stop",
                                       record_from_ms=record_from_ms)
        except Exception as exc:        # noqa: BLE001 - measured, not raised
            took_ms = (time.perf_counter() - began) * 1000.0
            self._stop_failed = str(exc) or type(exc).__name__
            self.emit(f"bus recovery: the stop did not go out ({exc})")
            return took_ms
        self._sent_broadcast_stop()
        return took_ms

    def _fast_reopen(self, bus, groups: int, port: "str | None" = None,
                     frames: bool = True, at: "float | None" = None
                     ) -> "tuple[int, str | None]":
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

        `frames=False` is the port and nothing else - the DTR toggle and the
        open's own padding, not one frame to a board. The trigger that
        follows needs no frame before it, and what was skipped is owed with
        the sweep.

        The frames are decided one by one AFTER the port is open - never
        before it, since the open itself costs 0.3 s and a decision taken at
        T-5.5 would be stale by T-5.2 (re-review of e139a33). EVERY frame is
        checked, whether or not the caller named a trigger: `at` is the
        port watcher's cue when it has one, and otherwise _frame_refusal()
        looks at the last picture's floor and the cue the session has armed
        itself (round-3 review: with `at=None` the idle watcher's frames went
        out unchecked, inside the last picture - the LOOK28 case exactly).
        The first frame that cannot go ends the frames.

        Returns (frames sent - 0, 1 or 2 -, why the rest did not go or None):
        "the last picture is still repainting", "a cue is too near", or
        "write failed: <error>" - never swallowed; the caller says it once.
        """
        if port is None:
            port = self.port or self._safe_find_port() or getattr(bus, "port", None)
        opened = self._open_retrying(
            bus, port, give_up_at=None if at is None else at - FIRE_SPIN_S)
        if opened and opened != port:
            self.emit(f"port {opened}")
        # The boards are owed their sweep from here on, whatever happens
        # below: as far as they are concerned this is a new port. It runs
        # only where _owed_setup_clear() says so (review F5), and not before
        # OWED_SETTLE_S from now.
        self._setup_owed = True
        self._owed_at = time.monotonic()
        if not frames:
            return 0, "no frames asked for"
        sent = 0
        try:
            why = self._frame_refusal(at, DEGRADED_WRITE_S)
            if why:
                return sent, why
            bus.send(stop(0xFF, groups))
            self._sent_broadcast_stop()
            sent += 1
            why = self._frame_refusal(at, DEGRADED_WRITE_S + FAST_REOPEN_READ_S)
            if why:
                return sent, why
            bus.request(slot_config(USB_BOARD, self.slot, group_count=groups,
                                    dev_type=self._active_dev_type()),
                        retries=1, timeout=FAST_REOPEN_READ_S)
            sent += 1
        except Exception as exc:    # noqa: BLE001 - reported, never raised
            return sent, f"write failed: {exc or exc.__class__.__name__}"
        return sent, None

    def _frame_refusal(self, at: "float | None", cost: float) -> "str | None":
        """Why one more frame, costing up to `cost`, may NOT go out now -
        or None when it may.

        The per-frame form of the pre-cue check's rule: not inside the last
        picture (`_guard_floor`), and handed over before
        `at - REMOTE_GUARD_HOLD_S` - `at` being the trigger given, or else
        the cue the session has armed. With neither, only the floor counts.
        """
        now = time.monotonic()
        if self._guard_floor is not None and now < self._guard_floor:
            return "the last picture is still repainting"
        if at is None and self.remote is not None:
            due = self.remote.due()
            at = due[1] if due is not None else None
        if at is not None and now + cost > at - self.remote_guard_hold:
            return "a cue is too near"
        return None

    def _frame_fits(self, at: "float | None", cost: float) -> bool:
        """_frame_refusal() as a yes/no."""
        return self._frame_refusal(at, cost) is None

    def _recovery_done(self, by: "str | None", before_ms: float,
                       after_ms: "float | None", recovered: bool,
                       note: "str | None" = None) -> dict:
        """Record and announce one recovery; the dict is what the agent's
        POST /bus/recover answers with. `note` is what the proof found, or
        why there was no cure: it goes into the log line, so a failure
        always says which step it failed at."""
        previous = self.bus_recovery or {}
        self.bus_recovery = {"at": time.time(), "by": by,
                             "before_ms": round(before_ms, 1),
                             "after_ms": (None if after_ms is None
                                          else round(after_ms, 1)),
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
        span = (f"{before_ms:.0f} ms" if after_ms is None
                else f"{before_ms:.0f} → {after_ms:.0f} ms")
        tail = f", {note}" if note else ""
        if recovered and by is None:
            self.emit(f"bus is clear, nothing to recover ({before_ms:.0f} ms"
                      f"{tail})")
        elif recovered:
            self.emit(f"bus recovered by {by.replace('_', ' ')} ({span}{tail})")
        else:
            self.emit(f"bus recovery failed ({span}{tail})")
        return {"recovered": recovered, "by": by,
                "before_ms": self.bus_recovery["before_ms"],
                "after_ms": self.bus_recovery["after_ms"]}

    def _recover_bus(self, bus, groups: int,
                     measured: "float | None" = None) -> dict:
        """Get a bus that accepts frames and executes none working again -
        the idle recovery (two stalled heartbeats) and POST /bus/recover.

          0  what is wrong, measured NOW: a timed STOP, and if that is not
             already a stall, a unicast STOP to the master. Only BOTH -
             the STOP under STALL_RECOVER_MS and the master answering - is
             "clear" ("bus was already clear" on the page). The STOP alone
             could read fast right behind the heartbeat that triggered this
             (61 ms half a second after a write, on the degraded master);
             the silence cannot be faked that way.
          1  the cure: the port closed, the master's USB device reset, its
             node waited for (it may come back renamed), the port opened
             again with retries while udev catches up (_usb_reset_reopen()).
          2  the PROOF (_prove_recovered()): the master ACKs, and a
             STOP after PROOF_GAP_S of silence is under RECOVERED_MS. Only
             then "by usb_reset"; anything less is "failed", with what was
             found, and the tile goes on saying "restart this unit".

        Padding and a port reopen are NOT offered as cures any more: on the
        unit neither brought a degraded master back (2026-09-28), and the
        padding's "recovered" was the false reading PROOF_GAP_S prevents.

        Bounded. Typically about 6 s (4.45 s measured with the unit's
        timings): a degraded STOP 0.4, the questions, the reset 0.3, the node
        back ~0.45, the open 0.3, the proof's question, the gap 3.0 and one
        STOP. With every limit hit at once about 20 s: the STOP 0.4, three
        candidates asked (0.4 + a 0.5 read + 0.05 overshoot each) 2.85, the
        reset at USBRESET_TIMEOUT_S 5.0, the node at USB_NODE_WAIT_S 3.0,
        the open at OPEN_RETRY_S 2.0 + 0.3, the STOP after it 0.4, the proof's
        three questions 2.85, the gap 3.0 and its STOP 0.4 = 20.2 s. The
        agent waits RECOVER_WAIT_S 22 s, the Conductor 25.
        Every entry is gated on the last picture's floor and a minute with
        no cue (recover_refusal(), _recover_quiet()), and nothing here
        paints; the probe sweep it owes waits for OWED_SETTLE_S and that
        same gate.

        Never re-entered: it is reached from _remote_guard_tick(), which
        _setup() and _fire_at() both call.
        """
        if self._recovering:
            return {"recovered": False, "by": None,
                    "before_ms": None, "after_ms": None}
        self._recovering = True
        try:
            return self._recover_bus_steps(bus, groups, measured)
        except Exception as exc:        # noqa: BLE001 - see _survived()
            self._survived(bus, "bus recovery", exc)
            return {"recovered": False, "by": None,
                    "before_ms": None, "after_ms": None}
        finally:
            self._recovering = False

    def _recover_bus_steps(self, bus, groups: int,
                           measured: "float | None" = None) -> dict:
        """_recover_bus()'s ladder, under its guard. `measured`: a caller
        that has just found the bus bad (_owed_sweep_bus_ok()) hands over
        its STOP's time and the ladder starts at the cure - measured again
        right behind that STOP, a degraded master reads 61 ms."""
        if measured is not None:
            before = measured
        else:
            before = self._timed_stop(bus, groups)
            if (self._stop_failed is None and before < STALL_RECOVER_MS
                    and self._master_answers(bus, groups)):
                return self._recovery_done(None, before, before, True,
                                           self._health_words())
        done, how = self._usb_reset_reopen(bus)
        if not done:
            return self._recovery_done(None, before, None, False, how)
        self._stop_after_reset(bus, groups)
        proven, after, found = self._prove_recovered(bus, groups)
        if proven:
            return self._recovery_done("usb_reset", before, after, True,
                                       found)
        return self._recovery_done(None, before, after, False, found)

    def _stop_after_reset(self, bus, groups: int,
                          at: "float | None" = None) -> None:
        """One broadcast STOP straight after a reset-reopen (review M4): if
        a USB reset ever restarts the master's factory autoplay, this
        silences it before the next cue does anything. Asked
        _frame_refusal() like every frame - not inside a picture, not in
        the hold before a trigger - and never on the fire path, where the
        show frame is what goes first and nothing follows it."""
        if self._frame_refusal(at, DEGRADED_WRITE_S) is None:
            self._timed_stop(bus, groups, record_from_ms=STALL_RECOVER_MS)

    def _fire_resend_signal(self, bus, groups: int) -> "bool | None":
        """Right after a stalled show frame: True if the master is silent
        (degraded - re-send), False if it answers (slow but working - do
        not), None if there is no safe way to ask. On this firmware nothing
        is safe to ask there (see _resend_on_stall()), so: None, and no frame
        on the wire."""
        return None

    def _resend_on_stall(self, bus, groups: int, frame, cue_id: str,
                         took_ms: float, at: float) -> "float | None":
        """The fire-time re-send (--resend-on-stall; OFF by default).

        Returns when the frame was re-sent, or None when it was not - and
        then the ORIGINAL frame stays the cue's: its send time, its late_ms
        and its guard floor (review of 349dcdd, M2). A re-send becomes the
        frame the picture starts from, so the caller takes those from it.

        A stall alone is NOT enough, because there are two states behind a
        blocked show frame (review H1):

          * DEGRADED (radxa-07, 2026-09-28 Run 2): the frame is accepted and
            never executed, and the master answers nothing - a 272 ms
            preset changed no panel;
          * SLOW but working (LOOK23, 2026-09-28 17:29): every picture
            appeared, each about 0.36 s late, and the master answers.

        Re-sending in the second repaints the slot twice, so a second signal
        has to tell them apart - and there is none left at fire time
        (2026-09-28 12:40, radxa-07): the unicast 0x02 this asked is never
        answered on this firmware (so every stall read "silent"), and the
        unicast STOP that replaced it elsewhere cannot go here - a working
        master is repainting and deaf right after the show frame, and a STOP
        to a board whose sweep delay has not run out cancels its picture. So
        with the flag on the stall is SAID and nothing is re-sent: "cue q05
        stalled 359 ms - not re-sent (no safe question at fire time)". The
        pre-cue check and the idle recovery are where the cure runs. The
        reset-and-re-send below is kept for a signal that is safe here, and
        `_fire_resend_signal` is where one would be plugged in.
        """
        if not self.resend_on_stall or took_ms < RESEND_STALL_MS:
            return None
        give_up = at + FIRE_RESEND_BUDGET_S
        silent = self._fire_resend_signal(bus, groups)
        if silent is None:
            self.emit(f"cue {cue_id} stalled {took_ms:.0f} ms - not re-sent "
                      f"(no safe question at fire time)")
            return None
        if not silent:
            self.emit(f"cue {cue_id} stalled {took_ms:.0f} ms, master answers "
                      f"- not re-sent")
            return None
        previous = self.resend or {}
        record = {"cue": cue_id, "at": time.time(),
                  "before_ms": round(took_ms, 1), "after_ms": None,
                  "late_s": None, "by": None,
                  "count": int(previous.get("count", 0)) + 1}
        self.resend = record
        # A port opened late is still the only way anything goes out on it:
        # the reopen after a failed reset may run past the budget, just not
        # past the next thing the unit has to do (FIRE_SPIN_S is ms).
        done, how = self._usb_reset_reopen(bus, give_up_at=give_up,
                                           reopen_by=give_up + OPEN_RETRY_S)
        if not done:
            self.emit(f"cue {cue_id} re-send failed ({how})")
            return None
        if time.monotonic() > give_up:
            late = time.monotonic() - at
            self.emit(f"cue {cue_id} re-send failed (the port was back "
                      f"{late:.1f} s late, past the {FIRE_RESEND_BUDGET_S:g} s "
                      f"budget)")
            return None
        try:
            after = self._send_timed(bus, frame, "show re-send")
        except Exception as exc:        # noqa: BLE001 - said, never raised
            self.emit(f"cue {cue_id} re-send failed (write: {exc})")
            return None
        sent = time.monotonic()
        late = sent - at
        record.update(after_ms=round(after, 1), late_s=round(late, 2),
                      by="usb_reset")
        self.emit(f"cue {cue_id} re-sent after usb reset (stall "
                  f"{took_ms:.0f} ms → {after:.0f} ms, {late:.1f} s late)")
        return sent

    def _precheck(self, bus, groups: int, cue_id: str, at: float) -> None:
        """PRECHECK_S before a cue: is the port going to take the frame?

        A timed broadcast STOP and a unicast STOP to the first health
        candidate (_health_candidates()), which ACKs; after one miss behind
        a STOP under STALL_LOG_MS, the next candidate with a short window if
        the reset step still fits behind it, else the reset. On a
        healthy unit that is two frames of a few milliseconds and the whole
        check. Either one bad - the STOP blocked STALL_RECOVER_MS or more,
        or the master silent - and the master's USB device is reset there
        and then (_usb_reset_reopen()), proven by its ACK again
        ("proof-lite": the 3 s gap of the full proof does not fit here).

        Called on every tick of _fire_at()'s wait; it decides for itself
        whether this is the moment, and runs at most once per cue:

        * not before PRECHECK_S ahead of `at`, and not BEGUN with less
          than REMOTE_GUARD_HOLD_S + DEGRADED_WRITE_S left (5.4 s) - the
          measuring STOP is itself one write that may block 0.4 s, and
          nothing but the trigger goes on the wire in the last
          REMOTE_GUARD_HOLD_S. A cue armed too close for that gets no
          check, and nothing is said about it;
        * not before `_guard_floor`: the previous cue's picture is
          certainly finished, so neither frame measures a board busy
          repainting nor lands inside a sweep.

        The cue is NEVER delayed by the verdict. The reset step is begun
        only if USB_RESET_BUDGET_S still fits before `at -
        REMOTE_GUARD_HOLD_S`; every frame is asked _frame_refusal() on its
        own; and the reset's own waits give up before the trigger.
        """
        if self.precheck_s <= 0 or self._prechecked == cue_id:
            return
        now = time.monotonic()
        remaining = at - now
        if remaining > self.precheck_s:
            return                              # not yet
        if remaining < self.remote_guard_hold + DEGRADED_WRITE_S:
            return                              # too late to begin: skip
        if self._guard_floor is not None and now < self._guard_floor:
            return                              # the last picture is drawing
        self._prechecked = cue_id
        deadline = at - self.remote_guard_hold

        before = self._timed_stop(bus, groups, record_from_ms=STALL_RECOVER_MS)
        # A STOP whose write raised went nowhere, however fast it failed:
        # that is the reset path, as for a silent master (final gate, LOW).
        failed = self._stop_failed
        stalled = failed is not None or before >= STALL_RECOVER_MS
        asked = answers = False
        no_second = False
        self._health_trail = []
        if not stalled and self._frame_fits(at, MASTER_ASK_COST_S):
            asked = True
            candidates = self._health_candidates()
            answers = self._ask_board(bus, groups, candidates[0], MASTER_ASK_S)
            if not answers and before < STALL_LOG_MS:
                # One miss behind a STOP this fast may be a board that
                # dropped off mid-show, or a lost frame: the NEXT candidate
                # is asked, with a short window - but only if the reset
                # step still fits behind it (gate on f9efd43, MED-1: a
                # degraded master whose STOP read under 50 ms got two full
                # asks and then "no time for a usb reset"). If it does not
                # fit, the one miss gets the reset: a needless reset was
                # harmless on radxa-07, a missed cure is a lost cue.
                if (time.monotonic() + HEALTH_SECOND_ASK_COST_S
                        + USB_RESET_BUDGET_S <= deadline):
                    board = (candidates[1] if len(candidates) > 1
                             else candidates[0])
                    answers = self._ask_board(bus, groups, board,
                                              HEALTH_SECOND_ASK_S)
                else:
                    no_second = True
        healthy = not stalled and (answers or not asked)
        found = self._health_words()
        steps = []
        cured = False
        if not healthy:
            if time.monotonic() + USB_RESET_BUDGET_S > deadline:
                steps.append("no time for a usb reset")
            else:
                steps.append("usb reset")
                # The reset, its node and its open are all done by the
                # hold (review M2 - a sudo that hung 4.5 s once reopened
                # the port at T-3.11). Only the fallback reopen after an
                # abandoned reset may run on towards the trigger: an open
                # port is the one thing the cue cannot go out without.
                done, how = self._usb_reset_reopen(
                    bus, give_up_at=deadline, reopen_by=at - FIRE_SPIN_S)
                if not done:
                    steps.append(f"failed ({how})")
                else:
                    self._stop_after_reset(bus, groups, at)
                    why = self._frame_refusal(at, MASTER_ASK_COST_S)
                    if why:
                        steps.append(f"not proven ({why})")
                    else:
                        # The first candidate NOT already silent in this
                        # check (final gate on 05cc86a, LOW-2): a board that
                        # just failed to answer is the worst witness of the
                        # cure. All of them silent: the first one.
                        silent = {b for b, ok in self._health_trail if not ok}
                        candidates = self._health_candidates()
                        board = next((b for b in candidates
                                      if b not in silent), candidates[0])
                        self._health_trail = []
                        if self._ask_board(bus, groups, board, MASTER_ASK_S):
                            cured = True
                            steps.append(f"ok ({self._health_words()})")
                        else:
                            steps.append(
                                f"no answer ({self._health_words()})")
        self.precheck = {"cue": cue_id, "at": time.time(),
                         "before_ms": round(before, 1),
                         "by": "usb_reset" if cured else None,
                         # No timed STOP after a cure: its 3 s gap does not
                         # fit before a trigger (see PRECHECK_S).
                         "after_ms": None,
                         "master_answers": answers or cured}
        if healthy and len(self._health_trail) > 1:
            self.emit(f"precheck {cue_id}: {found} → bus ok "
                      f"({before:.0f} ms)")
            return
        if healthy and not asked:
            # Begun too late for a question: the STOP alone is not "bus ok"
            # (final gate on 05cc86a, LOW-4) - it could read fast right
            # behind a write on a degraded master.
            self.emit(f"precheck {cue_id}: stop {before:.0f} ms, no time "
                      f"to ask a board")
            return
        if healthy:
            said = f", {found}" if asked else ""
            self.emit(f"precheck {cue_id}: bus ok ({before:.0f} ms{said})")
            return
        if no_second:
            found += ", no time to ask another"
        what = (f"write failed: {failed}" if failed is not None
                else f"stalled {before:.0f} ms" if stalled
                else f"{found} ({before:.0f} ms)")
        chain = "".join(f" → {step}" for step in steps)
        self.emit(f"precheck {cue_id}: {what}{chain}")

    # ---- a USB re-enumeration, noticed at once ----

    def _port_watch(self, bus, groups: int, at: "float | None" = None,
                    wanted=None) -> bool:
        """Poll the device node; if it has gone, wait for it and reopen.

        The kernel takes the node away and brings it back under a new name
        in about half a second (see PORT_POLL_S). Until this existed the
        unit found out at its next write - up to 20 s later - and then
        waited reopen_delay and swept every board, five seconds in all.

        From _fire_at()'s wait, `at` is the trigger and `wanted()` says
        whether the cue is still the one armed: the wait for the node never
        runs past the trigger's last FIRE_SPIN_S, re-polls every
        PORT_RETRY_S, and gives up at once on a cancel or a STOP - noticed
        within PORT_RETRY_S. Once the node is back the port is reopened,
        but each frame only goes with it where _frame_refusal() allows -
        from the idle loop just as much as before a cue, since the last
        picture's floor holds either way; otherwise it is the port alone,
        and the log says why (`port only (the last picture is still
        repainting)` / `(a cue is too near)` / `(write failed: ...)`).

        Never while _setup() is probing: that sweep's own writes find a
        lost port, and the reopen ladder around it is the one that owns
        the bus then.

        True when a loss was found AND dealt with, so the caller knows the
        port under it has been replaced. Rate-limited to PORT_POLL_S: this
        is called from every 50 ms tick of the worker's waits.
        """
        if self._probing:
            return False
        if self._needs_reopen:
            # Handed over by an abandoned reset (_reopen_after_failure()):
            # the port is CLOSED, so nothing else will notice until a write
            # fails. Kept up whatever --no-port-watch says - an open port is
            # the one thing a cue cannot go out without.
            return self._reopen_handed_over(bus, groups, at)
        if not self.port_watch or self.port_poll <= 0:
            return False
        now = time.monotonic()
        if now < self._next_port_poll:
            return False
        self._next_port_poll = now + self.port_poll
        port = getattr(bus, "port", None) or self.port
        if not port:
            return False
        try:
            if self._link_token(port) is not None:
                return False
        except Exception:               # noqa: BLE001 - see below
            # Cannot tell whether the node is there: do nothing on a guess.
            # Read as "gone" it would start a 3 s wait on every poll, and a
            # raise here must never reach the session (final gate, LOW).
            return False
        began = now
        found = None
        why = f"port gone for {self.port_back_wait:g} s, waiting"
        while True:
            now = time.monotonic()
            if now - began >= self.port_back_wait:
                break
            if at is not None and now >= at - FIRE_SPIN_S:
                why = "port gone at the trigger, not waiting past it"
                break
            if wanted is not None and not wanted():
                return False                # the cue was cancelled or moved
            candidate = self.port or self._safe_find_port()
            if candidate and self._safe_token(candidate) is not None:
                found = candidate
                break
            pause = PORT_RETRY_S
            if at is not None:
                pause = min(pause, max(0.0, at - FIRE_SPIN_S - now))
            if not self._sleep(pause):
                return False
        if found is None:
            # Not a re-enumeration (a cable out, a board with no power), or
            # the trigger came first. The ordinary ladder (reopen_delay,
            # port_wait, a full setup) owns that, so this stands down.
            self.emit(why)
            return False
        back = time.monotonic() - began
        # Frames or not is decided AFTER the port is open and frame by frame
        # - the open itself takes 0.3 s, and a verdict taken before it can be
        # inside the hold by the time it acts (re-review of e139a33).
        try:
            sent, why = self._fast_reopen(bus, groups, port=found, at=at)
        except Exception as exc:        # noqa: BLE001 - said, never raised
            self.emit(f"port lost → {found} back in {back:.1f} s, "
                      f"but it would not open ({exc})")
            return False
        if why is None:
            # Both frames went; the checking STOP is one more frame, asked
            # the same question.
            why = self._frame_refusal(at, DEGRADED_WRITE_S)
            if why is None:
                after = self._timed_stop(bus, groups)
                state = (f"write failed: {self._stop_failed}"
                         if self._stop_failed is not None
                         else f"bus ok ({after:.0f} ms)" if after < RECOVERED_MS
                         else f"bus still stalled ({after:.0f} ms)")
            else:
                state = f"reopened, not measured ({why})"
        elif sent == 0:
            state = f"port only ({why})"
        else:
            state = f"reopened, not measured ({why})"
        self.emit(f"port lost → {found} back in {back:.1f} s, {state}")
        self._next_port_poll = time.monotonic() + self.port_poll
        return True

    def _check_usb_reset(self, port: str) -> None:
        """Whether the one cure is available on this unit (review L2),
        asked once per runner: the ioctl's node writable, or `sudo -n` and
        usbreset there. Said in the log when it is NOT, since a recovery on
        such a unit can only ever report "failed"."""
        try:
            ok, why = self._usb_reset_check(port)
        except Exception as exc:        # noqa: BLE001 - unknown is "no"
            ok, why = False, str(exc)
        self.usb_reset_ok, self.usb_reset_why = bool(ok), why
        if not ok:
            self.emit(f"no usb reset on this unit ({why})")

    def _reopen_handed_over(self, bus, groups: int,
                            at: "float | None" = None) -> bool:
        """Open a port an abandoned reset left closed (`_needs_reopen`),
        once a node is there - every PORT_POLL_S, each frame after the open
        asked _frame_refusal() as always. True once it is open again."""
        now = time.monotonic()
        if now < self._next_port_poll:
            return False
        self._next_port_poll = now + max(self.port_poll, RETRY_POLL_S)
        port = self.port or self._safe_find_port() or getattr(bus, "port", None)
        if not port or self._safe_token(port) is None:
            return False
        try:
            sent, why = self._fast_reopen(bus, groups, port=port, at=at)
        except Exception:               # noqa: BLE001 - next poll tries again
            return False
        self._needs_reopen = False
        self.emit(f"port {port} reopened after the reset"
                  + (f" ({why})" if why and sent == 0 else ""))
        return True

    def _owed_setup_clear(self, session) -> bool:
        """May the probe sweep a fast reopen left owed run NOW? (review F5)

        That sweep opens with a broadcast 0x17 and then asks every board a
        unicast 0x17 and 0x1B. Handed straight back to the loop it ran the
        moment _fire_at() returned - milliseconds after the NEXT cue's show
        frame, inside the very sweep that frame had just started (a 0x17
        there leaves the change half-drawn, SPECIFICATION 4.2) and probing
        22 boards that are deaf because they are repainting.

        So all of these, the same as the idle recovery's own:
        * OWED_SETTLE_S since the reopen that owed it: a recovery is pressed
          right before a preset or a START, and a sweep begun at once was
          still sweeping when that cue fired (re-review of e139a33);
        * the last cue's picture is finished (`_guard_floor`);
        * no cue armed within recover_quiet (60 s);
        * no show being played or held - during a run cues keep coming, so
          the sweep simply waits for the run to end or for a gap that long.
        Not firing or probing goes without saying: this is only asked at
        the top of the loop, where neither is happening. And should a cue
        still fire from inside the sweep, it stands down (_setup()).
        """
        now = time.monotonic()
        if now - self._owed_at < self.owed_settle:
            return False
        if self._guard_floor is not None and now < self._guard_floor:
            return False
        if session.playing():
            return False
        due = session.due()
        return due is None or due[1] - now >= self.recover_quiet

    def _owed_sweep_bus_ok(self, bus, groups: int) -> bool:
        """May the owed sweep run on this bus? Asked once _owed_setup_clear()
        has said it may run at all (PM, radxa-07 2026-09-28 12:44): after the
        show the owed sweep ran FIRST, on a master that had degraded during
        it, found nothing and fell into the reopen loop, which never resets
        USB and never let the idle recovery in.

        So: a timed broadcast STOP under STALL_RECOVER_MS (a write that
        raised is not) and the master's ACK to a unicast STOP - then True,
        and the sweep runs. Otherwise the recovery ladder runs first
        (_recover_bus() from its cure: reset, prove) and False: the reset owes
        the sweep again, OWED_SETTLE_S from now, and this is asked again
        then. The ladder counts against the idle recovery's own attempts
        and backoff (recover_attempts_max, recover_backoff); once those are
        spent, or with --no-auto-recover, the sweep runs as it always did
        (and the setup's own reset, _setup_usb_reset(), is what is left).
        """
        try:
            before = self._timed_stop(bus, groups,
                                      record_from_ms=STALL_RECOVER_MS)
            failed = self._stop_failed
            stalled = failed is not None or before >= STALL_RECOVER_MS
            if not stalled and self._master_answers(bus, groups):
                return True
        except Exception as exc:        # noqa: BLE001 - see _survived()
            self._survived(bus, "owed sweep check", exc)
            self._owed_at = time.monotonic()    # asked again after the settle
            return False
        what = (f"write failed: {failed}" if failed is not None
                else f"stalled {before:.0f} ms" if stalled
                else f"{self._health_words()} ({before:.0f} ms)")
        now = time.monotonic()
        if (not self.auto_recover
                or self._recover_tries >= self.recover_attempts_max):
            self.emit(f"owed probe sweep: bus {what}, sweeping all the same")
            return True
        if now < self._recover_next:
            # Stays owed; asked again after the settle.
            self._owed_at = now
            return False
        self.emit(f"owed probe sweep: bus {what} → recovery first")
        self._recover_tries += 1
        self._recover_next = now + self.recover_backoff
        self._stall_streak = 0
        self._recover_bus(bus, groups, measured=before)
        # A reset owes the sweep itself (from now); one that could not reset
        # leaves it owed the same way, for the next attempt.
        self._setup_owed = True
        self._owed_at = time.monotonic()
        return False

    def _recover_quiet(self, now: float) -> bool:
        """True when nothing is going to want the bus for recover_quiet.

        The recovery is bounded but it is seconds long and it reopens the
        port, so it stands aside for everything: a cue armed within the
        quiet window, a prepare / burn / clear queued, a show being
        played or held (a run between two far-apart cues is exactly when
        this must NOT happen, however wide the gap looks), the last cue's
        picture still drawing (`_guard_floor` - the heartbeat that calls
        this already keeps it, and this says so rather than rely on it),
        and the probing sweep, which is already asking every board anyway.
        """
        session = self.remote
        if session is None or self._firing or self._probing:
            return False
        if self._guard_floor is not None and now < self._guard_floor:
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

        `auto_recover` off (--no-auto-recover) and this does nothing at
        all; the operator's POST /bus/recover still works.
        """
        if not self.auto_recover:
            return
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
                    # the last 20 ms and the frame. Its wait for the node
                    # is bounded by `at`, and it lets go the moment the cue
                    # is cancelled or moved.
                    self._port_watch(
                        bus, groups, at=at,
                        wanted=lambda: (not self._stop.is_set()
                                        and session.due()
                                        == (cue_id, at, slot, dev_type)))
                    # ...and the one frame that says whether THIS cue's
                    # broadcast is going to be taken at all. It decides for
                    # itself whether this tick is its moment: after the
                    # last picture's floor, and never inside the hold
                    # before `at` (see PRECHECK_S).
                    # Guarded: a check that raised must never fail the
                    # session or lose this cue (review N1).
                    try:
                        self._precheck(bus, groups, cue_id, at)
                    except Exception as exc:    # noqa: BLE001 - _survived()
                        self._survived(bus, f"precheck {cue_id}", exc)
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
            # Taken HERE, before any re-send is tried: if it is abandoned
            # the cue's send time, its late_ms and its guard floor are this
            # frame's (review of 349dcdd, M2 - a hung reset once reported
            # the cue +5251 ms). A re-send that went is the frame the
            # picture starts from, and takes its place.
            sent_at = time.monotonic()
            try:
                resent = self._resend_on_stall(bus, groups, frame, cue_id,
                                               took_ms, at)
            except Exception as exc:        # noqa: BLE001 - review N1
                # The cue already went; a re-send that raised costs nothing
                # but itself, and the original frame stays the cue's.
                self._survived(bus, f"cue {cue_id} re-send", exc)
                resent = None
            sent_at = self._last_show_at = resent or sent_at
            if self._probing:
                # Fired from inside a probe sweep (_fire_before_probing()):
                # an OWED sweep stands down at its next step (_setup()).
                self._fired_in_setup = True
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
        self._reset_setup_resets()
        self._setup_owed = False
        # Never carried over from a previous worker: a worker stopped with it
        # set would otherwise let THIS worker's start-up sweep stand down.
        self._setup_yields = False
        while not self._stop.is_set():
            port = self.port or self._safe_find_port()
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
                    self._needs_reopen = False      # a fresh open is open
                    groups = self._take_groups()
                    if self.usb_reset_ok is None:
                        self._check_usb_reset(port)
                    while not self._stop.is_set():
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
                            # Every gate again, now that the worker has it:
                            # one may have closed since the request passed
                            # them (review F6). The same words, so the
                            # operator reads the same 409 either way.
                            refusal = session.recover_refusal()
                            session.recovered(
                                recover_job,
                                {"error": refusal} if refusal
                                else self._recover_bus(bus, groups))
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
                        if self._setup_owed:
                            # A fast reopen (a recovery, the pre-cue check,
                            # a re-enumeration) left the probe sweep owed.
                            if needs_setup:
                                # The sweep about to run covers it - and it
                                # is a start-up one, which never yields.
                                self._setup_owed = False
                            elif (self._owed_setup_clear(session)
                                  and self._owed_sweep_bus_ok(bus, groups)):
                                # It runs HERE and only when nothing it sends
                                # can land in a picture or near a trigger;
                                # otherwise it stays owed and the loop goes
                                # on without it - the reopened port already
                                # carries triggers, which need nothing
                                # probed. Set immediately before this one
                                # _setup() call and nowhere else (round-3
                                # review): decided at the top of the loop, a
                                # /bus/recover taken on the same pass left it
                                # standing for a sweep that had skipped the
                                # 20 s settle. _setup() clears it again.
                                self._setup_owed = False
                                self._setup_yields = True
                                needs_setup = True
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
        self._reset_setup_resets()
        while not self._stop.is_set():
            port = self.port or self._safe_find_port()
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
                    if self.usb_reset_ok is None:
                        # Standby and the demos cure a dead master in their
                        # setup too (_setup_usb_reset()), so they ask once.
                        self._check_usb_reset(port)
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
