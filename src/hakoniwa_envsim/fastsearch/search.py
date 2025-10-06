# fastsearch/search.py
def point_in_aabb(aabb, x, y, z):
    return (
        aabb.minx <= x <= aabb.maxx and
        aabb.miny <= y <= aabb.maxy and
        aabb.minz <= z <= aabb.maxz
    )

def search_point(node, x, y, z, found=None):
    """BVHツリー内で点を探索"""
    if found is None:
        found = []

    # 現ノードのAABBがこの点を含まない場合は探索終了
    if not point_in_aabb(node.aabb, x, y, z):
        return found

    if node.is_leaf:
        found.extend(node.ids)
        return found

    # 子ノードを再帰探索
    if node.left:
        search_point(node.left, x, y, z, found)
    if node.right:
        search_point(node.right, x, y, z, found)

    return found

def search_primary(node, x, y, z, areas, mode="nearest"):
    """複数ヒット時に所属を決定"""
    hits = search_point(node, x, y, z)
    if not hits:
        return None

    if len(hits) == 1:
        return hits[0]

    if mode == "nearest":
        # 最も近いAABB中心を採用
        dists = []
        for h in hits:
            cx, cy, cz = areas[h].center()
            dist = ((x-cx)**2 + (y-cy)**2 + (z-cz)**2)**0.5
            dists.append((dist, h))
        dists.sort()
        return dists[0][1]
    else:
        # priorityベース（未設定なら最初）
        return hits[0]
