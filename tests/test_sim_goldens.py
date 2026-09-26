"""The JS/Python cross-check for the designer simulator (plan section 2).

tools/make_goldens.py reads tests/fixtures/sim/*.csv and writes
tests/goldens/model.json and conductor/web/sim/goldens.js from the SAME
data, computed by the real conductor.look/sequence/timeline. This file
checks that those generated artefacts are current, and (the one test
that actually runs JavaScript) that a browser's SIM implementation
scores 0 failures against them.
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from conductor.look import Design, LookError, LookMap  # noqa: E402
from conductor import sequence  # noqa: E402

sys.path.insert(0, str(ROOT / "tools"))
import make_goldens as mg  # noqa: E402

SIM_DIR = ROOT / "conductor" / "web" / "sim"
GOLDEN_JSON = ROOT / "tests" / "goldens" / "model.json"


@pytest.fixture(scope="module")
def golden():
    return json.loads(GOLDEN_JSON.read_text(encoding="utf-8"))


def test_goldens_are_current():
    """python tools/make_goldens.py --check must see no drift - the
    generator recomputes goldens.js/model.json bit-for-bit deterministic
    (sorted keys, no timestamps, no absolute paths)."""
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "make_goldens.py"), "--check"],
        cwd=str(ROOT), capture_output=True, text=True)
    assert result.returncode == 0, (
        f"tests/goldens/model.json or conductor/web/sim/goldens.js is stale - "
        f"run python tools/make_goldens.py\nstdout={result.stdout}\nstderr={result.stderr}")


def test_generator_ignores_showdata(tmp_path, monkeypatch):
    """The generator must depend ONLY on committed inputs (tests/fixtures/
    sim/*.csv and conductor/web/starter/*.csv) - a populated, git-ignored
    showdata/files/ (real client data that only exists on the show PC)
    must not change a single golden case, or `--check` fails on any
    machine that happens to have it. Regression test for the bug Coder Q
    found: an extra showdata-derived digest case the committed goldens
    (built on a machine without showdata/) did not have.

    Fully sandboxed under tmp_path (adversarial review round 2 - F8): the
    old version of this test wrote its probe files straight into the real
    repository's showdata/files/ - on a machine that (like the one it was
    caught on) has real, gitignored client data sitting there already, and
    whose `finally` block's rmdir() calls could raise and mask the real
    assertion if that directory somehow ended up non-empty. This copies the
    committed inputs build_goldens() actually reads into a throwaway
    directory, monkeypatches the module's own path constants to it, and
    only ever writes probes under tmp_path - if some future edit ever adds
    a real ROOT/showdata/files glob back into the generator, ROOT is
    monkeypatched too, so that edit would still see these probes and this
    test would still catch it."""
    fake_root = tmp_path / "repo"
    fake_starter = fake_root / "conductor" / "web" / "starter"
    fake_fixtures = fake_root / "tests" / "fixtures" / "sim"
    shutil.copytree(mg.STARTER_DIR, fake_starter)
    shutil.copytree(mg.FIXTURES_DIR, fake_fixtures)
    monkeypatch.setattr(mg, "ROOT", fake_root)
    monkeypatch.setattr(mg, "STARTER_DIR", fake_starter)
    monkeypatch.setattr(mg, "FIXTURES_DIR", fake_fixtures)

    before = mg.dumps_sorted(mg.build_goldens())

    showdata_files = fake_root / "showdata" / "files"
    showdata_files.mkdir(parents=True)
    (showdata_files / "ZZZ_generator_ignores_showdata_probe_map.csv").write_text(
        "side,row,col,board_no,socket,label\nfront,0,1,1,1,\n", encoding="utf-8")
    (showdata_files / "ZZZ_generator_ignores_showdata_probe_color_pattern01_grid.csv"
    ).write_text("side,row,shift,1\nfront,0,0,0x01\n", encoding="utf-8")
    after = mg.dumps_sorted(mg.build_goldens())

    assert before == after, "a populated showdata/files/ changed the generated goldens"

    # And the same, run out-of-process (the actual `--check` entry
    # point), so a stray CWD-relative glob elsewhere in main() would
    # still be caught even if build_goldens() itself looks clean.
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "make_goldens.py"), "--check"],
        cwd=str(ROOT), capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_goldens_js_is_well_formed(golden):
    # read_bytes(), not read_text() (adversarial review round 2 - F8):
    # Path.read_text() does universal-newline translation, so "\r\n" not in
    # text can never fire regardless of what is actually on disk - the CRLF
    # check needs the raw bytes to mean anything.
    raw = (SIM_DIR / "goldens.js").read_bytes()
    assert raw.endswith(b"\n")
    assert b"\r\n" not in raw
    text = raw.decode("utf-8")
    assert str(ROOT).replace("\\", "/") not in text.replace("\\\\", "/")
    assert "globalThis.SIM = Object.assign" in text and "GOLDENS:" in text
    assert golden["format"] == "epaper-sim-goldens"


def test_fixture_csvs_parse_the_same_in_python(golden):
    """Every fixture CSV parses (or fails) the same way when read fresh
    through conductor.look, independently of tools/make_goldens.py's own
    caching - guards against a fixture edited without regenerating."""
    fixtures_dir = ROOT / "tests" / "fixtures" / "sim"
    for case in golden["cases"]:
        if case["kind"] == "map":
            text = fixtures_dir.joinpath(case["fixture"]).read_text(encoding="utf-8")
            opts = case["opts"]
            try:
                m = LookMap.parse(io.StringIO(text), name=opts["name"], item=opts["item"])
                ok, problems = True, []
            except LookError as exc:
                m, ok, problems = None, False, list(exc.problems)
            assert ok == case["expect"]["ok"], case["fixture"]
            if not ok:
                assert problems == case["expect"]["problems"], case["fixture"]
        elif case["kind"] == "design":
            text = fixtures_dir.joinpath(case["fixture"]).read_text(encoding="utf-8")
            opts = case["opts"]
            try:
                Design.parse(io.StringIO(text), name=opts["name"], item=opts["item"],
                             pattern=opts["pattern"])
                ok = True
            except LookError:
                ok = False
            assert ok == case["expect"]["ok"], case["fixture"]


def test_goldens_cover_every_sequence_and_rule(golden):
    seq_covered = {c["sequence"] for c in golden["cases"] if c["kind"] == "ranks"}
    assert seq_covered == set(sequence.SEQUENCES)

    kinds = {c["kind"] for c in golden["cases"]}
    for expected_kind in ("fmt", "canonical", "clock", "mmss", "names", "map", "design", "check",
                          "ranks", "timeline", "state"):
        assert expected_kind in kinds, f"no golden cases of kind {expected_kind!r}"

    # The geometry sentence (2026-09-26) is the one check() problem a
    # designer is meant to act on rather than read past, so it must be in
    # the goldens - the browser self-test is what proves model.js words it
    # identically, and it can only do that if the case is here. It must
    # also LEAD its result: buried under a thousand "no colour for ..."
    # lines it would be exactly as invisible as the messages it replaces.
    results = [result for c in golden["cases"] if c["kind"] == "check"
               for result in c["expect"]["results"]]
    geometry = [p for result in results for p in result
                if "made for another layout of" in p]
    assert geometry, "no golden case for a design made for another layout"
    assert all("配線ナビ" in p for p in geometry)   # 配線ナビ
    for result in results:
        hits = [i for i, p in enumerate(result) if "made for another layout of" in p]
        assert hits in ([], [0]), \
            "the geometry sentence must lead, not trail the per-scale problems"

    # The file-name grammar covers both spellings of a design file, the
    # site's own <model>_<配色案名>_HW.csv included.
    name_cases = [c for c in golden["cases"] if c["kind"] == "names"]
    assert {c["expect"]["kind"] for c in name_cases} == {"map", "grid", None}
    assert any(c["filename"].lower().endswith("_hw.csv")
               and c["expect"]["kind"] == "grid" for c in name_cases)

    # state-digest cases come from conductor/web/starter/*.csv (Q's
    # committed real maps), not showdata/ - they must always be present
    # and reproducible on any machine, one per starter item.
    digest_cases = [c for c in golden["cases"] if c["kind"] == "state-digest"]
    # The exact 10 starter items, pinned by name - not "no name contains the
    # literal word showdata" (adversarial review round 2 - F8), which can
    # never fail: these cases are built from conductor/web/starter/*.csv
    # (Q's committed real maps), and nothing in that path ever contains the
    # word "showdata" regardless of whether the check that follows it is
    # doing anything. A pinned set catches an item silently going missing
    # (or an unexpected one appearing) the way the vacuous check could not.
    EXPECTED_STARTER_ITEMS = {
        "starter-AZ271SB2303", "starter-AZ271SC6302", "starter-AZ271SD1301",
        "starter-AZ271SD1305", "starter-AZ271SD1305_B", "starter-AZ271SD1306",
        "starter-AZ271SD1307", "starter-AZ271SG1035", "starter-AZ271SG1036",
        "starter-AZ271SG3037",
    }
    assert {c["name"] for c in digest_cases} == EXPECTED_STARTER_ITEMS

    # The exact half-to-even landmark: Python rounds 2.25 to one decimal
    # as "2.2" (even), not the "2.3" a naive JS Math.round-based toFixed
    # would give - this is the whole reason SIM.fmt exists.
    tie = [c for c in golden["cases"]
          if c["kind"] == "fmt" and c["op"] == "fixed" and c["x"] == 2.25 and c["n"] == 1]
    assert tie and tie[0]["expect"] == "2.2"

    # Every validate() spacing "binding" (write/refresh/rejoin) and the
    # design-kind/no-preset/sweep rules appear somewhere in the timeline
    # goldens, not just as Python unit tests nobody ported.
    all_problems = " ".join(
        p for c in golden["cases"] if c["kind"] == "timeline"
        for plist in c["expect"]["problems"].values() for p in plist)
    all_warnings = " ".join(
        w for c in golden["cases"] if c["kind"] == "timeline" for w in c["expect"]["warnings"])
    for phrase in ("refresh + ", "a board holds", "merge or remove cues",
                  "is not loaded", "make this a partial cue", "after the end of the show",
                  "already has a cue sent at the same moment",
                  "previous picture is complete", "sweep is at most 30 s",
                  "needs the item's map to be timed"):
        assert phrase in all_problems, phrase
    assert "no preset at 0:00" in all_warnings


def test_canonical_and_digest_match_a_recorded_table():
    """A pinned table (computed once, by hand-inspecting the output;
    mg.CANONICAL_TABLE is the same list make_goldens.py turns into the
    golden "canonical" cases, so there is exactly one list of inputs)
    that both tools/make_goldens.py's canonical()/digest64() and
    model.js's SIM.fmt.canonical()/digest64() must reproduce - the
    browser self-test is the real cross-check; this catches a
    regression in the Python half even without a browser."""
    expect = [
        ("0", "af63ad4c86019caf"), ("5", "af63a84c86019430"), ("-5", "07d00f07b497d7f7"),
        ("2.500", "ac5b7dc41134559c"), ("0.500", "d45564b0f53aea22"),
        ('"a"', "d4272417d7c77eea"), ('"a\\"b"', "aea405f405787fda"),
        ("true", "5b5c98ef514dbfa5"), ("false", "b5fae2c14238b978"),
        ("null", "5b9bc4ba528108e4"), ('[1,2,"x"]', "6893ac5ba04fa4f2"),
        ('{"a":2,"b":1}', "f85f5878cbf2dc03"), ('"\\u65e5\\u672c\\u8a9e"', "9a5893e1cdc4beb6"),
    ]
    assert len(expect) == len(mg.CANONICAL_TABLE)
    for value, (expect_canonical, expect_digest) in zip(mg.CANONICAL_TABLE, expect):
        got_canonical = mg.canonical(value)
        assert got_canonical == expect_canonical, value
        assert mg.digest64(got_canonical) == expect_digest, value


def _find_browser() -> "str | None":
    env = os.environ.get("CONDUCTOR_BROWSER")
    candidates = [env] if env else []
    candidates += [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        shutil.which("msedge"),
        shutil.which("microsoft-edge"),
        shutil.which("google-chrome"),
        shutil.which("chrome"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return None


def _standalone_harness() -> str:
    """A tiny self-contained page (no designer.html, no UI) that inlines
    model.js + state.js + goldens.js + selftest.js in dependency order -
    until Q's dist/az27ss-simulator.html exists, this is what proves
    Coder P's branch is testable standalone (plan section 6)."""
    parts = []
    for name in ("model.js", "state.js", "goldens.js", "selftest.js"):
        text = (SIM_DIR / name).read_text(encoding="utf-8")
        assert "</script" not in text.lower(), f"{name} contains a literal </script"
        parts.append(f"<script>\n{text}\n</script>")
    body = "\n".join(parts)
    return f"<!doctype html>\n<html><head><meta charset=\"utf-8\"></head><body>\n{body}\n</body></html>\n"


def test_browser_selftest_passes(tmp_path):
    browser = _find_browser()
    forced = os.environ.get("CONDUCTOR_BROWSER_TESTS") == "1"
    if not browser:
        if forced:
            pytest.fail("CONDUCTOR_BROWSER_TESTS=1 but no browser was found "
                       "(set $CONDUCTOR_BROWSER to its path)")
        pytest.skip("no Edge/Chrome found - set CONDUCTOR_BROWSER_TESTS=1 to force")

    html_path = tmp_path / "selftest_harness.html"
    html_path.write_text(_standalone_harness(), encoding="utf-8")
    url = "file:///" + str(html_path.resolve()).replace("\\", "/") + "#selftest"
    user_data_dir = tmp_path / "user-data"
    args = [browser, "--headless=new", "--disable-gpu", "--no-sandbox", "--no-first-run",
           f"--user-data-dir={user_data_dir}", "--virtual-time-budget=20000",
           "--dump-dom", url]
    result = subprocess.run(args, capture_output=True, timeout=60)
    dom = result.stdout.decode("utf-8", errors="replace")
    match = re.search(r'data-ok="(true|false)" data-total="(\d+)" data-failed="(\d+)"', dom)
    assert match, f"no #selftest-out found in the dumped DOM (rc={result.returncode}):\n{dom[:3000]}\n" \
                  f"stderr: {result.stderr.decode('utf-8', errors='replace')[:2000]}"
    ok, total, failed = match.group(1), int(match.group(2)), int(match.group(3))
    if ok != "true":
        detail = re.search(r'<pre id="selftest-out"[^>]*>(.*?)</pre>', dom, re.S)
        pytest.fail(f"browser self-test failed ({failed}/{total}):\n"
                   f"{detail.group(1) if detail else dom[:3000]}")
    assert total > 900          # sanity: the goldens really did load and run
