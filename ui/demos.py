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


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "demo"


class DemoStore:
    def __init__(self, root: "Path | None" = None):
        self.root = Path(root) if root is not None else STORE / "demos"

    # ---- disk ----

    def _path(self, slug: str) -> Path:
        return self.root / f"{slug}.json"

    def _write(self, path: Path, payload: dict) -> None:
        """Whole or not at all - the same rule as showplay._write."""
        self.root.mkdir(parents=True, exist_ok=True)
        scratch = path.with_name(path.name + ".tmp")
        scratch.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(scratch, path)

    def _entries(self) -> "list[dict]":
        entries = []
        if not self.root.is_dir():
            return entries
        for path in sorted(self.root.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue          # a half-written or foreign file: skip it
            if isinstance(data, dict) and data.get("slug"):
                entries.append(data)
        return entries

    # ---- commands ----

    def save(self, name: str, show: dict, loop: bool = False) -> str:
        name = str(name or "").strip()
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
        self._write(self._path(slug), {
            "slug": slug, "name": name, "loop": bool(loop),
            "saved_at": time.time(), "show": show})
        return slug

    def delete(self, slug: str) -> None:
        path = self._path(str(slug))
        if path.is_file():
            path.unlink()

    def load(self, slug: str) -> dict:
        try:
            data = json.loads(self._path(str(slug)).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RemoteError(f"no such demo: {slug}") from exc
        return data["show"]

    # ---- what the PC and the LCD read ----

    def list(self) -> "list[dict]":
        out = []
        for data in self._entries():
            show = data.get("show") or {}
            out.append({
                "slug": data["slug"], "name": data.get("name", ""),
                "cues": len(show.get("cues") or []),
                "duration": show.get("duration"),
                "loop": bool(data.get("loop")),
                "saved_at": data.get("saved_at")})
        out.sort(key=lambda d: d["saved_at"] or 0)
        return out
