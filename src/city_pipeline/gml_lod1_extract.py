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
import re
from pathlib import Path
from xml.etree import ElementTree as ET

from shapely.geometry import Polygon
from shapely.geometry.polygon import orient
from shapely.ops import unary_union

from geodesy import project_epsg6697_to_local_enu

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


def _open_ring(points):
    """Drop the duplicate closure and consecutive duplicate XY vertices."""
    output = []
    for point in points:
        xy = (float(point[0]), float(point[1]))
        if not output or xy != output[-1]:
            output.append(xy)
    if len(output) > 1 and output[0] == output[-1]:
        output.pop()
    return output


def _canonical_ring(coords):
    """Return an open ring with a deterministic first vertex."""
    ring = _open_ring(coords)
    if not ring:
        return []
    start = min(range(len(ring)), key=lambda index: ring[index])
    return ring[start:] + ring[:start]


def _canonical_polygon(polygon):
    """Serialize one valid polygon as CCW exterior and CW interior rings."""
    polygon = orient(polygon, sign=1.0)
    return {
        "vertices": _canonical_ring(polygon.exterior.coords),
        "interior_rings": [
            _canonical_ring(interior.coords) for interior in polygon.interiors
        ],
    }


def _base_polygons(bldg, zmin, base_eps, local_origin):
    """Extract ordered horizontal bottom surfaces without convexification."""
    polygons = []
    for element in bldg.findall(".//bldg:lod1Solid//gml:Polygon", NS):
        exterior = element.find("gml:exterior/gml:LinearRing/gml:posList", NS)
        if exterior is None or not exterior.text:
            continue
        exterior_geo = parse_poslist(exterior.text)
        if len(exterior_geo) < 4 or any(abs(point[2] - zmin) > base_eps for point in exterior_geo):
            continue

        exterior_enu = project_epsg6697_to_local_enu(
            exterior_geo, center_lat=local_origin[0], center_lon=local_origin[1]
        )
        holes = []
        for interior in element.findall("gml:interior/gml:LinearRing/gml:posList", NS):
            if not interior.text:
                continue
            interior_geo = parse_poslist(interior.text)
            if any(abs(point[2] - zmin) > base_eps for point in interior_geo):
                raise ValueError("LOD1 bottom polygon has a non-horizontal interior ring")
            interior_enu = project_epsg6697_to_local_enu(
                interior_geo, center_lat=local_origin[0], center_lon=local_origin[1]
            )
            holes.append(_open_ring(interior_enu))

        polygon = Polygon(_open_ring(exterior_enu), holes)
        if polygon.is_empty or polygon.area <= 0.0:
            continue
        if not polygon.is_valid:
            raise ValueError("LOD1 bottom polygon is invalid and cannot be preserved")
        polygons.append(polygon)
    return polygons


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
    
    # 頂点の算術平均ではなく、凹形状と穴も反映した面積重心を使う。
    if hasattr(footprint, "centroid"):
        cx, cy = footprint.centroid.x, footprint.centroid.y
    else:
        polygon = Polygon(footprint)
        cx, cy = polygon.centroid.x, polygon.centroid.y
    
    # 範囲チェック（相対座標なので ±ns_m, ±ew_m）
    return abs(cy) <= ns_m and abs(cx) <= ew_m


def extract_buildings_lod1(gml_path, base_eps=0.2, bounds=None, local_origin=None):
    """
    1つの GML ファイルから bldg:lod1Solid の元の底面外周と穴を抽出する。
    - base_eps: zmin からの許容差（m）。水平底面の判定に使用。
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

        zmin = float(min(p[2] for p in xyz))
        zmax = float(max(p[2] for p in xyz))

        base_polygons = _base_polygons(bldg, zmin, base_eps, local_origin)
        if not base_polygons:
            raise ValueError(f"LOD1 horizontal bottom surface was not found: building={bid}")

        merged = unary_union(base_polygons)
        if merged.geom_type == "Polygon":
            parts = [merged]
        elif merged.geom_type == "MultiPolygon":
            parts = sorted(merged.geoms, key=lambda item: tuple(item.bounds))
        else:
            raise ValueError(f"LOD1 bottom surfaces did not form polygons: building={bid}")

        for index, part in enumerate(parts, start=1):
            serialized = _canonical_polygon(part)
            footprint = serialized["vertices"]
            if not is_within_bounds(part, bounds):
                continue
            part_id = bid if len(parts) == 1 else f"{bid}__part_{index:03d}"
            results.append({
                "id": part_id,
                "vertices": [[float(x), float(y)] for (x, y) in footprint],
                "interior_rings": [
                    [[float(x), float(y)] for (x, y) in ring]
                    for ring in serialized["interior_rings"]
                ],
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
        geometry_keys = ("vertices", "interior_rings", "zmin", "zmax")
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
        "version": "0.2",
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
