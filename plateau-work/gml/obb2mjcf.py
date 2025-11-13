#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OBB JSON (center, half_size, yaw) -> MJCF (MuJoCo XML)

機能（シンプル版）:
- OBB結果JSONを読み、各建物を geom type="box" として出力
- --zsrc で LOD1 の zmin/zmax/height を id 突合して高さを補完
- --collide {all,drone,none} で接触設定を一括付与
- --floor で z=0 の無限平面を追加
- 高さ情報が無い場合は --height / --zmin をフォールバックに使用
- 入力が ENU 座標系の場合、MJCF 座標系 (X=North, Y=East, Z=Up) に変換して出力
"""

import argparse, json, math
from pathlib import Path
import xml.etree.ElementTree as ET


def f4(x):  # コンパクトな小数表記
    return f"{float(x):.6f}".rstrip("0").rstrip(".")


def indent(elem, level=0):  # XML整形
    i = "\n" + level * "  "
    if len(elem):
        if not elem.text or not elem.text.strip():
            elem.text = i + "  "
        for e in elem:
            indent(e, level + 1)
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
    default_rgba=(0.82, 0.82, 0.86, 1.0),
    fallback_height=5.0,
    fallback_zmin=0.0,
    add_floor=False,
    floor_rgba=(0.7, 0.7, 0.7, 1.0),
    model_name="obb_world",
    collide_mode="all",  # "all" | "drone" | "none"
    pos_fn=lambda x, y, z: (x, y, z),
    yaw_fn=lambda a: a,
):
    """
    pos_fn: (cx, cy, cz) -> (x_mj, y_mj, z_mj)
    yaw_fn: yaw_in -> yaw_for_mjcf
    """
    mujoco = ET.Element("mujoco", {"model": model_name})
    size_tag = ET.SubElement(mujoco, "size")
    size_tag.attrib.update({
        "nstack": "40000000",
        "nconmax": "500000",
    })
    world = ET.SubElement(mujoco, "worldbody")


    if add_floor:
        ET.SubElement(world, "geom", {
            "name": "floor",
            "type": "plane",
            "pos": "0 0 0",
            "size": "0 0 1",
            "rgba": " ".join(map(f4, floor_rgba)),
        })

    for i, it in enumerate(items):
        gid = str(it.get("id", f"bldg_{i}"))

        # center は入力座標系の値
        cx, cy = it["center"]

        sx, sy = it["half_size"]
        yaw_in = float(it.get("yaw_rad", it.get("yaw", 0.0)))

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

        # --- 座標系変換（ENU -> MJCFなど） ---
        px, py, pz = pos_fn(cx, cy, cz)
        yaw = yaw_fn(yaw_in)

        density = it.get("density", default_density)
        rgba = tuple(it.get("rgba", default_rgba))

        body = ET.SubElement(world, "body", {"name": f"body_{gid}", "pos": "0 0 0"})
        attrib = {
            "name": f"geom_{gid}",
            "type": "box",
            "size": f"{f4(sx)} {f4(sy)} {f4(sz)}",   # MuJoCoは半サイズ
            "pos": f"{f4(px)} {f4(py)} {f4(pz)}",
            "euler": f"0 0 {f4(yaw)}",
            "rgba": " ".join(map(f4, rgba)),
            "contype": "1",
            "conaffinity": "0",
        }

        # 衝突設定
        if collide_mode == "none":
            attrib["contype"] = "0"; attrib["conaffinity"] = "0"
        elif collide_mode == "drone":
            # 建物 = (1,2) に設定。ドローン側は contype=2 conaffinity=1 を付与してください。
            attrib["contype"] = "1"; attrib["conaffinity"] = "2"
        # "all" は contype=1 conaffinity=0 のまま（既定 = 全部当たる）

        if density is not None:
            attrib["density"] = f4(density)

        ET.SubElement(body, "geom", attrib)

    return mujoco


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inp", required=True, help="OBB JSON (has 'results' or 'polygons')")
    ap.add_argument("--out", required=True, help="Output MJCF .xml")
    ap.add_argument("--zsrc", type=str, default=None,
                    help="LOD1 JSON to merge height (id match). Uses zmin/zmax or height if present.")
    ap.add_argument("--density", type=float, default=None, help="Default density (kg/m^3)")
    ap.add_argument("--rgba", type=float, nargs=4, default=None, help="Default RGBA (0-1)")
    ap.add_argument("--height", type=float, default=5.0, help="Fallback height if no z info")
    ap.add_argument("--zmin", type=float, default=0.0, help="Fallback base z")
    ap.add_argument("--floor", action="store_true", help="Add infinite plane at z=0")
    ap.add_argument("--model-name", default="obb_world")
    ap.add_argument("--collide", choices=["all", "drone", "none"], default="all",
                    help="Contact setting for buildings")

    args = ap.parse_args()

    # 読み込み
    data = json.loads(Path(args.inp).read_text(encoding="utf-8"))
    items = data.get("results", data.get("polygons", []))
    if not items:
        raise SystemExit("[ERR] No items found in --inp (expects key 'results' or 'polygons').")

    # 座標系情報を表示
    coordinate_system = "enu"
    origin = data.get("origin")
    bounds = data.get("bounds")
    
    print(f"[INFO] Coordinate system: {coordinate_system}")
    if origin:
        print(f"[INFO] Origin: lat={origin.get('lat')}, lon={origin.get('lon')}")
    if bounds:
        print(f"[INFO] Bounds: ±{bounds.get('ns_m')}m (NS), ±{bounds.get('ew_m')}m (EW)")
    print(f"[INFO] Total buildings: {len(items)}")

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

    # === 座標変換関数を定義 ===
    if coordinate_system.startswith("enu"):
        # OBB: (x=East, y=North, z=Up)
        # MJCF: (x=North, y=East, z=Up)
        def pos_fn(cx, cy, cz):
            # ENU -> MJCF
            return cy, cx, cz

        def yaw_fn(yaw_enu):
            # ENUの yaw(東基準) -> MJCFの yaw(北基準)
            # 方向ベクトル一致条件より: yaw_mj = π/2 - yaw_enu
            rad = math.pi / 2.0 - yaw_enu
            deg = math.degrees(rad)
            return deg

        print("[INFO] Using ENU -> MJCF (X=North, Y=East, Z=Up) transform.")
    else:
        # 何も知らない座標系なら、とりあえず素通し
        def pos_fn(cx, cy, cz):
            return cx, cy, cz

        def yaw_fn(a):
            return a

        print("[INFO] Unknown coordinate system: no axis transform applied.")

    root = make_mjcf(
        items=items,
        default_density=args.density,
        default_rgba=default_rgba,
        fallback_height=args.height,
        fallback_zmin=args.zmin,
        add_floor=args.floor,
        model_name=args.model_name,
        collide_mode=args.collide,
        pos_fn=pos_fn,
        yaw_fn=yaw_fn,
    )

    indent(root)
    Path(args.out).write_text(ET.tostring(root, encoding="utf-8").decode("utf-8"), encoding="utf-8")
    print(f"[OK] Saved MJCF → {args.out}")


if __name__ == "__main__":
    main()
