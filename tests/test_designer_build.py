"""Tests for the designers' simulator build (Coder Q, plan_designer_sim.md).

Some of these are gated on inputs another coder owns and may not exist yet
mid-round (tests/fixtures/sim/bundle_v1.json from Coder P, showdata/files/
which is gitignored and machine-local) - they skip rather than fail then, so
this file is safe to run before those land, and starts actually checking
things the moment they do.
"""
import json
import os
import re
import shutil
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
INDEX_HTML = REPO / "conductor" / "web" / "index.html"
DESIGNER_HTML = REPO / "conductor" / "web" / "designer.html"
DIST = REPO / "dist" / "az27ss-simulator.html"
BUILD_SCRIPT = REPO / "tools" / "build_designer.py"
STARTER_SCRIPT = REPO / "tools" / "make_starter.py"

sys.path.insert(0, str(REPO))
import tools.build_designer as build_designer  # noqa: E402


def run(*args):
    return subprocess.run([sys.executable, *args], cwd=REPO, capture_output=True, text=True)


def test_build_is_current():
    # Plain --check, no flags (adversarial review round 2's third pass -
    # N1): the shipped variant (no goldens.js/selftest.js - Python-cross-
    # check data no designer's double-click needs, over half the page's
    # weight) is now build_designer.py's DEFAULT, exactly because the
    # committed dist/az27ss-simulator.html IS that variant - a --check that
    # needed a flag to agree with it would report it stale, and its own
    # "run `python tools/build_designer.py`" advice would then silently
    # overwrite the committed 550 KB dist with the 1.1 MB dev/CI one.
    result = run(str(BUILD_SCRIPT), "--check")
    assert result.returncode == 0, result.stdout + result.stderr


def test_built_file_is_self_contained():
    assert DIST.exists(), "dist/az27ss-simulator.html has not been built yet"
    html = DIST.read_text(encoding="utf-8")
    build_designer.check_self_contained(html)          # raises on failure


def test_built_file_is_under_the_size_budget():
    assert DIST.exists()
    assert DIST.stat().st_size <= build_designer.SIZE_BUDGET


CONSTANT_NAMES = ["PITCH", "MARGIN", "JITTER_MAX", "TINT_STEPS", "REFRESH_TINT",
                  "REFRESH_PHASE_A", "REFRESH_PALETTE", "REFRESH_PHASE_C"]


def _extract_constants(text: str, label: str) -> dict:
    # \b on BOTH sides of the name (adversarial review, 2026-09-25: the old
    # pattern had no trailing \b, so e.g. a hypothetical REFRESH_TINT2 = ...
    # would have matched as if it were REFRESH_TINT) and findall, not
    # search - a constant defined twice (a stray leftover copy after an
    # edit, say) used to silently take whichever definition happened to
    # come first instead of failing loudly.
    found = {}
    for name in CONSTANT_NAMES:
        matches = re.findall(rf"\b{name}\b\s*=\s*(\[[^\]]*\]|[0-9.]+)", text)
        assert len(matches) <= 1, (
            f"{label}: {name} is defined {len(matches)} times "
            f"(expected at most one): {matches}")
        if matches:
            found[name] = re.sub(r"\s+", "", matches[0])
    return found


def test_shared_constants_match_index_html():
    index_text = INDEX_HTML.read_text(encoding="utf-8")
    sim_text = "".join((REPO / "conductor" / "web" / "sim" / name).read_text(encoding="utf-8")
                        for name in ("render.js", "flicker.js"))
    index_constants = _extract_constants(index_text, "index.html")
    sim_constants = _extract_constants(sim_text, "render.js/flicker.js")
    assert set(index_constants) == set(CONSTANT_NAMES), \
        f"index.html is missing some of {CONSTANT_NAMES}: found {sorted(index_constants)}"
    assert set(sim_constants) == set(CONSTANT_NAMES), \
        f"render.js/flicker.js are missing some of {CONSTANT_NAMES}: found {sorted(sim_constants)}"
    for name in CONSTANT_NAMES:
        assert index_constants[name] == sim_constants[name], (
            f"{name} drifted: index.html has {index_constants[name]}, "
            f"the sim modules have {sim_constants[name]}")


def test_starter_is_current():
    if not (REPO / "showdata" / "files").is_dir():
        pytest.skip("showdata/ is gitignored and not present on this machine")
    result = run(str(STARTER_SCRIPT), "--check")
    assert result.returncode == 0, result.stdout + result.stderr


def test_bundle_fixture_round_trips_through_the_python_model():
    fixture = REPO / "tests" / "fixtures" / "sim" / "bundle_v1.json"
    if not fixture.exists():
        pytest.skip("tests/fixtures/sim/bundle_v1.json not landed yet (Coder P)")
    bundle = json.loads(fixture.read_text(encoding="utf-8"))
    assert bundle["format"] == "epaper-show-bundle"
    assert bundle["version"] == 1
    show = bundle["show"]
    assert show["format"] == "epaper-show" and show["version"] == 1
    assert "units" not in show, "show must carry no units key (plan §4.2)"
    from conductor import timeline
    raw_cues = show["cues"]
    cues = timeline.clean(raw_cues)
    # The actual cue fields survive the round trip, not just "clean()
    # returned a list" (adversarial review round 2 - F8: isinstance(cues,
    # list) is true of the empty list too, so a clean() that silently
    # dropped every cue would still have passed this).
    assert len(cues) == len(raw_cues)
    by_id = {c["id"]: c for c in cues}
    for raw in raw_cues:
        cleaned = by_id[str(raw["id"])]
        assert cleaned["item"] == raw["item"]
        assert cleaned["design"] == raw["design"]
        assert cleaned["at"] == raw["at"]
        assert cleaned["partial"] == raw["partial"]
        assert cleaned["sequence"] == raw["sequence"]


def test_build_designer_excludes_any_data_stub_script(tmp_path):
    # Exercises build_designer.py's stub-exclusion directly (adversarial
    # review round 2 - F8): designer.html no longer references a stub script
    # at all now that model.js/state.js have landed, so the old version of
    # this test - a loop over designer.html's own <script> tags, asserting
    # only inside an `if "stub" in ...` branch that no iteration ever took -
    # passed vacuously whether or not build_designer.py's guard still
    # worked. This builds a throwaway page that DOES carry a data-stub
    # script and checks it is actually dropped.
    src = tmp_path / "designer.html"
    (tmp_path / "real.js").write_text("globalThis.REAL = 1;\n", encoding="utf-8")
    (tmp_path / "stub.js").write_text("globalThis.STUB = 1;\n", encoding="utf-8")
    src.write_text(
        '<script src="real.js"></script>\n'
        '<script src="stub.js" data-stub="dev-only"></script>\n',
        encoding="utf-8")
    html, _sizes = build_designer.build(src, no_starter=True)
    assert "REAL" in html
    assert "STUB" not in html
    assert "data-stub" not in html


# A static regex scan of designer.html/designer-app.js/designer.css used to
# stand in for this check on its own - it passed cleanly while the rendered
# Transition dropdown still said "Socket order (P01 to P60)" (the label
# comes from state.sequences at runtime, never as a literal string in these
# files), which is exactly the false confidence a "did the word appear in
# the source" test gives (adversarial review round 2 - F4: "widen ... from
# file scanning to a rendered-UI assertion"). Superseded by
# test_banned_vocabulary_never_reaches_rendered_ui below, which scans what a
# designer's browser actually draws - the source files are still worth a
# glance for hardcoded copy, but that glance is now a human reviewer's job,
# not a test that can pass for the wrong reason.


def _find_browser() -> "str | None":
    env = os.environ.get("CONDUCTOR_BROWSER")
    candidates = [env] if env else []
    candidates += [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        shutil.which("msedge"), shutil.which("microsoft-edge"),
        shutil.which("google-chrome"), shutil.which("chrome"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return None


def _dump_dom(url: str, tmp_path: Path) -> str:
    browser = _find_browser()
    if not browser:
        return None
    user_data_dir = tmp_path / "user-data"
    args = [browser, "--headless=new", "--disable-gpu", "--no-sandbox", "--no-first-run",
            f"--user-data-dir={user_data_dir}", "--virtual-time-budget=20000", "--dump-dom", url]
    result = subprocess.run(args, capture_output=True, timeout=60)
    return result.stdout.decode("utf-8", errors="replace")


class _TextAndAttrScanner(HTMLParser):
    """Collects visible text plus title/placeholder/aria-label attribute
    values from a dumped DOM, dropping <script>/<style> content entirely -
    used by test_banned_vocabulary_never_reaches_rendered_ui (F4) to scan
    what a designer actually sees/hovers, not the JS/CSS source alongside
    it."""
    SKIP_TAGS = ("script", "style")
    WATCHED_ATTRS = ("title", "placeholder", "aria-label")

    def __init__(self):
        super().__init__()
        self.chunks = []
        self._skip_tag = None

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP_TAGS and self._skip_tag is None:
            self._skip_tag = tag
        for name, value in attrs:
            if name in self.WATCHED_ATTRS and value:
                self.chunks.append(value)

    def handle_startendtag(self, tag, attrs):
        for name, value in attrs:
            if name in self.WATCHED_ATTRS and value:
                self.chunks.append(value)

    def handle_endtag(self, tag):
        if tag == self._skip_tag:
            self._skip_tag = None

    def handle_data(self, data):
        if self._skip_tag is None:
            self.chunks.append(data)


def test_banned_vocabulary_never_reaches_rendered_ui(tmp_path):
    # The dynamic half of the check above: runs the REAL built page
    # (dist/az27ss-simulator.html, the same artefact a designer double-
    # clicks) with #displaycheck, which drives designer-app.js's own
    # deJargon()/seqLabel() against dirty strings copied verbatim from the
    # conductor/look.py and conductor/timeline.py f-string templates that
    # produce them, and against every real SIM.sequence.LABELS entry - see
    # displayCheck() in designer-app.js for exactly what it checks.
    forced = os.environ.get("CONDUCTOR_BROWSER_TESTS") == "1"
    if not _find_browser():
        if forced:
            pytest.fail("CONDUCTOR_BROWSER_TESTS=1 but no browser was found "
                       "(set $CONDUCTOR_BROWSER to its path)")
        pytest.skip("no Edge/Chrome found - set CONDUCTOR_BROWSER_TESTS=1 to force")
    assert DIST.exists(), "dist/az27ss-simulator.html has not been built yet"
    url = "file:///" + str(DIST.resolve()).replace("\\", "/") + "#displaycheck"
    dom = _dump_dom(url, tmp_path)
    match = re.search(r'data-ok="(true|false)" data-total="(\d+)" data-failed="(\d+)"', dom)
    assert match, f"no #displaycheck-out found in the dumped DOM:\n{dom[:3000] if dom else dom}"
    ok, total, failed = match.group(1), int(match.group(2)), int(match.group(3))
    if ok != "true":
        detail = re.search(r'<pre id="displaycheck-out"[^>]*>(.*?)</pre>', dom, re.S)
        pytest.fail(f"banned vocabulary reached the rendered UI ({failed}/{total}):\n"
                   f"{detail.group(1) if detail else dom[:3000]}")
    assert total > 0

    # The half displayCheck() cannot cover on its own (adversarial review
    # round 2 - F4): it only proves deJargon()/seqLabel() scrub the strings
    # it was handed, not that every place a garment/sequence is named
    # actually calls them. #displaycheck also forces the Timeline tab open
    # against the real starter data (designer-app.js's boot(), see its own
    # comment) - the one screen with the most model-derived text (tracks,
    # cue table, SHORTEST INTERVAL PER ITEM, the transition dropdowns) -
    # so this scans the REST of the dumped DOM (all visible text, plus
    # title/placeholder/aria-label attributes, with every <script>/<style>
    # dropped first) for the same banned words. A reviewer ran this by hand
    # once with zero hits, so it is not expected to be flaky.
    scanner = _TextAndAttrScanner()
    scanner.feed(dom)
    haystack = " ".join(scanner.chunks)
    banned = re.compile(r"\b(unit|units|radxa|bus|buses|board|boards|dip|socket|sockets)\b", re.IGNORECASE)
    hit = banned.search(haystack)
    assert not hit, (
        f"banned vocabulary reached the rendered Timeline tab's DOM: {hit.group(0)!r} "
        f"near {haystack[max(0, hit.start() - 60):hit.start() + 60]!r}")
