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
  * `sim/goldens.js` and `sim/selftest.js` are skipped together when
    --no-goldens is given (adversarial review round 2, "DIST SIZE"): the
    committed dist/az27ss-simulator.html is built this way - goldens.js
    alone is over half the page's weight, all of it Python-cross-check data
    a designer's own double-click never needs, only tools/make_goldens.py's
    own dev/CI harness (test_sim_goldens.py's test_browser_selftest_passes,
    which never goes through this script at all) and the Help tab's "Run
    self-test" button, which already says plainly when that button is not
    available in whichever build is running. designer.html (the dev page)
    keeps loading both normally either way - --no-goldens only affects what
    build_designer.py itself inlines.

After assembly the output is checked for self-containment (no http(s):// URL,
no @import, no <link>, no external src/srcset anywhere but the one named
exception below) and for the 2 MB size budget, and every inlined file is
scanned for stray control characters (a literal NUL or similar has no
business inside HTML/CSS/JS text and is refused with the offending file and
line number); all three abort the build. --check rebuilds into memory and
diffs against the committed dist file without writing anything
(tests/test_designer_build.py).

Usage: build_designer.py [--source PATH] [--out PATH] [--no-starter] [--no-goldens] [--check]
"""
import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = REPO / "conductor" / "web" / "designer.html"
DEFAULT_OUT = REPO / "dist" / "az27ss-simulator.html"
SIZE_BUDGET = 2 * 1024 * 1024

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


def build(source: Path, no_starter: bool, no_goldens: bool = False) -> "tuple[str, list[tuple[str, int]]]":
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

    def inline_script(m: "re.Match") -> str:
        pre, raw_src, post = m.groups()
        src = _unquote(raw_src)
        attrs = pre + post
        if "data-stub" in attrs.lower():
            return ""          # the dev-only stub never ships (see module docstring)
        if no_starter and Path(src).name == "starter.js":
            return ""
        if no_goldens and Path(src).name in ("goldens.js", "selftest.js"):
            return ""
        _refuse_if_unsafe(src, "<script>")
        text = (base / src).read_bytes().decode("utf-8")
        _refuse_control_chars(text, src)
        sizes.append((src, len(text.encode("utf-8"))))
        return f"<script>{_escape_script_hazards(text)}</script>"

    html = LINK_RE.sub(inline_link, html)
    html = SCRIPT_RE.sub(inline_script, html)
    return html, sizes


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--no-starter", action="store_true")
    ap.add_argument("--no-goldens", action="store_true")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    html, sizes = build(args.source, args.no_starter, args.no_goldens)
    check_self_contained(html)
    total = len(html.encode("utf-8"))
    if total > SIZE_BUDGET:
        print(f"build_designer: {total:,} bytes exceeds the {SIZE_BUDGET:,} byte budget", file=sys.stderr)
        return 1

    print(f"{'file':<40}{'bytes':>10}")
    for name, n in sizes:
        print(f"{name:<40}{n:>10,}")
    print(f"{'TOTAL (' + args.out.name + ')':<40}{total:>10,}")

    if args.check:
        if not args.out.exists():
            print(f"build_designer --check: {args.out} does not exist", file=sys.stderr)
            return 1
        # read_bytes(), compared against the freshly-built html re-encoded
        # to bytes - not read_text() (adversarial review round 2 - F7): a
        # dist/ corrupted to CRLF by a bad checkout would otherwise compare
        # equal to the correct "\n"-only rebuild every time, so --check
        # could never catch it (and CI would never notice a phantom-
        # modified dist/ either).
        current = args.out.read_bytes()
        if current != html.encode("utf-8"):
            print(f"build_designer --check: {args.out} is stale - run "
                  "`python tools/build_designer.py`", file=sys.stderr)
            return 1
        print("build_designer --check: up to date")
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    # write_bytes, not write_text(..., newline="\n") - that parameter needs
    # Python 3.10+ and this tool promises 3.9 (plan_designer_sim.md: "Python
    # 3.9 stdlib"). `html` only ever contains "\n" (every source file it
    # reads is itself LF-only), so encoding straight to bytes is exact.
    args.out.write_bytes(html.encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
