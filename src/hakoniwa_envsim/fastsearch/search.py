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
        if precise and node.areas:
            for a in node.areas:
                stats["visited"] += 1
                # print(f"Visiting leaf AABB id={a.id} ...")  # debug
                if point_in_aabb(a, x, y, z):
                    found.append(a.id)
                    # 1つで十分なら break / 複数欲しければ続行
                    break
        else:
            found.extend(node.ids or [])
        return found

    left_hit  = node.left  and point_in_aabb(node.left.aabb,  x, y, z)
    right_hit = node.right and point_in_aabb(node.right.aabb, x, y, z)

    if left_hit and not right_hit:
        search_point(node.left,  x, y, z, found, stats, precise)
    elif right_hit and not left_hit:
        search_point(node.right, x, y, z, found, stats, precise)
    elif left_hit and right_hit:
        # ★ 境界帯や子AABB重なり時は両方探索しないと取りこぼす
        search_point(node.left,  x, y, z, found, stats, precise)
        search_point(node.right, x, y, z, found, stats, precise)

    return found
