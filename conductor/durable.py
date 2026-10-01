"""Writes that survive a power cut: a file is either the old one or the
new one afterwards, never empty and never half of each.

Why this exists: radxa-05 (Debian 11, ext4 rw,relatime on the SD/eMMC)
lost its power twice on 2026-10-01 and came back with
`EXT4-fs (mmcblk0p3): recovery complete`. A plain write (or a write to
a scratch file renamed over the old one) sits in the page cache for up
to ~30 s under ext4's delayed allocation; a cut in that window can bring
the file back zero-length or old. So every persistent write goes:

    temp file in the SAME directory -> write -> flush -> fsync(file)
    -> chmod (the old file's mode, or the one asked for) -> os.replace()
    -> fsync(directory)

The directory fsync is what makes the rename itself durable; it is POSIX
only (Windows cannot open a directory that way - skipped silently, NTFS
journals the rename on its own).

Standard library only and importing nothing else of the package, so the
unit UI (ui/, Python 3.9 on the units) can use it as
`from conductor.durable import ...` without pulling in the Conductor.
The functions call `os.fsync` / `os.replace` through the `os` module at
call time, so a test can monkeypatch them.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import threading
import time
from pathlib import Path

# os.name, read through a module global so a test can pretend to be on
# POSIX (the directory fsync) while it runs on the Windows PC.
_POSIX = os.name == "posix"

_O_BINARY = getattr(os, "O_BINARY", 0)          # Windows: no CRLF games


def fsync_dir(path) -> None:
    """Make a directory's entries (a rename, a new file) durable. POSIX
    only; anything that will not open or sync is ignored - the data
    itself was already fsynced, this is the belt to its braces."""
    if not _POSIX:
        return
    try:
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def fsync_file(handle) -> None:
    """flush() + fsync() an open file object - for a writer that streams
    a large file itself (the music upload, a tar member) and only needs
    the one sync at its end."""
    handle.flush()
    os.fsync(handle.fileno())


def _existing_mode(path: Path) -> "int | None":
    try:
        return os.stat(str(path)).st_mode & 0o777
    except OSError:
        return None


def _open_temp(path: Path) -> "tuple[int, Path]":
    """A fresh temp file beside `path`, created with 0666 & ~umask (what a
    plain open(path, "w") would have made - unlike mkstemp's 0600)."""
    for _ in range(100):
        scratch = path.with_name(
            f".{path.name}.{os.getpid()}-{threading.get_ident()}-"
            f"{secrets.token_hex(4)}.tmp")
        try:
            fd = os.open(str(scratch),
                         os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY, 0o666)
        except FileExistsError:
            continue
        return fd, scratch
    raise FileExistsError(f"no free temp name beside {path}")


_REPLACE_TRIES = 20            # Windows only: x 10 ms, see _replace()


def _replace(scratch: Path, path: Path) -> None:
    """os.replace(), which on POSIX never fails because a reader has the
    target open - but on Windows (the PC's Conductor, the tests) does,
    with PermissionError, for as long as any reader holds it: a page poll,
    a virus scanner, the search indexer. Tried again for ~0.2 s there."""
    for attempt in range(_REPLACE_TRIES):
        try:
            os.replace(str(scratch), str(path))
            return
        except PermissionError:
            if _POSIX or attempt == _REPLACE_TRIES - 1:
                raise
            time.sleep(0.01)


def atomic_write_bytes(path, data: bytes, mode: "int | None" = None) -> None:
    """Replace `path` with `data`, power-loss safe (see the module doc).

    `mode`: the permission bits to give the file; None keeps the mode of
    the file being replaced (fleet.json's 0600 stays 0600), or the
    process's default (0666 & ~umask) for a new file. On any failure
    before the replace the temp file is removed and the old file is
    untouched."""
    path = Path(path)
    if mode is None:
        mode = _existing_mode(path)
    fd, scratch = _open_temp(path)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            fsync_file(handle)
        if mode is not None:
            os.chmod(str(scratch), mode)
        _replace(scratch, path)
    except BaseException:
        try:
            os.unlink(str(scratch))
        except OSError:
            pass
        raise
    fsync_dir(path.parent)


def atomic_write_text(path, text: str, encoding: str = "utf-8",
                      newline: "str | None" = None,
                      mode: "int | None" = None) -> None:
    """atomic_write_bytes() for text. `newline` as open()'s: None writes
    "\\n" as os.linesep (what Path.write_text() did), "" or "\\n" writes
    it untranslated, anything else replaces it."""
    if newline is None:
        newline = os.linesep
    if newline not in ("", "\n"):
        text = text.replace("\n", newline)
    atomic_write_bytes(path, text.encode(encoding), mode=mode)


def atomic_write_json(path, payload, *, indent=None, sort_keys: bool = False,
                      ensure_ascii: bool = True, trailing_newline: bool = False,
                      encoding: str = "utf-8", mode: "int | None" = None) -> None:
    """json.dumps(payload) written with atomic_write_text()."""
    text = json.dumps(payload, indent=indent, sort_keys=sort_keys,
                      ensure_ascii=ensure_ascii)
    if trailing_newline:
        text += "\n"
    atomic_write_text(path, text, encoding=encoding, mode=mode)


def fsync_tree(path) -> None:
    """fsync every file under `path`, then every directory bottom-up
    (`path` itself last) - for a folder that was just populated and is
    about to be renamed into place (the workspace import). A file that
    will not sync on POSIX raises; on Windows (no fsync of a read-only
    handle, no directory fsync) whatever will not open is skipped."""
    root = Path(path)
    for here, dirs, files in os.walk(str(root), topdown=False):
        for name in files:
            full = os.path.join(here, name)
            if _POSIX:
                fd = os.open(full, os.O_RDONLY)
            else:
                try:
                    fd = os.open(full, os.O_RDWR | _O_BINARY)
                except OSError:
                    continue
            try:
                os.fsync(fd)
            except OSError:
                if _POSIX:
                    raise
            finally:
                os.close(fd)
        fsync_dir(here)


# ---- reading back what an older version may have left torn ----

_noted: "set[tuple]" = set()
_noted_lock = threading.Lock()


def note_unreadable(path, exc, what: str = "treated as no record") -> None:
    """One log line (stderr -> the journal) for a state file that is
    there but does not parse - an empty or torn file from a power cut
    before this module existed. Once per file version (path, size,
    mtime), so a reader on a poll path does not repeat it every second."""
    try:
        st = os.stat(str(path))
        key = (str(path), st.st_size, st.st_mtime_ns)
        size = st.st_size
    except OSError:
        key, size = (str(path), None, None), None
    with _noted_lock:
        if key in _noted:
            return
        _noted.add(key)
    shown = "empty" if size == 0 else f"{size} bytes"
    print(f"{path}: unreadable ({shown}: {exc}) - {what}",
          file=sys.stderr, flush=True)
