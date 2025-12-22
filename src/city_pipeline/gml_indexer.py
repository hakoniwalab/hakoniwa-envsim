#!/usr/bin/env python3
import json
import re
from pathlib import Path
import argparse

# 6〜8桁のメッシュコード + kind
GML_RE = re.compile(r"^(\d{6,8})_(\w+)_.*\.gml$")

def build_index(topdir: Path):
    udx = topdir / "udx"
    records = []

    # udx 以下を再帰的に全部見る
    for gml in udx.rglob("*.gml"):
        m = GML_RE.match(gml.name)
        if not m:
            continue
        mesh, kind = m.group(1), m.group(2)
        records.append({
            "mesh": mesh,
            "kind": kind,
            "path": str(gml.resolve())
        })
    return records


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("topdir", help="PLATEAU citygml top dir")
    ap.add_argument("-o", "--out", default="gml_index.json")
    args = ap.parse_args()

    topdir = Path(args.topdir)
    records = build_index(topdir)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)

    print(f"indexed {len(records)} GML files -> {args.out}")

if __name__ == "__main__":
    main()
