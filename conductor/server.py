"""The conductor's local web UI: import the looks' CSVs, check and see them.

    python -m conductor serve            # http://127.0.0.1:8765

Standard library only, bound to localhost: this runs on the show PC and
nothing else should reach it. The one exception is EXHIBITION mode
(docs/SPECIFICATION.md "Exhibition mode", radxa/EXHIBITION.md), where the
Conductor runs headless on a unit that is also the Wi-Fi hotspot: `serve
--host 0.0.0.0 --speaker` opens it to the hotspot and plays the show's
music through mpg123 on that host. The page (conductor/web/index.html) reads
one JSON document, /api/state, rebuilt from the workspace folder on
every request - a dozen small CSVs parse in milliseconds, and it means
a file edited or dropped in by hand shows up on the next refresh.

The exhibition's show is authored on the same PC by a SECOND Conductor
(Start Exhibition Conductor.bat: `serve --workspace exhibition-data --port
8766 --label EXHIBITION`) - the same code, its own port, its own folder
and an amber EXHIBITION badge on its page, so the two windows and the two
data sets are never mixed up. Only one of them may drive the fleet at a
time (radxa/EXHIBITION.md).

Workspace (default ./showdata, git-ignored: the designs are the
client's, not the repo's):
    files/*.csv     the maps and colour grids, as delivered
    show.json       which unit carries which item, and the timeline:
                    which design each item wears when (conductor/timeline.py)
    history.json    earlier and undone versions of show.json, for undo/redo
    fleet.json      optional: {"units": {"radxa-01": "host:port", ...},
                    "token": "..."} when the units are not at their
                    usual 192.168.51.1NN:8787 (conductor/fleet.py)

Undo covers show.json - the timeline, the show's length and the unit
assignments - and is kept on disk, so it survives a reload of the page
and a restart of the server. Adding or deleting a CSV is not an edit of
the show and is not undone (the delete asks first).
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import http.client
import importlib.util
import json
import math
import os
import shutil
import socket
from dataclasses import replace
import re
import tarfile
import tempfile
import threading
import time
import unicodedata
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import sequence, showfile, timeline
from .fleet import (DEFAULT_HOTSPOT_UNIT, DEFAULT_LEAD_S, WIFI_SWITCH_RANGE_S,
                    Fleet, default_units)
from .look import (MAX_BOARD_ID, PALETTE, UNRELIABLE_DIP_NOTE, Design,
                   LookError, LookMap, check, compile_design, default_shift,
                   resolve_dips, unit_board_ids, unreliable_dip)
from .look import kind as file_kind
from .look import file_stem, map_item
from .look import NOT_A_CSV_NAME
from .look import ANOTHER_GARMENTS_MAP as look_another_garments_map
from .look import NO_SUCH_GARMENT as look_no_such_garment
from .look import conventional_name as look_conventional_name
from .look import fold_name as look_fold_name
from .look import is_mac_metadata as look_is_mac_metadata
from .look import mac_safe_name as look_mac_safe_name
from .look import name_problem as look_name_problem
from .look import normalize_name as look_normalize
from .look import refuse_reason as look_refuse_reason
from .look import rename_onto_item as look_rename_onto_item
from .look import unique_save_name as look_unique_save_name

WEB_DIR = Path(__file__).resolve().parent / "web"
REPO_DIR = Path(__file__).resolve().parent.parent
DESIGNER_SOURCE = WEB_DIR / "designer.html"
BUILD_DESIGNER_PY = REPO_DIR / "tools" / "build_designer.py"
UNITS = [f"radxa-{n:02d}" for n in range(1, 11)]
MAX_UPLOAD = 8 * 1024 * 1024
MAX_MUSIC = 64 * 1024 * 1024
MUSIC_CHUNK = 256 * 1024
HISTORY_DEPTH = 200
LABEL_MAX = 40
BOARD_NO_MAX = 9999
NUMBER_BRAND = 0x03        # the device type the units' UI sends (ui/patterns.py)
SHOW_FORMAT = "epaper-show"
SHOW_FORMAT_VERSION = 1
BUNDLE_FORMAT = "epaper-show-bundle"      # the designers' simulator export
BUNDLE_FORMAT_VERSION = 1
BUNDLE_MAX_FILES = 200
# Why a bundle's *_map.csv did not land: the wiring in this workspace is
# the operator's, regenerated from the 配線ナビ when the garment changes
# (2026-09-27 - a bundle loaded at 13:49 put a stale copy of
# AZ271SD1307_map.csv back over that morning's board 150). The designer's
# own copy travels in the bundle so a FRESH workspace can be built from
# it; it is not the source of truth for a garment already here.
BUNDLE_WIRING_KEPT = "the workspace's wiring is kept (the bundle's copy differs)"
DEMO_NAME_MAX = 14        # the unit's LCD menu row
# The unit's LCD font (ui/render.py, DejaVu) has no Japanese glyphs, so a
# demo's name must be plain ASCII the unit can actually draw - the same
# message whichever way the name is unusable (empty, too long, or not
# printable ASCII), so the operator sees one clear rule, not three.
DEMO_NAME_MESSAGE = (f"A-Z, 0-9 and symbols, up to {DEMO_NAME_MAX} characters "
                     "(the unit's screen cannot show Japanese)")
_DEMO_NAME_OK = re.compile(r"^[\x20-\x7e]+$")     # printable ASCII only
_DEMO_SLUG_OK = re.compile(r"^[a-z0-9][a-z0-9-]*$")
# Parts of show.json that must not make the page say "changed since Upload"
# (see Workspace.revision). The music and the LOOK / model labels never reach
# a unit at all - they are the operator's own notes about the show.
#
# `clear_after_show` is the one exception that DOES reach a unit, and is here
# on purpose (2026-09-27). Nothing about the pictures changes with it: the
# show keeps its id (conductor/showfile.py adds the key after the digest), so
# an Upload would rewrite nothing, and the conductor sends POST /show/clear
# itself when the run ends - it never depends on the unit's own copy, which is
# only the fallback for a show that ends with the PC gone. Counting it would
# turn ten green chips amber and ask for a three-minute re-Upload, the evening
# of the show, for a flag the clear does not need.
#
# `start_countdown_s` never reaches a unit at all: it is only the lead THE
# SHOW's ③ START gives the fleet (fleet.start_show(lead_s=...)), so it is
# not in any unit's show file, not in the show id, and changing it must never
# ask for an Upload.
#
# `loop_wait_s` (EXHIBITION mode's Loop, 2026-09-30) is the same kind of
# thing as the countdown: how THIS Conductor runs the evening, never part of
# a unit's show file.
_REVISION_IGNORES = {"music", "labels", "clear_after_show", "start_countdown_s",
                     "loop_wait_s"}
# THE SHOW's "Countdown before START" (show.json's `start_countdown_s`): how
# long ③ START counts down, -0:11 ... -0:01, before the show's 0:00. The
# owner's request (2026-09-29): 「ショー開始までのカウントダウン時間を設定
# できるように ... -11秒スタートとなるようにして」. A show without the key
# counts down START_COUNTDOWN_S. NEXT / MOVE keep their own, shorter lead.
START_COUNTDOWN_S = 11.0
START_COUNTDOWN_RANGE_S = (3.0, 60.0)
_PLAIN_DECIMAL = re.compile(r"^[+-]?([0-9]+\.?[0-9]*|\.[0-9]+)([eE][+-]?[0-9]+)?$")


def check_start_countdown(value) -> float:
    """A countdown before START in seconds (whole or decimal, to a tenth),
    or ValueError naming the range."""
    low, high = START_COUNTDOWN_RANGE_S
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"start_countdown_s: {low:.0f} to {high:.0f} seconds")
    if isinstance(value, str):
        # What the page's parseSeconds() accepts, exactly: NFKC (full-width
        # "１１" is 11), then a plain decimal - no "0x10", no "1_1", no
        # other scripts' digits float() would quietly take.
        value = unicodedata.normalize("NFKC", value).strip()
        if not _PLAIN_DECIMAL.match(value):
            raise ValueError(f"start_countdown_s: {low:.0f} to {high:.0f} seconds")
    try:
        seconds = round(float(value), 1)
    except (TypeError, ValueError):
        raise ValueError(f"start_countdown_s: {low:.0f} to {high:.0f} seconds")
    if not low <= seconds <= high:          # NaN fails this too
        raise ValueError(f"start_countdown_s: {low:.0f} to {high:.0f} seconds")
    return seconds


def start_countdown_of(show: dict) -> float:
    """show.json's countdown before START; absent (every show written
    before it existed) or unreadable is the default, 11 s."""
    try:
        return check_start_countdown(show.get("start_countdown_s",
                                              START_COUNTDOWN_S))
    except ValueError:
        return START_COUNTDOWN_S


# THE SHOW's `Loop` (show.json's `loop_wait_s`, EXHIBITION mode, 2026-09-30):
# when a run reaches its end the Conductor waits this long and starts again
# exactly as ③ START would, countdown included. Off is NO key (the default:
# every show written before it existed is off); on is the wait in seconds.
# Like the countdown it never reaches a unit (_REVISION_IGNORES).
#
# The floor is the TIMELINE's (2026-10-01, the owner: 「LOOP はゼロ秒で
# 再開」): after its last cue L a unit sends its idle STOP at max(L + 15, the
# guard floor L + 30..41) and needs 5 s clear before the next trigger - a
# seam of LOOP_SEAM_S between the last cue and the next run's first trigger
# keeps the master from going 60 s without a STOP (§4.6). The show's own
# tail (its length minus the last cue's time) is part of that seam, so
#     min_wait = max(0, LOOP_SEAM_S - tail), rounded up to a second
# - 0 for the exhibition show (last cue 9:37, end 10:54: a 77 s tail), 30
# for a show whose last cue is 10 s before its end. The next run's first
# trigger (the 0:00 preset) goes out complete_s BEFORE its T0 - about the
# length of the START countdown - so the seam is tail + wait, ~40 s from
# the last cue to the restart itself, not to the show's 0:00. A unit that
# joined a run late in the tail (supervision's "started late") can still
# be repainting its last cue at the next preset; rare, and only that unit.
LOOP_WAIT_S = 45.0
LOOP_WAIT_RANGE_S = (0.0, 600.0)
LOOP_SEAM_S = 40.0
WORKSPACE_TAR_MEMBERS = 5000
# The exhibition workspace travels between two Conductors as one .tar
# (GET /api/workspace/export -> POST /api/workspace/import): show.json,
# history.json, files/*.csv and the music - never fleet.json, which says
# where THIS host's units are. 200 MB is three times the biggest music file
# allowed plus every CSV a show could hold.
WORKSPACE_TAR_MAX = 200 * 1024 * 1024
WORKSPACE_TAR_CHUNK = 256 * 1024
# How much of a request body an EARLY refusal (401 / 409 / 413, answered
# before the body is wanted) still reads before answering - see
# Handler._refuse_early. Enough for every JSON body and a small tar; a
# bigger one gets `Connection: close` instead of 200 MB of draining.
EARLY_DRAIN_MAX = 2 * 1024 * 1024
EARLY_DRAIN_S = 2.0        # ...and how long a bigger one is still read after the answer
EARLY_IDLE_S = 10.0        # a drain's per-recv timeout: an idle client holds no thread
_TAR_TOP = ("show.json", "history.json")
_TAR_DIRS = ("files", "music")


def check_loop_wait(value, floor: float = 0.0, why: str = "") -> "float | None":
    """The Loop's wait in seconds (`floor` to 600, to a tenth), None for
    off, or ValueError naming the range - and `why` the floor is what it
    is (loop_floor_of). Read exactly as the countdown is
    (check_start_countdown): NFKC, a plain decimal, no bool."""
    if value is None:
        return None
    low, high = max(LOOP_WAIT_RANGE_S[0], float(floor)), LOOP_WAIT_RANGE_S[1]
    message = (f"loop_wait_s: {low:.0f} to {high:.0f} seconds here"
               + (f" ({why})" if why else "") + ", or null for off")
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(message)
    if isinstance(value, str):
        value = unicodedata.normalize("NFKC", value).strip()
        if not _PLAIN_DECIMAL.match(value):
            raise ValueError(message)
    try:
        seconds = round(float(value), 1)
    except (TypeError, ValueError):
        raise ValueError(message)
    if not low <= seconds <= high:          # NaN fails this too
        raise ValueError(message)
    return seconds


def loop_wait_of(show: dict) -> "float | None":
    """show.json's Loop wait AS STORED, or None when the Loop is off (no
    key, or a key nothing can read). Not held to the current floor: an
    edit that shortened the tail after the wait was set keeps the stored
    number, the Timeline warns, and the restart uses the floor instead
    (loop_effective_wait)."""
    try:
        return check_loop_wait(show.get("loop_wait_s"))
    except ValueError:
        return None


def loop_floor_of(show: dict) -> "tuple[float, float, str]":
    """(the least wait this timeline allows, its tail, why) - see
    LOOP_SEAM_S. The tail is the show's length minus the last cue's time;
    a timeline with no cue has nothing to keep away from and a floor of 0."""
    duration = float(show.get("duration", timeline.DEFAULT_DURATION_S))
    cues = timeline.clean(show.get("cues"))
    if not cues:
        return 0.0, duration, "no cue on the timeline"
    last = max(float(c["at"]) for c in cues)
    tail = max(0.0, duration - last)
    floor = float(math.ceil(max(0.0, LOOP_SEAM_S - tail) - 1e-9))
    why = (f"the last cue is {tail:.0f} s before the end"
           + (f", {LOOP_SEAM_S:.0f} s are needed between it and the next run"
              if floor > 0 else ""))
    return floor, tail, why


def loop_effective_wait(show: dict) -> "float | None":
    """The wait a restart really uses: the stored one, or the floor when
    an edit since has pulled the floor above it. None while off."""
    wait = loop_wait_of(show)
    if wait is None:
        return None
    return max(wait, loop_floor_of(show)[0])
# A CSV's name is conductor/look.py's business now (normalize_name /
# name_problem, the same rule the designers' simulator applies): this one
# is only for the MUSIC blob, which is a file on disk and nothing else -
# no cue references it by name, so folding a stray character to "_" costs
# nothing and saves a whole class of filesystem trouble.
_SAFE_MUSIC_NAME = re.compile(r"[^\w.\- ]", re.UNICODE)
_MAP_ITEM = re.compile(r"(.+?)_map", re.IGNORECASE)     # as look.py names items
_COPY_NO = re.compile(r"-[0-9]+$")
# [0-9], never \d - see conductor/look.py's _PATTERN_NO: Python's \d takes
# a full-width digit and JavaScript's does not, and conductor/web/sim's
# LOOK_NO has to agree with this one.
_LOOK_NO = re.compile(r"look\s*0*([0-9]+)", re.IGNORECASE)
_MUSIC_TYPES = {".mp3": "audio/mpeg", ".wav": "audio/wav",
                ".ogg": "audio/ogg", ".m4a": "audio/mp4"}
_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def _key(position) -> str:
    return "|".join(str(part) for part in position)


def _is_number(value) -> bool:
    """A real JSON number - not None, not a string, not True (which is an
    int in Python and would pass as 1.0 s)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def safe_music_name(name: str) -> str:
    """A name for the music blob on disk. Not a CSV rule: see above."""
    return _SAFE_MUSIC_NAME.sub(
        "_", Path(unicodedata.normalize("NFC", str(name))).name)


def _same_csv(a: "str | None", b: "str | None") -> bool:
    """Whether two CSVs are the same file picked twice.

    Line endings and a leading BOM are how the file travelled, not what
    it says: a grid saved here with "\\n" and handed back by a browser
    that read it with "\\r\\n" is the same design, and re-picking it
    must be "already there" rather than a second copy numbered "-2".
    """
    if a is None or b is None:
        return False
    def body(text: str) -> str:
        return text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    return body(a) == body(b)


def workspace_name(name: str) -> str:
    """A CSV's name as this workspace spells it - conductor/look.py's
    shared rule, on the name the caller actually sent.

    Raises ValueError quoting THAT name and the reason. It refuses rather
    than folds (review of a6b610b): the old substituting version turned
    柄・A, 柄　A and 柄＋A into one "柄_A", so three designs quietly
    overwrote each other. And it does not take the basename first (review
    of 3fd1a42): doing that dropped a directory in silence, so
    "sub/AZ_1_HW.csv" became a file of this workspace on the /api/files
    path while the simulator and import_bundle both refused it - and the
    error it did raise quoted the stripped name, not what was sent.
    """
    problem = look_name_problem(name)
    if problem:
        raise ValueError(f"{name}: {problem}")
    return look_normalize(name)


def _renamed_items(renamed: "dict[str, str]") -> "dict[str, str]":
    """The garment renames implied by the MAP renames in `renamed`.

    A map's name carries the item: respelling AZ271SD1305_map.csv renames
    the garment too, and the timeline names that garment in four more
    places than the design references (review of a6b610b).
    """
    items: "dict[str, str]" = {}
    for old, new in renamed.items():
        if file_kind(new) != "map":
            continue
        # The OLD item comes off the raw name: map_item() normalises, so
        # asking it for both sides would return the same string twice and
        # find no rename at all.
        was = _MAP_ITEM.match(file_stem(old))
        old_item = was.group(1) if was else None
        new_item = map_item(new)
        if old_item and new_item and old_item != new_item:
            items[old_item] = new_item
    return items


def _rename_design_refs(show: dict, renamed: "dict[str, str]") -> dict:
    """A copy of `show` with every name the import respelled rewritten
    wherever the timeline uses it.

    Design files are named by a cue's "design" and by a key of
    "transitions". A MAP's rename is a garment's rename, which the cues'
    "item" and the "units"/"labels"/"boards"/"dips" maps all key on. A name the
    bundle did not carry is composed too - it may well be pointing at
    something this workspace already holds in NFC - but never invented:
    a design reference is only rewritten when NFC changes it AND the
    result is a real design name.
    """
    show = dict(show)
    items = _renamed_items(renamed)

    def rename(name):
        if not isinstance(name, str):
            return name
        if name in renamed:
            return renamed[name]
        clean = look_normalize(name)
        return clean if clean != name and file_kind(clean) is not None else name

    def rename_item(name):
        if not isinstance(name, str):
            return name
        return items.get(name) or items.get(look_normalize(name)) or name

    cues = show.get("cues")
    if isinstance(cues, list):
        fresh = []
        for cue in cues:
            if not isinstance(cue, dict):
                fresh.append(cue)
                continue
            cue = dict(cue)
            if "design" in cue:
                cue["design"] = rename(cue["design"])
            if "item" in cue:
                cue["item"] = rename_item(cue["item"])
            fresh.append(cue)
        show["cues"] = fresh
    transitions = show.get("transitions")
    if isinstance(transitions, dict):
        show["transitions"] = {rename(k): v for k, v in transitions.items()}
    for key in ("units", "labels", "boards", "dips"):
        value = show.get(key)
        if isinstance(value, dict):
            show[key] = {rename_item(k): v for k, v in value.items()}
    return show


def _known_items(maps: "dict[str, LookMap]") -> "list[str]":
    """The garments this workspace already has a map for - what tells
    Design.name_parts() where the item ends in an <item>_<name>_HW.csv
    whose design name carries underscores of its own."""
    return [m.item for m in maps.values() if m.item]


def music_type(name: str) -> str:
    return _MUSIC_TYPES.get(Path(name).suffix.lower(), "application/octet-stream")


def _demo_name(raw) -> str:
    """The name typed for a standalone demo, stripped and upper-cased -
    or a ValueError with DEMO_NAME_MESSAGE for anything the unit's LCD
    could not show (empty, over 14 characters, or not plain ASCII)."""
    name = str(raw or "").strip()
    if not name or len(name) > DEMO_NAME_MAX or not _DEMO_NAME_OK.match(name):
        raise ValueError(DEMO_NAME_MESSAGE)
    return name.upper()


def _slugify(name: str) -> str:
    """A demo's id from its name, exactly as the unit makes one
    (ui/demos.py slugify) - the conductor's own marks are kept by name,
    and a delete only ever names the slug."""
    return re.sub(r"[^a-z0-9]+", "-", str(name).strip().lower()).strip("-") or "demo"


def _demo_slug(raw) -> str:
    """A demo's id, as the unit itself makes one (ui/demos.py: lower-case
    a-z0-9- from the name). Refused here too - a slug is about to become
    a path/JSON key on every unit, and this is cheaper than letting ten
    of them each say so themselves."""
    slug = str(raw or "").strip()
    if not _DEMO_SLUG_OK.match(slug):
        raise ValueError("bad demo id")
    return slug


def _only_units(raw) -> "list[str] | None":
    """The `units` of an Upload / Save on the units: which units of the
    compiled show this write is for ("Which LOOKs" in the page's dialog),
    or None for all of them - absent means all, which is what every
    client before this field sent and what the page sends for "All
    LOOKs".

    Read BEFORE the show is compiled, because it is what the compile is
    FOR: a one-LOOK write only refuses on the problems of its own units
    (Workspace.compile_for_write)."""
    if raw is None:
        return None
    if not isinstance(raw, list) or not all(isinstance(u, str) for u in raw):
        raise ValueError("units must be a list of unit names")
    names = list(dict.fromkeys(raw))
    if not names:
        raise ValueError("no unit chosen - pick a LOOK, or All LOOKs")
    return names


def _check_only_units(names: "list[str] | None",
                      shows: "dict[str, dict]") -> None:
    """A name that is not a unit of THIS timeline is refused by name
    rather than quietly dropped: the page builds the list from the
    timeline it is showing, so a mismatch means the two disagree about
    what is where (a stale page, another operator's edit) - exactly the
    moment to stop rather than write a look to nothing and report success.

    Nothing compiled at all (a timeline with a problem): the write is
    already going nowhere and the problems are the answer - saying
    "radxa-01 is not a unit of this timeline" on top of them would send
    the operator looking for the wrong thing."""
    for name in (names or ()) if shows else ():
        if name not in shows:
            raise ValueError(f"{name} is not a unit of this timeline")


def _design_transition(entry) -> dict:
    """A design's own transition, as the page reads it - always present,
    natural/0 when nothing was set (conductor/sequence.py's tidying, so
    a stray show.json entry cannot reach the page unclean)."""
    if not isinstance(entry, dict):
        entry = {}
    return {"sequence": sequence.clean_sequence(entry.get("sequence", "natural")),
            "span_s": sequence.clean_span(entry.get("span_s", 0.0))}


def _clean_transitions(raw) -> dict:
    """{design: {"sequence", "span_s"}}, tidied the way set_transition
    normalises one - an import is not a promise the file was hand-edited
    honestly, and timeline.resolve() must never see an entry that is not
    a plain dict of known shape (it would crash state() for everyone,
    not just the importer). A malformed or natural/zero entry is simply
    dropped, the same as if it had never been set."""
    result = {}
    if not isinstance(raw, dict):
        return result
    for design, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        seq = sequence.clean_sequence(entry.get("sequence", "natural"))
        span = min(sequence.MAX_DELAY_S, sequence.clean_span(entry.get("span_s", 0.0)))
        if seq != "natural" and span > 0:
            result[str(design)] = {"sequence": seq, "span_s": span}
    return result


def _parse_range(header: str, size: int):
    """(start, end) inclusive for one 'Range: bytes=a-b' request; the
    string "unsatisfiable" for one entirely past the end (416); or None
    for a header this server does not parse - which is answered as a
    plain 200, not refused (most player bugs are here, not in a
    generous fallback)."""
    match = _RANGE_RE.match((header or "").strip())
    if not match or size <= 0:
        return None
    first, last = match.groups()
    if first == "" and last == "":
        return None
    if first == "":                      # suffix: the last N bytes
        try:
            n = int(last)
        except ValueError:
            return None
        if n <= 0:
            return None
        start, end = max(0, size - n), size - 1
    else:
        start = int(first)
        end = int(last) if last != "" else size - 1
    if start > end or start >= size:
        return "unsatisfiable"
    return start, min(end, size - 1)


def _time_sweeps(cues: "list[dict]", maps: "dict[str, LookMap]") -> None:
    """Give every cue with a sweep its span (seconds it adds to the
    refresh), which only the item's map can say. A cue whose map is
    missing keeps no span and validate() reports it. Called after
    timeline.apply_transitions(), which is what gives each cue its
    resolved cue["sweep"]."""
    for cue in cues:
        look_map = maps.get(cue["item"].lower())
        sweep = cue["sweep"]
        if sweep["sequence"] == "natural":
            cue["span"] = 0.0
        elif look_map is not None:
            cue["span"] = sequence.span_s(look_map, sweep["sequence"],
                                          sweep["span_s"])


class Workspace:
    """The folder behind the UI. Every method is safe to call from the
    server's request threads."""

    def __init__(self, root):
        self.root = Path(root)
        self.files = self.root / "files"
        self.files.mkdir(parents=True, exist_ok=True)
        self.music = self.root / "music"        # made on the first upload
        self._lock = threading.Lock()
        # What the timeline looked like when it was last written to the
        # units: {"upload": revision, "demo:<NAME>": revision} - see
        # revision() and mark_written(). Lives as long as this conductor,
        # like fleet.shows, and is what lets the page say "changed since"
        # about an edit made after the write (an id comparison cannot: the
        # units hold exactly what they were sent, edits and all).
        self.marks: "dict[str, str]" = {}
        # The same, per unit: {"upload": {unit: revision}, ...}. A write
        # for one LOOK only reaches its own units, and the page has to be
        # able to say which units hold what is on screen and which are
        # still on an older timeline - the fleet-wide mark above cannot
        # say that, so it is simply absent after a partial write.
        self.unit_marks: "dict[str, dict[str, str]]" = {}
        # What the last compile_show() made of the timeline, and of which
        # revision: {"revision", "problems", "units"}. START's gate reads
        # it to tell "you edited and forgot to upload" (Upload again)
        # from "this timeline does not build at all" (fix it first) -
        # without paying for a compile of its own.
        self.compiled: "dict | None" = None

    # ---- show.json ----

    @property
    def _show_path(self) -> Path:
        return self.root / "show.json"

    def _load_show(self) -> dict:
        try:
            show = json.loads(self._show_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # No show.json at all: a NEW show, which opens on today's
            # default refresh (every reader's own show.get(..., REFRESH_S)).
            return {}
        if isinstance(show, dict) and not _is_number(show.get("refresh_s")):
            # A show FILE that names no refresh time (or names it as null,
            # as a couple of hand-made workspaces do) was written before
            # the refresh became effect-inclusive, 2026-09-26: it keeps the
            # LEGACY_REFRESH_S it was drawn against rather than silently
            # moving to today's longer default, so the Timeline tab can
            # OFFER the change instead of pretending it was made. Anything
            # this version writes always names it - see _write_show().
            show = dict(show, refresh_s=timeline.LEGACY_REFRESH_S)
        return show

    def _write_show(self, payload: dict) -> None:
        """show.json, always naming its refresh time. The one write path for
        the show (an edit, an undo, a redo), so a file this version leaves
        behind is never mistaken for a pre-2026-09-26 one by _load_show()."""
        if isinstance(payload, dict) and not _is_number(payload.get("refresh_s")):
            payload = dict(payload, refresh_s=timeline.REFRESH_S)
        self._write(self._show_path, payload)

    @property
    def _history_path(self) -> Path:
        return self.root / "history.json"

    def _load_history(self) -> dict:
        try:
            history = json.loads(self._history_path.read_text(encoding="utf-8"))
            return {"undo": list(history["undo"]), "redo": list(history["redo"])}
        except (OSError, ValueError, KeyError, TypeError):
            return {"undo": [], "redo": []}

    def _write(self, path: Path, payload) -> None:
        # Whole or not at all: a power cut in the middle of a plain write
        # would leave half a timeline, the night before the show.
        scratch = path.with_name(path.name + ".tmp")
        scratch.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        os.replace(scratch, path)

    def _commit(self, before: dict, after: dict) -> None:
        """Save an edit of the show (lock held). The version it replaces
        becomes the next undo; a new edit ends the redo line."""
        # A countdown equal to the default is stored as NO key, whichever
        # path wrote it (set_start_countdown, a show file, a bundle): the
        # file says only what somebody changed (review of 3f67087, LOW-5).
        if after.get("start_countdown_s") == START_COUNTDOWN_S:
            after = {k: v for k, v in after.items() if k != "start_countdown_s"}
        # ...and a Loop that is off is no key at all, the same way.
        if "loop_wait_s" in after and after["loop_wait_s"] is None:
            after = {k: v for k, v in after.items() if k != "loop_wait_s"}
        if after == before:
            return
        history = self._load_history()
        history["undo"] = (history["undo"] + [before])[-HISTORY_DEPTH:]
        history["redo"] = []
        self._write_show(after)
        self._write(self._history_path, history)

    def _step(self, take: str, give: str) -> bool:
        with self._lock:
            history = self._load_history()
            if not history[take]:
                return False
            current = self._load_show()
            restored = history[take].pop()
            history[give] = (history[give] + [current])[-HISTORY_DEPTH:]
            self._write_show(restored)
            self._write(self._history_path, history)
            return True

    def undo(self) -> bool:
        return self._step("undo", "redo")

    def redo(self) -> bool:
        return self._step("redo", "undo")

    # ---- migrating cues saved before `align` was removed ----
    # A cue used to say `align`: "done" (the design is complete at `at`)
    # or "start" (it begins at `at`). `at` is now always Start, so a
    # "done" cue moves its `at` back by whatever that refresh (and any
    # sweep) took; a "start" cue already meant Start and only loses the
    # key. Called from state()/compile_show()/export_show(), under
    # self._lock, so the one _commit it may need is safe to make there.

    def _maps_for_migration(self, show: dict) -> "dict[str, LookMap]":
        maps: "dict[str, LookMap]" = {}
        for path in sorted(self.files.glob("*.csv")):
            if self.kind(path.name) != "map":
                continue
            try:
                look_map = LookMap.from_csv(path)
            except (OSError, LookError):
                continue
            look_map = self._renumbered(look_map, show)
            maps[(look_map.item or path.stem).lower()] = look_map
        return maps

    @staticmethod
    def _migrate_cue(raw, refresh: float, transitions: dict,
                     maps: "dict[str, LookMap]") -> dict:
        """One raw cue -> the same cue without `align`, its `at` moved so
        the instant it used to mean (done or start) is unchanged. A cue
        that never had `align` passes through untouched."""
        if not isinstance(raw, dict) or "align" not in raw:
            return raw
        cleaned = timeline.clean([raw])[0]
        if raw.get("align", "done") == "done" and cleaned["at"] > 0:
            span = 0.0
            sweep = timeline.resolve(cleaned, transitions)
            if sweep["sequence"] != "natural":
                look_map = maps.get(cleaned["item"].lower())
                if look_map is not None:
                    span = sequence.span_s(look_map, sweep["sequence"],
                                           sweep["span_s"])
            # The same "send to picture complete" the timeline models
            # (timeline.complete_s over a cue carrying this span), so the
            # instant the old `align: done` meant is unchanged.
            paint = timeline.complete_s(dict(cleaned, span=span), refresh)
            cleaned["at"] = max(0.0, round(cleaned["at"] - paint, 1))
        return cleaned

    def _migrate_align(self, show: dict) -> dict:
        """A show whose cues still carry `align` -> the same show with
        every one migrated, as ONE _commit (one undo step); a show with
        nothing to migrate is returned untouched (the common case, so a
        plain state() poll pays nothing for this). Idempotent: the
        result never has `align`, so a second call is a no-op."""
        cues = show.get("cues")
        if not isinstance(cues, list) or not any(
                isinstance(c, dict) and "align" in c for c in cues):
            return show
        refresh = float(show.get("refresh_s", timeline.REFRESH_S))
        transitions = show.get("transitions") or {}
        maps = self._maps_for_migration(show)
        migrated = [self._migrate_cue(c, refresh, transitions, maps)
                   for c in cues]
        after = dict(show, cues=timeline.clean(migrated))
        self._commit(show, after)
        return after

    def assign(self, item: str, unit: "str | None") -> None:
        if unit is not None and unit not in UNITS:
            raise ValueError(f"unknown unit {unit!r}")
        with self._lock:
            before = self._load_show()
            units = dict(before.get("units", {}))
            if unit is None:
                units.pop(item, None)
            else:
                units[item] = unit
            if units == before.get("units", {}):
                return                      # nothing changed: not a step
            self._commit(before, dict(before, units=units))

    # ---- board numbers set on the page ----
    # The map CSV says which board drives which scale. The garment that is
    # finally sewn may carry other boards (a second garment made from the
    # same map; a board swapped for a spare): {number in the CSV: its own}.

    @staticmethod
    def _own_boards(show: dict, item: str) -> "dict[int, int]":
        try:
            return {int(old): int(new) for old, new in
                    show.get("boards", {}).get(item, {}).items()}
        except (AttributeError, TypeError, ValueError):
            return {}

    # ---- DIP IDs set by hand on the page ----
    # An address is normally the board's rank among the unit's board numbers
    # (look.unit_board_ids()). A board whose DIP switch was changed on the
    # garment itself cannot be said that way - AZ271SD1301 carries 27 boards
    # and the last one was set to 28 (2026-09-27) - so show.json's `dips`
    # carries {item: {board_no: the number the switches really have}}, keyed
    # on the board number as the page shows it (after `boards`).

    @staticmethod
    def _own_dips(show: dict, item: str) -> "dict[int, int]":
        """Per entry, not all-or-nothing like _own_boards(): resolve_dips()
        drops one unusable key and keeps the rest, so this - which is what
        tells the page which cells to badge - has to agree, or a single
        typo in a hand-edited show.json would take the badge off a board
        whose address really was set by hand (found in review)."""
        try:
            entries = show.get("dips", {}).get(item, {}).items()
        except (AttributeError, TypeError):
            return {}
        own: "dict[int, int]" = {}
        for no, dip in entries:
            try:
                own[int(no)] = int(dip)
            except (TypeError, ValueError):
                continue        # unusable: unit_board_ids() says so instead
        return own

    @staticmethod
    def _unit_dips(show: dict, maps: "list[LookMap]") -> "dict[int, int]":
        """The one {board_no: DIP} of a bus, from every item on it."""
        return resolve_dips(maps, show.get("dips") or {})

    def _renumbered(self, look_map: LookMap, show: dict) -> LookMap:
        own = {old: new for old, new in
               self._own_boards(show, look_map.item or "").items()
               if old in look_map.board_nos and new != old}
        if not own:
            return look_map
        result = [own.get(no, no) for no in look_map.board_nos]
        if len(set(result)) != len(result):     # a newer CSV: no longer fits
            return replace(look_map, warnings=look_map.warnings + [
                "the board numbers set on the page no longer fit this map "
                "and are ignored"])
        scales = [s if s.board_no not in own else replace(
                      s, board_no=own[s.board_no],
                      label=(f"{own[s.board_no]:03d}-{s.socket:02d}"
                             if s.label else ""))
                  for s in look_map.scales]
        return replace(look_map, scales=scales)

    @staticmethod
    def _shown_boards(look_map: LookMap,
                      own: "dict[int, int]") -> "dict[int, int]":
        """{board_no in the CSV: the number the PAGE shows for it} under the
        renumbering `own` - _renumbered()'s own rule, including its refusal:
        a mapping that would put two boards on one number is ignored whole,
        so the page shows the CSV's numbers. This is what `dips` is keyed
        on, so set_boards() can carry a hand-set DIP over to the board's
        new number."""
        moved = {old: new for old, new in own.items()
                 if old in look_map.board_nos and new != old}
        result = {no: moved.get(no, no) for no in look_map.board_nos}
        if len(set(result.values())) != len(result):
            return {no: no for no in look_map.board_nos}
        return result

    def _map_path(self, item: str) -> "Path | None":
        for path in sorted(self.files.glob("*.csv")):
            named = _MAP_ITEM.match(path.name)
            if (self.kind(path.name) == "map" and named
                    and named.group(1).lower() == item.lower()):
                return path
        return None

    def set_boards(self, item: str, boards: dict) -> None:
        """{board_no in the map CSV: the number the garment really has}."""
        with self._lock:
            before = self._load_show()
            path = self._map_path(str(item))
            try:
                look_map = LookMap.from_csv(path)
            except (TypeError, OSError, LookError):
                raise ValueError(f"{item}: no usable map")
            own = {old: new for old, new in
                   self._own_boards(before, item).items()
                   if old in look_map.board_nos}
            # What the page shows TODAY, before this call changes it: the
            # key `dips` is written under, carried over at the end.
            shown_before = self._shown_boards(look_map, own)
            try:
                own.update({int(old): int(new) for old, new in boards.items()})
            except (AttributeError, TypeError, ValueError):
                raise ValueError("boards: {board_no: board_no}")
            unknown = sorted(set(own) - set(look_map.board_nos))
            if unknown:
                raise ValueError(f"{item} has no board {unknown[0]}")
            result = [own.get(no, no) for no in look_map.board_nos]
            if not all(1 <= no <= BOARD_NO_MAX for no in result):
                raise ValueError(f"board numbers are 1 to {BOARD_NO_MAX}")
            twice = sorted({no for no in result if result.count(no) > 1})
            if twice:
                raise ValueError(f"board {twice[0]} would be there twice")
            every = dict(before.get("boards", {}))
            every[item] = {str(old): new for old, new in sorted(own.items())
                           if new != old}
            after = dict(before, boards=every)
            # `dips` is keyed on the number the PAGE shows, which is what
            # this call changes - so a DIP set by hand has to be carried
            # over to the board's new number, in the same step. Renumbering
            # 118 -> 119 used to leave `dips` saying {"118": 28}: the board
            # went back to its rank while its switches still read 28, the
            # cue painted nothing, and the board really at 27 took it. And
            # where a renumbering moved one board ONTO another's old number
            # (117 -> 118, 118 -> 130), the stale key silently became a
            # different board's setting (found in review).
            was = self._own_dips(before, item)
            if was:
                shown_after = self._shown_boards(look_map, own)
                moved = {shown_before[no]: shown_after[no]
                         for no in look_map.board_nos}
                carried = {moved[no]: dip for no, dip in sorted(was.items())
                           if no in moved}
                kept = dict(before.get("dips", {}))
                if carried:
                    kept[item] = {str(no): dip
                                  for no, dip in sorted(carried.items())}
                else:               # every setting was for a board this map
                    kept.pop(item, None)    # no longer has: nothing to carry
                after["dips"] = kept
            self._commit(before, after)

    def set_dips(self, item: str, dips: dict) -> None:
        """{board_no: the DIP ID that board's switches really have}.

        The board number is the one the page shows (the map CSV's own,
        after set_boards(), which carries these settings over when it
        changes one). A DIP of None drops the override - that is the "✕"
        on the page's badge, and the only way back to the rank. Every
        other item on the same unit is taken into account, so a number
        already in use on that bus is refused here rather than at Upload.

        A setting that happens to EQUAL the board's rank is kept, not
        discarded (found in review): it is a statement about what the
        switches on that board read, and dropping it would let a later
        renumbering - or another garment joining the unit and shifting
        every rank - move that board's address away from the hardware
        with nothing recorded to stop it.
        """
        with self._lock:
            before = self._load_show()
            path = self._map_path(str(item))
            try:
                look_map = self._renumbered(LookMap.from_csv(path), before)
            except (TypeError, OSError, LookError):
                raise ValueError(f"{item}: no usable map")
            own = {no: dip for no, dip in self._own_dips(before, item).items()
                   if no in look_map.board_nos}
            try:
                asked = {int(no): (None if dip is None else int(dip))
                         for no, dip in dips.items()}
            except (AttributeError, TypeError, ValueError):
                raise ValueError("dips: {board_no: DIP ID}")
            unknown = sorted(set(asked) - set(look_map.board_nos))
            if unknown:
                raise ValueError(f"{item} has no board {unknown[0]}")
            for no, dip in asked.items():
                if dip is None:
                    own.pop(no, None)
                elif not 1 <= dip <= MAX_BOARD_ID:
                    raise ValueError(f"a DIP ID is 1 to {MAX_BOARD_ID}")
                else:
                    own[no] = dip
            # Judged on the whole bus, by the one function that derives
            # addresses - so the page's refusal and Upload's agree.
            assigned = before.get("units", {})
            unit = assigned.get(look_map.item or "")
            on_unit = [look_map]
            if unit:
                for other in self._maps_for_migration(before).values():
                    if (other.item != look_map.item
                            and assigned.get(other.item or "") == unit):
                        on_unit.append(other)
            every = dict(before.get("dips", {}))
            every[item] = {str(no): dip for no, dip in sorted(own.items())}
            merged = resolve_dips(on_unit, dict(every))
            try:
                unit_board_ids(on_unit, merged, unit=unit)
            except LookError as exc:
                raise ValueError(exc.problems[0])
            if not every[item]:         # the last one was reverted with None
                every.pop(item)
            if every == (before.get("dips") or {}):
                return                      # nothing changed: not a step
            self._commit(before, dict(before, dips=every))

    def duplicate(self, item: str) -> str:
        """Another garment of the same shape, as an item of its own: the map
        CSV is copied under a new name (Look22 -> Look22-2). Nothing is
        shared afterwards - designs, cues, unit and label are its own."""
        with self._lock:
            source = self._map_path(str(item))
            if source is None:
                raise ValueError(f"{item}: no map to copy")
            base = _COPY_NO.sub("", _MAP_ITEM.match(source.name).group(1))
            number = 2
            while self._map_path(f"{base}-{number}") is not None:
                number += 1
            twin = f"{base}-{number}"
            shutil.copyfile(source, self.files / f"{twin}_map.csv")
            before = self._load_show()
            after = dict(before)
            for key in ("labels", "boards", "dips"):  # starts as what it copies
                if isinstance(before.get(key, {}).get(item), dict):
                    after[key] = dict(before[key], **{
                        twin: dict(before[key][item])})
            self._commit(before, after)
            return twin

    def arrange(self, units: dict) -> None:
        """Every assignment at once - {item: unit} - as one step of the
        history: dragging a look to another place moves all those between."""
        if not isinstance(units, dict):
            raise ValueError("units: {item: unit}")
        placed = {}
        for item, unit in units.items():
            if unit is None or unit == "":
                continue
            if unit not in UNITS:
                raise ValueError(f"unknown unit {unit!r}")
            placed[str(item)] = unit
        with self._lock:
            before = self._load_show()
            self._commit(before, dict(before, units=placed))

    def set_label(self, item: str, look, model) -> None:
        """What the garment is called on the page: its LOOK number and its
        model number (AZ271SD1301). The files keep their names - they are
        what ties a design to its map - so this is free to change."""
        item = str(item or "").strip()
        if not item:
            raise ValueError("no item")
        label = {}
        for key, value in (("look", look), ("model", model)):
            value = " ".join(str(value or "").split())
            if len(value) > LABEL_MAX:
                raise ValueError(f"{key}: at most {LABEL_MAX} characters")
            label[key] = value
        with self._lock:
            before = self._load_show()
            labels = dict(before.get("labels", {}))
            labels[item] = label
            self._commit(before, dict(before, labels=labels))

    def set_timeline(self, duration, cues, refresh=None) -> None:
        """Replace the whole timeline; the page always posts all of it.
        `refresh` (seconds a repaint takes) is kept when not given."""
        duration = timeline.check_duration(duration)     # 1 s .. 15:00
        cues = timeline.clean(cues)
        changes = {"duration": duration, "cues": cues}
        if refresh is not None:
            try:
                refresh = round(float(refresh), 1)
            except (TypeError, ValueError):
                raise ValueError(f"not a number of seconds: {refresh!r}")
            low, high = timeline.REFRESH_RANGE_S
            if not low <= refresh <= high:
                raise ValueError(f"a refresh takes between {low:.0f} and "
                                 f"{high:.0f} s")
            changes["refresh_s"] = refresh
        with self._lock:
            before = self._load_show()
            self._commit(before, dict(before, **changes))

    def set_clear_after_show(self, on) -> None:
        """"Clear pictures after the show" (show.json's
        `clear_after_show`, undoable like any other edit of the show).

        Per show and remembered with it, because it is a property of the
        EVENING, not of this browser: the pictures come back out of slots
        1-18 when the run ends or is stopped, so a garment unplugged with
        its boards still on battery cannot replay the show from its own
        factory autoplay (2026-09-27). Absent means false, which is what
        every show file written before this behaves as.
        """
        if not isinstance(on, bool):
            raise ValueError("clear_after_show must be true or false")
        with self._lock:
            before = self._load_show()
            if bool(before.get("clear_after_show")) == on:
                return                          # nothing changed: not a step
            after = dict(before)
            if on:
                after["clear_after_show"] = True
            else:
                # Removed rather than written as false: a show.json
                # without the key is exactly the old behaviour, and the
                # file says only what somebody chose.
                after.pop("clear_after_show", None)
            self._commit(before, after)

    def set_start_countdown(self, seconds) -> None:
        """THE SHOW's "Countdown before START" (show.json's
        `start_countdown_s`, undoable like any other edit of the show).

        Stored with the show, not in the browser, for the same reason as
        "Clear pictures after the show": it is how THIS evening starts, and
        it has to survive a reload and a second PC. The default (11 s) is
        stored as no key at all, so a show.json says only what somebody
        chose. Never part of what reaches a unit (_REVISION_IGNORES)."""
        seconds = check_start_countdown(seconds)
        with self._lock:
            before = self._load_show()
            # _commit() drops the key again when it is the default, and
            # makes no step when nothing changed.
            self._commit(before, dict(before, start_countdown_s=seconds))

    def start_countdown(self) -> float:
        """The show's countdown before ③ START (see set_start_countdown)."""
        with self._lock:
            return start_countdown_of(self._load_show())

    def set_loop(self, wait_s) -> None:
        """THE SHOW's `Loop` (show.json's `loop_wait_s`, undoable): the
        seconds between the end of a run and the next START, or None for
        off - which is stored as no key, so a show.json says only what
        somebody chose. Stored with the show for the same reason as the
        countdown: it is how this exhibition runs, and it has to survive a
        reload and the trip to the Conductor on radxa-05. Never part of
        what reaches a unit (_REVISION_IGNORES)."""
        with self._lock:
            before = self._load_show()
            floor, _tail, why = loop_floor_of(before)
            wait_s = check_loop_wait(wait_s, floor, why)
            # _commit() drops the key again when it is None, and makes no
            # step when nothing changed.
            self._commit(before, dict(before, loop_wait_s=wait_s))

    def loop_wait(self) -> "float | None":
        """The Loop's wait, or None while it is off (see set_loop)."""
        with self._lock:
            return loop_wait_of(self._load_show())

    def loop_settings(self) -> "tuple[float, float] | None":
        """What the fleet asks when a run reaches its end (Fleet's
        `loop_settings`): (the wait, the countdown the next run counts
        down), or None while the Loop is off."""
        with self._lock:
            show = self._load_show()
        wait = loop_effective_wait(show)
        return None if wait is None else (wait, start_countdown_of(show))

    def loop_floor(self) -> "tuple[float, float, str]":
        """(min wait, tail, why) for this timeline - loop_floor_of."""
        with self._lock:
            return loop_floor_of(self._load_show())

    def loop_view(self) -> dict:
        """Everything /api/fleet's `loop` object needs, from ONE read of
        show.json (it is asked once a second): the stored wait, the wait a
        restart really uses, the floor and why."""
        with self._lock:
            show = self._load_show()
        floor, tail, why = loop_floor_of(show)
        stored = loop_wait_of(show)
        return {"stored": stored,
                "effective": None if stored is None else max(stored, floor),
                "floor": floor, "tail": tail, "why": why}

    def set_transition(self, design: str, sequence_id, span_s) -> None:
        """A design's own transition (show.json, undoable): every cue that
        wears it and is not itself "custom" sweeps this way. Natural, or
        a span of 0, removes the entry - the two are the same thing."""
        design = Path(str(design or "")).name
        if self.kind(design) != "grid" or not (self.files / design).is_file():
            raise ValueError(f"{design}: not a design "
                             "(*_color_NAME_grid.csv)")
        sequence_id = sequence.clean_sequence(sequence_id)
        span = sequence.clean_span(span_s)
        if span > sequence.MAX_DELAY_S:
            raise ValueError(f"a sweep is at most {sequence.MAX_DELAY_S:.0f} s "
                             "from the first scale to the last "
                             "(the firmware's limit)")
        with self._lock:
            before = self._load_show()
            transitions = dict(before.get("transitions", {}))
            if sequence_id == "natural" or span <= 0:
                if design not in transitions:
                    return                       # nothing changed: not a step
                transitions.pop(design, None)
            else:
                transitions[design] = {"sequence": sequence_id, "span_s": span}
            self._commit(before, dict(before, transitions=transitions))

    # ---- the show's music ----
    # Bytes live under <root>/music, one file at a time; show.json only
    # ever points at it by name. Undo covers the pointer, not the file
    # (docs in the plan): music_info() below is what makes that safe.

    def save_music(self, name: str, stream, length: int) -> dict:
        """Stream `length` bytes of `name` from `stream` (the request's
        rfile; the caller has already checked length against MAX_MUSIC)
        to <root>/music, then point show.json at it as one commit.

        Written to a `.part` file unique to this request - two uploads
        at once must not collide - and only replaced into place once
        every byte is down; the lock is taken for that swap and the
        commit, never for the streaming itself.
        """
        safe = safe_music_name(name or "music") or "music"
        self.music.mkdir(parents=True, exist_ok=True)
        part = self.music / f"{safe}.{os.getpid()}-{threading.get_ident()}.part"
        try:
            written = 0
            with open(part, "wb") as handle:
                remaining = length
                while remaining > 0:
                    chunk = stream.read(min(MUSIC_CHUNK, remaining))
                    if not chunk:
                        raise ValueError("the upload ended early")
                    handle.write(chunk)
                    written += len(chunk)
                    remaining -= len(chunk)
            # Also here, not only in the handler's Content-Length check:
            # save_music() is called directly (the tests, and anything
            # else that grows a caller later), and an empty file must
            # never become the show's music - a name in show.json with no
            # audio under it is exactly what makes the designers'
            # simulator claim a built-in track and then play nothing.
            if written == 0:
                raise ValueError("that music file is empty (0 bytes) - "
                                 "nothing was uploaded")
            info = {"name": safe, "size": written, "type": music_type(safe)}
            with self._lock:
                before = self._load_show()
                old = before.get("music") or {}
                # The pointer moves first: if the commit fails (a full
                # disk), the bytes are still only `part` and nothing on
                # disk is broken - the old file is untouched and correct.
                self._commit(before, dict(before, music=info))
                os.replace(part, self.music / safe)
                if old.get("name") and old["name"] != safe:
                    (self.music / Path(old["name"]).name).unlink(missing_ok=True)
            return info
        except BaseException:
            part.unlink(missing_ok=True)
            raise

    def remove_music(self) -> None:
        """Forget the show's music and delete its file. Undoable like any
        other edit of show.json - but an undo of THIS step is the only
        one that can bring the file back, which is why this asks first
        on the page, the way deleting a CSV does."""
        with self._lock:
            before = self._load_show()
            info = before.get("music")
            if not isinstance(info, dict):
                return
            name = info.get("name")
            if name:
                (self.music / Path(name).name).unlink(missing_ok=True)
            after = dict(before)
            after.pop("music", None)
            self._commit(before, after)

    def music_info(self) -> "dict | None":
        """The show's music, or None once its entry or its file is gone -
        an undo cannot restore bytes that were deleted, so this is the
        one place that checks the file is still really there."""
        info = self._load_show().get("music")
        if not isinstance(info, dict) or not info.get("name"):
            return None
        try:
            stat = (self.music / Path(info["name"]).name).stat()
        except OSError:
            return None
        return {"name": info["name"], "size": stat.st_size,
                "type": info.get("type") or music_type(info["name"]),
                "url": f"/api/music/file?v={int(stat.st_mtime)}"}

    # ---- exporting and importing the timeline ----

    def export_show(self) -> dict:
        """The parts of show.json a person would call "the show" - not
        boards/labels' per-item numbering trivia... actually those too,
        so a restore is exact; just not the music bytes, which travel
        separately (only the name is kept, as a reminder)."""
        with self._lock:
            show = self._migrate_align(self._load_show())
        music = show.get("music")
        return {
            "format": SHOW_FORMAT, "version": SHOW_FORMAT_VERSION,
            "exported": datetime.datetime.now().isoformat(timespec="seconds"),
            "workspace": self.root.name,
            "duration": float(show.get("duration", timeline.DEFAULT_DURATION_S)),
            "refresh_s": float(show.get("refresh_s", timeline.REFRESH_S)),
            "cues": timeline.clean(show.get("cues")),
            # Travels with the show, because it is part of how this
            # evening is run (see Workspace.set_clear_after_show).
            "clear_after_show": bool(show.get("clear_after_show")),
            # ...and so does the countdown before ③ START (11 s unless
            # somebody chose otherwise - Workspace.set_start_countdown).
            "start_countdown_s": start_countdown_of(show),
            # ...and the Loop (null = off), because the show file is how an
            # exhibition workspace gets from the PC to radxa-05 by hand.
            "loop_wait_s": loop_wait_of(show),
            "transitions": show.get("transitions") or {},
            "labels": show.get("labels") or {},
            "units": show.get("units") or {},
            "boards": show.get("boards") or {},
            "dips": show.get("dips") or {},
            "music": {"name": music["name"]} if isinstance(music, dict)
                     and music.get("name") else None,
        }

    def _validate_show(self, payload: dict) -> "tuple[dict, list[dict] | None]":
        """The validating half of import_show(): raises on a bad payload,
        otherwise returns (changes, cues) without touching disk. Split
        out so import_bundle() can check a show is good *before* writing
        a single CSV - a bad bundle must leave the workspace exactly as
        it found it."""
        if not isinstance(payload, dict):
            raise ValueError("not a show file")
        if payload.get("format") != SHOW_FORMAT:
            raise ValueError(f"not a show file (format {payload.get('format')!r}, "
                             f"want {SHOW_FORMAT!r})")
        if payload.get("version") != SHOW_FORMAT_VERSION:
            raise ValueError(f"show file version {payload.get('version')!r} "
                             f"is not supported (want {SHOW_FORMAT_VERSION})")
        changes: dict = {}
        if "duration" in payload:
            changes["duration"] = timeline.check_duration(payload["duration"])
        # An explicit null means "I am not setting it", the same as leaving the
        # key out - set_timeline()'s own contract for `refresh`, and what a
        # hand-made show file is likeliest to mean by it (review, 2026-09-26).
        if payload.get("refresh_s") is not None:
            try:
                refresh = round(float(payload["refresh_s"]), 1)
            except (TypeError, ValueError):
                raise ValueError(f"not a number of seconds: "
                                 f"{payload['refresh_s']!r}")
            low, high = timeline.REFRESH_RANGE_S
            if not low <= refresh <= high:
                raise ValueError(f"a refresh takes between {low:.0f} and "
                                 f"{high:.0f} s")
            changes["refresh_s"] = refresh
        # A show file written before this key existed simply does not
        # mention it and keeps whatever this workspace is set to - the
        # same "leave it alone" rule refresh_s has. The designers'
        # bundles never carry it (their simulator has no notion of the
        # fleet), so import_bundle() goes on ignoring it for free: this
        # only ever fires on a key that is really there.
        if "clear_after_show" in payload:
            if not isinstance(payload["clear_after_show"], bool):
                raise ValueError("clear_after_show: must be true or false")
            changes["clear_after_show"] = payload["clear_after_show"]
        # The same "leave it alone" rule: a show file or a designers' bundle
        # without the key (their simulator has no fleet and no START) keeps
        # whatever this workspace counts down - 11 s unless somebody changed
        # it here. An explicit null is "not setting it", like refresh_s.
        if payload.get("start_countdown_s") is not None:
            changes["start_countdown_s"] = check_start_countdown(
                payload["start_countdown_s"])
        # The Loop is the one key where null MEANS something - off - because
        # the show file has to be able to carry "no loop" to a Conductor
        # that has one on. A file without the key (older exports, the
        # designers' bundles) leaves the Loop as it is here.
        if "loop_wait_s" in payload:
            changes["loop_wait_s"] = check_loop_wait(payload["loop_wait_s"])
        if "transitions" in payload:
            if not isinstance(payload["transitions"], dict):
                raise ValueError("transitions: must be an object")
            changes["transitions"] = _clean_transitions(payload["transitions"])
        cues = None
        if "cues" in payload:
            if not isinstance(payload["cues"], list):
                raise ValueError("cues: must be a list")
            raw_cues = payload["cues"]
            if any(isinstance(c, dict) and "align" in c for c in raw_cues):
                # An older export: migrated the same way as a stored
                # show, using what refresh/transitions/maps this import
                # leaves the workspace with.
                current = self._load_show()
                refresh = changes.get(
                    "refresh_s", float(current.get("refresh_s", timeline.REFRESH_S)))
                own_transitions = changes.get(
                    "transitions", _clean_transitions(current.get("transitions")))
                maps = self._maps_for_migration(current)
                raw_cues = [self._migrate_cue(c, refresh, own_transitions, maps)
                           for c in raw_cues]
            cues = timeline.clean(raw_cues)
            changes["cues"] = cues
        if "labels" in payload:
            if not isinstance(payload["labels"], dict):
                raise ValueError("labels: must be an object")
            changes["labels"] = {str(k): v for k, v in payload["labels"].items()}
        if "units" in payload:
            if not isinstance(payload["units"], dict):
                raise ValueError("units: must be an object")
            units = {}
            for item, unit in payload["units"].items():
                if unit is None or unit == "":
                    continue
                if unit not in UNITS:
                    raise ValueError(f"unknown unit {unit!r}")
                units[str(item)] = unit
            changes["units"] = units
        if "boards" in payload:
            if not isinstance(payload["boards"], dict):
                raise ValueError("boards: must be an object")
            changes["boards"] = {str(k): v for k, v in payload["boards"].items()}
        # The DIP IDs set by hand travel with the boards they belong to: a
        # restore that brought back the garments' own numbering but forgot
        # which board's switches were changed would address that board by
        # its rank again, and the cue would land on the wrong panel.
        if "dips" in payload:
            if not isinstance(payload["dips"], dict):
                raise ValueError("dips: must be an object")
            changes["dips"] = {str(k): v for k, v in payload["dips"].items()}
        return changes, cues

    def _apply_show_changes(self, changes: dict, cues: "list[dict] | None"
                            ) -> "tuple[int, list[str]]":
        """The committing half of import_show(): one _commit, then
        warnings computed against whatever CSVs are on disk *right now*
        (import_bundle() calls this after its own CSVs have been saved,
        so a cue can reference a design that arrived in the same bundle)."""
        with self._lock:
            paths = sorted(self.files.glob("*.csv"))
            before = self._load_show()
            self._commit(before, dict(before, **changes))
        warnings: "list[str]" = []
        if cues is not None:
            items = {(_MAP_ITEM.match(p.name).group(1).lower())
                     for p in paths if self.kind(p.name) == "map"
                     and _MAP_ITEM.match(p.name)}
            designs = {p.name for p in paths if self.kind(p.name) == "grid"}
            for cue in cues:
                if cue["item"].lower() not in items:
                    warnings.append(f"{cue['item']}: no such item here yet")
                elif cue["design"] not in designs:
                    warnings.append(f"{cue['item']}: design {cue['design']} "
                                    "is not in this workspace")
        return len(cues or []), warnings

    def import_show(self, payload: dict) -> "tuple[int, list[str]]":
        """The reverse of export_show(): one _commit that replaces
        whatever keys the file mentions and leaves the rest (the music
        entry included) exactly as it was."""
        changes, cues = self._validate_show(payload)
        return self._apply_show_changes(changes, cues)

    def import_bundle(self, payload: dict) -> dict:
        """The designers' project file (conductor/web/sim's "Save
        project..."): their CSVs and their timeline in one file.

        Whole or nothing: every file name and the show itself are
        validated *before* a single byte is written (a name that fails
        the same safety check /api/files applies goes straight to
        `refused`, never renamed and never saved; a bad show raises
        before any CSV lands). The timeline then replaces itself exactly
        as import_show() does - one _commit - except that a bundle with
        no unit/board changes of its own (the normal case: the
        designers' simulator has no notion of either) leaves the
        operator's assignments and board renumbering here untouched
        instead of wiping them to {}. `units: {"<item>": null}` clears
        that one item at most - it can never wipe every assignment, the
        way an empty `units: {}` would if it were not told apart from
        "no units key at all".

        The bundle's own "music" entry is a name only, same as the show
        file's - the actual bytes always travel by hand and are picked
        again on this machine (docs/SIMULATOR_FOR_DESIGNERS.md), so this
        never touches self.music; the name is only handed back for the
        page's toast.

        Designs are replaced and reported (`overwritten`) - that is what
        sending a bundle is for. A garment's WIRING is not: every
        *_map.csv this workspace already holds stays exactly as it is and
        is reported in `kept` instead (2026-09-27, operator's decision).
        The maps here are regenerated from the 配線ナビ as the site
        changes a garment, and a designer's bundle carries whatever copy
        their simulator was started from - at 13:49 that put the morning's
        board 150 back to the stale layout without a word. `kept` entries
        are {name, why}: `why` is empty when the bundle's bytes are the
        same file (nothing happened, nothing to say) and
        BUNDLE_WIRING_KEPT when they differ, which the page shows. A kept
        map is in none of `saved`, `overwritten` or `renamed` - nothing of
        the bundle's landed under any name (its composed spelling is still
        used to rewrite the timeline's references, since that is the
        spelling the file here has). The operator replaces a map
        deliberately, by hand (Delete, then Add CSV). A bundle map for a
        garment this workspace does NOT have yet is written as before: a
        new garment arrives with its wiring.
        """
        if not isinstance(payload, dict):
            raise ValueError("not a bundle file")
        if payload.get("format") != BUNDLE_FORMAT:
            raise ValueError(f"not a bundle file (format {payload.get('format')!r}, "
                             f"want {BUNDLE_FORMAT!r})")
        if payload.get("version") != BUNDLE_FORMAT_VERSION:
            raise ValueError(f"bundle file version {payload.get('version')!r} "
                             f"is not supported (want {BUNDLE_FORMAT_VERSION})")
        show = payload.get("show")
        if not isinstance(show, dict):
            raise ValueError("bundle: show must be an object")
        files = payload.get("files") or {}
        if not isinstance(files, dict):
            raise ValueError("bundle: files must be an object")
        if len(files) > BUNDLE_MAX_FILES:
            raise ValueError(f"bundle: at most {BUNDLE_MAX_FILES} files "
                             f"(got {len(files)})")
        to_save: "list[tuple[str, str]]" = []
        refused: "list[str]" = []
        # The bundle's spelling -> this workspace's, for every name NFC
        # changes: a Japanese 配色案名 that arrives DECOMPOSED (a Mac hands
        # file names over in NFD) is the same name as the composed one and
        # must land on the same file. The cues and transitions that name
        # those files are rewritten to match below, or every one of them
        # would read "design ... is not loaded" against a file that IS
        # there under its composed spelling.
        renamed: "dict[str, str]" = {}
        claimed: "dict[str, str]" = {}     # saved name -> the spelling that took it
        for name in sorted(files):
            text = files[name]
            if not isinstance(name, str) or not isinstance(text, str):
                raise ValueError("bundle: files must be name -> text")
            try:
                # The same rule /api/files applies through
                # Workspace.save(), computed here without ever calling it,
                # so a name this workspace cannot keep is refused outright
                # rather than mangled into some other file's name.
                # Composing it (NFC, plus U+3000 and the outer whitespace)
                # is the one change allowed, and it is reported.
                clean = look_normalize(name)
                # name_problem() first, and Path() only as a backstop:
                # Path("a:b.csv").name is "b.csv" on Windows and the whole
                # string on Linux, so leading with it would give the same
                # bundle two different refusal reasons on two machines.
                problem = look_name_problem(clean)
                if not problem and Path(clean).name != clean:
                    problem = "a bundle's file names may not hold a path"
                if not problem and self.kind(clean) is None:
                    problem = NOT_A_CSV_NAME
            except (OSError, ValueError):     # e.g. an embedded NUL byte
                problem = "unusable file name"
            if problem:
                refused.append(f"{name}: {problem}")
                continue
            # Two entries whose composed forms coincide would have had the
            # second silently overwrite the first, with nothing in
            # `refused` to say a design had gone missing (review of
            # a6b610b). Name both spellings and keep neither guess.
            if clean in claimed:
                # Both spellings LOOK identical on screen - that is the
                # whole trouble - so the message says why rather than
                # printing the same string twice and leaving the designer
                # to wonder which two files it means.
                refused.append(
                    f"{name}: the same file name as {claimed[clean]} once "
                    "composed - they differ only in how the characters are "
                    "written; rename one of them")
                continue
            claimed[clean] = name
            if clean != name:
                renamed[name] = clean
            to_save.append((clean, text))
        show = _rename_design_refs(show, renamed)
        # units: only a *populated* mapping counts as "the bundle brought
        # its own" - {"Look22": null} cleans to {}, same as no units key
        # at all, so it cannot wipe every other assignment the operator
        # made here.
        cleaned_units = {k: v for k, v in (show.get("units") or {}).items() if v}
        units_kept = not cleaned_units
        if units_kept:
            show.pop("units", None)
        boards_kept = not show.get("boards")
        if boards_kept:
            show.pop("boards", None)
        # `dips` follows `boards` exactly: a bundle carrying none leaves
        # whatever this workspace knows about its own garments' switches
        # alone (a DIP ID set by hand is a fact about the boards standing
        # here, not about the timeline that arrived), and a bundle carrying
        # a populated mapping replaces it.
        dips_kept = not show.get("dips")
        if dips_kept:
            show.pop("dips", None)
        # Validate the whole timeline before a single CSV is written.
        changes, cues = self._validate_show(show)
        with self._lock:
            # Folded, like intake()'s own look: the operator's PC is
            # Windows, where "look22_map.csv" in a bundle IS the
            # "Look22_map.csv" already here (look.fold_name).
            on_disk = self._on_disk()            # folded name -> the name here
        saved: "list[str]" = []
        overwritten: "list[str]" = []
        kept: "list[dict]" = []
        kept_folded: "set[str]" = set()
        # The bundle's own spellings of the maps that were kept: dropped
        # from the `renamed` this call REPORTS, because nothing of theirs
        # was written under any name. The composing itself still has to
        # happen (it is what rewrote the cues, transitions, units, labels
        # and boards above onto the spelling this workspace keeps), so it
        # is `renamed` the reply loses, not the rewrite.
        kept_cleans: "set[str]" = set()
        for name, text in to_save:
            if self.kind(name) == "map":
                here = on_disk.get(look_fold_name(name))
                if here is not None:
                    # The wiring stays. Once per garment, whatever the
                    # bundle calls it: two entries of one map (two
                    # spellings, two cases) are one file here and one line
                    # for the operator to read.
                    kept_cleans.add(name)
                    if look_fold_name(here) not in kept_folded:
                        kept_folded.add(look_fold_name(here))
                        same = _same_csv(self._existing_text(here), text)
                        kept.append({"name": here,
                                     "why": "" if same else BUNDLE_WIRING_KEPT})
                    continue
            try:
                saved_name = self.save(name, text)
            except OSError as exc:
                raise ValueError(
                    f"could not save {name}: {exc} - {len(saved)} file(s) "
                    f"already saved, {len(refused)} refused before this")
            # Folded, like everything else about "the same file": a design
            # whose name differs from the one here only in case IS that
            # file on NTFS, and reporting it as a plain save (as an exact
            # `in existing` did) told the operator a design had been added
            # when it had in fact replaced one. Named by the spelling on
            # disk, which is the file that was written.
            folded = look_fold_name(saved_name)
            if folded in on_disk:
                overwritten.append(on_disk[folded])
            saved.append(saved_name)
            # ...and a second entry of this same bundle landing on it is an
            # overwrite too, not a second design.
            on_disk.setdefault(folded, saved_name)
        # The CHECK below (and every one the page runs afterwards) reads
        # the CSVs on disk, so a design drawn for the bundle's stale
        # layout is judged against the wiring that was KEPT - which is the
        # point: it is flagged here rather than at the run-through.
        cue_count, warnings = self._apply_show_changes(changes, cues)
        music = payload.get("music") or show.get("music")
        renamed_here = {old: new for old, new in renamed.items()
                        if new not in kept_cleans}
        return {"ok": True, "saved": saved, "refused": refused,
                "renamed": renamed_here,
                "overwritten": overwritten, "kept": kept, "cues": cue_count,
                "warnings": warnings, "units_kept": units_kept,
                "boards_kept": boards_kept, "dips_kept": dips_kept,
                "music": music.get("name") if isinstance(music, dict) else None}

    # ---- the whole workspace, between two Conductors ----
    # EXHIBITION mode (2026-09-30): the show is authored on the PC and has to
    # reach the Conductor on radxa-05 without ssh. One plain .tar carries
    # what the folder holds - show.json, history.json, files/*.csv and the
    # music file - and nothing else: fleet.json says where THIS host's units
    # are and stays with the host.

    def export_tar(self, out) -> dict:
        """Write the workspace as a .tar to the file object `out`. Under
        the lock, so the show.json and the CSVs in it are one moment's.
        Returns what went in ({files, music, show, history})."""
        counts = {"files": 0, "music": None, "show": False, "history": False}

        def add(tar, path: Path, arcname: str) -> None:
            info = tar.gettarinfo(str(path), arcname)
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            with open(path, "rb") as handle:
                tar.addfile(info, handle)

        with self._lock:
            music = self._load_show().get("music")
            with tarfile.open(fileobj=out, mode="w") as tar:
                for name in _TAR_TOP:
                    path = self.root / name
                    if path.is_file():
                        add(tar, path, name)
                        counts["show" if name == "show.json" else "history"] = True
                for path in sorted(self.files.glob("*.csv")):
                    if path.is_file():
                        add(tar, path, f"files/{path.name}")
                        counts["files"] += 1
                if isinstance(music, dict) and music.get("name"):
                    path = self.music / Path(music["name"]).name
                    if path.is_file():
                        add(tar, path, f"music/{path.name}")
                        counts["music"] = path.name
        return counts

    @staticmethod
    def _tar_member_ok(member) -> "str | None":
        """Where this member lands, relative to the root - or None for one
        that is skipped (a directory entry; a fleet.json, which is per host).
        ValueError for anything the workspace may not hold: a path outside
        the four names above, a link, a device, a name a CSV or the music
        could not have."""
        name = member.name.replace("\\", "/")
        while name.startswith("./"):
            name = name[2:]
        parts = [p for p in name.split("/") if p not in ("", ".")]
        if not parts or ".." in parts:
            raise ValueError(f"{member.name}: not a workspace path")
        if member.isdir():
            if len(parts) == 1 and parts[0] in _TAR_DIRS:
                return None
            raise ValueError(f"{member.name}: not a workspace folder")
        if not member.isfile():
            raise ValueError(f"{member.name}: only plain files travel")
        if len(parts) == 1:
            if parts[0] == "fleet.json":
                return None                 # the receiver's own stays
            if parts[0] not in _TAR_TOP:
                raise ValueError(f"{member.name}: not part of a workspace")
            return parts[0]
        if len(parts) != 2 or parts[0] not in _TAR_DIRS:
            raise ValueError(f"{member.name}: not part of a workspace")
        folder, leaf = parts
        if folder == "files":
            problem = look_name_problem(leaf)
            if problem or file_kind(leaf) is None or look_normalize(leaf) != leaf:
                raise ValueError(f"{member.name}: {problem or NOT_A_CSV_NAME}")
        else:
            if (safe_music_name(leaf) != leaf
                    or Path(leaf).suffix.lower() not in _MUSIC_TYPES):
                raise ValueError(f"{member.name}: not a music file name")
        return f"{folder}/{leaf}"

    def import_tar(self, source) -> dict:
        """Replace the workspace with the .tar at `source` (a path).

        Whole or not at all, and nothing outside this folder: each member
        is checked by name as it is read (_tar_member_ok) and written into
        a staging folder BESIDE the workspace's own files - a member that
        fails the check aborts the import with the staging folder removed
        and the workspace untouched - and only then, under the lock, with
        nothing running (the caller's 409), are the four entries swapped:
        the old ones move aside, the new ones move in, the old ones are
        deleted. A failure half way through moves the old ones back and
        removes only what this import had placed. fleet.json is
        never touched (a fleet.json inside the tar is skipped), the
        receiver's marks of what its units hold are forgotten (the
        timeline is new: START says "Upload again", which is right), and
        the caller's compile is the reload - the workspace is read from
        disk on every request anyway.

        Returns {files, music, show, history}: what landed."""
        source = Path(source)
        if source.stat().st_size > WORKSPACE_TAR_MAX:
            raise ValueError(f"the workspace is at most "
                             f"{WORKSPACE_TAR_MAX // (1024 * 1024)} MB")
        tag = f"{os.getpid()}-{threading.get_ident()}"
        stage = self.root / f".import-{tag}"
        aside = self.root / f".import-old-{tag}"
        counts = {"files": 0, "music": None, "show": False, "history": False}
        try:
            shutil.rmtree(stage, ignore_errors=True)
            (stage / "files").mkdir(parents=True)
            # A plain tar only ("r:", never "r:*": a compressed one could
            # hold anything behind a small Content-Length), one member at
            # a time as it is read - never getmembers() up front, which
            # would walk an unbounded index - and at most
            # WORKSPACE_TAR_MEMBERS of them. A member that fails the name
            # check aborts the whole import before anything is swapped.
            with tarfile.open(str(source), mode="r:") as tar:
                total = 0
                count = 0
                for member in tar:
                    count += 1
                    if count > WORKSPACE_TAR_MEMBERS:
                        raise ValueError(f"more than {WORKSPACE_TAR_MEMBERS} "
                                         "entries in the tar")
                    where = self._tar_member_ok(member)
                    if where is None:
                        continue
                    total += max(0, int(member.size))
                    if total > WORKSPACE_TAR_MAX:
                        raise ValueError(f"the workspace is at most "
                                         f"{WORKSPACE_TAR_MAX // (1024 * 1024)} MB")
                    target = stage / where
                    target.parent.mkdir(parents=True, exist_ok=True)
                    handle = tar.extractfile(member)
                    if handle is None:
                        raise ValueError(f"{member.name}: unreadable")
                    with handle, open(target, "wb") as out:
                        shutil.copyfileobj(handle, out, WORKSPACE_TAR_CHUNK)
                    if where == "show.json":
                        counts["show"] = True
                    elif where == "history.json":
                        counts["history"] = True
                    elif where.startswith("files/"):
                        counts["files"] += 1
                    else:
                        counts["music"] = Path(where).name
            # The two JSON files have to parse, or the workspace they make
            # would open on an empty show and say nothing about why.
            for name in _TAR_TOP:
                path = stage / name
                if path.is_file():
                    try:
                        json.loads(path.read_text(encoding="utf-8"))
                    except (OSError, ValueError) as exc:
                        raise ValueError(f"{name}: not JSON ({exc})")
            with self._lock:
                self._swap_in(stage, aside)
                self.marks.clear()
                self.unit_marks.clear()
                self.compiled = None
        finally:
            shutil.rmtree(stage, ignore_errors=True)
            shutil.rmtree(aside, ignore_errors=True)
        return counts

    def _swap_in(self, stage: Path, aside: Path) -> None:
        """The swap itself (lock held): the workspace's four entries move
        into `aside`, the staged ones move in. A failure moves back what
        had already moved, so the workspace is never half of each."""
        names = list(_TAR_DIRS) + list(_TAR_TOP)
        aside.mkdir(parents=True, exist_ok=True)
        moved: "list[str]" = []           # originals now in `aside`
        placed: "list[str]" = []          # staged entries now in the root
        try:
            for name in names:
                here = self.root / name
                if here.exists():
                    os.replace(str(here), str(aside / name))
                    moved.append(name)
            for name in names:
                fresh = stage / name
                if fresh.exists():
                    os.replace(str(fresh), str(self.root / name))
                    placed.append(name)
        except OSError:
            # Only what THIS import put there goes; an original that was
            # never moved aside (the failure came before its turn) is left
            # exactly where it is, and the ones moved aside come back.
            for name in placed:
                landed = self.root / name
                if landed.is_dir():
                    shutil.rmtree(landed, ignore_errors=True)
                elif landed.exists():
                    landed.unlink(missing_ok=True)
            for name in moved:
                os.replace(str(aside / name), str(self.root / name))
            raise
        self.files.mkdir(parents=True, exist_ok=True)

    # ---- files ----

    # One grammar for both sides of the wire: conductor/look.py's
    # file_kind() also reads the production site's own
    # <item>_<配色案名>_HW.csv as a grid, so a file straight from the
    # "HW 用 CSV" button uploads without being renamed first.
    kind = staticmethod(file_kind)

    def save(self, name: str, text: str) -> str:
        """Write one CSV, replacing whatever is there under that name.

        The plain write, and it stays that way: import_bundle() is
        documented to overwrite the DESIGNS it names and to report what it
        overwrote (it keeps a garment's wiring and never calls this for
        it). The /api/files path does NOT come here directly - it goes
        through intake() below, which never lets one file land on another.
        """
        with self._lock:
            return self._save_locked(name, text)

    def _save_locked(self, name: str, text: str) -> str:
        """save(), with self._lock already held by the caller.

        intake() holds the lock across the whole of its own work - the
        moment it lets go between "which names are free" and "write", two
        requests can pick the same free name and one clobbers the other
        (review of dbed7d5, 25 of 25 runs with two threads). threading.Lock
        is not reentrant, so the two halves have to be separate functions.
        """
        name = workspace_name(name)          # raises on an unusable name
        if self.kind(name) is None:
            raise ValueError(f"{name}: {NOT_A_CSV_NAME}")
        # open(), not Path.write_text(newline=...): that is 3.10+, and
        # the units' Python 3.9 should be able to run this too.
        with open(self.files / name, "w", encoding="utf-8",
                  newline="") as handle:
            handle.write(text)
        return name

    def item_names(self) -> "list[str]":
        """The garments this workspace has a wiring file for."""
        names = []
        for path in sorted(self.files.glob("*.csv")):
            if self.kind(path.name) != "map":
                continue
            named = _MAP_ITEM.match(file_stem(path.name))
            if named:
                names.append(named.group(1))
        return names

    def _on_disk(self) -> "dict[str, str]":
        """{the folded name: the name as the folder actually spells it}.

        Folded, because the operator's PC is Windows: NTFS cannot tell
        "…_Pattern_grid.csv" from "…_pattern_grid.csv", so neither may
        anything here (see look.fold_name).
        """
        return {look_fold_name(path.name): path.name
                for path in sorted(self.files.glob("*.csv"))}

    def _existing_text(self, name: str) -> "str | None":
        try:
            return (self.files / name).read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            return None

    def intake(self, entries, item: "str | None" = None) -> dict:
        """Several picked files at once - the /api/files path.

        Nothing here ever lands on a file that is already in the
        workspace (2026-09-26, "the 4th of 5 CSVs was overwritten"):

          * a Mac's own clutter is turned away by name, before the text
            is looked at - the "._NAME.csv" AppleDouble twins end in
            _map.csv and used to become garments of their own;
          * Safari's "NAME.csv.txt" is read as the NAME.csv it is;
          * the same bytes under a name the workspace already holds is
            the same file picked twice - set aside, not saved again;
          * anything else that would land on a name already taken (by a
            file on disk OR by an earlier file of this same pick) is
            numbered "-2", "-3"... on its DESIGN name, so nothing is
            lost. A *_map.csv has nowhere to put a number - a garment
            has one wiring file - so a second, different one is refused
            with what to do about it.

        `item` is a garment's own "Add CSV": every file is renamed onto
        that garment, because a design CSV belongs to the garment its
        name begins with and two garments of the same shape come back
        from the designer under the same file names. With ONE exception
        - another garment's *_map.csv, which is not interchangeable the
        way its designs are: renaming it onto this one would replace
        this garment's wiring with another garment's, throw away the
        original, and report it as a success (the simulator's own
        adversarial review F1).

        Returns {"saved": [...], "renamed": [{from, to}], "skipped":
        [{from, as}], "refused": ["name: why"]} - the page says all four
        out loud, so a file that was picked and did not arrive under its
        own name can never pass unnoticed. Lists, not maps keyed by the
        picked name: a folder drop can hold two "pattern.csv" one
        directory apart, and a map would report one of them.

        The whole of it runs under self._lock. Deciding which names are
        free and then writing without the lock let two requests - two
        tabs, two operators, a re-fired drop - pick the same free name
        and one clobber the other (review of dbed7d5).

        import_bundle() deliberately does NOT come this way: a bundle
        replaces the designs it names and reports what it overwrote
        (docs/SIMULATOR_FOR_DESIGNERS.md), which is the whole point of
        sending one. It agrees with this function about WIRING, though -
        since 2026-09-27 a bundle's *_map.csv for a garment already here
        is kept, not written, and the operator replaces one deliberately
        (Delete, then Add CSV) exactly as the rule above says.
        """
        with self._lock:
            return self._intake_locked(list(entries), item)

    def _intake_locked(self, entries, item: "str | None") -> dict:
        on_disk = self._on_disk()                 # folded name -> real name
        items = self.item_names()
        saved: "list[str]" = []
        renamed: "list[dict]" = []
        skipped: "list[dict]" = []
        refused: "list[str]" = []

        def name_of(entry) -> str:
            return str(entry.get("name", "")) if isinstance(entry, dict) else ""

        if item is not None:
            # Spelled the way the workspace spells it, so everything
            # below compares one name with itself.
            known = {look_fold_name(name): name for name in items}
            item = known.get(look_fold_name(item))
            if item is None:
                return {"saved": [], "renamed": [], "skipped": [],
                        "refused": [f"{name_of(e)}: {look_no_such_garment}"
                                    for e in entries]}

        # ---- pass 1: what each picked file wants to be called ----
        # Resolved for EVERY entry before any of them is numbered, so a
        # "-2" can never steal the name a genuine "…-2_grid.csv" in the
        # same pick already wants (review of dbed7d5: a new A_grid landing
        # on an occupied name became A-2_grid, and the real A-2_grid that
        # came with it was then pushed to A-2-2_grid).
        wanted: "list[str | None]" = []
        for entry in entries:
            raw = name_of(entry)
            wanted.append(None)
            if not isinstance(entry, dict):
                refused.append(f"{raw}: a file must be sent as "
                               "{name, text}")
                continue
            text = entry.get("text")
            if not isinstance(text, str):
                refused.append(f"{raw}: no text was sent with this file")
                continue
            if look_is_mac_metadata(raw):
                refused.append(f"{raw}: a macOS metadata file, not one of "
                               "the designers' CSVs")
                continue
            resolved = look_conventional_name(look_mac_safe_name(raw), text,
                                              item, items)
            if "error" in resolved:
                refused.append(f"{raw}: {resolved['error']}")
                continue
            if item is not None:
                owner = (map_item(resolved["name"])
                         if self.kind(resolved["name"]) == "map" else None)
                if (owner and look_fold_name(owner) != look_fold_name(item)
                        and look_fold_name(owner) in
                        {look_fold_name(i) for i in items}):
                    refused.append(f"{raw}: {look_another_garments_map}")
                    continue
                onto = look_rename_onto_item(item, resolved["name"])
                if onto is None:
                    refused.append(f"{raw}: {look_refuse_reason(resolved['name'])}")
                    continue
                resolved = dict(resolved, name=onto)
            try:
                # The name conventional_name() built is still put through
                # the shared rule: the garment half of it can come from a
                # caller, not only from a map already on disk.
                name = workspace_name(resolved["name"])
            except ValueError as exc:
                refused.append(f"{raw}: {exc}")
                continue
            if self.kind(name) is None:
                refused.append(f"{raw}: {NOT_A_CSV_NAME}")
                continue
            wanted[-1] = name

        # ---- pass 2: claim a name for each, and write ----
        claimed = set(on_disk)                    # folded
        for index, entry in enumerate(entries):
            target = wanted[index]
            if target is None:
                continue
            raw, text = name_of(entry), entry["text"]
            folded = look_fold_name(target)
            here = on_disk.get(folded)
            if here is not None and _same_csv(self._existing_text(here), text):
                skipped.append({"from": raw, "as": here})
                continue
            if folded in claimed:
                # Every OTHER picked file's own name is off limits while
                # this one looks for a free "-2".
                others = {look_fold_name(w) for i, w in enumerate(wanted)
                          if w is not None and i != index}
                name = look_unique_save_name(target, claimed | others)
            else:
                name = target
            if name is None:
                refused.append(
                    f"{raw}: {here or target} is already here and a garment "
                    "has one wiring file - delete it first if this "
                    "replaces it")
                continue
            try:
                self._save_locked(name, text)
            except (OSError, ValueError) as exc:
                refused.append(f"{raw}: {exc}")
                continue
            claimed.add(look_fold_name(name))
            on_disk[look_fold_name(name)] = name
            saved.append(name)
            if name != raw:
                renamed.append({"from": raw, "to": name})
        return {"saved": saved, "renamed": renamed, "skipped": skipped,
                "refused": refused}

    def delete(self, name: str) -> None:
        target = self.files / Path(name).name
        with self._lock:
            if target.is_file():
                target.unlink()

    # ---- what goes to the units ----

    def _fleet_json(self) -> dict:
        try:
            config = json.loads((self.root / "fleet.json")
                                .read_text(encoding="utf-8"))
        except (OSError, ValueError):
            config = {}
        return config if isinstance(config, dict) else {}

    def fleet_config(self) -> "tuple[dict, str | None]":
        config = self._fleet_json()
        units = dict(default_units())
        units.update(config.get("units") or {})
        return units, config.get("token") or None

    def fleet_option(self, key: str, default=None):
        """One more key of fleet.json (EXHIBITION mode): "passcode" (the
        page's, see PASSCODE_HEADER), "hotspot" (the unit that is the
        AZ-Epaper hotspot, radxa-05 unless said otherwise) or
        "speaker_volume" (the host's loudness, conductor/speaker.py)."""
        value = self._fleet_json().get(key)
        return default if value in (None, "") else value

    def set_fleet_option(self, key: str, value) -> None:
        """Write one key of fleet.json, keeping the rest - the per-host
        settings the page changes (the speaker's volume). Whole or not at
        all, like show.json; the file's mode is kept (a chmod 600 with the
        passcode in it stays 600); a fleet.json that does not PARSE is
        never overwritten - RuntimeError, the caller's 500."""
        path = self.root / "fleet.json"
        with self._lock:
            try:
                raw = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                raw = None
            if raw is None:
                config: dict = {}
            else:
                try:
                    config = json.loads(raw)
                except ValueError as exc:
                    raise RuntimeError(f"fleet.json does not parse ({exc}) - "
                                       "not overwriting it; fix it by hand")
                if not isinstance(config, dict):
                    raise RuntimeError("fleet.json is not an object - not "
                                       "overwriting it; fix it by hand")
            config[key] = value
            mode = None
            try:
                mode = os.stat(path).st_mode & 0o777
            except OSError:
                pass
            scratch = path.with_name(path.name + ".tmp")
            scratch.write_text(json.dumps(config, indent=1), encoding="utf-8")
            if mode is not None:
                try:
                    os.chmod(scratch, mode)
                except OSError:
                    pass
            os.replace(scratch, path)

    def compile_units(self, choices: "dict[str, str]", cue: str
                      ) -> "tuple[dict[str, dict], list[str]]":
        """{item: design file} -> ({unit: the agent's /prepare body}, problems).

        Addresses run across everything the unit carries, chosen or not,
        so a board keeps its DIP setting whichever items a cue touches.
        A design with undecided (-) scales is sent as a partial cue.
        """
        with self._lock:
            paths = sorted(self.files.glob("*.csv"))
            show = self._load_show()
            assigned = show.get("units", {})
        maps: "dict[str, LookMap]" = {}
        for path in paths:
            if self.kind(path.name) == "map":
                try:
                    look_map = LookMap.from_csv(path)
                except (OSError, LookError):
                    continue            # reported where the item is chosen
                look_map = self._renumbered(look_map, show)
                maps[(look_map.item or path.stem).lower()] = look_map
        problems: "list[str]" = []
        payloads: "dict[str, dict]" = {}
        for item, design_name in sorted(choices.items()):
            look_map = maps.get(item.lower())
            unit = assigned.get(look_map.item) if look_map else None
            if look_map is None:
                problems.append(f"{item}: no map")
                continue
            if unit is None:
                problems.append(f"{item}: not assigned to a unit")
                continue
            try:
                design = Design.from_csv(self.files / Path(design_name).name,
                                         items=_known_items(maps))
                on_unit = [m for key, m in maps.items()
                           if assigned.get(m.item) == unit]
                ids = unit_board_ids(on_unit,
                                     self._unit_dips(show, on_unit),
                                     unit=unit)
                partial = bool(check(look_map, design))
                arrays = compile_design(look_map, design, partial=partial,
                                        ids=ids)
            except (OSError, LookError) as exc:
                problems.append(f"{item}: {exc}")
                continue
            name = design.label or design.name
            payload = payloads.setdefault(unit, {
                "cue": cue, "label": "", "dev_type": NUMBER_BRAND,
                "boards": {},
                # How long this cue needs to finish once it is fired.
                # The unit's guard STOP waits for it instead of a flat
                # 12 s, so a long sweep is never cut off half-drawn
                # (ui/runner.py's _guard_for(), F1 2026-09-26). `span_s`
                # grows below with whatever the chosen designs sweep.
                "refresh_s": float(show.get("refresh_s", timeline.REFRESH_S)),
                "span_s": 0.0})
            # showfile.unit_label(): the same fold the timeline's own cue
            # labels get, for the same reason - this string is drawn on
            # the unit's screen by a font with no CJK glyphs. The design's
            # real name, full-width characters and all, stays in the
            # workspace and on the Conductor's own screens.
            payload["label"] = (payload["label"] + " + " if payload["label"]
                                else "") + showfile.unit_label(
                                    f"{look_map.item} {name}")
            payload["boards"].update({str(address): array.hex()
                                      for address, array in arrays.items()})
            # The design's own transition (Designs tab) sweeps a manual
            # cue the same way it sweeps a timeline cue: the tables go
            # with the colours, a natural design sends none and the
            # boards keep whatever table they hold until the show says.
            sweep = (show.get("transitions") or {}).get(Path(design_name).name)
            if isinstance(sweep, dict):
                seq = sequence.clean_sequence(sweep.get("sequence"))
                span = sequence.clean_span(sweep.get("span_s"))
                if seq != "natural" and span > 0:
                    tables = sequence.compile_delays(look_map, seq, span, ids=ids)
                    payload.setdefault("delays", {}).update(
                        {str(address): table.hex()
                         for address, table in tables.items()})
                    # Items sharing a unit are one broadcast: the cue is
                    # only finished when the slowest sweep on it is.
                    payload["span_s"] = max(
                        payload["span_s"],
                        sequence.span_s(look_map, seq, span))
        return payloads, problems

    def revision(self) -> str:
        """A fingerprint of everything compile_show() reads: the parts of
        show.json that reach the units, and the name, size and mtime of
        every CSV. Two revisions being equal means an Upload right now
        would send the units exactly what they already hold.

        Not the whole of show.json: the music and the LOOK / model labels
        are the operator's own notes about the show and never leave this
        PC, so loading a track or renaming a look would otherwise turn
        every chip red for nothing (found in review). "Clear pictures
        after the show" is left out for the same reason though it does
        reach a unit - see _REVISION_IGNORES.

        Why not the compiled show ids themselves: compiling builds every
        picture of every board (measured 2026-09-25: 364 ms for a two-unit
        toy show of five cues, so seconds for ten units of eighteen), and
        the page asks for this on every poll - once per second while the
        Units tab is open. This costs one stat per CSV, about a
        millisecond, and is wrong only in the harmless direction: an edit
        that happens to compile to the same pictures reads as "changed
        since" until the next Upload, never the other way round."""
        show = {key: value for key, value in self._load_show().items()
                if key not in _REVISION_IGNORES}
        parts = [json.dumps(show, sort_keys=True, default=str)]
        for path in sorted(self.files.glob("*.csv")):
            try:
                info = path.stat()
            except OSError:                 # deleted between glob and stat
                continue
            parts.append(f"{path.name}:{info.st_size}:{info.st_mtime_ns}")
        return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()[:16]

    def mark_written(self, what: str, rev: "str | None" = None,
                     units: "list[str] | None" = None,
                     all_units: "list[str] | None" = None,
                     whole: bool = True) -> None:
        """Remember `rev` (the revision the write actually carried, taken
        before it started) under `what` ("upload", or "demo:<NAME>"), for
        the `units` it reached.

        Not the revision NOW: writing ten units takes seconds, and an
        edit that lands while it is in flight belongs to the next write,
        not this one - recording it here would have the chips say "up to
        date" about a timeline the units have never seen (found in
        review). `rev` is only omitted where there is nothing in between
        to worry about.

        The fleet-wide mark (what the chips' "up to date" is read from)
        is only set when every unit of `all_units` - the units of the
        whole compiled timeline - now holds `rev`. One LOOK written on
        its own leaves the rest of the fleet on whatever they held, so
        claiming the timeline is on the units would be the chip lying
        about the one thing it exists to answer; the fleet-wide mark is
        dropped instead, and the per-unit ones say who does hold this
        revision.

        `whole=False` says the write itself could not have carried the
        whole timeline however many units it reached - it left something
        out (showfile.build's `warnings`: a garment with cues and no unit
        at all). The fleet-wide mark is read as "a full Upload would send
        the units what they already hold", and a full Upload of such a
        timeline is still refused, so it must not be set here."""
        rev = self.revision() if rev is None else rev
        if units is None:
            self.marks[what] = rev
            return
        per_unit = self.unit_marks.setdefault(what, {})
        for unit in units:
            per_unit[unit] = rev
        targets = list(all_units if all_units is not None else units)
        # A unit the timeline no longer reaches is not "behind" - it is
        # not in the show at all, and leaving its mark here would have
        # START refuse for a garment that was taken out weeks ago.
        for unit in [u for u in per_unit if u not in targets]:
            del per_unit[unit]
        if whole and targets and all(per_unit.get(unit) == rev
                                     for unit in targets):
            self.marks[what] = rev
        else:
            self.marks.pop(what, None)

    def timeline_units(self) -> "set[str]":
        """The units this timeline needs - every unit an item with a cue
        is assigned to. What compile_show() would target, read straight
        out of show.json: no CSV is parsed and no picture is built, so
        START can ask it without paying the seconds a compile costs."""
        show = self._load_show()
        assigned = show.get("units") or {}
        return {assigned[cue["item"]] for cue in (show.get("cues") or [])
                if isinstance(cue, dict) and assigned.get(cue.get("item"))}

    def forget_demo(self, slug: str) -> None:
        """Drop the marks of a demo just deleted from the units - the
        name is free again, and a mark left behind would have the next
        demo written under it inherit an "up to date" it never earned."""
        for key in [k for k in self.marks
                    if k.startswith("demo:") and _slugify(k[5:]) == slug]:
            self.marks.pop(key, None)
        for key in [k for k in self.unit_marks
                    if k.startswith("demo:") and _slugify(k[5:]) == slug]:
            self.unit_marks.pop(key, None)

    def written_state(self) -> dict:
        """What /api/fleet tells the page about the timeline itself:
        `revision` now, and the revision each write put on the units -
        fleet-wide (`uploaded`, `demos`: set only when the write reached
        every unit of the timeline) and per unit (`uploaded_units`,
        `demo_units`, which is how the dialog and the tiles say WHICH
        units hold what is on screen after a one-LOOK write).
        A name with no mark (written by an earlier conductor, or before
        this conductor came up) is simply absent - the page then says
        nothing about "up to date", rather than guessing."""
        return {"revision": self.revision(),
                "uploaded": self.marks.get("upload"),
                "uploaded_units": dict(self.unit_marks.get("upload", {})),
                "demos": {key[len("demo:"):]: value
                          for key, value in self.marks.items()
                          if key.startswith("demo:")},
                "demo_units": {key[len("demo:"):]: dict(value)
                               for key, value in self.unit_marks.items()
                               if key.startswith("demo:")}}

    def compile_show(self) -> "tuple[dict[str, dict], list[str]]":
        """The whole timeline -> ({unit: show file}, problems)."""
        shows, problems, _ = self.compile_for_write()
        return shows, problems

    def compile_for_write(self, only: "list[str] | None" = None
                          ) -> "tuple[dict[str, dict], list[str], list[str]]":
        """({unit: show file}, problems, warnings) - compile_show() with
        the write's own reach named. `only` is the units of the one LOOK
        the dialog's "Which LOOKs" picked: a problem about an item on
        another unit is then a warning rather than a refusal, because
        that unit is not written to at all (showfile.build's own rule).
        `None` is the whole timeline, whole or not at all."""
        # Taken before the work, so what is remembered below is the state
        # this compile was OF, not one an edit landed on meanwhile.
        rev = self.revision()
        shows, problems, warnings = self._compile_show(only)
        # Both halves, because `compiled` answers a question about the
        # TIMELINE and not about this write: START reads it to tell "the
        # units disagree" from "the timeline does not even build" (the
        # _one_timeline gate), and a problem a one-LOOK compile merely
        # warned about is exactly the second thing.
        self.compiled = {"revision": rev,
                         "problems": list(problems) + list(warnings),
                         "units": sorted(shows)}
        return shows, problems, warnings

    def _compile_show(self, only: "list[str] | None" = None
                      ) -> "tuple[dict[str, dict], list[str], list[str]]":
        with self._lock:
            paths = sorted(self.files.glob("*.csv"))
            show = self._migrate_align(self._load_show())
        assigned = show.get("units", {})
        maps: "dict[str, LookMap]" = {}
        facts: "dict[str, dict]" = {}
        broken: "list[str]" = []
        for path in paths:
            if self.kind(path.name) != "map":
                continue
            try:
                look_map = LookMap.from_csv(path)
            except (OSError, LookError) as exc:
                # Said here, at upload time - not as a vague "no such
                # item" on every cue of the garment.
                broken.append(f"{path.name}: {exc}")
                continue
            look_map = self._renumbered(look_map, show)
            maps[(look_map.item or path.stem).lower()] = look_map
        designs: "dict[str, Design]" = {}
        for path in paths:
            if self.kind(path.name) == "grid":
                try:
                    designs[path.name] = Design.from_csv(
                        path, items=_known_items(maps))
                except (OSError, LookError):
                    pass
        for key, look_map in maps.items():
            mine = {name: d for name, d in designs.items()
                    if (d.item or "").lower() == key}
            facts[key] = {
                "item": look_map.item, "unit": assigned.get(look_map.item),
                "boards": len(look_map.board_nos),
                "designs": {name: {"full": not check(look_map, d),
                                   "partial": not check(look_map, d, True)}
                            for name, d in mine.items()}}
        refresh = float(show.get("refresh_s", timeline.REFRESH_S))
        duration = float(show.get("duration", timeline.DEFAULT_DURATION_S))
        cues = timeline.clean(show.get("cues"))
        timeline.apply_transitions(cues, show.get("transitions") or {})
        _time_sweeps(cues, maps)
        cue_problems, _ = timeline.validate(cues, facts, duration, refresh)
        if broken:
            # A map that will not parse is not one garment's business: the
            # item is missing from `maps` altogether, so nothing here can
            # say which unit it would have been on. Blocking, `only` or
            # not - it is a repair, not a write.
            return {}, broken, []
        clear = bool(show.get("clear_after_show"))
        loop_on = loop_wait_of(show) is not None
        # With the Loop on, the UNIT's copy of the show must not clear
        # itself at its own ENDED (it would, from the show file's flag,
        # whatever the conductor's loop guard says - and the restart would
        # then meet "cleared" and die). The conductor keeps the operator's
        # flag under its own key and performs the clear itself, on STOP.
        # Both keys are added after the show id's digest (showfile.build),
        # so neither changes the id nor asks for a "changed since" Upload.
        shows, problems, warnings = showfile.build(
            maps, assigned, lambda name: designs[name], cues, refresh,
            duration, cue_problems, name=self.root.name, only=only,
            dips=show.get("dips"), clear_after_show=clear and not loop_on)
        if clear and loop_on:
            for unit_show in shows.values():
                unit_show["conductor_clear_after_show"] = True
        return shows, problems, warnings

    # ---- the state the page draws ----

    def state(self) -> dict:
        with self._lock:
            paths = sorted(self.files.glob("*.csv"))
            show = self._migrate_align(self._load_show())
            history = self._load_history()
            assigned = show.get("units", {})
            labels = show.get("labels", {})
            transitions = show.get("transitions") or {}
        maps: "dict[str, LookMap]" = {}
        items: "dict[str, dict]" = {}

        def item_entry(name: str) -> dict:
            label = labels.get(name)
            if not isinstance(label, dict):
                # Until somebody says otherwise, Look22 is LOOK 22.
                number = _LOOK_NO.match(name)
                label = {"look": str(int(number.group(1))) if number else "",
                         "model": ""}
            return items.setdefault(name.lower(), {
                "item": name, "unit": assigned.get(name), "map": None,
                "look": str(label.get("look") or ""),
                "model": str(label.get("model") or ""),
                "designs": [], "problems": []})

        for path in paths:
            if self.kind(path.name) != "map":
                continue
            try:
                look_map = LookMap.from_csv(path)
            except (OSError, LookError) as exc:
                entry = item_entry(Path(path.stem[:-4]).name)
                entry["map"] = {"name": path.name, "scales": [], "sides": [],
                                "shifts": {}}
                entry["problems"] += getattr(exc, "problems", [str(exc)])
                continue
            look_map = self._renumbered(look_map, show)
            entry = item_entry(look_map.item or path.stem)
            maps[entry["item"].lower()] = look_map
            # Only the rows where the map disagrees with default_shift() -
            # the page's shiftOf() already falls back to that same rule,
            # so a look with no shift column (or none of its rows differ)
            # sends none of this and draws exactly as before.
            rows = {(s.side, s.row) for s in look_map.scales}
            shifts = {_key(pos): look_map.shift(*pos) for pos in rows
                      if look_map.shift(*pos) != default_shift(pos[1])}
            entry["map"] = {
                "name": path.name, "sides": look_map.sides,
                "warnings": look_map.warnings, "shifts": shifts,
                "scales": [[s.side, s.row, s.col, s.board_no, s.socket]
                           for s in look_map.scales]}

        orphans = []
        for path in paths:
            if self.kind(path.name) != "grid":
                continue
            known = _known_items(maps)
            try:
                design = Design.from_csv(path, items=known)
                problems = []
            except (OSError, LookError) as exc:     # deleted meanwhile, too
                design, problems = None, getattr(exc, "problems", [str(exc)])
            item = (design.item if design else None) or path.stem
            look_map = maps.get(item.lower())
            record = {"name": path.name,
                      "pattern": design.pattern if design else None,
                      "label": (design.label if design
                                else Design.name_parts(path.name, known)[2]),
                      # problems: as a full cue. partial_problems: as a
                      # cue that leaves uncoloured scales as they are - a
                      # design that only passes that way is a partial one,
                      # not a broken one.
                      "problems": problems, "partial_problems": problems,
                      "colors": {}, "shifts": {}, "undecided": [],
                      "transition": _design_transition(transitions.get(path.name))}
            if design is not None:
                record["colors"] = {_key(p): c for p, c in design.colors.items()}
                record["shifts"] = {_key(p): s for p, s in design.shifts.items()}
                record["undecided"] = [_key(p) for p in sorted(design.undecided)]
                if look_map is not None:
                    record["problems"] = check(look_map, design)
                    record["partial_problems"] = check(look_map, design,
                                                       partial=True)
            if look_map is None and item.lower() not in items:
                record["problems"] = (record["problems"]
                                      or [f"no {item}_map.csv yet"])
                orphans.append(record)
            else:
                item_entry(item)["designs"].append(record)

        # Addresses are per unit: items sharing a unit share its bus.
        groups: "dict[str, list[str]]" = {}
        for key, entry in items.items():
            if key in maps:
                groups.setdefault(entry["unit"] or f"\0{key}", []).append(key)
        unit_problems: "dict[str, list[str]]" = {}
        # Every address a hand-set DIP produced, and every one whose
        # switches the operator reported flaky - gathered here so state()
        # can warn about the whole show in one place, before Upload.
        dip_warnings: "list[str]" = []
        for group, keys in sorted(groups.items()):
            on_unit = [maps[k] for k in keys]
            named = None if group.startswith("\0") else group
            try:
                ids = unit_board_ids(on_unit, self._unit_dips(show, on_unit),
                                     unit=named)
            except LookError as exc:
                unit_problems[group] = exc.problems
                ids = None
            for key in keys:
                look_map = maps[key]
                items[key]["boards"] = look_map.dip_sheet(ids)
                # The page can renumber boards: which one is which in the CSV.
                was = {new: old for old, new in
                       self._own_boards(show, items[key]["item"]).items()}
                by_hand = self._own_dips(show, items[key]["item"])
                for board in items[key]["boards"]:
                    board["source_no"] = was.get(board["board_no"],
                                                 board["board_no"])
                    # What the page needs to draw the DIP cell: whether this
                    # address was typed in, and whether its switch pattern
                    # is one the operator reported unreliable.
                    board["dip_by_hand"] = board["board_no"] in by_hand
                    board["dip_unreliable"] = unreliable_dip(board["dip_id"])
                    if board["dip_unreliable"]:
                        dip_warnings.append(
                            f"{items[key]['item']} board "
                            f"{board['board_no']}: DIP {board['dip_id']} has "
                            f"{UNRELIABLE_DIP_NOTE}")
                if ids is None:
                    items[key]["problems"] += unit_problems[group]

        for key, look_map in maps.items():
            order = [s.position for s in look_map.scales]
            items[key]["sequences"] = {}
            for name in sequence.SEQUENCES:
                if name != "natural":
                    ranked = sequence.ranks(look_map, name)     # once, not per scale
                    items[key]["sequences"][name] = [ranked[p] for p in order]
        ordered = sorted(items.values(),
                         key=lambda e: (e["unit"] or "~", e["item"].lower()))
        for entry in ordered:
            entry["designs"].sort(key=lambda d: (d["pattern"] is None,
                                                 d["pattern"] or 0, d["name"]))
            entry.setdefault("boards", [])
        facts = {key: {"item": entry["item"], "unit": entry["unit"],
                       "boards": len(entry["boards"]),
                       "designs": {d["name"]: {"full": not d["problems"],
                                               "partial": not d["partial_problems"]}
                                   for d in entry["designs"]}}
                 for key, entry in items.items() if key in maps}
        duration = float(show.get("duration", timeline.DEFAULT_DURATION_S))
        refresh = float(show.get("refresh_s", timeline.REFRESH_S))
        cues = timeline.clean(show.get("cues"))
        timeline.apply_transitions(cues, transitions)
        _time_sweeps(cues, maps)
        cue_problems, warnings = timeline.validate(cues, facts, duration,
                                                   refresh)
        # A warning, never a problem: the garments' switches are already set
        # for the rank numbering, and refusing to build a show two days
        # before it runs would be worse than the flakiness. The operator
        # reads it beside the Upload button and sets another ID by hand if
        # there is time (the operator, 2026-09-27).
        warnings = warnings + dip_warnings
        # A Loop wait set before an edit pulled the floor above it: kept
        # as stored, said here, and the restart waits the floor instead.
        loop_floor, loop_tail, loop_why = loop_floor_of(show)
        stored_wait = loop_wait_of(show)
        if stored_wait is not None and stored_wait < loop_floor:
            warnings = warnings + [
                f"Loop: the wait of {stored_wait:.0f} s is below the "
                f"{loop_floor:.0f} s this timeline needs ({loop_why}) - the "
                f"loop waits {loop_floor:.0f} s"]
        cue_ends = timeline.ends(cues, refresh, duration)
        for cue in cues:
            cue["sent"], cue["complete"] = timeline.times(cue, refresh)
            cue["refresh"] = timeline.effective_refresh(cue, refresh)
            cue["refresh_source"] = ("cue" if isinstance(
                cue.get("refresh_s"), (int, float)) else "show")
            cue["end"], cue["end_source"] = cue_ends[cue["id"]]
            cue["problems"] = cue_problems[cue["id"]]
        unit_boards: "dict[str, int]" = {}
        unit_of_item: "dict[str, str]" = {}
        for key, fact in facts.items():
            name = fact["unit"] or f"({fact['item']})"
            unit_boards[name] = unit_boards.get(name, 0) + fact["boards"]
            unit_of_item[key] = name
        # The floor THIS show needs on each unit, not the default one: a unit
        # carrying a 7 s sweep needs 15 s between sends, and telling the
        # operator "9 s" while validate() rejects a cue 14 s later reads as a
        # contradiction (review, 2026-09-26). A unit with no cues yet keeps
        # the default.
        unit_cues: "dict[str, list[dict]]" = {name: [] for name in unit_boards}
        for cue in cues:
            name = unit_of_item.get(cue["item"].lower())
            if name is not None:
                unit_cues[name].append(cue)
        return {"show": {"duration": duration, "refresh_s": refresh,
                         # The checkbox next to ③ START: delete slots
                         # 1-18 on every unit when the run ends or is
                         # stopped (Workspace.set_clear_after_show).
                         "clear_after_show": bool(
                             show.get("clear_after_show")),
                         # The field next to ③ START: the lead START
                         # gives the fleet, counted down -0:11 ... 0:00
                         # (Workspace.set_start_countdown).
                         "start_countdown_s": start_countdown_of(show),
                         # THE SHOW's Loop: seconds between runs, or null
                         # for off (Workspace.set_loop).
                         "loop_wait_s": loop_wait_of(show),
                         "loop_default_s": LOOP_WAIT_S,
                         # The least wait THIS timeline allows, and why
                         # (loop_floor_of): the field's min and tooltip.
                         "loop_min_wait_s": loop_floor,
                         "loop_tail_s": round(loop_tail, 1),
                         "loop_min_why": loop_why,
                         # The current default, so the page never has a
                         # refresh number of its own: it labels the "show
                         # default" choice with refresh_s and offers the
                         # hint to a show still set below this one.
                         "refresh_default": timeline.REFRESH_S,
                         "panel_repaint_s": timeline.PANEL_REPAINT_S,
                         # The director's gap after a picture completes, so
                         # the page can say what a min_interval is MADE of
                         # instead of deriving it back out of the total
                         # (which only worked while every interval was
                         # refresh + gap - review, 2026-09-26).
                         "gap_s": timeline.GAP_AFTER_REFRESH_S,
                         "cues": cues, "warnings": warnings,
                         "min_interval": {
                             unit: timeline.min_interval_of(
                                 unit_cues[unit], n, refresh)
                             for unit, n in unit_boards.items()}},
                "history": {"undo": len(history["undo"]),
                            "redo": len(history["redo"])},
                "units": UNITS, "items": ordered, "orphans": orphans,
                "sequences": [{"id": name, "label": sequence.LABELS[name]}
                              for name in sequence.SEQUENCES],
                "palette": [{"name": n, "rgb": list(rgb)} for n, rgb in PALETTE],
                "transitions": transitions, "music": self.music_info(),
                "workspace": str(self.root.resolve())}


# ---- the designers' simulator, built on demand ----
#
# The single-file simulator the director's team double-clicks is silent
# unless the show's audio is inside it, and the audio changes. Rather than
# make that a developer's errand (checkout, Python, a command, a 23 MB file
# to hand over), the Conductor builds it here, in-process, from whatever
# music is loaded right now: the Timeline toolbar's "Simulator for
# designers…" is one click, and the answer to "the music changed" is to
# press it again.
_designer_builder = None
_designer_builder_lock = threading.Lock()


def designer_builder():
    """tools/build_designer.py, loaded by path.

    By path, not `import tools.build_designer`: `tools/` is not a package
    and putting the repo root on sys.path to make it one would let any
    other `tools` on the path win instead. This is the same file the CLI
    runs, so the page the operator downloads and the page a developer
    builds cannot drift apart."""
    global _designer_builder
    with _designer_builder_lock:
        if _designer_builder is None:
            spec = importlib.util.spec_from_file_location(
                "az27ss_build_designer", BUILD_DESIGNER_PY)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _designer_builder = module
    return _designer_builder


# Building the real show's page means base64-encoding 17.5 MB and
# assembling a 23 MB string - about two seconds - and an operator who
# clicks the button twice, or whose browser retries the download, should
# not pay it twice. Bounded to the two most recent keys, so a session that
# replaces the music ten times does not keep ten 23 MB pages alive.
_simulator_cache: "dict" = {}
_simulator_cache_lock = threading.Lock()
_SIMULATOR_CACHE_MAX = 2


def _music_key(path: Path, name: str, size: int) -> tuple:
    """What decides the bytes of a with-music page.

    NOT int(st_mtime): replacing the music with a different file of the
    same name and the same size within the same second - which is one drag
    and drop of a re-exported mix, not a contrived case - left the key
    unchanged and served the OLD track, silently, with the new name on it.
    st_mtime_ns is the fix; the digest of the first and last 64 KB is the
    cheap insurance for a filesystem whose nanoseconds are coarse or a
    copy that preserves timestamps. Not the whole file: hashing 17.5 MB on
    every click to save a build that only happens when the key changes is
    the wrong trade, and the ends of an audio file are where a different
    take differs."""
    try:
        stat = path.stat()
        mtime_ns, real_size = stat.st_mtime_ns, stat.st_size
    except OSError:
        mtime_ns, real_size = 0, size
    edges = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            edges.update(handle.read(65536))
            if real_size > 65536:
                handle.seek(max(65536, real_size - 65536))
                edges.update(handle.read(65536))
    except OSError:
        pass
    return (name, real_size, mtime_ns, edges.hexdigest())


def build_simulator(music: "tuple | None") -> bytes:
    """The simulator page as bytes, cached per _music_key().

    `music` is (path, name, type, size) or None for the lean page. The
    lock is held across the build, not just the lookup: two clicks in
    quick succession should queue behind one build, not run two."""
    key = None if music is None else _music_key(music[0], music[1], music[3])
    with _simulator_cache_lock:
        hit = _simulator_cache.get(key)
        if hit is not None:
            return hit
        builder = designer_builder()
        if music is None:
            page = builder.build_page(DESIGNER_SOURCE)
        else:
            path, name, mime, _size = music
            page = builder.build_page(DESIGNER_SOURCE, music=path.read_bytes(),
                                      music_name=name, music_type=mime)
        body = page.encode("utf-8")
        _simulator_cache[key] = body
        while len(_simulator_cache) > _SIMULATOR_CACHE_MAX:
            _simulator_cache.pop(next(iter(_simulator_cache)))
        return body


# ---- sending the workspace to another Conductor ----
#
# The page asks ITS OWN Conductor to send (POST /api/workspace/send {"to":
# "radxa-05:8765"}) and the two Conductors talk server to server: the
# browser never has to reach the other host (no CORS, no second page), the
# fleet token travels with the request, and the page polls the job for
# progress - the tar is packed to a temp file first, so the bytes sent are
# counted against a known total.

# Generous: the receiver compiles every unit's show before it answers
# (seconds for ten units of eighteen cues on a Radxa).
SEND_TIMEOUT_S = 300.0
SEND_PROBE_TIMEOUT_S = 10.0
_SEND_JOBS_KEPT = 8
_HOST_PORT = re.compile(r"^(?:https?://)?\[?([^\[\]/:\s]+|[0-9a-fA-F:]+)\]?(?::([0-9]{1,5}))?/?$")


def parse_conductor_address(raw) -> "tuple[str, int]":
    """"radxa-05:8765" / "10.42.0.105" / "http://radxa-05:8765/" -> (host,
    port); the port defaults to 8765. ValueError for anything else."""
    text = unicodedata.normalize("NFKC", str(raw or "")).strip()
    match = _HOST_PORT.match(text)
    if not match:
        raise ValueError("where to send to: host:port, e.g. radxa-05:8765")
    host, port = match.group(1), int(match.group(2) or 8765)
    if not 1 <= port <= 65535:
        raise ValueError("port: 1 to 65535")
    return host, port


class _Counting:
    """A file object whose reads are counted - the POST's body."""

    def __init__(self, handle, job):
        self._handle, self._job = handle, job

    def read(self, size=-1):
        chunk = self._handle.read(size)
        self._job.sent += len(chunk)
        return chunk


class SendJob:
    """One transfer of this workspace to another Conductor, on its own
    thread: pack, then POST, then the receiver's reply. The page reads
    status() until `done`."""

    def __init__(self, workspace: Workspace, host: str, port: int,
                 token: "str | None", passcode: "str | None" = None):
        self.workspace, self.host, self.port, self.token = workspace, host, port, token
        self.passcode = passcode
        self.id = f"{int(time.time() * 1000) % 10 ** 9:09d}"
        self.state = "packing"          # packing | sending | done | failed
        self.total = 0
        self.sent = 0
        self.reply: "dict | None" = None
        self.error: "str | None" = None
        self.counts: "dict | None" = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> "SendJob":
        self._thread.start()
        return self

    def status(self) -> dict:
        return {"job": self.id, "state": self.state, "to": f"{self.host}:{self.port}",
                "total": self.total, "sent": min(self.sent, self.total),
                "counts": self.counts, "reply": self.reply, "error": self.error}

    def _probe(self) -> None:
        """The target has to BE a Conductor - one that answers /api/fleet
        with a fleet - before a whole workspace is posted at it (a typo
        in the address must not push 20 MB at a unit's agent or a phone)."""
        conn = http.client.HTTPConnection(self.host, self.port,
                                          timeout=SEND_PROBE_TIMEOUT_S)
        try:
            conn.request("GET", "/api/fleet")
            response = conn.getresponse()
            raw = response.read()
        except OSError as exc:
            raise RuntimeError(f"{self.host}:{self.port} does not answer "
                               f"({exc})")
        finally:
            conn.close()
        try:
            answer = json.loads(raw or b"")
        except ValueError:
            answer = None
        if (response.status != 200 or not isinstance(answer, dict)
                or "units" not in answer or "shows" not in answer):
            raise RuntimeError(f"{self.host}:{self.port} is not a Conductor "
                               f"(HTTP {response.status} on /api/fleet)")

    def _run(self) -> None:
        try:
            self._probe()
            with tempfile.TemporaryFile() as pack:
                self.counts = self.workspace.export_tar(pack)
                self.total = pack.tell()
                if self.total > WORKSPACE_TAR_MAX:
                    raise ValueError(f"the workspace is {self.total // (1024 * 1024)} "
                                     f"MB; the receiver takes at most "
                                     f"{WORKSPACE_TAR_MAX // (1024 * 1024)} MB")
                pack.seek(0)
                self.state = "sending"
                headers = {"Content-Type": "application/x-tar",
                           "Content-Length": str(self.total)}
                if self.token:
                    headers["X-Show-Token"] = self.token
                if self.passcode:
                    headers[PASSCODE_HEADER] = self.passcode
                conn = http.client.HTTPConnection(self.host, self.port,
                                                  timeout=SEND_TIMEOUT_S)
                try:
                    conn.request("POST", "/api/workspace/import",
                                 body=_Counting(pack, self), headers=headers)
                    response = conn.getresponse()
                    raw = response.read()
                finally:
                    conn.close()
                try:
                    reply = json.loads(raw or b"{}")
                except ValueError:
                    reply = {"error": f"HTTP {response.status}: not a Conductor "
                                      f"({raw[:80]!r})"}
                if response.status != 200 or not isinstance(reply, dict):
                    error = reply.get("error") if isinstance(reply, dict) else None
                    raise RuntimeError(error or f"HTTP {response.status}")
                self.reply = reply
                self.state = "done"
        except Exception as exc:            # noqa: BLE001 - reported to the page
            self.error = str(exc) or exc.__class__.__name__
            self.state = "failed"


# The passcode (EXHIBITION mode, `serve --passcode` or fleet.json's
# "passcode"): on the hotspot anybody who knows the Wi-Fi password can reach
# the page, so when a passcode is set every request that CHANGES something
# (every POST) and every one that hands out the show's material (the two
# exports, the music, the simulator) needs it from a client that is not this
# host itself - the unit's own LCD row talks to 127.0.0.1 and stays free.
# The page asks for it once (its first 401), keeps it in localStorage and
# sends it as the X-Passcode header; the <audio> element cannot set a header,
# so the same value in a `passcode` cookie is accepted too.
PASSCODE_HEADER = "X-Passcode"
PASSCODE_COOKIE = "passcode"
# The example value in radxa/exhibition/fleet.json: a Conductor open to other
# hosts refuses to start with it, or with no passcode at all (serve()).
PASSCODE_EXAMPLE = "CHANGE-ME-2026"
_PASSCODE_GETS = {"/api/workspace/export", "/api/music/file", "/api/show/export",
                  "/api/simulator"}
_PASSCODE_COOKIE_PATHS = {"/api/music/file"}      # the <audio> element's route
_LOCAL_HOSTS = ("127.0.0.1", "::1", "::ffff:127.0.0.1")


class Handler(BaseHTTPRequestHandler):
    workspace: Workspace = None            # set by make_server()
    fleet: "Fleet | None" = None
    speaker = None                         # conductor/speaker.py, with --speaker
    token: "str | None" = None             # fleet.json's, for the two workspace endpoints
    passcode: "str | None" = None          # see PASSCODE_HEADER
    local_hosts = _LOCAL_HOSTS             # clients the passcode never applies to
    hotspot: str = DEFAULT_HOTSPOT_UNIT    # fleet.json's "hotspot" (wifi_select)
    adopt: bool = False                    # the exhibition's Conductor (serve --adopt)
    label: "str | None" = None             # serve --label: the page's amber badge
    port: "int | None" = None              # the port this Conductor answers on
    other_conductor: "dict | None" = None  # OtherConductorWatch: the other launcher's, when it is up
    prepared: "dict[str, str]" = {}        # unit -> the cue it was last sent
    prepared_lock = threading.Lock()       # request threads share the dict
    send_jobs: "dict[str, SendJob]" = {}   # id -> a workspace transfer
    server_version = "conductor"

    def _client_local(self) -> bool:
        host = str(self.client_address[0]).lower()
        return host in self.local_hosts

    def _identity(self) -> dict:
        """/api/conductor's document - see the GET for what each key is."""
        other = self.other_conductor
        return {"label": self.label, "workspace_name": self.workspace.root.name,
                "workspace": str(self.workspace.root.resolve()), "port": self.port,
                "other_conductor": dict(other) if other else None}

    def _passcode_ok(self) -> bool:
        """The passcode, when this host has one and the client is not this
        host: the header, or the cookie the page sets for its <audio>."""
        code = self.passcode
        if not code or self._client_local():
            return True
        given = self.headers.get(PASSCODE_HEADER) or ""
        # The cookie counts only where a header cannot be set - the music
        # file the <audio> element streams. Everything else, every POST
        # above all, is the page's own fetch() and carries the header.
        if not given and self.path.split("?", 1)[0] in _PASSCODE_COOKIE_PATHS:
            for part in (self.headers.get("Cookie") or "").split(";"):
                name, _, value = part.strip().partition("=")
                if name == PASSCODE_COOKIE:
                    given = urllib.parse.unquote(value)
                    break
        return hmac.compare_digest(given.encode("utf-8", "replace"),
                                   str(code).encode("utf-8", "replace"))

    def _refuse_early(self, payload: dict, status: int,
                      drain_all: bool = False) -> None:
        """An answer given BEFORE the request body was read (401, 409,
        413...). Windows resets the connection under a client whose body
        is still on the wire, and the client then sees a connection
        error instead of the status. So the body is drained first - up to
        EARLY_DRAIN_MAX (a passcode or token refusal is not worth 200 MB
        of reading), or WHOLE with `drain_all` (the 409 to an
        authenticated client whose 5-30 MB Send over the hotspot takes
        longer than any short grace: it must read the 409's text, not a
        reset). A bigger body without `drain_all` is answered with
        `Connection: close` after the headers and still read for
        EARLY_DRAIN_S, which is the most a server can do to get the status
        through before the reset. Every read here has a socket timeout
        (EARLY_IDLE_S per recv): an idle client never holds a thread for
        good, and a timeout is the end of the drain, not a traceback."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if 0 < length <= EARLY_DRAIN_MAX or (drain_all and length > 0):
            try:
                self.connection.settimeout(EARLY_IDLE_S)
                self._drain(length)
            except (OSError, ValueError):
                self.close_connection = True       # the client went quiet
            return self._json(payload, status=status)
        self._json(payload, status=status, close=length > 0)
        if length > 0:
            # The answer is on its way; now keep READING for a bounded
            # while, so a client still sending finishes, reads the status
            # and closes on its own - closing with unread bytes in the
            # socket is what makes Windows send the reset. A client that
            # is still sending after EARLY_DRAIN_S (a 200 MB body on a
            # slow link) loses the status to the reset; nothing more can
            # be done for it from here. read1(), one recv at a time, and
            # the socket timeout shrunk to what is left of the deadline,
            # so the bound is a bound.
            try:
                self.wfile.flush()
                deadline = time.monotonic() + EARLY_DRAIN_S
                remaining = length
                while remaining > 0:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        break
                    self.connection.settimeout(max(0.05, left))
                    chunk = self.rfile.read1(min(WORKSPACE_TAR_CHUNK, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
            except (OSError, ValueError):
                pass

    def _refuse_passcode(self, drain: bool = True) -> None:
        if not drain:
            return self._json({"error": "passcode required"}, status=401)
        return self._refuse_early({"error": "passcode required"}, 401)

    def log_message(self, fmt, *args):     # keep the console for errors
        pass

    def _send(self, status: int, body: bytes, content_type: str,
              close: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if close:
            # Said in the headers, not only done afterwards: the client
            # reads the status before the socket goes (see _refuse_early).
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, status: int = 200, close: bool = False) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"),
                   "application/json; charset=utf-8", close=close)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_UPLOAD:
            raise ValueError("upload too large")
        return json.loads(self.rfile.read(length) or b"{}")

    def _drop_imported_loop(self) -> "str | None":
        """A show that arrived by import (a show file, a workspace tar) may
        carry a Loop - and only the exhibition's Conductor (`--adopt`) may
        take it: on the show PC the Loop is turned off again, said in the
        corrections and in the reply, so an export from radxa-05 can never
        make the PC restart shows on its own. Returns the note, or None."""
        if self.adopt or self.workspace.loop_wait() is None:
            return None
        self.workspace.set_loop(None)
        note = ("the imported show had Loop on - turned off here (only the "
                "exhibition's Conductor keeps it)")
        if self.fleet is not None:
            self.fleet._note(note)
        return note

    def _loop_object(self, pending: "dict | None") -> dict:
        """THE SHOW's Loop as /api/fleet and POST /api/loop report it - one
        shape whether or not a restart is pending, because the unit's own
        LCD (its EXHIBITION row) reads it too:

            {"on": bool,            show.json's loop_wait_s is set
             "wait_s": int,         the wait (the default while off)
             "next_in_s": float|null,  seconds to the next run, when one is pending
             "runs": int,           the Loop's restarts so far this show
             "problem": str|null,   why the pending restart has not gone out
             "min_wait_s": int,     the least wait this timeline allows
             "stored_wait_s": int|null}  the wait as stored (wait_s is the effective one)

        `pending` is Fleet.loop_state() - None when nothing is pending."""
        view = self.workspace.loop_view()
        wait, floor = view["effective"], view["floor"]
        pending = pending or {}
        return {"on": wait is not None,
                # The wait a restart really uses (the floor when an edit
                # pulled it above the stored value); `stored_wait_s` is raw.
                "wait_s": int(round(wait if wait is not None else max(LOOP_WAIT_S, floor))),
                "stored_wait_s": (None if view["stored"] is None
                                  else int(round(view["stored"]))),
                # null while a refused restart is being retried: the LCD
                # counts next_in_s down, and a 5 s retry is not a countdown.
                "next_in_s": pending.get("next_in_s"),
                "retrying": bool(pending.get("retrying")),
                # `waiting` is the LCD's word for the same state (Coder
                # AA's EXHIBITION row reads it): true while retrying.
                "waiting": bool(pending.get("waiting")),
                "retry_in_s": pending.get("retry_in_s"),
                "runs": int(pending.get("runs") or 0),
                "problem": pending.get("problem"),
                "min_wait_s": int(floor)}

    def _set_loop(self, body: dict) -> None:
        """POST /api/loop {"on": true|false, "wait_s": 30} - the wait is
        optional with on=true (the show's own, or 30 s, is kept), ignored
        with on=false. Answers the same `loop` object /api/fleet carries."""
        on = body.get("on")
        if not isinstance(on, bool):
            raise ValueError("on must be true or false")
        if not on:
            self.workspace.set_loop(None)
        else:
            wait = body.get("wait_s")
            if wait is None:
                # Turned on with no number: the stored wait (or the 45 s
                # default), lifted to the floor this timeline has - never
                # a 400 for a plain "on".
                view = self.workspace.loop_view()
                wait = max(view["stored"] if view["stored"] is not None else LOOP_WAIT_S,
                           view["floor"])
            self.workspace.set_loop(wait)
        pending = self.fleet.loop_state() if self.fleet is not None else None
        answer = self._loop_object(pending)
        if on and self.fleet is not None and (any(
                bool(unit_show.get("clear_after_show"))
                for unit_show in self.fleet.shows.values()) or any(
                bool((((link.status or {}).get("show") or {}).get("clear_after_show")))
                for link in self.fleet.links.values() if link.online)):
            # The units hold a copy that clears ITSELF at its end (uploaded
            # before the Loop was on): the restart would meet "cleared".
            # Said here and in the corrections; an Upload writes the copy
            # that leaves the clear to the conductor (same id, no pictures
            # rewritten).
            answer["note"] = ("the units hold a show that clears its own pictures "
                              "at its end - Upload again before START, or the "
                              "Loop stops after run 1")
            self.fleet._note("Loop on: " + answer["note"])
        return self._json(answer)

    def _own_units(self) -> "list[str]":
        """The units that are THIS host - reached at a loopback address, or
        at one of this host's own - which a fleet-wide Wi-Fi switch must
        tell last (Fleet.wifi_select)."""
        fleet = self.fleet
        if fleet is None:
            return []
        mine = {"127.0.0.1", "localhost", "::1"}
        mine.update(local_ipv4s())          # no DNS on a request thread
        own = []
        for name, link in fleet.links.items():
            host = getattr(link, "address", "").rsplit(":", 1)[0].strip("[]")
            if host in mine:
                own.append(name)
        return own

    def _token_ok(self) -> bool:
        """The fleet token (fleet.json's), when this host has one, gates
        the two workspace endpoints: a Conductor that may drive these
        units may also hand them a workspace. Compared in constant time."""
        token = self.token
        if not token:
            return True
        given = self.headers.get("X-Show-Token") or ""
        # Bytes, not str: compare_digest() on str insists on ASCII, and a
        # token is whatever somebody typed into fleet.json.
        return hmac.compare_digest(given.encode("utf-8", "replace"),
                                   str(token).encode("utf-8", "replace"))

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in _PASSCODE_GETS and not self._passcode_ok():
            return self._refuse_passcode(drain=False)
        if path == "/api/music/file":
            return self._music_file(head=False)
        if path == "/api/show/export":
            return self._export_show()
        if path == "/api/workspace/export":
            return self._export_workspace()
        try:
            if path == "/api/workspace/send":
                query = urllib.parse.parse_qs(self.path.partition("?")[2])
                job = self.send_jobs.get(query.get("job", [""])[0])
                if job is None:
                    return self._json({"error": "no such transfer"}, status=404)
                return self._json(job.status())
            if path == "/api/simulator":
                # Inside the try, so anything unexpected on the way to the
                # build (a show.json that will not parse, say) comes back
                # as the JSON error the page knows how to show, not as a
                # traceback and a dead socket.
                query = urllib.parse.parse_qs(self.path.partition("?")[2])
                return self._simulator(query.get("music", ["0"])[0] == "1")
            if path == "/api/state":
                # The identity keys (see /api/conductor) ride on the state
                # too, so the page's first paint has them.
                return self._json(dict(self.workspace.state(), **self._identity()))
            if path == "/api/conductor":
                # Which Conductor this is, and whether the OTHER launcher's
                # is up. `label` is this Conductor's name when it is a
                # SEPARATE one (serve --label EXHIBITION): the page wears
                # it as an amber badge and puts it first in the window
                # title; null on the show PC's own Conductor, whose page
                # is unchanged. `workspace_name` is the folder's own name
                # (showdata / exhibition-data) next to `workspace`, the
                # full path; `port` is this Conductor's. `other_conductor` is the other launcher's
                # Conductor when it answers ({"port", "label",
                # "workspace_name"}, OtherConductorWatch), else null - the
                # page shows it in red in the top bar, because an OPEN
                # idle Conductor still acts on the units (its supervision
                # sends STOP to a unit it did not start, fires its armed
                # clears): the other window has to be CLOSED before
                # Upload / START here, not merely left alone. Cheap: the
                # page polls it every few seconds on every tab.
                return self._json(self._identity())
            if path == "/api/fleet":
                # `timeline` is about the workspace, not the units: what
                # the timeline is now, and what it was when it was last
                # written to them (Workspace.written_state) - the page's
                # "up to date" / "changed since" needs both, and an id
                # comparison alone cannot see an edit made since.
                # `speaker` is the Conductor host's own music player
                # (serve --speaker): null when there is none, otherwise
                # its state - the page then mutes its own player by
                # default and says where the sound comes from. `loop` is
                # THE SHOW's Loop, always present (see _loop_object).
                speaker = None if self.speaker is None else self.speaker.status()
                if self.fleet is None:
                    return self._json({"units": [], "last_fire": None,
                                       "run": None, "shows": {},
                                       "corrections": [], "prepared": {},
                                       "start_at": 0.0, "show_duration": None,
                                       "loop": self._loop_object(None),
                                       "speaker": speaker,
                                       "timeline": self.workspace.written_state()})
                with self.prepared_lock:
                    prepared = dict(self.prepared)
                snap = self.fleet.snapshot()
                return self._json(dict(snap, prepared=prepared, speaker=speaker,
                                       loop=self._loop_object(snap.get("loop")),
                                       timeline=self.workspace.written_state()))
            if path == "/api/fleet/demos":
                # list_demos() skips an unreachable unit rather than wait
                # out its timeout (its result carries the exact "offline"
                # text); one that answered but refused (a 404 from an
                # older agent with no /demo/list, say) is a different
                # thing and goes in "failed" instead, with its own
                # message - the page tells the two apart in what it says.
                if self.fleet is None:
                    return self._json({"units": {}, "offline": [], "failed": {}})
                results = self.fleet.list_demos()
                # Each demo carries the id of the per-unit show it was
                # written with (showfile.build_unit_show's own hash).
                # "current" compares it against what was actually
                # UPLOADED (fleet.shows - free, already in memory) and
                # only falls back to compiling the timeline right now
                # when nothing was uploaded this session at all. Neither
                # a missing reference id nor a demo entry with no
                # show_id of its own (an older unit) is "older" - it is
                # simply not known, so the page says "—", never "older".
                reference = dict(self.fleet.shows)
                if not reference:
                    reference, _ = self.workspace.compile_show()
                def annotate(unit, demos):
                    current_id = (reference.get(unit) or {}).get("id")
                    out = []
                    for demo in demos:
                        show_id = demo.get("show_id")
                        current = (None if current_id is None or show_id is None
                                  else show_id == current_id)
                        out.append(dict(demo, current=current))
                    return out
                units = {name: annotate(name, r["demos"])
                        for name, r in results.items() if r["ok"]}
                offline = sorted(name for name, r in results.items()
                                 if not r["ok"] and r["error"] == "offline")
                failed = {name: r["error"] for name, r in results.items()
                         if not r["ok"] and r["error"] != "offline"}
                return self._json({"units": units, "offline": offline,
                                   "failed": failed})
        except Exception as exc:        # noqa: BLE001 - a poll must get JSON
            return self._json({"error": f"{exc.__class__.__name__}: {exc}"},
                              status=500)
        if path in ("/", "/index.html"):
            page = (WEB_DIR / "index.html").read_bytes()
            return self._send(200, page, "text/html; charset=utf-8")
        self._send(404, b"not found", "text/plain")

    def do_HEAD(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/music/file":
            if not self._passcode_ok():
                self.send_response(401)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            return self._music_file(head=True)
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _export_show(self) -> None:
        payload = self.workspace.export_show()
        body = json.dumps(payload, indent=1).encode("utf-8")
        stamp = time.strftime("%Y%m%d-%H%M")
        filename = f"{payload['workspace']}-show-{stamp}.json"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Disposition",
                         f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _export_workspace(self) -> None:
        """GET /api/workspace/export - the workspace as one .tar (see
        Workspace.export_tar). Packed to a temp file first so the reply
        carries its length; behind the fleet token when this host has one."""
        if not self._token_ok():
            return self._json({"error": "fleet token required"}, status=401)
        stamp = time.strftime("%Y%m%d-%H%M")
        filename = f"{self.workspace.root.name}-workspace-{stamp}.tar"
        try:
            with tempfile.TemporaryFile() as pack:
                self.workspace.export_tar(pack)
                size = pack.tell()
                pack.seek(0)
                self.send_response(200)
                self.send_header("Content-Type", "application/x-tar")
                self.send_header("Content-Disposition",
                                 f'attachment; filename="{filename}"')
                self.send_header("Content-Length", str(size))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    shutil.copyfileobj(pack, self.wfile, WORKSPACE_TAR_CHUNK)
                except (BrokenPipeError, ConnectionError, OSError):
                    pass          # a cancelled download is not an error
        except OSError as exc:
            return self._json({"error": f"{exc.__class__.__name__}: {exc}"},
                              status=500)

    def _drain(self, length: int) -> None:
        """Read and drop a request body before answering: answering with
        the body unread makes Windows reset the connection under the
        client, which then sees ConnectionAborted instead of the answer
        (see _upload_music)."""
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(WORKSPACE_TAR_CHUNK, remaining))
            if not chunk:
                break
            remaining -= len(chunk)

    def _import_workspace(self) -> None:
        """POST /api/workspace/import - the body is the .tar. 401 without
        the fleet token (when this host has one), 409 while a run is
        active (a Loop between runs included: the run is still there),
        413 over WORKSPACE_TAR_MAX - and none of them reads more than it
        has to. The swap itself is Workspace.import_tar; the reply is what
        landed plus the show as it now compiles (per-unit ids), which is
        also what makes the new workspace's first compile happen here and
        not on the next poll."""
        if not self._token_ok():
            return self._refuse_early({"error": "fleet token required"}, 401)
        try:
            length = int(self.headers.get("Content-Length") or -1)
        except ValueError:
            length = -1
        if length < 0:
            return self._json({"error": "Content-Length required"}, status=400)
        if length > WORKSPACE_TAR_MAX:
            return self._refuse_early({"error": f"the workspace is at most "
                                       f"{WORKSPACE_TAR_MAX // (1024 * 1024)} MB"},
                                      413)
        if self.fleet is not None and self.fleet.run is not None:
            # Drained WHOLE: this client passed the token and is sending a
            # real workspace; it must read "STOP it first", not a reset.
            return self._refuse_early({"error": "a run is active on this Conductor "
                                                "- STOP it first"}, 409,
                                      drain_all=True)
        # To a temp file beside the workspace (the same disk the swap
        # renames on), streamed in chunks: never the whole tar in memory.
        # The spool is named BEFORE the body is read and removed in the one
        # finally below, so a truncated or stalled upload leaves no
        # .import-*.tar behind.
        try:
            spool = tempfile.NamedTemporaryFile(dir=str(self.workspace.root),
                                                prefix=".import-", suffix=".tar",
                                                delete=False)
        except OSError as exc:
            return self._json({"error": str(exc)}, status=500)
        spool_path = Path(spool.name)
        try:
            try:
                with spool:
                    remaining = length
                    while remaining > 0:
                        chunk = self.rfile.read(min(WORKSPACE_TAR_CHUNK, remaining))
                        if not chunk:
                            raise ValueError("the upload ended early")
                        spool.write(chunk)
                        remaining -= len(chunk)
            except (OSError, ValueError) as exc:
                return self._json({"error": str(exc)}, status=400)
            # Checked again with the tar in hand: a START may have landed
            # while the body was on its way.
            if self.fleet is not None and self.fleet.run is not None:
                return self._json({"error": "a run is active on this Conductor "
                                            "- STOP it first"}, status=409)
            try:
                counts = self.workspace.import_tar(spool_path)
            except (tarfile.TarError, ValueError, OSError) as exc:
                return self._json({"error": f"not a workspace tar: {exc}"},
                                  status=400)
        finally:
            spool_path.unlink(missing_ok=True)
        if self.fleet is not None:
            # What the units hold is the OLD show now. The fleet forgets it
            # (and the startup offer), and the units it knew are marked as
            # holding an upload older than this timeline, so a START right
            # after the import is refused with "Upload again" rather than
            # running yesterday's pictures under today's music.
            held = sorted(self.fleet.shows)
            self.fleet.forget_shows()
            if held:
                self.workspace.unit_marks["upload"] = {u: "before-import" for u in held}
        dropped = self._drop_imported_loop()
        shows, problems = self.workspace.compile_show()
        with self.prepared_lock:
            self.prepared.clear()           # manual cues of the old workspace
        return self._json({"ok": True, **counts,
                           "cues": len(self.workspace.state()["show"]["cues"]),
                           "revision": self.workspace.revision(),
                           "shows": {unit: show["id"] for unit, show in shows.items()},
                           "problems": problems + ([dropped] if dropped else [])})

    def _send_workspace(self, body: dict) -> None:
        """POST /api/workspace/send {"to": "radxa-05:8765"} -> {"job": id};
        the page then polls GET /api/workspace/send?job=id (SendJob)."""
        host, port = parse_conductor_address(body.get("to"))
        job = SendJob(self.workspace, host, port, self.token,
                      self.passcode).start()
        jobs = self.send_jobs
        jobs[job.id] = job
        for old in list(jobs)[:-_SEND_JOBS_KEPT]:
            jobs.pop(old, None)
        return self._json(dict(job.status(), ok=True))

    def _simulator(self, with_music: bool) -> None:
        """GET /api/simulator[?music=1] - the designers' single-file
        simulator, as a download.

        With music=1 and music loaded, the show's audio is embedded and the
        file is named ...-with-music.html so nobody has to guess which copy
        on their desktop is the one that plays. With no music loaded the
        lean page is sent instead (the page says so in a toast) - refusing
        would be worse: the simulator is still useful silent, and the
        operator may simply not have uploaded the track yet."""
        music = None
        if with_music:
            info = self.workspace.music_info()
            if info is not None:
                path = self.workspace.music / Path(info["name"]).name
                music = (path, info["name"], info["type"], info["size"])
        stamp = time.strftime("%Y%m%d")
        suffix = "-with-music" if music is not None else ""
        filename = f"az27ss-simulator-{stamp}{suffix}.html"
        try:
            body = build_simulator(music)
        except Exception as exc:        # noqa: BLE001 - a download must say why
            # The cause only, in the same shape as every other endpoint's
            # error: the page supplies the "Could not build the
            # simulator:" lead-in, and repeating it here read as
            # "Could not build the simulator: could not build the
            # simulator: OSError: ...".
            return self._json({"error": f"{exc.__class__.__name__}: {exc}"},
                              status=500)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Disposition",
                         f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionError, OSError):
            pass          # a cancelled 23 MB download is not an error

    def _music_file(self, head: bool) -> None:
        info = self.workspace.music_info()
        if info is None:
            body = b"" if head else json.dumps(
                {"error": "no music uploaded"}).encode("utf-8")
            self.send_response(404)
            if not head:
                self.send_header("Content-Type",
                                 "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if not head:
                self.wfile.write(body)
            return
        path = self.workspace.music / Path(info["name"]).name
        try:
            mtime = int(path.stat().st_mtime)
        except OSError:
            mtime = 0
        size, content_type = info["size"], info["type"]
        etag = f'"{size}-{mtime}"'
        # The URL is already versioned (?v=<mtime>), so a long-lived,
        # private cache is safe: a new upload is a new URL. ETag/304
        # saves the bytes again on a reload of the *same* version.
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control",
                             "private, max-age=31536000, immutable")
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            return
        status, start, end = 200, 0, max(0, size - 1)
        range_header = self.headers.get("Range")
        if range_header:
            parsed = _parse_range(range_header, size)
            if parsed == "unsatisfiable":
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if parsed is not None:
                start, end = parsed
                status = 206
        length = max(0, end - start + 1)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "private, max-age=31536000, immutable")
        self.send_header("ETag", etag)
        self.send_header("X-Content-Type-Options", "nosniff")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if head:
            return
        try:
            with open(path, "rb") as handle:
                handle.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = handle.read(min(MUSIC_CHUNK, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionError, OSError):
            pass          # the listener went away mid-stream; nothing to do

    def _upload_music(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or -1)
        except ValueError:
            length = -1
        if length < 0:
            return self._json({"error": "Content-Length required"}, status=400)
        # An empty file is not music. It used to be accepted, which put a
        # name into show.json with no audio under it - and once that is
        # embedded in the designers' simulator, the music line says the
        # track is built in and Play does nothing.
        if length == 0:
            return self._json({"error": "that music file is empty (0 bytes) - "
                                        "nothing was uploaded"}, status=400)
        if length > MAX_MUSIC:
            # Refused before the body is wanted: `Connection: close` (the
            # body is far past EARLY_DRAIN_MAX) - see _refuse_early.
            return self._refuse_early({"error": f"music is at most "
                                       f"{MAX_MUSIC // (1024 * 1024)} MB"}, 400)
        # The page sends encodeURIComponent(name): decoded here so a
        # Japanese or accented file name survives, not just ASCII ones.
        name = urllib.parse.unquote(self.headers.get("X-File-Name") or "music")
        if Path(name).suffix.lower() not in _MUSIC_TYPES:
            allowed = ", ".join(sorted(_MUSIC_TYPES))
            # Answering before the request body is read makes Windows
            # reset the connection under the client, which then sees
            # ConnectionAborted instead of this 400 (a flaky test found
            # it): the one rule for every early answer, _refuse_early.
            return self._refuse_early({"error": f"music must be one of {allowed}"},
                                      400)
        try:
            self.workspace.save_music(name, self.rfile, length)
        except (OSError, ValueError) as exc:
            return self._json({"error": str(exc)}, status=400)
        return self._json({"ok": True, "music": self.workspace.music_info()})

    def do_POST(self):
        # Every POST changes something: all of them are behind the passcode
        # for a client that is not this host (see PASSCODE_HEADER).
        if not self._passcode_ok():
            return self._refuse_passcode()
        if self.path == "/api/music":
            return self._upload_music()
        if self.path == "/api/workspace/import":
            return self._import_workspace()      # a tar, not JSON
        try:
            body = self._body()
            if self.path == "/api/workspace/send":
                return self._send_workspace(body)
            if self.path == "/api/loop":
                return self._set_loop(body)
            if self.path == "/api/speaker/volume":
                # The Conductor host's own loudness (conductor/speaker.py):
                # {"volume": 0-100} or {"delta": +-n} on the stored value.
                if self.speaker is None:
                    return self._json({"error": "no speaker on this Conductor "
                                                "(serve --speaker)"}, status=400)
                if "delta" in body:
                    if not _is_number(body["delta"]) or not math.isfinite(body["delta"]):
                        raise ValueError("delta: a number of percent")
                    wanted = self.speaker.status()["volume"] + float(body["delta"])
                else:
                    wanted = body.get("volume")
                    if (not _is_number(wanted) or not math.isfinite(wanted)
                            or not 0 <= float(wanted) <= 100):
                        raise ValueError("volume: 0 to 100")
                try:
                    return self._json(self.speaker.set_volume(wanted))
                except RuntimeError as exc:         # fleet.json unreadable: kept
                    return self._json({"error": str(exc)}, status=500)
            if self.path == "/api/files":
                # A malformed body is the caller's mistake, not a 500:
                # `files` as a list of bare strings used to reach
                # intake() and come back as an AttributeError traceback
                # (review of dbed7d5). intake() itself refuses an entry
                # that is not {name, text}; this catches the shape above
                # it, where there is nothing to name in a refusal.
                files = body.get("files") or []
                if not isinstance(files, list):
                    return self._json(
                        {"error": "files: a list of {name, text}"},
                        status=400)
                # `item` set: a garment's own "Add CSV" - see intake().
                item = body.get("item")
                if item is not None and not isinstance(item, str):
                    return self._json({"error": "item: a garment's name"},
                                      status=400)
                return self._json(self.workspace.intake(files, item or None))
            if self.path == "/api/duplicate":
                return self._json({"ok": True, "item":
                                   self.workspace.duplicate(body["item"])})
            if self.path == "/api/boards":
                # One endpoint for the one table: `boards` is which board the
                # garment carries, `dips` is the DIP ID that board's switches
                # really have. The page sends whichever cell was typed in,
                # and a call that names both is two steps of the history.
                if "boards" in body:
                    self.workspace.set_boards(body["item"], body["boards"])
                if "dips" in body:
                    self.workspace.set_dips(body["item"], body["dips"])
                if "boards" not in body and "dips" not in body:
                    return self._json({"error": "boards or dips"}, status=400)
                return self._json({"ok": True})
            if self.path == "/api/arrange":
                self.workspace.arrange(body["units"])
                return self._json({"ok": True})
            if self.path == "/api/label":
                self.workspace.set_label(body["item"], body.get("look"),
                                         body.get("model"))
                return self._json({"ok": True})
            if self.path == "/api/assign":
                self.workspace.assign(body["item"], body.get("unit") or None)
                return self._json({"ok": True})
            if self.path == "/api/show":
                self.workspace.set_timeline(body.get("duration", timeline.DEFAULT_DURATION_S),
                                            body.get("cues", []),
                                            body.get("refresh_s"))
                return self._json({"ok": True})
            if self.path == "/api/show/clear_after":
                self.workspace.set_clear_after_show(body.get("on"))
                return self._json({"ok": True})
            if self.path == "/api/show/start_countdown":
                self.workspace.set_start_countdown(body.get("s"))
                return self._json({"ok": True})
            if self.path == "/api/show/import":
                cues, warnings = self.workspace.import_show(body)
                dropped = self._drop_imported_loop()
                return self._json({"ok": True, "cues": cues,
                                   "warnings": warnings + ([dropped] if dropped else [])})
            if self.path == "/api/bundle/import":
                return self._json(self.workspace.import_bundle(body))
            if self.path == "/api/transition":
                self.workspace.set_transition(body.get("design", ""),
                                              body.get("sequence", "natural"),
                                              body.get("span_s", 0))
                return self._json({"ok": True})
            if self.path == "/api/music/remove":
                self.workspace.remove_music()
                return self._json({"ok": True})
            if self.path.startswith("/api/fleet/"):
                return self._fleet_command(self.path[len("/api/fleet/"):], body)
            if (self.path.startswith("/api/units/")
                    and self.path.endswith("/bus/recover")):
                return self._recover_bus(urllib.parse.unquote(
                    self.path[len("/api/units/"):-len("/bus/recover")]))
            if self.path in ("/api/undo", "/api/redo"):
                step = (self.workspace.undo if self.path == "/api/undo"
                        else self.workspace.redo)
                return self._json({"ok": step()})
            if self.path == "/api/delete":
                self.workspace.delete(body["name"])
                return self._json({"ok": True})
        except (ValueError, KeyError, TypeError, OverflowError) as exc:
            # TypeError: hostile JSON ({"at": null}, {"at": {}}, ...) that
            # reaches a str/float/dict call before it reaches a ValueError
            # of its own. OverflowError: float() on a JSON integer with a
            # few hundred digits (found in review) - either way, a 400,
            # never a 500 that drops the connection.
            return self._json({"error": str(exc)}, status=400)
        self._send(404, b"not found", "text/plain")


    def _one_timeline(self, fleet) -> None:
        """Refuse a START / PRESET that would run the fleet on two
        different timelines, on one it has moved past, or on one that does
        not build at all.

        This is the other half of "Which LOOKs": the dialog says "START
        needs every unit of the timeline to hold this upload", and this
        is where that sentence is true. Nothing downstream can catch it -
        a unit's show id is the id THIS conductor gave it, so
        `_burn()`'s id check matches happily for a unit still holding
        last hour's show, and show_duration() just takes the longest of
        the two (review F1/F2).

        Only the per-unit marks of THIS conductor are evidence: a
        conductor restarted mid-show knows nothing about who holds what
        and must not refuse on a guess.

        Its override is `split_ok`, its own field and nobody else's: the
        burn gate's `force` used to wave this one through too, so a page
        that had already asked "some boards did not take it - start
        anyway?" started a fleet running two timelines without ever
        asking about THAT (N1). Two gates, two questions, two answers.

        It lives here rather than in Fleet because only the workspace
        knows what revision the timeline on screen is."""
        marks = self.workspace.unit_marks.get("upload", {})
        if not marks:
            return
        rev = self.workspace.revision()
        # A timeline that does not build is refused BEFORE the units are
        # counted, because the count can come out clean while the show is
        # still full of holes: a garment with cues and no unit at all is
        # in no unit's marks and in `timeline_units()` either, so a
        # one-LOOK upload of the garments that DO have units leaves
        # nothing missing and nothing behind (found in review,
        # 2026-09-27 - the probe only tripped over the burn gate, by
        # luck). Read off the last compile, and only while it is still a
        # compile of what is on screen; `problems` there is everything
        # the timeline has, warnings a one-LOOK write waved through
        # included (compile_for_write). "Upload again" is no use when an
        # Upload could not happen (N2), so this says what to do instead.
        compiled = self.workspace.compiled
        if (compiled and compiled["revision"] == rev
                and (compiled["problems"] or not compiled["units"])):
            raise ValueError("the timeline has problems - fix them on the "
                             "Timeline tab, then Upload")
        # Both halves of "every unit of the timeline holds this upload":
        # the units that hold an OLDER one, and the units this timeline
        # needs that were never written at all - a one-LOOK upload from
        # a fresh conductor leaves the others in the second group, where
        # fleet.shows does not even mention them.
        wanted = self.workspace.timeline_units()
        missing = sorted(unit for unit in wanted if unit not in marks)
        known = {unit: mark for unit, mark in marks.items()
                 if unit in fleet.shows or unit in wanted}
        behind = sorted(unit for unit, mark in known.items() if mark != rev)
        if not missing and not behind:
            return
        if not missing and len(set(known.values())) == 1:
            # They agree with each other, and all disagree with the
            # timeline on screen: the ordinary "edited and forgot to
            # upload". The revision ignores labels and music, so this is
            # always a real change to the cues, the CSVs or the units.
            raise ValueError(
                "every unit holds an older upload than the timeline on "
                "screen - Upload again before the show")
        named = ", ".join(missing + behind)
        raise ValueError(
            f"{named} {'is' if len(missing) + len(behind) == 1 else 'are'} "
            "not on this upload - Upload for All LOOKs before the show")

    def _all_units(self, shows: "dict[str, dict]") -> "list[str]":
        """Every unit this timeline needs - what mark_written() measures a
        fleet-wide "the units hold this" against.

        The compiled shows AND what show.json says the timeline reaches:
        a one-LOOK write may compile past a problem on another unit
        (showfile.build's warnings), and that unit is then missing from
        `shows` while very much still being in the show. Counting only the
        compiled ones would let one written LOOK claim the whole
        timeline - the one thing the chip exists to answer."""
        return sorted(set(shows) | self.workspace.timeline_units())

    def _recover_bus(self, name: str) -> None:
        """POST /api/units/<name>/bus/recover - straight through to that
        unit's own endpoint (ui/agent.py).

        One unit, and no gate of its own: the UNIT decides whether this
        is safe (it refuses while a show is running or holding, or a cue
        is armed within the minute), and it is the only thing that can -
        this conductor may not even be the one driving it. Everything
        that can go wrong is an answer, never a 500: an unknown unit, a
        refusal, a unit that has gone off the WLAN, an agent too old for
        the endpoint (404). The page shows whatever comes back.
        """
        fleet = self.fleet
        if fleet is None:
            return self._json({"error": "no fleet configured"}, status=400)
        try:
            return self._json(fleet.recover_bus(name))
        except KeyError as exc:
            return self._json({"error": str(exc).strip('"')}, status=404)
        except Exception as exc:        # noqa: BLE001 - reported, never raised
            return self._json({"error": str(exc) or exc.__class__.__name__},
                              status=502)

    def _fleet_command(self, command: str, body: dict) -> None:
        fleet = self.fleet
        if fleet is None:
            return self._json({"error": "no fleet configured"}, status=400)
        if command == "prepare":
            cue = f"m{int(time.time()) % 1000000:06d}"
            payloads, problems = self.workspace.compile_units(
                body.get("choices") or {}, cue)
            results = fleet.prepare(payloads) if payloads else {}
            with self.prepared_lock:
                for unit, result in results.items():
                    if result["ok"]:
                        self.prepared[unit] = cue
            return self._json({"cue": cue, "units": results,
                               "problems": problems})
        if command == "fire":
            with self.prepared_lock:
                units = body.get("units") or list(self.prepared)
                cues = {u: self.prepared[u] for u in units
                        if u in self.prepared}
            lead = float(body.get("lead_s", DEFAULT_LEAD_S))
            if not 0.5 <= lead <= 60:
                raise ValueError("lead time is 0.5-60 s")
            return self._json({"units": fleet.fire(cues, lead), "lead_s": lead})
        if command == "upload":
            # Every picture is written at Upload time now: doing that while
            # a show is running would rewrite slots a unit may be reading
            # from for its next trigger (found in review).
            if fleet.run is not None and not body.get("force"):
                raise ValueError("stop the show first")
            # Taken BEFORE the show is compiled: that and the writing
            # take seconds, and an edit landing in between belongs to the
            # next upload, not to this one.
            rev = self.workspace.revision()
            only = _only_units(body.get("units"))
            shows, problems, warnings = self.workspace.compile_for_write(only)
            _check_only_units(only, shows)
            results = (fleet.upload(shows, force=bool(body.get("force")),
                                    only=only)
                       if shows else {})
            # What went out is remembered, PER UNIT, so an edit made
            # after this reads as "changed since" however long the page
            # has been open and whatever it was reloaded to - and so a
            # one-LOOK upload cannot claim the whole timeline is on the
            # units (mark_written works out the fleet-wide mark itself).
            written = sorted(u for u, r in results.items() if r["ok"])
            if written:
                self.workspace.mark_written("upload", rev, units=written,
                                            all_units=self._all_units(shows),
                                            whole=not warnings)
            return self._json({"units": results, "problems": problems,
                               "warnings": warnings,
                               "shows": {u: s["id"] for u, s in shows.items()}})
        if command == "write_demo":
            name = _demo_name(body.get("name"))
            loop = body.get("loop", False)
            if not isinstance(loop, bool):
                raise ValueError("loop must be true or false")
            # Saving a demo writes every picture on every unit, exactly as
            # Upload does - during a run that would rewrite slots a unit
            # is about to trigger. The unit itself refuses /demo/save
            # while it plays anything ("a show is running - stop it
            # first"), and the page's dialog says so before it offers the
            # choice; this is the same rule where every client meets it.
            # Unlike Upload there is no `force`: a demo is never the way
            # back into a running show.
            if fleet.run is not None:
                raise ValueError("stop the show first")
            # The same "whole show or not at all" rule as Upload, and the
            # same reach: a timeline with a problem writes nothing, and a
            # one-LOOK Save answers for its own units only - the page
            # shows exactly the problems Upload itself would have refused
            # on, and the same warnings about what was left out.
            rev = self.workspace.revision()      # see the upload above
            only = _only_units(body.get("units"))
            shows, problems, warnings = self.workspace.compile_for_write(only)
            _check_only_units(only, shows)
            results = (fleet.write_demo(name, loop, shows, only=only)
                       if shows else {})
            written = sorted(u for u, r in results.items() if r["ok"])
            if written:
                self.workspace.mark_written(f"demo:{name}", rev, units=written,
                                            all_units=self._all_units(shows),
                                            whole=not warnings)
            return self._json({"units": results, "problems": problems,
                               "warnings": warnings, "name": name})
        if command == "clear_pictures":
            # "Clear pictures now" (the WRITE TO UNITS dialog): delete
            # slots 1-18 on the units of this timeline. Never during a
            # run - those slots are what the next trigger reads from, and
            # unlike Upload there is no `force`: a clear is never a way
            # back into a running show. The units refuse it too.
            if fleet.run is not None:
                raise ValueError("stop the show first")
            # No compile: nothing is built or written here, and the units
            # this can touch are the ones the fleet already believes hold
            # this conductor's show. A one-LOOK choice is checked against
            # those, the same way and with the same message.
            only = _only_units(body.get("units"), fleet.shows)
            results = fleet.clear_pictures(only=only)
            return self._json({"units": results})
        if command == "delete_demo":
            slug = _demo_slug(body.get("slug"))
            results = fleet.delete_demo(slug)
            if any(r["ok"] for r in results.values()):
                self.workspace.forget_demo(slug)
            return self._json({"units": results})
        if command == "preset":
            # The same `force` as START's: waves through a unit that
            # failed to burn some boards (the page asks first), never
            # one still burning or with nothing written (fleet.py) - and
            # The split fleet - which the preset would otherwise show as
            # two different 0:00 looks side by side - is a gate of its
            # own, with its own answer (`split_ok`).
            if not body.get("split_ok"):
                self._one_timeline(fleet)
            return self._json({"units": fleet.preset(
                force=bool(body.get("force")))})
        if command == "seek":
            if body.get("manual") is not True:
                raise ValueError('Manual control is off. Tick "Manual '
                                 'control" to move the show position.')
            to_s = body.get("to_s")
            if not isinstance(to_s, (int, float)) or isinstance(to_s, bool):
                raise ValueError("Where to move to must be a number of "
                                 "seconds.")
            lead = float(body.get("lead_s", DEFAULT_LEAD_S))
            if not 0.5 <= lead <= 60:
                raise ValueError("lead time is 0.5-60 s")
            if not fleet.shows:
                if fleet.run is not None:
                    # Adopted from the units after a restart with
                    # nothing of its own uploaded: there is no
                    # show_duration to check `to_s` against here.
                    return self._json({"units": {}, "mode": "none", "note":
                                       "This conductor did not upload the "
                                       "show - Upload first."})
                return self._json({"units": {}, "note":
                                   "Nothing uploaded yet - Upload first."})
            mode, results = fleet.seek(float(to_s), lead)
            snap = fleet.snapshot()
            to_s = round(float(to_s), 1)
            if mode == "start_at":
                note = f"START will begin at {timeline.format_clock(to_s)}."
            elif mode == "holding":
                note = (f"On hold at {timeline.format_clock(to_s)}. "
                        "RESUME continues from here.")
            else:
                note = ""
            return self._json({"units": results, "run": snap["run"],
                               "mode": mode, "to_s": to_s,
                               "start_at": snap["start_at"], "note": note})
        if command in ("start", "next"):
            # The page always says which lead it means (START from 0:00: the
            # show's "Countdown before START"; START from a mark and NEXT: the
            # "take effect in" field). A START that does not say gets the
            # same thing - decided below, once `at` is known.
            lead = body.get("lead_s")
            if lead is not None or command == "next":
                lead = float(DEFAULT_LEAD_S if lead is None else lead)
                if not 0.5 <= lead <= 60:
                    raise ValueError("lead time is 0.5-60 s")
            if command == "start":
                if not fleet.shows:
                    return self._json({"units": {}, "note":
                                       "Nothing uploaded yet - Upload first."})
                # A second click on START must not move a running show's
                # clock; starting over is said out loud (the page asks).
                # A run that has reached its end (ENDED, a Loop wait) is
                # not running: START then is the next run, no `force`
                # asked for and none implied.
                if (fleet.run is not None and not body.get("force")
                        and not fleet.run_is_over()):
                    return self._json({"units": {}, "note":
                                       "The show is already running."})
                # Every unit of the timeline has to hold the SAME upload,
                # which is the sentence the "Which LOOKs" dialog prints
                # under a one-look Upload. Refused before start_show(),
                # because after it the fleet is already running - and on
                # its own answer, never on the burn gate's `force`.
                if not body.get("split_ok"):
                    self._one_timeline(fleet)
                from_s = body.get("from_s")
                if from_s is None:
                    # No range check skipped here: fleet.start_show()
                    # below validates whichever `at` it is given, this
                    # branch included - a remembered position from a
                    # SEEK is not proof it still fits a show re-uploaded
                    # since (found in review, was the open door).
                    at = fleet.start_at
                else:
                    if (not isinstance(from_s, (int, float))
                            or isinstance(from_s, bool)):
                        raise ValueError("Where to start from must be a "
                                         "number of seconds.")
                    at = round(float(from_s), 1)
                    if at > 0 and body.get("manual") is not True:
                        raise ValueError(
                            'Manual control is off. Tick "Manual control" '
                            "to start from a time other than 0:00.")
                # start_show() range-checks `at` itself (the same
                # ValueError seek() raises) and uses exactly this value -
                # never re-reading fleet.start_at - so the note below and
                # the T0 actually run on can never disagree. The same
                # `force` that waves through a second START also waves
                # through a unit that merely failed to burn some boards
                # (never one still burning, offline, or on another show).
                if lead is None:
                    # From 0:00 the show's countdown (11 s unless changed);
                    # from a mark the ordinary lead - a restart mid-show is
                    # not a show opening (review of 3f67087, MED-1).
                    lead = (DEFAULT_LEAD_S if float(at or 0) > 0
                            else self.workspace.start_countdown())
                results = fleet.start_show(lead, at, force=bool(body.get("force")))
                response = {"units": results, "lead_s": lead, "from_s": at}
                if at > 0:
                    response["note"] = (f"Started from "
                                        f"{timeline.format_clock(at)}.")
                return self._json(response)
            results = fleet.next_cue(lead)
            return self._json({"units": results, "lead_s": lead, "note":
                               "" if results else "No cue ahead to jump to "
                               "(or it is already due)."})
        if command == "hold":
            results = fleet.hold()
            return self._json({"units": results, "note":
                               "" if results else "The show is not running."})
        if command == "resume":
            results = fleet.resume()
            return self._json({"units": results, "note":
                               "" if results else "The show is not on hold."})
        if command == "stop":
            return self._json({"units": fleet.stop_show()})
        if command == "wifi_select":
            # EXHIBITION mode: move every online unit to a Wi-Fi profile
            # (AZ-Epaper, or the router's), `after_s` from now, so they
            # all switch together after the last one has been told. The
            # unit refuses (409) while a show runs or is held on it, and
            # the answer here is per unit. This host's own unit is told
            # last (see _own_units): its switch takes the hotspot down.
            profile = str(body.get("profile") or "").strip()
            if not profile or len(profile) > 64:
                raise ValueError("profile: the Wi-Fi profile's name")
            after = body.get("after_s", 20)
            low, high = WIFI_SWITCH_RANGE_S
            if not _is_number(after) or not low <= float(after) <= high:
                raise ValueError(f"after_s: {low:.0f} to {high:.0f} seconds")
            own = self._own_units()
            return self._json({"units": fleet.wifi_select(profile, float(after),
                                                          last=own,
                                                          hotspot=self.hotspot),
                               "last": own, "hotspot": self.hotspot,
                               "profile": profile, "after_s": float(after)})
        if command in ("cancel", "standby", "release"):
            units = body.get("units") or list(fleet.links)
            if command == "standby":
                fleet.stop_show()           # a running show would refuse it
            if command != "cancel":
                with self.prepared_lock:
                    for unit in units:
                        self.prepared.pop(unit, None)
            return self._json({"units": fleet.simple(units, "/" + command)})
        self._send(404, b"not found", "text/plain")


class _Server(ThreadingHTTPServer):
    # http.server asks for SO_REUSEADDR, which on Windows lets a second
    # process bind a port that is already being served - two conductors
    # then answer the same URL at random (seen 2026-09-21). Off on
    # Windows, so the second bind fails as it should.
    allow_reuse_address = os.name != "nt"
    daemon_threads = True


def make_server(workspace, port: int = 8765, host: str = "127.0.0.1",
                fleet: "Fleet | None" = None, speaker=None,
                token: "str | None" = None, passcode: "str | None" = None,
                hotspot: str = DEFAULT_HOTSPOT_UNIT,
                adopt: bool = False,
                label: "str | None" = None) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,),
                   {"workspace": Workspace(workspace), "fleet": fleet,
                    "speaker": speaker, "token": token, "passcode": passcode,
                    "hotspot": hotspot, "adopt": adopt, "label": label,
                    "prepared": {}, "send_jobs": {}})
    server = _Server((host, port), handler)
    handler.port = server.server_address[1]     # the real one when port was 0
    return server


# `serve --label`: the name of a Conductor that is a SEPARATE application
# from the show PC's own - Start Exhibition Conductor.bat passes EXHIBITION.
# The page wears it as an amber badge next to the app name, puts it first in
# the window title and tints the tab's icon amber; /api/state carries it.
# Short, because it sits in the top bar and in a browser tab's title.
LABEL_MAX = 24


def clean_label(label) -> "str | None":
    """`serve --label`, tidied: one line, trimmed, at most LABEL_MAX
    characters; None for nothing at all (the show PC's own Conductor)."""
    text = " ".join(str(label or "").split())
    return text[:LABEL_MAX] or None


# The fleet.json a labelled Conductor (Start Exhibition Conductor.bat on the
# PC) writes into its workspace whenever there is none - the first start, or
# after the operator deleted it: the default units (192.168.51.10x, the
# router's addresses, default_units()) with radxa-05 named as the hotspot
# unit - which is what the fleet-wide Wi-Fi switch and "Send workspace to"
# default to. Never a passcode, never "adopt", never a speaker: those are
# radxa-05's own (radxa/exhibition/fleet.json), and the PC's copy is for
# authoring, Upload and Send. An existing fleet.json is never touched.
PC_FLEET_TEMPLATE = {
    "_comment": ("This PC's exhibition Conductor (Start Exhibition Conductor.bat, "
                 "port 8766, workspace exhibition-data). The units are the router's "
                 "defaults radxa-NN -> 192.168.51.1NN:8787; \"hotspot\" names the unit "
                 "that is the AZ-Epaper hotspot at the venue. To Send workspace to "
                 "radxa-05 add its \"passcode\" here (the same value as in "
                 "/home/radxa/exhibition/fleet.json on radxa-05) - nothing else: "
                 "no adopt, no speaker, this is not the venue's Conductor."),
    "hotspot": DEFAULT_HOTSPOT_UNIT,
}


def write_fleet_template(root: Path, template: dict = PC_FLEET_TEMPLATE) -> bool:
    """Put `template` at <root>/fleet.json when there is none - True when
    written, False when a fleet.json (any content, even a broken one) is
    already there. Exclusive create (open "x"): a file that appears
    between the look and the write is kept too, never overwritten. Any
    other failure (a read-only folder, say) is the caller's OSError - the
    Conductor serves without the template, it does not stop."""
    path = Path(root) / "fleet.json"
    text = json.dumps(template, indent=1, ensure_ascii=False) + "\n"
    try:
        with open(path, "x", encoding="utf-8") as handle:
            handle.write(text)
    except FileExistsError:
        return False
    return True


def conductor_info(port: int, timeout: float = 2.0) -> "dict | None":
    """Who answers on 127.0.0.1:<port>: {"port", "label", "workspace_name",
    "workspace"} for a Conductor, None for nothing or something else. Asks
    /api/conductor; a Conductor from before it (404) is asked /api/state
    and comes back with what that has (label None, no folder name)."""
    import urllib.error
    import urllib.request

    for path in ("/api/conductor", "/api/state"):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}",
                                        timeout=timeout) as response:
                reply = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and path == "/api/conductor":
                continue
            return None
        except (OSError, ValueError):
            return None
        if not isinstance(reply, dict) or not (
                "workspace_name" in reply or "workspace" in reply):
            return None
        return {"port": port, "label": reply.get("label"),
                "workspace_name": reply.get("workspace_name"),
                "workspace": reply.get("workspace")}
    return None


def already_serving(port: int) -> bool:
    """Is a conductor answering on this port already?"""
    return conductor_info(port) is not None


# The two launchers' ports: Start Conductor.bat (the show's, no label) on
# 8765 and Start Exhibition Conductor.bat (--label EXHIBITION) on 8766. Each
# watches the OTHER, because an open idle Conductor still acts on the units:
# Fleet._supervise tells a running unit "stopped (missed STOP)" once its own
# STOP flag is set and fires its armed clears - so the other window has to be
# closed before Upload / START here, and the page says so while it is up.
SHOW_CONDUCTOR_PORT = 8765
EXHIBITION_CONDUCTOR_PORT = 8766
OTHER_CONDUCTOR_EVERY_S = 5.0


def other_conductor_port(port: int, label: "str | None") -> "int | None":
    """The port the OTHER launcher's Conductor would answer on: a labelled
    Conductor watches the show's 8765, an unlabelled one the exhibition's
    8766 - never its own port (a labelled one started on 8765 watches
    8766, and the other way round)."""
    other = SHOW_CONDUCTOR_PORT if label else EXHIBITION_CONDUCTOR_PORT
    if other == port:
        other = EXHIBITION_CONDUCTOR_PORT if label else SHOW_CONDUCTOR_PORT
    return None if other == port else other


def other_conductor_warning(info: dict) -> str:
    """The one sentence, for the console and the page's top bar."""
    where = f"port {info.get('port')}"
    if info.get("workspace_name"):
        where += f" (workspace {info['workspace_name']})"
    if info.get("label"):
        where += f" [{info['label']}]"
    return (f"another Conductor is running on {where} - close its black window "
            "(Ctrl+C) before Upload or START here: an open Conductor still acts "
            "on the units")


class OtherConductorWatch(threading.Thread):
    """Probes the other launcher's port every OTHER_CONDUCTOR_EVERY_S and
    keeps `handler.other_conductor` current: the probe's dict while it
    answers (with `warning`, the sentence), None once it is gone - so the
    page's red note appears and disappears on its own. The probe is
    conductor_info() unless a test hands in another."""

    def __init__(self, handler, port: "int | None", probe=None,
                 every_s: float = OTHER_CONDUCTOR_EVERY_S):
        super().__init__(name="other-conductor", daemon=True)
        self.handler = handler
        self.port = port
        self.probe = probe or conductor_info
        self.every_s = every_s
        self._stop = threading.Event()

    def check(self) -> "dict | None":
        """One probe, applied. Returns what the page will see."""
        info = self.probe(self.port) if self.port else None
        seen = None
        if info:
            seen = {"port": info.get("port", self.port), "label": info.get("label"),
                    "workspace_name": info.get("workspace_name")}
            seen["warning"] = other_conductor_warning(seen)
        self.handler.other_conductor = seen
        return seen

    def run(self) -> None:
        while not self._stop.wait(self.every_s):
            try:
                self.check()
            except Exception:           # noqa: BLE001 - a probe must never end the watch
                self.handler.other_conductor = None

    def stop(self) -> None:
        self._stop.set()


def reachable_urls(host: str, port: int) -> "list[str]":
    """The URLs the page answers on, for the console: the one address
    when bound to one, otherwise localhost and every IPv4 this host has
    (an `--host 0.0.0.0` on radxa-05 is reached as 10.42.0.1 from the
    hotspot and as 192.168.51.105 from the router - both are printed)."""
    if host not in ("0.0.0.0", "", "::"):
        return [f"http://{host}:{port}"]
    urls = [f"http://127.0.0.1:{port}"]
    for address in local_ipv4s():
        url = f"http://{address}:{port}"
        if url not in urls:
            urls.append(url)
    return urls


def offer_startup_shows(fleet: "Fleet", ws: Workspace
                        ) -> "tuple[dict[str, dict], list[str]]":
    """EXHIBITION mode's startup adoption (`serve --adopt`): compile the
    workspace and offer the per-unit shows to the fleet. A unit adopted
    (it reports that id, pictures burned) is marked as holding the
    compile's REVISION, exactly as an Upload would mark it - so the page's
    "changed since" and START's one-timeline gate work over an adopted
    show, and an edit made after the restart is refused with "Upload
    again" instead of running the old pictures under the new timeline.
    A timeline with problems compiles to nothing and offers nothing."""
    rev = ws.revision()
    try:
        compiled, problems = ws.compile_show()
    except Exception as exc:                # noqa: BLE001 - a bad workspace still serves
        compiled, problems = {}, [f"{exc.__class__.__name__}: {exc}"]
    if compiled and not problems:
        def mark(unit: str) -> None:
            ws.mark_written("upload", rev, units=[unit],
                            all_units=sorted(set(compiled) | ws.timeline_units()))
        fleet.offer_shows(compiled, on_adopt=mark)
    return compiled, problems


def passcode_problem(host: str, passcode) -> "str | None":
    """Why a Conductor bound to other hosts may not start with this
    passcode - none at all, or the example value still in fleet.json - or
    None when it may. Loopback needs none: nothing but this host reaches
    it."""
    if host in _LOCAL_HOSTS or host == "localhost":
        return None
    if not passcode:
        return ("no passcode - put \"passcode\": \"<your own>\" into the "
                "workspace's fleet.json (chmod 600) or pass --passcode; every "
                "other host then needs it before it may change the show")
    if str(passcode) == PASSCODE_EXAMPLE:
        return (f"the passcode is still the example value {PASSCODE_EXAMPLE!r} "
                "from radxa/exhibition/fleet.json - choose your own")
    return None


def local_ipv4s() -> "list[str]":
    """This host's own IPv4 addresses, without a DNS lookup: the UDP
    "connect" trick (no packet is sent) against the two networks the
    fleet lives on and the public one, which yields the address the
    kernel would route each from, plus `ip -4 -o addr` where there is
    one (Linux) - never gethostbyname_ex(), which resolves the hostname
    and hangs a thread while the resolver waits on a hotspot with no
    upstream."""
    found: "list[str]" = []

    def add(address: str) -> None:
        if address and not address.startswith("127.") and address not in found:
            found.append(address)

    for probe in ("10.42.0.1", "192.168.51.1", "8.8.8.8"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect((probe, 9))
                add(sock.getsockname()[0])
        except OSError:
            continue
    try:
        import subprocess
        out = subprocess.run(["ip", "-4", "-o", "addr"], capture_output=True,
                             text=True, timeout=2).stdout
        for line in out.splitlines():
            parts = line.split()
            if "inet" in parts:
                add(parts[parts.index("inet") + 1].split("/")[0])
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return found


def serve(workspace, port: int = 8765, open_browser: bool = False,
          host: str = "127.0.0.1", speaker: bool = False,
          speaker_lead_ms: "float | None" = None,
          speaker_output: "str | None" = None, speaker_factory=None,
          speaker_runner=None, passcode: "str | None" = None,
          adopt: bool = False, label: "str | None" = None) -> int:
    """`python -m conductor serve`. `host` is 127.0.0.1 unless asked
    (EXHIBITION mode: 0.0.0.0 on the unit that is also the hotspot);
    `speaker` plays the show's music through mpg123 on this host
    (conductor/speaker.py), `speaker_lead_ms` trims its output allowance,
    `speaker_output` is mpg123's -o module ("alsa" under systemd);
    `passcode` (or fleet.json's) gates the page from other hosts; `label`
    names a SEPARATE Conductor (the PC's exhibition one: its page wears
    the badge, and its workspace gets PC_FLEET_TEMPLATE whenever it has
    no fleet.json)."""
    import webbrowser

    label = clean_label(label)
    folder = Path(workspace).name
    url = f"http://127.0.0.1:{port}"
    # Double-clicking the launcher twice must not be an error, and must
    # not start a second server: it just brings the page up again. But
    # only when it IS this Conductor: the show's launcher finding the
    # exhibition's Conductor on its port (or the other way round, or any
    # `serve` with another folder) must say so, not open the wrong page.
    running = conductor_info(port) if port else None
    if running:
        same = ("workspace_name" not in running       # older: cannot tell
                or (running.get("label") == label
                    and running.get("workspace_name") == folder))
        if not same:
            print(f"a different Conductor is on port {port} (label "
                  f"{running.get('label') or 'none'}, workspace "
                  f"{running.get('workspace_name') or '?'}) - close it (its "
                  "black window, Ctrl+C) before starting this one (label "
                  f"{label or 'none'}, workspace {folder})", flush=True)
            return 2
        print(f"conductor UI is already running: {url}", flush=True)
        if open_browser:
            webbrowser.open(url)
        return 0
    config = Workspace(workspace)             # makes the folder (and files/)
    templated = False
    if label:
        try:
            templated = write_fleet_template(config.root)
        except OSError as exc:
            print(f"warning: could not write {config.root / 'fleet.json'} "
                  f"({exc}) - serving without it; the units are the defaults",
                  flush=True)
    units, token = config.fleet_config()
    stored = config.fleet_option("passcode")
    if passcode and stored and str(passcode) != str(stored):
        print(f"warning: --passcode and {Path(workspace) / 'fleet.json'}'s "
              "\"passcode\" differ - the command line's is used", flush=True)
    passcode = passcode or stored
    problem = passcode_problem(host, passcode)
    if problem:
        # Open to other hosts with no real passcode is open to the room:
        # refused before anything listens (review of 53b9b6b, MED-6).
        print(f"refusing to serve on {host}: {problem}", flush=True)
        return 2
    hotspot = str(config.fleet_option("hotspot", DEFAULT_HOTSPOT_UNIT))
    adopt = bool(adopt or config.fleet_option("adopt", False))
    try:
        server = make_server(workspace, port, host, token=token,
                             passcode=passcode, hotspot=hotspot, adopt=adopt,
                             label=label)
    except OSError as exc:
        print(f"cannot listen on {host}:{port}: {exc}", flush=True)
        return 1
    ws = server.RequestHandlerClass.workspace
    # The Loop's settings come from THIS workspace's show.json, read when a
    # run reaches its end - never cached on the fleet.
    fleet = Fleet(units, token, loop_settings=ws.loop_settings)
    # EXHIBITION mode only (`--adopt`, or fleet.json "adopt": true): what
    # this workspace compiles to is offered to the units, so a restart of
    # the headless conductor does not cost an Upload of every picture
    # before the Loop or START work again. The show PC never offers - a
    # restart there costs an Upload exactly as it always has (review of
    # d79681a: an adopted show with no marks let an edit + START run the
    # old pictures).
    compiled, problems = {}, []
    if adopt:
        compiled, problems = offer_startup_shows(fleet, ws)
    fleet.start()
    server.RequestHandlerClass.fleet = fleet
    player = None
    if speaker:
        from .speaker import DEVICE_LATENCY_S, Speaker

        def track():
            info = ws.music_info()
            return None if info is None else ws.music / Path(info["name"]).name
        extra = (DEVICE_LATENCY_S if speaker_lead_ms is None
                 else float(speaker_lead_ms) / 1000.0)
        player = Speaker(track, fleet.run_snapshot, factory=speaker_factory,
                         extra_lead_s=extra, output=speaker_output,
                         runner=speaker_runner,
                         # The host's loudness lives in fleet.json (per host,
                         # never in the show), and a change goes back there.
                         volume=config.fleet_option("speaker_volume", 70),
                         save_volume=lambda v: ws.set_fleet_option("speaker_volume", v))
        player.start()
        server.RequestHandlerClass.speaker = player
    if open_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    for reachable in reachable_urls(host, port):
        print(f"{label + ' ' if label else ''}conductor UI: {reachable}", flush=True)
    print(f"  workspace {Path(workspace).resolve()}"
          + (f"  label: {label}" if label else "")
          + ("  speaker: mpg123 on this host" if speaker else "")
          + ("  passcode: set" if passcode else ""), flush=True)
    if templated:
        print(f"  wrote {Path(workspace).resolve() / 'fleet.json'} (hotspot "
              f"{hotspot}; add radxa-05's \"passcode\" there before Send "
              "workspace to ...)", flush=True)
    if compiled:
        print(f"  show compiles for {', '.join(sorted(compiled))} - a unit "
              "holding it is adopted on its first poll", flush=True)
    for problem in problems[:5]:
        print(f"  timeline: {problem}", flush=True)
    # The other launcher's Conductor (the show's 8765 from the exhibition's
    # 8766, and the other way round): said once here when it is up at
    # start, and kept current for the page's red note - an open Conductor,
    # idle or not, still acts on the units (Fleet._supervise), so it has to
    # be closed before Upload / START here.
    watch = OtherConductorWatch(server.RequestHandlerClass,
                                other_conductor_port(port, label))
    other = watch.check()
    if other:
        print(f"WARNING: {other['warning']}", flush=True)
    watch.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        watch.stop()
        if player is not None:
            player.stop()
        fleet.stop()
        server.server_close()
    return 0
