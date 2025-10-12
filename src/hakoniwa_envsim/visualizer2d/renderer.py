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
    def __init__(self, projector: GeoProjector, overlay: TileOverlay | None):
        self.p = projector
        self.ov = overlay

    def draw(self, areas: List[VisualArea], mode: str, wind_scale: float, markers: list[Marker]):
        fig, ax = plt.subplots(figsize=(10,8), dpi=120)

        # 値/カラーマップ
        if mode == "temperature":
            values = [a.temperature for a in areas if a.temperature is not None]
            getv = lambda a: a.temperature or 0.0
            cmap, cbar_label, as_alpha = plt.cm.coolwarm, "Temperature [°C]", False
        else:
            values = [a.gps_strength for a in areas if getattr(a, "gps_strength", None) is not None]
            getv = lambda a: getattr(a, "gps_strength", 0.0)
            cmap, cbar_label, as_alpha = plt.cm.Reds, "GPS Weakness (red=low strength)", True
        vmin, vmax = (min(values), max(values)) if values else (0.0, 1.0)
        if abs(vmax-vmin)<1e-6: vmin, vmax = vmin-0.1, vmax+0.1
        norm = Normalize(vmin=vmin, vmax=vmax)

        # 背景地図
        if self.ov:
            # === 1) ROS座標系の範囲（m） ======================================
            north_min_m = min(a.aabb2d.xmin for a in areas)
            north_max_m = max(a.aabb2d.xmax for a in areas)
            west_min_m  = min(a.aabb2d.ymin for a in areas)
            west_max_m  = max(a.aabb2d.ymax for a in areas)

            print(f"north=[{north_min_m},{north_max_m}], west=[{west_min_m},{west_max_m}]")

            # === 2) 原点をWebMercatorに変換 ====================================
            merc_origin_x_m, merc_origin_y_m = self.p.proj.lonlat_to_xy(
                self.p.origin_lon, self.p.origin_lat
            )

            # === 3) ROS→Mercator変換式（X:北+, Y:西+） ========================
            merc_x_min_m = merc_origin_x_m - (west_max_m + self.p.offset_y)  # 東方向は負
            merc_x_max_m = merc_origin_x_m - (west_min_m + self.p.offset_y)
            merc_y_min_m = merc_origin_y_m + (north_min_m + self.p.offset_x)
            merc_y_max_m = merc_origin_y_m + (north_max_m + self.p.offset_x)

            print(f"MercX=[{merc_x_min_m:.3f},{merc_x_max_m:.3f}] ΔX={merc_x_max_m-merc_x_min_m:.3f} m")
            print(f"MercY=[{merc_y_min_m:.3f},{merc_y_max_m:.3f}] ΔY={merc_y_max_m-merc_y_min_m:.3f} m")

            # === 4) 背景地図の取得 =============================================
            img, _, _ = self.ov.fetch(merc_x_min_m, merc_y_min_m, merc_x_max_m, merc_y_max_m, 100)

            # === 5) プロット（x軸=西(+), y軸=北(+)）=============================
            extent_west_north = (west_min_m, west_max_m, north_min_m, north_max_m)
            ax.imshow(
                img,
                extent=(west_min_m, west_max_m, north_min_m, north_max_m),  # (x_min, x_max, y_min, y_max)
                origin="upper",
                interpolation="bilinear",
                zorder=0,
            )

#            ax.imshow(
#                img,
##                extent=(ros_ymin, ros_ymax, ros_xmin, ros_xmax), 
 ##               zorder=0,
   #             interpolation="bilinear"
    #        )

        # グリッド描画（横=X, 縦=Y）
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
                (aabb.ymin, aabb.xmin),          # 左下 = (Ymin, Xmin)
                aabb.ymax - aabb.ymin,           # 横幅 = ΔY
                aabb.xmax - aabb.xmin,           # 縦幅 = ΔX
                linewidth=0, facecolor=face_rgba, zorder=2
            )
            ax.add_patch(rect)

        # 風ベクトルスケール
        mags = []
        for a in areas:
            if a.wind_velocity:
                vx, vy, vz = a.wind_velocity  # ROS: North(+), West(+), Up(+)
                mags.append((vx*vx + vy*vy + vz*vz) ** 0.5)
        max_mag = max(mags) if mags else 1.0
        cell_dx = min(a.aabb2d.xmax - a.aabb2d.xmin for a in areas)
        cell_dy = min(a.aabb2d.ymax - a.aabb2d.ymin for a in areas)
        s = (0.5 * max(1e-6, min(cell_dx, cell_dy)) / max(1e-6, max_mag)) * wind_scale

        for a in areas:
            if a.wind_velocity:
                vx, vy, vz = a.wind_velocity           # vx:North, vy:West, vz:Up
                x_north, y_west = a.aabb2d.center()    # (X_ros, Y_ros)
                # 描画座標は (x=西, y=北) なので：
                start_x = y_west
                start_y = x_north
                dx = -vy * s     # 西(+)方向
                dy = vx * s     # 北(+)方向

                ax.arrow(start_x, start_y, dx, dy,
                        head_width=0.5*s, head_length=0.25*s,
                        fc="yellow", ec="yellow",
                        length_includes_head=True, zorder=4)

        # マーカー
        for m in markers or []:
            x_m, y_m = self.p.lonlat_to_enu(m.lat, m.lon)  # ENU座標 (x: forward, y: left)
            # そのまま (横=Y, 縦=X) で描画すればOK
            ax.plot(y_m, x_m, marker='o', markersize=6,
                    mec='black', mfc='yellow', zorder=6)
            if m.label:
                ax.annotate(m.label, (y_m, x_m),
                            xytext=(5, 8), textcoords='offset points',
                            fontsize=12,
                            bbox=dict(boxstyle="round,pad=0.25",
                                    fc="white", ec="gray", alpha=0.85),
                            zorder=7)

        # 軸・凡例
        xmin = min(a.aabb2d.xmin for a in areas); xmax = max(a.aabb2d.xmax for a in areas)
        ymin = min(a.aabb2d.ymin for a in areas); ymax = max(a.aabb2d.ymax for a in areas)
        ax.set_xlim(ymin - 1.0, ymax + 1.0)
        ax.set_ylim(xmin - 1.0, xmax + 1.0)
        #ax.invert_xaxis()
        ax.set_aspect("equal", adjustable="box")
        ax.set_xlabel("E [m]")
        ax.set_ylabel("N [m]")
        sm = plt.cm.ScalarMappable(cmap=cmap, norm=norm); sm.set_array([])
        cbar = plt.colorbar(sm, ax=ax, fraction=0.035, pad=0.02); cbar.set_label(cbar_label)
        plt.tight_layout(); plt.show()
