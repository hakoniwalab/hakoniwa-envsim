# fastsearch/search.py
def point_in_aabb(aabb, x, y, z):
    return (
        aabb.minx <= x < aabb.maxx and
        aabb.miny <= y < aabb.maxy and
        aabb.minz <= z < aabb.maxz
    )


def search_point(node, x, y, z, found=None, stats=None, precise=True):
    if found is None:
        found = []
    if stats is None:
        stats = {"visited": 0}

    stats["visited"] += 1
    if not point_in_aabb(node.aabb, x, y, z):
        return found

    if node.is_leaf:
        if precise:
            # ★ 実AABB群で厳密判定（必要最小限のループ）
            for a in (node.areas or []):
                stats["visited"] += 1
                if point_in_aabb(a, x, y, z):
                    found.append(a.id)
                    return found  
        else:
            # 粗モード：リーフ全IDを一括追加
            found.extend(node.ids or [])
        return found

    # 左右のどちらに入るか（境界時は両方）
    left_hit  = node.left  and point_in_aabb(node.left.aabb,  x, y, z)
    right_hit = node.right and point_in_aabb(node.right.aabb, x, y, z)
    if left_hit:
        search_point(node.left,  x, y, z, found, stats, precise)
    if right_hit and (not left_hit or left_hit):  # leftとright両trueなら両方
        search_point(node.right, x, y, z, found, stats, precise)
    return found

