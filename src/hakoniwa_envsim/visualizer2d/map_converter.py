# map_converter.py
from copy import deepcopy
from hakoniwa_envsim.model.models import VisualArea, AABB2D

def _normalize_aabb_map(aabb):
    # aabb: MAP(E,N) 前提
    xmin, xmax = (aabb.xmin, aabb.xmax) if aabb.xmin <= aabb.xmax else (aabb.xmax, aabb.xmin)
    ymin, ymax = (aabb.ymin, aabb.ymax) if aabb.ymin <= aabb.ymax else (aabb.ymax, aabb.ymin)
    return xmin, xmax, ymin, ymax

def to_map_frame_scene(scenes: list[VisualArea]) -> list[VisualArea]:
    """
    ROS座標系 (X=北+, Y=西+) → MAP座標系 (X=東+, Y=北+) に変換
    """
    converted = []
    for a in scenes:
        na = deepcopy(a)
        aabb = a.aabb2d

        # === 新しいAABBを生成（不変構造体対応） ===
        new_aabb = AABB2D(
            xmin = -aabb.ymax,  # 西→東反転
            xmax = -aabb.ymin,
            ymin =  aabb.xmin,  # 北そのまま
            ymax =  aabb.xmax
        )
        na = na.__class__(  # VisualAreaの再生成
            area_id=na.area_id,
            aabb2d=new_aabb,
            temperature=na.temperature,
            gps_strength=na.gps_strength,
            wind_velocity=(
                (-na.wind_velocity[1], na.wind_velocity[0], na.wind_velocity[2])
                if na.wind_velocity else None
            ),
            # 他のフィールドがあれば適宜コピー
        )
        converted.append(na)

    return converted
