#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gml2oob_min.py
  - 入力: polygon JSON (gml_lod1_extract.py が出すような形式)
  - 出力: 各ポリゴンの OBB(外接最小矩形) を計算した JSON
  - 機能:
      * グラフ描画なし
      * "after"（変換後 OBB）だけ出力
      * --in / --out は必須引数
      * --origin-lat, --origin-lon で原点を指定
      * 原点 lat/lon をポリゴンと同じ EPSG に変換し、
        その点からの相対位置 [m] として center / rect_corners を保存

  依存:
    pip install numpy pyproj
"""

import json
import argparse
from pathlib import Path

import numpy as np

try:
    from pyproj import Transformer
except ImportError as e:
    raise SystemExit("pyproj が必要です: pip install pyproj") from e


# ---------- geometry utils (元スクリプトから必要分だけ) ----------

def _cross(o, a, b):
    """2D cross product (OA x OB)."""
    return (a[0]-o[0])*(b[1]-o[1]) - (a[1]-o[1])*(b[0]-o[0])

def polygon_area(points: np.ndarray) -> float:
    """2D polygon area (shoelace). points: (N,2) closed or open."""
    pts = np.asarray(points, float)
    if len(pts) < 3:
        return 0.0
    x, y = pts[:,0], pts[:,1]
    return 0.5 * abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))

def convex_hull(points: np.ndarray):
    """Monotone chain convex hull. Returns hull (M,2) CCW, no duplicate closure."""
    pts = np.array(points, dtype=float)
    pts = np.unique(pts, axis=0)  # unique
    if len(pts) <= 2:
        return pts
    pts = pts[np.lexsort((pts[:,1], pts[:,0]))]  # sort by x, then y

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
    c, s = np.cos(theta), np.sin(theta)
    return np.array([[ c,  s],
                     [-s,  c]], dtype=float)

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
        return c, np.array([0.0, 0.0]), 0.0, np.tile(c, (4,1)), 0.0
    if len(hull) == 2:
        mid = hull.mean(axis=0)
        d = np.linalg.norm(hull[1]-hull[0]) / 2.0
        yaw = np.arctan2(hull[1,1]-hull[0,1], hull[1,0]-hull[0,0])
        size = np.array([d, 0.0])
        R = rotation_matrix(yaw)
        rect_local = np.array([[-d, 0],[ d, 0],[ d, 0],[-d, 0]], dtype=float)
        rect = rect_local @ R + mid
        return mid, size, yaw, rect, 0.0

    best = {
        "area": np.inf, "yaw": 0.0,
        "xmin": 0.0, "xmax": 0.0, "ymin": 0.0, "ymax": 0.0,
        "mu": np.zeros(2)
    }
    mu = hull.mean(axis=0)

    # 各凸包エッジに平行な向きで評価
    for i in range(len(hull)):
        p0, p1 = hull[i], hull[(i+1) % len(hull)]
        edge = p1 - p0
        yaw = np.arctan2(edge[1], edge[0])
        R = rotation_matrix(yaw)
        Hrot = (hull - mu) @ R.T
        xmin, ymin = Hrot.min(axis=0)
        xmax, ymax = Hrot.max(axis=0)
        area = (xmax - xmin) * (ymax - ymin)
        if area < best["area"]:
            best.update(dict(area=area, yaw=yaw,
                             xmin=xmin, xmax=xmax, ymin=ymin, ymax=ymax, mu=mu))

    # 復元
    yaw = best["yaw"]; R = rotation_matrix(yaw)
    xmin, xmax = best["xmin"], best["xmax"]
    ymin, ymax = best["ymin"], best["ymax"]

    rect_local = np.array([[xmin, ymin],[xmax, ymin],[xmax, ymax],[xmin, ymax]], dtype=float)
    rect = rect_local @ R + best["mu"]
    cx_l, cy_l = (xmax+xmin)/2.0, (ymax+ymin)/2.0
    center = np.array([cx_l, cy_l]) @ R + best["mu"]
    sx, sy = (xmax-xmin)/2.0, (ymax-ymin)/2.0

    # 長辺を sx に正規化（yawも安定化）
    if sy > sx:
        sx, sy = sy, sx
        yaw = (yaw + np.pi/2.0)
        yaw = (yaw + np.pi) % (2*np.pi) - np.pi
        R = rotation_matrix(yaw)
        rect = rect_local[:, ::-1] @ R + best["mu"]
        center = np.array([cy_l, cx_l]) @ R + best["mu"]

    return center, np.array([sx, sy]), yaw, rect, best["area"]


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in",  dest="in_path",  type=str, required=True,
                    help="入力 polygon JSON (gml_lod1_extract の出力など)")
    ap.add_argument("--out", dest="out_path", type=str, required=True,
                    help="OBB(after) の出力 JSON パス")
    ap.add_argument("--origin-lat", type=float, required=True,
                    help="原点となる緯度（deg, EPSG:4326）")
    ap.add_argument("--origin-lon", type=float, required=True,
                    help="原点となる経度（deg, EPSG:4326）")
    ap.add_argument("--crs-epsg", type=int, default=None,
                    help="ポリゴンXYのEPSG。省略時は input JSON の `crs` を参照し、無ければ 4326 とみなす。")
    args = ap.parse_args()

    in_path  = Path(args.in_path)
    out_path = Path(args.out_path)

    with open(in_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    polys_in = list(data["polygons"])

    # CRS を決定
    epsg = args.crs_epsg
    if epsg is None:
        crs_str = data.get("crs")
        if isinstance(crs_str, str) and crs_str.upper().startswith("EPSG:"):
            try:
                epsg = int(crs_str.split(":")[1])
            except Exception:
                epsg = None
    if epsg is None:
        epsg = 4326  # fallback

    # 原点 lat/lon → XY(ポリゴンと同じEPSG)
    if epsg == 4326:
        origin_x = args.origin_lon
        origin_y = args.origin_lat
    else:
        tf = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)
        origin_x, origin_y = tf.transform(args.origin_lon, args.origin_lat)

    origin = {
        "lat": args.origin_lat,
        "lon": args.origin_lon,
        "x": float(origin_x),
        "y": float(origin_y),
        "epsg": epsg,
    }

    results_after = []

    for poly in polys_in:
        pid = poly.get("id", "poly")
        pts = np.array(poly["vertices"], dtype=float)

        if len(pts) < 3:
            continue

        # z情報はそのまま引き継ぐ（あれば）
        zmin   = poly.get("zmin")
        zmax   = poly.get("zmax")
        height = poly.get("height")
        source = poly.get("source_gml")

        # OBB 計算（絶対座標系）
        center, halfsize, yaw, rect, area = min_area_rect_calipers(pts)

        # 原点からの相対位置に変換
        center_rel = np.array([center[0] - origin_x, center[1] - origin_y], dtype=float)
        rect_rel   = rect.copy()
        rect_rel[:,0] -= origin_x
        rect_rel[:,1] -= origin_y

        rec = {
            "id": pid,
            "center": [float(center_rel[0]), float(center_rel[1])],
            "half_size": [float(halfsize[0]), float(halfsize[1])],
            "yaw_rad": float(yaw),
            "yaw_deg": float(np.degrees(yaw)),
            "rect_corners": rect_rel.tolist(),
            "area": float(area),
        }
        if zmin   is not None: rec["zmin"]   = float(zmin)
        if zmax   is not None: rec["zmax"]   = float(zmax)
        if height is not None: rec["height"] = float(height)
        if source is not None: rec["source_gml"] = source

        results_after.append(rec)

    out = {
        "version": "0.3",
        "mode": "after",
        "source": str(in_path),
        "origin": origin,
        "results": results_after,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"[OK] OBB count: {len(results_after)}  → {out_path}")


if __name__ == "__main__":
    main()
