# fastsearch/test_search.py
import json
import sys
from fastsearch.builder import AABB, build_bvh
from fastsearch.search import search_point, search_primary

def load_areas_from_json(filepath):
    """area.jsonを読み込んでAABBリストに変換"""
    with open(filepath, "r") as f:
        data = json.load(f)
    areas = []
    for a in data.get("space_areas", []):
        b = a["bounds"]
        areas.append(
            AABB(
                b["min"]["x"], b["min"]["y"], b["min"]["z"],
                b["max"]["x"], b["max"]["y"], b["max"]["z"],
                a["area_id"]
            )
        )
    return areas

def main():
    if len(sys.argv) < 5:
        print("Usage: python -m fastsearch.test_search <area.json> <max_depth> <x> <y> <z>")
        return

    filepath = sys.argv[1]
    max_depth = int(sys.argv[2])
    x, y, z = map(float, sys.argv[3:6])

    print(f"📦 読み込み: {filepath}")
    areas = load_areas_from_json(filepath)
    print(f"🧮 AABB数: {len(areas)}")
    areas_map = {a.id: a for a in areas} 

    tree = build_bvh(areas, max_depth=max_depth)

    # for test
    hits = search_point(tree, x, y, z)
    print(f"🎯 検索座標: ({x:.2f}, {y:.2f}, {z:.2f})")
    print(f"🔍 含まれるエリア数: {len(hits)}")
    for h in hits:
        print(f"  - {h}")

    # for real data usecase
    primary_hit = search_primary(tree, x, y, z, areas_map)
    if primary_hit is not None:
        print(f"🎯 主ヒット: {primary_hit}")
        print(f"   AABB: {areas_map[primary_hit]}")
    else:
        print("🎯 主ヒットなし")

if __name__ == "__main__":
    main()
