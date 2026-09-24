"""Sample design grids for the looks that have none yet, generated from
their map CSVs in the site's grid format (side,row,shift,1..N; "0" = no
hole, "0xNN" = palette colour). Two per item, checked with the repo's own
check() before they are uploaded through the running conductor."""
import json
import sys
import urllib.request
from pathlib import Path

if __name__ == "__main__":
    sys.path.insert(0, str(Path(sys.argv[1])))
from conductor.look import Design, LookMap, check  # noqa: E402

# item -> (colours for the band pattern, colours for the diagonal pattern)
PLAN = {
    "AZ271SD1301": ([0x03, 0x07], [0x03, 0x0A, 0x01, 0x07]),          # red/almond bands; warm diagonal
    "AZ271SC6302": ([0x02, 0x09], [0x02, 0x09, 0x06, 0x00]),          # blue/sky bands; cool diagonal
    "AZ271SB2303": ([0x09, 0x02], [0x06, 0x02, 0x00, 0x09]),          # the skirt answers the top
    "AZ271SD1306": ([0x05, 0x0B], [0x05, 0x0B, 0x0C, 0x07]),          # green/yellow-green; olive diagonal
}


def grid_csv(look_map, colour_of):
    cols = sorted({s.col for s in look_map.scales})
    lo, hi = min(cols), max(cols)
    by_pos = look_map.by_position
    lines = ["side,row,shift," + ",".join(str(c) for c in range(lo, hi + 1))]
    for side in look_map.sides:
        rows = sorted({s.row for s in look_map.scales if s.side == side}, reverse=True)
        for row in rows:
            cells = []
            for col in range(lo, hi + 1):
                scale = by_pos.get((side, row, col))
                cells.append(f"0x{colour_of(scale):02X}" if scale else "0")
            lines.append(f"{side},{row},{look_map.shift(side, row)}," + ",".join(cells))
    return "\n".join(lines) + "\n"


def main(repo: Path, url: str) -> None:
    files = repo / "showdata" / "files"
    payload = []
    for item, (bands, diag) in PLAN.items():
        look_map = LookMap.from_csv(files / f"{item}_map.csv")
        designs = {
            f"{item}_color_sampleA_grid.csv": grid_csv(look_map, lambda s: bands[(s.row // 3) % 2]),
            f"{item}_color_sampleB_grid.csv": grid_csv(look_map, lambda s: diag[(s.row + s.col) % 4]),
        }
        for name, text in designs.items():
            tmp = Path(__file__).parent / name
            tmp.write_text(text, encoding="utf-8")
            design = Design.from_csv(tmp)
            problems = check(look_map, design)
            print(f"{name}: {len(design.colors)} colours, problems: {problems[:2] or 'none'}")
            if problems:
                sys.exit(1)
            payload.append({"name": name, "text": text})

    req = urllib.request.Request(url + "/api/files", data=json.dumps({"files": payload}).encode(),
                                 headers={"Content-Type": "application/json"})
    print(json.load(urllib.request.urlopen(req, timeout=30)))


if __name__ == "__main__":
    main(Path(sys.argv[1]), sys.argv[2])
