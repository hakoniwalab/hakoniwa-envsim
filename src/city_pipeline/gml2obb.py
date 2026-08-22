#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gml2obb.py
  - 入力: polygon JSON (gml_lod1_extract.py が出すような形式)
  - 出力: 各ポリゴンの OBB(外接最小矩形) または 壁BOX(薄いBOX列) を計算した JSON

  機能:
    * グラフ描画なし
    * "after"（変換後 OBB / 壁BOX）だけ出力
    * --in / --out は必須引数
    * 入力JSONに origin / bounds 情報があればそのまま引き継ぐ
    * 相対座標・絶対座標どちらもそのまま処理
    * --waste-threshold で OBB内の空白面積 / OBB面積 (0..1) の閾値を指定
        - 閾値以下: 従来どおり 1枚の OBB (mode="obb")
        - 閾値超え: ポリゴンの各辺を薄いBOXで表現し、屋根用footprintを保持 (mode="wall")
    * --wall-thickness で壁BOXの厚みを指定

  依存:
    pip install numpy
"""

import json
import math
import argparse
from pathlib import Path

import numpy as np


# ---------- geometry utils ----------

def _cross(o, a, b):
    """2D cross product (OA x OB)."""
    return (a[0]-o[0])*(b[1]-o[1]) - (a[1]-o[1])*(b[0]-o[0])


def polygon_area(points: np.ndarray) -> float:
    """2D polygon area (shoelace). points: (N,2) closed or open."""
    pts = np.asarray(points, float)
    if len(pts) < 3:
        return 0.0
    x, y = pts[:, 0], pts[:, 1]
    return 0.5 * abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def obb_empty_area_ratio(area_polygon: float, area_obb: float) -> float:
    """Fraction of the OBB area not occupied by the source footprint (0..1)."""
    if area_obb <= 0.0:
        return 0.0
    return min(1.0, max(0.0, (area_obb - area_polygon) / area_obb))


def convex_hull(points: np.ndarray):
    """Monotone chain convex hull. Returns hull (M,2) CCW, no duplicate closure."""
    pts = np.array(points, dtype=float)
    pts = np.unique(pts, axis=0)  # unique
    if len(pts) <= 2:
        return pts
    # sort by x, then y
    pts = pts[np.lexsort((pts[:, 1], pts[:, 0]))]

    lower = []
    for p in pts:
        while len(lower) >= 2 and _cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(tuple(p))

    upper = []
    for p in pts[::-1]:
        while len(upper) >= 2 and _cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(tuple(p))

    hull = np.array(lower[:-1] + upper[:-1], dtype=float)
    return hull


def rotation_matrix(theta: float):
    """2x2 rotation matrix for yaw=theta."""
    c, s = np.cos(theta), np.sin(theta)
    return np.array([[c,  s],
                     [-s, c]], dtype=float)


def min_area_rect_calipers(points: np.ndarray):
    """
    最小面積外接矩形（回転キャリパ法）

    Returns:
        center: (2,)
        half_size: (2,)  (sx, sy) ＊sx>=sy に正規化
        yaw: float [rad]  +x 軸→矩形の長辺方向
        rect: (4,2) CCW corners (world coords)
        area: float
    """
    hull = convex_hull(points)
    if len(hull) == 0:
        raise ValueError("Empty point set")
    if len(hull) == 1:
        c = hull[0]
        return c, np.array([0.0, 0.0]), 0.0, np.tile(c, (4, 1)), 0.0
    if len(hull) == 2:
        mid = hull.mean(axis=0)
        d = np.linalg.norm(hull[1] - hull[0]) / 2.0
        yaw = np.arctan2(hull[1, 1] - hull[0, 1], hull[1, 0] - hull[0, 0])
        size = np.array([d, 0.0])
        R = rotation_matrix(yaw)
        rect_local = np.array(
            [[-d, 0],
             [ d, 0],
             [ d, 0],
             [-d, 0]],
            dtype=float
        )
        rect = rect_local @ R + mid
        return mid, size, yaw, rect, 0.0

    best = {
        "area": np.inf,
        "yaw": 0.0,
        "xmin": 0.0, "xmax": 0.0,
        "ymin": 0.0, "ymax": 0.0,
        "mu": np.zeros(2)
    }
    mu = hull.mean(axis=0)

    # 各凸包エッジに平行な向きで評価
    for i in range(len(hull)):
        p0, p1 = hull[i], hull[(i + 1) % len(hull)]
        edge = p1 - p0
        yaw = np.arctan2(edge[1], edge[0])
        R = rotation_matrix(yaw)
        Hrot = (hull - mu) @ R.T
        xmin, ymin = Hrot.min(axis=0)
        xmax, ymax = Hrot.max(axis=0)
        area = (xmax - xmin) * (ymax - ymin)
        if area < best["area"]:
            best.update(dict(
                area=area, yaw=yaw,
                xmin=xmin, xmax=xmax,
                ymin=ymin, ymax=ymax,
                mu=mu
            ))

    # 復元
    yaw = best["yaw"]
    R = rotation_matrix(yaw)
    xmin, xmax = best["xmin"], best["xmax"]
    ymin, ymax = best["ymin"], best["ymax"]

    rect_local = np.array(
        [[xmin, ymin],
         [xmax, ymin],
         [xmax, ymax],
         [xmin, ymax]],
        dtype=float
    )
    rect = rect_local @ R + best["mu"]

    cx_l, cy_l = (xmax + xmin) / 2.0, (ymax + ymin) / 2.0
    center = np.array([cx_l, cy_l]) @ R + best["mu"]
    sx, sy = (xmax - xmin) / 2.0, (ymax - ymin) / 2.0

    # 長辺を sx に正規化（yawも安定化）
    if sy > sx:
        sx, sy = sy, sx
        yaw = (yaw + np.pi / 2.0)
        # [-pi, pi) に正規化
        yaw = (yaw + np.pi) % (2 * np.pi) - np.pi
        R = rotation_matrix(yaw)
        rect = rect_local[:, ::-1] @ R + best["mu"]
        center = np.array([cy_l, cx_l]) @ R + best["mu"]

    return center, np.array([sx, sy]), yaw, rect, best["area"]


# ---------- record builders ----------

def make_obb_record(
    pid: str,
    center: np.ndarray,
    halfsize: np.ndarray,
    yaw: float,
    rect: np.ndarray,
    area_obb: float,
    zmin,
    zmax,
    height,
    source,
    waste_ratio: float | None = None,
):
    """1枚OBB用のレコード生成."""
    rec = {
        "id": pid,
        "center": [float(center[0]), float(center[1])],
        "half_size": [float(halfsize[0]), float(halfsize[1])],
        "yaw_rad": float(yaw),
        "yaw_deg": float(np.degrees(yaw)),
        "rect_corners": rect.tolist(),
        "area": float(area_obb),
        "mode": "obb",
    }
    if waste_ratio is not None:
        rec["waste_ratio"] = float(waste_ratio)
    if zmin   is not None:
        rec["zmin"] = float(zmin)
    if zmax   is not None:
        rec["zmax"] = float(zmax)
    if height is not None:
        rec["height"] = float(height)
    if source is not None:
        rec["source_gml"] = source
    return rec


def make_wall_records(
    pid: str,
    pts: np.ndarray,
    zmin,
    zmax,
    height,
    source,
    wall_thickness: float,
    waste_ratio: float,
    min_edge_len: float = 1e-3,
    boundary_kind: str = "exterior",
    ring_index: int = 0,
):
    """
    壁BOXモード:
      ポリゴンの各辺を薄いBOXとして表現するレコードのリストを返す。
    """
    n = len(pts)
    records = []

    for i in range(n):
        x0, y0 = pts[i]
        x1, y1 = pts[(i + 1) % n]

        dx = x1 - x0
        dy = y1 - y0
        length = math.hypot(dx, dy)
        if length <= min_edge_len:
            # 極端に短い辺は無視
            continue

        yaw_edge = math.atan2(dy, dx)
        cx = 0.5 * (x0 + x1)
        cy = 0.5 * (y0 + y1)

        half_len = 0.5 * length
        half_thick = 0.5 * wall_thickness

        R = rotation_matrix(yaw_edge)
        rect_local = np.array(
            [[-half_len, -half_thick],
             [ half_len, -half_thick],
             [ half_len,  half_thick],
             [-half_len,  half_thick]],
            dtype=float
        )
        rect_edge = rect_local @ R + np.array([cx, cy])

        edge_id = (
            f"{pid}_edge{i}"
            if boundary_kind == "exterior"
            else f"{pid}_interior{ring_index}_edge{i}"
        )
        rec = {
            "id": edge_id,
            "parent_id": pid,
            "center": [float(cx), float(cy)],
            "half_size": [float(half_len), float(half_thick)],
            "yaw_rad": float(yaw_edge),
            "yaw_deg": float(np.degrees(yaw_edge)),
            "rect_corners": rect_edge.tolist(),
            "area": float(4.0 * half_len * half_thick),
            "mode": "wall",
            "edge_index": i,
            "boundary_kind": boundary_kind,
            "ring_index": ring_index,
            "waste_ratio": float(waste_ratio),
        }
        if zmin   is not None:
            rec["zmin"] = float(zmin)
        if zmax   is not None:
            rec["zmax"] = float(zmax)
        if height is not None:
            rec["height"] = float(height)
        if source is not None:
            rec["source_gml"] = source

        records.append(rec)

    return records


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in",  dest="in_path",  type=str, required=True,
                    help="入力 polygon JSON (gml_lod1_extract の出力など)")
    ap.add_argument("--out", dest="out_path", type=str, required=True,
                    help="OBB(after) / 壁BOX(after) の出力 JSON パス")
    ap.add_argument("--waste-threshold", type=float, default=None,
                    help="OBB内の空白面積 / OBB面積 (0..1) がこの値を超えたら壁BOXモードに切り替え")
    ap.add_argument("--wall-thickness", type=float, default=1.0,
                    help="壁BOXの厚み（入力座標系の単位）")
    args = ap.parse_args()

    in_path = Path(args.in_path)
    out_path = Path(args.out_path)
    waste_th = args.waste_threshold
    wall_t = args.wall_thickness
    if waste_th is not None and not 0.0 <= waste_th <= 1.0:
        raise SystemExit("[ERR] --waste-threshold must be in [0, 1]")
    if wall_t <= 0.0:
        raise SystemExit("[ERR] --wall-thickness must be positive")

    with open(in_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    polys_in = list(data["polygons"])

    # メタ情報を引き継ぐ
    crs = data.get("crs", "unknown")
    coordinate_system = data.get("coordinate_system", "unknown")
    if coordinate_system != "local-enu":
        raise SystemExit(
            "[ERR] Input must use coordinate_system='local-enu'; "
            f"found {coordinate_system!r}"
        )
    origin = data.get("origin")
    bounds = data.get("bounds")

    results_after = []
    wall_roofs = []

    for poly in polys_in:
        pid = poly.get("id", "poly")
        pts = np.array(poly["vertices"], dtype=float)
        interior_rings = [
            np.array(ring, dtype=float) for ring in poly.get("interior_rings", [])
        ]

        if len(pts) < 3:
            continue

        # z情報はそのまま引き継ぐ（あれば）
        zmin   = poly.get("zmin")
        zmax   = poly.get("zmax")
        height = poly.get("height")
        source = poly.get("source_gml")

        # 元多角形の面積（凹み込み）
        area_poly = polygon_area(pts) - sum(polygon_area(ring) for ring in interior_rings)

        # OBB 計算（入力座標系のまま）
        center, halfsize, yaw, rect, area_obb = min_area_rect_calipers(pts)

        if area_poly <= 0.0:
            # 面積が0なら waste_ratio の評価はできないので、素直に1枚OBBとして出力
            rec = make_obb_record(
                pid, center, halfsize, yaw, rect, area_obb,
                zmin, zmax, height, source,
                waste_ratio=None
            )
            results_after.append(rec)
            continue

        waste_ratio = obb_empty_area_ratio(area_poly, area_obb)
        use_wall_mode = (waste_th is not None and waste_ratio > waste_th)

        if not use_wall_mode:
            # 従来通り 1枚OBB
            rec = make_obb_record(
                pid, center, halfsize, yaw, rect, area_obb,
                zmin, zmax, height, source,
                waste_ratio=waste_ratio
            )
            results_after.append(rec)
        else:
            print(f"[INFO] id={pid}: waste_ratio={waste_ratio:.3f} > {waste_th} → wall mode")
            # 壁BOXモード：多角形の各辺を薄いBOXで表現
            wall_records = make_wall_records(
                pid, pts, zmin, zmax, height, source,
                wall_thickness=wall_t,
                waste_ratio=waste_ratio,
                min_edge_len=1e-3
            )
            for ring_index, ring in enumerate(interior_rings):
                wall_records.extend(make_wall_records(
                    pid, ring, zmin, zmax, height, source,
                    wall_thickness=wall_t,
                    waste_ratio=waste_ratio,
                    min_edge_len=1e-3,
                    boundary_kind="interior",
                    ring_index=ring_index,
                ))
            # 万一全部の辺が短すぎてスキップされた場合は、保険でOBBを残す
            if not wall_records:
                rec = make_obb_record(
                    pid, center, halfsize, yaw, rect, area_obb,
                    zmin, zmax, height, source,
                    waste_ratio=waste_ratio
                )
                results_after.append(rec)
            else:
                results_after.extend(wall_records)
                wall_roof = {
                    "id": pid,
                    "vertices": pts.tolist(),
                    "interior_rings": [ring.tolist() for ring in interior_rings],
                    "waste_ratio": float(waste_ratio),
                }
                if zmin is not None:
                    wall_roof["zmin"] = float(zmin)
                if zmax is not None:
                    wall_roof["zmax"] = float(zmax)
                if height is not None:
                    wall_roof["height"] = float(height)
                if source is not None:
                    wall_roof["source_gml"] = source
                wall_roofs.append(wall_roof)

    out = {
        "version": "0.7",
        "mode": "after",
        "source": str(in_path),
        "crs": crs,
        "coordinate_system": coordinate_system,
        "results": results_after,
        "wall_roofs": wall_roofs,
    }

    # origin / bounds があれば引き継ぐ
    if origin is not None:
        out["origin"] = origin
    if bounds is not None:
        out["bounds"] = bounds

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"[OK] record count: {len(results_after)}  → {out_path}")
    print(f"[INFO] CRS: {crs}, Coordinate system: {coordinate_system}")
    if origin:
        print(f"[INFO] Origin: lat={origin.get('lat')}, lon={origin.get('lon')}")
    if waste_th is not None:
        print(f"[INFO] waste-threshold: {waste_th}, wall-thickness: {wall_t}")


if __name__ == "__main__":
    main()
