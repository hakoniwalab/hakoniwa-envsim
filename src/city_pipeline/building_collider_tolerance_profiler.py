#!/usr/bin/env python3
"""Profile approximate coplanar Collider reduction without writing Physics output."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from shapely.geometry import Polygon, box as shapely_box
from shapely.ops import unary_union
from shapely.strtree import STRtree

from building_lod2_colliders import (
    UNION_AREA_TOLERANCE_M2,
    _is_convex_polygon,
    _oriented_plane,
    _plane_basis,
    polygon_prism_for_surface,
    prepare_classes_geometry,
)
from mjcf_prism import prism_as_box


SURFACE_KINDS = (
    "WallSurface",
    "RoofSurface",
    "OuterCeilingSurface",
    "OuterFloorSurface",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _candidate(piece: dict) -> dict | None:
    ring = np.asarray(piece["source_vertices"], dtype=float)
    plane = _oriented_plane(ring)
    if plane is None:
        return None
    normal, _ = plane
    return {
        "building_id": piece["building_id"],
        "surface_kind": piece["surface_kind"],
        "ring": ring,
        "normal": normal,
        "source_points": ring.copy(),
        "source_count": 1,
    }


def _bbox_may_touch(first: dict, second: dict, tolerance_m: float) -> bool:
    first_min = first["source_points"].min(axis=0)
    first_max = first["source_points"].max(axis=0)
    second_min = second["source_points"].min(axis=0)
    second_max = second["source_points"].max(axis=0)
    margin = 2.0 * tolerance_m + 1e-6
    return bool(np.all(first_min <= second_max + margin) and np.all(
        second_min <= first_max + margin
    ))


def _try_merge(first: dict, second: dict, tolerance_m: float, normal_tolerance_deg: float):
    if not _bbox_may_touch(first, second, tolerance_m):
        return None
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
    displacements = np.abs(source_points @ normal - offset)
    maximum_displacement = float(displacements.max())
    if maximum_displacement > tolerance_m + 1e-9:
        return None

    axis_u, axis_v = _plane_basis(normal)

    def projected_polygon(candidate):
        return Polygon([
            (float(point @ axis_u), float(point @ axis_v))
            for point in candidate["ring"]
        ])

    first_polygon = projected_polygon(first)
    second_polygon = projected_polygon(second)
    if not first_polygon.is_valid or not second_polygon.is_valid:
        return None
    if first_polygon.intersection(second_polygon).area > UNION_AREA_TOLERANCE_M2:
        return None
    if first_polygon.boundary.intersection(
        second_polygon.boundary
    ).length <= UNION_AREA_TOLERANCE_M2:
        return None
    component = unary_union((first_polygon, second_polygon))
    if component.geom_type != "Polygon" or component.interiors:
        return None
    if not _is_convex_polygon(component):
        return None
    coordinates = list(component.exterior.coords)[:-1]
    ring = np.asarray([
        axis_u * x + axis_v * y + normal * offset for x, y in coordinates
    ])
    oriented = _oriented_plane(ring)
    if oriented is None:
        return None
    if float(oriented[0] @ normal) < 0:
        ring = ring[::-1]
    return {
        "building_id": first["building_id"],
        "surface_kind": first["surface_kind"],
        "ring": ring,
        "normal": normal,
        "source_points": source_points,
        "source_count": first["source_count"] + second["source_count"],
        "maximum_displacement_m": maximum_displacement,
    }


def _is_box(candidate: dict, thickness_m: float) -> bool:
    ring = candidate["ring"]
    prism, _, _ = polygon_prism_for_surface(
        ring,
        thickness_m,
        prefer_world_z=(candidate["surface_kind"] == "RoofSurface"),
    )
    return prism_as_box(ring, prism) is not None


def profile_pieces(
    pieces: list[dict], *, tolerance_m: float, normal_tolerance_deg: float,
    thickness_m: float, surface_kinds=SURFACE_KINDS,
    preserve_box_primitives: bool = False,
) -> dict:
    selected_kinds = set(surface_kinds)
    candidates = []
    retained_count = 0
    retained_by_surface = Counter()
    before_by_surface = Counter(piece["surface_kind"] for piece in pieces)
    baseline_boxes = 0
    for piece in pieces:
        candidate = _candidate(piece)
        if candidate is None or piece["surface_kind"] not in selected_kinds:
            retained_count += 1
            retained_by_surface[piece["surface_kind"]] += 1
            baseline_boxes += int(
                prism_as_box(piece["source_vertices"], piece["vertices"]) is not None
            )
            continue
        candidates.append(candidate)
        baseline_boxes += int(
            prism_as_box(piece["source_vertices"], piece["vertices"]) is not None
        )

    groups = defaultdict(list)
    for candidate in candidates:
        groups[(candidate["building_id"], candidate["surface_kind"])].append(candidate)

    result_candidates = []
    merge_count = 0
    for group in groups.values():
        current = list(group)
        margin = 2.0 * tolerance_m + 1e-6
        query_boxes = []
        for item in current:
            minimum = item["source_points"].min(axis=0)
            maximum = item["source_points"].max(axis=0)
            query_boxes.append(shapely_box(
                minimum[0] - margin, minimum[1] - margin,
                maximum[0] + margin, maximum[1] + margin,
            ))
        spatial_index = STRtree(query_boxes)
        edges = sorted({
            (left, int(right))
            for left in range(len(current))
            for right in spatial_index.query(query_boxes[left])
            if int(right) > left
        })
        parent = list(range(len(current)))

        def find(index):
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        changed = True
        while changed:
            changed = False
            for original_left, original_right in edges:
                left = find(original_left)
                right = find(original_right)
                if left == right:
                    continue
                merged = _try_merge(
                    current[left], current[right], tolerance_m,
                    normal_tolerance_deg,
                )
                if merged is None:
                    continue
                if (
                    preserve_box_primitives
                    and (_is_box(current[left], thickness_m)
                         or _is_box(current[right], thickness_m))
                    and not _is_box(merged, thickness_m)
                ):
                    continue
                keep, discard = min(left, right), max(left, right)
                current[keep] = merged
                parent[discard] = keep
                merge_count += 1
                changed = True
        result_candidates.extend(
            current[index] for index in range(len(current)) if find(index) == index
        )

    approximated = [item for item in result_candidates if item["source_count"] > 1]
    displacement_values = [
        float(item.get("maximum_displacement_m", 0.0)) for item in approximated
    ]
    after_count = retained_count + len(result_candidates)
    box_count = baseline_boxes - sum(
        _is_box(item, thickness_m) for item in candidates
    ) + sum(_is_box(item, thickness_m) for item in result_candidates)
    by_surface = retained_by_surface + Counter(
        item["surface_kind"] for item in result_candidates
    )
    approximated_by_surface = Counter(item["surface_kind"] for item in approximated)
    return {
        "tolerance_m": tolerance_m,
        "normal_tolerance_deg": normal_tolerance_deg,
        "preserve_box_primitives": preserve_box_primitives,
        "colliders_before": len(pieces),
        "colliders_after": after_count,
        "colliders_eliminated": len(pieces) - after_count,
        "reduction_ratio": (
            (len(pieces) - after_count) / len(pieces) if pieces else 0.0
        ),
        "merge_count": merge_count,
        "approximated_collider_count": len(approximated),
        "maximum_displacement_m": max(displacement_values, default=0.0),
        "box_before": baseline_boxes,
        "box_after": int(box_count),
        "mesh_after": int(after_count - box_count),
        "colliders_before_by_surface": dict(sorted(before_by_surface.items())),
        "colliders_after_by_surface": dict(sorted(by_surface.items())),
        "colliders_eliminated_by_surface": {
            surface_kind: before_by_surface[surface_kind] - by_surface[surface_kind]
            for surface_kind in sorted(before_by_surface)
        },
        "approximated_by_surface": dict(sorted(approximated_by_surface.items())),
    }


def build_report(
    prepared, *, tolerances_m, normal_tolerance_deg: float, thickness_m: float,
    surface_kinds=SURFACE_KINDS, preserve_box_primitives: bool = False,
) -> dict:
    class_reports = {}
    for class_id, geometry in prepared.items():
        class_reports[class_id] = [
            profile_pieces(
                geometry.pieces, tolerance_m=tolerance,
                normal_tolerance_deg=normal_tolerance_deg,
                thickness_m=thickness_m, surface_kinds=surface_kinds,
                preserve_box_primitives=preserve_box_primitives,
            )
            for tolerance in tolerances_m
        ]
    totals = []
    for index, tolerance in enumerate(tolerances_m):
        rows = [class_reports[class_id][index] for class_id in class_reports]
        before = sum(row["colliders_before"] for row in rows)
        after = sum(row["colliders_after"] for row in rows)
        totals.append({
            "tolerance_m": tolerance,
            "colliders_before": before,
            "colliders_after": after,
            "colliders_eliminated": before - after,
            "reduction_ratio": (before - after) / before if before else 0.0,
            "box_before": sum(row["box_before"] for row in rows),
            "box_after": sum(row["box_after"] for row in rows),
            "mesh_after": sum(row["mesh_after"] for row in rows),
            "maximum_displacement_m": max(
                (row["maximum_displacement_m"] for row in rows), default=0.0
            ),
        })
    return {
        "schema_version": 1,
        "status": "analysis_only",
        "baseline_reduction": "convex-decompose",
        "derived_geometry_written": False,
        "normal_tolerance_deg": normal_tolerance_deg,
        "preserve_box_primitives": preserve_box_primitives,
        "surface_kinds": list(surface_kinds),
        "tolerances_m": list(tolerances_m),
        "totals": totals,
        "by_class": class_reports,
        "limitations": [
            "greedy pairwise merging does not guarantee the minimum collider count",
            "reported counts are estimates; no MJCF or GLB is written",
            "runtime speedup is not inferred from collider counts",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--classification", type=Path, required=True)
    parser.add_argument("--world-frame", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--tolerances", nargs="+", type=float,
        default=(0.0, 0.05, 0.1, 0.2, 0.5),
    )
    parser.add_argument("--normal-tolerance-deg", type=float, default=2.0)
    parser.add_argument("--thickness", type=float, default=0.02)
    parser.add_argument("--surface-kinds", nargs="+", choices=SURFACE_KINDS, default=SURFACE_KINDS)
    parser.add_argument(
        "--preserve-box-primitives", action="store_true",
        help="Reject a merge that would replace any MuJoCo box with a mesh",
    )
    args = parser.parse_args()
    if not args.tolerances or any(value < 0 or value > 0.5 for value in args.tolerances):
        parser.error("--tolerances must contain values in [0, 0.5]")
    if args.normal_tolerance_deg < 0 or args.normal_tolerance_deg > 10:
        parser.error("--normal-tolerance-deg must be in [0, 10]")
    prepared = prepare_classes_geometry(
        args.selection, args.classification, args.world_frame,
        class_ids=("P1", "P2", "P3"), roof_thickness_m=args.thickness,
        collider_reduction="convex-decompose",
    )
    report = build_report(
        prepared, tolerances_m=tuple(sorted(set(args.tolerances))),
        normal_tolerance_deg=args.normal_tolerance_deg,
        thickness_m=args.thickness, surface_kinds=args.surface_kinds,
        preserve_box_primitives=args.preserve_box_primitives,
    )
    report["inputs"] = {
        name: {"path": str(path.resolve()), "sha256": _sha256(path)}
        for name, path in (
            ("selection", args.selection),
            ("classification", args.classification),
            ("world_frame", args.world_frame),
        )
    }
    report["profiled_classes"] = ["P1", "P2", "P3"]
    report["excluded_classes"] = ["P0"]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[OK] Collider tolerance profile: {args.out}")
    for row in report["totals"]:
        print(
            f"  tolerance={row['tolerance_m']:.3f}m "
            f"colliders={row['colliders_before']}->{row['colliders_after']} "
            f"reduction={row['reduction_ratio']:.1%} "
            f"boxes={row['box_before']}->{row['box_after']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
