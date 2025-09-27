from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Tuple, Dict, List

Vec3 = Tuple[float, float, float]


# --- 幾何 -------------------------------------------------------------

@dataclass(frozen=True)
class Point3D:
    x: float
    y: float
    z: float


@dataclass(frozen=True)
class AABB3D:
    """Axis-Aligned Bounding Box (3D)"""
    min: Point3D
    max: Point3D

    def width(self) -> float:
        return self.max.x - self.min.x

    def height(self) -> float:
        return self.max.y - self.min.y

    def depth(self) -> float:
        return self.max.z - self.min.z

    def to_2d(self) -> "AABB2D":
        return AABB2D(xmin=self.min.x, ymin=self.min.y, xmax=self.max.x, ymax=self.max.y)


@dataclass(frozen=True)
class AABB2D:
    """2D 用の矩形（可視化で利用）"""
    xmin: float
    ymin: float
    xmax: float
    ymax: float

    def center(self) -> Tuple[float, float]:
        return ((self.xmin + self.xmax) * 0.5, (self.ymin + self.ymax) * 0.5)


# --- 実行モデル（分割ファイルに対応）-----------------------------------

@dataclass(frozen=True)
class SpaceArea:
    area_id: str
    bounds: AABB3D


@dataclass(frozen=True)
class AreaProperty:
    """エリア非依存の基準プロパティ（Link で結び付け）"""
    id: str
    wind_velocity: Vec3
    temperature: float
    sea_level_atm: float


@dataclass(frozen=True)
class Link:
    area_id: str
    area_property_id: str


# --- 可視化/表示のための統合ビュー -----------------------------------

@dataclass
class VisualArea:
    area_id: str
    aabb2d: AABB2D
    temperature: Optional[float] = None
    sea_level_atm: Optional[float] = None
    wind_velocity: Optional[Vec3] = None  # (x, y, z)


__all__ = [
    "Point3D",
    "AABB3D",
    "AABB2D",
    "SpaceArea",
    "AreaProperty",
    "Link",
    "VisualArea",
    "Vec3",
]
