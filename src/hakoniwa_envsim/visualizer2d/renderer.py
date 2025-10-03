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
            ros_xmin = min(a.aabb2d.xmin for a in areas) 
            ros_xmax = max(a.aabb2d.xmax for a in areas)
            ros_ymin = min(a.aabb2d.ymin for a in areas)
            ros_ymax = max(a.aabb2d.ymax for a in areas)
            # ENU->3857 bbox（画像式に合わせ：X=MercXは y 軸起点、Y=MercYは x 軸起点）
            print(f"ros_xmin={ros_xmin:.3f}, ros_xmax={ros_xmax:.3f}, ros_ymin={ros_ymin:.3f}, ros_ymax={ros_ymax:.3f}")
            X0, Y0 = self.p.proj.lonlat_to_xy(self.p.origin_lon, self.p.origin_lat)
            Xmin = X0
            Ymin = Y0
            Xmax = X0 + (ros_ymax + self.p.offset_y)
            Ymax = Y0 + (ros_xmax + self.p.offset_x)
            cell_dx = min(a.aabb2d.xmax - a.aabb2d.xmin for a in areas)
            cell_dy = min(a.aabb2d.ymax - a.aabb2d.ymin for a in areas)
            print(f"cell_dx={cell_dx:.3f}, cell_dy={cell_dy:.3f}")
            cell_size = max(1e-6, min(cell_dx, cell_dy))
            print(f"cell_size={cell_size:.3f} m")
            print(f"Fetching map tiles for bbox (MercX,Y) = ({Xmin:.3f},{Ymin:.3f}) - ({Xmax:.3f},{Ymax:.3f}) ...")
            img, _, _ = self.ov.fetch(Xmin, Ymin, Xmax, Ymax, 100)
            #ax.imshow(img, extent=(ymax, ymin, xmin, xmax), zorder=0, interpolation="bilinear")
            ax.imshow(
                img,
                extent=(ros_ymin, ros_ymax, ros_xmin, ros_xmax), 
                zorder=0,
                interpolation="bilinear"
        )

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
                wx, wy, wz = a.wind_velocity
                mags.append((wx**2 + wy**2 + wz**2) ** 0.5)
        max_mag = max(mags) if mags else 1.0
        cell_dx = min(a.aabb2d.xmax - a.aabb2d.xmin for a in areas)
        cell_dy = min(a.aabb2d.ymax - a.aabb2d.ymin for a in areas)
        s = (0.5 * max(1e-6, min(cell_dx, cell_dy)) / max(1e-6, max_mag)) * wind_scale

        for a in areas:
            if a.wind_velocity:
                wx, wy, wz = a.wind_velocity  # ENU風速 (x=北+, y=西+)
                mag = (wx**2 + wy**2 + wz**2) ** 0.5
                if mag > 1e-6:
                    cx, cy = a.aabb2d.center()  # (x, y) in ENU
                    # プロット座標系に合わせて (y, x) で配置
                    ax.arrow(cy, cx,
                            wy * s,   # 横成分 (Y軸方向)
                            wx * s,   # 縦成分 (X軸方向)
                            head_width=0.5*s, head_length=0.25*s,
                            fc="blue", ec="blue", length_includes_head=True, zorder=4)


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
        ax.set_xlabel("N [m]")
        ax.set_ylabel("E [m]")
        sm = plt.cm.ScalarMappable(cmap=cmap, norm=norm); sm.set_array([])
        cbar = plt.colorbar(sm, ax=ax, fraction=0.035, pad=0.02); cbar.set_label(cbar_label)
        plt.tight_layout(); plt.show()
