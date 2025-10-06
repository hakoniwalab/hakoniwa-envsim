# envsearch.py
import sys
from envbuilder import Environment

def main():
    if len(sys.argv) < 7:
        print("Usage: python -m envsearch <area.json> <link.json> <property.json> <max_depth> <x> <y> <z>")
        return

    area_json = sys.argv[1]
    link_json = sys.argv[2]
    prop_json = sys.argv[3]
    max_depth = int(sys.argv[4])
    x, y, z = map(float, sys.argv[5:8])

    env = Environment.from_files(
        area_json, link_json, prop_json,
        max_depth=max_depth,
        leaf_capacity=1  # 必要なら 2〜4 で安定化
    )

    area_id, props = env.get_property_at(x, y, z)

    print(f"📦 area.json: {area_json}")
    print(f"🔗 link.json: {link_json}")
    print(f"🧪 property.json: {prop_json}")
    print(f"🎯 point: ({x:.3f}, {y:.3f}, {z:.3f})  max_depth={max_depth}")

    if area_id is None:
        print("❌ Hit area: None")
        return

    print(f"✅ Hit area: {area_id}")
    if props is None:
        print("⚠️ No property linked.")
        return

    print("🧾 area_property:")
    for k, v in props.items():
        print(f"  - {k}: {v}")

    # for debug
    print("\n--- Environment Debug Info ---")
    debug_info = env.explain_at(x, y, z)
    print(debug_info)


if __name__ == "__main__":
    main()
