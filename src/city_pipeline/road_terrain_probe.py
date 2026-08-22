#!/usr/bin/env python3
"""Drape PLATEAU LOD1 roads on a generated DEM hfield for visual validation."""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import trimesh
from shapely.geometry import Polygon, box
from shapely.ops import triangulate

from geodesy import project_epsg6697_to_local_enu
from world_frame import load_world_frame

GML = "http://www.opengis.net/gml"
TRAN = "http://www.opengis.net/citygml/transportation/2.0"
GML_ID = f"{{{GML}}}id"

SURFACE_STYLE = {
    "roadway": [52, 55, 59, 255],
    "lane": [48, 51, 55, 255],
    "intersection": [62, 65, 69, 255],
    "sidewalk": [174, 171, 162, 255],
    "island": [124, 137, 108, 255],
}


class RoadProbeError(RuntimeError):
    pass


def read_hfield(path: Path):
    data = path.read_bytes()
    if len(data) < 8:
        raise RoadProbeError(f"invalid hfield: {path}")
    nrow, ncol = struct.unpack("<ii", data[:8])
    expected = 8 + 4 * nrow * ncol
    if len(data) != expected:
        raise RoadProbeError(f"invalid hfield byte length: {len(data)} != {expected}")
    return nrow, ncol, list(struct.unpack(f"<{nrow*ncol}f", data[8:]))


def terrain_height(x, y, samples, nrow, ncol, ns_m, ew_m):
    col = (x + ns_m) * (ncol - 1) / (2.0 * ns_m)
    row = (y + ew_m) * (nrow - 1) / (2.0 * ew_m)
    col = min(max(col, 0.0), ncol - 1.0)
    row = min(max(row, 0.0), nrow - 1.0)
    c0, r0 = int(col), int(row)
    c1, r1 = min(c0 + 1, ncol - 1), min(r0 + 1, nrow - 1)
    dc, dr = col - c0, row - r0
    at = lambda r, c: samples[r * ncol + c]
    return (
        at(r0, c0) * (1 - dc) * (1 - dr)
        + at(r0, c1) * dc * (1 - dr)
        + at(r1, c0) * (1 - dc) * dr
        + at(r1, c1) * dc * dr
    )


def extract_lod1_roads(path: Path, latitude: float, longitude: float, ns_m: float, ew_m: float):
    clip = box(-ns_m, -ew_m, ns_m, ew_m)
    roads = []
    for _, element in ET.iterparse(path, events=("end",)):
        if element.tag != f"{{{TRAN}}}Road":
            continue
        road_id = element.get(GML_ID, f"road-{len(roads)}")
        for index, polygon_element in enumerate(
            element.findall(f".//{{{TRAN}}}lod1MultiSurface//{{{GML}}}Polygon")
        ):
            exterior = polygon_element.find(
                f"./{{{GML}}}exterior/{{{GML}}}LinearRing/{{{GML}}}posList"
            )
            if exterior is None or not exterior.text:
                continue
            values = [float(value) for value in exterior.text.split()]
            points = [values[offset:offset + 3] for offset in range(0, len(values), 3)]
            enu = project_epsg6697_to_local_enu(points, latitude, longitude)
            polygon = Polygon([(north, -east) for east, north, _ in enu])
            if not polygon.is_valid:
                polygon = polygon.buffer(0)
            clipped = polygon.intersection(clip)
            if clipped.is_empty:
                continue
            geometries = [clipped] if clipped.geom_type == "Polygon" else list(clipped.geoms)
            for part, geometry in enumerate(geometries):
                if geometry.area > 1e-6:
                    roads.append((f"{road_id}-{index}-{part}", geometry))
        element.clear()
    if not roads:
        raise RoadProbeError("no LOD1 road polygon intersects the requested range")
    return roads


def _surface_polygon(polygon_element, latitude, longitude):
    exterior = polygon_element.find(
        f"./{{{GML}}}exterior/{{{GML}}}LinearRing/{{{GML}}}posList"
    )
    if exterior is None or not exterior.text:
        return None

    def ring(pos_list):
        values = [float(value) for value in pos_list.text.split()]
        points = [values[offset:offset + 3] for offset in range(0, len(values), 3)]
        enu = project_epsg6697_to_local_enu(points, latitude, longitude)
        return [(north, -east) for east, north, _ in enu]

    holes = []
    for interior in polygon_element.findall(
        f"./{{{GML}}}interior/{{{GML}}}LinearRing/{{{GML}}}posList"
    ):
        if interior.text:
            holes.append(ring(interior))
    return Polygon(ring(exterior), holes)


def extract_transport_surfaces(
    path: Path, latitude: float, longitude: float, ns_m: float, ew_m: float,
    lod_evidence: dict[str, int] | None = None,
):
    """Extract semantic LOD2 road surfaces and clip them to the requested area."""
    clip = box(-ns_m, -ew_m, ns_m, ew_m)
    surfaces = {name: [] for name in SURFACE_STYLE}
    classification = {
        ("TrafficArea", "1000"): "roadway",
        ("TrafficArea", "1010"): "lane",
        ("TrafficArea", "1020"): "intersection",
        ("TrafficArea", "1030"): "roadway",
        ("TrafficArea", "2000"): "sidewalk",
        ("AuxiliaryTrafficArea", "3000"): "island",
    }
    for _, road in ET.iterparse(path, events=("end",)):
        if road.tag != f"{{{TRAN}}}Road":
            continue
        for feature_name in ("TrafficArea", "AuxiliaryTrafficArea"):
            for feature in road.findall(f".//{{{TRAN}}}{feature_name}"):
                function = (feature.findtext(f"{{{TRAN}}}function") or "").strip()
                category = classification.get((feature_name, function))
                if category is None:
                    continue
                feature_id = feature.get(GML_ID, f"{category}-{len(surfaces[category])}")
                polygons = feature.findall(
                    f".//{{{TRAN}}}lod3MultiSurface//{{{GML}}}Polygon"
                )
                selected_lod = "lod3"
                if not polygons:
                    polygons = feature.findall(
                        f".//{{{TRAN}}}lod2MultiSurface//{{{GML}}}Polygon"
                    )
                    selected_lod = "lod2_fallback"
                for index, polygon_element in enumerate(polygons):
                    polygon = _surface_polygon(polygon_element, latitude, longitude)
                    if polygon is None:
                        continue
                    if not polygon.is_valid:
                        polygon = polygon.buffer(0)
                    clipped = polygon.intersection(clip)
                    if clipped.is_empty:
                        continue
                    geometries = [clipped] if clipped.geom_type == "Polygon" else list(clipped.geoms)
                    for part, geometry in enumerate(geometries):
                        if geometry.geom_type == "Polygon" and geometry.area > 1e-6:
                            surfaces[category].append((f"{feature_id}-{index}-{part}", geometry))
                            if lod_evidence is not None:
                                lod_evidence[selected_lod] = lod_evidence.get(selected_lod, 0) + 1
        road.clear()
    if not any(surfaces.values()):
        raise RoadProbeError("no classified LOD2 transport surface intersects the requested range")
    return surfaces


def transport_source_paths(source: Path) -> list[Path]:
    if source.is_file():
        return [source]
    if source.is_dir():
        paths = sorted(source.rglob("*tran*_op.gml"))
        if paths:
            return paths
    raise RoadProbeError(f"no PLATEAU transportation CityGML source found: {source}")


def extract_all_transport_surfaces(
    source: Path, latitude: float, longitude: float, ns_m: float, ew_m: float
):
    combined = {name: [] for name in SURFACE_STYLE}
    lod_evidence: dict[str, int] = {"lod3": 0, "lod2_fallback": 0}
    paths = transport_source_paths(source)
    for path in paths:
        try:
            surfaces = extract_transport_surfaces(
                path, latitude, longitude, ns_m, ew_m, lod_evidence
            )
        except RoadProbeError as exc:
            if "no classified" in str(exc):
                continue
            raise
        for category, records in surfaces.items():
            combined[category].extend(records)
    if not any(combined.values()):
        raise RoadProbeError("no classified transport surface intersects the requested range")
    return paths, combined, lod_evidence


def _display_vertex(x, y, altitude, offset):
    # MuJoCo X=North,Y=-East,Z=Up -> GLB X=East,Y=Up,Z=-North.
    return (-y, altitude - offset, -x)


def build_component_scenes(surfaces, samples, nrow, ncol, ns_m, ew_m, altitude_offset):
    terrain_vertices = []
    for row in range(nrow):
        y = -ew_m + 2.0 * ew_m * row / (nrow - 1)
        for col in range(ncol):
            x = -ns_m + 2.0 * ns_m * col / (ncol - 1)
            terrain_vertices.append(_display_vertex(x, y, samples[row*ncol + col], altitude_offset))
    terrain_faces = []
    for row in range(nrow - 1):
        for col in range(ncol - 1):
            a = row * ncol + col
            terrain_faces.extend(((a, a + ncol, a + 1), (a + 1, a + ncol, a + ncol + 1)))
    terrain = trimesh.Trimesh(
        vertices=np.asarray(terrain_vertices), faces=np.asarray(terrain_faces), process=False
    )
    terrain.visual.vertex_colors = np.tile(
        np.array([120, 145, 105, 255], dtype=np.uint8),
        (len(terrain.vertices), 1),
    )

    terrain_scene = trimesh.Scene()
    terrain_scene.add_geometry(terrain, node_name="terrain")
    road_scene = trimesh.Scene()
    triangle_counts = {}
    for category, polygons in surfaces.items():
        vertices, faces = [], []
        for _, polygon in polygons:
            for triangle in triangulate(polygon):
                if not polygon.covers(triangle.representative_point()):
                    continue
                coordinates = list(triangle.exterior.coords)[:3]
                base = len(vertices)
                for x, y in coordinates:
                    altitude = terrain_height(x, y, samples, nrow, ncol, ns_m, ew_m) + 0.03
                    vertices.append(_display_vertex(x, y, altitude, altitude_offset))
                faces.append((base, base + 1, base + 2))
        triangle_counts[category] = len(faces)
        if not faces:
            continue
        mesh = trimesh.Trimesh(
            vertices=np.asarray(vertices), faces=np.asarray(faces), process=False
        )
        mesh.visual.vertex_colors = np.tile(
            np.array(SURFACE_STYLE[category], dtype=np.uint8),
            (len(mesh.vertices), 1),
        )
        road_scene.add_geometry(mesh, node_name=category, geom_name=category)
    return terrain_scene, road_scene, triangle_counts


def build_scene(surfaces, samples, nrow, ncol, ns_m, ew_m, altitude_offset):
    terrain_scene, road_scene, triangle_counts = build_component_scenes(
        surfaces, samples, nrow, ncol, ns_m, ew_m, altitude_offset
    )
    scene = trimesh.Scene()
    for component in (terrain_scene, road_scene):
        for geometry in component.dump():
            scene.add_geometry(geometry)
    return scene, triangle_counts


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_component(scene, path: Path, receipt: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    scene.export(path)
    receipt.update({
        "output": str(path.resolve()),
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
    })
    receipt_path = path.with_name(path.stem + f"-{path.suffix.removeprefix('.')}-receipt.json")
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    return receipt_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--roads", type=Path, required=True)
    parser.add_argument("--terrain-receipt", type=Path, required=True)
    parser.add_argument("--world-frame", type=Path)
    parser.add_argument("--terrain-out", type=Path)
    parser.add_argument("--roads-out", type=Path)
    parser.add_argument("--out", type=Path, help="legacy combined terrain and roads GLB")
    args = parser.parse_args()
    if not args.out and not (args.terrain_out and args.roads_out):
        parser.error("specify both --terrain-out and --roads-out, or legacy --out")
    receipt = json.loads(args.terrain_receipt.read_text(encoding="utf-8"))
    world_frame_path = args.world_frame or Path(receipt["world_frame"])
    world_frame = load_world_frame(world_frame_path)
    nrow, ncol, samples = read_hfield(Path(receipt["hfield"]["path"]))
    center = world_frame["origin"]
    extent = world_frame["half_extent_m"]
    source_paths, surfaces, lod_evidence = extract_all_transport_surfaces(
        args.roads,
        center["latitude"],
        center["longitude"],
        extent["north_south"],
        extent["east_west"],
    )
    terrain_scene, road_scene, triangle_counts = build_component_scenes(
        surfaces,
        samples,
        nrow,
        ncol,
        extent["north_south"],
        extent["east_west"],
        center["altitude_offset_m"],
    )
    common_receipt = {
        "schema_version": 1,
        "terrain_receipt": str(args.terrain_receipt.resolve()),
        "world_frame": str(world_frame_path.resolve()),
    }
    if args.terrain_out and args.roads_out:
        _write_component(terrain_scene, args.terrain_out, {
            **common_receipt,
            "component": "terrain",
            "terrain_triangles": 2 * (nrow - 1) * (ncol - 1),
        })
        _write_component(road_scene, args.roads_out, {
            **common_receipt,
            "component": "roads",
            "sources": [str(path.resolve()) for path in source_paths],
            "surface_polygon_counts": {key: len(value) for key, value in surfaces.items()},
            "surface_triangle_counts": triangle_counts,
            "lod_polygon_counts": lod_evidence,
            "surface_colors_rgba": SURFACE_STYLE,
            "road_vertical_offset_m": 0.03,
        })
        print(f"OK: terrain GLB: {args.terrain_out}")
        print(f"OK: roads GLB: {args.roads_out}")
    if args.out:
        combined = trimesh.Scene()
        for component in (terrain_scene, road_scene):
            for geometry in component.dump():
                combined.add_geometry(geometry)
        _write_component(combined, args.out, {
            **common_receipt,
            "component": "terrain-roads-legacy",
            "sources": [str(path.resolve()) for path in source_paths],
            "surface_polygon_counts": {key: len(value) for key, value in surfaces.items()},
            "surface_triangle_counts": triangle_counts,
            "lod_polygon_counts": lod_evidence,
            "surface_colors_rgba": SURFACE_STYLE,
            "road_vertical_offset_m": 0.03,
        })
        print(f"OK: terrain and road GLB: {args.out}")
    print("OK: transport surfaces: " + ", ".join(
        f"{key}={len(value)} polygons/{triangle_counts.get(key, 0)} triangles"
        for key, value in surfaces.items()
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
