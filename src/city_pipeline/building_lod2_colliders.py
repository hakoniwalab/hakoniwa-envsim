"""Class-specific LOD2 building collider helpers.

P1 and P2 replace a legacy LOD1 approximation with thin convex prisms derived
from source LOD2 WallSurface and RoofSurface polygons. P3 includes the same
outer profile plus source OuterCeilingSurface and OuterFloorSurface polygons
so that overhang undersides are represented without filling the space below.
GroundSurface is omitted because the city terrain owns the floor.
"""

from __future__ import annotations

import json
import heapq
from collections import defaultdict
from pathlib import Path
from typing import NamedTuple
from xml.etree import ElementTree as ET

import numpy as np
from shapely.geometry import Polygon
from shapely.ops import unary_union
from shapely.strtree import STRtree

from citygml2glb import GML_ID, NS, _polygon_rings, triangulate_rings
from geodesy import project_epsg6697_to_local_enu
from mjcf_prism import (
    polygon_prism,
    polygon_prism_along_normal,
    polygon_prism_for_surface,
    triangular_prism,
    triangular_prism_along_normal,
)
from gml_lod1_extract import validate_epsg6697_contract
from world_frame import load_world_frame
from building_tolerant_planar import reduce_tolerant_planar

B = "{http://www.opengis.net/citygml/building/2.0}"
GML_POLYGON = "{http://www.opengis.net/gml}Polygon"
MIN_TRIANGLE_AREA_M2 = 1e-6
MIN_TRIANGLE_SHAPE_RATIO = 1e-6
PLANAR_ABSOLUTE_TOLERANCE_M = 1e-5
PLANAR_RELATIVE_TOLERANCE = 1e-8
COPLANAR_NORMAL_DECIMALS = 6
COPLANAR_OFFSET_DECIMALS = 4
UNION_AREA_TOLERANCE_M2 = 1e-6
COLLIDER_REDUCTION_MODES = {
    "safe", "coplanar-union", "convex-decompose", "tolerant-planar"
}
CLASS_SURFACE_KINDS = {
    "P1": ("WallSurface", "RoofSurface"),
    "P2": ("WallSurface", "RoofSurface"),
    "P3": ("WallSurface", "RoofSurface", "OuterCeilingSurface", "OuterFloorSurface"),
}


class BuildingLod2ColliderError(RuntimeError):
    pass


class PreparedSurfaceGeometry(NamedTuple):
    pieces: list[dict]
    skipped_degenerate_by_surface: dict[str, int]
    collider_optimization: dict


def _convex_planar_ring(points):
    """Return a cleaned convex planar ring, or a reason it must stay triangulated."""
    values = np.asarray(points, dtype=float)
    if values.ndim != 2 or values.shape[1] != 3 or len(values) < 3:
        return None, "invalid_ring"
    cleaned = []
    for point in values:
        if not cleaned or float(np.linalg.norm(point - cleaned[-1])) > 1e-9:
            cleaned.append(point)
    if len(cleaned) > 2 and float(np.linalg.norm(cleaned[0] - cleaned[-1])) <= 1e-9:
        cleaned.pop()
    if len(cleaned) < 3:
        return None, "degenerate_ring"
    values = np.asarray(cleaned, dtype=float)

    normal = np.zeros(3, dtype=float)
    for index, current in enumerate(values):
        normal += np.cross(current, values[(index + 1) % len(values)])
    normal_length = float(np.linalg.norm(normal))
    if normal_length <= 1e-12:
        return None, "degenerate_ring"
    unit_normal = normal / normal_length
    scale = max(float(np.ptp(values, axis=0).max()), 1.0)
    tolerance = max(PLANAR_ABSOLUTE_TOLERANCE_M, scale * PLANAR_RELATIVE_TOLERANCE)
    distances = np.abs((values - values[0]) @ unit_normal)
    if float(distances.max()) > tolerance:
        return None, "non_planar"

    drop_axis = int(np.argmax(np.abs(unit_normal)))
    projected = np.delete(values, drop_axis, axis=1)

    def cross_2d(first, second):
        return float(first[0] * second[1] - first[1] * second[0])

    changed = True
    while changed and len(projected) > 3:
        changed = False
        keep = []
        for index in range(len(projected)):
            previous = projected[index - 1]
            current = projected[index]
            following = projected[(index + 1) % len(projected)]
            cross = cross_2d(current - previous, following - current)
            edge_scale = max(
                float(np.linalg.norm(current - previous)),
                float(np.linalg.norm(following - current)),
                1.0,
            )
            if abs(cross) <= 1e-10 * edge_scale * edge_scale:
                changed = True
            else:
                keep.append(index)
        if len(keep) < 3:
            return None, "degenerate_ring"
        values = values[keep]
        projected = projected[keep]

    turns = []
    for index in range(len(projected)):
        previous = projected[index - 1]
        current = projected[index]
        following = projected[(index + 1) % len(projected)]
        turns.append(cross_2d(current - previous, following - current))
    positive = any(value > 0 for value in turns)
    negative = any(value < 0 for value in turns)
    if positive and negative:
        return None, "concave"
    return values, None


def _oriented_plane(points):
    values = np.asarray(points, dtype=float)
    normal = np.zeros(3, dtype=float)
    for index, current in enumerate(values):
        normal += np.cross(current, values[(index + 1) % len(values)])
    length = float(np.linalg.norm(normal))
    if length <= 1e-12:
        return None
    normal /= length
    return normal, float(normal @ values[0])


def _plane_basis(normal):
    reference = np.asarray(
        (1.0, 0.0, 0.0) if abs(float(normal[0])) < 0.9 else (0.0, 1.0, 0.0)
    )
    axis_u = np.cross(normal, reference)
    axis_u /= np.linalg.norm(axis_u)
    axis_v = np.cross(normal, axis_u)
    return axis_u, axis_v


def _is_convex_polygon(polygon):
    if polygon.is_empty or not polygon.is_valid or polygon.interiors:
        return False
    return polygon.convex_hull.area - polygon.area <= max(
        UNION_AREA_TOLERANCE_M2, polygon.area * 1e-9
    )


def _apply_coplanar_union(
    pieces, thickness_m, stats, *, include_triangulated_fallback: bool = False,
    progress_callback=None,
):
    """Exactly merge adjacent source polygons when their union is one convex face.

    This deliberately does not snap vertices or take a convex hull. Any gap,
    hole, concavity, different semantic surface kind, or different plane keeps
    the existing safe colliders unchanged.
    """
    groups = defaultdict(list)
    retained = []
    for piece in pieces:
        eligible = piece.pop("_coplanar_union_eligible", False)
        scope = piece.pop("_coplanar_union_scope", None)
        if not eligible or (scope is not None and not include_triangulated_fallback):
            piece.pop("_coplanar_union_scope", None)
            retained.append(piece)
            continue
        ring = np.asarray(piece["source_vertices"], dtype=float)
        plane = _oriented_plane(ring)
        if plane is None:
            retained.append(piece)
            continue
        normal, offset = plane
        key = (
            piece["building_id"],
            piece["surface_kind"],
            scope,
            *(round(float(value), COPLANAR_NORMAL_DECIMALS) for value in normal),
            round(offset, COPLANAR_OFFSET_DECIMALS),
        )
        groups[key].append((piece, ring, normal, offset))

    merged = []
    rejected_non_convex = 0
    rejected_holes = 0
    group_values = list(groups.values())
    progress_interval = max(1000, len(group_values) // 100)
    for group_index, group in enumerate(group_values, start=1):
        if len(group) < 2:
            merged.extend(item[0] for item in group)
            if progress_callback is not None and (
                group_index == 1
                or group_index == len(group_values)
                or group_index % progress_interval == 0
            ):
                progress_callback(group_index, len(group_values))
            continue
        normal = group[0][2]
        offset = group[0][3]
        axis_u, axis_v = _plane_basis(normal)
        projected = [
            Polygon([(float(point @ axis_u), float(point @ axis_v)) for point in ring])
            for _, ring, _, _ in group
        ]
        candidates = [
            {"polygon": polygon, "pieces": [item[0]]}
            for item, polygon in zip(group, projected)
        ]
        if len(projected) <= 8:
            # Most source polygons contain only a few triangles. A spatial
            # index costs more than direct comparison at this size.
            edges = [
                (left, right)
                for left in range(len(projected))
                for right in range(left + 1, len(projected))
            ]
        else:
            spatial_index = STRtree(projected)
            edges = sorted({
                (left, int(right))
                for left, polygon in enumerate(projected)
                for right in spatial_index.query(polygon)
                if int(right) > left
            })
        parent = list(range(len(candidates)))
        neighbors = [set() for _ in candidates]
        for left, right in edges:
            neighbors[left].add(right)
            neighbors[right].add(left)

        def find(index):
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        pending = list(edges)
        heapq.heapify(pending)
        queued = set(edges)
        while pending:
            original_left, original_right = heapq.heappop(pending)
            queued.discard((original_left, original_right))
            left, right = find(original_left), find(original_right)
            if left == right:
                continue
            first_polygon = candidates[left]["polygon"]
            second_polygon = candidates[right]["polygon"]
            if first_polygon.boundary.intersection(
                second_polygon.boundary
            ).length <= UNION_AREA_TOLERANCE_M2:
                continue
            component = unary_union([first_polygon, second_polygon])
            if component.geom_type != "Polygon":
                continue
            if component.interiors:
                rejected_holes += 1
                continue
            if not _is_convex_polygon(component):
                rejected_non_convex += 1
                continue
            keep, discard = min(left, right), max(left, right)
            candidates[keep] = {
                "polygon": component,
                "pieces": candidates[left]["pieces"] + candidates[right]["pieces"],
            }
            parent[discard] = keep
            stats["convex_merge_count"] += 1
            stats["convex_merge_colliders_eliminated"] += 1

            affected = neighbors[keep] | neighbors[discard]
            neighbors[keep] = set()
            neighbors[discard].clear()
            for candidate_index in affected:
                root = find(candidate_index)
                if root == keep:
                    continue
                neighbors[keep].add(root)
                neighbors[root].discard(discard)
                neighbors[root].add(keep)
                pair = (min(keep, root), max(keep, root))
                if pair not in queued:
                    heapq.heappush(pending, pair)
                    queued.add(pair)

        candidates = [
            candidates[index]
            for index in range(len(candidates))
            if find(index) == index
        ]

        for candidate in candidates:
            component = candidate["polygon"]
            first = candidate["pieces"][0]
            # Unchanged candidates preserve their original prism exactly.
            matching_original = next(
                (item[0] for item, polygon in zip(group, projected)
                 if polygon.equals(component)),
                None,
            )
            if matching_original is not None:
                merged.append(matching_original)
                continue
            coordinates = list(component.exterior.coords)[:-1]
            ring = np.asarray([
                axis_u * x + axis_v * y + normal * offset for x, y in coordinates
            ])
            ring, cleanup_reason = _convex_planar_ring(ring)
            if cleanup_reason is not None:
                merged.extend(candidate["pieces"])
                continue
            oriented = _oriented_plane(ring)
            if oriented is None:
                merged.extend(candidate["pieces"])
                continue
            if float(oriented[0] @ normal) < 0:
                ring = ring[::-1]
            prism, prism_faces, _ = polygon_prism_for_surface(
                ring,
                thickness_m,
                prefer_world_z=(first["surface_kind"] == "RoofSurface"),
            )
            merged.append({
                **first,
                "source_vertices": ring.tolist(),
                "vertices": prism,
                "faces": prism_faces,
                "_collider_origin": "coplanar-union",
            })
        if progress_callback is not None and (
            group_index == 1
            or group_index == len(group_values)
            or group_index % progress_interval == 0
        ):
            progress_callback(group_index, len(group_values))

    stats["convex_merge_rejected_non_convex_count"] = rejected_non_convex
    stats["convex_merge_rejected_hole_count"] = rejected_holes
    return retained + merged


def _selected_class(selection_path: Path, classification_path: Path, class_id: str):
    selected, center_lat, center_lon = _selected_classes(
        selection_path, classification_path, (class_id,)
    )
    return {
        source: classes[class_id]
        for source, classes in selected.items()
        if classes.get(class_id)
    }, center_lat, center_lon


def _selected_classes(selection_path: Path, classification_path: Path, class_ids):
    selection = json.loads(selection_path.read_text(encoding="utf-8"))
    classification = json.loads(classification_path.read_text(encoding="utf-8"))
    wanted = set(class_ids)
    selected_class = {
        record["building_id"]: record["class"]
        for record in classification.get("buildings", [])
        if record.get("class") in wanted
    }
    by_source = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for part in selection.get("polygons", []):
        building_id = part["id"].split("__part_", 1)[0]
        class_id = selected_class.get(building_id)
        if class_id is not None:
            by_source[Path(part["source_gml"]).resolve()][class_id][building_id].append(part)
    missing = sorted(
        set(selected_class)
        - {
            building_id
            for classes in by_source.values()
            for buildings in classes.values()
            for building_id in buildings
        }
    )
    if missing:
        raise BuildingLod2ColliderError(
            f"class-specific buildings missing from LOD1 selection: {missing}"
        )
    origin = selection.get("origin") or {}
    return by_source, float(origin["lat"]), float(origin["lon"])


def _surface_pieces(
    by_source, center_lat, center_lon, frame, thickness_m: float, class_id: str,
    collider_reduction: str = "safe",
):
    wrapped = {
        source: {class_id: buildings} for source, buildings in by_source.items()
    }
    return _surface_pieces_for_classes(
        wrapped, center_lat, center_lon, frame, thickness_m, (class_id,),
        collider_reduction,
    )[class_id]


def _surface_pieces_for_classes(
    by_source, center_lat, center_lon, frame, thickness_m: float, class_ids,
    collider_reduction: str = "safe",
):
    """Prepare all requested classes while parsing each source GML only once."""
    offset = float(frame["origin"]["altitude_offset_m"])
    pieces = {class_id: [] for class_id in class_ids}
    counters = {class_id: CounterLike() for class_id in class_ids}
    skipped = {class_id: defaultdict(int) for class_id in class_ids}
    optimization = {
        class_id: {
            "triangles_before": 0,
            "colliders_after": 0,
            "convex_polygon_collider_count": 0,
            "triangular_fallback_collider_count": 0,
            "merged_group_count": 0,
            "triangles_eliminated": 0,
            "fallback_polygon_counts": defaultdict(int),
            "reduction_mode": collider_reduction,
            "colliders_before_reduction": 0,
            "convex_merge_count": 0,
            "convex_merge_colliders_eliminated": 0,
            "roof_normal_extrusion_fallback_count": 0,
        }
        for class_id in class_ids
    }
    ordered_sources = sorted(by_source.items(), key=lambda item: str(item[0]))
    for source_index, (source, classes) in enumerate(ordered_sources, start=1):
        root = ET.parse(source).getroot()
        validate_epsg6697_contract(root, source)
        indexed = {element.get(GML_ID): element for element in root.findall(".//bldg:Building", NS)}
        for class_id in class_ids:
            buildings = classes.get(class_id, {})
            for building_id in sorted(buildings):
                building = indexed.get(building_id)
                if building is None:
                    raise BuildingLod2ColliderError(
                        f"{class_id} building not found in source: {building_id}"
                    )
                seen = set()
                for surface_kind in CLASS_SURFACE_KINDS[class_id]:
                    for surface in building.iter(B + surface_kind):
                        for polygon in surface.iter(GML_POLYGON):
                            polygon_key = polygon.get(GML_ID) or id(polygon)
                            key = (surface_kind, polygon_key)
                            if key in seen:
                                continue
                            seen.add(key)
                            parsed = _polygon_rings(polygon)
                            if not parsed:
                                continue
                            rings = []
                            for _, points in parsed:
                                enu = project_epsg6697_to_local_enu(
                                    points, center_lat, center_lon
                                )
                                rings.append([
                                    (north, -east, altitude - offset)
                                    for east, north, altitude in enu
                                ])
                            vertices, faces = triangulate_rings(rings)
                            valid_triangles = []
                            for face in faces:
                                triangle = np.asarray(vertices[face], dtype=float)
                                edges = [
                                    float(np.linalg.norm(
                                        triangle[(index + 1) % 3] - triangle[index]
                                    ))
                                    for index in range(3)
                                ]
                                doubled_area = float(np.linalg.norm(np.cross(
                                    triangle[1] - triangle[0], triangle[2] - triangle[0]
                                )))
                                max_edge = max(edges)
                                shape_ratio = (
                                    doubled_area / (max_edge * max_edge)
                                    if max_edge else 0.0
                                )
                                if (
                                    doubled_area / 2.0 < MIN_TRIANGLE_AREA_M2
                                    or shape_ratio < MIN_TRIANGLE_SHAPE_RATIO
                                ):
                                    skipped[class_id][surface_kind] += 1
                                    continue
                                valid_triangles.append(triangle)

                            stats = optimization[class_id]
                            stats["triangles_before"] += len(valid_triangles)
                            merged_ring = None
                            fallback_reason = None
                            if len(rings) == 1:
                                merged_ring, fallback_reason = _convex_planar_ring(
                                    rings[0]
                                )
                            else:
                                fallback_reason = "interior_ring"

                            if merged_ring is not None and valid_triangles:
                                prism, prism_faces, extrusion_mode = (
                                    polygon_prism_for_surface(
                                        merged_ring,
                                        thickness_m,
                                        prefer_world_z=(
                                            surface_kind == "RoofSurface"
                                        ),
                                    )
                                )
                                if extrusion_mode == "surface-normal-fallback":
                                    stats["roof_normal_extrusion_fallback_count"] += 1
                                piece_index = counters[class_id].next(building_id)
                                pieces[class_id].append({
                                    "id": (
                                        f"{class_id.lower()}_surface_{building_id}_"
                                        f"piece_{piece_index:04d}"
                                    ),
                                    "building_id": building_id,
                                    "surface_kind": surface_kind,
                                    "source_polygon_id": str(polygon_key),
                                    "source_vertices": merged_ring.tolist(),
                                    "vertices": prism,
                                    "faces": prism_faces,
                                    "_coplanar_union_eligible": True,
                                    "_coplanar_union_scope": None,
                                    "_collider_origin": "convex-polygon",
                                })
                                stats["colliders_after"] += 1
                                stats["convex_polygon_collider_count"] += 1
                                if len(valid_triangles) > 1:
                                    stats["merged_group_count"] += 1
                                    stats["triangles_eliminated"] += (
                                        len(valid_triangles) - 1
                                    )
                                continue

                            if fallback_reason:
                                stats["fallback_polygon_counts"][fallback_reason] += 1
                            for triangle in valid_triangles:
                                prism, prism_faces, extrusion_mode = (
                                    polygon_prism_for_surface(
                                        triangle,
                                        thickness_m,
                                        prefer_world_z=(
                                            surface_kind == "RoofSurface"
                                        ),
                                    )
                                )
                                if extrusion_mode == "surface-normal-fallback":
                                    stats["roof_normal_extrusion_fallback_count"] += 1
                                piece_index = counters[class_id].next(building_id)
                                pieces[class_id].append({
                                    "id": (
                                        f"{class_id.lower()}_surface_{building_id}_"
                                        f"piece_{piece_index:04d}"
                                    ),
                                    "building_id": building_id,
                                    "surface_kind": surface_kind,
                                    "source_polygon_id": str(polygon_key),
                                    "source_vertices": triangle.tolist(),
                                    "vertices": prism,
                                    "faces": prism_faces,
                                    "_coplanar_union_eligible": True,
                                    "_coplanar_union_scope": str(polygon_key),
                                    "_collider_origin": "triangular-fallback",
                                })
                                stats["colliders_after"] += 1
                                stats["triangular_fallback_collider_count"] += 1
                if counters[class_id].value(building_id) == 0:
                    raise BuildingLod2ColliderError(
                        f"{class_id} building has no usable source collision surfaces: "
                        f"{building_id}"
                    )
        print(
            "[HAKO_PROGRESS] " + json.dumps({
                "phase": "building_physics_surfaces",
                "current": source_index,
                "total": len(ordered_sources),
            }, separators=(",", ":")),
            flush=True,
        )
    if collider_reduction in {
        "coplanar-union", "convex-decompose", "tolerant-planar"
    }:
        for class_index, class_id in enumerate(class_ids, start=1):
            stats = optimization[class_id]
            stats["colliders_before_reduction"] = len(pieces[class_id])
            print(
                "[HAKO_PROGRESS] " + json.dumps({
                    "phase": "building_physics_exact_reduction",
                    "current": class_index,
                    "total": len(class_ids),
                    "class_id": class_id,
                    "colliders": len(pieces[class_id]),
                }, separators=(",", ":")),
                flush=True,
            )
            pieces[class_id] = _apply_coplanar_union(
                pieces[class_id], thickness_m, stats,
                include_triangulated_fallback=(
                    collider_reduction in {"convex-decompose", "tolerant-planar"}
                ),
                progress_callback=lambda current, total, class_id=class_id: print(
                    "[HAKO_PROGRESS] " + json.dumps({
                        "phase": "building_physics_exact_groups",
                        "current": current,
                        "total": total,
                        "class_id": class_id,
                    }, separators=(",", ":")),
                    flush=True,
                ),
            )
            if collider_reduction == "tolerant-planar":
                print(
                    "[HAKO_PROGRESS] " + json.dumps({
                        "phase": "building_physics_tolerant_reduction",
                        "current": class_index,
                        "total": len(class_ids),
                        "class_id": class_id,
                        "colliders": len(pieces[class_id]),
                    }, separators=(",", ":")),
                    flush=True,
                )
                pieces[class_id], tolerant_stats = reduce_tolerant_planar(
                    pieces[class_id],
                    thickness_m=thickness_m,
                    tolerance_m=0.05,
                    normal_tolerance_deg=2.0,
                    surface_kinds=("WallSurface",),
                    preserve_box_primitives=True,
                    progress_callback=lambda current, total, class_id=class_id: print(
                        "[HAKO_PROGRESS] " + json.dumps({
                            "phase": "building_physics_tolerant_groups",
                            "current": current,
                            "total": total,
                            "class_id": class_id,
                        }, separators=(",", ":")),
                        flush=True,
                    ),
                )
                stats["tolerant_planar"] = tolerant_stats
            stats["colliders_after"] = len(pieces[class_id])
            stats["convex_polygon_collider_count"] = sum(
                piece.get("_collider_origin") != "triangular-fallback"
                for piece in pieces[class_id]
            )
            stats["triangular_fallback_collider_count"] = sum(
                piece.get("_collider_origin") == "triangular-fallback"
                for piece in pieces[class_id]
            )
            for piece in pieces[class_id]:
                piece.pop("_collider_origin", None)
                piece.pop("_tolerant_source_count", None)
                piece.pop("_tolerant_maximum_displacement_m", None)
                piece.pop("_extrusion_mode", None)
    else:
        for class_id in class_ids:
            optimization[class_id]["colliders_before_reduction"] = len(
                pieces[class_id]
            )
            for piece in pieces[class_id]:
                piece.pop("_coplanar_union_eligible", None)
                piece.pop("_coplanar_union_scope", None)
                piece.pop("_collider_origin", None)

    return {
        class_id: PreparedSurfaceGeometry(
            pieces=pieces[class_id],
            skipped_degenerate_by_surface=dict(sorted(skipped[class_id].items())),
            collider_optimization={
                **{
                    key: value
                    for key, value in optimization[class_id].items()
                    if key != "fallback_polygon_counts"
                },
                "fallback_polygon_counts": dict(sorted(
                    optimization[class_id]["fallback_polygon_counts"].items()
                )),
                "rejected_concave_count": optimization[class_id][
                    "fallback_polygon_counts"
                ].get("concave", 0),
                "reduction_ratio": (
                    1.0 - (
                        optimization[class_id]["colliders_after"]
                        / optimization[class_id]["triangles_before"]
                    )
                    if optimization[class_id]["triangles_before"] else 0.0
                ),
            },
        )
        for class_id in class_ids
    }


class CounterLike:
    def __init__(self):
        self.values = defaultdict(int)

    def next(self, key):
        value = self.values[key]
        self.values[key] += 1
        return value

    def value(self, key):
        return self.values[key]


def prepare_class_geometry(
    selection_path: Path,
    classification_path: Path,
    world_frame_path: Path,
    *,
    class_id: str,
    roof_thickness_m: float,
    collider_reduction: str = "safe",
):
    if class_id not in CLASS_SURFACE_KINDS:
        raise BuildingLod2ColliderError(f"unsupported LOD2 collider class: {class_id}")
    if roof_thickness_m <= 0:
        raise BuildingLod2ColliderError(
            f"{class_id} surface collision thickness must be positive"
        )
    if collider_reduction not in COLLIDER_REDUCTION_MODES:
        raise BuildingLod2ColliderError(
            f"unsupported building collider reduction: {collider_reduction}"
        )
    by_source, center_lat, center_lon = _selected_class(
        selection_path, classification_path, class_id
    )
    frame = load_world_frame(world_frame_path)
    geometry = _surface_pieces(
        by_source, center_lat, center_lon, frame, roof_thickness_m, class_id,
        collider_reduction,
    )
    return geometry


def prepare_classes_geometry(
    selection_path: Path,
    classification_path: Path,
    world_frame_path: Path,
    *,
    class_ids,
    roof_thickness_m: float,
    collider_reduction: str = "safe",
):
    class_ids = tuple(class_ids)
    if any(class_id not in CLASS_SURFACE_KINDS for class_id in class_ids):
        raise BuildingLod2ColliderError(f"unsupported LOD2 collider classes: {class_ids}")
    if roof_thickness_m <= 0:
        raise BuildingLod2ColliderError(
            "surface collision thickness must be positive"
        )
    if collider_reduction not in COLLIDER_REDUCTION_MODES:
        raise BuildingLod2ColliderError(
            f"unsupported building collider reduction: {collider_reduction}"
        )
    by_source, center_lat, center_lon = _selected_classes(
        selection_path, classification_path, class_ids
    )
    frame = load_world_frame(world_frame_path)
    return _surface_pieces_for_classes(
        by_source, center_lat, center_lon, frame, roof_thickness_m, class_ids,
        collider_reduction,
    )


def prepare_p1_geometry(
    selection_path: Path,
    classification_path: Path,
    world_frame_path: Path,
    *,
    roof_thickness_m: float,
    collider_reduction: str = "safe",
):
    return prepare_class_geometry(
        selection_path,
        classification_path,
        world_frame_path,
        class_id="P1",
        roof_thickness_m=roof_thickness_m,
        collider_reduction=collider_reduction,
    )


def prepare_p2_geometry(
    selection_path: Path,
    classification_path: Path,
    world_frame_path: Path,
    *,
    roof_thickness_m: float,
    collider_reduction: str = "safe",
):
    return prepare_class_geometry(
        selection_path,
        classification_path,
        world_frame_path,
        class_id="P2",
        roof_thickness_m=roof_thickness_m,
        collider_reduction=collider_reduction,
    )


def prepare_p3_geometry(
    selection_path: Path,
    classification_path: Path,
    world_frame_path: Path,
    *,
    roof_thickness_m: float,
    collider_reduction: str = "safe",
):
    return prepare_class_geometry(
        selection_path,
        classification_path,
        world_frame_path,
        class_id="P3",
        roof_thickness_m=roof_thickness_m,
        collider_reduction=collider_reduction,
    )
