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
                    usual 192.168.50.1NN:8787 (conductor/fleet.py)

Undo covers show.json - the timeline, the show's length and the unit
assignments - and is kept on disk, so it survives a reload of the page
and a restart of the server. Adding or deleting a CSV is not an edit of
the show and is not undone (the delete asks first).
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import showfile, timeline
from .fleet import DEFAULT_LEAD_S, Fleet, default_units
from .look import (PALETTE, Design, LookError, LookMap, check,
                   compile_design, unit_board_ids)

WEB_DIR = Path(__file__).resolve().parent / "web"
UNITS = [f"radxa-{n:02d}" for n in range(1, 11)]
MAX_UPLOAD = 8 * 1024 * 1024
HISTORY_DEPTH = 200
NUMBER_BRAND = 0x03        # the device type the units' UI sends (ui/patterns.py)
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._\- ]")
_IS_MAP = re.compile(r"_map$", re.IGNORECASE)
_IS_GRID = re.compile(r"_color_pattern\s*\d+", re.IGNORECASE)


def _key(position) -> str:
    return "|".join(str(part) for part in position)


class Workspace:
    """The folder behind the UI. Every method is safe to call from the
    server's request threads."""

    def __init__(self, root):
        self.root = Path(root)
        self.files = self.root / "files"
        self.files.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

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

    # ---- files ----

    @staticmethod
    def kind(name: str) -> "str | None":
        stem = Path(name).stem
        if not name.lower().endswith(".csv"):
            return None
        if _IS_GRID.search(stem):
            return "grid"
        if _IS_MAP.search(stem):
            return "map"
        return None

    def save(self, name: str, text: str) -> str:
        name = _SAFE_NAME.sub("_", Path(name).name)
        if self.kind(name) is None:
            raise ValueError(f"{name}: not a *_map.csv or "
                             "*_color_patternNN_grid.csv")
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
            assigned = self._load_show().get("units", {})
        maps: "dict[str, LookMap]" = {}
        for path in paths:
            if self.kind(path.name) == "map":
                try:
                    look_map = LookMap.from_csv(path)
                except (OSError, LookError):
                    continue            # reported where the item is chosen
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
                design = Design.from_csv(self.files / Path(design_name).name)
                on_unit = [m for key, m in maps.items()
                           if assigned.get(m.item) == unit]
                ids = unit_board_ids(on_unit)
                partial = bool(check(look_map, design))
                arrays = compile_design(look_map, design, partial=partial,
                                        ids=ids)
            except (OSError, LookError) as exc:
                problems.append(f"{item}: {exc}")
                continue
            name = (f"P{design.pattern:02d}" if design.pattern is not None
                    else design.name)
            payload = payloads.setdefault(unit, {
                "cue": cue, "label": "", "dev_type": NUMBER_BRAND,
                "boards": {}})
            payload["label"] = (payload["label"] + " + " if payload["label"]
                                else "") + f"{look_map.item} {name}"
            payload["boards"].update({str(address): array.hex()
                                      for address, array in arrays.items()})
        return payloads, problems

    def compile_show(self) -> "tuple[dict[str, dict], list[str]]":
        """The whole timeline -> ({unit: show file}, problems)."""
        with self._lock:
            paths = sorted(self.files.glob("*.csv"))
            show = self._load_show()
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
            maps[(look_map.item or path.stem).lower()] = look_map
        designs: "dict[str, Design]" = {}
        for path in paths:
            if self.kind(path.name) == "grid":
                try:
                    designs[path.name] = Design.from_csv(path)
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
            show = self._load_show()
            history = self._load_history()
            assigned = show.get("units", {})
        maps: "dict[str, LookMap]" = {}
        items: "dict[str, dict]" = {}

        def item_entry(name: str) -> dict:
            return items.setdefault(name.lower(), {
                "item": name, "unit": assigned.get(name), "map": None,
                "designs": [], "problems": []})

        for path in paths:
            if self.kind(path.name) != "map":
                continue
            try:
                look_map = LookMap.from_csv(path)
            except (OSError, LookError) as exc:
                entry = item_entry(Path(path.stem[:-4]).name)
                entry["map"] = {"name": path.name, "scales": [], "sides": []}
                entry["problems"] += getattr(exc, "problems", [str(exc)])
                continue
            entry = item_entry(look_map.item or path.stem)
            maps[entry["item"].lower()] = look_map
            entry["map"] = {
                "name": path.name, "sides": look_map.sides,
                "warnings": look_map.warnings,
                "scales": [[s.side, s.row, s.col, s.board_no, s.socket]
                           for s in look_map.scales]}

        orphans = []
        for path in paths:
            if self.kind(path.name) != "grid":
                continue
            try:
                design = Design.from_csv(path)
                problems = []
            except (OSError, LookError) as exc:     # deleted meanwhile, too
                design, problems = None, getattr(exc, "problems", [str(exc)])
            item = (design.item if design else None) or path.stem
            look_map = maps.get(item.lower())
            record = {"name": path.name,
                      "pattern": design.pattern if design else None,
                      # problems: as a full cue. partial_problems: as a
                      # cue that leaves uncoloured scales as they are - a
                      # design that only passes that way is a partial one,
                      # not a broken one.
                      "problems": problems, "partial_problems": problems,
                      "colors": {}, "shifts": {}, "undecided": []}
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
                if ids is None:
                    items[key]["problems"] += unit_problems[group]

        ordered = sorted(items.values(),
                         key=lambda e: (e["unit"] or "~", e["item"].lower()))
        for entry in ordered:
            entry["designs"].sort(key=lambda d: (d["pattern"] or 0, d["name"]))
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
        cue_problems, warnings = timeline.validate(cues, facts, duration,
                                                   refresh)
        for cue in cues:
            cue["sent"], cue["complete"] = timeline.times(cue, refresh)
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
                "palette": [{"name": n, "rgb": list(rgb)} for n, rgb in PALETTE],
                "workspace": str(self.root.resolve())}


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
        try:
            if path == "/api/state":
                return self._json(self.workspace.state())
            if path == "/api/fleet":
                if self.fleet is None:
                    return self._json({"units": [], "last_fire": None})
                with self.prepared_lock:
                    prepared = dict(self.prepared)
                return self._json(dict(self.fleet.snapshot(),
                                       prepared=prepared))
        except Exception as exc:        # noqa: BLE001 - a poll must get JSON
            return self._json({"error": f"{exc.__class__.__name__}: {exc}"},
                              status=500)
        if path in ("/", "/index.html"):
            page = (WEB_DIR / "index.html").read_bytes()
            return self._send(200, page, "text/html; charset=utf-8")
        self._send(404, b"not found", "text/plain")

    def do_POST(self):
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
            if self.path == "/api/assign":
                self.workspace.assign(body["item"], body.get("unit") or None)
                return self._json({"ok": True})
            if self.path == "/api/show":
                self.workspace.set_timeline(body.get("duration", 600),
                                            body.get("cues", []),
                                            body.get("refresh_s"))
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
        except (ValueError, KeyError) as exc:
            return self._json({"error": str(exc)}, status=400)
        self._send(404, b"not found", "text/plain")


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
            shows, problems = self.workspace.compile_show()
            results = fleet.upload(shows) if shows else {}
            return self._json({"units": results, "problems": problems,
                               "shows": {u: s["id"] for u, s in shows.items()}})
        if command == "preset":
            return self._json({"units": fleet.simple(fleet._targets(),
                                                     "/show/preset")})
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
                return self._json({"units": fleet.start_show(lead),
                                   "lead_s": lead})
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
