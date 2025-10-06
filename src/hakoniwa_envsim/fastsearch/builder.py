# fastsearch/builder.py
from dataclasses import dataclass
from typing import List, Optional
import numpy as np

@dataclass
class AABB:
    minx: float; miny: float; minz: float
    maxx: float; maxy: float; maxz: float
    id: int

    def center(self):
        return np.array([(self.minx + self.maxx) / 2,
                         (self.miny + self.maxy) / 2,
                         (self.minz + self.maxz) / 2])

@dataclass
class Node:
    aabb: AABB
    left: Optional['Node'] = None
    right: Optional['Node'] = None
    is_leaf: bool = False
    ids: Optional[List[str]] = None
    areas: Optional[List[AABB]] = None  # ★ 追加：このリーフが抱える実AABBたち

def merge_aabb(a: AABB, b: AABB, new_id: int):
    return AABB(
        min(a.minx, b.minx), min(a.miny, b.miny), min(a.minz, b.minz),
        max(a.maxx, b.maxx), max(a.maxy, b.maxy), max(a.maxz, b.maxz),
        new_id
    )

# fastsearch/builder.py
def build_bvh(areas: List[AABB], depth=0, max_depth=5, leaf_capacity: int = 1) -> Node:
    if len(areas) == 0:
        raise ValueError("areas is empty")

    # ★ 終端条件：個数が閾値以下 or 深さ上限
    if len(areas) <= leaf_capacity or depth >= max_depth:
        minx = min(a.minx for a in areas)
        miny = min(a.miny for a in areas)
        minz = min(a.minz for a in areas)
        maxx = max(a.maxx for a in areas)
        maxy = max(a.maxy for a in areas)
        maxz = max(a.maxz for a in areas)
        merged_leaf = AABB(minx, miny, minz, maxx, maxy, maxz, new_id := -1)
        return Node(
            aabb=merged_leaf,
            is_leaf=True,
            ids=[a.id for a in areas],
            areas=areas[:]  # ★ 実AABBをそのまま保持
        )

    centers = np.array([[(a.minx+a.maxx)/2, (a.miny+a.maxy)/2, (a.minz+a.maxz)/2] for a in areas])
    spreads = centers.max(axis=0) - centers.min(axis=0)
    axis = int(np.argmax(spreads))

    # 空間順（min座標）で分割
    key_names = ["minx", "miny", "minz"]
    areas.sort(key=lambda a: getattr(a, key_names[axis]))
    mid = len(areas) // 2

    left  = build_bvh(areas[:mid],  depth+1, max_depth, leaf_capacity)
    right = build_bvh(areas[mid:], depth+1, max_depth, leaf_capacity)

    merged = AABB(
        min(left.aabb.minx, right.aabb.minx),
        min(left.aabb.miny, right.aabb.miny),
        min(left.aabb.minz, right.aabb.minz),
        max(left.aabb.maxx, right.aabb.maxx),
        max(left.aabb.maxy, right.aabb.maxy),
        max(left.aabb.maxz, right.aabb.maxz),
        -1
    )
    return Node(aabb=merged, left=left, right=right)
