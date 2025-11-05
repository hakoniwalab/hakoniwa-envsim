#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Minimum-Area Bounding Rectangle (Rotating Calipers)
- 入力: polygons(2D) の JSON
- 出力:
    --mode before : 変換前(元ポリゴン)のみプロット & JSON保存
    --mode after  : 変換後(OBB)のみプロット & JSON保存
    --mode both   : 両方（既定）
- 可視化: matplotlib で重ね描き（both時は両方）

使い方:
  pip install numpy matplotlib
  python calipers_demo.py --in poly_demo.json --out obb_after.json --mode both
"""
import json
import argparse
from pathlib import Path
import numpy as np
import matplotlib.pyplot as plt

# ---------- geometry utils ----------
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
    ap.add_argument("--in",  dest="in_path",  type=str, default="data.json")
    ap.add_argument("--out", dest="out_path", type=str, default="obb_after.json",
                    help="after(変換後)の保存先。--out-after と同義。")
    ap.add_argument("--out-before", type=str, default=None,
                    help="before(変換前)の保存先。未指定なら <out_path基準>_before.json。")
    ap.add_argument("--out-after",  type=str, default=None,
                    help="after(変換後)の保存先。指定があれば --out より優先。")
    ap.add_argument("--mode", choices=["before", "after", "both"], default="both",
                    help="プロット/保存の対象を選択（既定 both）")
    ap.add_argument("--no-show", action="store_true", help="グラフ非表示")
    ap.add_argument("--no-legend", action="store_true", help="凡例を非表示にする")
    ap.add_argument("--head", type=int, default=None, help="先頭 N 件だけ処理")
    ap.add_argument("--bbox", type=float, nargs=4, metavar=("XMIN","XMAX","YMIN","YMAX"),
                    help="矩形範囲で抽出（どれかの頂点が入れば採用）")
    ap.add_argument("--min-area", type=float, default=0.0, help="ポリゴン面積の下限（m^2）")
    args = ap.parse_args()

    in_path  = Path(args.in_path)
    out_after = Path(args.out_after) if args.out_after else Path(args.out_path)
    out_before = Path(args.out_before) if args.out_before else Path(out_after.with_suffix("").as_posix() + "_before.json")

    with open(in_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    polys_in = list(data["polygons"])

    # 前処理：head / bbox / min-area
    if args.head and args.head > 0:
        polys_in = polys_in[:args.head]

    def in_bbox(verts, bb):
        xmin,xmax,ymin,ymax = bb
        verts = np.asarray(verts, float)
        return np.any(
            (verts[:,0] >= xmin) & (verts[:,0] <= xmax) &
            (verts[:,1] >= ymin) & (verts[:,1] <= ymax)
        )

    if args.bbox is not None:
        polys_in = [p for p in polys_in if in_bbox(p["vertices"], args.bbox)]

    if args.min_area > 0:
        tmp = []
        for p in polys_in:
            if polygon_area(np.asarray(p["vertices"], float)) >= args.min_area:
                tmp.append(p)
        polys_in = tmp

    # 準備
    results_after = []
    results_before = []
    draw_before = args.mode in ("before", "both")
    draw_after  = args.mode in ("after", "both")

    fig, ax = plt.subplots(figsize=(7, 7))

    for poly in polys_in:
        pid = poly.get("id", "poly")
        pts = np.array(poly["vertices"], dtype=float)

        # オプションで付与されている高さ情報を保持（zmin/zmax/height はそのまま引き継ぐ）
        zmin = poly.get("zmin", None)
        zmax = poly.get("zmax", None)
        height = poly.get("height", None)

        if draw_before:
            pts_closed = np.vstack([pts, pts[0]])
            ax.plot(pts_closed[:,0], pts_closed[:,1], linewidth=1.5, label=None)
            rec = {
                "id": pid,
                "vertices": [[float(x), float(y)] for x, y in pts]
            }
            if zmin is not None: rec["zmin"] = float(zmin)
            if zmax is not None: rec["zmax"] = float(zmax)
            if height is not None: rec["height"] = float(height)
            results_before.append(rec)

        if draw_after:
            center, halfsize, yaw, rect, area = min_area_rect_calipers(pts)
            rect_closed = np.vstack([rect, rect[0]])
            ax.plot(rect_closed[:,0], rect_closed[:,1], linestyle="--", linewidth=1.5, label=None)
            rec = {
                "id": pid,
                "center": [float(center[0]), float(center[1])],
                "half_size": [float(halfsize[0]), float(halfsize[1])],
                "yaw_rad": float(yaw),
                "yaw_deg": float(np.degrees(yaw)),
                "rect_corners": rect.tolist(),
                "area": float(area)
            }
            if zmin is not None: rec["zmin"] = float(zmin)
            if zmax is not None: rec["zmax"] = float(zmax)
            if height is not None: rec["height"] = float(height)
            results_after.append(rec)

    ax.set_aspect('equal', 'box')
    if args.mode == "before":
        ax.set_title("Polygons (Before conversion)")
    elif args.mode == "after":
        ax.set_title("Minimum-Area Bounding Rectangle (After conversion)")
    else:
        ax.set_title("Before & After (Polygons + OBB)")

    if not args.no_legend:
        # 多数描画時は凡例なしの方が軽いのでデフォは非表示扱い
        pass
    ax.grid(True)

    # 表示
    if not args.no_show:
        plt.show()

    # 保存
    if draw_before:
        out_b = {
            "version": "0.2",
            "mode": "before",
            "source": str(in_path),
            "polygons": results_before
        }
        with open(out_before, "w", encoding="utf-8") as f:
            json.dump(out_b, f, ensure_ascii=False, indent=2)
        print(f"[OK] Saved (before) → {out_before}")

    if draw_after:
        out_a = {
            "version": "0.2",
            "mode": "after",
            "source": str(in_path),
            "results": results_after
        }
        with open(out_after, "w", encoding="utf-8") as f:
            json.dump(out_a, f, ensure_ascii=False, indent=2)
        print(f"[OK] Saved (after)  → {out_after}")

if __name__ == "__main__":
    main()
