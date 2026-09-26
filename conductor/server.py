"""The conductor's local web UI: import the looks' CSVs, check and see them.

    python -m conductor serve            # http://127.0.0.1:8765

Standard library only, bound to localhost: this runs on the show PC and
nothing else should reach it. The page (conductor/web/index.html) reads
one JSON document, /api/state, rebuilt from the workspace folder on
every request - a dozen small CSVs parse in milliseconds, and it means
a file edited or dropped in by hand shows up on the next refresh.

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
import importlib.util
import json
import os
import shutil
from dataclasses import replace
import re
import threading
import time
import unicodedata
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import sequence, showfile, timeline
from .fleet import DEFAULT_LEAD_S, Fleet, default_units
from .look import (PALETTE, Design, LookError, LookMap, check,
                   compile_design, default_shift, unit_board_ids)
from .look import kind as file_kind
from .look import file_stem, map_item
from .look import name_problem as look_name_problem
from .look import normalize_name as look_normalize

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
DEMO_NAME_MAX = 14        # the unit's LCD menu row
# The unit's LCD font (ui/render.py, DejaVu) has no Japanese glyphs, so a
# demo's name must be plain ASCII the unit can actually draw - the same
# message whichever way the name is unusable (empty, too long, or not
# printable ASCII), so the operator sees one clear rule, not three.
DEMO_NAME_MESSAGE = (f"A-Z, 0-9 and symbols, up to {DEMO_NAME_MAX} characters "
                     "(the unit's screen cannot show Japanese)")
_DEMO_NAME_OK = re.compile(r"^[\x20-\x7e]+$")     # printable ASCII only
_DEMO_SLUG_OK = re.compile(r"^[a-z0-9][a-z0-9-]*$")
# Parts of show.json that never reach a unit: the operator's own notes
# about the show (see Workspace.revision).
_REVISION_IGNORES = {"music", "labels"}
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


def safe_music_name(name: str) -> str:
    """A name for the music blob on disk. Not a CSV rule: see above."""
    return _SAFE_MUSIC_NAME.sub(
        "_", Path(unicodedata.normalize("NFC", str(name))).name)


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
    "item" and the "units"/"labels"/"boards" maps all key on. A name the
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
    for key in ("units", "labels", "boards"):
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


def _only_units(raw, shows: "dict[str, dict]") -> "list[str] | None":
    """The `units` of an Upload / Save on the units: which units of the
    compiled show this write is for ("Which LOOKs" in the page's dialog),
    or None for all of them - absent means all, which is what every
    client before this field sent and what the page sends for "All
    LOOKs".

    A name that is not a unit of THIS timeline is refused by name rather
    than quietly dropped: the page builds the list from the timeline it
    is showing, so a mismatch means the two disagree about what is where
    (a stale page, another operator's edit) - exactly the moment to stop
    rather than write a look to nothing and report success."""
    if raw is None:
        return None
    if not isinstance(raw, list) or not all(isinstance(u, str) for u in raw):
        raise ValueError("units must be a list of unit names")
    names = list(dict.fromkeys(raw))
    if not names:
        raise ValueError("no unit chosen - pick a LOOK, or All LOOKs")
    # Nothing compiled at all (a timeline with a problem): the write is
    # already going nowhere and the problems are the answer - saying
    # "radxa-01 is not a unit of this timeline" on top of them would send
    # the operator looking for the wrong thing.
    for name in names if shows else ():
        if name not in shows:
            raise ValueError(f"{name} is not a unit of this timeline")
    return names


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
            return json.loads(self._show_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

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
        if after == before:
            return
        history = self._load_history()
        history["undo"] = (history["undo"] + [before])[-HISTORY_DEPTH:]
        history["redo"] = []
        self._write(self._show_path, after)
        self._write(self._history_path, history)

    def _step(self, take: str, give: str) -> bool:
        with self._lock:
            history = self._load_history()
            if not history[take]:
                return False
            current = self._load_show()
            restored = history[take].pop()
            history[give] = (history[give] + [current])[-HISTORY_DEPTH:]
            self._write(self._show_path, restored)
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
            eff = timeline.effective_refresh(cleaned, refresh)
            span = 0.0
            sweep = timeline.resolve(cleaned, transitions)
            if sweep["sequence"] != "natural":
                look_map = maps.get(cleaned["item"].lower())
                if look_map is not None:
                    span = sequence.span_s(look_map, sweep["sequence"],
                                           sweep["span_s"])
            cleaned["at"] = max(0.0, round(cleaned["at"] - eff - span, 1))
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
            self._commit(before, dict(before, boards=every))

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
            for key in ("labels", "boards"):    # it starts as what it copies
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
        duration = timeline.parse_clock(duration)
        if not 1 <= duration <= 6 * 3600:
            raise ValueError("the show lasts between 1 s and 6 h")
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
            "transitions": show.get("transitions") or {},
            "labels": show.get("labels") or {},
            "units": show.get("units") or {},
            "boards": show.get("boards") or {},
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
            duration = timeline.parse_clock(payload["duration"])
            if not 1 <= duration <= 6 * 3600:
                raise ValueError("the show lasts between 1 s and 6 h")
            changes["duration"] = duration
        if "refresh_s" in payload:
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
                    problem = ("not a *_map.csv, *_color_NAME_grid.csv "
                               "or *_HW.csv (the wiring site writes _HW "
                               "in capitals)")
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
        # Validate the whole timeline before a single CSV is written.
        changes, cues = self._validate_show(show)
        with self._lock:
            existing = {p.name for p in self.files.glob("*.csv")}
        saved: "list[str]" = []
        overwritten: "list[str]" = []
        for name, text in to_save:
            try:
                saved_name = self.save(name, text)
            except OSError as exc:
                raise ValueError(
                    f"could not save {name}: {exc} - {len(saved)} file(s) "
                    f"already saved, {len(refused)} refused before this")
            if saved_name in existing:
                overwritten.append(saved_name)
            saved.append(saved_name)
        cue_count, warnings = self._apply_show_changes(changes, cues)
        music = payload.get("music") or show.get("music")
        return {"ok": True, "saved": saved, "refused": refused,
                "renamed": renamed,
                "overwritten": overwritten, "cues": cue_count,
                "warnings": warnings, "units_kept": units_kept,
                "boards_kept": boards_kept,
                "music": music.get("name") if isinstance(music, dict) else None}

    # ---- files ----

    # One grammar for both sides of the wire: conductor/look.py's
    # file_kind() also reads the production site's own
    # <item>_<配色案名>_HW.csv as a grid, so a file straight from the
    # "HW 用 CSV" button uploads without being renamed first.
    kind = staticmethod(file_kind)

    def save(self, name: str, text: str) -> str:
        name = workspace_name(name)          # raises on an unusable name
        if self.kind(name) is None:
            raise ValueError(f"{name}: not a *_map.csv, "
                             "*_color_NAME_grid.csv or *_HW.csv (the "
                             "wiring site writes _HW in capitals)")
        with self._lock:
            # open(), not Path.write_text(newline=...): that is 3.10+, and
            # the units' Python 3.9 should be able to run this too.
            with open(self.files / name, "w", encoding="utf-8",
                      newline="") as handle:
                handle.write(text)
        return name

    def delete(self, name: str) -> None:
        target = self.files / Path(name).name
        with self._lock:
            if target.is_file():
                target.unlink()

    # ---- what goes to the units ----

    def fleet_config(self) -> "tuple[dict, str | None]":
        try:
            config = json.loads((self.root / "fleet.json")
                                .read_text(encoding="utf-8"))
        except (OSError, ValueError):
            config = {}
        units = dict(default_units())
        units.update(config.get("units") or {})
        return units, config.get("token") or None

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
                ids = unit_board_ids(on_unit)
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
        every chip red for nothing (found in review).

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
                     all_units: "list[str] | None" = None) -> None:
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
        revision."""
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
        if targets and all(per_unit.get(unit) == rev for unit in targets):
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
        # Taken before the work, so what is remembered below is the state
        # this compile was OF, not one an edit landed on meanwhile.
        rev = self.revision()
        shows, problems = self._compile_show()
        self.compiled = {"revision": rev, "problems": list(problems),
                         "units": sorted(shows)}
        return shows, problems

    def _compile_show(self) -> "tuple[dict[str, dict], list[str]]":
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
            return {}, broken
        return showfile.build(maps, assigned, lambda name: designs[name],
                              cues, refresh, duration, cue_problems,
                              name=self.root.name)

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
        for group, keys in groups.items():
            try:
                ids = unit_board_ids([maps[k] for k in keys])
            except LookError as exc:
                unit_problems[group] = exc.problems
                ids = None
            for key in keys:
                look_map = maps[key]
                items[key]["boards"] = look_map.dip_sheet(ids)
                # The page can renumber boards: which one is which in the CSV.
                was = {new: old for old, new in
                       self._own_boards(show, items[key]["item"]).items()}
                for board in items[key]["boards"]:
                    board["source_no"] = was.get(board["board_no"],
                                                 board["board_no"])
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
        cue_ends = timeline.ends(cues, refresh, duration)
        for cue in cues:
            cue["sent"], cue["complete"] = timeline.times(cue, refresh)
            cue["refresh"] = timeline.effective_refresh(cue, refresh)
            cue["refresh_source"] = ("cue" if isinstance(
                cue.get("refresh_s"), (int, float)) else "show")
            cue["end"], cue["end_source"] = cue_ends[cue["id"]]
            cue["problems"] = cue_problems[cue["id"]]
        unit_boards: "dict[str, int]" = {}
        for fact in facts.values():
            name = fact["unit"] or f"({fact['item']})"
            unit_boards[name] = unit_boards.get(name, 0) + fact["boards"]
        return {"show": {"duration": duration, "refresh_s": refresh,
                         "cues": cues, "warnings": warnings,
                         "min_interval": {unit: timeline.min_interval(n, refresh)
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


class Handler(BaseHTTPRequestHandler):
    workspace: Workspace = None            # set by make_server()
    fleet: "Fleet | None" = None
    prepared: "dict[str, str]" = {}        # unit -> the cue it was last sent
    prepared_lock = threading.Lock()       # request threads share the dict
    server_version = "conductor"

    def log_message(self, fmt, *args):     # keep the console for errors
        pass

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, status: int = 200) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_UPLOAD:
            raise ValueError("upload too large")
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/music/file":
            return self._music_file(head=False)
        if path == "/api/show/export":
            return self._export_show()
        try:
            if path == "/api/simulator":
                # Inside the try, so anything unexpected on the way to the
                # build (a show.json that will not parse, say) comes back
                # as the JSON error the page knows how to show, not as a
                # traceback and a dead socket.
                query = urllib.parse.parse_qs(self.path.partition("?")[2])
                return self._simulator(query.get("music", ["0"])[0] == "1")
            if path == "/api/state":
                return self._json(self.workspace.state())
            if path == "/api/fleet":
                # `timeline` is about the workspace, not the units: what
                # the timeline is now, and what it was when it was last
                # written to them (Workspace.written_state) - the page's
                # "up to date" / "changed since" needs both, and an id
                # comparison alone cannot see an edit made since.
                if self.fleet is None:
                    return self._json({"units": [], "last_fire": None,
                                       "run": None, "shows": {},
                                       "corrections": [], "prepared": {},
                                       "start_at": 0.0, "show_duration": None,
                                       "timeline": self.workspace.written_state()})
                with self.prepared_lock:
                    prepared = dict(self.prepared)
                return self._json(dict(self.fleet.snapshot(),
                                       prepared=prepared,
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
            # Refused before a single byte is read off the wire.
            self.close_connection = True
            return self._json({"error": f"music is at most "
                               f"{MAX_MUSIC // (1024 * 1024)} MB"}, status=400)
        # The page sends encodeURIComponent(name): decoded here so a
        # Japanese or accented file name survives, not just ASCII ones.
        name = urllib.parse.unquote(self.headers.get("X-File-Name") or "music")
        if Path(name).suffix.lower() not in _MUSIC_TYPES:
            allowed = ", ".join(sorted(_MUSIC_TYPES))
            # Answering before the request body is read makes Windows
            # reset the connection under the client, which then sees
            # ConnectionAborted instead of this 400 (a flaky test found
            # it). Drain a small body first; a big one is closed instead.
            if length <= 1024 * 1024:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
            else:
                self.close_connection = True
            return self._json({"error": f"music must be one of {allowed}"},
                              status=400)
        try:
            self.workspace.save_music(name, self.rfile, length)
        except (OSError, ValueError) as exc:
            return self._json({"error": str(exc)}, status=400)
        return self._json({"ok": True, "music": self.workspace.music_info()})

    def do_POST(self):
        if self.path == "/api/music":
            return self._upload_music()
        try:
            body = self._body()
            if self.path == "/api/files":
                saved, refused = [], []
                for entry in body.get("files", []):
                    try:
                        saved.append(self.workspace.save(entry["name"],
                                                         entry["text"]))
                    except ValueError as exc:
                        refused.append(str(exc))
                return self._json({"saved": saved, "refused": refused})
            if self.path == "/api/duplicate":
                return self._json({"ok": True, "item":
                                   self.workspace.duplicate(body["item"])})
            if self.path == "/api/boards":
                self.workspace.set_boards(body["item"], body["boards"])
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
                self.workspace.set_timeline(body.get("duration", 600),
                                            body.get("cues", []),
                                            body.get("refresh_s"))
                return self._json({"ok": True})
            if self.path == "/api/show/import":
                cues, warnings = self.workspace.import_show(body)
                return self._json({"ok": True, "cues": cues,
                                   "warnings": warnings})
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
        different timelines, or on one it has moved past.

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
        # "Upload again" is no use when an Upload could not happen: the
        # timeline itself does not build (N2). Read off the last compile,
        # and only while it is still a compile of what is on screen.
        compiled = self.workspace.compiled
        if (compiled and compiled["revision"] == rev
                and (compiled["problems"] or not compiled["units"])):
            raise ValueError("the timeline has problems - fix them on the "
                             "Timeline tab, then Upload")
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
            shows, problems = self.workspace.compile_show()
            only = _only_units(body.get("units"), shows)
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
                                            all_units=sorted(shows))
            return self._json({"units": results, "problems": problems,
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
            # The same "whole show or not at all" rule as Upload: a
            # timeline with a problem writes nothing, and the page shows
            # exactly the problems Upload itself would have refused on.
            rev = self.workspace.revision()      # see the upload above
            shows, problems = self.workspace.compile_show()
            only = _only_units(body.get("units"), shows)
            results = (fleet.write_demo(name, loop, shows, only=only)
                       if shows else {})
            written = sorted(u for u, r in results.items() if r["ok"])
            if written:
                self.workspace.mark_written(f"demo:{name}", rev, units=written,
                                            all_units=sorted(shows))
            return self._json({"units": results, "problems": problems,
                               "name": name})
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
            lead = float(body.get("lead_s", DEFAULT_LEAD_S))
            if not 0.5 <= lead <= 60:
                raise ValueError("lead time is 0.5-60 s")
            if command == "start":
                if not fleet.shows:
                    return self._json({"units": {}, "note":
                                       "Nothing uploaded yet - Upload first."})
                # A second click on START must not move a running show's
                # clock; starting over is said out loud (the page asks).
                if fleet.run is not None and not body.get("force"):
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
                fleet: "Fleet | None" = None) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,),
                   {"workspace": Workspace(workspace), "fleet": fleet,
                    "prepared": {}})
    return _Server((host, port), handler)


def already_serving(port: int) -> bool:
    """Is a conductor answering on this port already?"""
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state",
                                    timeout=2) as response:
            return "workspace" in json.loads(response.read())
    except (OSError, ValueError):
        return False


def serve(workspace, port: int = 8765, open_browser: bool = False) -> int:
    import webbrowser

    url = f"http://127.0.0.1:{port}"
    # Double-clicking the launcher twice must not be an error, and must
    # not start a second server: it just brings the page up again.
    if port and already_serving(port):
        print(f"conductor UI is already running: {url}", flush=True)
        if open_browser:
            webbrowser.open(url)
        return 0
    try:
        server = make_server(workspace, port)
    except OSError as exc:
        print(f"cannot listen on port {port}: {exc}", flush=True)
        return 1
    units, token = Workspace(workspace).fleet_config()
    fleet = Fleet(units, token)
    fleet.start()
    server.RequestHandlerClass.fleet = fleet
    if open_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    print(f"conductor UI: http://127.0.0.1:{port}  (workspace "
          f"{Path(workspace).resolve()})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        fleet.stop()
        server.server_close()
    return 0
