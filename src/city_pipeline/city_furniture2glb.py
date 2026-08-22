#!/usr/bin/env python3
"""Convert actual PLATEAU road-marking CityFurniture geometry to GLB.

No lane or marking geometry is inferred.  Only LOD3 polygons carried by
CityFurniture features classified as traffic facilities are exported.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import trimesh
from trimesh.visual.material import PBRMaterial

from citygml2glb import _polygon_rings, triangulate_rings
from geodesy import project_epsg6697_to_local_enu
from road_terrain_probe import read_hfield, terrain_height
from world_frame import load_world_frame

GML = "http://www.opengis.net/gml"
FRN = "http://www.opengis.net/citygml/cityfurniture/2.0"
APP = "http://www.opengis.net/citygml/appearance/2.0"
GML_ID = f"{{{GML}}}id"

TRAFFIC_FACILITY_CLASS = "1000"
ROAD_MARKING_FUNCTIONS = {
    "1000": "road_marking",
    "1010": "lane_line",
    "1020": "center_line",
    "1030": "lane_boundary_line",
    "1040": "road_edge_line",
    "1100": "directive_marking",
    "1110": "crosswalk",
    "1120": "stop_line",
    "1200": "regulatory_marking",
}
DEFAULT_RGBA = (230, 230, 220, 255)


class CityFurnitureError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def extract_material_colors(path: Path) -> dict[str, tuple[int, int, int, int]]:
    """Read polygon colors without retaining the large CityGML tree."""
    colors = {}
    material_tag = f"{{{APP}}}X3DMaterial"
    for _, material in ET.iterparse(path, events=("end",)):
        if material.tag != material_tag:
            continue
        text = material.findtext(f"{{{APP}}}diffuseColor")
        if text:
            values = [min(1.0, max(0.0, float(value))) for value in text.split()]
            if len(values) == 3:
                rgba = tuple(round(value * 255) for value in values) + (255,)
                for target in material.findall(f"{{{APP}}}target"):
                    reference = (target.get("uri") or target.text or "").strip().removeprefix("#")
                    if reference:
                        colors[reference] = rgba
        material.clear()
    return colors


def _in_range(points, latitude, longitude, ns_m, ew_m) -> bool:
    enu = project_epsg6697_to_local_enu(points, latitude, longitude)
    east = [point[0] for point in enu]
    north = [point[1] for point in enu]
    return (
        min(east) <= ew_m and max(east) >= -ew_m
        and min(north) <= ns_m and max(north) >= -ns_m
    )


def _glb_points(
    points,
    latitude,
    longitude,
    altitude_offset_m,
    terrain_samples,
    nrow,
    ncol,
    ns_m,
    ew_m,
    marking_vertical_offset_m,
):
    enu = project_epsg6697_to_local_enu(points, latitude, longitude)
    # Hakoniwa X=North,Y=-East,Z=Up -> GLB X=East,Y=Up,Z=-North.
    output = []
    for east, north, _ in enu:
        altitude = terrain_height(
            north, -east, terrain_samples, nrow, ncol, ns_m, ew_m
        ) + marking_vertical_offset_m
        output.append((east, altitude - altitude_offset_m, -north))
    return output


def convert(
    source: Path,
    world_frame_path: Path,
    terrain_receipt_path: Path,
    output: Path,
    receipt_path: Path | None = None,
    marking_vertical_offset_m: float = 0.055,
) -> dict:
    frame = load_world_frame(world_frame_path)
    origin = frame["origin"]
    extent = frame["half_extent_m"]
    latitude = float(origin["latitude"])
    longitude = float(origin["longitude"])
    altitude_offset = float(origin["altitude_offset_m"])
    ns_m = float(extent["north_south"])
    ew_m = float(extent["east_west"])
    terrain_receipt = json.loads(terrain_receipt_path.read_text(encoding="utf-8"))
    nrow, ncol, terrain_samples = read_hfield(Path(terrain_receipt["hfield"]["path"]))

    colors = extract_material_colors(source)
    batches = defaultdict(lambda: {"vertices": [], "faces": []})
    feature_counts = Counter()
    polygon_count = triangle_count = material_polygon_count = fallback_polygon_count = 0

    furniture_tag = f"{{{FRN}}}CityFurniture"
    for _, furniture in ET.iterparse(source, events=("end",)):
        if furniture.tag != furniture_tag:
            continue
        feature_class = (furniture.findtext(f"{{{FRN}}}class") or "").strip()
        function = (furniture.findtext(f"{{{FRN}}}function") or "").strip()
        category = ROAD_MARKING_FUNCTIONS.get(function)
        if feature_class != TRAFFIC_FACILITY_CLASS or category is None:
            furniture.clear()
            continue

        selected_feature = False
        for polygon in furniture.findall(f".//{{{FRN}}}lod3Geometry//{{{GML}}}Polygon"):
            rings_with_ids = _polygon_rings(polygon)
            if not rings_with_ids:
                continue
            source_rings = [points for _, points in rings_with_ids]
            if not _in_range(source_rings[0], latitude, longitude, ns_m, ew_m):
                continue
            polygon_id = polygon.get(GML_ID, "")
            rgba = colors.get(polygon_id, DEFAULT_RGBA)
            if polygon_id in colors:
                material_polygon_count += 1
            else:
                fallback_polygon_count += 1
            rings = [
                _glb_points(
                    points,
                    latitude,
                    longitude,
                    altitude_offset,
                    terrain_samples,
                    nrow,
                    ncol,
                    ns_m,
                    ew_m,
                    marking_vertical_offset_m,
                )
                for points in source_rings
            ]
            vertices, faces = triangulate_rings(rings)
            batch = batches[(category, rgba)]
            base = len(batch["vertices"])
            batch["vertices"].extend(vertices.tolist())
            batch["faces"].extend((face + base).tolist() for face in faces)
            polygon_count += 1
            triangle_count += len(faces)
            selected_feature = True
        if selected_feature:
            feature_counts[function] += 1
        furniture.clear()

    if not batches:
        raise CityFurnitureError("no actual PLATEAU road-marking geometry intersects the requested range")

    scene = trimesh.Scene()
    for index, ((category, rgba), batch) in enumerate(sorted(batches.items())):
        mesh = trimesh.Trimesh(
            vertices=np.asarray(batch["vertices"], dtype=float),
            faces=np.asarray(batch["faces"], dtype=np.int64),
            process=False,
        )
        # PLATEAU road-marking rings are not guaranteed to use an upward-facing
        # winding.  Paint is a visual surface, so preserve the source geometry
        # and make the material visible from either side instead of rewriting
        # polygon orientation.
        mesh.visual = trimesh.visual.TextureVisuals(material=PBRMaterial(
            name=f"{category}-{index}",
            baseColorFactor=list(rgba),
            metallicFactor=0.0,
            roughnessFactor=1.0,
            doubleSided=True,
        ))
        scene.add_geometry(mesh, node_name=f"{category}-{index}", geom_name=f"{category}-{index}")

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(scene.export(file_type="glb"))
    receipt = {
        "schema_version": 1,
        "component": "actual_plateau_road_markings",
        "source": str(source.resolve()),
        "source_sha256": _sha256(source),
        "world_frame": str(world_frame_path.resolve()),
        "terrain_receipt": str(terrain_receipt_path.resolve()),
        "selection_policy": "LOD3 polygon intersects configured horizontal range",
        "geometry_policy": "source CityFurniture horizontal geometry draped on the shared DEM; no inferred markings",
        "marking_vertical_offset_m": marking_vertical_offset_m,
        "material_policy": "PLATEAU X3DMaterial diffuseColor with documented fallback",
        "rendering_policy": "double-sided road-marking material; source winding preserved",
        "function_labels": ROAD_MARKING_FUNCTIONS,
        "feature_counts": dict(sorted(feature_counts.items())),
        "polygon_count": polygon_count,
        "triangle_count": triangle_count,
        "material_polygon_count": material_polygon_count,
        "fallback_polygon_count": fallback_polygon_count,
        "fallback_rgba": list(DEFAULT_RGBA),
        "output": str(output.resolve()),
        "bytes": output.stat().st_size,
        "sha256": _sha256(output),
    }
    receipt_path = receipt_path or output.with_name(output.stem + "-glb-receipt.json")
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--world-frame", type=Path, required=True)
    parser.add_argument("--terrain-receipt", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()
    receipt = convert(
        args.source, args.world_frame, args.terrain_receipt, args.out, args.receipt
    )
    print(f"OK: actual PLATEAU road-marking GLB: {args.out}")
    print("OK: features: " + ", ".join(
        f"{ROAD_MARKING_FUNCTIONS[key]}={value}" for key, value in receipt["feature_counts"].items()
    ))
    print(f"OK: polygons={receipt['polygon_count']} triangles={receipt['triangle_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
