#!/usr/bin/env python3
import argparse
import json
import math
import os
import shutil
from pathlib import Path

# ----------------------------
# 緯度経度 + 距離(m) = bbox 計算
# ----------------------------

def make_query_bbox(lat, lon, ns, ew):
    """中心lat/lonから ±ns, ±ew メートルの bbox を返す"""
    # 地球半径
    R = 6378137.0
    dlat = (ns / R) * (180.0 / math.pi)
    dlon = (ew / (R * math.cos(math.radians(lat)))) * (180.0 / math.pi)
    return {
        "min_lat": lat - dlat,
        "max_lat": lat + dlat,
        "min_lon": lon - dlon,
        "max_lon": lon + dlon
    }


# ---------------------------------
# メッシュコード → bbox 計算
# ---------------------------------
# PLATEAU の GML は 2次/3次メッシュ（6桁/8桁）が中心
# ここでは 6桁 → 座標、8桁 → 6桁の subdiv として扱う

def mesh_to_bbox(meshcode):
    """6桁 or 8桁メッシュコードから緯度経度BBoxを返す"""
    m = str(meshcode)
    if len(m) not in (6, 8):
        raise ValueError(f"Unsupported meshcode length: {meshcode}")

    # 1次メッシュ
    lat1 = int(m[0:2]) * 2/3          # 0.6666度刻み
    lon1 = int(m[2:4]) + 100

    # 2次メッシュ（→ 6桁）
    lat2 = lat1 + (int(m[4:5]) * 1/12)
    lon2 = lon1 + (int(m[5:6]) * 1/8)

    # mesh size
    lat_size = 1/12     # 約 5.5km
    lon_size = 1/8      # 約 7.5km

    # 3次メッシュ（8桁のとき）
    if len(m) == 8:
        lat2 += (int(m[6:7]) * lat_size / 10)
        lon2 += (int(m[7:8]) * lon_size / 10)
        lat_size /= 10
        lon_size /= 10

    return {
        "min_lat": lat2,
        "max_lat": lat2 + lat_size,
        "min_lon": lon2,
        "max_lon": lon2 + lon_size
    }


# ----------------------------
# bbox intersect
# ----------------------------

def is_intersect(a, b):
    return not (a["max_lat"] < b["min_lat"] or
                a["min_lat"] > b["max_lat"] or
                a["max_lon"] < b["min_lon"] or
                a["min_lon"] > b["max_lon"])


# ----------------------------
# メイン処理
# ----------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--src-root", required=True)
    parser.add_argument("--index", required=True)
    parser.add_argument("--lat", type=float, required=True)
    parser.add_argument("--lon", type=float, required=True)
    parser.add_argument("--ns", type=float, required=True, help="north/south half-size in meters")
    parser.add_argument("--ew", type=float, required=True, help="east/west half-size in meters")
    parser.add_argument("--out-root", required=True)
    args = parser.parse_args()

    src_root = Path(args.src_root)
    out_root = Path(args.out_root)

    print("### Loading index...")
    with open(args.index, "r") as f:
        index = json.load(f)

    # クエリ bbox
    qbbox = make_query_bbox(args.lat, args.lon, args.ns, args.ew)
    print("Query BBOX:", qbbox)

    # 出力先を作成
    if out_root.exists():
        shutil.rmtree(out_root)
    out_root.mkdir(parents=True)

    # 上位フォルダは丸コピー
    copy_dirs = ["codelists", "metadata", "schemas", "specification"]
    copy_files = ["README.md", "13113_indexmap_op.pdf"]

    for d in copy_dirs:
        src_d = src_root / d
        if src_d.exists():
            shutil.copytree(src_d, out_root / d)

    for f in copy_files:
        src_f = src_root / f
        if src_f.exists():
            shutil.copy2(src_f, out_root / f)

    # udx 以下のフィルタコピー
    print("### Filtering udx ...")
    for item in index:
        mesh = item["mesh"]
        kind = item["kind"]
        src_file = Path(item["path"])

        # bbox intersect?
        mbbox = mesh_to_bbox(mesh)
        if not is_intersect(qbbox, mbbox):
            continue

        # 元パスから src_root との相対パスを求める
        rel = src_file.relative_to(src_root)
        dst_file = out_root / rel
        dst_file.parent.mkdir(parents=True, exist_ok=True)

        # .gml 本体をコピー
        shutil.copy2(src_file, dst_file)

        # appearance など同じメッシュに関連するものを同ディレクトリからコピー
        mesh_prefix = f"{mesh}_{kind}"
        src_dir = src_file.parent
        for f in src_dir.iterdir():
            if not f.name.startswith(mesh_prefix):
                continue
            if f == src_file:
                continue  # さっきコピーした本体

            rel2 = f.relative_to(src_root)
            dst2 = out_root / rel2
            if f.is_dir():
                if dst2.exists():
                    shutil.rmtree(dst2)
                shutil.copytree(f, dst2)
            else:
                dst2.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dst2)

    # クエリ情報を保存
    meta = {
        "center_lat": args.lat,
        "center_lon": args.lon,
        "ns_m": args.ns,
        "ew_m": args.ew,
        "bbox": qbbox
    }
    with open(out_root / "query_meta.json", "w") as f:
        json.dump(meta, f, indent=2)

    print("### Completed!")
    print("Output:", str(out_root))


if __name__ == "__main__":
    main()
