#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Extract PLATEAU LOD1 buildings into query-centered local ENU JSON.

The input contract is deliberately narrow: each CityGML file must declare the
EPSG:6697 compound CRS with three-dimensional ``latitude longitude height``
coordinates.  ``query_meta.json`` supplies the latitude/longitude origin and
optional north/south and east/west half extents.  Both a single GML file and a
directory containing ``*bldg*_op.gml`` files are accepted.

Example::

    python gml_lod1_extract.py \
      --in ./source \
      --out plateau-city-lod1.json
"""

import argparse
import json
import math
import re
from pathlib import Path
from xml.etree import ElementTree as ET

import numpy as np
from shapely.geometry import MultiPoint

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


def validate_epsg6697_contract(root, gml_path):
    """Validate the authoritative PLATEAU compound CRS declaration."""
    envelopes = root.findall(".//gml:Envelope", NS)
    if not envelopes:
        raise ValueError(f"CityGML gml:Envelope is missing: {gml_path}")
    for envelope in envelopes:
        srs_name = envelope.get("srsName", "")
        match = re.search(r"(?:/|:)(\d+)$", srs_name)
        if match is None or int(match.group(1)) != 6697:
            raise ValueError(
                f"CityGML must declare EPSG:6697; found srsName={srs_name!r}: {gml_path}"
            )
        if envelope.get("srsDimension") != "3":
            raise ValueError(
                "EPSG:6697 CityGML must declare srsDimension=3; "
                f"found {envelope.get('srsDimension')!r}: {gml_path}"
            )


def project_epsg6697_to_local_enu(points, center_lat, center_lon):
    """Geographic coordinates to query-centered local ENU meters.

    PLATEAU EPSG:6697 uses JGD2011 geographic latitude/longitude/height. For a
    small simulation world, its horizontal coordinates can be converted with
    the WGS84/JGD2011-compatible ellipsoid to ECEF and rotated into a local
    tangent plane. The original CityGML altitude is preserved as Z.
    """
    semi_major = 6378137.0
    inv_flattening = 298.257222101  # GRS80, used by JGD2011
    flattening = 1.0 / inv_flattening
    eccentricity_sq = flattening * (2.0 - flattening)

    def ecef(lat_deg, lon_deg):
        lat = math.radians(lat_deg)
        lon = math.radians(lon_deg)
        sin_lat, cos_lat = math.sin(lat), math.cos(lat)
        radius = semi_major / math.sqrt(1.0 - eccentricity_sq * sin_lat * sin_lat)
        return (
            radius * cos_lat * math.cos(lon),
            radius * cos_lat * math.sin(lon),
            radius * (1.0 - eccentricity_sq) * sin_lat,
        )

    origin = ecef(center_lat, center_lon)
    lat0, lon0 = math.radians(center_lat), math.radians(center_lon)
    sin_lat0, cos_lat0 = math.sin(lat0), math.cos(lat0)
    sin_lon0, cos_lon0 = math.sin(lon0), math.cos(lon0)
    output = []
    for lat, lon, z in points:
        point = ecef(lat, lon)
        dx, dy, dz = (point[i] - origin[i] for i in range(3))
        east = -sin_lon0 * dx + cos_lon0 * dy
        north = -sin_lat0 * cos_lon0 * dx - sin_lat0 * sin_lon0 * dy + cos_lat0 * dz
        output.append((east, north, z))
    return output


def convex_hull_xy(points_xy, min_points=3):
    """XY の凸包 (Shapely) → 頂点列（反時計回り）。"""
    if len(points_xy) < min_points:
        return []

    pts2d = [(p[0], p[1]) for p in points_xy]

    hull = MultiPoint(pts2d).convex_hull  # Polygon or LineString or Point
    if hull.geom_type == "Polygon":
        xys = list(hull.exterior.coords)[:-1]  # 閉路の最後を落とす
        return [(float(x), float(y)) for (x, y) in xys]
    if hull.geom_type == "LineString":
        xys = list(hull.coords)
        return [(float(x), float(y)) for (x, y) in xys]
    if hull.geom_type == "Point":
        return [(float(hull.x), float(hull.y))]
    return []


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


def extract_buildings_lod1(gml_path, base_eps=0.2, bounds=None, local_origin=None):
    """
    1つの GML ファイルから bldg:lod1Solid の点群を抽出し、(XY凸包, zmin, zmax) を返す。
    - base_eps: zmin からの許容差（m）。底面近傍点の抽出に使用。
    - local_origin: (latitude, longitude) を指定し、局所ENUの原点とする
    - bounds: {"ns_m": float, "ew_m": float} を指定した場合、範囲外の建物をフィルタ
    """
    tree = ET.parse(gml_path)
    root = tree.getroot()
    validate_epsg6697_contract(root, gml_path)
    if local_origin is None:
        raise ValueError("EPSG:6697 conversion requires a query-centered local origin")

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

        xyz = project_epsg6697_to_local_enu(
            pts_all,
            center_lat=local_origin[0],
            center_lon=local_origin[1],
        )

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

        if not is_within_bounds(footprint, bounds):
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


def merge_unique_footprints(target, footprints, source_gml):
    """Merge buildings by CityGML ID across overlapping municipality files.

    PLATEAU range queries can return the same third-level mesh from adjacent
    municipality datasets.  Identical copies are kept once; a conflicting
    geometry under the same authoritative building ID is rejected instead of
    silently selecting one.
    """
    by_id = {item["id"]: item for item in target}
    duplicate_count = 0
    for poly in footprints:
        candidate = dict(poly)
        candidate["source_gml"] = str(source_gml)
        existing = by_id.get(candidate["id"])
        if existing is None:
            target.append(candidate)
            by_id[candidate["id"]] = candidate
            continue
        geometry_keys = ("vertices", "zmin", "zmax")
        if any(existing.get(key) != candidate.get(key) for key in geometry_keys):
            raise ValueError(
                "conflicting PLATEAU building geometry for duplicate ID "
                f"{candidate['id']}: {existing.get('source_gml')} vs {source_gml}"
            )
        duplicate_count += 1
    return duplicate_count


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in",  dest="in_path",  type=str, required=True,
                    help="入力GMLパス or ディレクトリパス")
    ap.add_argument("--out", dest="out_path", type=str, required=True,
                    help="出力JSONパス")
    ap.add_argument("--base-eps", type=float, default=0.2,
                    help="底面抽出のZ許容[m]")
    ap.add_argument("--pattern", type=str, default="*bldg*_op.gml",
                    help="ディレクトリ指定時に探索するGMLのglobパターン（既定=*bldg*_op.gml）")
    args = ap.parse_args()

    in_path  = Path(args.in_path)
    out_path = Path(args.out_path)

    # query_meta.json の探索
    origin_meta = None
    bounds = None
    
    if in_path.is_dir():
        meta_path = in_path / "query_meta.json"
    else:
        meta_path = in_path.parent / "query_meta.json"
    
    origin_meta = load_query_meta(meta_path)

    if origin_meta is None:
        raise SystemExit("EPSG:6697 conversion requires query_meta.json with center_lat/center_lon")
    
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
        
        print("[INFO] 局所ENU原点: east=0.00, north=0.00")

    # 対象 GML 一覧を集める
    gml_paths = collect_gml_paths(in_path, args.pattern)
    if not gml_paths:
        raise SystemExit(f"No GML found for pattern '{args.pattern}' under {in_path}")

    print(f"[INFO] Target GML count: {len(gml_paths)}")

    all_footprints = []
    duplicate_count = 0
    for gml in gml_paths:
        footprints = extract_buildings_lod1(
            gml_path=gml,
            base_eps=args.base_eps,
            bounds=bounds,
            local_origin=(origin_meta["center_lat"], origin_meta["center_lon"]),
        )
        duplicate_count += merge_unique_footprints(all_footprints, footprints, gml)

    out = {
        "version": "0.1",
        "source_crs": "EPSG:6697",
        "crs": "LOCAL_ENU_GRS80",
        "coordinate_system": "local-enu",
        "deduplicated_buildings": duplicate_count,
        "polygons": all_footprints
    }
    
    out["origin"] = {
        "lat": origin_meta["center_lat"],
        "lon": origin_meta["center_lon"],
        "east": 0.0,
        "north": 0.0,
    }
    if bounds:
        out["bounds"] = bounds
    
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"[OK] buildings: {len(all_footprints)}  → {out_path}")
    if duplicate_count:
        print(f"[INFO] deduplicated identical buildings by CityGML ID: {duplicate_count}")
    print(f"[OK] 局所ENU座標系で出力しました（原点: lat={origin_meta['center_lat']}, lon={origin_meta['center_lon']}）")
    if bounds:
        print(f"[OK] 範囲フィルタ適用: ±{bounds['ns_m']}m (NS), ±{bounds['ew_m']}m (EW)")


if __name__ == "__main__":
    main()
