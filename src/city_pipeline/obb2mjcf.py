#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OBB JSON (center, half_size, yaw) -> MJCF (MuJoCo XML)

機能（シンプル版）:
- OBB結果JSONを読み、各建物を geom type="box" として出力
- wall mode建物は元footprintを三角形分割し、薄い屋根collision meshで閉じる
- --zsrc で LOD1 の zmin/zmax/height を id 突合して高さを補完
- --collide {all,drone,none} で接触設定を一括付与
- --floor で z=0 の無限平面を追加
- 高さ情報が無い場合は --height / --zmin をフォールバックに使用
- 入力が ENU 座標系の場合、MJCF 座標系 (X=North, Y=-East, Z=Up) に変換して出力
"""

import argparse, hashlib, json, math
from collections import Counter, defaultdict
from pathlib import Path
import xml.etree.ElementTree as ET

import numpy as np

from citygml2glb import GlbError, triangulate_rings
from building_lod2_colliders import (
    prepare_p1_geometry,
    prepare_p2_geometry,
    prepare_p3_geometry,
)
from mjcf_collision import collision_attributes
from mjcf_prism import format_numbers, triangular_prism
from world_frame import load_world_frame


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


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_physics_application_receipt(
    classification_path,
    items,
    root,
    output_path,
    receipt_path,
    *,
    p1_surface_piece_count=0,
    p2_surface_piece_count=0,
    p3_surface_piece_count=0,
    p1_skipped_degenerate=None,
    p2_skipped_degenerate=None,
    p3_skipped_degenerate=None,
    max_physics_level=3,
):
    """Prove which classified buildings are represented by the current MJCF."""
    classification = json.loads(Path(classification_path).read_text(encoding="utf-8"))
    classified = {
        record["building_id"]: record for record in classification.get("buildings", [])
    }
    input_counts = Counter(
        str(item.get("parent_id") or item.get("id", "")).split("__part_", 1)[0]
        for item in items
    )
    geom_counts = defaultdict(int)
    for geom in root.findall(".//geom"):
        name = geom.get("name", "")
        for building_id in classified:
            if (
                name == f"geom_{building_id}"
                or name.startswith(f"geom_{building_id}_")
                or name.startswith(f"roof_{building_id}_piece_")
                or name.startswith(f"p1_surface_{building_id}_piece_")
                or name.startswith(f"p2_surface_{building_id}_piece_")
                or name.startswith(f"p3_surface_{building_id}_piece_")
            ):
                geom_counts[building_id] += 1
                break
    represented = set(input_counts) | {key for key, value in geom_counts.items() if value > 0}
    missing = sorted(set(classified) - represented)
    unknown = sorted(set(input_counts) - set(classified))
    if missing or unknown:
        raise ValueError(
            "classification/collider building identity mismatch: "
            f"missing={missing}, unknown={unknown}"
        )
    records = []
    classified_counts = Counter(record["class"] for record in classified.values())
    class_status = {}
    for class_id in ("P0", "P1", "P2", "P3"):
        if class_id == "P0":
            class_status[class_id] = "applied"
        elif class_id in {"P1", "P2", "P3"}:
            piece_count = {
                "P1": p1_surface_piece_count,
                "P2": p2_surface_piece_count,
                "P3": p3_surface_piece_count,
            }[class_id]
            class_status[class_id] = (
                "applied" if classified_counts[class_id] == 0 or piece_count > 0
                else "pending"
            )
        else:
            class_status[class_id] = "pending"
    for building_id, record in sorted(classified.items()):
        class_id = record["class"]
        records.append({
            "building_id": building_id,
            "class": class_id,
            "status": class_status[class_id],
            "actual_strategy": (
                "lod1-approved" if class_id == "P0"
                else "lod2-wall-and-roof-surface-prisms" if class_id == "P1"
                else "lod2-height-profile-surface-prisms" if class_id == "P2"
                else "lod2-void-preserving-exterior-surface-prisms"
            ),
            "input_primitive_count": input_counts[building_id],
            "mjcf_geom_count": geom_counts[building_id],
        })
    collider_geom_counts = {
        class_id: sum(
            record["mjcf_geom_count"] for record in records if record["class"] == class_id
        )
        for class_id in ("P0", "P1", "P2", "P3")
    }
    output = Path(output_path)
    receipt = {
        "schema_version": 1,
        "status": (
            "complete" if all(value == "applied" for value in class_status.values())
            else "partial"
        ),
        "policy": "incremental-building-physics-v1",
        "max_physics_level": max_physics_level,
        "precedence": [f"P{level}" for level in range(max_physics_level, -1, -1)],
        "classification": str(Path(classification_path)),
        "mjcf": {"path": str(output), "bytes": output.stat().st_size, "sha256": _sha256(output)},
        "physics_modified_by_classification": (
            p1_surface_piece_count > 0 or p2_surface_piece_count > 0
            or p3_surface_piece_count > 0
        ),
        "class_status": class_status,
        "counts": dict(Counter(record["class"] for record in records)),
        "collider_geom_counts": {
            "total": sum(collider_geom_counts.values()),
            "by_class": collider_geom_counts,
        },
        "buildings": records,
        "derived_geometry": [
            {
                "class": class_id,
                "type": "surface_collision_thickness",
                "piece_count": piece_count,
                "skipped_numerically_degenerate_triangles": (
                    p1_skipped_degenerate if class_id == "P1"
                    else p2_skipped_degenerate if class_id == "P2"
                    else p3_skipped_degenerate
                ) or {},
                "direction": {
                    "RoofSurface": "negative_world_z",
                    "WallSurface": "source_triangle_normal",
                    **({
                        "OuterCeilingSurface": "source_triangle_normal",
                        "OuterFloorSurface": "source_triangle_normal",
                    } if class_id == "P3" else {}),
                },
                "purpose": (
                    "make each selected source semantic-surface triangle a "
                    "watertight convex collision mesh"
                ),
            }
            for class_id, piece_count in (
                ("P1", p1_surface_piece_count),
                ("P2", p2_surface_piece_count),
                ("P3", p3_surface_piece_count),
            )
            if piece_count
        ],
        "limitations": [
            "P2/P3 prioritize source-shape fidelity; collider-count optimization is pending",
            "interior building partitions and structural solids are outside the collision scope",
        ],
    }
    target = Path(receipt_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return receipt


def coordinate_transform(coordinate_system: str):
    """Return the one supported local-ENU to Hakoniwa MJCF transform."""
    if coordinate_system != "local-enu":
        raise ValueError(
            "OBB input must use coordinate_system='local-enu'; "
            f"found {coordinate_system!r}"
        )
    return (
        lambda east, north, up: (north, -east, up),
        lambda yaw_rad: math.degrees(yaw_rad),
        lambda east_half, north_half: (north_half, east_half),
    )


def add_wall_roofs(asset, world, roof_specs, thickness_m, collide_mode, pos_fn,
                   fallback_height, fallback_zmin, rgba):
    """Close wall-mode buildings with footprint-preserving thin roof meshes."""
    piece_count = 0
    for roof in roof_specs:
        gid = str(roof.get("id", f"building_{piece_count}"))
        if "zmax" in roof:
            zmax = float(roof["zmax"])
        else:
            zmin = float(roof.get("zmin", fallback_zmin))
            zmax = zmin + float(roof.get("height", fallback_height))

        rings = []
        for ring in [roof.get("vertices", []), *roof.get("interior_rings", [])]:
            if len(ring) >= 3:
                rings.append([[float(x), float(y), zmax] for x, y in ring])
        if not rings:
            continue
        try:
            vertices, faces = triangulate_rings(rings)
        except (GlbError, ValueError) as error:
            raise ValueError(f"failed to triangulate wall roof {gid}: {error}") from error

        body = ET.SubElement(world, "body", {"name": f"body_{gid}_roof", "pos": "0 0 0"})
        for roof_piece_index, face in enumerate(faces):
            triangle = np.asarray([pos_fn(*vertices[index]) for index in face], dtype=float)
            prism, prism_faces = triangular_prism(triangle, thickness_m)
            piece_id = f"roof_{gid}_piece_{roof_piece_index:04d}"
            ET.SubElement(asset, "mesh", {
                "name": piece_id,
                "vertex": format_numbers(prism.reshape(-1)),
                "face": " ".join(str(int(value)) for value in prism_faces.reshape(-1)),
            })
            ET.SubElement(body, "geom", {
                "name": piece_id,
                "type": "mesh",
                "mesh": piece_id,
                "rgba": " ".join(map(f4, rgba)),
                **collision_attributes(collide_mode),
            })
            piece_count += 1
    return piece_count


def add_lod2_surface_pieces(root, pieces, collide_mode, rgba):
    """Add independent convex prisms from source LOD2 Wall/Roof surfaces."""
    if not pieces:
        return 0
    asset = root.find("asset")
    if asset is None:
        asset = ET.Element("asset")
        world = root.find("worldbody")
        root.insert(list(root).index(world), asset)
    world = root.find("worldbody")
    for piece in pieces:
        ET.SubElement(asset, "mesh", {
            "name": piece["id"],
            "vertex": format_numbers(piece["vertices"].reshape(-1)),
            "face": " ".join(str(int(value)) for value in piece["faces"].reshape(-1)),
        })
        ET.SubElement(world, "geom", {
            "name": piece["id"],
            "type": "mesh",
            "mesh": piece["id"],
            "rgba": " ".join(map(f4, rgba)),
            **collision_attributes(collide_mode),
        })
    return len(pieces)


def make_mjcf(
    items,
    wall_roofs=(),
    roof_thickness_m=0.02,
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
    sxy_fn=lambda sx, sy: (sx, sy),
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
    asset = ET.SubElement(mujoco, "asset")
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

        sx, sy = sxy_fn(*it["half_size"])
        yaw_in = float(it.get("yaw_rad", it.get("yaw", 0.0)))

        # --- 高さ決定（優先順位: it.height / it.zmin,zmax / fallback） ---
        if "height" in it:
            height = float(it["height"])
            zmin = float(it.get("zmin", fallback_zmin))
            zmax = zmin + height
        elif "zmin" in it and "zmax" in it:
            zmin = float(it["zmin"]); zmax = float(it["zmax"])
            height = zmax - zmin
            #print(f"[DEBUG] zmin = {zmin}, zmax = {zmax} for id={gid}")
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
            **collision_attributes(collide_mode),
        }

        if density is not None:
            attrib["density"] = f4(density)

        ET.SubElement(body, "geom", attrib)

    roof_piece_count = add_wall_roofs(
        asset, world, wall_roofs, roof_thickness_m, collide_mode, pos_fn,
        fallback_height, fallback_zmin, default_rgba,
    )
    if roof_piece_count == 0:
        mujoco.remove(asset)

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
    ap.add_argument("--roof-thickness", type=float, default=0.02,
                    help="Downward numerical collision thickness for wall-mode roofs")
    ap.add_argument("--world-frame", type=Path,
                    help="Shared city world-frame.json; uses its altitude offset instead of building minimum")
    ap.add_argument("--classification", type=Path,
                    help="P0-P3 building classification used to audit incremental Physics application")
    ap.add_argument("--application-receipt", type=Path,
                    help="Write class-to-MJCF application provenance; requires --classification")
    ap.add_argument("--max-physics-level", type=int, choices=range(4), default=3,
                    help="Maximum applied building Physics class (P0..P3)")

    args = ap.parse_args()

    # 読み込み
    data = json.loads(Path(args.inp).read_text(encoding="utf-8"))
    items = data.get("results", data.get("polygons", []))
    wall_roofs = data.get("wall_roofs", [])
    if not items:
        raise SystemExit("[ERR] No items found in --inp (expects key 'results' or 'polygons').")
    # Preserve the pre-classification input for provenance. P1 primitives are
    # replaced below, but the receipt should record what was superseded.
    receipt_items = list(items)
    if args.roof_thickness <= 0:
        raise SystemExit("[ERR] --roof-thickness must be positive")
    if bool(args.classification) != bool(args.application_receipt):
        raise SystemExit("[ERR] --classification and --application-receipt must be used together")
    if args.classification and (not args.zsrc or not args.world_frame):
        raise SystemExit("[ERR] class-specific Physics requires --zsrc and --world-frame")

    p1_surface_pieces = []
    p2_surface_pieces = []
    p3_surface_pieces = []
    p1_skipped_degenerate = {}
    p2_skipped_degenerate = {}
    p3_skipped_degenerate = {}
    if args.classification:
        class_data = json.loads(args.classification.read_text(encoding="utf-8"))
        if int(class_data.get("max_level", 3)) != args.max_physics_level:
            raise SystemExit(
                "[ERR] classification max_level does not match --max-physics-level"
            )
        class_ids = {
            class_id: {
                record["building_id"]
                for record in class_data.get("buildings", [])
                if record.get("class") == class_id
            }
            for class_id in ("P1", "P2", "P3")
        }
        replaced_ids = class_ids["P1"] | class_ids["P2"] | class_ids["P3"]
        if replaced_ids:
            def item_building_id(item):
                return str(item.get("parent_id") or item.get("id", "")).split("__part_", 1)[0]
            items = [item for item in items if item_building_id(item) not in replaced_ids]
            wall_roofs = [
                roof for roof in wall_roofs
                if str(roof.get("id", "")).split("__part_", 1)[0] not in replaced_ids
            ]
        if class_ids["P1"]:
            p1_geometry = prepare_p1_geometry(
                Path(args.zsrc), args.classification, args.world_frame,
                roof_thickness_m=args.roof_thickness,
            )
            p1_surface_pieces = p1_geometry.pieces
            p1_skipped_degenerate = p1_geometry.skipped_degenerate_by_surface
            print(
                f"[INFO] P1 class collider: buildings={len(class_ids['P1'])} "
                f"surface_pieces={len(p1_surface_pieces)} "
                f"skipped_degenerate={sum(p1_skipped_degenerate.values())}"
            )
        if class_ids["P2"]:
            p2_geometry = prepare_p2_geometry(
                Path(args.zsrc), args.classification, args.world_frame,
                roof_thickness_m=args.roof_thickness,
            )
            p2_surface_pieces = p2_geometry.pieces
            p2_skipped_degenerate = p2_geometry.skipped_degenerate_by_surface
            print(
                f"[INFO] P2 class collider: buildings={len(class_ids['P2'])} "
                f"surface_pieces={len(p2_surface_pieces)} "
                f"skipped_degenerate={sum(p2_skipped_degenerate.values())}"
            )
        if class_ids["P3"]:
            p3_geometry = prepare_p3_geometry(
                Path(args.zsrc), args.classification, args.world_frame,
                roof_thickness_m=args.roof_thickness,
            )
            p3_surface_pieces = p3_geometry.pieces
            p3_skipped_degenerate = p3_geometry.skipped_degenerate_by_surface
            print(
                f"[INFO] P3 class collider: buildings={len(class_ids['P3'])} "
                f"surface_pieces={len(p3_surface_pieces)} "
                f"skipped_degenerate={sum(p3_skipped_degenerate.values())}"
            )

    # 座標系情報を表示
    coordinate_system = data.get("coordinate_system", "unknown")
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

    # --- 全体の zmin を 0 にそろえるためのオフセット計算 ---
    all_zmin = []
    for it in items:
        if "zmin" in it:
            all_zmin.append(float(it["zmin"]))
        else:
            all_zmin.append(float(args.zmin))

    if all_zmin:
        z_offset = (
            float(load_world_frame(args.world_frame)["origin"]["altitude_offset_m"])
            if args.world_frame else min(all_zmin)
        )
        print(f"[INFO] Global z-offset = {-z_offset} (min z = {z_offset})")

        # 全オブジェクトの zmin/zmax をオフセットして 0 基準にそろえる
        for it in items:
            #print(f"[INFO] Adjusting zmin {it.get('zmin', args.zmin)} to {it.get('zmin', args.zmin)-z_offset}...")
            if "zmin" in it:
                it["zmin"] = float(it["zmin"]) - z_offset
            if "zmax" in it:
                it["zmax"] = float(it["zmax"]) - z_offset
            # height は変更不要（差分に基づくため）
        for roof in wall_roofs:
            if "zmin" in roof:
                roof["zmin"] = float(roof["zmin"]) - z_offset
            if "zmax" in roof:
                roof["zmax"] = float(roof["zmax"]) - z_offset


    default_rgba = tuple(args.rgba) if args.rgba else (0.82, 0.82, 0.86, 1.0)

    # === 座標変換関数を定義 ===
    pos_fn, yaw_fn, sxy_fn = coordinate_transform(coordinate_system)
    print("[INFO] Using ENU -> MJCF (X=North, Y=-East, Z=Up) transform.")

    root = make_mjcf(
        items=items,
        wall_roofs=wall_roofs,
        roof_thickness_m=args.roof_thickness,
        default_density=args.density,
        default_rgba=default_rgba,
        fallback_height=args.height,
        fallback_zmin=args.zmin,
        add_floor=args.floor,
        model_name=args.model_name,
        collide_mode=args.collide,
        pos_fn=pos_fn,
        yaw_fn=yaw_fn,
        sxy_fn=sxy_fn,
    )
    add_lod2_surface_pieces(
        root, p1_surface_pieces, args.collide, (0.95, 0.72, 0.18, 1.0)
    )
    add_lod2_surface_pieces(
        root, p2_surface_pieces, args.collide, (0.94, 0.42, 0.12, 1.0)
    )
    add_lod2_surface_pieces(
        root, p3_surface_pieces, args.collide, (0.62, 0.36, 0.80, 1.0)
    )

    indent(root)
    Path(args.out).write_text(ET.tostring(root, encoding="utf-8").decode("utf-8"), encoding="utf-8")
    print(f"[OK] Saved MJCF → {args.out}")
    if args.classification:
        receipt = write_physics_application_receipt(
            args.classification, receipt_items, root, args.out, args.application_receipt,
            p1_surface_piece_count=len(p1_surface_pieces),
            p2_surface_piece_count=len(p2_surface_pieces),
            p3_surface_piece_count=len(p3_surface_pieces),
            p1_skipped_degenerate=p1_skipped_degenerate,
            p2_skipped_degenerate=p2_skipped_degenerate,
            p3_skipped_degenerate=p3_skipped_degenerate,
            max_physics_level=args.max_physics_level,
        )
        print(
            "[OK] Building Physics application: "
            f"P0/P1/P2/P3=applied → {args.application_receipt}"
        )


if __name__ == "__main__":
    main()
