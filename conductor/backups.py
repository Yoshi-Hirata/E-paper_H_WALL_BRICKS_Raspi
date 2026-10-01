"""Automatic workspace backups, and restoring one on site (EXHIBITION
mode, 2026-10-01).

The exhibition's Conductor runs headless on radxa-05 and the venue's
power is cut several times a day. A show damaged there - a half-written
show.json, a CSV gone, an edit nobody can undo - has to come back WITHOUT
the PC that authored it. So, with `serve --backups` (the exhibition's
service file) or fleet.json "backups": true, this module keeps
GENERATIONS of the workspace:

    <workspace>/../<workspace-name>-backups/
        20261001-152003-upload-1a2b3c4d.tar     a generation: exactly the
        20261001-153511-edit-9f8e7d6c.tar       tar GET /api/workspace/export
        ...                                     answers (show.json,
        index.json                              history.json, files/*.csv,
                                                the music) - never fleet.json

OUTSIDE the workspace folder, so an import (or a restore, which IS an
import) swapping the workspace's entries never touches them.

When a generation is taken (always on this module's own worker thread -
the tar is never written on an HTTP thread):

* `start`   - once when the Conductor starts, so the first boot with
              backups on already has one;
* `import`  - after a successful POST /api/workspace/import (Send workspace);
* `upload`  - after a successful Upload; that generation is MARKED as the
              one the units hold (index.json "uploaded") and is never
              pruned, however old;
* `edit`    - after edits, debounced: once the workspace has been quiet
              for EDIT_QUIET_S (60 s) after its last change - the worker
              looks at the files' sizes and times every POLL_S, so no
              edit path of the Workspace needs a hook;
* `prerestore` - the state just before a restore, so a restore of the
              wrong generation is itself undone by restoring this one.

A generation whose content equals the NEWEST one's is not written again
(the content revision: a hash over every member's name and bytes - not
Workspace.revision(), which reads mtimes and so changes on every import).
The newest `keep` (fleet.json "backup_keep", 1-50, default 5) are kept,
plus the uploaded one.

`edit` and `start` generations are never written while a run is going
or a START is presetting (`busy`: the SD card and the CPU belong to the
show then) - the change is remembered and taken once the run has ended
(the Loop's wait counts as ended), and none is written when the disk
would be left with less than MIN_FREE_BYTES.

Power-safe: each tar is written to a dot-named temp file in the folder,
flushed and fsync'ed, then read back in full (every member, and the
end-of-archive blocks - a tar cut at a member boundary still reads as a
shorter valid tar otherwise). That read is likely served from the page
cache, so it proves the tar is WELL-FORMED as written, not that the card
holds it - the fsync is what asks for that. Only then is it renamed into
place, the folder fsync'ed, and the generation entered in index.json
(written the same way). A temp file left by a power cut is deleted at
the next start; a tar the index does not know is read in full and
adopted, or deleted if it does not read.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import queue
import re
import shutil
import tarfile
import threading
import time
from pathlib import Path

from . import durable

MIN_FREE_BYTES = 200 * 1024 * 1024   # what a generation must leave free on the disk
BACKUP_KEEP = 5
BACKUP_KEEP_RANGE = (1, 50)
EDIT_QUIET_S = 60.0          # quiet after the last change before an "edit" generation
POLL_S = 2.0                 # how often the worker looks for a change
RESTORE_WAIT_S = 120.0       # how long a restore waits for its "prerestore" generation
INDEX_NAME = "index.json"
INDEX_VERSION = 1
TEMP_SUFFIX = ".part"
REASONS = ("start", "import", "upload", "edit", "prerestore")
HASH_CHUNK = 256 * 1024
_NAME = re.compile(r"^(\d{8})-(\d{6})-([a-z]+)-([0-9a-f]{8})\.tar$")
BACKUPS_OFF = ("backups are off on this Conductor - start it with --backups "
               "(or fleet.json \"backups\": true)")


def backups_folder(root) -> Path:
    """Where the generations of the workspace at `root` live: BESIDE it,
    <parent>/<name>-backups (/home/radxa/exhibition-backups)."""
    root = Path(root).resolve()
    return root.parent / f"{root.name}-backups"


def backup_listing(backups: "Backups | None") -> dict:
    """GET /api/backups - the generations newest first, or, on a
    Conductor without --backups (the PC's), that they are off."""
    if backups is None:
        return {"enabled": False, "backups": [], "uploaded": None,
                "problem": None, "why": BACKUPS_OFF}
    return backups.listing()


def start_backups(handler, config, flag: bool) -> "Backups | None":
    """serve()'s hook: with `serve --backups` or fleet.json "backups":
    true, a running Backups for the handler's workspace (handler.backups)
    - None otherwise, and then nothing at all is made on disk (the PC's
    default Conductor). `config` is the Workspace serve() reads
    fleet.json through."""
    if not (flag or config.fleet_option("backups") is True):
        return None
    raw = config.fleet_option("backup_keep", BACKUP_KEEP)
    keep = clean_keep(raw)
    if keep != raw:
        low, high = BACKUP_KEEP_RANGE
        print(f"warning: fleet.json \"backup_keep\" is {raw!r} - a whole number "
              f"{low}-{high}; keeping {keep}", flush=True)
    keeper = Backups(handler.workspace, keep=keep,
                     busy=lambda: fleet_busy(getattr(handler, "fleet", None))).start()
    handler.backups = keeper
    print(f"  backups: {keeper.folder} (the newest {keep}, and the one last "
          "uploaded)", flush=True)
    return keeper


def restore_note(units_hold: bool) -> str:
    """What a restore says - the LCD shows it as it stands."""
    return ("restored - units already hold it" if units_hold
            else "restored - Upload needed")


def clean_keep(value, default: int = BACKUP_KEEP) -> int:
    """fleet.json's "backup_keep": a whole number 1-50; anything else is
    the default (said by the caller, not raised - a typo there must not
    stop the show's Conductor from starting)."""
    low, high = BACKUP_KEEP_RANGE
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value if low <= value <= high else default


# Writing so that a power cut leaves the old file or the new one is
# conductor/durable.py's (index.json: atomic_write_json; the tar: its own
# .part file, durable.fsync_file, the read-back, os.replace, durable.fsync_dir).
# A temp file either leaves behind - `.<name>.part` (the tar's) or
# `.<name>.<hex>.tmp` (durable's) - is deleted at the next start.
_TEMP_ENDINGS = (TEMP_SUFFIX, ".tmp")


# ---- what a generation holds ----

def _feed(digest, name: str, size: int, chunks) -> None:
    digest.update(f"{name}\0{size}\0".encode("utf-8"))
    for chunk in chunks:
        digest.update(chunk)


def _file_chunks(path: Path):
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(HASH_CHUNK)
            if not chunk:
                return
            yield chunk


def _music_name(show_bytes: bytes) -> "str | None":
    try:
        show = json.loads(show_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    music = show.get("music") if isinstance(show, dict) else None
    if isinstance(music, dict) and music.get("name"):
        return Path(str(music["name"])).name
    return None


def _cues_of(show_bytes: "bytes | None") -> "int | None":
    if show_bytes is None:
        return 0
    try:
        show = json.loads(show_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    cues = show.get("cues") if isinstance(show, dict) else None
    return len(cues) if isinstance(cues, list) else 0


def content_revision(workspace) -> str:
    """The content revision of the workspace as it is on disk now - the
    same hash inspect_tar() makes of a tar exported from it (the same
    members, in export_tar's order), read without a tar. 16 hex digits."""
    digest = hashlib.sha1()
    root = Path(workspace.root)
    music = None
    for name in ("show.json", "history.json"):
        path = root / name
        if path.is_file():
            data = path.read_bytes()
            _feed(digest, name, len(data), [data])
            if name == "show.json":
                music = _music_name(data)
    for path in sorted(Path(workspace.files).glob("*.csv")):
        if path.is_file():
            _feed(digest, f"files/{path.name}", path.stat().st_size,
                  _file_chunks(path))
    if music:
        path = Path(workspace.music) / music
        if path.is_file():
            _feed(digest, f"music/{path.name}", path.stat().st_size,
                  _file_chunks(path))
    return digest.hexdigest()[:16]


def inspect_tar(path) -> dict:
    """Read the tar at `path` IN FULL - every member's every byte, and the
    two zero blocks that end an archive - and return {"revision", "cues"}.
    ValueError for anything short of a whole, readable tar: a tar cut off
    mid-member, cut at a member boundary (which tarfile alone would read
    as a valid shorter archive), or not a tar at all."""
    digest = hashlib.sha1()
    show = None
    try:
        with tarfile.open(str(path), mode="r:") as tar:
            for member in tar:
                if not member.isfile():
                    continue
                handle = tar.extractfile(member)
                if handle is None:
                    raise ValueError(f"{member.name}: unreadable")
                got = 0
                keep = member.name == "show.json"
                kept = []
                digest.update(f"{member.name}\0{member.size}\0".encode("utf-8"))
                with handle:
                    while True:
                        chunk = handle.read(HASH_CHUNK)
                        if not chunk:
                            break
                        got += len(chunk)
                        digest.update(chunk)
                        if keep:
                            kept.append(chunk)
                if got != member.size:
                    raise ValueError(f"{member.name}: cut short "
                                     f"({got} of {member.size} bytes)")
                if keep:
                    show = b"".join(kept)
            end = tar.offset
        with open(path, "rb") as raw:
            raw.seek(end)
            if raw.read(1024) != b"\0" * 1024:
                raise ValueError("the archive has no end (cut short)")
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise ValueError(f"not a whole tar: {exc}")
    return {"revision": digest.hexdigest()[:16], "cues": _cues_of(show)}


def _at_of(name: str) -> str:
    """"2026-10-01 15:20:03" out of a generation's file name."""
    match = _NAME.match(name)
    if not match:
        return ""
    day, clock = match.group(1), match.group(2)
    return (f"{day[:4]}-{day[4:6]}-{day[6:]} "
            f"{clock[:2]}:{clock[2:4]}:{clock[4:]}")


def fleet_busy(fleet) -> bool:
    """A run is going (not merely ended - the Loop's wait is free) or a
    START is presetting: no `edit` / `start` generation now."""
    if fleet is None:
        return False
    if getattr(fleet, "_staging", None) is not None:
        return True
    return fleet.run is not None and not fleet.run_is_over()


class _NotTaken(Exception):
    """A generation deliberately not taken (busy, damaged, no space)."""


class _Job:
    def __init__(self, reason: str, upload: "dict | None", protect):
        self.reason = reason
        self.upload = upload
        self.protect = set(protect or ())
        self.done = threading.Event()
        self.result: "dict | None" = None
        self.error: "str | None" = None
        self.not_taken: "str | None" = None    # why none was taken, on purpose
        self.state = "queued"                  # queued / running / done / cancelled
        self._state_lock = threading.Lock()

    def cancel(self) -> bool:
        """Never run it (a restore that gave up waiting for its safety
        copy must not have that copy taken AFTER some later swap). False
        when it is already running or done."""
        with self._state_lock:
            if self.state != "queued":
                return False
            self.state = "cancelled"
        self.done.set()
        return True

    def begin(self) -> bool:
        with self._state_lock:
            if self.state != "queued":
                return False
            self.state = "running"
            return True


class Backups:
    """The generations of one workspace. Every method is safe from any
    thread; the tars themselves are written by the worker (start()) - or,
    when no worker runs (the tests), inline by whoever asks."""

    def __init__(self, workspace, folder=None, keep: int = BACKUP_KEEP,
                 clock=time.monotonic, wall=time.time,
                 quiet_s: float = EDIT_QUIET_S, poll_s: float = POLL_S,
                 log=None, busy=None, min_free: int = MIN_FREE_BYTES):
        self.ws = workspace
        # () -> bool: the show is running now (fleet_busy over the
        # server's fleet) - no edit / start generation then.
        self._busy = busy or (lambda: False)
        self.min_free = int(min_free)
        self.folder = Path(folder) if folder else backups_folder(workspace.root)
        self.keep = clean_keep(keep)
        self.quiet_s = float(quiet_s)
        self.poll_s = float(poll_s)
        self._clock = clock
        self._wall = wall
        self._log = log or (lambda text: print(f"backups: {text}", flush=True))
        self._lock = threading.RLock()          # the index (held briefly)
        self._write_lock = threading.Lock()     # one generation at a time
        self._index: "dict | None" = None
        self._jobs: "queue.Queue[_Job]" = queue.Queue()
        self._thread: "threading.Thread | None" = None
        self._stop = threading.Event()
        self.problem: "str | None" = None       # the last thing that went wrong
        # Edits: the files' (name, size, mtime) as last seen, when that
        # last changed, and as they were at the last generation.
        self._seen = None
        self._changed_at: "float | None" = None
        self._baseline = None

    # ---- the worker ----

    def start(self, initial: bool = True) -> "Backups":
        """Tidy the folder, then run the worker: it takes a `start`
        generation (unless the newest already holds this content) and
        then looks for edits every poll_s."""
        self.load()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="workspace-backups")
        self._thread.start()
        if initial:
            self.request("start")
        return self

    def stop(self, timeout: float = 5.0) -> None:
        """End the worker - after the generation it is writing, if any
        (waited for up to `timeout`)."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._jobs.get(timeout=self.poll_s)
            except queue.Empty:
                job = None
            try:
                if job is not None:
                    self._run(job)
                else:
                    self.tick()
            except Exception as exc:            # noqa: BLE001 - the worker never dies
                self._fail(f"{exc.__class__.__name__}: {exc}")

    def request(self, reason: str, upload: "dict | None" = None,
                wait: "float | None" = None, protect=()) -> "_Job":
        """Ask for a generation. On the worker (queued, in order) when one
        runs, else inline. `wait` seconds: block until it is done (a
        restore's `prerestore`). Returns the job: `.result` is the
        generation it took or found equal, `.error` why there is none."""
        if reason not in REASONS:
            raise ValueError(f"reason: one of {', '.join(REASONS)}")
        job = _Job(reason, upload, protect)
        if self._thread is None or threading.current_thread() is self._thread:
            self._run(job)
            return job
        self._jobs.put(job)
        if wait:
            job.done.wait(wait)
        return job

    def after_import(self) -> None:
        """A workspace import landed (Send workspace)."""
        self.request("import")

    def after_upload(self, rev: str, whole: bool) -> None:
        """An Upload reached the units. `rev` is Workspace.revision() as
        the Upload took it, before compiling: the generation is marked as
        the units' only when the workspace is still exactly that when the
        tar is written. `whole`: every unit of the timeline took it."""
        self.request("upload", upload={"rev": rev, "whole": bool(whole)})

    def _run(self, job: _Job) -> None:
        if not job.begin():
            return                              # cancelled while it waited
        try:
            job.result = self._snapshot(job.reason, job.upload, job.protect)
        except _NotTaken as why:
            job.not_taken = str(why)
        except Exception as exc:                # noqa: BLE001 - reported
            job.error = f"{exc.__class__.__name__}: {exc}"
            self._fail(f"{job.reason}: {job.error}")
        finally:
            with job._state_lock:
                job.state = "done"
            job.done.set()

    def _fail(self, text: str) -> None:
        self.problem = text
        self._log(text)

    # ---- edits ----

    def _fingerprint(self):
        """(name, size, mtime) of everything a generation holds - a stat
        per file, about a millisecond: cheap enough every POLL_S."""
        root = Path(self.ws.root)
        paths = [root / "show.json", root / "history.json"]
        try:
            paths += sorted(Path(self.ws.files).glob("*.csv"))
        except OSError:
            pass
        try:
            paths += sorted(Path(self.ws.music).iterdir())
        except OSError:
            pass
        seen = []
        for path in paths:
            try:
                info = path.stat()
            except OSError:
                continue
            seen.append((str(path.relative_to(root)), info.st_size, info.st_mtime_ns))
        return tuple(seen)

    def tick(self) -> "dict | None":
        """One look for edits: an `edit` generation once the files have
        not changed for quiet_s since their last change and differ from
        what they were at the last generation. Returns the generation, if
        one was taken."""
        now = self._clock()
        seen = self._fingerprint()
        if seen != self._seen:
            self._seen = seen
            self._changed_at = now
        if self._baseline is None:
            self._baseline = seen           # nothing to compare with yet
            return None
        if self._is_busy():
            # The show is running: nothing written now. `_changed_at`
            # stays, so the generation is taken as soon as it has ended.
            return None
        if (seen != self._baseline and self._changed_at is not None
                and now - self._changed_at >= self.quiet_s):
            # Restarted first: a generation that fails (a full disk) is
            # tried again after another quiet_s, not on every tick.
            self._changed_at = now
            job = self.request("edit")
            return job.result
        return None

    def _is_busy(self) -> bool:
        try:
            return bool(self._busy())
        except Exception:                       # noqa: BLE001 - never stops the worker
            return False

    def settle(self) -> None:
        """The workspace as it is now is accounted for (a restore just put
        a generation back): no `edit` generation for it."""
        seen = self._fingerprint()
        self._seen = self._baseline = seen
        self._changed_at = self._clock()

    # ---- the index ----

    def _index_path(self) -> Path:
        return self.folder / INDEX_NAME

    def load(self) -> None:
        """Read index.json and make it agree with the folder: stray temp
        files (a power cut mid-write) deleted, entries whose tar is gone
        dropped, a tar the index does not know read in full and adopted -
        or deleted when it does not read. Safe to call again."""
        with self._lock:
            index = {"version": INDEX_VERSION, "seq": 0, "uploaded": None,
                     "generations": []}
            try:
                raw = json.loads(self._index_path().read_text(encoding="utf-8"))
                if isinstance(raw, dict) and isinstance(raw.get("generations"), list):
                    index.update({k: raw[k] for k in ("seq", "uploaded", "generations")
                                  if k in raw})
            except FileNotFoundError:
                pass
            except (OSError, ValueError) as exc:
                self._fail(f"{INDEX_NAME} unreadable ({exc}) - rebuilt from the folder")
            if not isinstance(index.get("seq"), int):
                index["seq"] = 0
            records = [r for r in index["generations"]
                       if isinstance(r, dict) and isinstance(r.get("name"), str)
                       and _NAME.match(r["name"])]
            on_disk = {}
            if self.folder.is_dir():
                for path in self.folder.iterdir():
                    if path.name.endswith(_TEMP_ENDINGS) and path.name.startswith("."):
                        try:
                            path.unlink()
                            self._log(f"deleted {path.name} (left by an interrupted write)")
                        except OSError:
                            pass
                    elif _NAME.match(path.name) and path.is_file():
                        on_disk[path.name] = path
            changed = False
            kept = []
            for record in records:
                path = on_disk.pop(record["name"], None)
                if path is None:
                    changed = True
                    continue
                if (path.stat().st_size != record.get("size")
                        or not isinstance(record.get("revision"), str)
                        or not record["revision"]):
                    # A size that moved, or an entry missing what a restore
                    # compares by: read the tar again rather than trust it.
                    try:
                        record.update(inspect_tar(path), size=path.stat().st_size)
                    except ValueError as exc:
                        self._drop_file(path, exc)
                        changed = True
                        continue
                    changed = True
                kept.append(record)
            adopted = []
            for name, path in sorted(on_disk.items()):
                try:
                    found = inspect_tar(path)
                except ValueError as exc:
                    self._drop_file(path, exc)
                    continue
                match = _NAME.match(name)
                adopted.append({"name": name, "at": _at_of(name),
                                "reason": match.group(3), "revision": found["revision"],
                                "cues": found["cues"], "size": path.stat().st_size})
                changed = True
            if adopted:
                # Written but never indexed (the power went between the
                # rename and the index): ordered among the rest by when
                # the files were written.
                everything = kept + adopted
                everything.sort(key=lambda r: (self.folder / r["name"]).stat().st_mtime)
                for seq, record in enumerate(everything, start=1):
                    record["seq"] = seq
                kept = everything
                index["seq"] = max(index["seq"], len(everything))
            kept.sort(key=lambda r: r.get("seq", 0))
            index["generations"] = kept
            names = {r["name"] for r in kept}
            uploaded = index.get("uploaded")
            if not (isinstance(uploaded, dict) and uploaded.get("name") in names):
                if uploaded is not None:
                    changed = True
                index["uploaded"] = None
            self._index = index
            if changed and self.folder.is_dir():
                self._write_index()

    def _drop_file(self, path: Path, why) -> None:
        try:
            path.unlink()
        except OSError:
            pass
        self._log(f"deleted {path.name}: it does not read whole ({why})")

    def _loaded(self) -> dict:
        if self._index is None:
            self.load()
        return self._index

    def _write_index(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        durable.atomic_write_json(self._index_path(), self._index, indent=1)

    def _newest(self) -> "dict | None":
        generations = self._loaded()["generations"]
        return generations[-1] if generations else None

    # ---- taking a generation ----

    def _damaged(self) -> "str | None":
        """Why the workspace as it is must not become a generation - a
        show.json or history.json that does not parse (a power cut in the
        middle of a write) - or None. Backing that up would only push a
        good generation out."""
        for name in ("show.json", "history.json"):
            path = Path(self.ws.root) / name
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                continue
            except (OSError, ValueError) as exc:
                return f"{name} does not parse ({exc})"
        return None

    def _estimate(self) -> int:
        """About how big the next tar is: what it would hold, plus the
        tar's own headers and padding."""
        return sum(size for _, size, _ in self._fingerprint()) + 64 * 1024

    def _empty(self) -> bool:
        root = Path(self.ws.root)
        return (not (root / "show.json").is_file()
                and not any(Path(self.ws.files).glob("*.csv")))

    def _snapshot(self, reason: str, upload: "dict | None", protect) -> "dict | None":
        # One generation at a time (_write_lock, held throughout); the
        # index lock only around the index itself, so GET /api/backups -
        # the LCD's 3 s request - never waits for a 30 MB tar.
        with self._write_lock:
            return self._snapshot_locked(reason, upload, protect)

    def _snapshot_locked(self, reason: str, upload: "dict | None",
                         protect) -> "dict | None":
        seen = self._fingerprint()
        if reason in ("edit", "start") and self._is_busy():
            if reason == "start":
                # Not lost: a baseline nothing matches makes the first
                # quiet tick after the run take it (as an `edit`).
                self._baseline = ()
                self._seen = seen
                self._changed_at = self._clock()
            raise _NotTaken("the show is running - taken once it has ended")
        damaged = self._damaged()
        if damaged:
            self._baseline = seen           # not again until it changes
            self._fail(f"{reason}: not backed up - {damaged}")
            raise _NotTaken(f"the workspace is damaged - {damaged}")
        if reason == "start" and self._empty():
            self._baseline = seen
            return None
        with self._lock:
            index = self._loaded()
            newest = self._newest()
        rev_before = self.ws.revision()
        current = content_revision(self.ws)
        if newest is not None and newest["revision"] == current:
            self._baseline = seen
            if upload is not None:
                with self._lock:
                    self._mark_upload(newest, upload, rev_before)
                    self._write_index()
            return dict(newest, skipped=True)
        # The tar itself, with no index lock held.
        self.folder.mkdir(parents=True, exist_ok=True)
        need = self._estimate()
        try:
            free = shutil.disk_usage(str(self.folder)).free
        except OSError:
            free = None
        if free is not None and free - need < self.min_free:
            why = (f"not enough free space ({free // (1024 * 1024)} MB free, "
                   f"{need // (1024 * 1024) + 1} MB needed, "
                   f"{self.min_free // (1024 * 1024)} MB must stay free)")
            self._fail(f"{reason}: not backed up - {why}")
            raise _NotTaken(why)
        stamp = datetime.datetime.fromtimestamp(self._wall())
        scratch = self.folder / (f".{stamp:%Y%m%d-%H%M%S}-{reason}-"
                                 f"{os.getpid()}{TEMP_SUFFIX}")
        try:
            with open(scratch, "wb") as out:
                self.ws.export_tar(out)
                durable.fsync_file(out)
            found = inspect_tar(scratch)
            rev_after = self.ws.revision()
            if newest is not None and newest["revision"] == found["revision"]:
                scratch.unlink()
                self._baseline = seen
                if upload is not None:
                    with self._lock:
                        self._mark_upload(newest, upload, rev_before, rev_after)
                        self._write_index()
                return dict(newest, skipped=True)
            name = self._free_name(stamp, reason, found["revision"])
            os.replace(str(scratch), str(self.folder / name))
            durable.fsync_dir(self.folder)
        except BaseException:
            try:
                scratch.unlink()
            except OSError:
                pass
            raise
        size = (self.folder / name).stat().st_size
        with self._lock:
            index["seq"] = int(index.get("seq") or 0) + 1
            record = {"name": name, "seq": index["seq"], "at": _at_of(name),
                      "reason": reason, "revision": found["revision"],
                      "cues": found["cues"], "size": size}
            index["generations"].append(record)
            if upload is not None:
                self._mark_upload(record, upload, rev_before, rev_after)
            self._prune(set(protect or ()))
            self._write_index()
        self._baseline = seen
        self.problem = None
        self._log(f"{name} ({record['cues']} cues, {size / (1024 * 1024):.1f} MB)")
        return dict(record)

    def _free_name(self, stamp: datetime.datetime, reason: str, revision: str) -> str:
        """The file name, a second later while one of that name exists
        (the clock of a unit with no RTC can come back behind itself)."""
        for step in range(3600):
            moment = stamp + datetime.timedelta(seconds=step)
            name = f"{moment:%Y%m%d-%H%M%S}-{reason}-{revision[:8]}.tar"
            if not (self.folder / name).exists():
                return name
        raise OSError("no free backup name")

    def _mark_upload(self, record: dict, upload: dict, rev_before: str,
                     rev_after: "str | None" = None) -> None:
        """The units now hold what `upload` carried. This generation is
        theirs only when the workspace was exactly that while it was
        written (an edit landed between the Upload and the tar: then no
        generation is - and the old mark is wrong too, so it goes)."""
        index = self._index
        rev_after = rev_before if rev_after is None else rev_after
        if upload.get("rev") == rev_before == rev_after:
            index["uploaded"] = {"name": record["name"],
                                 "revision": record["revision"],
                                 "whole": bool(upload.get("whole")),
                                 "at": record["at"]}
        else:
            index["uploaded"] = None

    def _prune(self, protect: "set[str]") -> None:
        """Keep the newest `keep`, the uploaded one and `protect` (a
        restore's target); the index is written by the caller BEFORE the
        files go, so a power cut in between leaves tars the next start
        adopts as the oldest - never an index naming a missing file."""
        index = self._index
        generations = index["generations"]
        uploaded = (index.get("uploaded") or {}).get("name")
        newest = {r["name"] for r in generations[-self.keep:]}
        keep = newest | {uploaded} | protect
        gone = [r for r in generations if r["name"] not in keep]
        if not gone:
            return
        index["generations"] = [r for r in generations if r["name"] in keep]
        self._write_index()
        for record in gone:
            try:
                (self.folder / record["name"]).unlink()
            except OSError:
                pass

    # ---- reading ----

    def listing(self) -> dict:
        """GET /api/backups: the generations newest first."""
        with self._lock:
            index = self._loaded()
            uploaded = (index.get("uploaded") or {}).get("name")
            rows = [{"name": r["name"], "at": r.get("at") or _at_of(r["name"]),
                     "reason": r.get("reason"), "revision": r.get("revision"),
                     "uploaded": r["name"] == uploaded, "cues": r.get("cues"),
                     "size": r.get("size")}
                    for r in reversed(index["generations"])]
            return {"enabled": True, "folder": str(self.folder), "keep": self.keep,
                    "uploaded": uploaded, "problem": self.problem, "backups": rows}

    def find(self, name) -> dict:
        """The generation called `name`, or KeyError."""
        if not isinstance(name, str) or not _NAME.match(name):
            raise KeyError(name)
        with self._lock:
            for record in self._loaded()["generations"]:
                if record["name"] == name:
                    return dict(record)
        raise KeyError(name)

    def path(self, name: str) -> Path:
        return self.folder / self.find(name)["name"]

    def holds_upload(self, record: dict) -> bool:
        """Is `record` what the last Upload put on EVERY unit of the
        timeline (the same content revision as the marked generation, and
        that Upload was whole)?"""
        with self._lock:
            uploaded = self._loaded().get("uploaded")
        return (isinstance(uploaded, dict) and bool(uploaded.get("whole"))
                and uploaded.get("revision") == record.get("revision"))
