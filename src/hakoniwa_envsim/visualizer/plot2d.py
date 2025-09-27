from __future__ import annotations
import matplotlib.pyplot as plt
from matplotlib import patches
from typing import List
import argparse

from hakoniwa_envsim.model.models import VisualArea
from hakoniwa_envsim.model.loader import ModelLoader


def plot_areas(areas: List[VisualArea], show_wind: bool = True) -> None:
    """Visualize environment areas in ROS 2D coordinates (X forward, Y left)."""

    fig, ax = plt.subplots(figsize=(6, 6))

    # 温度で塗り分け
    temps = [a.temperature for a in areas if a.temperature is not None]
    vmin, vmax = (min(temps), max(temps)) if temps else (0, 1)

    for area in areas:
        aabb = area.aabb2d
        temp = area.temperature if area.temperature is not None else 0.0
        color = plt.cm.coolwarm((temp - vmin) / (vmax - vmin)) if temps else "gray"

        # Rect: 横軸=Y, 縦軸=X
        rect = patches.Rectangle(
            (aabb.ymin, aabb.xmin),  # (Ymin, Xmin)
            aabb.ymax - aabb.ymin,   # 幅 = ΔY
            aabb.xmax - aabb.xmin,   # 高さ = ΔX
            linewidth=1,
            edgecolor="black",
            facecolor=color,
            alpha=0.6,
        )
        ax.add_patch(rect)

        cx, cy = aabb.center()  # center() は (x,y)
        #ax.text(cy, cx, f"{area.area_id}\nT={temp:.1f}°C",
        #        ha="center", va="center", fontsize=8)

        # 風向ベクトル
        if show_wind and area.wind_velocity:
            wx, wy, wz = area.wind_velocity
            mag = (wx**2 + wy**2 + wz**2) ** 0.5
            if mag > 1e-6:  # 風速の大きさが閾値より大きい場合のみ描画
                ax.arrow(
                    cy, cx, wy, wx,   # ROS座標系 (Y軸=横, X軸=縦)
                    head_width=0.2, head_length=0.3,
                    fc="blue", ec="blue"
                )


    # 軸範囲
    xmin = min(a.aabb2d.xmin for a in areas)
    xmax = max(a.aabb2d.xmax for a in areas)
    ymin = min(a.aabb2d.ymin for a in areas)
    ymax = max(a.aabb2d.ymax for a in areas)

    pad_x, pad_y = 1.0, 1.0
    ax.set_xlim(ymin - pad_y, ymax + pad_y)  # 横軸=Y
    ax.set_ylim(xmin - pad_x, xmax + pad_x)  # 縦軸=X

    # ROS: Y+ は左 → 横軸を反転
    ax.invert_xaxis()

    ax.set_aspect("equal", adjustable="box")
    ax.grid(False)
    ax.set_xlabel("Y [m] (ROS left is +)")
    ax.set_ylabel("X [m] (ROS forward is +)")
    ax.set_title("Hakoniwa Environment Map (ROS 2D)")

    if temps:
        sm = plt.cm.ScalarMappable(cmap="coolwarm", norm=plt.Normalize(vmin=vmin, vmax=vmax))
        sm.set_array([])
        cbar = plt.colorbar(sm, ax=ax)
        cbar.set_label("Temperature [°C]")

    plt.show()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Visualize Hakoniwa Environment Areas in 2D")
    parser.add_argument("--area", default="../examples/models/area.json", help="Path to area.json")
    parser.add_argument("--property", default="../examples/models/property.json", help="Path to property.json")
    parser.add_argument("--link", default="../examples/models/link.json", help="Path to link.json")
    parser.add_argument("--no-wind", action="store_true", help="Disable wind vector drawing")

    args = parser.parse_args()

    loader = ModelLoader(validate_schema=True)
    areas = loader.load_space_areas(args.area)
    props = loader.load_area_properties(args.property)
    links = loader.load_links(args.link)
    scene = loader.build_visual_areas(areas, props, links)

    plot_areas(scene, show_wind=not args.no_wind)
