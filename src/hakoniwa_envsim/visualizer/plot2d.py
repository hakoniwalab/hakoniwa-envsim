from __future__ import annotations
import argparse
from typing import List, Callable

import matplotlib.pyplot as plt
from matplotlib import patches
from matplotlib.colors import Normalize

from hakoniwa_envsim.model.models import VisualArea
from hakoniwa_envsim.model.loader import ModelLoader


def plot_areas(
    areas: List[VisualArea],
    show_wind: bool = True,
    mode: str = "temperature",
    wind_scale: float = 1.0,
) -> None:
    import matplotlib.pyplot as plt
    from matplotlib import patches
    from matplotlib.colors import Normalize

    fig, ax = plt.subplots(figsize=(6, 6))

    # ---- 値の選択＆色設定 ----
    if mode == "temperature":
        values = [a.temperature for a in areas if a.temperature is not None]
        get_value = lambda a: a.temperature if a.temperature is not None else 0.0
        cmap = plt.cm.coolwarm
        cbar_label = "Temperature [°C]"
        as_transparent_overlay = False
    elif mode == "gps":
        values = [a.gps_strength for a in areas if getattr(a, "gps_strength", None) is not None]
        get_value = lambda a: getattr(a, "gps_strength", 0.0)
        # カラーバー用（弱い=赤、強い=白寄り）※透明度は別で付ける
        cmap = plt.cm.Reds
        cbar_label = "GPS Weakness"
        as_transparent_overlay = True
    else:
        raise ValueError(f"Unsupported mode: {mode}")

    # ---- 正規化 ----
    if values:
        vmin, vmax = min(values), max(values)
        if abs(vmax - vmin) < 1e-6:
            vmin, vmax = vmin - 0.1, vmax + 0.1
    else:
        vmin, vmax = 0.0, 1.0
    norm = Normalize(vmin=vmin, vmax=vmax)

    # ---- 風矢印スケール自動調整 ----
    cell_dx = min(a.aabb2d.xmax - a.aabb2d.xmin for a in areas)
    cell_dy = min(a.aabb2d.ymax - a.aabb2d.ymin for a in areas)
    cell_size = max(1e-6, min(cell_dx, cell_dy))

    wind_mags = []
    for a in areas:
        if a.wind_velocity:
            wx, wy, wz = a.wind_velocity
            wind_mags.append((wx**2 + wy**2 + wz**2) ** 0.5)
    max_mag = max(wind_mags) if wind_mags else 1.0
    base_scale = 0.5 * cell_size / max(1e-6, max_mag)

    # ---- 描画 ----
    for area in areas:
        aabb = area.aabb2d
        val = get_value(area)

        if mode == "gps" and as_transparent_overlay:
            # 強い=透明、弱い=赤
            strength_norm = norm(val)            # 0..1 (弱〜強)
            weakness = 1.0 - strength_norm       # 0..1 (強〜弱)
            face_rgba = (1.0, 0.0, 0.0, weakness)  # 赤 + 透明度
            edgecolor = (0, 0, 0, 0.4)
        else:
            face_rgba = cmap(norm(val))
            edgecolor = "black"

        rect = patches.Rectangle(
            (aabb.ymin, aabb.xmin),
            aabb.ymax - aabb.ymin,
            aabb.xmax - aabb.xmin,
            linewidth=1,
            edgecolor=edgecolor,
            facecolor=face_rgba,
        )
        ax.add_patch(rect)

        # 風ベクトル
        if show_wind and area.wind_velocity:
            wx, wy, wz = area.wind_velocity
            mag = (wx**2 + wy**2 + wz**2) ** 0.5
            if mag > 1e-6:
                cx, cy = aabb.center()
                s = base_scale * wind_scale
                ax.arrow(
                    cy, cx, wy * s, wx * s,
                    head_width=0.5 * s, head_length=0.25 * s,
                    fc="blue", ec="blue", length_includes_head=True
                )

    # ---- 軸など ----
    xmin = min(a.aabb2d.xmin for a in areas); xmax = max(a.aabb2d.xmax for a in areas)
    ymin = min(a.aabb2d.ymin for a in areas); ymax = max(a.aabb2d.ymax for a in areas)
    ax.set_xlim(ymin - 1.0, ymax + 1.0); ax.set_ylim(xmin - 1.0, xmax + 1.0)
    ax.invert_xaxis()
    ax.set_aspect("equal", adjustable="box")
    ax.grid(False)
    ax.set_xlabel("Y [m] (ROS left is +)")
    ax.set_ylabel("X [m] (ROS forward is +)")
    ax.set_title(f"Hakoniwa Environment Map ({mode})")

    # カラーバー（GPSは「弱さ」を表示）
    if mode == "gps" and as_transparent_overlay:
        import numpy as np
        from matplotlib.cm import ScalarMappable
        # 0..1 の「弱さ」をバーに出す
        weak_norm = Normalize(vmin=0.0, vmax=1.0)
        sm = ScalarMappable(cmap=plt.cm.Reds, norm=weak_norm)
        sm.set_array(np.linspace(0, 1, 256))
        cbar = plt.colorbar(sm, ax=ax)
        cbar.set_label("GPS Weakness (red=low strength)")
    else:
        sm = plt.cm.ScalarMappable(cmap=cmap, norm=norm)
        sm.set_array([])
        cbar = plt.colorbar(sm, ax=ax)
        cbar.set_label(cbar_label)

    plt.show()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Visualize Hakoniwa Environment Areas in 2D")
    parser.add_argument("--area", default="../examples/datasets/simple_room/area.json")
    parser.add_argument("--property", default="../examples/datasets/simple_room/property.json")
    parser.add_argument("--link", default="../examples/datasets/simple_room/link.json")
    parser.add_argument("--no-wind", action="store_true")
    parser.add_argument("--mode", choices=["temperature", "gps"], default="gps",
                        help="Visualization mode")
    parser.add_argument("--wind-scale", type=float, default=1.0,
                        help="Additional multiplier for wind arrows (default: 1.0)")
    args = parser.parse_args()

    loader = ModelLoader(validate_schema=True)
    areas = loader.load_space_areas(args.area)
    props = loader.load_area_properties(args.property)
    links = loader.load_links(args.link)
    scene = loader.build_visual_areas(areas, props, links)

    plot_areas(scene, show_wind=not args.no_wind, mode=args.mode, wind_scale=args.wind_scale)
