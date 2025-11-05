#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OBB JSON (center, half_size, yaw) -> MJCF (MuJoCo XML)

機能:
- OBB結果JSONを読み、各建物を geom type="box" として出力
- --zsrc で LOD1 の zmin/zmax/height を id 突合して高さを補完（最優先）
- --head N で先頭 N 件に絞り、1件目の center を原点に XY 相対化
- --collide {all,drone,none} で接触設定を一括付与
- --floor で z=0 の無限平面を追加
- 高さ情報が無い場合は --height / --zmin をフォールバックに使用

使い方例:
  python obbjson2mjcf.py \
    --inp lod1_obb.json \
    --zsrc lod1.json \
    --out lod1_obb_realheight.xml \
    --head 500 \
    --collide drone \
    --density 2000 \
    --floor
"""

import argparse, json
from pathlib import Path
import xml.etree.ElementTree as ET

def f4(x):  # コンパクトな小数表記
    return f"{float(x):.6f}".rstrip("0").rstrip(".")

def indent(elem, level=0):  # XML整形
    i = "\n" + level*"  "
    if len(elem):
        if not elem.text or not elem.text.strip():
            elem.text = i + "  "
        for e in elem:
            indent(e, level+1)
        if not e.tail or not e.tail.strip():
            e.tail = i
    if level and (not elem.tail or not elem.tail.strip()):
        elem.tail = i

def load_zmap(zsrc_path: str):
    """LOD1 JSON を読み、id -> {zmin,zmax,height} 辞書を作る"""
    if not zsrc_path:
        return {}
    j = json.loads(Path(zsrc_path).read_text(encoding="utf-8"))
    items = j.get("polygons", j.get("results", []))
    zmap = {}
    for p in items:
        gid = str(p.get("id", ""))
        if not gid:
            continue
        e = {}
        if "zmin" in p and "zmax" in p:
            e["zmin"] = float(p["zmin"])
            e["zmax"] = float(p["zmax"])
        if "height" in p:
            e["height"] = float(p["height"])
        if e:
            zmap[gid] = e
    return zmap

def make_mjcf(
    items,
    default_density=None,
    default_rgba=(0.82,0.82,0.86,1.0),
    fallback_height=5.0,
    fallback_zmin=0.0,
    add_floor=False,
    floor_rgba=(0.7,0.7,0.7,1.0),
    model_name="obb_world",
    rel_origin_xy=None,         # (x0,y0) を指定すると XY 相対化
    collide_mode="all"          # "all" | "drone" | "none"
):
    mujoco = ET.Element("mujoco", {"model": model_name})
    world = ET.SubElement(mujoco, "worldbody")

    if add_floor:
        ET.SubElement(world, "geom", {
            "name": "floor",
            "type": "plane",
            "pos": "0 0 0",
            "size": "0 0 1",
            "rgba": " ".join(map(f4, floor_rgba))
        })

    for i, it in enumerate(items):
        gid = str(it.get("id", f"bldg_{i}"))
        cx, cy = it["center"]
        if rel_origin_xy is not None:
            x0, y0 = rel_origin_xy
            cx, cy = cx - x0, cy - y0

        sx, sy = it["half_size"]
        yaw = float(it.get("yaw_rad", it.get("yaw", 0.0)))

        # --- 高さ決定（優先順位: it.height / it.zmin,zmax / fallback） ---
        if "height" in it:
            height = float(it["height"])
            zmin = float(it.get("zmin", fallback_zmin))
            zmax = zmin + height
        elif "zmin" in it and "zmax" in it:
            zmin = float(it["zmin"]); zmax = float(it["zmax"])
            height = zmax - zmin
        else:
            zmin = float(fallback_zmin)
            height = float(fallback_height)
            zmax = zmin + height

        cz = 0.5 * (zmin + zmax)
        sz = 0.5 * height

        density = it.get("density", default_density)
        rgba = tuple(it.get("rgba", default_rgba))

        body = ET.SubElement(world, "body", {"name": f"body_{gid}", "pos": "0 0 0"})
        attrib = {
            "name": f"geom_{gid}",
            "type": "box",
            "size": f"{f4(sx)} {f4(sy)} {f4(sz)}",   # MuJoCoは半サイズ
            "pos":  f"{f4(cx)} {f4(cy)} {f4(cz)}",
            "euler": f"0 0 {f4(yaw)}",
            "rgba": " ".join(map(f4, rgba)),
        }
        # 衝突設定
        if collide_mode == "none":
            attrib["contype"] = "0"; attrib["conaffinity"] = "0"
        elif collide_mode == "drone":
            # 建物 = (1,2) に設定。ドローン側は contype=2 conaffinity=1 を付与してください。
            attrib["contype"] = "1"; attrib["conaffinity"] = "2"
        # "all" は何も付与しない（既定 = 全部当たる）

        if density is not None:
            attrib["density"] = f4(density)

        ET.SubElement(body, "geom", attrib)

    return mujoco

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inp", required=True, help="OBB JSON (has 'results')")
    ap.add_argument("--out", required=True, help="Output MJCF .xml")
    ap.add_argument("--zsrc", type=str, default=None,
                    help="LOD1 JSON to merge height (id match). Uses zmin/zmax or height if present.")
    ap.add_argument("--density", type=float, default=None, help="Default density (kg/m^3)")
    ap.add_argument("--rgba", type=float, nargs=4, default=None, help="Default RGBA (0-1)")
    ap.add_argument("--height", type=float, default=5.0, help="Fallback height if no z info")
    ap.add_argument("--zmin", type=float, default=0.0, help="Fallback base z")
    ap.add_argument("--floor", action="store_true", help="Add infinite plane at z=0")
    ap.add_argument("--model-name", default="obb_world")
    ap.add_argument("--head", type=int, default=None,
                    help="Take first N items and make XY relative to the first one's center")
    ap.add_argument("--collide", choices=["all","drone","none"], default="all",
                    help="Contact setting for buildings")
    args = ap.parse_args()

    # 読み込み
    data = json.loads(Path(args.inp).read_text(encoding="utf-8"))
    items = data.get("results", data.get("polygons", []))
    if not items:
        raise SystemExit("[ERR] No items found in --inp (expects key 'results' or 'polygons').")

    # 先頭N件 + 相対化
    rel_origin_xy = None
    if args.head is not None and args.head > 0:
        items = items[:args.head]
        x0, y0 = items[0]["center"]
        rel_origin_xy = (float(x0), float(y0))
        print(f"[INFO] head={args.head}, XY origin = ({x0}, {y0})")

    # 高さ突合
    zmap = load_zmap(args.zsrc) if args.zsrc else {}
    if zmap:
        hit = 0
        for it in items:
            gid = str(it.get("id", ""))
            ref = zmap.get(gid)
            if not ref:
                continue
            # zmin/zmax があればそれを、無ければ height を適用
            if "zmin" in ref and "zmax" in ref:
                it["zmin"] = ref["zmin"]; it["zmax"] = ref["zmax"]; hit += 1
            elif "height" in ref and ("zmin" not in it and "zmax" not in it):
                it["height"] = ref["height"]; hit += 1
        print(f"[INFO] height merged for {hit}/{len(items)} items from --zsrc")

    default_rgba = tuple(args.rgba) if args.rgba else (0.82, 0.82, 0.86, 1.0)

    root = make_mjcf(
        items=items,
        default_density=args.density,
        default_rgba=default_rgba,
        fallback_height=args.height,
        fallback_zmin=args.zmin,
        add_floor=args.floor,
        model_name=args.model_name,
        rel_origin_xy=rel_origin_xy,
        collide_mode=args.collide,
    )

    indent(root)
    Path(args.out).write_text(ET.tostring(root, encoding="utf-8").decode("utf-8"), encoding="utf-8")
    print(f"[OK] Saved MJCF → {args.out}")

if __name__ == "__main__":
    main()
