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


def _extract_constants(text: str) -> dict:
    found = {}
    for name in CONSTANT_NAMES:
        m = re.search(rf"\b{name}\s*=\s*(\[[^\]]*\]|[0-9.]+)", text)
        if m:
            found[name] = re.sub(r"\s+", "", m.group(1))
    return found


def test_shared_constants_match_index_html():
    index_text = INDEX_HTML.read_text(encoding="utf-8")
    sim_text = "".join((REPO / "conductor" / "web" / "sim" / name).read_text(encoding="utf-8")
                        for name in ("render.js", "flicker.js"))
    index_constants = _extract_constants(index_text)
    sim_constants = _extract_constants(sim_text)
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
    cues = timeline.clean(show["cues"])
    assert isinstance(cues, list)


def test_no_stub_in_designer_html_scripts_that_ship():
    # A cheap guard against forgetting to remove the dev-only stub reference
    # once model.js/state.js land: build_designer.py already refuses to
    # inline anything carrying data-stub, but this makes the intent explicit
    # and catches a stray second stub reference without one.
    html = DESIGNER_HTML.read_text(encoding="utf-8")
    for m in re.finditer(r'<script\b[^>]*\bsrc="([^"]+)"[^>]*>', html):
        if "stub" in m.group(1).lower():
            assert 'data-stub="dev-only"' in m.group(0), (
                f"{m.group(1)} looks like a stub but has no data-stub marker - "
                "build_designer.py would ship it")


def _strip_comments(text: str, html: bool) -> str:
    if html:
        return re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"(?m)//.*$", "", text)
    # displayCheck()'s own `dirty` fixture array (designer-app.js) is a set
    # of DELIBERATELY dirty strings copied from the Python templates, fed
    # straight into deJargon() to prove it scrubs them - never displayed
    # verbatim, so they are not "vocabulary reaching the UI" and would
    # otherwise be a permanent false positive here.
    text = re.sub(r"const dirty = \[.*?\];", "const dirty = [];", text, flags=re.DOTALL)
    # displayCheck()'s own banned-word regex literal - the pattern it tests
    # WITH, not a string that reaches the screen.
    text = re.sub(r"const banned = /.*?/i;", "const banned = /x/i;", text)
    # `show.boards` / `project.show.boards` / a bare `boards:` property name
    # is the bundle schema's own field (plan §4.2: "boards": {} - a board-
    # renumbering map the designer never sees or edits, always {} here) -
    # a JS identifier, not UI prose. Blank it the same way property access
    # generally would not count as "vocabulary reaching the UI".
    text = re.sub(r"\.boards\b", ".X", text)
    text = re.sub(r"\bboards\s*:", "X:", text)
    text = re.sub(r'"boards"', '"X"', text)
    return text


def test_no_unit_vocabulary_hardcoded_in_designer_source():
    # plan_designer_sim.md: "no unit/radxa/bus/board/DIP/socket vocabulary
    # anywhere in the designer UI". This is the cheap half of that check: the
    # literal UI strings this page authors itself (comments may still
    # explain the rule in those words). It cannot see a word that only shows
    # up at runtime - e.g. the "natural" sequence's real label
    # ("Socket order (P01 to P60)", ported verbatim from
    # conductor/sequence.py, never hardcoded here) unless the display
    # override that hides it (designer-app.js's seqLabel()) is actually
    # wired up - see test_banned_vocabulary_never_reaches_rendered_ui below
    # for that half (adversarial review, 2026-09-25: this test alone passed
    # while the rendered Transition dropdown still said "Socket order").
    banned = re.compile(r"\b(unit|units|radxa|bus|buses|board|boards|dip|socket|sockets)\b", re.IGNORECASE)
    for name, html in (("designer.html", True), ("sim/designer-app.js", False), ("sim/designer.css", False)):
        text = _strip_comments((REPO / "conductor" / "web" / name).read_text(encoding="utf-8"), html)
        hit = banned.search(text)
        assert not hit, f"{name} contains banned vocabulary: {hit.group(0)!r}"


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
