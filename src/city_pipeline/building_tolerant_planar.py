"""Bounded, opt-in approximation for adjacent building wall colliders."""

from __future__ import annotations

import math
import heapq
from collections import Counter, defaultdict

import numpy as np
from shapely.geometry import Polygon, box as shapely_box
from shapely.ops import unary_union
from shapely.strtree import STRtree

from mjcf_prism import polygon_prism_for_surface, prism_as_box


AREA_EPSILON_M2 = 1e-6


def _plane(points):
    values = np.asarray(points, dtype=float)
    normal = np.zeros(3, dtype=float)
    for index, current in enumerate(values):
        normal += np.cross(current, values[(index + 1) % len(values)])
    length = float(np.linalg.norm(normal))
    if length <= 1e-12:
        return None
    return normal / length


def _basis(normal):
    reference = np.asarray(
        (1.0, 0.0, 0.0) if abs(float(normal[0])) < 0.9 else (0.0, 1.0, 0.0)
    )
    axis_u = np.cross(normal, reference)
    axis_u /= np.linalg.norm(axis_u)
    return axis_u, np.cross(normal, axis_u)


def _convex(polygon):
    return (
        not polygon.is_empty
        and polygon.is_valid
        and not polygon.interiors
        and polygon.convex_hull.area - polygon.area
        <= max(AREA_EPSILON_M2, polygon.area * 1e-9)
    )


def _candidate(piece):
    ring = np.asarray(piece["source_vertices"], dtype=float)
    normal = _plane(ring)
    if normal is None:
        return None
    return {
        "template": piece,
        "ring": ring,
        "normal": normal,
        "source_points": ring.copy(),
        "source_count": 1,
        "maximum_displacement_m": 0.0,
    }


def _try_merge(first, second, tolerance_m, normal_tolerance_deg):
    first_normal = first["normal"]
    second_normal = second["normal"]
    dot = float(first_normal @ second_normal)
    if dot < 0:
        second_normal = -second_normal
        dot = -dot
    dot = max(-1.0, min(1.0, dot))
    if math.degrees(math.acos(dot)) > normal_tolerance_deg:
        return None
    normal = first_normal + second_normal
    length = float(np.linalg.norm(normal))
    if length <= 1e-12:
        return None
    normal /= length
    source_points = np.vstack((first["source_points"], second["source_points"]))
    offset = float(np.mean(source_points @ normal))
    maximum_displacement = float(np.abs(source_points @ normal - offset).max())
    if maximum_displacement > tolerance_m + 1e-9:
        return None
    axis_u, axis_v = _basis(normal)

    def project(candidate):
        return Polygon([
            (float(point @ axis_u), float(point @ axis_v))
            for point in candidate["ring"]
        ])

    first_polygon, second_polygon = project(first), project(second)
    if not first_polygon.is_valid or not second_polygon.is_valid:
        return None
    # A tolerance in plane height must never become a tolerance in XY: gaps
    # and overlaps remain rejected rather than silently inventing geometry.
    if first_polygon.intersection(second_polygon).area > AREA_EPSILON_M2:
        return None
    if first_polygon.boundary.intersection(second_polygon.boundary).length <= AREA_EPSILON_M2:
        return None
    component = unary_union((first_polygon, second_polygon))
    if component.geom_type != "Polygon" or not _convex(component):
        return None
    ring = np.asarray([
        axis_u * x + axis_v * y + normal * offset
        for x, y in list(component.exterior.coords)[:-1]
    ])
    oriented = _plane(ring)
    if oriented is None:
        return None
    if float(oriented @ normal) < 0:
        ring = ring[::-1]
    return {
        "template": min(
            (first["template"], second["template"]), key=lambda item: item["id"]
        ),
        "ring": ring,
        "normal": normal,
        "source_points": source_points,
        "source_count": first["source_count"] + second["source_count"],
        "maximum_displacement_m": max(
            maximum_displacement,
            first["maximum_displacement_m"],
            second["maximum_displacement_m"],
        ),
    }


def _prism(candidate, thickness_m):
    return polygon_prism_for_surface(
        candidate["ring"],
        thickness_m,
        prefer_world_z=(candidate["template"]["surface_kind"] == "RoofSurface"),
    )


def _is_box(candidate, thickness_m):
    prism, _, _ = _prism(candidate, thickness_m)
    return prism_as_box(candidate["ring"], prism) is not None


def reduce_tolerant_planar(
    pieces,
    *,
    thickness_m,
    tolerance_m=0.05,
    normal_tolerance_deg=2.0,
    surface_kinds=("WallSurface",),
    preserve_box_primitives=True,
    progress_callback=None,
):
    """Merge adjacent near-coplanar convex faces under a bounded policy."""
    selected = set(surface_kinds)
    retained, candidates = [], []
    before_by_surface = Counter(piece["surface_kind"] for piece in pieces)
    boxes_before = sum(
        prism_as_box(piece["source_vertices"], piece["vertices"]) is not None
        for piece in pieces
    )
    for piece in pieces:
        candidate = _candidate(piece)
        if candidate is None or piece["surface_kind"] not in selected:
            retained.append(piece)
        else:
            candidates.append(candidate)

    groups = defaultdict(list)
    for candidate in candidates:
        template = candidate["template"]
        groups[(template["building_id"], template["surface_kind"])].append(candidate)

    results, merge_count, rejected_box_downgrade = [], 0, 0
    group_values = list(groups.values())
    for group_index, group in enumerate(group_values, start=1):
        current = list(group)
        margin = 2.0 * tolerance_m + 1e-6
        query_boxes = []
        for item in current:
            minimum, maximum = item["source_points"].min(0), item["source_points"].max(0)
            query_boxes.append(shapely_box(
                minimum[0] - margin, minimum[1] - margin,
                maximum[0] + margin, maximum[1] + margin,
            ))
        index = STRtree(query_boxes)
        edges = sorted({
            (left, int(right))
            for left in range(len(current))
            for right in index.query(query_boxes[left])
            if int(right) > left
        })
        parent = list(range(len(current)))
        neighbors = [set() for _ in current]
        for left, right in edges:
            neighbors[left].add(right)
            neighbors[right].add(left)

        def find(value):
            while parent[value] != value:
                parent[value] = parent[parent[value]]
                value = parent[value]
            return value

        pending = list(edges)
        heapq.heapify(pending)
        queued = set(edges)
        while pending:
            original_left, original_right = heapq.heappop(pending)
            queued.discard((original_left, original_right))
            left, right = find(original_left), find(original_right)
            if left == right:
                continue
            merged = _try_merge(
                current[left], current[right], tolerance_m, normal_tolerance_deg
            )
            if merged is None:
                continue
            if (
                preserve_box_primitives
                and (_is_box(current[left], thickness_m) or _is_box(current[right], thickness_m))
                and not _is_box(merged, thickness_m)
            ):
                rejected_box_downgrade += 1
                continue
            keep, discard = min(left, right), max(left, right)
            current[keep], parent[discard] = merged, keep
            merge_count += 1

            # Only comparisons touching the changed component can acquire a
            # different result. Requeue those instead of rescanning every edge.
            affected = neighbors[keep] | neighbors[discard]
            neighbors[keep] = set()
            neighbors[discard].clear()
            for candidate in affected:
                root = find(candidate)
                if root == keep:
                    continue
                neighbors[keep].add(root)
                neighbors[root].discard(discard)
                neighbors[root].add(keep)
                pair = (min(keep, root), max(keep, root))
                if pair not in queued:
                    heapq.heappush(pending, pair)
                    queued.add(pair)
        results.extend(current[value] for value in range(len(current)) if find(value) == value)
        if progress_callback is not None and (
            group_index == 1 or group_index == len(group_values) or group_index % 25 == 0
        ):
            progress_callback(group_index, len(group_values))

    reduced = list(retained)
    for candidate in results:
        prism, faces, extrusion_mode = _prism(candidate, thickness_m)
        piece = dict(candidate["template"])
        piece.update({
            "source_vertices": candidate["ring"].tolist(),
            "vertices": prism,
            "faces": faces,
            "_collider_origin": "tolerant-planar",
            "_tolerant_source_count": candidate["source_count"],
            "_tolerant_maximum_displacement_m": candidate["maximum_displacement_m"],
            "_extrusion_mode": extrusion_mode,
        })
        reduced.append(piece)
    reduced.sort(key=lambda piece: piece["id"])
    after_by_surface = Counter(piece["surface_kind"] for piece in reduced)
    boxes_after = sum(
        prism_as_box(piece["source_vertices"], piece["vertices"]) is not None
        for piece in reduced
    )
    stats = {
        "tolerance_m": tolerance_m,
        "normal_tolerance_deg": normal_tolerance_deg,
        "surface_kinds": sorted(selected),
        "preserve_box_primitives": preserve_box_primitives,
        "colliders_before": len(pieces),
        "colliders_after": len(reduced),
        "colliders_eliminated": len(pieces) - len(reduced),
        "reduction_ratio": (
            (len(pieces) - len(reduced)) / len(pieces) if pieces else 0.0
        ),
        "merge_count": merge_count,
        "maximum_displacement_m": max(
            (item["maximum_displacement_m"] for item in results), default=0.0
        ),
        "box_before": boxes_before,
        "box_after": boxes_after,
        "mesh_after": len(reduced) - boxes_after,
        "rejected_box_to_mesh_count": rejected_box_downgrade,
        "colliders_before_by_surface": dict(sorted(before_by_surface.items())),
        "colliders_after_by_surface": dict(sorted(after_by_surface.items())),
        "colliders_eliminated_by_surface": {
            kind: before_by_surface[kind] - after_by_surface[kind]
            for kind in sorted(before_by_surface)
        },
    }
    return reduced, stats
