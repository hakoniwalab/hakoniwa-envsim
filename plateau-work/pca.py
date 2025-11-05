#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JSON → 2D OBB(回転付き直方体) 変換＆可視化
- 入力: polygons（2D頂点列）のJSON
- 出力: OBBパラメータ（中心, 半サイズ, yaw, 4隅）を JSON 保存
- 可視化: 元ポリゴンとOBBをmatplotlibで重ね描き

使い方:
  python obb_demo.py --in poly_demo.json --out obb_result.json

依存: numpy, matplotlib
  pip install numpy matplotlib
"""
import json
import argparse
from pathlib import Path
import numpy as np
import matplotlib.pyplot as plt


def pca_obb_2d(points: np.ndarray):
    """
    PCAで2D OBBを推定
    Args:
        points: (N,2) のnumpy配列 (x,y)
    Returns:
        center:   (2,)  OBB中心（元座標）
        halfsize: (2,)  (sx, sy) ＝ (幅/2, 奥行/2)
        yaw:      float  ラジアン（x軸→主成分方向）
        rect:     (4,2)  OBBの4隅（元座標、反時計回り）
    """
    assert points.ndim == 2 and points.shape[1] == 2, "points must be (N,2)"
    mu = points.mean(axis=0)
    X = points - mu

    # 2x2 共分散 → 固有分解（昇順）
    C = (X.T @ X) / len(points)
    w, V = np.linalg.eigh(C)
    v_major = V[:, 1]  # 最大固有値の固有ベクトル
    yaw = float(np.arctan2(v_major[1], v_major[0]))

    # PCA軸へ回す（ここでは -yaw でなく、軸合わせの行列を自前定義）
    R = np.array([[ np.cos(yaw),  np.sin(yaw)],
                  [-np.sin(yaw),  np.cos(yaw)]], dtype=float)
    Xr = X @ R.T

    # 回転後にAABBを取り、その中心とサイズを計算
    xmin, ymin = Xr.min(axis=0)
    xmax, ymax = Xr.max(axis=0)
    W, D = (xmax - xmin), (ymax - ymin)
    center_rot = np.array([(xmax + xmin)/2.0, (ymax + ymin)/2.0], dtype=float)

    # 元座標へ復元
    center = mu + center_rot @ R
    sx, sy = W/2.0, D/2.0

    # 4隅（回転後フレームの矩形）→ 元座標へ戻す
    rect_rot = np.array([
        [ xmin, ymin ],
        [ xmax, ymin ],
        [ xmax, ymax ],
        [ xmin, ymax ]
    ], dtype=float)
    rect = rect_rot @ R + mu
    return center, np.array([sx, sy], dtype=float), yaw, rect


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in",  dest="in_path",  type=str, default="poly_demo.json")
    ap.add_argument("--out", dest="out_path", type=str, default="obb_result.json")
    ap.add_argument("--show", action="store_true", help="グラフ表示（デフォルトON）")
    ap.add_argument("--no-show", dest="no_show", action="store_true", help="グラフ非表示")
    args = ap.parse_args()

    in_path  = Path(args.in_path)
    out_path = Path(args.out_path)

    with open(in_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    results = []
    fig, ax = plt.subplots(figsize=(7, 7))

    for poly in data["polygons"]:
        pid = poly.get("id", "poly")
        pts = np.array(poly["vertices"], dtype=float)
        # OBB
        center, halfsize, yaw, rect = pca_obb_2d(pts)

        # 可視化：元ポリゴン（閉じる）
        pts_closed = np.vstack([pts, pts[0]])
        ax.plot(pts_closed[:,0], pts_closed[:,1], linewidth=2, label=f"{pid}")

        # 可視化：OBB（閉じる）
        rect_closed = np.vstack([rect, rect[0]])
        ax.plot(rect_closed[:,0], rect_closed[:,1], linestyle="--", linewidth=2, label=f"OBB({pid})")

        results.append({
            "id": pid,
            "center":   [float(center[0]), float(center[1])],
            "half_size":[float(halfsize[0]), float(halfsize[1])],
            "yaw_rad":  float(yaw),
            "yaw_deg":  float(np.degrees(yaw)),
            "rect_corners": rect.tolist()
        })

    ax.set_aspect('equal', 'box')
    ax.set_title("JSON → 2D OBB (PCA)")
    ax.legend()
    ax.grid(True)

    if not args.no_show:
        plt.show()

    out = {"version": "0.1", "results": results, "source": str(in_path)}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"[OK] Saved OBB results → {out_path}")


if __name__ == "__main__":
    main()
