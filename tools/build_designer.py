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
    Coder P's real model.js/state.js not landed yet during solo development)
    is never inlined - the stub must not ship (plan_designer_sim.md §6).
  * `sim/starter.js` (the committed CSVs) is skipped when --no-starter is
    given - "the way out" the plan asks for, for a build without the ~ hundred
    KB of starter data.

After assembly the output is checked for self-containment (no http(s)://, no
<link, no non-data src anywhere) and for the 2 MB size budget; both failures
abort the build. --check rebuilds into memory and diffs against the
committed dist file without writing anything (tests/test_designer_build.py).

Usage: build_designer.py [--source PATH] [--out PATH] [--no-starter] [--check]
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
SCRIPT_RE = re.compile(r'<script\b([^>]*?)src="([^"]+)"([^>]*?)></script>', re.IGNORECASE)


def _refuse_if_unsafe(path: str, tag: str):
    if path.startswith(("http://", "https://", "//", "/")):
        raise ValueError(f"{tag} references an external/absolute path: {path!r}")
    if ".." in Path(path).parts:
        raise ValueError(f"{tag} references a path outside its folder: {path!r}")


def _escape_close(text: str, tag: str) -> str:
    # </script> or </style> inside the inlined text would end the wrapping
    # tag early; neither CSS nor JS legitimately contains that sequence
    # outside a string, and even there this keeps the browser's parser from
    # ever seeing it.
    return re.sub(r"</(" + tag + r")", r"<\\/\1", text, flags=re.IGNORECASE)


COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def build(source: Path, no_starter: bool) -> "tuple[str, list[tuple[str, int]]]":
    html = source.read_text(encoding="utf-8")
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
        text = (base / href).read_text(encoding="utf-8")
        sizes.append((href, len(text.encode("utf-8"))))
        return f"<style>{_escape_close(text, 'style')}</style>"

    def inline_script(m: "re.Match") -> str:
        pre, src, post = m.groups()
        attrs = pre + post
        if "data-stub" in attrs.lower():
            return ""          # the dev-only stub never ships (see module docstring)
        if no_starter and Path(src).name == "starter.js":
            return ""
        _refuse_if_unsafe(src, "<script>")
        text = (base / src).read_text(encoding="utf-8")
        sizes.append((src, len(text.encode("utf-8"))))
        return f"<script>{_escape_close(text, 'script')}</script>"

    html = LINK_RE.sub(inline_link, html)
    html = SCRIPT_RE.sub(inline_script, html)
    return html, sizes


def check_self_contained(html: str):
    # The one allowed exception: the SVG XML namespace URI
    # (xmlns="http://www.w3.org/2000/svg", verbatim from index.html's own
    # renderGarment() - see render.js's header) is an XML namespace name, not
    # a network resource; nothing ever fetches it.
    stripped = html.replace("http://www.w3.org/2000/svg", "")
    if re.search(r'https?://', stripped):
        raise ValueError("build is not self-contained: an http(s):// URL remains")
    if re.search(r'<link\b', html, re.IGNORECASE):
        raise ValueError("build is not self-contained: a <link> tag remains")
    for m in re.finditer(r'\bsrc="([^"]*)"', html, re.IGNORECASE):
        if not m.group(1).startswith("data:"):
            raise ValueError(f"build is not self-contained: external src={m.group(1)!r} remains")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--no-starter", action="store_true")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    html, sizes = build(args.source, args.no_starter)
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
        current = args.out.read_text(encoding="utf-8")
        if current != html:
            print(f"build_designer --check: {args.out} is stale - run "
                  "`python tools/build_designer.py`", file=sys.stderr)
            return 1
        print("build_designer --check: up to date")
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(html, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
