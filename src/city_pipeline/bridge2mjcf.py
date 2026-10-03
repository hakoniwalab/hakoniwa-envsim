#!/usr/bin/env python3
"""Generate lightweight MuJoCo bridge-deck collision from PLATEAU bridge data.

The deck is each bridge's OuterFloorSurface: from its LOD3 geometry when it
has any, else from its LOD2 bounded surfaces (many cities publish bridges at
LOD2 only; those still name their floor surfaces).

Each accepted OuterFloorSurface triangle is represented by one thin convex
triangular-prism mesh.  Keeping each prism independent is intentional:
MuJoCo collides with a mesh's convex hull, so combining a curved or branched
deck into one mesh would create collision where no bridge exists.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

import numpy as np

from bridge2glb import LOD2_TAGS, bridge_lod_in_use, bridge_source_paths, bridges_with_lod3, validate_bridge_crs
from citygml2glb import GlbError, _polygon_rings, triangulate_rings
from geodesy import epsg_label, project_to_local_enu
from mjcf_collision import COLLISION_MODES, collision_attributes
from mjcf_prism import format_numbers, polygon_prism_along_normal, triangular_prism
from road_terrain_probe import read_hfield, terrain_height
from world_frame import load_world_frame

GML = "http://www.opengis.net/gml"
BRID = "http://www.opengis.net/citygml/bridge/2.0"
GML_ID = f"{{{GML}}}id"
BRIDGE_TAG = f"{{{BRID}}}Bridge"
OUTER_FLOOR_TAG = f"{{{BRID}}}OuterFloorSurface"
WALL_TAG = f"{{{BRID}}}WallSurface"
# Bridge walls (piers, side walls, parapets) as colliders: a car drove through
# a pier that only had a look. Each wall triangle is a slab this thick along
# its normal (inwards), its face kept where the source puts it.
WALL_THICKNESS_M = 0.2
LOD3_TAGS = {f"{{{BRID}}}lod3Geometry", f"{{{BRID}}}lod3MultiSurface"}
POLYGON_TAG = f"{{{GML}}}Polygon"


class BridgePhysicsError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _mjcf_points(points, frame, epsg=6697):
    origin = frame["origin"]
    enu = project_to_local_enu(
        points, float(origin["latitude"]), float(origin["longitude"]), epsg
    )
    offset = float(origin["altitude_offset_m"])
    # Hakoniwa/MuJoCo city-world coordinates: X=North, Y=-East, Z=Up.
    return np.asarray([(north, -east, altitude - offset) for east, north, altitude in enu])


def _intersects_range(vertices: np.ndarray, frame) -> bool:
    extent = frame["half_extent_m"]
    ns_m = float(extent["north_south"])
    ew_m = float(extent["east_west"])
    return (
        float(vertices[:, 0].min()) <= ns_m
        and float(vertices[:, 0].max()) >= -ns_m
        and float(vertices[:, 1].min()) <= ew_m
        and float(vertices[:, 1].max()) >= -ew_m
    )


def triangle_slope_deg(vertices: np.ndarray) -> float:
    normal = np.cross(vertices[1] - vertices[0], vertices[2] - vertices[0])
    length = float(np.linalg.norm(normal))
    if length <= 1e-12:
        raise BridgePhysicsError("degenerate bridge triangle")
    vertical = min(1.0, abs(float(normal[2])) / length)
    return math.degrees(math.acos(vertical))


def _edge_key(first, second, tolerance_m=0.001):
    def point_key(point):
        return tuple(round(float(value) / tolerance_m) for value in point)
    a, b = point_key(first), point_key(second)
    return (a, b) if a <= b else (b, a)


def extract_prisms(source: Path, frame: dict, thickness_m: float, max_slope_deg: float,
                   *, include_walls: bool = False):
    """The floor pieces (OuterFloorSurface triangles as thin prisms), and with
    include_walls the WallSurface triangles too (surface_kind "wall"); the
    boundary and the counts describe the floors."""
    pieces = []
    wall_triangles = 0
    source_surfaces = selected_surfaces = rejected_polygons = 0
    rejected_slopes = rejected_degenerate = 0
    bridge_ids: set[str] = set()
    source_bridge_ids: set[str] = set()
    edges: Counter = Counter()
    edge_points = {}

    source_crs = set()
    for path in bridge_source_paths(source):
        epsg = validate_bridge_crs(path)
        source_crs.add(epsg_label(epsg))
        current_bridge = None
        floor_depth = wall_depth = lod3_depth = lod2_depth = polygon_depth = 0
        lod3_bridges = bridges_with_lod3(path)
        stack: list[ET.Element] = []
        for event, element in ET.iterparse(path, events=("start", "end")):
            if event == "start":
                stack.append(element)
                if element.tag == BRIDGE_TAG:
                    current_bridge = element.get(GML_ID, "unknown-bridge")
                    source_bridge_ids.add(current_bridge)
                if element.tag == OUTER_FLOOR_TAG:
                    floor_depth += 1
                    source_surfaces += 1
                if element.tag == WALL_TAG:
                    wall_depth += 1
                if element.tag in LOD3_TAGS:
                    lod3_depth += 1
                if element.tag in LOD2_TAGS:
                    lod2_depth += 1
                if element.tag == POLYGON_TAG:
                    polygon_depth += 1
                continue

            if element.tag == POLYGON_TAG:
                lod = bridge_lod_in_use(lod3_depth, lod2_depth, current_bridge, lod3_bridges)
                if include_walls and wall_depth and lod:
                    parsed = _polygon_rings(element)
                    if parsed:
                        rings = [_mjcf_points(points, frame, epsg).tolist() for _, points in parsed]
                        try:
                            vertices, faces = triangulate_rings(rings)
                        except (GlbError, ValueError):
                            vertices, faces = None, []
                        if vertices is not None and _intersects_range(vertices, frame):
                            for triangle_index, face in enumerate(faces):
                                triangle = np.asarray(vertices[face], dtype=float)
                                if np.linalg.norm(np.cross(triangle[1] - triangle[0], triangle[2] - triangle[0])) < 1e-4:
                                    continue
                                prism, prism_faces = polygon_prism_along_normal(triangle, WALL_THICKNESS_M)
                                pieces.append({
                                    "id": f"bridge_piece_{len(pieces):06d}",
                                    "bridge_id": current_bridge or "unknown-bridge",
                                    "lod": lod,
                                    "surface_kind": "wall",
                                    "surface_id": element.get(GML_ID, "wall"),
                                    "triangle_index": triangle_index,
                                    "source_vertices": triangle.tolist(),
                                    "vertices": prism,
                                    "faces": prism_faces,
                                })
                                wall_triangles += 1
                if floor_depth and lod:
                    parsed = _polygon_rings(element)
                    polygon_id = element.get(GML_ID, f"polygon-{source_surfaces}")
                    if parsed:
                        rings = [_mjcf_points(points, frame, epsg).tolist() for _, points in parsed]
                        try:
                            vertices, faces = triangulate_rings(rings)
                        except (GlbError, ValueError):
                            rejected_polygons += 1
                        else:
                            if _intersects_range(vertices, frame):
                                accepted_here = 0
                                for triangle_index, face in enumerate(faces):
                                    triangle = np.asarray(vertices[face], dtype=float)
                                    try:
                                        slope = triangle_slope_deg(triangle)
                                    except BridgePhysicsError:
                                        rejected_degenerate += 1
                                        continue
                                    if slope > max_slope_deg:
                                        rejected_slopes += 1
                                        continue
                                    prism, prism_faces = triangular_prism(triangle, thickness_m)
                                    piece_id = f"bridge_piece_{len(pieces):06d}"
                                    pieces.append({
                                        "id": piece_id,
                                        "bridge_id": current_bridge or "unknown-bridge",
                                        "lod": lod,
                                        "surface_id": polygon_id,
                                        "triangle_index": triangle_index,
                                        "slope_deg": slope,
                                        "source_vertices": triangle.tolist(),
                                        "vertices": prism,
                                        "faces": prism_faces,
                                    })
                                    bridge_ids.add(current_bridge or "unknown-bridge")
                                    accepted_here += 1
                                    for index in range(3):
                                        first = triangle[index]
                                        second = triangle[(index + 1) % 3]
                                        key = _edge_key(first, second)
                                        edges[key] += 1
                                        edge_points[key] = (first.tolist(), second.tolist())
                                if accepted_here:
                                    selected_surfaces += 1
                polygon_depth -= 1
                if len(stack) >= 2:
                    stack[-2].remove(element)
                element.clear()
            elif polygon_depth == 0:
                if element.tag == BRIDGE_TAG:
                    current_bridge = None
                element.clear()
            if element.tag == OUTER_FLOOR_TAG:
                floor_depth -= 1
            if element.tag == WALL_TAG:
                wall_depth -= 1
            if element.tag in LOD3_TAGS:
                lod3_depth -= 1
            if element.tag in LOD2_TAGS:
                lod2_depth -= 1
            stack.pop()

    boundary = []
    seen = set()
    for key, count in edges.items():
        if count != 1:
            continue
        for point in edge_points[key]:
            quantized = tuple(round(value, 3) for value in point)
            if quantized not in seen:
                seen.add(quantized)
                boundary.append(point)
    boundary.sort(key=lambda point: (point[0], point[1], point[2]))
    return pieces, boundary, {
        "source_crs": sorted(source_crs),
        "bridge_ids": sorted(bridge_ids),
        "source_bridge_ids": sorted(source_bridge_ids),
        "source_surface_count": source_surfaces,
        "selected_surface_count": selected_surfaces,
        "rejected_polygon_count": rejected_polygons,
        "rejected_slope_triangle_count": rejected_slopes,
        "rejected_degenerate_triangle_count": rejected_degenerate,
        "wall_triangle_count": wall_triangles,
    }


def endpoint_validation(boundary, terrain_receipt_path: Path | None):
    result = {
        "method": "outer_floor_boundary_vertices_vs_dem",
        "applied_correction": False,
        "boundary_vertex_count": len(boundary),
    }
    if not boundary or terrain_receipt_path is None:
        result.update({"status": "not_evaluated", "reason": "boundary_or_terrain_not_available"})
        return result, []
    receipt = json.loads(terrain_receipt_path.read_text(encoding="utf-8"))
    nrow, ncol, samples = read_hfield(Path(receipt["hfield"]["path"]))
    ns_m = float(receipt["half_extent_m"]["north_south"])
    ew_m = float(receipt["half_extent_m"]["east_west"])
    offset = float(receipt["altitude_offset_m"])
    records = []
    for x, y, relative_z in boundary:
        terrain_absolute = terrain_height(x, y, samples, nrow, ncol, ns_m, ew_m)
        bridge_absolute = relative_z + offset
        records.append({
            "x_m": x, "y_m": y,
            "bridge_height_m": bridge_absolute,
            "neighboring_terrain_height_m": terrain_absolute,
            "difference_m": bridge_absolute - terrain_absolute,
        })
    differences = [record["difference_m"] for record in records]
    candidates = sorted(records, key=lambda item: (abs(item["difference_m"]), item["x_m"], item["y_m"]))[:8]
    result.update({
        "status": "measured",
        "sample_count": len(records),
        "difference_m": {
            "minimum": min(differences),
            "maximum": max(differences),
            "median": statistics.median(differences),
        },
        "connection_candidates": candidates,
        "candidate_policy": "up to 8 boundary vertices with smallest absolute DEM height difference",
    })
    return result, records


def terrain_relationship_validation(pieces, terrain_receipt_path: Path | None):
    result = {
        "method": "source_bridge_triangle_vertices_vs_dem",
        "applied_correction": False,
    }
    if not pieces or terrain_receipt_path is None:
        result.update({"status": "not_evaluated", "reason": "bridge_or_terrain_not_available"})
        return result
    receipt = json.loads(terrain_receipt_path.read_text(encoding="utf-8"))
    nrow, ncol, samples = read_hfield(Path(receipt["hfield"]["path"]))
    ns_m = float(receipt["half_extent_m"]["north_south"])
    ew_m = float(receipt["half_extent_m"]["east_west"])
    offset = float(receipt["altitude_offset_m"])
    unique = {}
    for piece in pieces:
        for x, y, relative_z in piece["source_vertices"]:
            unique[(round(x, 3), round(y, 3), round(relative_z, 3))] = (x, y, relative_z)
    differences = []
    for x, y, relative_z in unique.values():
        terrain_absolute = terrain_height(x, y, samples, nrow, ncol, ns_m, ew_m)
        differences.append(relative_z + offset - terrain_absolute)
    tolerance = 0.05
    result.update({
        "status": "measured",
        "sample_count": len(differences),
        "difference_m": {
            "minimum": min(differences),
            "maximum": max(differences),
            "median": statistics.median(differences),
        },
        "below_dem_tolerance_m": tolerance,
        "below_dem_sample_count": sum(value < -tolerance for value in differences),
        "interpretation": (
            "negative values are reported source/DEM disagreement; bridge geometry is not moved"
        ),
    })
    return result


def write_mjcf(path: Path, pieces, collision_mode: str) -> None:
    root = ET.Element("mujoco", {"model": "plateau_bridge_physics"})
    asset = ET.SubElement(root, "asset")
    worldbody = ET.SubElement(root, "worldbody")
    for piece in pieces:
        ET.SubElement(asset, "mesh", {
            "name": piece["id"],
            "vertex": format_numbers(piece["vertices"].reshape(-1)),
            "face": " ".join(str(int(value)) for value in piece["faces"].reshape(-1)),
        })
        ET.SubElement(worldbody, "geom", {
            "name": piece["id"], "type": "mesh", "mesh": piece["id"],
            "rgba": "0.32 0.48 0.62 1",
            **collision_attributes(collision_mode),
        })
    if not pieces:
        root.remove(asset)
    ET.indent(root, space="  ")
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(path, encoding="unicode", xml_declaration=False)
    with path.open("a", encoding="utf-8") as stream:
        stream.write("\n")


def convert(source: Path, world_frame_path: Path, output: Path, receipt_path: Path,
            terrain_receipt: Path | None, thickness_m: float, max_slope_deg: float,
            collision_mode: str = "all", walls: bool = True):
    if thickness_m <= 0:
        raise BridgePhysicsError("collision thickness must be positive")
    if not 0 < max_slope_deg < 90:
        raise BridgePhysicsError("maximum surface slope must be in (0, 90)")
    if collision_mode not in COLLISION_MODES:
        raise BridgePhysicsError("collision mode must be all, drone, or none")
    frame = load_world_frame(world_frame_path)
    sources = bridge_source_paths(source)
    pieces, boundary, counts = extract_prisms(source, frame, thickness_m, max_slope_deg, include_walls=walls)
    floors = [piece for piece in pieces if piece.get("surface_kind") != "wall"]
    source_crs = ",".join(counts.pop("source_crs", None) or ["EPSG:6697"])
    endpoint, endpoint_records = endpoint_validation(boundary, terrain_receipt)
    terrain_relationship = terrain_relationship_validation(floors, terrain_receipt)
    write_mjcf(output, pieces, collision_mode)
    debug_dir = output.parent / "debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    debug_path = debug_dir / "bridge-surfaces.json"
    debug = {
        "pieces": [{key: value for key, value in piece.items() if key not in {"vertices", "faces"}}
                   for piece in pieces],
        "endpoint_samples": endpoint_records,
    }
    debug_path.write_text(json.dumps(debug, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    status = "available" if pieces else "scoped_out"
    receipt = {
        "schema_version": 1,
        "component": "bridges",
        "status": status,
        "capability": "bridge_surface_collision" if pieces else "scoped_out",
        "reason": None if pieces else "usable_bridge_surface_not_available",
        "source": "PLATEAU CityGML",
        "sources": [{"path": str(path.resolve()), "sha256": _sha256(path)} for path in sources],
        "source_crs": source_crs,
        "lod_used": max((piece["lod"] for piece in pieces), default=None),
        "lod_policy": "per bridge: LOD3 OuterFloorSurface when it has LOD3 geometry, else LOD2",
        "lod2_bridge_count": len({piece["bridge_id"] for piece in pieces if piece["lod"] == 2}),
        "lod3_bridge_count": len({piece["bridge_id"] for piece in pieces if piece["lod"] == 3}),
        "surface_source": "OuterFloorSurface" + (" + WallSurface" if walls else ""),
        "surface_selection": {"maximum_slope_deg": max_slope_deg},
        "physics_representation": "independent_thin_convex_triangular_prism_meshes",
        "collision_filter": {
            "mode": collision_mode,
            **collision_attributes(collision_mode),
        },
        "source_bridge_count": len(counts["source_bridge_ids"]),
        "bridge_count": len(counts["bridge_ids"]),
        **counts,
        "physics_geom_count": len(pieces),
        "endpoint_height_validation": endpoint,
        "terrain_relationship_validation": terrain_relationship,
        "derived_geometry": [{
            "type": "collision_thickness",
            "value_m": thickness_m,
            "direction": "negative_world_z",
            "purpose": "make each source surface triangle a watertight convex collision mesh",
        }] if pieces else [],
        "corrections": [],
        "limitations": [
            "bridge inspection geometry is outside current scope",
            "floors: only geometrically walkable OuterFloorSurface triangles (LOD3, else LOD2) are collision-enabled",
            "triangular-prism pieces are not yet merged or otherwise optimized",
        ],
        "mjcf": {"path": str(output.resolve()), "sha256": _sha256(output)},
        "debug": str(debug_path.resolve()),
    }
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--world-frame", type=Path, required=True)
    parser.add_argument("--terrain-receipt", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--collision-thickness", type=float, default=0.02)
    parser.add_argument("--max-slope-deg", type=float, default=60.0)
    parser.add_argument("--collide", choices=tuple(COLLISION_MODES), default="all")
    parser.add_argument("--no-walls", action="store_true",
                        help="floors only (default: the bridges' WallSurface piers and parapets collide too)")
    args = parser.parse_args()
    receipt = args.receipt or args.out.with_name("receipt.json")
    result = convert(
        args.source, args.world_frame, args.out, receipt, args.terrain_receipt,
        args.collision_thickness, args.max_slope_deg, args.collide, walls=not args.no_walls,
    )
    print(f"OK: bridge physics status={result['status']} geoms={result['physics_geom_count']}")
    print(f"OK: bridge MJCF: {args.out}")
    print(f"OK: bridge receipt: {receipt}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
