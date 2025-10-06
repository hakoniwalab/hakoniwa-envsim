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
    ids: Optional[List[int]] = None

def merge_aabb(a: AABB, b: AABB, new_id: int):
    return AABB(
        min(a.minx, b.minx), min(a.miny, b.miny), min(a.minz, b.minz),
        max(a.maxx, b.maxx), max(a.maxy, b.maxy), max(a.maxz, b.maxz),
        new_id
    )

def build_bvh(areas: List[AABB], depth=0, max_depth=5):
    if len(areas) <= 1 or depth >= max_depth:
        return Node(aabb=areas[0], is_leaf=True, ids=[a.id for a in areas])

    # 最も広がりのある軸を選ぶ
    centers = np.array([a.center() for a in areas])
    spreads = centers.max(axis=0) - centers.min(axis=0)
    axis = np.argmax(spreads)

    # ソートして二分割
    areas.sort(key=lambda a: a.center()[axis])
    mid = len(areas) // 2
    left = build_bvh(areas[:mid], depth+1, max_depth)
    right = build_bvh(areas[mid:], depth+1, max_depth)

    # new_id = -1 means virtual branch node
    merged = merge_aabb(left.aabb, right.aabb, new_id=-1)
    return Node(aabb=merged, left=left, right=right)
