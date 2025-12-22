#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OBB JSON を可視化するスクリプト

使い方:
  python visualize_obb.py --in obb.json --out obb_plot.png
"""

import argparse
import json
from pathlib import Path
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.patches import Polygon as MPLPolygon


def plot_obb(items, title="OBB Visualization", output_path=None):
    """OBBの矩形を描画"""
    fig, ax = plt.subplots(figsize=(12, 12))
    
    colors = plt.cm.tab20(np.linspace(0, 1, 20))
    
    for i, item in enumerate(items):
        rect_corners = np.array(item["rect_corners"])
        
        # 色を循環
        color = colors[i % len(colors)]
        
        # 矩形を描画
        poly = MPLPolygon(rect_corners, fill=True, alpha=0.6, 
                         edgecolor='black', facecolor=color, linewidth=0.5)
        ax.add_patch(poly)
        
        # 中心点を描画
        center = item["center"]
        ax.plot(center[0], center[1], 'k.', markersize=1)
    
    ax.set_aspect('equal')
    ax.grid(True, alpha=0.3)
    ax.set_xlabel('X (East-West) [m]')
    ax.set_ylabel('Y (North-South) [m]')
    ax.set_title(title)
    
    # 範囲を自動調整
    all_corners = []
    for item in items:
        all_corners.extend(item["rect_corners"])
    all_corners = np.array(all_corners)
    
    if len(all_corners) > 0:
        margin = 50
        xmin, xmax = all_corners[:, 0].min() - margin, all_corners[:, 0].max() + margin
        ymin, ymax = all_corners[:, 1].min() - margin, all_corners[:, 1].max() + margin
        ax.set_xlim(xmin, xmax)
        ax.set_ylim(ymin, ymax)
    
    plt.tight_layout()
    
    if output_path:
        plt.savefig(output_path, dpi=150, bbox_inches='tight')
        print(f"[OK] Saved plot → {output_path}")
    else:
        plt.show()
    
    plt.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="in_path", required=True, 
                    help="Input OBB JSON")
    ap.add_argument("--out", dest="out_path", default=None,
                    help="Output image path (png/pdf/svg). If not specified, show plot.")
    args = ap.parse_args()
    
    in_path = Path(args.in_path)
    
    with open(in_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    items = data.get("results", data.get("polygons", []))
    
    if not items:
        raise SystemExit("[ERR] No items found in JSON")
    
    print(f"[INFO] Total buildings: {len(items)}")
    
    # 座標系情報を表示
    coord_sys = data.get("coordinate_system", "unknown")
    origin = data.get("origin")
    bounds = data.get("bounds")
    
    title_parts = [f"OBB Visualization ({len(items)} buildings)"]
    if coord_sys == "relative" and origin:
        title_parts.append(f"Origin: {origin['lat']:.4f}°, {origin['lon']:.4f}°")
    if bounds:
        title_parts.append(f"Bounds: ±{bounds['ns_m']}m (NS), ±{bounds['ew_m']}m (EW)")
    
    title = "\n".join(title_parts)
    
    plot_obb(items, title=title, output_path=args.out_path)


if __name__ == "__main__":
    main()