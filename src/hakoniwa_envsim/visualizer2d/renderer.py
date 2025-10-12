# renderer.py
from typing import List
import numpy as np
import matplotlib.pyplot as plt
from matplotlib import patches
from matplotlib.colors import Normalize
from .markers import Marker
from .projection import GeoProjector
from .overlay import TileOverlay
from hakoniwa_envsim.model.models import VisualArea


class PlotRenderer:
    """
    MAP座標系（X=East+, Y=North+, Z=Up+）前提の可視化レンダラ。
    ※事前に scene（VisualArea）が ROS→MAP 変換されていること。
    """

    def __init__(self, projector: GeoProjector, overlay: TileOverlay | None):
        self.p = projector
        self.ov = overlay

    def draw(self, areas: List[VisualArea], mode: str, wind_scale: float, markers: list[Marker]):
        fig, ax = plt.subplots(figsize=(10, 8), dpi=120)

        # === カラーマップ設定 =================================================
        if mode == "temperature":
            values = [a.temperature for a in areas if a.temperature is not None]
            getv = lambda a: a.temperature or 0.0
            cmap, cbar_label, as_alpha = plt.cm.coolwarm, "Temperature [°C]", False
        else:
            values = [a.gps_strength for a in areas if getattr(a, "gps_strength", None) is not None]
            getv = lambda a: getattr(a, "gps_strength", 0.0)
            cmap, cbar_label, as_alpha = plt.cm.Reds, "GPS Weakness (red=low strength)", True

        vmin, vmax = (min(values), max(values)) if values else (0.0, 1.0)
        if abs(vmax - vmin) < 1e-6:
            vmin, vmax = vmin - 0.1, vmax + 0.1
        norm = Normalize(vmin=vmin, vmax=vmax)

        # === 背景地図の描画 ====================================================
        if self.ov:
            east_min_m  = min(a.aabb2d.xmin for a in areas)
            east_max_m  = max(a.aabb2d.xmax for a in areas)
            north_min_m = min(a.aabb2d.ymin for a in areas)
            north_max_m = max(a.aabb2d.ymax for a in areas)

            print(f"east=[{east_min_m},{east_max_m}], north=[{north_min_m},{north_max_m}]")

            merc_origin_x_m, merc_origin_y_m = self.p.proj.lonlat_to_xy(
                self.p.origin_lon, self.p.origin_lat
            )

            merc_x_min_m = merc_origin_x_m + (east_min_m + self.p.offset_x)
            merc_x_max_m = merc_origin_x_m + (east_max_m + self.p.offset_x)
            merc_y_min_m = merc_origin_y_m + (north_min_m + self.p.offset_y)
            merc_y_max_m = merc_origin_y_m + (north_max_m + self.p.offset_y)

            print(
                f"MercX=[{merc_x_min_m:.3f},{merc_x_max_m:.3f}] ΔX={merc_x_max_m - merc_x_min_m:.3f} m"
            )
            print(
                f"MercY=[{merc_y_min_m:.3f},{merc_y_max_m:.3f}] ΔY={merc_y_max_m - merc_y_min_m:.3f} m"
            )

            img, _, _ = self.ov.fetch(merc_x_min_m, merc_y_min_m, merc_x_max_m, merc_y_max_m, 100)

            ax.imshow(
                img,
                extent=(east_min_m, east_max_m, north_min_m, north_max_m),
                origin="upper",
                interpolation="bilinear",
                zorder=0,
            )

        # === グリッド塗り分け ==================================================
        for a in areas:
            aabb = a.aabb2d
            val = getv(a)
            if mode == "gps" and as_alpha:
                weakness = 1.0 - val
                alpha = min(0.5, weakness)
                face_rgba = (1.0, 0.0, 0.0, alpha)
            else:
                face_rgba = cmap(norm(val))

            rect = patches.Rectangle(
                (aabb.xmin, aabb.ymin),  # 左下 (East, North)
                aabb.xmax - aabb.xmin,   # 横幅 ΔE
                aabb.ymax - aabb.ymin,   # 縦幅 ΔN
                linewidth=0,
                facecolor=face_rgba,
                zorder=2,
            )
            ax.add_patch(rect)

        # === 風ベクトル描画 ====================================================
        mags = []
        for a in areas:
            if a.wind_velocity:
                vx, vy, vz = a.wind_velocity  # vx:East(+), vy:North(+)
                mags.append((vx * vx + vy * vy + vz * vz) ** 0.5)
        max_mag = max(mags) if mags else 1.0
        cell_dx = min(a.aabb2d.xmax - a.aabb2d.xmin for a in areas)
        cell_dy = min(a.aabb2d.ymax - a.aabb2d.ymin for a in areas)
        s = (0.5 * max(1e-6, min(cell_dx, cell_dy)) / max(1e-6, max_mag)) * wind_scale

        for a in areas:
            if a.wind_velocity:
                vx, vy, vz = a.wind_velocity
                cx, cy = a.aabb2d.center()
                ax.arrow(
                    cx, cy,
                    vx * s, vy * s,
                    head_width=0.5 * s, head_length=0.25 * s,
                    fc="yellow", ec="yellow",
                    length_includes_head=True, zorder=4,
                )

        # === マーカー描画 ======================================================
        for m in markers or []:
            x_m, y_m = self.p.lonlat_to_enu(m.lat, m.lon)  # ENUとMAPは同軸
            ax.plot(x_m, y_m, marker='o', markersize=6,
                    mec='black', mfc='yellow', zorder=6)
            if m.label:
                ax.annotate(
                    m.label, (x_m, y_m),
                    xytext=(5, 8), textcoords='offset points',
                    fontsize=12,
                    bbox=dict(boxstyle="round,pad=0.25",
                              fc="white", ec="gray", alpha=0.85),
                    zorder=7,
                )

        # === 軸・凡例 ==========================================================
        east_min_m  = min(a.aabb2d.xmin for a in areas)
        east_max_m  = max(a.aabb2d.xmax for a in areas)
        north_min_m = min(a.aabb2d.ymin for a in areas)
        north_max_m = max(a.aabb2d.ymax for a in areas)

        ax.set_xlim(east_min_m - 1.0, east_max_m + 1.0)
        ax.set_ylim(north_min_m - 1.0, north_max_m + 1.0)
        ax.set_aspect("equal", adjustable="box")
        ax.set_xlabel("E [m]")
        ax.set_ylabel("N [m]")

        sm = plt.cm.ScalarMappable(cmap=cmap, norm=norm)
        sm.set_array([])
        cbar = plt.colorbar(sm, ax=ax, fraction=0.035, pad=0.02)
        cbar.set_label(cbar_label)

        plt.tight_layout()
        plt.show()
