"""The unit runs the show by itself: a show file, a start time, no PC needed.

The PC compiles the timeline into one file per unit (conductor/showfile.py)
and loads it before the start. Pre-burn (docs/MERIS_REPLY_3SLOT.pdf,
2026-09-24): every cue's picture already went into its own slot (1..18)
the moment the show was load()ed - see RemoteSession.burn() - so from
then on the only thing that ever has to arrive is T0 - "second 0 of the
show, in your monotonic clock". The player turns every cue into a single
timed trigger on its own: arm a broadcast "show slot N" for T0 + sent,
fire it. Nothing is written here - a colour save, a delay table and a
slot's own config all persist across a power cycle, so a write only
ever happens again at the next /show/load. The Wi-Fi may drop for the
rest of the show.

Everything the operator does to a running show is a new T0:

    HOLD     stop scheduling (a cue armed is disarmed)
    RESUME   run again with T0 moved later by the time held
    NEXT     run with T0 moved earlier, so the next cue is due now

so the player has one rule to follow, not four: look at the clock, work
out which cue should be on the garment and which comes next, and do what
is missing. That same rule is the recovery. After a reboot, a late
start or a jump, "what should be showing" is just re-armed - the trigger
alone puts the garment right, since the picture was already there.

The same goes for a board that missed a trigger outright (it was not
powered up yet, say): a cue counts as on the garment once it fired, but
if a board that was not live then turns up later, the garment is
`dirty` until its slot is re-triggered - no rewrite, just the same
broadcast again (once per cue, so a board that is simply dead does not
make this repeat for ever).

Across a reboot the monotonic clock starts over, so T0 is also kept as
wall-clock time on disk. That is only good to within the reboot itself
(no RTC battery), which is why the restored player waits a few seconds
before acting: the PC, which watches every unit's T0, will have sent the
exact one by then if it can reach the unit at all. A restored T0 that
lies in the future cannot be right - nothing starts a show more than a
minute ahead - and is not run on.

A standalone demo (ui/demos.py, load(demo=True)) is restored by those
very same rules, as a demo: `demo`, `demo_name`, `demo_slug` and
`demo_loop` are part of the run record on disk, so a unit that was
playing one when the power went comes back playing it - what a showroom
loop is for - and one that was merely loaded comes back LOADED, for the
next KEY1. The PC sees it as a demo again either way (status.show.demo),
which is what keeps the conductor's supervision from adopting it as its
own. The run record is written BEFORE show.json (see _persist()), so a
power cut inside a load() can lose the show file but never pair the
previous show's record with the new one.

The burn is a property of the LOADED SHOW, not of the session (review
finding F1, 2026-09-25): status()["burn"] is never None for a loaded
show, and its "state" is one of

    "burning"    the pictures are being written now (done/total)
    "burned"     every picture is in its slot - run()/preset() go ahead
    "failed"     the burn walked its whole list and some pairs are not
                 written (failed = [[board, slot], ...]): a board absent
                 at the time is a known gap and passes; a live board
                 that refused is refused back ("did not take the burn")
                 unless run() or preset() is given force=True. A garment
                 with no power at all lands here too, every pair absent,
                 with "reason": "none of its 16 boards answered" - so
                 one dark unit never holds the other nine out of the
                 show (2026-09-25)
    "cancelled"  the burn never walked its whole list, so nothing says
                 what is in which slot: STOP, the port taken by a local
                 pattern, no serial port, a busy bus, a setup cut short.
                 "reason" carries which of those (absent for the
                 operator's own STOP) - Upload again, and force does
                 NOT pass it
    "none"       nothing burned for THIS show since the unit started (a
                 restart after Upload, or a burn that never began) -
                 Upload again

Two more keys appear only when they apply: "reason" (above) and
"record": "unsaved: <err>" - the burn finished but the disk would not
take the record, so a restart will come back "none" (the PC's tile says
so; the write is retried on the next load(), not on every poll).

A finished burn is recorded on disk (BURN_FILE, beside show-run.json)
and restore() reads it back only when it names the restored show; the
record is removed by load() BEFORE the new show file is written, so a
restart in the middle of a burn - even of the very same show - can
never come back as "burned".
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from conductor import durable      # standard library only, nothing else of the Conductor
from .remote import ARMED, DEFAULT_SLOT, FAILED, FIRED, PREPARING, READY, RemoteError

STORE = Path.home() / ".epaper"
# A show's own cues take 1..18: 19 (DEFAULT_SLOT) stays the manual
# Prepare / demo one-shot slot, and 0 is the standby white (contract
# update, 2026-09-24 - boards hold slot_capacity == 20 slots, 0..19).
SHOW_SLOT_MIN, SHOW_SLOT_MAX = 1, DEFAULT_SLOT - 1
# Pre-burn (docs/MERIS_REPLY_3SLOT.pdf, 2026-09-24): every cue's picture is
# written into its own slot at /show/load time (RemoteSession.burn()), so
# RUNNING a show is triggers only - a broadcast "show slot N", nothing to
# write. SAVE_S_PER_BOARD is what one board's 0x13 costs over the 9600 bps
# relay and CLEAR_S_PER_BOARD what the 0x25 beside it costs on a FIRST
# burn (review finding F5: every (board, slot) gets its pipeline cleared
# or its table written once) - together the anchor of the burn estimate
# the docs quote: 36 boards x 10 cues = 360 pictures ~ 112 s, x 18 cues
# ~ 195 s, and the same show again ~ 0 s (nothing is written twice).
# tests/test_showplay.py times a fake burn of exactly that size against
# it; nothing here schedules by it any more.
SAVE_S_PER_BOARD = 0.25    # a little over the measured 0.22 s
CLEAR_S_PER_BOARD = 0.06   # the 0x25 / 0x1F beside it, first burn only
RESTORE_GRACE_S = 6.0      # let the PC correct a restored T0 first
RESTORE_AHEAD_S = 90.0     # a restored T0 further ahead than this is junk
RESTORE_OVER_S = 60.0      # past its end by this much: that show is over
CATCH_UP_LEAD_S = 0.3
RETRY_AFTER_FAILED_S = 3.0
END_SLACK_S = 30.0
# How long a STOP waits before it takes the show's pictures out of the slots
# (`clear_after_show`). STOP is also how a director aborts a show mid-way -
# the ordinary reason to press it - and clearing cannot be undone without a
# three-minute Upload, so the operator gets a window in which a START (or a
# PRESET) takes it back. The natural END of the show does NOT wait: it is over
# either way. The conductor has the same window (conductor/fleet.py's
# CLEAR_AFTER_STOP_S) and normally gets there first; this is the unit's own
# copy, for a STOP pressed with the PC already gone (KEY2 on the unit).
CLEAR_AFTER_STOP_S = 30.0

LOADED, RUNNING, HOLDING, STOPPED, ENDED = (
    "loaded", "running", "holding", "stopped", "ended")
DELAY_UNIT_MS = 10        # conductor/showfile.py's DELAY_UNIT_MS (10 ms frames)
BURN_FILE = "show-burn.json"   # {"burned": show id, "when", "state", "total", "failed"}


def validate_show(show: dict) -> None:
    """What `ShowPlayer.load()` needs a show file to have - also used by
    ui/demos.py so a bad show is refused at /demo/save, not at KEY1."""
    if not isinstance(show, dict) or not show.get("id"):
        raise RemoteError("the show has no id")
    cues = show.get("cues")
    if not isinstance(cues, list) or not cues:
        raise RemoteError("the show has no cues")
    try:
        float(show["refresh_s"]), float(show["duration"])
    except (KeyError, TypeError, ValueError):
        raise RemoteError("the show needs refresh_s and duration")
    unit_ms = show.get("delay_unit_ms")
    if unit_ms is not None and unit_ms != DELAY_UNIT_MS:
        raise RemoteError(f"this unit's delay tables are "
                          f"{DELAY_UNIT_MS} ms frames; the show says "
                          f"{unit_ms!r}")
    for cue in cues:
        if not isinstance(cue, dict):
            raise RemoteError("a cue must be an object")
        for key in ("id", "sent", "boards", "state"):
            if key not in cue:
                raise RemoteError(f"cue without {key}")
    # The pre-burn redesign (2026-09-24): a cue with no slot cannot be
    # burned, and this unit no longer knows how to play a show any other
    # way - conductor/showfile.py always assigns one now. "slot_capacity"
    # (boards hold slots 0..19) is how a burned show file identifies
    # itself; an old file carries neither and gets the same refusal.
    # Cues use 1..SHOW_SLOT_MAX only: SHOW_SLOT_MAX+1 (19) stays the
    # manual Prepare / demo one-shot slot, so a show's own burn can never
    # be overwritten by one, and slot 0 is the standby white.
    if "slot_capacity" not in show or any("slot" not in cue for cue in cues):
        raise RemoteError("show file has no slots - update the conductor")
    for cue in cues:
        try:
            slot = int(cue["slot"])
        except (TypeError, ValueError):
            raise RemoteError(f"cue {cue.get('id', '?')}: bad slot "
                              f"{cue['slot']!r}")
        if not SHOW_SLOT_MIN <= slot <= SHOW_SLOT_MAX:
            raise RemoteError(f"cue {cue.get('id', '?')}: slot {slot} must "
                              f"be {SHOW_SLOT_MIN}-{SHOW_SLOT_MAX} "
                              f"({DEFAULT_SLOT} is the manual slot, 0 is "
                              f"the standby)")


def show_boards(show: dict) -> "list[int]":
    """The garment this show was built for.

    conductor/showfile.py writes the unit's own board list into the show
    (`boards`), and every cue carries an entry for every one of those
    addresses - so an older file with no list of its own is read from
    its cues instead. This is what the unit is told to stop probing
    outside of (ui/remote.py's set_boards()).
    """
    addresses = set()
    for value in show.get("boards") or []:
        try:
            addresses.add(int(value))
        except (TypeError, ValueError):
            continue
    if addresses:
        return sorted(addresses)
    for cue in show.get("cues") or []:
        if not isinstance(cue, dict):
            continue
        for key in ("state", "boards"):
            for value in (cue.get(key) or {}):
                try:
                    addresses.add(int(value))
                except (TypeError, ValueError):
                    continue
    return sorted(addresses)


class ShowPlayer:
    def __init__(self, session, store: "Path | None" = STORE,
                 clock=time.monotonic, wall=time.time,
                 save_s=None, margin_s=None,
                 grace_s: float = RESTORE_GRACE_S, tick_s: float = 0.2,
                 setup_s=None, setup_board_s=None,
                 retry_s: float = RETRY_AFTER_FAILED_S,
                 clear_after_stop_s: float = CLEAR_AFTER_STOP_S):
        # save_s / margin_s / setup_s / setup_board_s: the live-write
        # design's lead-time knobs, still accepted so existing callers
        # (ui/app.py, the tests) need not change, but nothing is timed
        # by them any more - a trigger needs no lead (pre-burn).
        self.session = session
        # A bus recovery reopens the serial port, so it must never run
        # inside a run - not even between two cues a minute apart. This
        # is the only object that knows there is one, so it says so here
        # rather than leave every caller to wire it up (ui/remote.py's
        # `playing`, ui/runner.py's _recover_quiet()). A HOLD counts: the
        # director is standing in the middle of the show either way.
        session.playing = lambda: self.state in (RUNNING, HOLDING)
        self.store = Path(store) if store else None
        self._clock, self._wall = clock, wall
        self.grace_s, self.tick_s = grace_s, tick_s
        self.retry_s = retry_s
        # The window a STOP's clear waits out before it goes (see
        # CLEAR_AFTER_STOP_S). A knob so the tests can compress it, like
        # every other wait on this player.
        self.clear_after_stop_s = clear_after_stop_s

        self.show: "dict | None" = None
        self.state = STOPPED
        self.t0: "float | None" = None
        self.synced = False            # T0 came from the PC, not from disk
        # A show loaded with load(show, demo=True) - ui/demos.py's stored
        # standalone shows, played from the unit's own menu. Running one
        # is identical to running a PC-driven show; the flag is what the
        # PC (status.show.demo) and the LCD read to know whose show this
        # is, and restore() brings it back with the show (see there).
        self.is_demo = False
        self.demo_name = ""            # the name it was written under
        self.demo_slug = ""            # ui/demos.py's slug: which row it is
        self.demo_loop = False         # ...and whether it plays again
        self.applied: "str | None" = None      # cue id on the garment now
        self.dirty = False             # some board does not show `applied`
        self.note = ""
        # Boards presumed to show `applied` - every LIVE board at the
        # moment its trigger last went out (a broadcast has no per-board
        # ACK to check instead). One that was not live then and joins
        # later is what makes `dirty` true (see _check_dirty()).
        self._ever_ok: "set[int]" = set()
        self._counted: "str | None" = None     # session key already tallied
        # The last landing check the unit did (ui/runner.py), as last
        # written to the run record. A cue that could not be confirmed
        # is about a garment that may still be showing the wrong
        # picture, and that outlives this process - so it is persisted
        # and restored, not only held in the session.
        self._verify_seen: "dict | None" = None
        # The burn belongs to a show (module docstring): the session's
        # burn counts for the loaded show only while _burn_id names it;
        # _burn_disk is a finished burn read back by restore() (paired by
        # id there); _burn_saved is what is already recorded on disk.
        self._burn_id: "str | None" = None
        self._burn_disk: "dict | None" = None
        self._burn_saved = None
        # A burn record the disk would not take: carried in the burn
        # dict ("record": "unsaved: <err>") rather than in `note`, which
        # status() reads before the burn and run() clears, so the PC saw
        # it a poll late or not at all (review round 2, 2026-09-25). Not
        # retried on every poll - the next load() tries again.
        self._burn_record_error: "str | None" = None
        self._burn_none_why = "(the burn never started)"
        # The show asked for its pictures to be deleted once it is over
        # (show.json's `clear_after_show`, carried in the show file):
        # which show id a clear has already been asked for, so the ENDED
        # tick asks once and not every 200 ms. Cleared by load() and by
        # run().
        self._clear_asked: "str | None" = None
        # ...and the hand-off from _plan() (which holds the lock) to
        # _loop(), which does the asking outside it - clear() may have to
        # take the port, exactly like _send().
        self._clear_due = False
        # When a STOP's clear becomes due (CLEAR_AFTER_STOP_S), on the
        # monotonic clock, or None when none is armed. A START or a PRESET
        # inside that window takes it back.
        self._clear_at: "float | None" = None
        # restore() found this unit in the middle of a show: ui/main.py
        # must not paint the standby white over the picture the garment
        # is still holding (real unit, 2026-09-25 - 16 s of probing and
        # then WHITE, mid-show, before the show came back).
        self.restored_running = False
        # ...and WHICH show that was. ui/main.py reads restored_running
        # once at start-up and nothing ever clears it, so the LCD's own
        # "is the PC's show on the garment right now?" (ui/app.py's
        # _pc_show_wins) pairs it with the id: a show restored onto the
        # garment but sitting LOADED - held, or waiting for the PC to
        # send a T0 - is still the PC's, and a demo must not paint over
        # it. load() and stop() are the two ways it stops being true.
        self.restored_id: "str | None" = None
        self._retry_at = 0.0
        self._not_before = 0.0
        # Bumped by every command. _send() runs without the lock (it may
        # wait for the port), so what _plan() decided can be overtaken by
        # a HOLD, a STOP or a new show; the epoch is how it notices, and
        # a cue is only ever armed under the lock in an unchanged epoch.
        self._epoch = 0
        self._run_no = 0               # part of the session key, see run()
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._quit = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    # ---- commands (from the agent) ----

    def load(self, show: dict, demo: bool = False, name: str = "",
             slug: str = "", loop: bool = False) -> None:
        validate_show(show)
        if self.session.busy():
            # Refused before anything changes: the previous show (and its
            # burn state) stays exactly as it was.
            raise RemoteError("unit is busy (firmware update, scan or reboot)")
        with self._lock:
            self._epoch += 1
            # A fresh run_no here too, not just in run()'s own "start from
            # the top" branch: the burn this triggers below always takes
            # real time (even a fast one leaves a window between load()
            # returning and the operator's next /show/run), and the
            # background loop keeps calling _tally() the whole time - a
            # FIRED session key left over from a PREVIOUS run of the same
            # show can share this one's cue ids, and without a bump here
            # its run_no would still match self._run_no, resurrecting
            # `applied` before this run ever sends anything of its own
            # (found reproducing "load(); wait for the burn; run()" back
            # to back on a fast bus, 2026-09-24).
            self._run_no += 1
            self._disarm()
            # The burn state follows the show: until burn() below has
            # started one for THIS show, the session's burn (the previous
            # show's "burned", say) must not read as this show's - so the
            # pairing is broken first, and the record on disk goes BEFORE
            # the new show file is written, so a restart in the middle of
            # the burn (even of the same show id) comes back "none", never
            # "burned" (review finding F1, 2026-09-25).
            self._burn_id, self._burn_disk, self._burn_saved = None, None, None
            self._burn_record_error = None       # one more try, this show
            self._burn_none_why = "(the burn never started)"
            # A fresh Upload is how the operator undoes a clear: nothing
            # of the last one may survive into this show's state. The
            # session's own clear is dropped by burn() below, which is
            # also what makes a clear queued a moment ago never run.
            self._clear_asked, self._clear_due = None, False
            self._clear_at = None            # ...and a STOP's armed window
            self._forget_burn_record()
            self.show = show
            self.is_demo = bool(demo)
            # The name it was written under (ui/demos.py), not show["name"]
            # (the look's own name from the timeline) - the PC's Units
            # tile labels a unit "demo: <name>" from /status.show. The
            # slug goes with it so that restore() can hand the LCD back
            # the very menu row this came from (ui/app.py's KEY1-hold and
            # its loop both work on the slug, not on the name).
            self.demo_name = str(name) if demo else ""
            self.demo_slug = str(slug) if demo else ""
            # Recorded with it, so that an adopted lap after a restart
            # does not depend on the menu row still being there.
            self.demo_loop = bool(loop) and demo
            # Whatever restore() put on the garment, this show replaces.
            self.restored_id = None
            self.state, self.t0, self.synced = LOADED, None, False
            self._forget_garment()
            self.session.forget_verify()     # the previous show's, not this one's
            self._verify_seen = None
            self.note = ""
            self._persist(with_show=True)
        # Which sockets this garment has, before anything is written and
        # whatever the burn below turns out to be: a burn every pair of
        # which the cache already holds writes nothing, and a demo's own
        # list is no different from a PC show's (ui/remote.py's
        # set_boards()).
        self.session.set_boards(show_boards(show))
        # Burn every cue into its own slot now, ahead of the show itself -
        # RUNNING sends only a trigger per cue, never a write (see the
        # module docstring and ui/remote.py's RemoteSession.burn()). Runs
        # on the runner's worker; status()["burn"] is the progress. If
        # this raises (the unit went busy just now) the new show stays
        # loaded with burn "none" - never paired with an older burn.
        self.session.burn(self._burn_items(show), int(show.get("dev_type", 3)))
        with self._lock:
            if self.show is show:
                self._burn_id = show["id"]
        self._wake.set()

    @staticmethod
    def _burn_items(show: dict) -> "list[dict]":
        """One item per cue, in cue order (preset first): the FULL
        picture (`state`, not the diff `boards`) - a slot is self
        contained, so a board left out of a cue's own change must still
        show the right colour once that slot is triggered."""
        return [{"slot": int(cue["slot"]),
                "boards": {int(a): bytes.fromhex(h)
                          for a, h in cue["state"].items()},
                "delays": {int(a): bytes.fromhex(h)
                          for a, h in (cue.get("delays") or {}).items()},
                # Not written anywhere: it is what the burn's own log
                # line says the sweep was ASKED for, so a board whose
                # last start falls short of it reads as "the farthest
                # scales are on another board" rather than a bug.
                "span_s": cue.get("span")}
               for cue in show["cues"]]

    def run(self, t0: float, show_id: "str | None" = None,
            force: bool = False) -> None:
        """`force`: the operator's "START anyway" - passes a burn that
        FAILED on a live board (the picture is missing there and they
        know it); never a burn still in progress, cancelled, or one
        this unit has no record of (see _burn_gate())."""
        with self._lock:
            if self.show is None:
                raise RemoteError("no show loaded")
            if show_id is not None and show_id != self.show["id"]:
                raise RemoteError(f"loaded show is {self.show['id']}, "
                                  f"not {show_id}")
            if self.session.busy():
                raise RemoteError("unit is busy (firmware update, scan "
                                  "or reboot)")
            self._take_back_the_pictures()
            self._burn_gate(force)
            self._epoch += 1
            self.t0, self.synced = float(t0), True
            self.state, self.note = RUNNING, ""
            self._not_before = 0.0
            if self._clock() < self.t0:
                # T0 ahead of now is a start from the top (RESUME and NEXT
                # land inside the show). A new run gets new session keys,
                # so nothing left in the session from the last run can
                # pass for this run's cue, and the garment is only taken
                # as known if it is cleanly showing the preset.
                self._run_no += 1
                first = self.show["cues"][0]["id"]
                if self.applied != first or self.dirty:
                    self._forget_garment()
            # A forward jump (a SEEK past due, or a big NEXT) can leave a
            # cue that was already loading/armed for the OLD T0 behind
            # the new one - its `sent` is now in the past. Left alone it
            # would just fire as scheduled (or the moment it is ready),
            # showing that skipped cue while the true current one waits
            # behind it (_plan()'s branch 2 is blocked by `owned_unfired`
            # until this fires). Disarming it clears fire_at, which is
            # exactly what lets that branch repaint the picture this new
            # T0 actually wants (owned_unfired requires a fire_at).
            session = self.session
            if (self._owns(session.cue_id)
                    and session.phase in (PREPARING, READY, ARMED)):
                cue_id = self._parse(session.cue_id)
                cue = next((c for c in self.show["cues"]
                           if c["id"] == cue_id), None)
                if cue is not None and cue["sent"] <= self._clock() - self.t0:
                    self._disarm()
            # A start is a new fire onto boards nobody is vouching
            # for: the last run's landing verdict says nothing about it,
            # must not hang over this run's tile in red, and must not let
            # the unit skip checking a cue id this run repeats (a
            # one-cue show, a looping demo - review round 3).
            self.session.forget_verify()
            self._verify_seen = None
            self._persist()
        self._wake.set()

    def preset(self, force: bool = False) -> None:
        """Put the first cue's picture up now, before the start.

        Through the player rather than as a loose cue, so that it knows
        the preset is on the garment and START does not repaint it.
        `force` as in run().
        """
        with self._lock:
            if self.show is None:
                raise RemoteError("no show loaded")
            if self.state == RUNNING:
                raise RemoteError("the show is running")
            if self.session.busy():
                raise RemoteError("unit is busy (firmware update, scan "
                                  "or reboot)")
            self._take_back_the_pictures()
            self._burn_gate(force)
            self._epoch += 1
            self._run_no += 1
            show, first = self.show, self.show["cues"][0]
            self._forget_garment()
            # A preset repaints the garment, so the last check's verdict
            # is not about what is going up now - and a second PRESET of
            # the same cue must be checked on its own, not waved through
            # as "landed already" (ui/runner.py). Today the _run_no bump
            # above happens to make every preset a new session key, which
            # would do it too; this says it where it is meant rather than
            # leaning on that (review round 4).
            self.session.forget_verify()
            self._verify_seen = None
            self._persist()
            # No write to budget for any more - the picture is already
            # burned into its slot; a small margin only covers arm()
            # possibly waiting for the port (start_remote()).
            fire_at = self._clock() + CATCH_UP_LEAD_S
            epoch = self._epoch
        self._send(show, first, fire_at, epoch)   # may take the port
        self._wake.set()

    # ---- the clear after the show (show file's `clear_after_show`) ----

    def clear_after_show(self) -> bool:
        """Does this show ask for its pictures to be deleted once it is
        over? (conductor/showfile.py carries show.json's key into the
        show file; a file without it behaves exactly as before.)

        Never for a standalone DEMO. A demo plays from those very slots
        the next time somebody presses KEY1, so a menu row that empties
        itself after one play is not what "clear after the show" means -
        and the whole point of a demo is that it needs no PC to write
        them again.
        """
        show = self.show
        return (show is not None and not self.is_demo
                and bool(show.get("clear_after_show")))

    def clear_pictures(self, show_id: "str | None" = None) -> None:
        """Delete this show's pictures from slots 1-18 (the conductor's
        POST /show/clear, and this player's own end-of-show when the show
        file asks for it).

        Nothing is repainted: the garment keeps the last look it was
        shown for as long as it has power (the operator's rule,
        2026-09-27). Refused while a run is RUNNING or HOLDING - those
        slots are what the next trigger reads from.

        Asking twice is free: a show whose pictures are already gone
        returns without touching the bus, which is what lets the
        conductor send this on the run's END and again on STOP.
        """
        with self._lock:
            if self.show is None:
                raise RemoteError("no show loaded")
            if show_id is not None and show_id != self.show["id"]:
                raise RemoteError(f"loaded show is {self.show['id']}, "
                                  f"not {show_id}")
            if self.state in (RUNNING, HOLDING):
                raise RemoteError("stop the show first")
            if self.is_demo:
                raise RemoteError("this is a demo stored on the unit - "
                                  "clearing its slots would empty the menu "
                                  "row; delete the demo instead")
            burn, _ = self._burn_record_locked()
            if (burn or {}).get("state") == "cleared":
                return                          # already done
            if (self._clear_asked == self.show["id"]
                    and self.session.clear_record()["state"] == "clearing"):
                # One is already on the bus for this show. Asking twice is
                # normal - this unit's own STOP queues a clear and the
                # conductor's POST /show/clear lands a few ms behind it -
                # and starting over would abort that walk part way and
                # re-walk every pair, with the slots the first walk
                # already emptied answering NAK and reading as failures
                # (review, 2026-09-27).
                return
            self._clear_asked = self.show["id"]
            self._clear_due = False
            # The clear is about THIS show, so the record that comes out
            # of it is this show's. Said here because a show RESTORED
            # from disk has no LIVE burn paired with it (restore() never
            # re-burns), and without this the tile and both gates would
            # go on reading the "burned" on disk from before the clear.
            self._burn_id = self.show["id"]
        self.session.clear(range(SHOW_SLOT_MIN, SHOW_SLOT_MAX + 1))
        self._wake.set()

    def _take_back_the_pictures(self) -> None:
        """A START or a PRESET wants the slots again (lock held).

        Called BEFORE the burn gate on purpose. A clear that never began
        is dropped here and the gate then passes exactly as it always
        did; one that has already deleted a slot stops after the slot it
        is on and leaves the burn record "cleared", so the gate refuses -
        START must never run a garment half its pictures were taken from.

        A STOP's clear that is still inside its CLEAR_AFTER_STOP_S window
        has not begun at all, so this is simply the end of it: the
        pictures are untouched and the show runs again.
        """
        self.session.cancel_clear("a new run started")
        self._clear_asked, self._clear_due = None, False
        self._clear_at = None

    def _take_clear_due(self) -> bool:
        """Is a clear to be asked for right now? _plan() decides under the
        lock; _loop() does the asking outside it, because clear() may have
        to take the port (exactly like _send()).

        Two ways one comes due: the show ENDED (immediate - it is over
        either way), or a STOP armed one and its window has run out.
        """
        with self._lock:
            due, self._clear_due = self._clear_due, False
            if not due and self._clear_at is not None:
                if self._clock() >= self._clear_at:
                    self._clear_at = None
                    due = True
            return due

    def _clear_wait(self) -> "float | None":
        """Seconds until an armed STOP clear is due, for the loop's own
        wait - so it asks AT the window's end rather than a tick later."""
        with self._lock:
            if self._clear_at is None:
                return None
            return max(0.0, self._clear_at - self._clock())

    def _clear_when_over_locked(self) -> None:
        """The show has just reached ENDED: queue the clear if this show
        asked for one and it has not been asked for already (a restored
        show whose pictures are gone comes back "cleared" and the ask is
        a no-op anyway)."""
        if not self.clear_after_show():
            return
        if self._clear_asked == self.show["id"]:
            return
        self._clear_due = True

    def _burn_gate(self, force: bool = False) -> None:
        """Refuse to run/preset unless the loaded show's pictures are in
        their slots - called with the lock held. Only "burned" passes
        outright; "failed" passes when every board involved is absent
        (a known gap - it never gets a picture anyway), or with `force`
        when a LIVE board refused the write (the operator's "START
        anyway"). `force` never passes "burning", "cancelled", "none",
        or a burn the bus gave up on part way (nothing says what was
        never reached).

        A clear after the show is its own refusal, ahead of the burn's:
        the pictures were deliberately taken out of the slots and the
        only way back in is an Upload - `force` never passes it."""
        clear = self.session.clear_record()
        if clear.get("state") == "clearing":
            raise RemoteError(f"the pictures are being deleted after the "
                              f"show ({clear['done']}/{clear['total']}) - "
                              f"Upload again")
        burn, complete = self._burn_record_locked()
        state = burn["state"]
        if state == "burned":
            return
        if state == "cleared":
            why = burn.get("reason")
            raise RemoteError("pictures were cleared after the last show"
                              + (f" ({why})" if why else "")
                              + " - Upload again")
        if state == "burning":
            raise RemoteError(f"still writing the pictures: "
                              f"{burn['done']}/{burn['total']}")
        if state == "cancelled":
            why = burn.get("reason")
            raise RemoteError("the pictures were not written "
                              f"(cancelled{': ' + why if why else ''}) - "
                              "Upload again")
        if state == "none":
            raise RemoteError(f"pictures not written {self._burn_none_why} - "
                              f"Upload again")
        if state != "failed":
            raise RemoteError(f"the pictures are not written ({state}) - "
                              f"Upload again")
        if not complete:
            why = self.session.error or self.session.runner.error or "bus error"
            raise RemoteError(f"the burn did not finish ({why}) - Upload again")
        absent = self.session.runner.absent_snapshot()
        stuck = sorted({b for b, s in burn["failed"] if b not in absent})
        if stuck and not force:
            names = ",".join(str(b) for b in stuck)
            raise RemoteError(f"board {names} did not take the burn - "
                              f"Upload again")

    def _burn_total(self, show: dict) -> int:
        return sum(len(cue["state"]) for cue in show["cues"])

    def _burn_record_locked(self) -> "tuple[dict | None, bool]":
        """(status()["burn"], whether that burn ran to its end) for the
        loaded show - None only with no show loaded. A finished burn is
        recorded on disk here, the first time it is seen (restore()
        reads it back)."""
        show = self.show
        if show is None:
            return None, False
        live, complete = self.session.burn_record()
        if live is not None and self._burn_id == show["id"]:
            if complete:
                self._persist_burn(show["id"], live)
            live = self._fresh_reason(live)
            if self._burn_record_error:
                live = dict(live, record=self._burn_record_error)
            return live, complete
        disk = self._burn_disk
        if disk is not None and disk["show"] == show["id"]:
            back = {"done": disk["total"], "total": disk["total"],
                    "failed": [list(pair) for pair in disk["failed"]],
                    "state": disk["state"]}
            if disk.get("reason"):
                back["reason"] = disk["reason"]
            return self._fresh_reason(back), True
        return ({"done": 0, "total": self._burn_total(show), "failed": [],
                 "state": "none"}, False)

    def _fresh_reason(self, burn: dict) -> dict:
        """"none of its 16 boards answered" stops being true the moment
        those boards answer - the feed was switched on, they are back,
        and they hold whatever was written before. The pairs are still
        not written (nothing has re-burned them), but the sentence must
        go, or the PC keeps offering "that garment keeps whatever it
        shows" about a garment that is now awake (R5, review round 3).
        """
        if burn.get("state") != "failed" or not burn.get("reason"):
            return burn
        absent = self.session.runner.absent_snapshot()
        if all(pair[0] in absent for pair in burn["failed"]):
            return burn
        burn = dict(burn)
        burn.pop("reason")
        return burn

    def hold(self) -> None:
        with self._lock:
            self._epoch += 1
            if self.state == RUNNING:
                self.state = HOLDING
                self._disarm()
                self._persist()
        self._wake.set()

    def stop(self) -> None:
        was_running = False
        with self._lock:
            self._epoch += 1
            was_running = self.state in (RUNNING, HOLDING, ENDED)
            if self.state in (RUNNING, HOLDING):
                self._disarm()
            if self.show is not None:
                self.state = STOPPED
            self.t0 = None
            # The PC (or KEY2) has let go: whatever restore() brought
            # back is no longer something anyone is driving, so it stops
            # holding a demo out of the menu (ui/app.py's _enter_demo).
            self.restored_id = None
            # Let go of the last cue's verdict with the run itself (the
            # same reasoning as run()'s).
            self.session.forget_verify()
            self._verify_seen = None
            self._persist()
        self.session.cancel_burn()   # give up on a burn still in flight
        # STOP is the other end of the show, and the one the operator
        # actually presses before unplugging a garment: if this show asked
        # for its pictures to come back out of the slots, this is where it
        # is ARMED (HOLD deliberately does not - the show is not over).
        #
        # Armed, not done: STOP is also how a director aborts a show
        # half way through, and clearing cannot be undone without a
        # three-minute Upload. CLEAR_AFTER_STOP_S later _plan() asks for
        # it, and a START or a PRESET inside that window takes it back.
        if was_running and self.clear_after_show():
            with self._lock:
                self._clear_at = self._clock() + self.clear_after_stop_s
        self._wake.set()

    def close(self) -> None:
        self._quit.set()
        self._wake.set()
        self._thread.join(timeout=2)

    @property
    def running(self) -> bool:
        return self.state == RUNNING

    # ---- disk ----

    def _write(self, name: str, payload) -> None:
        """Whole or not at all: the power cut restore() exists for must
        not find half a file - nor an empty one, so fsynced before and
        after the rename (conductor/durable.py)."""
        durable.atomic_write_json(self.store / name, payload)

    def _persist_burn(self, show_id: str, burn: dict) -> None:
        """Record a finished burn ("burned", or "failed" with its pairs)
        of `show_id`, once - what restore() pairs with the show file."""
        if self.store is None:
            return
        key = (show_id, burn["state"], burn.get("reason"),
               tuple(tuple(pair) for pair in burn["failed"]))
        if key == self._burn_saved:
            return
        try:
            self.store.mkdir(parents=True, exist_ok=True)
            self._write(BURN_FILE, {
                "burned": show_id, "when": self._wall(),
                "state": burn["state"], "total": burn["total"],
                "reason": burn.get("reason"),
                "failed": [list(pair) for pair in burn["failed"]]})
            self._burn_saved = key
            self._burn_record_error = None
        except OSError as exc:
            # Said in the burn dict, which the PC's tile shows, and not
            # tried again until the next load(): _burn_record_locked()
            # runs on every poll, and a full disk would have it fail
            # (slowly) every second (review round 2, 2026-09-25).
            self._burn_saved = key
            self._burn_record_error = f"unsaved: {exc}"

    def _forget_burn_record(self) -> None:
        if self.store is None:
            return
        try:
            (self.store / BURN_FILE).unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            self.note = f"cannot clear the burn record: {exc}"

    def _read_burn_record(self, show: dict) -> "dict | None":
        """The finished burn on disk, if it names this show."""
        try:
            record = json.loads((self.store / BURN_FILE)
                                .read_text(encoding="utf-8"))
            if record.get("burned") != show["id"]:
                return None
            state = record.get("state", "burned")
            # "cleared" too: a unit restarted after the show must come
            # back still knowing its slots were emptied, or START would
            # wave a garment with no pictures through (the pictures are
            # what survives a power cycle; the knowledge that they do not
            # any more has to survive it as well).
            if state not in ("burned", "failed", "cleared"):
                return None
            failed = [(int(b), int(slot)) for b, slot in record.get("failed", [])]
            if state == "failed" and not failed:
                return None
            return {"show": show["id"], "state": state,
                    "total": int(record.get("total", self._burn_total(show))),
                    "reason": record.get("reason"), "failed": failed}
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            # Empty or torn (a power cut under an older version): no
            # record, said once per file version, not on every poll.
            durable.note_unreadable(self.store / BURN_FILE, exc)
            return None
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None

    def _persist(self, with_show: bool = False) -> None:
        if self.store is None:
            return
        try:
            self.store.mkdir(parents=True, exist_ok=True)
            # The run record goes FIRST, even on a load. A power cut
            # between the two files then leaves a record that names the
            # NEW show beside the OLD show.json, which restore() reads as
            # "this record says nothing about this show file" and takes
            # as a plain PC show. The other order could leave the
            # PREVIOUS show's record - `demo: true` and all - paired with
            # a freshly loaded PC show that happens to carry the same id
            # (a demo written from the very show the PC is running), and
            # the unit would come back guarding a demo nobody started.
            try:
                self._write("show-run.json", {
                    "show": self.show["id"] if self.show else None,
                    "state": self.state, "applied": self.applied,
                    # What the show IS, not just what it holds: a demo
                    # comes back as a demo after a restart (restore()),
                    # so the LCD owns it again and the PC keeps leaving
                    # it alone.
                    "demo": self.is_demo, "demo_name": self.demo_name,
                    "demo_slug": self.demo_slug, "demo_loop": self.demo_loop,
                    # The unit's own verdict on the last cue it sent
                    # (ui/remote.py's RemoteSession.verify). A restart
                    # must not turn a red "not applied" into a blank
                    # while the garment is still wrong.
                    "verify": self._verify_seen,
                    # T0 as wall time: what survives a reboot.
                    "t0_wall": (None if self.t0 is None else
                                self._wall() + (self.t0 - self._clock()))})
            except OSError:
                # The record could not be replaced, so the one on disk is
                # the PREVIOUS show's - and with show.json about to be
                # replaced (or already holding that older show), a
                # restart would read the two as a matched pair and
                # restore, say, yesterday's demo over a PC load that has
                # since happened. No record at all is the honest state:
                # restore() bails on it and the unit comes up with
                # nothing loaded, waiting for the PC.
                self._forget_run_record()
                raise
            if with_show and self.show is not None:
                self._write("show.json", self.show)     # only when it changes
        except OSError as exc:
            self.note = f"cannot save the show: {exc}"

    def _forget_run_record(self) -> None:
        try:
            (self.store / "show-run.json").unlink()
        except OSError:
            pass                # missing, or a disk that will not have it

    def _read_state(self, name: str) -> "dict | None":
        """One of restore()'s two files as a dict, or None: missing (a
        unit that never loaded a show), or there but empty or torn - a
        power cut under a version before conductor/durable.py - which is
        the same "no record" plus one log line, never a crash at boot."""
        path = self.store / name
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except ValueError as exc:            # JSON, or not UTF-8 at all
            durable.note_unreadable(path, exc, "restored as nothing loaded")
            return None
        if not isinstance(data, dict):
            durable.note_unreadable(path, "not a JSON object",
                                    "restored as nothing loaded")
            return None
        return data

    def restore(self) -> None:
        """At start-up: pick the show up again if it was running.

        A standalone demo (load(demo=True)) comes back as a demo - the
        flag, the name and the slug are all part of the run record - and
        follows exactly the same rules from there, so the LCD can own it
        again (ui/app.py adopts a resumed one onto the DEMO screen) and
        the PC keeps leaving it alone.
        """
        if self.store is None:
            return
        try:
            show = self._read_state("show.json")
            run = self._read_state("show-run.json")
            if show is None or run is None:
                return
            duration = float(show["duration"])
            float(show["refresh_s"]), show["cues"][0]["sent"]
            if not isinstance(run.get("t0_wall"), (int, float, type(None))):
                return
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            return
        with self._lock:
            self.show = show
            self.state = LOADED
            # The garment's own board list, whatever state this show
            # comes back in: it is this unit's show again, and nothing
            # else will say so - restore() never re-burns, so no job
            # carries the list (radxa-04, 2026-09-26: it came back
            # mid-show and spent the rest of it probing the six empty
            # sockets its standby discovery had found). The worker takes
            # it when the first cue is armed (ui/remote.py's
            # set_boards()).
            self.session.set_boards(show_boards(show))
            # Only a run record that NAMES this show file says anything
            # about it. One that names another show is a load() that was
            # cut in half (show-run.json written, show.json not yet): the
            # record describes the show the unit was ASKED to load, while
            # show.json still holds the one before it, and neither
            # vouches for the other. Taken as a PC show, which is the
            # safe way round: the conductor may then load over it, where
            # a wrongly-restored demo would have it left alone for ever.
            named = run.get("show") == show.get("id")
            self.is_demo = bool(run.get("demo")) and named
            self.demo_name = run.get("demo_name", "") if self.is_demo else ""
            self.demo_slug = run.get("demo_slug", "") if self.is_demo else ""
            self.demo_loop = bool(run.get("demo_loop")) and self.is_demo
            # The burn state comes back only from the record that names
            # this very show; anything else is "none" and run() refuses
            # until the PC uploads again (restore() never re-burns).
            self._burn_id, self._burn_saved = None, None
            self._burn_disk = self._read_burn_record(show)
            self._burn_none_why = "since this unit restarted"
            if not named:
                return
            # What the unit last made of its own broadcast, back into the
            # session so /status (and the PC's tile) says it again. Only
            # from a record that names this very show: a verdict about
            # another show's cue says nothing about this garment.
            verify = run.get("verify")
            if isinstance(verify, dict) and verify.get("landed"):
                self._verify_seen = dict(verify)
                self.session.verify = dict(verify)
            # A demo comes back AS a demo (radxa-05, 2026-09-26: it used
            # to come back as a plain PC show, and from then on KEY1 on
            # every demo row was refused - "PC show loaded - use the PC" -
            # until someone pressed STOP on the PC). It takes the very
            # same path a PC-driven show takes from here: one that was
            # RUNNING when the power went resumes on its own, which is
            # what a showroom loop is for, and one that was merely loaded
            # or stopped comes back LOADED, for KEY1 to start again.
            t0_wall = run.get("t0_wall")
            t0 = (None if t0_wall is None
                  else self._clock() + (t0_wall - self._wall()))
            # "Still on" is the same test for RUNNING and for HOLDING,
            # and it decides whether ui/main.py skips its start-up
            # standby: a run whose end is long past - last night's show,
            # the Pi power-cycled this morning - is NOT mid-show, and
            # the white standby is exactly what should happen then. This
            # used to be set before the staleness tests, which left the
            # wall on the finale for ever (R1, review round 3).
            still_on = (t0 is not None and self._burn_disk is not None
                        and self._clock() - t0 <= duration + RESTORE_OVER_S)
            if run.get("state") == HOLDING and still_on:
                # Held, not running: nothing is going to move that
                # picture, and it is a picture of this show.
                self.restored_running = True
                self.restored_id = show.get("id")
            if run.get("state") != RUNNING or t0_wall is None:
                return
            if self._burn_disk is None:
                # RUNNING on disk but no finished burn recorded for this
                # show (run() records one before it ever runs, so this is
                # a crash in that window, or a disk that would not take
                # the record): not resumed on pictures nobody vouches for.
                self.note = ("restarted with no record of its pictures - "
                             "waiting for the PC")
                return
            if not still_on:
                return                  # that show is long over
            # Genuinely mid-show: keep the garment as it is, even if the
            # T0 below turns out to need the PC's help.
            self.restored_running = True
            self.restored_id = show.get("id")
            if t0 - self._clock() > RESTORE_AHEAD_S:
                # No RTC: the wall clock came up behind. Running on this T0
                # would sit out the show waiting for a start that is past.
                self.note = "restored T0 is in the future - waiting for the PC"
                return
            self.t0, self.synced = t0, False
            self.state = RUNNING
            self._forget_garment()      # unknown after a restart: send state
            self.note = "restored after restart"
            self._not_before = self._clock() + self.grace_s
            behind = [c for c in show["cues"] if c["sent"] <= self._clock() - t0]
            cue = (behind[-1] if behind else show["cues"][0])["id"]
            # On the unit's own log and the PC's tile: this is the line
            # that says the white standby was skipped on purpose.
            self.session.runner.emit(f"resumed the "
                                     f"{'demo' if self.is_demo else 'show'} "
                                     f"after a restart: cue {cue}, no standby")
        self._wake.set()

    # ---- the one rule ----

    def _forget_garment(self) -> None:
        self.applied, self.dirty = None, False
        self._ever_ok = set()
        self._counted = None

    def _lead(self, cue: "dict | None" = None,
              after_another: bool = False) -> float:
        """Kept for ui/app.py's KEY1 handler (_start_demo_show() /
        _loop_demo_show() add this to their own margin before their own
        run()): the pre-burn redesign needs no per-cue write-time
        estimate here any more, since nothing is written while RUNNING -
        this is just arm()'s own small margin (see preset()). A freshly
        load()ed show still needs its burn to finish before run()
        succeeds ("still writing the pictures") - ui/app.py's
        _await_demo_burn() waits for that before it calls run()."""
        return CATCH_UP_LEAD_S

    def _disarm(self) -> None:
        """Take the fire time off whatever this show has in the session -
        loading as much as armed: a cue still arming already carries its
        time and would fire the moment it is ready."""
        session = self.session
        if (self._owns(session.cue_id)
                and session.phase in (PREPARING, READY, ARMED)):
            session.cancel()

    def _owns(self, cue_id) -> bool:
        return bool(self.show) and str(cue_id or "").startswith(
            self.show["id"] + ":")

    def _key(self, show: dict, cue: dict) -> str:
        return f"{show['id']}:{self._run_no}:{cue['id']}"

    @staticmethod
    def _parse(key: str) -> str:
        """session key -> cue id."""
        return key.rsplit(":", 1)[1]

    def _run_no_of(self, key: str) -> "int | None":
        """The run number a session key was made under, by stripping the
        known "<show id>:" prefix rather than counting colons - a show
        id is never expected to contain one, but this way nothing breaks
        if it ever does."""
        if not self.show:
            return None
        prefix = self.show["id"] + ":"
        if not key.startswith(prefix):
            return None
        try:
            return int(key[len(prefix):].split(":", 1)[0])
        except ValueError:
            return None

    def _send(self, show: dict, cue: dict, fire_at: float, epoch: int,
              heal: bool = False) -> None:
        """Make the session hold this cue, timed for `fire_at`.

        Called WITHOUT the player's lock: arm() may take the serial port
        (start_remote(), which can wait seconds for a previous worker),
        and /status (the PC's poll, the LCD) must keep answering
        meanwhile. Nothing is written - the picture is already burned
        into the cue's slot - but giving the cue its time still happens
        under the lock, in the epoch it was decided in, since a HOLD or
        a STOP or a new show may have overtaken the decision meanwhile.

        `heal=True` is a re-arm of a cue that already fired (its own
        key would otherwise read as "already loaded, leave it alone") -
        arm() is called again regardless, which resets it to READY so
        the fire below always follows.
        """
        session = self.session
        key = self._key(show, cue)
        # Every start of driving comes through here - preset(), run(),
        # RESUME, NEXT, a heal - and arm() below is what takes the port
        # back after a KEY2/KEY1 detour, on a runner that has just gone
        # back to its own discovery. So the garment's list goes with it,
        # every time: it is idempotent (the worker only logs and
        # re-probes when it actually changes) and it costs no bus
        # traffic. Without it, Upload -> KEY2 -> KEY1 -> START put the
        # unit straight back into radxa-04's state (review, 2026-09-27).
        session.set_boards(show_boards(show))
        if heal or session.cue_id != key or session.phase == FAILED:
            session.arm(key, int(cue["slot"]),
                       int(show.get("dev_type", 3)), cue.get("label", ""),
                       # How long this cue needs after it fires, so the
                       # guard STOP waits for the sweep instead of
                       # landing inside it (ui/runner.py's _guard_for()).
                       # A show file from before cues carried a span
                       # says nothing and keeps the flat guard.
                       span_s=cue.get("span"),
                       refresh_s=cue.get("refresh_s", show.get("refresh_s")))
        with self._lock:
            if epoch != self._epoch:
                if session.cue_id == key:
                    session.cancel()        # decided before the command
                return
            if session.cue_id == key and (
                    session.phase in (PREPARING, READY) or (
                        session.phase == ARMED and session.fire_at is not None
                        and abs(session.fire_at - fire_at) > 0.001)):
                session.fire(key, fire_at)  # timed, or (re)timed: T0 moved

    def _tally(self) -> None:
        """What the session last fired is what is on the garment - noted
        whether or not the show runs (the preset comes first). A
        broadcast trigger has no per-board ACK, so every LIVE board at
        the moment it went out is presumed to now show it - see
        _check_dirty() for the only way that presumption gets revised."""
        session = self.session
        key = session.cue_id
        if (session.phase != FIRED or not self._owns(key)
                or key == self._counted
                # A FIRED cue left over from a run that has since been
                # restarted (run() bumps _run_no every time T0 starts a
                # fresh top) must not be mistaken for this run's - most
                # visibly on a one-cue show, where the last cue of the
                # old run and the first of the new one are the same id
                # and _forget_garment() has just cleared `applied` and
                # `_counted`, so nothing else here tells them apart.
                or self._run_no_of(key) != self._run_no):
            return
        self._counted = key
        self.applied = self._parse(key)
        self._ever_ok = set(session.runner.live)
        self.dirty = False
        if self.note.startswith("board "):
            self.note = ""

    def _check_dirty(self) -> None:
        """A board that was not live when the applied cue's trigger last
        went out cannot be trusted to be showing it - it missed the
        broadcast outright (there is no queue to catch up from) and
        needs it resent; no rewrite, the picture is already in its
        flash (see the heal branch in _plan())."""
        if self.applied is None:
            return
        joined = sorted(set(self.session.runner.live) - self._ever_ok)
        if joined:
            first_look = not self._ever_ok
            self._ever_ok |= set(joined)
            self.dirty = True
            if first_look:
                # Nothing was known about the garment yet (a restart, or
                # a run starting from the top): every board there "joined
                # late", which is not news and read as a fault on the
                # tile - the re-arm still happens (R9, review round 3).
                return
            named = ",".join(str(b) for b in joined[:3])
            if len(joined) > 3:
                named += f" +{len(joined) - 3} more"
            self.note = (f"board {named} joined late: "
                         f"re-arming {self.applied}")

    def _note_verify(self) -> None:
        """Keep the run record's copy of the landing check up to date.

        The verdict arrives on the runner's thread, whenever the check
        finishes; this is the tick that notices. Written only when it
        changes - once per cue - so the card costs nothing to keep
        honest.

        What it buys depends on what the restart interrupts: a show that
        comes back RUNNING has its next cue checked within seconds, and
        the restored verdict stands only until then. The one that
        matters is the show whose last cue was the one that went wrong -
        stopped, held, or simply loaded and waiting - where nothing else
        would ever say so again.
        """
        verify = self.session.verify
        if verify == self._verify_seen:
            return
        self._verify_seen = dict(verify) if verify else None
        self._persist()

    def _plan(self) -> "tuple[float, tuple | None]":
        """(seconds until it is worth looking again, what to send now)."""
        with self._lock:
            self._tally()
            if self.show is not None:
                self._note_verify()
            if self.state != RUNNING or self.show is None or self.t0 is None:
                return self.tick_s, None
            now_mono = self._clock()
            if now_mono < self._not_before:
                return min(self.tick_s, self._not_before - now_mono), None
            self._check_dirty()
            show, session = self.show, self.session
            cues = show["cues"]
            now = now_mono - self.t0

            past = [c for c in cues if c["sent"] <= now]
            ahead = [c for c in cues if c["sent"] > now]
            current = past[-1] if past else None
            nxt = ahead[0] if ahead else None
            # An owned cue with a fire_at set (ARMED, or still PREPARING
            # but already given one by _send()) carries a promise to fire
            # at that instant; arm()-ing any OTHER cue would displace it
            # outright (its cue_id, its fire_at), and the runner's own
            # session.fired() for the displaced cue would then find a
            # different cue_id and drop the tally with no error - the
            # cue is simply never shown (found in the timing review,
            # 2026-09-24, back when a cue's own write time could overrun
            # this same window; a trigger is instant, but a STOPPED or
            # HELD show, or the port still being taken by arm()'s own
            # start_remote(), can still leave one owned-and-promised for
            # a moment). A cue that is merely READY, or PREPARING with no
            # fire_at yet, has no such promise and is fine to preempt;
            # requiring fire_at is what tells the two apart.
            owned = self._parse(session.cue_id) if self._owns(
                session.cue_id) else None
            owned_unfired = (owned is not None
                             and session.phase in (PREPARING, READY, ARMED)
                             and session.fire_at is not None)
            duration = float(show["duration"])
            if nxt is None and now > duration + END_SLACK_S:
                # Over on the clock - even if the last cue's trigger never
                # reached a dead board and would be retried for ever.
                self._disarm()
                self.state = ENDED
                self._clear_when_over_locked()
                self._persist()
                return self.tick_s, None
            if (session.phase == FAILED and self._owns(session.cue_id)
                    and now_mono < self._retry_at):
                return self.tick_s, None        # not a tight re-arm loop

            action = None
            heal = False
            # `self.applied` (from _tally(), reading the session's FIRED
            # state) and `current` (from THIS tick's own clock read) are
            # two independent clocks - right on a cue boundary they can
            # disagree by a hair: the runner's _fire_at() may have just
            # fired `nxt` on its own slightly-later read of the clock,
            # so applied is already `nxt`'s id while this tick's `now`
            # still classifies `current` as the cue before it. Comparing
            # by each cue's own `sent` (order in the show), not id
            # equality, is what tells "genuinely behind" apart from
            # "already there, just a hair off" - id equality alone would
            # otherwise re-arm the cue that just fired one instant late,
            # right after its own successor already went out (found
            # chasing a one-cue-show-run-twice test firing 5 shows for a
            # 3-cue show instead of 3, 2026-09-24).
            applied_cue = next((c for c in cues if c["id"] == self.applied),
                               None)
            behind = (current is not None
                     and (applied_cue is None
                          or applied_cue["sent"] < current["sent"]))
            # 1. The garment must show `current` right now - either it
            #    never has (a start, a jump, a restart) or it did and a
            #    board joined late since (dirty). A trigger is instant,
            #    so this fires at `current`'s own instant - already past
            #    by definition of `current`, so it goes out at once and
            #    the delay from `now_mono` is what late_ms then reports.
            #    A heal reuses the very same key session.arm() would
            #    otherwise leave alone as "already loaded". Blocked only
            #    by a DIFFERENT owned unfired cue - re-deciding this same
            #    one's fire_at is how a HOLD/RESUME or a backward seek
            #    re-times a cue that is already armed for it, and unlike
            #    the old live-write design this fire_at is always the
            #    fixed t0 + current["sent"], never a moving "now + a
            #    lead", so redeciding it every tick cannot livelock.
            if (current is not None
                    and not (owned_unfired and owned != current["id"])
                    and (behind or self.dirty)):
                heal = self.applied == current["id"]
                self.dirty = False
                action = (show, current, self.t0 + current["sent"])
            # 2. Already showing the right thing, nothing due yet: arm
            #    the NEXT cue ahead of its own instant, so the worker's
            #    busy-wait (ui/runner.py's _fire_at, not this tick
            #    loop's own granularity) is what decides exactly when
            #    it fires. Blocked by a DIFFERENT owned cue still
            #    carrying a promise to fire - arm()-ing over it would
            #    displace it outright and the runner's own
            #    session.fired() for the displaced cue would then drop
            #    the tally with no error, the cue simply never shown
            #    (found in the timing review, 2026-09-24, back when a
            #    cue's own write could overrun this same window; nothing
            #    is written any more, but a STOPPED/HELD show, or arm()'s
            #    own start_remote() still taking the port, can still
            #    leave one owned for a moment). Re-deciding THIS SAME
            #    cue's fire_at is fine (same reasoning as branch 1's own
            #    fixed target). A cue that is merely READY, or PREPARING
            #    with no fire_at yet, has no promise at all and is fine
            #    to preempt outright; requiring fire_at is what tells
            #    the two apart.
            elif (nxt is not None
                    and not (owned_unfired and owned != nxt["id"])
                    and not (self.applied == nxt["id"] and not self.dirty)):
                action = (show, nxt, self.t0 + nxt["sent"])

            if (nxt is None and action is None and not owned_unfired
                    and current is not None and self.applied == current["id"]
                    and now > duration):
                self.state = ENDED
                self._clear_when_over_locked()
                self._persist()
            if action is not None and session.phase == FAILED:
                self._retry_at = now_mono + self.retry_s
            if action is None:
                return self.tick_s, None
            return self.tick_s, action + (self._epoch, heal)

    def _loop(self) -> None:
        while not self._quit.is_set():
            wait = 1.0
            try:
                wait, action = self._plan()
                if action is not None:
                    self._send(*action)
                # The show is over and asked for its pictures to go -
                # either it ENDED, or a STOP armed it and the window has
                # run out. Done here, outside the lock, because clear()
                # may have to take the port (exactly like _send()).
                if self._take_clear_due():
                    self.clear_pictures()
                else:
                    left = self._clear_wait()
                    if left is not None:
                        wait = min(wait, left)
            except RemoteError as exc:
                # A refused prepare (the session holds a cue about to
                # fire, or the unit is busy): look again next tick, not a
                # second later - a second is most of a cue's lead.
                self.note = str(exc)
                wait = self.tick_s
            except Exception as exc:        # noqa: BLE001 - never die mid-show
                self.note = f"player error: {exc}"
            self._wake.wait(wait)
            self._wake.clear()

    # ---- what the PC and the LCD read ----

    @property
    def loaded_here(self) -> bool:
        """True for a show this process load()ed - its burn is the
        session's own, `_burn_id` names it; False for one restore() read
        back from disk (state LOADED, burn "burned" from the record, but
        nobody has uploaded anything since this boot). The LCD's WIFI
        lock (ui/app.py's _wifi_locked) needs the difference: a PC that
        uploaded a show since the unit came up is waiting to START it,
        while a show restored after a power cycle would otherwise keep
        every unit that ever played one locked for good (review round 2,
        2026-09-30)."""
        with self._lock:
            show = self.show
            return show is not None and self._burn_id == show["id"]

    def status(self) -> "dict | None":
        with self._lock:
            if self.show is None:
                return None
            show = self.show
            out = {"id": show["id"], "name": show.get("name", ""),
                   "cues": len(show["cues"]), "state": self.state,
                   "t0": self.t0, "synced": self.synced,
                   "applied": self.applied, "dirty": self.dirty,
                   "note": self.note, "now": None, "next": None,
                   "duration": show.get("duration"),
                   # ui/demos.py's standalone shows: the conductor's own
                   # supervise()/_adopt() should leave one of these alone
                   # rather than mistake it for its own show; the Units
                   # tile labels a unit "demo: <name>" from demo_name.
                   # True again after a restart that resumed one - which
                   # is what keeps the conductor leaving it alone then
                   # too (conductor/fleet.py's _playing_demo()).
                   "demo": self.is_demo, "demo_name": self.demo_name,
                   # Which menu row it is: not used by the PC, but it is
                   # how /status says the restored demo identity is whole.
                   "demo_slug": self.demo_slug,
                   # "writing pictures n/N" (ui/remote.py's burn()) - what
                   # the PC gates START on and the LCD/Units tile show.
                   # Never None for a loaded show (module docstring).
                   "burn": self._burn_record_locked()[0],
                   # Whether THIS show asked for its pictures to come
                   # back out of the slots when it is over: what the unit
                   # believes, which is the show file it holds (the
                   # conductor sends POST /show/clear itself, so this is
                   # the unit's own fallback for a show that ends with
                   # the PC gone).
                   "clear_after_show": self.clear_after_show()}
            if self.t0 is not None and self.state in (RUNNING, ENDED):
                now = self._clock() - self.t0
                out["now"] = round(now, 2)
                ahead = [c for c in show["cues"] if c["sent"] > now]
                if ahead:
                    out["next"] = {"id": ahead[0]["id"],
                                   "label": ahead[0].get("label", ""),
                                   "in_s": round(ahead[0]["sent"] - now, 1)}
            return out
