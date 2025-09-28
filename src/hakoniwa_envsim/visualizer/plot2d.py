from __future__ import annotations
import matplotlib.pyplot as plt
from matplotlib import patches
from typing import List
import argparse

from hakoniwa_envsim.model.models import VisualArea
from hakoniwa_envsim.model.loader import ModelLoader

def plot_areas(areas: List[VisualArea], show_wind: bool = True, mode: str = "temperature") -> None:
    """Visualize environment areas in ROS 2D coordinates (X forward, Y left).
    mode: "temperature" or "gps"
    """

    fig, ax = plt.subplots(figsize=(6, 6))

    # 可視化する値の選択
    if mode == "temperature":
        values = [a.temperature for a in areas if a.temperature is not None]
        cmap = plt.cm.coolwarm
        label = "Temperature [°C]"
        get_value = lambda a: a.temperature if a.temperature is not None else 0.0
    elif mode == "gps":
        values = [a.gps_strength for a in areas if getattr(a, "gps_strength", None) is not None]
        cmap = plt.cm.RdYlGn
        label = "GPS Strength"
        get_value = lambda a: getattr(a, "gps_strength", 0.0)
    else:
        raise ValueError(f"Unsupported mode: {mode}")

    # 値のレンジ計算
    if values:
        vmin, vmax = min(values), max(values)
        if abs(vmax - vmin) < 1e-6:
            vmin, vmax = vmin - 0.1, vmax + 0.1
    else:
        vmin, vmax = 0.0, 1.0

    # 各エリア描画
    for area in areas:
        aabb = area.aabb2d
        val = get_value(area)
        color = cmap((val - vmin) / (vmax - vmin)) if values else "gray"

        rect = patches.Rectangle(
            (aabb.ymin, aabb.xmin),   # (Ymin, Xmin)
            aabb.ymax - aabb.ymin,    # 幅 = ΔY
            aabb.xmax - aabb.xmin,    # 高さ = ΔX
            linewidth=1,
            edgecolor="black",
            facecolor=color,
            alpha=0.6,
        )
        ax.add_patch(rect)

        # 風向ベクトル
        if show_wind and area.wind_velocity:
            wx, wy, wz = area.wind_velocity
            mag = (wx**2 + wy**2 + wz**2) ** 0.5
            if mag > 1e-6:
                scale = 0.3
                cx, cy = aabb.center()
                ax.arrow(
                    cy, cx, wy * scale, wx * scale,
                    head_width=0.1, head_length=0.15,
                    fc="blue", ec="blue"
                )

    # 軸範囲
    xmin = min(a.aabb2d.xmin for a in areas)
    xmax = max(a.aabb2d.xmax for a in areas)
    ymin = min(a.aabb2d.ymin for a in areas)
    ymax = max(a.aabb2d.ymax for a in areas)

    ax.set_xlim(ymin - 1.0, ymax + 1.0)
    ax.set_ylim(xmin - 1.0, xmax + 1.0)
    ax.invert_xaxis()
    ax.set_aspect("equal", adjustable="box")
    ax.set_xlabel("Y [m] (ROS left is +)")
    ax.set_ylabel("X [m] (ROS forward is +)")
    ax.set_title(f"Hakoniwa Environment Map ({mode})")

    if values:
        sm = plt.cm.ScalarMappable(cmap=cmap, norm=plt.Normalize(vmin=vmin, vmax=vmax))
        sm.set_array([])
        cbar = plt.colorbar(sm, ax=ax)
        cbar.set_label(label)

    plt.show()
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Visualize Hakoniwa Environment Areas in 2D")
    parser.add_argument("--area", default="../examples/datasets/simple_room/area.json")
    parser.add_argument("--property", default="../examples/datasets/simple_room/property.json")
    parser.add_argument("--link", default="../examples/datasets/simple_room/link.json")
    parser.add_argument("--no-wind", action="store_true")
    parser.add_argument("--mode", choices=["temperature", "gps"], default="gps", help="Visualization mode")
    args = parser.parse_args()

    loader = ModelLoader(validate_schema=True)
    areas = loader.load_space_areas(args.area)
    props = loader.load_area_properties(args.property)
    links = loader.load_links(args.link)
    scene = loader.build_visual_areas(areas, props, links)

    plot_areas(scene, show_wind=not args.no_wind, mode=args.mode)
