import sys
import json
from fastsearch.builder import AABB, build_bvh
from fastsearch.estimator import estimate_cost
from fastsearch.analysis import analyze_tree
def load_areas_from_json(filepath):
    """area.jsonを読み込んでAABBリストを生成"""
    import json
    from fastsearch.builder import AABB

    with open(filepath, 'r') as f:
        data = json.load(f)

    # --- 柔軟に対応 ---
    if "space_areas" in data:
        items = data["space_areas"]
    elif "areas" in data:
        items = data["areas"]
    elif isinstance(data, list):
        items = data
    else:
        print("⚠️ 未対応のJSON形式です")
        return []

    areas = []
    for i, a in enumerate(items):
        b = a.get("bounds") or a.get("aabb") or a
        if not b:
            continue

        minv = b.get("min")
        maxv = b.get("max")
        if isinstance(minv, dict) and isinstance(maxv, dict):
            x0, y0, z0 = minv["x"], minv["y"], minv["z"]
            x1, y1, z1 = maxv["x"], maxv["y"], maxv["z"]
        elif isinstance(minv, list) and isinstance(maxv, list):
            x0, y0, z0 = minv
            x1, y1, z1 = maxv
        else:
            continue

        areas.append(AABB(x0, y0, z0, x1, y1, z1, i))

    return areas



def main():
    if len(sys.argv) < 3:
        print("Usage: python -m fastsearch.test <area.json> <max_depth>")
        sys.exit(1)

    filepath = sys.argv[1]
    areas = load_areas_from_json(filepath)

    # パラメータ（適宜調整）
    max_depth = int(sys.argv[2])

    print(f"📦 読み込み: {filepath}")
    print(f"🧮 AABB数: {len(areas)}")

    tree = build_bvh(areas, max_depth=max_depth)
    est = estimate_cost(num_areas=len(areas), max_depth=max_depth)
    stats = analyze_tree(tree)

    print("\n📘 理論推定:", est)
    print("🌳 実際のツリー解析:", stats)


if __name__ == "__main__":
    main()
