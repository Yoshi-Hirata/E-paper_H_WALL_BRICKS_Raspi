"""Build dist/az27ss-simulator.html: one self-contained file the designers can
double-click, no server, no install (plan_designer_sim.md §1.1, §5).

A dumb, generic inliner: it walks conductor/web/designer.html's own
`<link rel="stylesheet" href="...">` and `<script src="...">` tags IN
DOCUMENT ORDER (that order is the module dependency order - see designer.html's
own header comment) and replaces each with its file's contents inlined. It
does not understand JS or CSS - it only requires those files to keep meaning
the same thing once embedded as text between HTML tags, which is why closing
tags inside the inlined text are escaped rather than parsed around.

Two tags are treated specially, both on purpose:
  * a script carrying `data-stub="dev-only"` (conductor/web/sim/model.stub.js,
    a throwaway stand-in used only while Coder P's real model.js/state.js
    were not yet on this branch) is never inlined - the stub must not ship
    (plan_designer_sim.md §6). designer.html no longer references one, but
    the guard stays: a stray future stub script must still never ship
    silently.
  * `sim/starter.js` (the committed CSVs) is skipped when --no-starter is
    given - "the way out" the plan asks for, for a build without the ~ hundred
    KB of starter data.
  * `sim/goldens.js` and `sim/selftest.js` are skipped together UNLESS
    --with-goldens is given (adversarial review round 2, "DIST SIZE"; made
    the default the review's third pass asked for - "the shipped variant
    IS the default, dev build opts in"): the committed
    dist/az27ss-simulator.html is the SHIPPED variant - goldens.js alone is
    over half the page's weight, all of it Python-cross-check data a
    designer's own double-click never needs, only tools/make_goldens.py's
    own dev/CI harness (test_sim_goldens.py's test_browser_selftest_passes,
    which never goes through this script at all) and the Help tab's "Run
    self-test" button, which already says plainly when that button is not
    available in whichever build is running. designer.html (the dev page,
    plain <script src> tags, not run through this script at all) keeps
    loading both normally regardless - --with-goldens only affects what
    build_designer.py itself inlines. Get it wrong here and the plain
    (no-flags) build silently overwrites the committed 550 KB dist with a
    1.1 MB one, since without this default `python tools/build_designer.py`
    and its own `--check` would disagree with what is actually committed.

A fourth special case is the music, and it is the only thing this script adds
rather than merely inlines. `--music PATH|auto|none` embeds the show's audio
file as one extra `<script>`, emitted immediately BEFORE the first inlined
module (so `SIM.embeddedMusic` is already there whichever way the page's own
boot() ends up being scheduled - `defer` in the dev page, plain inline order
in the built one):

    globalThis.SIM.embeddedMusic = {name, type, size, dataUrl}

`dataUrl` is a `data:<type>;base64,...` URL; the page decodes it exactly once
into a Blob and drops the string (designer-app.js's builtInMusicUrl()). This
is the answer to "the file the director's team double-clicks is silent": a
browser cannot reach out to a file on disk on its own, so the only way the
music can be there out of the box is for it to BE in the file. The audio is
not re-encoded (there is no ffmpeg on the build machine and none is wanted -
re-encoding the show's master is not this script's business), so a 17.5 MB MP3
costs ~23.4 MB of base64. That is fine for a file someone opens off their own
disk, and hopeless as a committed artefact - hence two outputs:

  * dist/az27ss-simulator.html            the LEAN page, no music, committed,
                                          still under the 2 MB budget; what a
                                          flagless build and --check mean.
  * dist/az27ss-simulator-with-music.html the music build's default output,
                                          gitignored, never committed.

The operator never runs this script: the Conductor's Timeline toolbar has a
"Simulator for designers…" button that calls build_page() in-process against
whatever music is loaded right now (conductor/server.py's /api/simulator).
That is deliberate - the music WILL change, and the answer to "the music
changed" must not be "ask a developer".

After assembly the output is checked for self-containment (no http(s):// URL,
no @import, no <link>, no external src/srcset anywhere but the one named
exception below - a data: URL is explicitly fine, which is what makes the
embedded music legal) and, for a build with no music, the 2 MB size budget;
every inlined file is scanned for stray control characters (a literal NUL or
similar has no business inside HTML/CSS/JS text and is refused with the
offending file and line number). The generated music script is not run
through that scan or through the </script> escaping: base64's alphabet
(A-Za-z0-9+/=) contains no control character, no "<" and no "&", so there is
nothing in it to escape - and running two 23 MB regex substitutions over it
would cost seconds for provably no effect. Its only free-form parts, the
name and the type, ARE escaped (JSON plus <). --check rebuilds the LEAN
page into memory and diffs against the committed dist file without writing
anything (tests/test_designer_build.py).

Usage: build_designer.py [--source PATH] [--out PATH] [--no-starter]
                         [--with-goldens] [--check]
                         [--music PATH|auto|none] [--workspace PATH]
    (plain, no flags: builds the SHIPPED variant - what dist/ actually is)
    --with-goldens: builds the DEV/CI variant instead (adds goldens.js +
                    selftest.js, ~560 KB heavier) - never committed as dist/
    --music auto:   the music named in <workspace>/show.json, read from
                    <workspace>/music/ (--workspace defaults to ./showdata)
"""
import argparse
import base64
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = REPO / "conductor" / "web" / "designer.html"
DEFAULT_OUT = REPO / "dist" / "az27ss-simulator.html"
MUSIC_OUT = REPO / "dist" / "az27ss-simulator-with-music.html"
DEFAULT_WORKSPACE = Path("showdata")
SIZE_BUDGET = 2 * 1024 * 1024

# Deliberately a copy of conductor/server.py's _MUSIC_TYPES rather than an
# import: this script is a standalone stdlib tool (it must run from a bare
# checkout with nothing importable), and conductor/server.py imports THIS
# module for /api/simulator - importing back the other way would tie the two
# into a cycle for the sake of five lines of table.
MUSIC_TYPES = {".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg",
               ".m4a": "audio/mp4", ".aac": "audio/aac", ".flac": "audio/flac"}


def music_type(name: str) -> str:
    return MUSIC_TYPES.get(Path(name).suffix.lower(), "application/octet-stream")


# RFC 9110's `token` characters on both sides of the slash, and nothing
# else. The MIME goes into a data: URL as raw text - it cannot be quoted or
# escaped there, because the ";base64," that follows is URL syntax, not a
# string - so it is the one value in the generated script that has to be
# refused rather than escaped. It arrives from show.json, which is a file a
# person can hand-edit, and `audio/mpeg";x="` in it would otherwise close
# the dataUrl literal and let the rest run as code.
_MIME_RE = re.compile(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+")


def safe_mime(mime: str, name: str) -> str:
    """`mime` if it is a plain type/subtype, otherwise the one the file
    name implies. Silently falling back beats failing the build: a broken
    `type` in show.json is the operator's typo, not a reason they cannot
    hand the director's team a file, and the extension is the better guess
    anyway."""
    mime = (mime or "").strip()
    if _MIME_RE.fullmatch(mime):
        return mime
    return music_type(name)

LINK_RE = re.compile(r'<link\b([^>]*?)href="([^"]+)"([^>]*?)/?>', re.IGNORECASE)
# src accepts a quoted (single or double) or bare (no space, no ">") value -
# `\bsrc="..."` alone missed e.g. <script src=sim/foo.js> (technically legal
# HTML, and cheap for a hostile or careless edit to slip past a quote-only
# check unnoticed).
SRC_VALUE = r'src\s*=\s*("[^"]*"|\'[^\']*\'|[^\s>]+)'
SCRIPT_RE = re.compile(r'<script\b([^>]*?)' + SRC_VALUE + r'([^>]*?)></script>', re.IGNORECASE)
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
SVG_XMLNS_RE = re.compile(r'xmlns\s*=\s*"http://www\.w3\.org/2000/svg"')


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _refuse_if_unsafe(path: str, tag: str):
    if path.startswith(("http://", "https://", "//", "/")):
        raise ValueError(f"{tag} references an external/absolute path: {path!r}")
    if ".." in Path(path).parts:
        raise ValueError(f"{tag} references a path outside its folder: {path!r}")


def _refuse_control_chars(text: str, name: str):
    m = CONTROL_RE.search(text)
    if not m:
        return
    line = text.count("\n", 0, m.start()) + 1
    col = m.start() - text.rfind("\n", 0, m.start())
    raise ValueError(
        f"{name}:{line}:{col}: contains a stray control character "
        f"(0x{ord(m.group()):02x}) - not safe to inline into HTML/JS text "
        f"(fix the source file; a JS string separator should use a plain "
        f"printable character, never a raw control byte)")


def _escape_close(text: str, tag: str) -> str:
    # </script> or </style> inside the inlined text would end the wrapping
    # tag early; neither CSS nor JS legitimately contains that sequence
    # outside a string, and even there this keeps the browser's parser from
    # ever seeing it.
    return re.sub(r"</(" + tag + r")", r"<\\/\1", text, flags=re.IGNORECASE)


def _escape_script_hazards(text: str) -> str:
    # Beyond the plain </script> case above: HTML5's script-parsing state
    # machine enters "script data double escaped" the moment it sees a
    # literal "<!--" followed later by a literal "<script" INSIDE a script
    # element - and once there, a single "</script" (even one this file
    # already escaped as a distinct string) no longer closes the element on
    # its own. Escaping "<!--" and "<script" too means that state can never
    # be entered in the first place, so the earlier </script> escaping stays
    # sufficient no matter what the inlined JS happens to contain.
    text = _escape_close(text, "script")
    text = re.sub(r"<!--", r"<\\!--", text)
    text = re.sub(r"<script", r"<\\script", text, flags=re.IGNORECASE)
    return text


COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def _js_string(value: str) -> str:
    """A JS string literal safe to sit inside a <script> element: JSON
    quoting for the usual suspects, plus \\u003c for "<" so no music file
    called `</script>foo.mp3` (or `<!--`, or `<script`) can ever end the
    element early or push HTML5's parser into its double-escaped state."""
    return json.dumps(value, ensure_ascii=False).replace("<", "\\u003c")


def music_script(data: bytes, name: str, mime: str) -> str:
    """The one <script> that carries the show's audio (see module docstring).

    `size` is the real byte count of the audio, not of the base64 - it is
    what the page shows and what a test can compare against the file on
    disk; getting that wrong by 4/3 would be a quiet, plausible-looking
    lie."""
    mime = safe_mime(mime, name)
    encoded = base64.b64encode(data).decode("ascii")
    # The base64 body is concatenated in AFTER the escaping below, not run
    # through it: its alphabet (A-Za-z0-9+/=) provably contains no "<" and
    # no control character, so there is nothing in it for the escaping to
    # find - and three regex passes over 23 MB to prove that again on every
    # build is a second of nothing. Everything with a person's text in it
    # (the name, the type) IS escaped, exactly as an inlined module is.
    opening = ("/* the show's music, embedded by "
               "tools/build_designer.py --music */\n"
               "globalThis.SIM = Object.assign(globalThis.SIM || {}, "
               "{ embeddedMusic: {\n"
               f"  name: {_js_string(name)},\n"
               f"  type: {_js_string(mime)},\n"
               f"  size: {len(data)},\n"
               f'  dataUrl: "data:{mime};base64,')
    closing = '"\n} });\n'
    # The <script> tags themselves stay outside the escaping, or it would
    # turn the closing tag into text and the element would never end.
    opening = _escape_script_hazards(opening)
    _refuse_control_chars(opening, "the embedded music's name/type")
    return "<script>" + opening + encoded + _escape_script_hazards(closing) + "</script>"


def build(source: Path, no_starter: bool, with_goldens: bool = False,
          music_tag: str = "") -> "tuple[str, list[tuple[str, int]]]":
    # read_bytes().decode(), not read_text() (adversarial review round 2 -
    # F7): read_text() does universal-newline translation, so a source file
    # already corrupted to CRLF (a bad checkout, core.autocrlf=true) would
    # silently come back LF-only here, and --check below would then have
    # nothing left to catch it against - decoding bytes ourselves preserves
    # whatever line endings are really on disk.
    html = source.read_bytes().decode("utf-8")
    # Strip HTML comments first: designer.html documents the model.js/state.js
    # swap (see its own header comment) using literal-looking <script> text
    # that must never be mistaken for a real tag to inline.
    html = COMMENT_RE.sub("", html)
    base = source.parent
    sizes: "list[tuple[str, int]]" = []

    def inline_link(m: "re.Match") -> str:
        pre, href, post = m.groups()
        if "stylesheet" not in (pre + post).lower():
            return m.group(0)
        _refuse_if_unsafe(href, "<link>")
        text = (base / href).read_bytes().decode("utf-8")
        _refuse_control_chars(text, href)
        sizes.append((href, len(text.encode("utf-8"))))
        return f"<style>{_escape_close(text, 'style')}</style>"

    # The music goes in FRONT of the first module this loop inlines, not at
    # the end of <body>: the dev page's own scripts are `defer`, so
    # designer-app.js's boot() runs the moment that file executes, while in
    # the built page (plain inline <script>s) boot() waits for
    # DOMContentLoaded instead. Emitting the music first is the one position
    # that is correct under BOTH schedules, so the page never has to cope
    # with SIM.embeddedMusic arriving late.
    pending_music = [music_tag]

    def inline_script(m: "re.Match") -> str:
        pre, raw_src, post = m.groups()
        src = _unquote(raw_src)
        attrs = pre + post
        if "data-stub" in attrs.lower():
            return ""          # the dev-only stub never ships (see module docstring)
        if no_starter and Path(src).name == "starter.js":
            return ""
        if not with_goldens and Path(src).name in ("goldens.js", "selftest.js"):
            return ""
        _refuse_if_unsafe(src, "<script>")
        text = (base / src).read_bytes().decode("utf-8")
        _refuse_control_chars(text, src)
        sizes.append((src, len(text.encode("utf-8"))))
        first, pending_music[0] = pending_music[0], ""
        return f"{first}<script>{_escape_script_hazards(text)}</script>"

    html = LINK_RE.sub(inline_link, html)
    html = SCRIPT_RE.sub(inline_script, html)
    if pending_music[0]:
        # No module was inlined at all (a --source with nothing but the
        # music, or every script skipped by the flags): refusing beats
        # silently dropping the audio the caller asked for.
        raise ValueError("--music was given but the page has no script to "
                         "embed it in front of")
    return html, sizes


def build_page(source: Path, *, starter: bool = True, goldens: bool = False,
               music: "bytes | None" = None, music_name: str = "",
               music_type: str = "") -> str:
    """The whole page as text: assemble, embed the music if there is any,
    and refuse anything that is not self-contained.

    This is the importable entry point - conductor/server.py's
    /api/simulator calls it directly so the operator can rebuild the
    director's copy from the Conductor's own page the moment the music
    changes, with no checkout and no Python on their part. main() below is
    a thin wrapper over it that adds the CLI's flags, the size budget and
    the writing of files."""
    tag = ""
    if music is not None:
        if not music_name:
            raise ValueError("music needs a file name")
        tag = music_script(music, music_name, music_type)
    html, _sizes = build(source, no_starter=not starter, with_goldens=goldens,
                         music_tag=tag)
    check_self_contained(html)
    return html


def check_self_contained(html: str):
    # The one allowed exception: the SVG XML namespace URI
    # (xmlns="http://www.w3.org/2000/svg", verbatim from index.html's own
    # renderGarment() - see render.js's header) is an XML namespace name, not
    # a network resource; nothing ever fetches it. Anchored to the exact
    # attribute form (not a blanket string removal) so this can never mask a
    # genuine external reference that merely contains the same substring.
    stripped = SVG_XMLNS_RE.sub("", html)
    if re.search(r'https?://', stripped):
        raise ValueError("build is not self-contained: an http(s):// URL remains")
    # Protocol-relative ("//host/path", no scheme - the browser resolves it
    # against the page's own) in any of the three places a URL can hide:
    # CSS's url(), an href attribute, or a src attribute (adversarial
    # review round 2 - F10). The https?:// check above cannot see this form
    # at all, and it fetches exactly as eagerly as an explicit https:// one.
    if re.search(r'(?:url\(\s*[\'"]?|\bhref\s*=\s*[\'"]?|\bsrc\s*=\s*[\'"]?)//', html, re.IGNORECASE):
        raise ValueError("build is not self-contained: a protocol-relative // URL remains")
    if re.search(r'@import\s+(url\(|["\'])', html, re.IGNORECASE):
        raise ValueError("build is not self-contained: a CSS @import remains")
    if re.search(r'<link\b', html, re.IGNORECASE):
        raise ValueError("build is not self-contained: a <link> tag remains")
    # Scoped to an actual HTML tag (<tagname ... >), not just "src=" or
    # "srcset=" anywhere in the file - the inlined JS legitimately contains
    # plain assignments like `player.src = url;` (transport.js), which is
    # code, not a tag attribute, and must not trip this check.
    TAG_RE = re.compile(r'<[a-zA-Z][a-zA-Z0-9-]*\b[^>]*>')
    for m in TAG_RE.finditer(html):
        tag = m.group(0)
        if re.search(r'\bsrcset\s*=', tag, re.IGNORECASE):
            raise ValueError(f"build is not self-contained: a srcset attribute remains ({tag[:80]!r})")
        src_m = re.search(SRC_VALUE, tag, re.IGNORECASE)
        if src_m:
            value = _unquote(src_m.group(1))
            if not value.startswith("data:"):
                raise ValueError(f"build is not self-contained: external src={value!r} remains")


def workspace_music(workspace: Path) -> "tuple[bytes, str, str] | None":
    """The music the workspace's show.json names, as (bytes, name, type) -
    or None when the show has no music, or names a file that is not there.

    The same "the entry AND the file" rule as conductor/server.py's
    Workspace.music_info(): a show.json left pointing at an audio file
    somebody deleted must read as "no music", not blow up a build."""
    try:
        show = json.loads((Path(workspace) / "show.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    info = show.get("music")
    if not isinstance(info, dict) or not info.get("name"):
        return None
    name = str(info["name"])
    # Path(...).name, as the server does everywhere it turns a show.json
    # name back into a path: show.json is a file on disk that a person can
    # edit, so a name carrying "../.." must not read outside music/.
    path = Path(workspace) / "music" / Path(name).name
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return data, name, str(info.get("type") or "") or music_type(name)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--no-starter", action="store_true")
    ap.add_argument("--with-goldens", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--music", default=None, metavar="PATH|auto|none",
                    help="embed an audio file: a path, or 'auto' for the one "
                         "--workspace's show.json names, or 'none'")
    ap.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE,
                    help="where 'auto' looks for show.json and music/ "
                         "(default: ./showdata)")
    args = ap.parse_args()

    music = name = mime = None
    if args.music and args.music != "none":
        if args.music == "auto":
            found = workspace_music(args.workspace)
            if found is None:
                print(f"build_designer --music auto: {args.workspace}/show.json "
                      f"names no music, or its file is missing from "
                      f"{args.workspace}/music/", file=sys.stderr)
                return 1
            music, name, mime = found
        else:
            path = Path(args.music)
            try:
                music = path.read_bytes()
            except OSError as exc:
                print(f"build_designer --music: {exc}", file=sys.stderr)
                return 1
            name, mime = path.name, music_type(path.name)

    # The default output depends on what is being built, and the two must
    # never swap: a music build landing on dist/az27ss-simulator.html would
    # replace the committed 550 KB page with a 23 MB one that --check then
    # reports as stale forever (and that somebody eventually commits).
    out = args.out or (MUSIC_OUT if music is not None else DEFAULT_OUT)

    html, sizes = build(args.source, args.no_starter, args.with_goldens,
                        music_tag="" if music is None
                                  else music_script(music, name, mime))
    check_self_contained(html)
    total = len(html.encode("utf-8"))
    # The budget is about the COMMITTED page (what a designer downloads from
    # a repo they cannot build), so it applies to the lean build only - the
    # music build is inherently ~23 MB per 17.5 MB of audio and is never
    # committed. --check is lean-only for the same reason.
    if music is None and total > SIZE_BUDGET:
        print(f"build_designer: {total:,} bytes exceeds the {SIZE_BUDGET:,} byte budget", file=sys.stderr)
        return 1

    print(f"{'file':<40}{'bytes':>10}")
    for label, n in sizes:
        print(f"{label:<40}{n:>10,}")
    if music is not None:
        print(f"{'music: ' + name:<40}{len(music):>10,}")
    print(f"{'TOTAL (' + out.name + ')':<40}{total:>10,}")

    if args.check:
        # --check is about the committed page only (see the module
        # docstring): the with-music build is regenerated on demand from
        # whatever music is loaded that day and has nothing to be stale
        # against, so asking is a mistake worth naming rather than a
        # comparison worth attempting.
        if music is not None:
            print("build_designer --check: --check is for the committed lean "
                  "page; it has nothing to compare a --music build against",
                  file=sys.stderr)
            return 1
        if not out.exists():
            print(f"build_designer --check: {out} does not exist", file=sys.stderr)
            return 1
        # read_bytes(), compared against the freshly-built html re-encoded
        # to bytes - not read_text() (adversarial review round 2 - F7): a
        # dist/ corrupted to CRLF by a bad checkout would otherwise compare
        # equal to the correct "\n"-only rebuild every time, so --check
        # could never catch it (and CI would never notice a phantom-
        # modified dist/ either).
        current = out.read_bytes()
        if current != html.encode("utf-8"):
            print(f"build_designer --check: {out} is stale - run "
                  "`python tools/build_designer.py`", file=sys.stderr)
            return 1
        print("build_designer --check: up to date")
        return 0

    out.parent.mkdir(parents=True, exist_ok=True)
    # write_bytes, not write_text(..., newline="\n") - that parameter needs
    # Python 3.10+ and this tool promises 3.9 (plan_designer_sim.md: "Python
    # 3.9 stdlib"). `html` only ever contains "\n" (every source file it
    # reads is itself LF-only), so encoding straight to bytes is exact.
    out.write_bytes(html.encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
