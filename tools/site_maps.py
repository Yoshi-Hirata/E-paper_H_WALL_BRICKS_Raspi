"""Rebuild <item>_map.csv files straight from the wiring pages' own data,
with the `shift` column the site's csvMap() does not export but its
drawing code applies: a row with a centre cell ("C" in its addresses,
e.g. "F-3-C") gets shift 0, every other row gets shift 0.5
(vglabjp.synology.me, the production site).

The wiring pages carry their data as a JSON <script>; csvMap() in the
page turns D.links into <item>_map.csv. Same arithmetic here, plus the
added shift column.

Usage: site_maps.py <html-dir> <out-dir>
    <html-dir>  a folder of downloaded "*-wiring.html" pages
    <out-dir>   where to write "<item>_map.csv" (created if missing)
"""
import json
import re
import sys
from pathlib import Path

# The wiring page's own stem (its filename, "-wiring" stripped) does not
# always match the item name our repo uses (showdata/files/<item>_map.csv):
# some pages are named after the LOOK, others after the garment itself.
STEM_ITEM = {
    "look19": "AZ271SD1301",
    "look20": "AZ271SC6302",
    "look20-skirt": "AZ271SB2303",
    "look23": "AZ271SD1306",
    "look24": "AZ271SD1307",
    "az271sd1305b": "AZ271SD1305_B",
    "eink01": "AZ271SD1305",
    # Bags 01-03 (added to the site 2026-09-24): named after the item
    "az271sg1035": "AZ271SG1035",
    "az271sg1036": "AZ271SG1036",
    "az271sg3037": "AZ271SG3037",
}


def main():
    if len(sys.argv) != 3:
        sys.exit("usage: site_maps.py <html-dir> <out-dir>")
    here = Path(sys.argv[1])
    out = Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)

    for page in sorted(here.glob("*.html")):
        html = page.read_text(encoding="utf-8")
        title = re.search(r"<title>(.*?)</title>", html).group(1)
        scripts = re.findall(r"<script([^>]*)>(.*?)</script>", html, re.S)
        data = None
        for attrs, body in scripts:
            if body.lstrip().startswith("{") and '"links"' in body:
                data = json.loads(body)
                break
        if data is None:
            print(page.name, "no data")
            continue
        links = data["links"]
        file_const = re.search(r'const FILE\s*=\s*([^;]+);', html)
        print(page.name, "|", title, "| name", data.get("name"), "| links",
              len(links), "| keys", sorted(links[0].keys()),
              "| FILE", file_const.group(1)[:60] if file_const else None)

        # Two address schemes: "F-12-L03" / "B-04-C" (columns counted
        # left/right of a centre, the garments) and "W-00-07" (one face,
        # columns simply numbered - the AZ271SG3037 bag). The page's own
        # colOf() only knows the first; the second is read literally.
        def plain(c):
            return c.isdigit()

        def col_no(c):
            return 0 if c == "C" else int(c) if plain(c) else int(c[1:])
        m = max(col_no(l["addr"].split("-")[2]) for l in links)
        has_c = set()
        for l in links:
            s, r, c = l["addr"].split("-")
            # The side letter comes from the link ("F"/"B"); the address
            # prefix may be another letter for a single-face item ("W").
            assert int(r) == l["row"], l["addr"]
            if c == "C":
                has_c.add(l["side"] + r)

        def col_of(c, centre):
            if plain(c):
                return int(c)
            if c == "C":
                return m + 1
            if c[0] == "L":
                return m + 1 - int(c[1:])
            return (m + 1 if centre else m) + int(c[1:])

        rows = ["side,row,col,board_no,socket,label,shift"]
        for l in sorted(links, key=lambda l: (l["no"], int(l["sock"][1:]))):
            s, r, c = l["addr"].split("-")
            centre = l["side"] + r in has_c
            shift = 0 if centre else 0.5
            rows.append(",".join(str(v) for v in (
                # The yokes are FY / BY in the address; they are rows of the
                # front and the back (the page's own export calls FY "back").
                "front" if l["side"] == "F" else "back", int(r),
                col_of(c, centre),
                l["no"], int(l["sock"][1:]), l["label"], shift)))
        stem = page.stem.replace("-wiring", "")
        item = STEM_ITEM.get(stem, stem)
        # write_bytes, not write_text(..., newline="") - that parameter
        # needs Python 3.10+ (plan_designer_sim.md: "Python 3.9 stdlib");
        # the string already has explicit "\r\n" line endings, so encoding
        # straight to bytes writes exactly that with no translation either
        # way (adversarial review round 2 - F3).
        (out / f"{item}_map.csv").write_bytes(("\r\n".join(rows) + "\r\n").encode("utf-8"))
        boards = sorted({l["no"] for l in links})
        print("   boards", len(boards), boards[0], "-", boards[-1],
              "| sample", links[0])


if __name__ == "__main__":
    main()
