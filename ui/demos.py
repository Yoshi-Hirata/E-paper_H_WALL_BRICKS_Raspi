"""Standalone demos: a show written into the unit's own menu.

The show PC compiles one file per unit for every show it runs
(conductor/showfile.py's build_unit_show output - the same file
`/show/load` already accepts). Naming it and writing it here turns it
into a menu row: KEY1 plays it on the unit's own clock, no PC required
(ui/app.py's DEMO screen, ui/showplay.py's ShowPlayer un-changed - a
demo runs exactly like a PC-driven show once it is loaded).

One JSON file per demo under STORE/"demos" (STORE = ui.showplay.STORE,
the same ~/.epaper the running show is kept in), so a `git pull` or a
reboot never touches what has been written. The slug is the file name
and the menu row's key; a name is free to repeat words, so two demos
named alike get "-2", "-3", ... appended - but writing the *same* name
again updates that one demo in place (the operator renaming nothing,
just re-sending the timeline).

Each demo is TWO files: "<slug>.json" (the wrapper + the full show -
cues x boards x hex arrays, 100-300 KB) and "<slug>.meta.json" (just
what a menu row or /status needs: name, cues, duration, loop, saved_at,
show_id - a few dozen bytes). list()/_entries() read only the sidecars,
because they are called from Agent.status() on every poll and every
POST answer, and from the LCD's own refresh_demos() every couple of
seconds - parsing every full show file that often, in the very process
that is also timing cue fires, would stall the GIL for hundreds of ms
per show stored. load(), which actually needs the boards, is called
once per KEY1/KEY1-hold/loop lap, never on a poll path.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from .remote import RemoteError
from .showplay import STORE, validate_show

MAX_DEMOS = 20
MAX_NAME_LEN = 14          # the LCD's menu row width, enforced here too
_SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]*")


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "demo"


class DemoStore:
    def __init__(self, root: "Path | None" = None):
        self.root = Path(root) if root is not None else STORE / "demos"

    # ---- disk ----

    def _path(self, slug: str) -> Path:
        """The full show file for a slug - also where every public
        method's slug comes in from the outside (an HTTP body), so this
        is the one place a bad slug ("../show-run", "", "..") is turned
        into a refusal instead of a path outside `self.root`."""
        slug = str(slug or "")
        if not _SLUG_RE.fullmatch(slug):
            raise RemoteError(f"no such demo: {slug}")
        return self.root / f"{slug}.json"

    def _meta_path(self, slug: str) -> Path:
        return self.root / f"{slug}.meta.json"       # slug already validated

    def _write(self, path: Path, payload: dict) -> None:
        """Whole or not at all - the same rule as showplay._write. On a
        write failure (a full SD card) the half-written scratch file is
        removed rather than left behind for the next listing to trip on."""
        self.root.mkdir(parents=True, exist_ok=True)
        scratch = path.with_name(path.name + ".tmp")
        try:
            scratch.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(scratch, path)
        except OSError:
            try:
                scratch.unlink()
            except OSError:
                pass
            raise

    def _meta_of(self, slug: str, name: str, show: dict, loop: bool,
                saved_at: float) -> dict:
        return {"slug": slug, "name": name,
                "cues": len(show.get("cues") or []),
                "duration": show.get("duration"), "loop": bool(loop),
                "saved_at": saved_at, "show_id": show.get("id")}

    def _rebuild_meta(self, slug: str) -> "dict | None":
        """A "<slug>.json" whose sidecar is missing (an older write, or
        one that lost it) is re-derived once rather than left invisible
        to the menu and to /demo/list."""
        try:
            data = json.loads(self._path(slug).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        show = data.get("show")
        if not isinstance(data, dict) or not isinstance(show, dict):
            return None
        meta = self._meta_of(slug, data.get("name", slug), show,
                             bool(data.get("loop")), data.get("saved_at"))
        try:
            self._write(self._meta_path(slug), meta)
        except OSError:
            pass                    # still returned below for this listing
        return meta

    def _entries(self) -> "list[dict]":
        entries, known = [], set()
        if not self.root.is_dir():
            return entries
        for path in sorted(self.root.glob("*.meta.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue          # a half-written or foreign file: skip it
            if isinstance(data, dict) and data.get("slug"):
                entries.append(data)
                known.add(data["slug"])
        for path in sorted(self.root.glob("*.json")):
            if path.name.endswith(".meta.json"):
                continue
            slug = path.stem
            if slug in known:
                continue
            entry = self._rebuild_meta(slug)
            if entry is not None:
                entries.append(entry)
        return entries

    # ---- commands ----

    def save(self, name: str, show: dict, loop: bool = False) -> str:
        name = str(name or "").strip()[:MAX_NAME_LEN]
        if not name:
            raise RemoteError("a demo needs a name")
        validate_show(show)
        existing = {e["slug"]: e for e in self._entries()}
        slug = slugify(name)
        if slug in existing and existing[slug].get("name") != name:
            n = 2
            while f"{slug}-{n}" in existing:
                n += 1
            slug = f"{slug}-{n}"
        if slug not in existing and len(existing) >= MAX_DEMOS:
            raise RemoteError("demo store full - delete one first")
        saved_at = time.time()
        try:
            self._write(self._path(slug), {
                "slug": slug, "name": name, "loop": bool(loop),
                "saved_at": saved_at, "show": show})
            self._write(self._meta_path(slug),
                       self._meta_of(slug, name, show, loop, saved_at))
        except OSError as exc:
            raise RemoteError(f"could not write the demo: {exc}") from exc
        return slug

    def delete(self, slug: str) -> None:
        path = self._path(slug)
        if not path.is_file():
            raise RemoteError(f"no such demo: {slug}")
        path.unlink()
        meta = self._meta_path(slug)
        if meta.is_file():
            meta.unlink()

    def load(self, slug: str) -> dict:
        path = self._path(slug)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data["show"]
        except (OSError, ValueError) as exc:
            raise RemoteError(f"no such demo: {slug}") from exc
        except (KeyError, TypeError) as exc:
            raise RemoteError(f"demo {slug} has no show") from exc

    # ---- what the PC and the LCD read ----

    def list(self) -> "list[dict]":
        out = [{"slug": e["slug"], "name": e.get("name", ""),
               "cues": e.get("cues", 0), "duration": e.get("duration"),
               "loop": bool(e.get("loop")), "saved_at": e.get("saved_at"),
               "show_id": e.get("show_id")} for e in self._entries()]
        out.sort(key=lambda d: d["saved_at"] or 0)
        return out
