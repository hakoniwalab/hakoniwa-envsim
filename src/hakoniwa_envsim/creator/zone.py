from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Dict, Tuple
import math
import random 
import numpy as np

Pos3 = Tuple[float, float, float]

class ZoneEffect:
    """
    Zone定義に基づいて風ベクトルを修正するクラス
    """

    def __init__(self, zone_def):
        self.name = zone_def["name"]
        self.shape = zone_def["shape"]
        self.effect = zone_def["effect"]
        self.priority = zone_def.get("priority", 0)
        self.active = zone_def.get("active", None)

        # Turbulence 用乱数シード制御
        if self.effect["mode"] == "turbulence":
            seed = self.effect["turbulence"].get("seed", None)
            if seed is not None:
                random.seed(seed)

    # ============================================================
    # 形状判定
    # ============================================================
    def contains(self, pos_m):
        """
        pos_m = (x, y, z) がこのZoneに含まれるか判定
        """
        if "circle" in self.shape:
            return self._contains_circle(pos_m)
        elif "rect" in self.shape:
            return self._contains_rect(pos_m)
        return False

    def _contains_circle(self, pos_m):
        cx, cy = self.shape["circle"]["center_m"]
        r = self.shape["circle"]["radius_m"]
        dx, dy = pos_m[0] - cx, pos_m[1] - cy
        return dx * dx + dy * dy <= r * r

    def _contains_rect(self, pos_m):
        cx, cy = self.shape["rect"]["center_m"]
        sx, sy = self.shape["rect"]["size_m"]
        return (
            abs(pos_m[0] - cx) <= sx / 2
            and abs(pos_m[1] - cy) <= sy / 2
        )

    # ============================================================
    # Effect 適用
    # ============================================================
    def apply(self, wind_ms, pos_m):
        """
        このZoneのeffectを与えられた風ベクトルに適用
        wind_ms: np.array([vx, vy, vz])
        pos_m:   (x, y, z)
        """
        mode = self.effect["mode"]

        if mode == "absolute":
            return np.array(self.effect["wind_ms"])
        elif mode == "scale":
            return np.array(wind_ms) * self.effect["scale"]
        elif mode == "add":
            return np.array(wind_ms) + np.array(self.effect["add_ms"])
        elif mode == "vortex":
            return self._apply_vortex(wind_ms, pos_m)
        elif mode == "turbulence":
            return self._apply_turbulence(wind_ms)
        else:
            return wind_ms

    # ============================================================
    # Vortex モード
    # ============================================================
    def _apply_vortex(self, wind_ms, pos_m):
        vdef = self.effect["vortex"]
        cx, cy = vdef["center_m"]
        dx, dy = pos_m[0] - cx, pos_m[1] - cy
        r = math.sqrt(dx * dx + dy * dy)
        if r < vdef.get("r_min_m", 0.1):
            return wind_ms  # 中心近くは無視

        gain = vdef["gain"] / r
        if vdef.get("decay") == "gaussian":
            sigma = vdef.get("sigma_m", 10.0)
            gain *= math.exp(-(r * r) / (2 * sigma * sigma))

        # 回転方向
        if vdef.get("clockwise", True):
            vx, vy = gain * (-dy / r), gain * (dx / r)
        else:
            vx, vy = gain * (dy / r), gain * (-dx / r)

        # 最大速度制限
        max_ms = vdef.get("max_ms", None)
        vec = np.array([vx, vy, 0.0])
        if max_ms is not None:
            norm = np.linalg.norm(vec)
            if norm > max_ms:
                vec = vec * (max_ms / norm)

        return np.array(wind_ms) + vec

    # ============================================================
    # Turbulence モード
    # ============================================================
    def _apply_turbulence(self, wind_ms):
        tdef = self.effect["turbulence"]
        std = tdef["std_ms"]
        model = tdef["type"]

        if model == "gauss":
            noise = np.random.normal(0, std, 3)
        elif model == "perlin":
            # TODO: 本格Perlin Noiseを導入しても良い
            noise = np.random.normal(0, std, 3) * 0.5
        elif model == "ou":
            # Ornstein–Uhlenbeck過程の簡易版
            theta = 0.15
            noise = theta * (-np.array(wind_ms)) + np.random.normal(0, std, 3)
        else:
            noise = np.zeros(3)

        return np.array(wind_ms) + noise


    # ---------- gps ----------
    def apply_gps(self, base: float, pos: Pos3) -> float:
        eff = self.effect
        # GPS効果は mode と独立に併用可、という設計にする
        g = base
        if "gps_abs" in eff:
            g = float(eff["gps_abs"])
        if "gps_add" in eff:
            g += float(eff["gps_add"])
        if "gps_scale" in eff:
            g *= float(eff["gps_scale"])
        return max(0.0, min(1.0, g))