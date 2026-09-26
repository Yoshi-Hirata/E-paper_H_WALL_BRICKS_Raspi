"""Tests for the designers' simulator build (Coder Q, plan_designer_sim.md).

Some of these are gated on inputs another coder owns and may not exist yet
mid-round (tests/fixtures/sim/bundle_v1.json from Coder P, showdata/files/
which is gitignored and machine-local) - they skip rather than fail then, so
this file is safe to run before those land, and starts actually checking
things the moment they do.
"""
import json
import math
import os
import re
import shutil
import subprocess
import sys
from html import unescape
from html.parser import HTMLParser
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
INDEX_HTML = REPO / "conductor" / "web" / "index.html"
DESIGNER_HTML = REPO / "conductor" / "web" / "designer.html"
DIST = REPO / "dist" / "az27ss-simulator.html"
BUILD_SCRIPT = REPO / "tools" / "build_designer.py"
STARTER_SCRIPT = REPO / "tools" / "make_starter.py"
STARTER_JS = REPO / "conductor" / "web" / "sim" / "starter.js"

sys.path.insert(0, str(REPO))
import tools.build_designer as build_designer  # noqa: E402
import tools.make_starter as make_starter  # noqa: E402


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


CONSTANT_NAMES = ["PITCH", "MARGIN", "JITTER_MAX", "SWEEP_JITTER_FRAC",
                  "TINT_STEPS", "REFRESH_TINT",
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


# ---- the embedded music (the built-in track) ----

# A real, playable 8-bit mono WAV of 1000 silent samples: small enough to
# live in a test as bytes, and an actual file a browser will decode rather
# than a plausible-looking blob (the headless test below presses nothing,
# but a source the media element refuses outright is not the thing being
# shipped). 0x80 is silence for unsigned 8-bit PCM.
def _silent_wav(samples: int = 1000) -> bytes:
    import struct
    pcm = b"\x80" * samples
    fmt = struct.pack("<4sIHHIIHH", b"fmt ", 16, 1, 1, 8000, 8000, 1, 8)
    data = struct.pack("<4sI", b"data", len(pcm)) + pcm
    body = b"WAVE" + fmt + data
    return struct.pack("<4sI", b"RIFF", len(body)) + body


def _embedded(html: str) -> dict:
    """The name/type/size SIM.embeddedMusic carries, read back out of the
    built page (deliberately not the dataUrl - 23 MB of base64 has no
    business in an assertion message)."""
    block = re.search(r"embeddedMusic:\s*\{(.*?)dataUrl:", html, re.S)
    assert block, "the page has no SIM.embeddedMusic"
    found = dict(re.findall(r"(\w+):\s*(\"(?:[^\"\\]|\\.)*\"|\d+)", block.group(1)))
    return {k: json.loads(v) for k, v in found.items()}


def test_build_page_embeds_the_music_it_is_given():
    audio = _silent_wav()
    html = build_designer.build_page(DESIGNER_HTML, music=audio,
                                     music_name="AZ 27SS.DEMO.wav",
                                     music_type="audio/wav")
    assert _embedded(html) == {"name": "AZ 27SS.DEMO.wav",
                               "type": "audio/wav",
                               # the AUDIO's byte count, not the base64's
                               "size": len(audio)}
    import base64
    assert base64.b64encode(audio).decode("ascii") in html
    # Still self-contained (build_page checks this itself and would have
    # raised) - asserted again here because the whole scheme rests on
    # check_self_contained treating a data: URL as fine while it refuses
    # every http(s)/protocol-relative one.
    build_designer.check_self_contained(html)
    # In front of the modules, so SIM.embeddedMusic is there whichever way
    # the page's boot() gets scheduled.
    assert html.index("embeddedMusic") < html.index("SIM.look")


def test_check_self_contained_takes_a_data_url_and_refuses_the_rest():
    ok = '<html><audio src="data:audio/mpeg;base64,AAAA"></audio></html>'
    build_designer.check_self_contained(ok)          # raises on failure
    for bad, why in [
        ('<html><script src="https://cdn.example/x.js"></script></html>', "http(s)"),
        ('<html><script src="//cdn.example/x.js"></script></html>', "protocol-relative"),
        ('<html><style>@import url("x.css");</style></html>', "@import"),
        ('<html><link rel="stylesheet" href="x.css"></html>', "<link>"),
    ]:
        with pytest.raises(ValueError):
            build_designer.check_self_contained(bad)


def test_base64_does_not_trip_the_control_character_or_escaping_guards():
    # The two guards the music script bypasses on purpose (see the module
    # docstring): every byte value appears in this audio, so its base64
    # covers the whole alphabet, and neither a control character nor a
    # "</script" can come out the other side.
    audio = bytes(range(256)) * 64
    html = build_designer.build_page(DESIGNER_HTML, music=audio,
                                     music_name="all-bytes.mp3", music_type="audio/mpeg")
    body = html[html.index("embeddedMusic"):]
    tag_end = body.index("</script>")
    assert not re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", body[:tag_end])
    assert "<" not in body[:tag_end]


def test_a_hostile_music_file_name_cannot_end_the_script_element():
    html = build_designer.build_page(
        DESIGNER_HTML, music=b"x", music_type="audio/mpeg",
        music_name='</script><script>globalThis.PWNED=1</script><!--.mp3')
    assert "PWNED" in html                     # the name is still carried...
    assert _embedded(html)["name"].endswith(".mp3")
    # ...but only ever as \u003c inside the string literal, so it is text.
    assert "\\u003c/script>" in html
    start = html.index("embeddedMusic")
    assert "</script" not in html[start:start + html[start:].index("</script>")]


def test_a_hostile_mime_cannot_break_out_of_the_data_url(tmp_path):
    # The MIME is the one value in the generated script that cannot be
    # escaped: it sits in the data: URL as raw text, because the
    # ";base64," after it is URL syntax, not a string. show.json is
    # hand-editable, so `audio/mpeg";x="` in its `type` used to close the
    # dataUrl literal and let whatever followed run as code.
    hostile = 'audio/mpeg";globalThis.PWNED=1;x="'
    html = build_designer.build_page(DESIGNER_HTML, music=b"x",
                                     music_name="clip.mp3", music_type=hostile)
    assert "PWNED" not in html
    embedded = _embedded(html)
    assert embedded["type"] == "audio/mpeg"          # fell back to the extension
    assert 'dataUrl: "data:audio/mpeg;base64,' in html

    # ...and the same through --music auto, which is where a real one comes
    # from: show.json's `type`, read by workspace_music().
    ws = tmp_path / "showdata"
    (ws / "music").mkdir(parents=True)
    (ws / "music" / "Track.wav").write_bytes(_silent_wav(64))
    (ws / "show.json").write_text(json.dumps(
        {"music": {"name": "Track.wav", "type": hostile}}), encoding="utf-8")
    _data, name, mime = build_designer.workspace_music(ws)
    assert build_designer.safe_mime(mime, name) == "audio/wav"

    for bad in ["", "audio", "audio/", "/wav", "audio/wav; charset=x",
                "audio/wav\nx", 'a"b/c', "audio/mpeg;base64,x"]:
        assert build_designer.safe_mime(bad, "clip.mp3") == "audio/mpeg", bad
    for good in ["audio/mpeg", "audio/x-wav", "application/octet-stream"]:
        assert build_designer.safe_mime(good, "clip.mp3") == good


def test_music_auto_reads_the_workspaces_show_json(tmp_path):
    audio = _silent_wav(64)
    ws = tmp_path / "showdata"
    (ws / "music").mkdir(parents=True)
    (ws / "music" / "Track One.wav").write_bytes(audio)
    (ws / "show.json").write_text(json.dumps(
        {"duration": 600, "music": {"name": "Track One.wav",
                                    "size": len(audio), "type": "audio/wav"}}),
        encoding="utf-8")
    assert build_designer.workspace_music(ws) == (audio, "Track One.wav", "audio/wav")

    out = tmp_path / "built.html"
    result = run(str(BUILD_SCRIPT), "--music", "auto", "--workspace", str(ws),
                 "--out", str(out))
    assert result.returncode == 0, result.stdout + result.stderr
    assert _embedded(out.read_text(encoding="utf-8"))["name"] == "Track One.wav"
    assert out.stat().st_size > DIST.stat().st_size


def test_music_auto_says_so_when_the_workspace_has_none(tmp_path):
    # Silently building the lean page here would be the worst outcome: it
    # would land on dist/az27ss-simulator.html (the committed one) and look
    # like a success.
    ws = tmp_path / "showdata"
    ws.mkdir()
    assert build_designer.workspace_music(ws) is None
    (ws / "show.json").write_text('{"music": {"name": "gone.mp3"}}', encoding="utf-8")
    assert build_designer.workspace_music(ws) is None, \
        "an entry whose file is missing is not music"
    result = run(str(BUILD_SCRIPT), "--music", "auto", "--workspace", str(ws))
    assert result.returncode == 1
    assert "names no music" in result.stderr


def test_a_music_build_never_lands_on_the_committed_lean_page(tmp_path):
    audio = _silent_wav(64)
    src = tmp_path / "clip.wav"
    src.write_bytes(audio)
    before = DIST.read_bytes()
    # The operator's real with-music page may be sitting at MUSIC_OUT (it is
    # the file handed to the director's team, and it is NOT committed, so
    # deleting it here lost it once). Move it aside for the test and put it
    # back afterwards - never leave dist/ poorer than it was found.
    kept = build_designer.MUSIC_OUT.with_name("az27ss-simulator-with-music.kept")
    kept.unlink(missing_ok=True)
    had_real = build_designer.MUSIC_OUT.exists()
    if had_real:
        build_designer.MUSIC_OUT.rename(kept)
    try:
        result = run(str(BUILD_SCRIPT), "--music", str(src))
        assert result.returncode == 0, result.stdout + result.stderr
        assert build_designer.MUSIC_OUT.exists()
        assert _embedded(build_designer.MUSIC_OUT.read_text(encoding="utf-8"))["name"] == "clip.wav"
        assert DIST.read_bytes() == before, \
            "a --music build must not overwrite dist/az27ss-simulator.html"
        # ...and the file it does write is gitignored, or someone will
        # eventually commit 23 MB of audio.
        ignored = subprocess.run(["git", "check-ignore", "-q",
                                  str(build_designer.MUSIC_OUT)], cwd=REPO)
        assert ignored.returncode == 0, \
            f"{build_designer.MUSIC_OUT} is not gitignored"
    finally:
        build_designer.MUSIC_OUT.unlink(missing_ok=True)
        if had_real:
            kept.rename(build_designer.MUSIC_OUT)


def test_the_timeline_toolbar_offers_the_simulator_download():
    index_text = INDEX_HTML.read_text(encoding="utf-8")
    match = re.search(r'<a[^>]*id="tl-simulator"[^>]*>', index_text)
    assert match, 'index.html has no "Simulator for designers…" link'
    tag = match.group(0)
    assert 'href="/api/simulator?music=1"' in tag
    assert "download" in tag
    assert "hand this file to the director's team" in tag
    # The click is handled in JS and the anchor's own download suppressed:
    # with `download` set, a failed build came back as a 500 whose JSON
    # body the browser saved AS the simulator - a file that looks right,
    # opens blank and explains nothing.
    assert re.search(r'id === "tl-simulator".*?preventDefault\(\).*?downloadSimulator\(\)',
                     index_text), "the simulator link still downloads without checking"
    handler = index_text[index_text.index("async function downloadSimulator"):]
    handler = handler[:handler.index("\nasync function ")]
    assert "response.ok" in handler and "Could not build the simulator" in handler
    assert "revokeObjectURL" in handler, "the 23 MB blob is never released"


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


def _require_browser(tmp_path):
    forced = os.environ.get("CONDUCTOR_BROWSER_TESTS") == "1"
    if not _find_browser():
        if forced:
            pytest.fail("CONDUCTOR_BROWSER_TESTS=1 but no browser was found "
                       "(set $CONDUCTOR_BROWSER to its path)")
        pytest.skip("no Edge/Chrome found - set CONDUCTOR_BROWSER_TESTS=1 to force")


# The probe appended to a COPY of the built page (never to the page itself):
# --dump-dom returns the DOM, not JS values, so the one way to see what
# SIM.app.getState() holds is to have the page write it into an element.
# Appending it here rather than shipping a "#musiccheck" hook keeps a
# test-only affordance out of the file a designer opens.
_MUSIC_PROBE = """
<script>
window.addEventListener("load", function () {
  setTimeout(function () {
    var music = ((globalThis.SIM && SIM.app && SIM.app.getState()) || {}).music || {};
    var pre = document.createElement("pre");
    pre.id = "musiccheck-out";
    pre.setAttribute("data-name", String(music.name));
    pre.setAttribute("data-scheme", String(music.url).split(":")[0]);
    document.body.appendChild(pre);
  }, 200);
});
</script>
"""


def test_an_embedded_track_is_loaded_at_start_up_without_any_pick(tmp_path):
    # The whole point of the feature, checked in a real browser: open a
    # with-music build in a clean profile, touch nothing, and the transport
    # already has audio. state.music.url being a blob: URL is what says the
    # bytes were decoded (not merely that a name was remembered) - a page
    # that only knew the name is exactly the silent one this replaces.
    _require_browser(tmp_path)
    audio = _silent_wav(8000)
    html = build_designer.build_page(DESIGNER_HTML, music=audio,
                                     music_name="Probe Track.wav",
                                     music_type="audio/wav")
    page = tmp_path / "with-music.html"
    page.write_text(html.replace("</body>", _MUSIC_PROBE + "</body>", 1),
                    encoding="utf-8")
    url = "file:///" + str(page.resolve()).replace("\\", "/")
    dom = _dump_dom(url, tmp_path)
    match = re.search(r'<pre id="musiccheck-out" data-name="([^"]*)" '
                      r'data-scheme="([^"]*)"', dom or "")
    assert match, f"no #musiccheck-out in the dumped DOM:\n{(dom or '')[:3000]}"
    assert match.group(1) == "Probe Track.wav"
    assert match.group(2) == "blob", \
        f"state.music.url is {match.group(2)!r}, so nothing was decoded"


def test_the_lean_page_has_no_built_in_track(tmp_path):
    # The other half: the committed page must NOT claim a built-in track,
    # or every designer who opens the lean file sees a music line promising
    # audio that is not in it.
    _require_browser(tmp_path)
    page = tmp_path / "lean.html"
    page.write_text(DIST.read_text(encoding="utf-8")
                    .replace("</body>", _MUSIC_PROBE + "</body>", 1),
                    encoding="utf-8")
    url = "file:///" + str(page.resolve()).replace("\\", "/")
    dom = _dump_dom(url, tmp_path)
    match = re.search(r'<pre id="musiccheck-out" data-name="([^"]*)" '
                      r'data-scheme="([^"]*)"', dom or "")
    assert match, f"no #musiccheck-out in the dumped DOM:\n{(dom or '')[:3000]}"
    assert match.group(1) == "null" and match.group(2) == "null"


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


# ============================================================
# The starter data's labels (user, 2026-09-25)
# ============================================================
# The show's own LOOK and model numbers, as the production show.json holds
# them. The starter used to write {"look": n, "model": ""}, so a designer
# opening the simulator saw "LOOK 23" with an empty "model no." box where the
# operator sees "LOOK 23 · AZ271SD1305" - and the three bags, which carry no
# LOOK number at all, showed only their file-name stem. This table is the
# contract; tools/make_starter.py's STARTER_LABELS is where it is written
# down, and conductor/web/sim/starter.js is the generated copy the page
# actually reads.
SHOW_LABELS = {
    "AZ271SD1305":   {"look": "23", "model": "AZ271SD1305"},
    "AZ271SD1305_B": {"look": "24", "model": "AZ271SD1305"},
    "AZ271SD1301":   {"look": "25", "model": "AZ271SD1301"},
    "AZ271SB2303":   {"look": "26", "model": "AZ271SB2303 (Skirt)"},
    "AZ271SC6302":   {"look": "26", "model": "AZ271SC6302 (Tops)"},
    "AZ271SD1306":   {"look": "27", "model": "AZ271SD1306"},
    "AZ271SD1307":   {"look": "28", "model": "AZ271SD1307"},
    "AZ271SG1035":   {"look": "",   "model": "AZ271SG1035 (Bag 01)"},
    "AZ271SG1036":   {"look": "",   "model": "AZ271SG1036 (Bag 02)"},
    "AZ271SG3037":   {"look": "",   "model": "AZ271SG3037 (Bag 03)"},
}


def _starter_payload() -> dict:
    """SIM.STARTER out of the committed starter.js - the generated artefact
    the page loads, not make_starter.py's constants (those are checked
    separately below): a table that is right in the script but stale in the
    committed file is exactly the drift worth catching."""
    text = STARTER_JS.read_text(encoding="utf-8")
    head = "{ STARTER: "
    return json.loads(text[text.index(head) + len(head):text.rindex(" });")])


def test_starter_labels_are_the_shows_own_look_and_model_numbers():
    labels = _starter_payload()["show"]["labels"]
    assert labels == SHOW_LABELS
    assert {item: {"look": look, "model": model}
            for item, (look, model) in make_starter.STARTER_LABELS.items()} == SHOW_LABELS
    # STARTER_LOOKS (which orders the line-up) is derived from the same table
    # and must stay in step with it - the two used to be hand-maintained.
    assert make_starter.STARTER_LOOKS == {
        item: label["look"] for item, label in SHOW_LABELS.items() if label["look"]}


def test_starter_labels_match_the_production_workspace():
    """The table above is a copy of the operator's own show.json. On the show
    PC (and on any machine that has the workspace) check the copy against the
    original, so the two cannot drift apart unnoticed; elsewhere that
    directory is gitignored and absent, and the table alone is all there is
    to check."""
    show_json = REPO / "showdata" / "show.json"
    if not show_json.exists():
        pytest.skip("the show workspace is gitignored and not present on this machine")
    labels = json.loads(show_json.read_text(encoding="utf-8")).get("labels") or {}
    assert labels, "the show workspace carries no labels to compare against"
    assert {k: {"look": str(v.get("look", "")), "model": str(v.get("model", ""))}
            for k, v in labels.items()} == SHOW_LABELS


def test_every_garment_in_the_starter_data_is_labelled():
    # A garment the table forgets renders as its bare file-name stem, which
    # is the state this table replaced - so "the table is right" is only
    # half the check; it also has to be complete.
    payload = _starter_payload()
    items = sorted({make_starter.map_item(name) for name in payload["files"]
                    if make_starter.kind(name) == "map"})
    assert items == sorted(SHOW_LABELS), \
        "conductor/web/starter/ holds a garment tools/make_starter.py has no label for"


# ============================================================
# The Designs tab's per-item "Add CSV" (user, 2026-09-25)
# ============================================================
# Appended to a COPY of the built page, like _MUSIC_PROBE above: drives the
# real page as a designer would (clicking each item in the sidebar, then the
# per-item file input's own change event) and writes what it found into an
# element, because --dump-dom returns the DOM and nothing else.
#
# It waits for the page to have booted rather than guessing at a delay - the
# starter data is ~350 KB of CSV to parse before the ITEMS sidebar exists.
_ITEM_CSV_PROBE = """
<script>
(function () {
  var out = { error: null, items: [], global: false, toast: "", seam: {}, render: {}, backfill: {} };
  function ready() {
    try {
      var st = globalThis.SIM && SIM.app && SIM.app.getState();
      return !!(st && st.items && st.items.length && document.querySelector('#items .item[data-item]'));
    } catch (e) { return false; }
  }
  function publish() {
    var pre = document.createElement("pre");
    pre.id = "itemcsv-out";
    pre.textContent = JSON.stringify(out);
    document.body.appendChild(pre);
  }
  function pickerFor(key) {
    return document.querySelector('#content label.filebtn[data-pick-item="' + key + '"]');
  }
  function itemNamed(key) {
    return SIM.app.getState().items.filter(function (i) { return i.item === key; })[0];
  }
  function nameOf(el) {
    if (!el) return null;
    var small = el.querySelector("small");
    var sub = small ? small.textContent : "";
    // "".replace("", x) inserts at position 0, so only strip a sub that
    // actually has text.
    return { name: sub ? el.textContent.replace(sub, "") : el.textContent,
             sub: sub, title: el.getAttribute("title") };
  }
  function run() {
    var keys = [].slice.call(document.querySelectorAll('#items .item[data-item]'))
                 .map(function (c) { return c.dataset.item; });
    out.global = !!document.querySelector('#pick');
    keys.forEach(function (key) {
      document.querySelector('#items .item[data-item="' + key + '"]').click();
      var btn = pickerFor(key);
      var input = btn && btn.querySelector('input[type="file"]');
      out.items.push({
        item: key,
        label: btn ? btn.textContent.trim() : null,
        tabindex: btn ? btn.getAttribute("tabindex") : null,
        multiple: !!(input && input.multiple),
        accept: input ? input.getAttribute("accept") : null,
        inCard: !!(btn && btn.closest(".card") &&
                   /DESIGNS OF THIS ITEM/.test(btn.closest(".card").querySelector("h2").textContent))
      });
    });

    // ---- what the page NAMES things, against the shipped labels ----
    // A garment with no LOOK number (the bags) must name itself by its model
    // number, and a shared LOOK must not print the model twice.
    document.querySelector('[data-tab="timeline"]').click();
    var rows = [].slice.call(document.querySelectorAll('.tl-name'));
    out.render.bagTrack = nameOf(rows.filter(function (e) { return /Bag 03/.test(e.textContent); })[0]);
    out.render.sharedTrack = nameOf(rows.filter(function (e) { return /Skirt/.test(e.textContent); })[0]);
    out.render.plainTrack = nameOf(rows.filter(function (e) { return /^LOOK 23/.test(e.textContent); })[0]);
    out.render.lookHeads = [].slice.call(document.querySelectorAll('.lk-head')).map(
      function (e) { var s = e.querySelector(".sub"); var st = s ? s.textContent : "";
        return (st ? e.textContent.replace(st, "") : e.textContent).replace(/refreshing…/, "").trim(); });
    out.render.cueItems = [].slice.call(document.querySelectorAll('table tbody tr td:nth-child(4)')).map(
      function (e) { return e.textContent; });
    var sharedRow = [].slice.call(document.querySelectorAll('table tbody tr')).filter(
      function (r) { return /Skirt/.test(r.textContent); })[0];
    if (sharedRow) sharedRow.click();
    var head = document.querySelector('#cue-editor-body .cue-head b');
    out.render.cueEditorHead = head ? head.textContent : null;
    out.render.minInterval = [].slice.call(document.querySelectorAll('.card')).filter(function (c) {
      var h = c.querySelector("h2"); return h && /SHORTEST INTERVAL/.test(h.textContent); }).map(
      function (c) { return [].slice.call(c.querySelectorAll("tbody tr td:first-child")).map(function (e) { return e.textContent; }); })[0] || [];
    // Which garment of a shared LOOK is drawn on top: lookGroups() sorts a
    // group's items by stackRank, and the looks row draws them in that order.
    out.render.look26 = (SIM.looks.lookGroups(SIM.app.getState().items).filter(
      function (g) { return String(g.look) === "26"; })[0] || { items: [] }).items.map(
      function (i) { return i.model; });

    // ---- the seam, called directly ----
    var key = keys.filter(function (k) { return k !== "AZ271SB2303"; })[0];
    out.seam.item = key;
    document.querySelector('#items .item[data-item="' + key + '"]').click();
    var foreign = "AZ271SB2303_color_probeA_grid.csv";
    out.seam.refused = SIM.app.addFilesToItem(key, [{ name: "notes.txt", text: "x" },
                                                    { name: "notes.csv", text: "x" }]).refused;
    var r = SIM.app.addFilesToItem(key, [{ name: foreign, text: SIM.STARTER.files["AZ271SB2303_color_sampleA_grid.csv"] }]);
    out.seam.renamed = r.renamed;
    out.seam.saved = r.saved;
    out.seam.unknown = SIM.app.addFilesToItem("NO_SUCH_ITEM", [{ name: foreign, text: "x" }]);
    out.seam.designsAfter = itemNamed(key).designs.map(function (d) { return d.name; });

    // A garment's MAP is not interchangeable: picking another garment's map
    // here must change nothing at all (it used to overwrite this one's).
    var mapBefore = { name: itemNamed(key).map.name, scales: itemNamed(key).map.scales.length,
                      text: SIM.app.getProject().files[key + "_map.csv"] };
    var otherKey = keys.filter(function (k) { return k !== key; })[0];
    var otherMap = otherKey + "_map.csv";
    out.seam.otherMap = otherMap;
    out.seam.mapRefused = SIM.app.addFilesToItem(key, [{ name: otherMap, text: SIM.STARTER.files[otherMap] }]);
    out.seam.mapAfter = { name: itemNamed(key).map.name, scales: itemNamed(key).map.scales.length,
                          unchanged: SIM.app.getProject().files[key + "_map.csv"] === mapBefore.text,
                          sameScales: itemNamed(key).map.scales.length === mapBefore.scales };
    // ...but the garment's OWN map, re-picked, still replaces itself.
    out.seam.ownMap = SIM.app.addFilesToItem(key, [{ name: key + "_map.csv", text: mapBefore.text }]);

    // Two picked files that would land on the same name: the first wins.
    out.seam.clash = SIM.app.addFilesToItem(key, [
      { name: "AZ271SB2303_color_clash_grid.csv", text: SIM.STARTER.files["AZ271SB2303_color_sampleA_grid.csv"] },
      { name: "AZ271SC6302_color_clash_grid.csv", text: SIM.STARTER.files["AZ271SC6302_color_sampleA_grid.csv"] }]);

    // ---- the label back-fill, on a project stored before the fix ----
    var old = { files: {}, show: { labels: {
      "AZ271SD1305": { look: "23", model: "" },
      "AZ271SD1301": { look: "25", model: "" },
      "AZ271SD1307": { look: "28", model: "TYPED BY HAND" } } } };
    out.backfill.after = globalThis.__labelBackfill(old).show.labels;

    // ---- and the buttons themselves, through their own change events ----
    document.querySelector('[data-tab="designs"]').click();
    var gdt = new DataTransfer();
    gdt.items.add(new File(["x"], "cover.png", { type: "image/png" }));
    var global = document.querySelector('#pick');
    global.files = gdt.files;
    global.dispatchEvent(new Event("change", { bubbles: true }));
    setTimeout(function () {
      var t0 = document.querySelector("#toast");
      out.globalToast = t0 ? t0.textContent : "";
      document.querySelector('#items .item[data-item="' + key + '"]').click();
      var input = pickerFor(key).querySelector('input[type="file"]');
      var dt = new DataTransfer();
      dt.items.add(new File(["not,a,grid\\n"], "notes.txt", { type: "text/csv" }));
      input.files = dt.files;
      input.dispatchEvent(new Event("change", { bubbles: true }));
      setTimeout(function () {
        var t = document.querySelector("#toast");
        out.toast = t ? t.textContent : "";
        out.filesAfter = Object.keys(SIM.app.getProject().files).filter(
          function (n) { return /notes|cover/.test(n); });
        publish();
      }, 300);
    }, 300);
  }
  var tries = 0;
  var timer = setInterval(function () {
    if (!ready() && ++tries < 200) return;
    clearInterval(timer);
    try { run(); } catch (e) { out.error = String((e && e.stack) || e); publish(); }
  }, 50);
})();
</script>
"""


def _probe_page(tmp, probe: str, out_id: str) -> dict:
    page = tmp / (out_id + ".html")
    page.write_text(DIST.read_text(encoding="utf-8").replace("</body>", probe + "</body>", 1),
                    encoding="utf-8")
    url = "file:///" + str(page.resolve()).replace("\\", "/")
    dom = _dump_dom(url, tmp)
    match = re.search(r'<pre id="%s">(.*?)</pre>' % out_id, dom or "", re.S)
    assert match, f"no #{out_id} in the dumped DOM:\n{(dom or '')[:3000]}"
    data = json.loads(unescape(match.group(1)))
    assert data.get("error") is None, data["error"]
    return data


@pytest.fixture(scope="module")
def item_csv_probe(tmp_path_factory):
    """One headless run of the real built page, shared by the tests below -
    booting it costs a few seconds and parsing the starter CSVs costs more,
    and every test here asks about the same page."""
    tmp = tmp_path_factory.mktemp("itemcsv")
    _require_browser(tmp)
    assert DIST.exists(), "dist/az27ss-simulator.html has not been built yet"
    return _probe_page(tmp, _ITEM_CSV_PROBE, "itemcsv-out")


def test_every_item_offers_its_own_add_csv_button(item_csv_probe):
    data = item_csv_probe
    assert len(data["items"]) == len(SHOW_LABELS), \
        f"expected one card per starter garment, got {len(data['items'])}"
    for entry in data["items"]:
        assert entry["label"] == "Add CSV", f"{entry['item']}: {entry!r}"
        assert entry["inCard"], f"{entry['item']}: the button is not in its DESIGNS OF THIS ITEM card"
        assert entry["multiple"], f"{entry['item']}: the picker takes only one file"
        assert entry["accept"] == ".csv", f"{entry['item']}: {entry['accept']!r}"
        # Keyboard: the <label> is the button (its input is display:none), so
        # it has to be in the tab order - designer-app.js gives it Enter and
        # Space to match.
        assert entry["tabindex"] == "0", f"{entry['item']}: the button cannot be tabbed to"
    assert data["global"], 'the header\'s own "Add CSV" is gone'


def test_a_csv_that_cannot_belong_to_the_item_is_refused(item_csv_probe):
    data = item_csv_probe
    seam = data["seam"]
    # Each refusal says what is wrong with THAT name (adversarial review F6).
    assert [r["name"] for r in seam["refused"]] == ["notes.txt", "notes.csv"]
    assert seam["refused"][0]["error"] == "not a .csv file"
    assert "_map.csv" in seam["refused"][1]["error"] and "_grid.csv" in seam["refused"][1]["error"]
    # Nothing was saved under that name, by either route.
    assert data["filesAfter"] == []
    # ...and the designer is told, naming the file.
    assert "refused" in data["toast"] and "notes.txt" in data["toast"], data["toast"]
    # The header's own Add CSV says so too, instead of doing nothing visible
    # (adversarial review F6).
    assert "cover.png" in data["globalToast"] and "not a .csv file" in data["globalToast"], \
        data["globalToast"]

    # A design CSV named after a DIFFERENT garment is not refused - it is
    # renamed onto this item, exactly as the Conductor's own per-item upload
    # does (index.html's uploadOwn()) - and the toast names both names.
    key = seam["item"]
    assert key != "AZ271SB2303", "the probe picked the garment the file is already named after"
    assert seam["renamed"] == [{"from": "AZ271SB2303_color_probeA_grid.csv",
                                "to": key + "_color_probeA_grid.csv"}]
    assert seam["saved"] == [key + "_color_probeA_grid.csv"]
    assert key + "_color_probeA_grid.csv" in seam["designsAfter"]
    assert not [n for n in seam["designsAfter"] if n.startswith("AZ271SB2303")], \
        "the file landed on the garment it was named after, not the one it was added to"

    # An item that is not in the project takes nothing.
    assert seam["unknown"]["saved"] == [] and seam["unknown"]["renamed"] == []
    assert len(seam["unknown"]["refused"]) == 1


def test_another_garments_map_is_never_renamed_onto_this_one(item_csv_probe):
    # Adversarial review F1: a per-item pick of another garment's *_map.csv
    # used to be renamed like a design grid, so it REPLACED this garment's
    # wiring - the original text gone, the toast reading like a success, and
    # several hundred CHECK problems the only hint.
    seam = item_csv_probe["seam"]
    refused = seam["mapRefused"]["refused"]
    assert [r["name"] for r in refused] == [seam["otherMap"]], seam["mapRefused"]
    assert "another garment's map" in refused[0]["error"]
    assert "Add CSV" in refused[0]["error"], "the refusal does not say what to do instead"
    assert seam["mapRefused"]["saved"] == [] and seam["mapRefused"]["renamed"] == []
    assert seam["mapAfter"]["name"] == seam["item"] + "_map.csv"
    assert seam["mapAfter"]["unchanged"], "this garment's map text was overwritten"
    assert seam["mapAfter"]["sameScales"]
    # The garment's own map, re-picked, still replaces itself - the rule is
    # "another garment's map", not "no map at all".
    assert seam["ownMap"]["saved"] == [seam["item"] + "_map.csv"], seam["ownMap"]
    assert seam["ownMap"]["refused"] == []


def test_two_picked_files_cannot_land_on_the_same_name(item_csv_probe):
    # Adversarial review F3: both renamed to <item>_color_clash_grid.csv, so
    # addFiles() silently kept the last one while the toast counted two saved.
    clash = item_csv_probe["seam"]["clash"]
    key = item_csv_probe["seam"]["item"]
    assert clash["saved"] == [key + "_color_clash_grid.csv"], clash
    assert [r["name"] for r in clash["refused"]] == ["AZ271SC6302_color_clash_grid.csv"], clash
    # Naming both, so it is clear which of the two was kept.
    assert "AZ271SB2303_color_clash_grid.csv" in clash["refused"][0]["error"], clash


def test_a_garment_with_no_look_names_itself_by_its_model_number(item_csv_probe):
    # Adversarial review F4/F5, on the rendered page rather than on the
    # helpers: a bag has no LOOK number, so the model number is the name -
    # the raw item code (a file-name stem) must appear nowhere.
    render = item_csv_probe["render"]
    bag = render["bagTrack"]
    assert bag is not None, "no bag track row found"
    assert bag["name"] == "AZ271SG3037 (Bag 03)", bag
    assert not bag["sub"], f"the model number is printed twice: {bag!r}"
    assert "AZ271SG3037 (Bag 03)" in render["lookHeads"], render["lookHeads"]
    assert [h for h in render["lookHeads"] if h == "AZ271SG3037"] == [], \
        "the looks row still names a bag by its item code"
    assert "AZ271SG3037 (Bag 03)" in render["cueItems"], render["cueItems"]

    # A shared LOOK is disambiguated by the model number, not the item code,
    # and the model is not then repeated underneath.
    shared = render["sharedTrack"]
    assert shared["name"] == "LOOK 26 · AZ271SB2303 (Skirt)", shared
    assert not shared["sub"], f"the model number is printed twice: {shared!r}"
    # The long form (the row's own tooltip, the cue table, the EDIT CUE
    # heading) is not a blind join of the two either.
    assert shared["title"] == "LOOK 26 · AZ271SB2303 (Skirt)", shared
    assert "LOOK 26 · AZ271SB2303 (Skirt)" in render["cueItems"], render["cueItems"]
    assert [c for c in render["cueItems"] if c.count("(Skirt)") > 1] == [], render["cueItems"]
    assert render["cueEditorHead"] == "LOOK 26 · AZ271SB2303 (Skirt)", render["cueEditorHead"]
    assert render["minInterval"], "SHORTEST INTERVAL PER ITEM rendered no rows to check"
    assert [k for k in render["minInterval"] if "AZ271SB2303 (Skirt)" in k], render["minInterval"]
    assert [k for k in render["minInterval"] if k in ("AZ271SB2303", "LOOK 26 · AZ271SB2303")] == [], \
        "SHORTEST INTERVAL still disambiguates by item code"

    # An unshared LOOK keeps the model number on its own second line.
    plain = render["plainTrack"]
    assert plain["name"] == "LOOK 23" and plain["sub"] == "AZ271SD1305", plain
    assert plain["title"] == "LOOK 23 · AZ271SD1305", plain
    assert "LOOK 23 · AZ271SD1305" in render["cueItems"], render["cueItems"]


def test_a_shared_looks_top_is_drawn_above_its_skirt(item_csv_probe):
    # lookGroups() sorts a LOOK's garments by stackRank, which can only read
    # the model number's own wording (an item carries no structural side
    # hint) - so this is only true once the labels carry "(Tops)"/"(Skirt)".
    assert item_csv_probe["render"]["look26"] == ["AZ271SC6302 (Tops)", "AZ271SB2303 (Skirt)"], \
        item_csv_probe["render"]["look26"]


def test_a_project_stored_before_the_labels_were_fixed_gets_the_model_numbers(item_csv_probe):
    # Adversarial review F2, on the back-fill itself; the end-to-end route
    # (seed localStorage, reload, read the rendered page) is the test below.
    after = item_csv_probe["backfill"]["after"]
    assert after["AZ271SD1305"] == {"look": "23", "model": "AZ271SD1305"}
    assert after["AZ271SD1301"] == {"look": "25", "model": "AZ271SD1301"}
    # Never over something a designer typed.
    assert after["AZ271SD1307"] == {"look": "28", "model": "TYPED BY HAND"}
    # An item the old starter had no label for at all (the bags) gets the
    # whole label, LOOK included - which for a bag is deliberately blank.
    assert after["AZ271SG1035"] == {"look": "", "model": "AZ271SG1035 (Bag 01)"}
    assert set(after) == set(SHOW_LABELS)


# The end-to-end half of F2: the autosave beats the starter data, so the fix
# only means anything if it survives a real boot from a real stored project.
# Two passes in one page - seed localStorage, reload, report - because the app
# has already read localStorage by the time any appended script can run.
_BACKFILL_PROBE = """
<script>
(function () {
  var KEY = "az27ss.project.v1", FLAG = "az27ss.test.seeded";
  function publish(out) {
    var pre = document.createElement("pre");
    pre.id = "backfill-out";
    pre.textContent = JSON.stringify(out);
    document.body.appendChild(pre);
  }
  function ready() {
    try {
      var st = globalThis.SIM && SIM.app && SIM.app.getState();
      return !!(st && st.items && st.items.length);
    } catch (e) { return false; }
  }
  function go() {
    var seeded = null;
    try { seeded = sessionStorage.getItem(FLAG); }
    catch (e) { publish({ error: null, storage: false }); return; }
    if (!seeded) {
      var old = { files: {}, show: {} };
      Object.keys(SIM.STARTER.files).forEach(function (n) { old.files[n] = SIM.STARTER.files[n]; });
      old.show = JSON.parse(JSON.stringify(SIM.STARTER.show));
      // The labels the starter USED to write: a LOOK number, an empty model,
      // and no entry at all for the three bags.
      var oldLabels = {};
      Object.keys(SIM.STARTER.show.labels).forEach(function (k) {
        var look = SIM.STARTER.show.labels[k].look;
        if (look) oldLabels[k] = { look: look, model: "" };
      });
      old.show.labels = oldLabels;
      try {
        localStorage.setItem(KEY, JSON.stringify(old));
        sessionStorage.setItem(FLAG, "1");
      } catch (e) { publish({ error: null, storage: false }); return; }
      location.reload();
      return;
    }
    var labels = {};
    SIM.app.getState().items.forEach(function (i) { labels[i.item] = { look: i.look, model: i.model }; });
    publish({ error: null, storage: true, labels: labels, stored: Object.keys(JSON.parse(localStorage.getItem(KEY)).show.labels).length });
  }
  var tries = 0;
  var timer = setInterval(function () {
    if (!ready() && ++tries < 200) return;
    clearInterval(timer);
    try { go(); } catch (e) { publish({ error: String((e && e.stack) || e) }); }
  }, 50);
})();
</script>
"""


def test_a_stored_project_shows_the_new_labels_after_a_real_boot(tmp_path):
    _require_browser(tmp_path)
    assert DIST.exists(), "dist/az27ss-simulator.html has not been built yet"
    data = _probe_page(tmp_path, _BACKFILL_PROBE, "backfill-out")
    if not data.get("storage"):
        pytest.skip("localStorage is not usable in this headless profile")
    labels = data["labels"]
    assert labels["AZ271SD1305"] == {"look": "23", "model": "AZ271SD1305"}, labels
    assert labels["AZ271SG1035"] == {"look": "", "model": "AZ271SG1035 (Bag 01)"}, labels
    assert labels["AZ271SB2303"] == {"look": "26", "model": "AZ271SB2303 (Skirt)"}, labels


# ============================================================
# Appending designs along the timeline (user, 2026-09-25)
# ============================================================
# 「ショーの開始から次々に後ろに新しいデザインを足していきたい」 - the director's
# team builds a show by appending one design after another from 0.00, and the
# only way to do that used to be an undiscoverable click on empty track space.
# This probe drives the two controls that replaced it, in the real built page:
# the per-row "+", and the hover ghost that makes the click visible.
_APPEND_PROBE = """
<script>
(function () {
  var out = { error: null, rows: [], trackItems: [], hint: null, addButton: null,
              ghostEmpty: null, ghostOverBand: null, ghostDuringDrag: null,
              appended: null, editLink: null, fractional: null, empty: null };
  function ready() {
    try {
      var st = globalThis.SIM && SIM.app && SIM.app.getState();
      return !!(st && st.items && st.items.length && document.querySelector('#items .item[data-item]'));
    } catch (e) { return false; }
  }
  function publish() {
    var pre = document.createElement("pre");
    pre.id = "append-out";
    pre.textContent = JSON.stringify(out);
    document.body.appendChild(pre);
  }
  function cuesOf(key) {
    return SIM.app.getState().show.cues.filter(function (c) { return c.item === key; })
             .sort(function (a, b) { return a.sent - b.sent; });
  }
  function itemNamed(key) {
    return SIM.app.getState().items.filter(function (i) { return i.item === key; })[0];
  }
  // Geometric, not elementFromPoint: the fixed dock covers the lower track
  // rows in an 800x600 headless window, and what is painted on top of a
  // track has nothing to do with which handler a dispatched event reaches.
  function emptySpot(track) {
    var box = track.getBoundingClientRect();
    var y = box.top + box.height / 2;
    var rects = [].slice.call(track.querySelectorAll('.cue-hold, .cue-band')).map(
      function (e) { return e.getBoundingClientRect(); });
    for (var f = 0.95; f > 0.02; f -= 0.005) {
      var x = box.left + box.width * f;
      var clear = rects.every(function (r) { return x < r.left - 3 || x > r.right + 3; });
      if (clear) return { x: x, y: y };
    }
    return null;
  }
  function move(el, p) {
    el.dispatchEvent(new PointerEvent("pointermove",
      { bubbles: true, clientX: p.x, clientY: p.y, pointerId: 1 }));
  }
  function ghostInfo() {
    var g = document.querySelector('.cue-ghost');
    if (!g) return null;
    var lab = g.querySelector('.cue-ghost-label');
    var track = g.closest('.tl-track');
    return { at: g.dataset.at, label: lab ? lab.textContent : null,
             left: g.style.left, width: g.style.width,
             track: track ? track.dataset.track : null,
             html: g.outerHTML };
  }
  function snapped(track, x) {
    var box = track.getBoundingClientRect();
    var D = SIM.app.getState().show.duration;
    return Math.round(Math.max(0, Math.min(D, (x - box.left) / box.width * D)));
  }
  function run() {
    document.querySelector('[data-tab="timeline"]').click();
    out.trackItems = SIM.app.getState().items.filter(
      function (i) { return i.map && i.map.scales.length; }).map(function (i) { return i.item; });
    out.hint = (document.querySelector('.tl-hint') || {}).textContent || null;

    // ---- a "+" on every track row ----
    [].slice.call(document.querySelectorAll('.tl-row')).forEach(function (row) {
      var track = row.querySelector('.tl-track');
      if (!track) return;
      var btn = row.querySelector('button.tl-add');
      if (btn) btn.focus();
      out.rows.push({ item: track.dataset.track, has: !!btn,
                      tag: btn ? btn.tagName : null,
                      type: btn ? btn.getAttribute("type") : null,
                      text: btn ? btn.textContent.trim() : null,
                      forItem: btn ? btn.dataset.add : null,
                      title: btn ? btn.getAttribute("title") : null,
                      aria: btn ? btn.getAttribute("aria-label") : null,
                      disabled: btn ? btn.disabled : null,
                      focusable: btn ? document.activeElement === btn : null,
                      lastInRow: btn ? row.lastElementChild === btn : null });
    });
    out.addButton = (document.querySelector('button.tl-add') || {}).outerHTML || null;

    // ---- pressing "+" appends after the last cue ----
    var key = out.trackItems.filter(function (k) { return cuesOf(k).length > 0; })[0];
    var before = cuesOf(key);
    var last = before[before.length - 1];
    var designs = itemNamed(key).designs.map(function (d) { return d.name; });
    var beforeIds = before.map(function (c) { return String(c.id); });
    document.querySelector('button.tl-add[data-add="' + key + '"]').click();
    var after = cuesOf(key);
    var fresh = after.filter(function (c) { return beforeIds.indexOf(String(c.id)) < 0; });
    var sel = document.querySelector('.cue-hold.sel');
    out.appended = {
      item: key, designs: designs, countBefore: before.length, countAfter: after.length,
      lastBefore: { at: last.at, complete: last.complete, design: last.design },
      fresh: fresh.map(function (c) { return { id: String(c.id), at: c.at, design: c.design, partial: c.partial }; }),
      selectedBand: sel ? sel.dataset.cue : null,
      editorStart: (document.querySelector('#cue-start') || {}).value || null,
      editorDesign: (document.querySelector('#cue-design') || {}).value || null,
      duration: SIM.app.getState().show.duration
    };

    // ---- EDIT CUE's own link, relative to the cue it has open ----
    var link = document.querySelector('#cue-append');
    var selId = out.appended.fresh.length ? out.appended.fresh[0].id : null;
    var selCue = cuesOf(key).filter(function (c) { return String(c.id) === selId; })[0];
    var idsBefore = cuesOf(key).map(function (c) { return String(c.id); });
    out.editLink = { present: !!link, tag: link ? link.tagName : null,
                     text: link ? link.textContent.trim() : null,
                     html: link ? link.outerHTML : null,
                     underDesignRow: !!(link && link.closest('.cue-append') &&
                       link.closest('.cue-append').previousElementSibling &&
                       link.closest('.cue-append').previousElementSibling.className === "cue-design"),
                     from: selCue ? { complete: selCue.complete, design: selCue.design } : null };
    if (link) {
      link.click();
      var grown = cuesOf(key).filter(function (c) { return idsBefore.indexOf(String(c.id)) < 0; });
      out.editLink.fresh = grown.map(function (c) { return { at: c.at, design: c.design }; });
    }

    var saved = JSON.parse(JSON.stringify(SIM.app.getProject()));

    // ---- the hover ghost, on a project rigged to HAVE empty track space ----
    // In the starter show every track is covered end to end (a cue is held
    // until the next one on that item, and the last one until the end of the
    // show), which is exactly why "click an empty spot" was so hard to find.
    // One cue, moved to 2.00, leaves 0.00-2.00 empty in front of it and a
    // band behind it - both cases on the same track.
    var rigged = JSON.parse(JSON.stringify(saved));
    var firstKey = document.querySelector('.tl-track').dataset.track;
    var keep = rigged.show.cues.filter(function (c) { return c.item === firstKey; })[0];
    keep.at = 120;
    rigged.show.cues = [keep];
    rigged.show.duration = Math.max(rigged.show.duration, 300);
    SIM.app.setProject(rigged);
    var track = document.querySelector('.tl-track[data-track="' + firstKey + '"]');
    var spot = emptySpot(track);
    out.ghostEmpty = { spot: !!spot, item: firstKey };
    if (spot) {
      move(track, spot);
      out.ghostEmpty.ghost = ghostInfo();
      out.ghostEmpty.want = snapped(track, spot.x);
      out.ghostEmpty.cursor = getComputedStyle(track).cursor;
      out.ghostEmpty.design = (function () {
        var used = {}; cuesOf(firstKey).forEach(function (c) { used[c.design] = 1; });
        var ds = itemNamed(firstKey).designs;
        var d = ds.filter(function (x) { return !used[x.name]; })[0] || ds[0];
        return d ? (d.label || d.name) : null;
      })();
    }
    // The label flips to the other side of the band when it would otherwise
    // run past the end of the track - measured, not guessed from the time
    // (review F2), so sweep the width and check it never hangs out.
    out.ghostSweep = [0.05, 0.4, 0.75, 0.9, 0.97, 0.999].map(function (f) {
      var tb = track.getBoundingClientRect();
      move(track, { x: tb.left + tb.width * f, y: tb.top + tb.height / 2 });
      var lab = document.querySelector('.cue-ghost-label');
      var lb = lab.getBoundingClientRect();
      return { at: f, flipped: lab.className.indexOf("flip") >= 0,
               past: +(lb.right - tb.right).toFixed(1), before: +(tb.left - lb.left).toFixed(1) };
    });

    // ...never over an existing cue.
    var hold = track.querySelector('.cue-hold');
    var hb = hold.getBoundingClientRect();
    move(hold, { x: hb.left + Math.min(4, hb.width / 2), y: hb.top + hb.height / 2 });
    out.ghostOverBand = ghostInfo();
    // ...and never while a cue is being dragged over that same empty space.
    var drag = track.querySelector('.cue-hold[data-drag="1"]');
    out.ghostDuringDrag = { dragged: !!(drag && spot) };
    if (drag && spot) {
      var db = drag.getBoundingClientRect();
      var start = { x: db.left + 3, y: db.top + db.height / 2 };
      drag.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true, cancelable: true,
                                                           clientX: start.x, clientY: start.y, pointerId: 1 }));
      move(track, spot);
      out.ghostDuringDrag.ghost = ghostInfo();
      document.dispatchEvent(new PointerEvent("pointerup", { bubbles: true,
                                                             clientX: start.x, clientY: start.y, pointerId: 1 }));
    }

    // ---- a fractional refresh still appends a LEGAL cue ----
    // 7.4 s is an ordinary Default refresh time, and it puts the model's own
    // floor (validate()'s refresh + sweep + gap after the previous send, i.e.
    // the previous cue's complete + 1.0) on a .4 second. Rounding to nearest
    // would land 0.4 s early and the model would flag the cue the designer
    // just asked for: "only 8.0 s after the previous send; at least 8.4 s is
    // needed" (review F1).
    var frac = JSON.parse(JSON.stringify(saved));
    var fkey = firstKey;
    var fkeep = frac.show.cues.filter(function (c) { return c.item === fkey; })[0];
    fkeep.at = 60;
    fkeep.refresh_s = null;                 // so it takes the show default below
    frac.show.cues = [fkeep];
    frac.show.refresh_s = 7.4;
    frac.show.duration = Math.max(frac.show.duration, 300);
    SIM.app.setProject(frac);
    var fbefore = cuesOf(fkey);
    var flast = fbefore[fbefore.length - 1];
    var fids = fbefore.map(function (c) { return String(c.id); });
    document.querySelector('button.tl-add[data-add="' + fkey + '"]').click();
    var fnew = cuesOf(fkey).filter(function (c) { return fids.indexOf(String(c.id)) < 0; })[0];
    out.fractional = {
      refresh: SIM.app.getState().show.refresh_s,
      lastAt: flast.at, lastComplete: flast.complete,
      at: fnew ? fnew.at : null,
      problems: fnew ? (fnew.problems || []).map(String) : null,
      track: cuesOf(fkey).map(function (c) { return { at: c.at, problems: (c.problems || []).map(String) }; })
    };

    // ---- the empty states, on a copy of the project with no cues ----
    var blank = JSON.parse(JSON.stringify(saved));
    blank.show.cues = [];
    SIM.app.setProject(blank);
    out.empty = {
      tracks: document.querySelectorAll('.tl-track').length,
      inTrack: [].slice.call(document.querySelectorAll('.tl-track .tl-empty')).map(
        function (e) { return e.textContent.trim(); }),
      firstHint: (document.querySelector('.tl-firsthint') || {}).textContent || null,
      firstHintHtml: (document.querySelector('.tl-firsthint') || {}).outerHTML || null
    };
    SIM.app.setProject(saved);
    publish();
  }
  var tries = 0;
  var timer = setInterval(function () {
    if (!ready() && ++tries < 200) return;
    clearInterval(timer);
    try { run(); } catch (e) { out.error = String((e && e.stack) || e); publish(); }
  }, 50);
})();
</script>
"""


@pytest.fixture(scope="module")
def append_probe(tmp_path_factory):
    """One headless run of the real built page for every test below - the
    same trick as item_csv_probe: booting the page and parsing the starter
    CSVs costs seconds, and these all ask about the same Timeline tab."""
    tmp = tmp_path_factory.mktemp("append")
    _require_browser(tmp)
    assert DIST.exists(), "dist/az27ss-simulator.html has not been built yet"
    return _probe_page(tmp, _APPEND_PROBE, "append-out")


def test_every_track_row_offers_its_own_append_button(append_probe):
    data = append_probe
    assert data["trackItems"], "no tracks were drawn at all"
    # Sets, not lists: the rows are ordered by LOOK (orderByLook), the state
    # holds the garments in file order - what matters here is that no track
    # is missing a "+" and no "+" belongs to a garment with no track.
    assert sorted(r["item"] for r in data["rows"]) == sorted(data["trackItems"]), \
        "the track rows and the items with a map do not line up"
    for row in data["rows"]:
        assert row["has"], f'{row["item"]}: no "+" at the end of its row'
        # A real <button>: in the tab order and answering Enter/Space with no
        # handler of its own (and type="button", so it can never submit).
        assert row["tag"] == "BUTTON", f'{row["item"]}: {row["tag"]!r}'
        assert row["type"] == "button", f'{row["item"]}: type={row["type"]!r}'
        assert row["focusable"], f'{row["item"]}: the "+" cannot be focused'
        assert not row["disabled"], f'{row["item"]}: the "+" is disabled'
        assert row["text"] == "+", f'{row["item"]}: {row["text"]!r}'
        assert row["forItem"] == row["item"], \
            f'{row["item"]}: the button carries {row["forItem"]!r} instead'
        assert row["lastInRow"], f'{row["item"]}: the "+" is not at the right-hand end'
        # It says what it does, on hover and to a screen reader.
        assert "next design" in row["title"], f'{row["item"]}: {row["title"]!r}'
        assert row["aria"] and row["aria"].endswith(row["title"]), \
            f'{row["item"]}: {row["aria"]!r}'


def test_the_hint_above_the_tracks_names_both_ways_in(append_probe):
    hint = append_probe["hint"]
    assert hint, "the hint line above the tracks is gone"
    assert "Click anywhere on a track" in hint
    assert "+" in hint and "next design" in hint


def test_pressing_append_adds_the_next_design_one_second_after_the_last(append_probe):
    data = append_probe["appended"]
    assert data["countAfter"] == data["countBefore"] + 1, data
    assert len(data["fresh"]) == 1, data["fresh"]
    fresh = data["fresh"][0]
    # The earliest legal moment: the last cue's picture is finished at its
    # complete time (start + its refresh + its sweep span), then a one-second
    # gap, rounded UP to a whole second - complete + gap is the model's own
    # floor (validate()'s refresh + sweep + gap), not a target to land near,
    # so rounding to nearest would fall below it for any refresh whose
    # fraction is under .5 (see the fractional-refresh test below).
    assert fresh["at"] == math.ceil(data["lastBefore"]["complete"] + 1), \
        f'{fresh["at"]} is not ceil({data["lastBefore"]["complete"]} + 1 s)'
    assert fresh["at"] > data["lastBefore"]["complete"]
    assert fresh["at"] == int(fresh["at"]), "the appended cue is not on a whole second"
    assert not fresh["partial"]
    # ...showing the NEXT design in the item's own rotation, wrapping round.
    designs = data["designs"]
    nxt = designs[(designs.index(data["lastBefore"]["design"]) + 1) % len(designs)]
    assert fresh["design"] == nxt, \
        f'appended {fresh["design"]!r}, the rotation after {data["lastBefore"]["design"]!r} is {nxt!r}'
    # ...and it is what EDIT CUE now has open.
    assert data["selectedBand"] == fresh["id"], \
        f'the selected cue is {data["selectedBand"]!r}, not the one just added'
    assert data["editorDesign"] == fresh["design"], data
    minutes, seconds = divmod(int(fresh["at"]), 60)
    assert data["editorStart"] == f"{minutes}.{seconds:02d}", \
        f'EDIT CUE shows Start {data["editorStart"]!r} for a cue at {fresh["at"]} s'
    # The show is long enough to hold what was just appended.
    assert data["duration"] > fresh["at"], data


def test_edit_cue_appends_the_next_design_after_the_cue_it_has_open(append_probe):
    link = append_probe["editLink"]
    assert link["present"], "EDIT CUE has no append link"
    assert link["tag"] == "BUTTON", link["tag"]          # focusable, Enter/Space for free
    assert link["text"] == "＋ Add next design after this cue", link["text"]
    assert link["underDesignRow"], "the link is not directly under the Design row"
    assert len(link["fresh"]) == 1, link["fresh"]
    assert link["fresh"][0]["at"] == math.ceil(link["from"]["complete"] + 1), link


def test_a_fractional_refresh_still_appends_a_cue_the_model_accepts(append_probe):
    frac = append_probe["fractional"]
    assert frac["refresh"] == 7.4, frac
    assert frac["at"] == math.ceil(frac["lastComplete"] + 1), frac
    # The case is only worth anything while it tells the two apart.
    assert frac["at"] != round(frac["lastComplete"] + 1), \
        "7.4 s no longer distinguishes rounding up from rounding to nearest"
    # The point of the whole thing: what the "+" places is legal. Rounding to
    # nearest put it 0.4 s inside the model's floor and validate() said so.
    assert frac["problems"] == [], frac
    assert all(c["problems"] == [] for c in frac["track"]), frac["track"]


def test_the_hover_ghost_shows_what_a_click_would_place(append_probe):
    empty = append_probe["ghostEmpty"]
    assert empty["spot"], "the first track had no empty space to hover over"
    ghost = empty["ghost"]
    assert ghost, "no .cue-ghost after a pointermove over empty track space"
    assert int(ghost["at"]) == empty["want"], \
        f'the ghost sits at {ghost["at"]}, the snapped time is {empty["want"]}'
    minutes, seconds = divmod(empty["want"], 60)
    assert ghost["label"] == f'+ {empty["design"]} at {minutes}.{seconds:02d}', ghost["label"]
    assert ghost["left"].endswith("%") and ghost["width"].endswith("%"), ghost
    assert ghost["track"] == append_probe["rows"][0]["item"], ghost
    # The pointer already says "this click puts something here".
    assert empty["cursor"] == "copy", empty["cursor"]
    # Wherever it is on the track, the label stays on the track: it hangs off
    # the right of the band and flips to its left when it would not fit. The
    # flip is measured, so a long design name flips earlier than a short one
    # and neither runs past the card (review F2).
    sweep = append_probe["ghostSweep"]
    assert sweep and any(s["flipped"] for s in sweep) and any(not s["flipped"] for s in sweep), \
        f"the label never flips, or always does: {sweep}"
    for s in sweep:
        assert s["past"] <= 0, f'the label hangs {s["past"]} px past the track at {s["at"]}: {sweep}'


def test_the_ghost_stays_away_from_existing_cues_and_from_drags(append_probe):
    assert append_probe["ghostOverBand"] is None, \
        f'a ghost was drawn on top of an existing cue: {append_probe["ghostOverBand"]}'
    drag = append_probe["ghostDuringDrag"]
    assert drag["dragged"], "the drag case never ran"
    assert drag.get("ghost") is None, \
        f'a ghost was drawn during a cue drag: {drag["ghost"]}'


def test_a_project_with_nothing_placed_says_where_to_start(append_probe):
    empty = append_probe["empty"]
    assert empty["tracks"] > 0
    # Every empty track carries the invitation, inside the track itself.
    assert len(empty["inTrack"]) == empty["tracks"], empty
    for text in empty["inTrack"]:
        assert text == "click here to place the first design at 0.00", repr(text)
    # ...and the CUES card says the same thing, full size, instead of a table.
    assert empty["firstHint"], "an empty project's CUES card says nothing"
    assert "first design at 0.00" in " ".join(empty["firstHint"].split())
    assert "+" in empty["firstHint"]
