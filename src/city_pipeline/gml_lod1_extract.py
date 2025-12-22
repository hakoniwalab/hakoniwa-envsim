#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CityGML (PLATEAU) GML から LOD1 のフットプリントと高さを抽出して JSON 出力。

- 単一 GML ファイルだけでなく、ディレクトリ配下の複数 GML もまとめて処理できるようにした。
  * --in にファイルを渡した場合: その1ファイルだけ処理
  * --in にディレクトリを渡した場合: 再帰的に *_bldg_*_op.gml を全部処理

- query_meta.json がある場合、center_lat/center_lon を原点として相対座標に変換
  * --in がディレクトリの場合: そのディレクトリ直下の query_meta.json を探す
  * --in がファイルの場合: その親ディレクトリの query_meta.json を探す

- 取得対象:
  * bldg:Building ごとの bldg:lod1Solid / gml:Solid / gml:CompositeSurface / gml:Polygon / gml:LinearRing / gml:posList
  * posList は (lat lon z) or (lon lat z) など GML依存だが、PLATEAUの例では (lat lon z) が多い。
    → 本スクリプトでは (lat, lon, z) と仮定し、XY=(lon, lat) にマップする（必要なら --no-swap-latlon で変更可能）。
- Z の最小/最大値から高さを推定（zmin/zmax）。
- 底面近傍 (|z - zmin| <= --base-eps) の点群から 2D 凸包 → フットプリント（XY）を生成。
- 出力JSONは poly_demo.json 互換 + {zmin,zmax,source_gml} を含む。

使い方:
  pip install lxml numpy shapely pyproj

  # 1ファイルだけ処理
  python gml_lod1_extract.py \
    --in 53393567_bldg_6697_op.gml \
    --out poly_from_gml.json \
    --to-epsg 6677

  # ディレクトリ配下の *_bldg_*_op.gml を全部処理（query_meta.jsonで相対座標化）
  python gml_lod1_extract.py \
    --in ./udx/bldg \
    --out shibuya_lod1.json \
    --to-epsg 6677
"""

import argparse
import json
from pathlib import Path
from xml.etree import ElementTree as ET

import numpy as np

# 依存は任意（なくても最低限動く）。投影や凸包には shapely/pyproj が便利。
try:
    from shapely.geometry import MultiPoint
except Exception:
    MultiPoint = None

try:
    from pyproj import Transformer
except Exception:
    Transformer = None


NS = {
    "gml":  "http://www.opengis.net/gml",
    "bldg": "http://www.opengis.net/citygml/building/2.0",
    "core": "http://www.opengis.net/citygml/2.0",
    # CityGML3系や拡張が混じる場合は適宜追加
}


def parse_poslist(text):
    """gml:posList の空白区切り数列 → [(lat,lon,z), ...] へ (3要素ずつ)。"""
    vals = [float(t) for t in text.strip().split()]
    if len(vals) % 3 != 0:
        raise ValueError("posList length is not a multiple of 3")
    pts = []
    for i in range(0, len(vals), 3):
        lat, lon, z = vals[i], vals[i+1], vals[i+2]
        pts.append((lat, lon, z))
    return pts


def latlon_to_xy(points, swap_latlon=True):
    """
    (lat,lon,z) → (x,y,z) に並べ替え。
    - swap_latlon=True のとき: (x,y)=(lon,lat) として扱う。
      （PLATEAUの posList が lat lon z の順で来るケースを想定）
    """
    out = []
    for (lat, lon, z) in points:
        if swap_latlon:
            out.append((lon, lat, z))
        else:
            out.append((lat, lon, z))
    return out


def project_xy(points_xy, src_epsg=4326, dst_epsg=None):
    """
    XY を EPSG 変換（例: 4326→6677）。dst_epsg=None なら変換しない。
    points_xy: [(x,y,z), ...]
    """
    if dst_epsg is None:
        return points_xy
    if Transformer is None:
        raise RuntimeError("pyproj が必要です。pip install pyproj")

    tf = Transformer.from_crs(f"EPSG:{src_epsg}", f"EPSG:{dst_epsg}", always_xy=True)
    out = []
    for (x, y, z) in points_xy:
        X, Y = tf.transform(x, y)
        out.append((X, Y, z))
    return out


def to_relative_coords(points_xy, origin_xy):
    """
    絶対座標を原点からの相対座標に変換。
    points_xy: [(x,y,z), ...]
    origin_xy: (origin_x, origin_y)
    """
    ox, oy = origin_xy
    out = []
    for (x, y, z) in points_xy:
        out.append((x - ox, y - oy, z))
    return out


def convex_hull_xy(points_xy, min_points=3):
    """
    XY の凸包 (shapely) → 頂点列（反時計回り）。
    shapely が無い場合は、簡易 Monotone Chain 実装で代用。
    """
    if len(points_xy) < min_points:
        return []

    pts2d = [(p[0], p[1]) for p in points_xy]

    if MultiPoint is not None:
        hull = MultiPoint(pts2d).convex_hull  # Polygon or LineString or Point
        if hull.geom_type == "Polygon":
            xys = list(hull.exterior.coords)[:-1]  # 閉路の最後を落とす
            return [(float(x), float(y)) for (x, y) in xys]
        elif hull.geom_type == "LineString":
            xys = list(hull.coords)
            return [(float(x), float(y)) for (x, y) in xys]
        elif hull.geom_type == "Point":
            return [(float(hull.x), float(hull.y))]
        else:
            return []
    else:
        # shapely 無し → Monotone Chain
        P = sorted(set(pts2d))
        if len(P) == 1:
            return [P[0]]
        if len(P) == 2:
            return P

        def cross(o, a, b):
            return (a[0]-o[0])*(b[1]-o[1]) - (a[1]-o[1])*(b[0]-o[0])

        lower = []
        for p in P:
            while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
                lower.pop()
            lower.append(p)
        upper = []
        for p in reversed(P):
            while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
                upper.pop()
            upper.append(p)
        hull = lower[:-1] + upper[:-1]
        return hull


def load_query_meta(meta_path):
    """query_meta.json を読み込み、center_lat/center_lon と範囲 (ns_m, ew_m) を返す。"""
    if not meta_path.exists():
        return None
    
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        center_lat = data.get("center_lat")
        center_lon = data.get("center_lon")
        ns_m = data.get("ns_m")
        ew_m = data.get("ew_m")
        
        if center_lat is None or center_lon is None:
            print(f"[WARN] query_meta.json にcenter_lat/center_lonがありません: {meta_path}")
            return None
        
        print(f"[INFO] 原点座標: lat={center_lat}, lon={center_lon}")
        if ns_m is not None and ew_m is not None:
            print(f"[INFO] フィルタ範囲: ±{ns_m}m (NS), ±{ew_m}m (EW)")
        
        return {
            "center_lat": center_lat,
            "center_lon": center_lon,
            "ns_m": ns_m,
            "ew_m": ew_m
        }
    except Exception as e:
        print(f"[WARN] query_meta.json の読み込みに失敗: {e}")
        return None


def is_within_bounds(footprint, bounds):
    """
    建物のフットプリント（相対座標）が指定範囲内にあるかチェック。
    footprint: [(x, y), ...] の頂点リスト（相対座標）
    bounds: {"ns_m": float, "ew_m": float} or None
    
    建物の重心が範囲内にあればTrue。
    """
    if bounds is None or bounds.get("ns_m") is None or bounds.get("ew_m") is None:
        return True  # 範囲指定なしなら全部通す
    
    ns_m = bounds["ns_m"]
    ew_m = bounds["ew_m"]
    
    # フットプリントの重心を計算
    xs = [p[0] for p in footprint]
    ys = [p[1] for p in footprint]
    cx = sum(xs) / len(xs)
    cy = sum(ys) / len(ys)
    
    # 範囲チェック（相対座標なので ±ns_m, ±ew_m）
    return abs(cy) <= ns_m and abs(cx) <= ew_m


def extract_buildings_lod1(gml_path, to_epsg=None, src_epsg=4326,
                           base_eps=0.2, swap_latlon=True, origin_xy=None, bounds=None):
    """
    1つの GML ファイルから bldg:lod1Solid の点群を抽出し、(XY凸包, zmin, zmax) を返す。
    - base_eps: zmin からの許容差（m）。底面近傍点の抽出に使用。
    - origin_xy: (origin_x, origin_y) を指定した場合、相対座標に変換
    - bounds: {"ns_m": float, "ew_m": float} を指定した場合、範囲外の建物をフィルタ
    """
    tree = ET.parse(gml_path)
    root = tree.getroot()

    print(f"[INFO] Processing GML: {gml_path}")
    results = []
    # CityGMLでは bldg:Building 要素が親。複数棟あればループで拾う
    for bldg in root.findall(".//bldg:Building", NS):
        bid = (bldg.get("{http://www.opengis.net/gml}id")
               or bldg.findtext("gml:name", default="bldg", namespaces=NS)
               or f"bldg_{len(results)+1}")

        # LOD1 Solid の posList 群をすべて収集
        pos_texts = bldg.findall(".//bldg:lod1Solid//gml:LinearRing/gml:posList", NS)
        if not pos_texts:
            # LOD1 が無い建物はスキップ
            continue

        pts_all = []
        for pos in pos_texts:
            pts = parse_poslist(pos.text)
            pts_all.extend(pts)

        if not pts_all:
            continue

        # (lat,lon,z) -> (x,y,z) へ（デフォは (lon,lat) に並べ替え）
        xyz = latlon_to_xy(pts_all, swap_latlon=swap_latlon)

        # 必要なら EPSG 変換
        xyz = project_xy(xyz, src_epsg=src_epsg, dst_epsg=to_epsg)

        # 原点からの相対座標に変換
        if origin_xy is not None:
            xyz = to_relative_coords(xyz, origin_xy)

        # 高さレンジ
        zs = np.array([p[2] for p in xyz], dtype=float)
        zmin = float(np.min(zs))
        zmax = float(np.max(zs))

        # 底面近傍 (|z - zmin| <= base_eps) の XY を抽出
        base_xy = [(x, y, z) for (x, y, z) in xyz if abs(z - zmin) <= base_eps]
        if len(base_xy) < 3:
            # 壁面しか拾えなかった等 → 全点から凸包（苦肉の策）
            base_xy = xyz

        # 凸包でフットプリント（安定重視の簡便法）
        footprint = convex_hull_xy(base_xy)
        if len(footprint) < 3:
            # 退避：点や線しか得られない場合はスキップ
            continue

        # 範囲チェック（相対座標の場合のみ）
        if origin_xy is not None and not is_within_bounds(footprint, bounds):
            continue  # 範囲外なのでスキップ

        results.append({
            "id": bid,
            "vertices": [[float(x), float(y)] for (x, y) in footprint],
            "zmin": zmin,
            "zmax": zmax,
        })

    return results


def collect_gml_paths(in_path: Path, pattern: str):
    """
    in_path がファイルなら [in_path] を返す。
    ディレクトリなら再帰的に pattern (glob) にマッチする GML を列挙。
    """
    if in_path.is_file():
        return [in_path]
    paths = sorted(in_path.rglob(pattern))
    return paths


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in",  dest="in_path",  type=str, required=True,
                    help="入力GMLパス or ディレクトリパス")
    ap.add_argument("--out", dest="out_path", type=str, required=True,
                    help="出力JSONパス")
    ap.add_argument("--to-epsg", type=int, default=None,
                    help="出力XYのEPSG（例: 6677）。未指定なら投影しない（度のまま）")
    ap.add_argument("--src-epsg", type=int, default=4326,
                    help="入力の経緯度 EPSG（既定=4326）")
    ap.add_argument("--base-eps", type=float, default=0.2,
                    help="底面抽出のZ許容[m]")
    ap.add_argument("--no-swap-latlon", action="store_true",
                    help="posListが(lon,lat,z)順のときに使う。既定は(lat,lon,z)想定でswap=True。")
    ap.add_argument("--pattern", type=str, default="*bldg*_op.gml",
                    help="ディレクトリ指定時に探索するGMLのglobパターン（既定=*bldg*_op.gml）")
    args = ap.parse_args()

    in_path  = Path(args.in_path)
    out_path = Path(args.out_path)

    # query_meta.json の探索
    origin_meta = None
    origin_xy = None
    bounds = None
    
    if in_path.is_dir():
        meta_path = in_path / "query_meta.json"
    else:
        meta_path = in_path.parent / "query_meta.json"
    
    origin_meta = load_query_meta(meta_path)
    
    # 原点座標を投影座標系に変換
    if origin_meta is not None:
        center_lat = origin_meta["center_lat"]
        center_lon = origin_meta["center_lon"]
        
        # 範囲情報を保持
        if origin_meta.get("ns_m") is not None and origin_meta.get("ew_m") is not None:
            bounds = {
                "ns_m": origin_meta["ns_m"],
                "ew_m": origin_meta["ew_m"]
            }
        
        # swap_latlon に従って (lon, lat) または (lat, lon) に
        if not args.no_swap_latlon:
            origin_points = [(center_lon, center_lat, 0)]
        else:
            origin_points = [(center_lat, center_lon, 0)]
        
        # EPSG変換
        origin_points = project_xy(origin_points, src_epsg=args.src_epsg, dst_epsg=args.to_epsg)
        origin_xy = (origin_points[0][0], origin_points[0][1])
        print(f"[INFO] 投影後の原点: x={origin_xy[0]:.2f}, y={origin_xy[1]:.2f}")

    # 対象 GML 一覧を集める
    gml_paths = collect_gml_paths(in_path, args.pattern)
    if not gml_paths:
        raise SystemExit(f"No GML found for pattern '{args.pattern}' under {in_path}")

    print(f"[INFO] Target GML count: {len(gml_paths)}")

    all_footprints = []
    filtered_count = 0
    for gml in gml_paths:
        footprints = extract_buildings_lod1(
            gml_path=gml,
            to_epsg=args.to_epsg,
            src_epsg=args.src_epsg,
            base_eps=args.base_eps,
            swap_latlon=(not args.no_swap_latlon),
            origin_xy=origin_xy,
            bounds=bounds,
        )
        # どのGMLから来たか分かるようにする
        for poly in footprints:
            poly_with_src = dict(poly)
            poly_with_src["source_gml"] = str(gml)
            all_footprints.append(poly_with_src)

    out = {
        "version": "0.1",
        "crs": f"EPSG:{args.to_epsg}" if args.to_epsg else f"EPSG:{args.src_epsg}",
        "coordinate_system": "relative" if origin_xy else "absolute",
        "polygons": all_footprints
    }
    
    if origin_xy:
        out["origin"] = {
            "lat": origin_meta["center_lat"],
            "lon": origin_meta["center_lon"],
            "x": origin_xy[0],
            "y": origin_xy[1]
        }
        if bounds:
            out["bounds"] = bounds
    
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"[OK] buildings: {len(all_footprints)}  → {out_path}")
    if origin_xy:
        print(f"[OK] 相対座標系で出力しました（原点: lat={origin_meta['center_lat']}, lon={origin_meta['center_lon']}）")
        if bounds:
            print(f"[OK] 範囲フィルタ適用: ±{bounds['ns_m']}m (NS), ±{bounds['ew_m']}m (EW)")


if __name__ == "__main__":
    main()