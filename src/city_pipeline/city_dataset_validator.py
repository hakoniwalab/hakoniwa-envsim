#!/usr/bin/env python3
"""Summarize PLATEAU City World dataset capabilities and LOD fallbacks."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


class DatasetValidationError(RuntimeError):
    pass


def _load(path: Path, label: str) -> dict:
    if not path.is_file():
        raise DatasetValidationError(f"{label} receipt not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1:
        raise DatasetValidationError(f"unsupported {label} receipt schema: {path}")
    return data


def _lod_resolution(levels: list[tuple[str, int]]) -> dict:
    available = [(lod, count) for lod, count in levels if count > 0]
    effective_lod = (
        available[0][0] if len(available) == 1
        else "mixed" if available
        else None
    )
    preferred_lod, preferred_count = levels[0]
    fallback_levels = levels[1:]
    legacy_fallback_lod, legacy_fallback_count = (
        fallback_levels[0] if fallback_levels else (None, 0)
    )
    return {
        "preferred_lod": preferred_lod,
        "effective_lod": effective_lod,
        "preferred_count": preferred_count,
        # Preserve the schema-v1 two-level view for existing consumers.  The
        # ordered levels list is the authoritative representation when more
        # than one fallback exists.
        "fallback_lod": legacy_fallback_lod,
        "fallback_count": legacy_fallback_count,
        "levels": [
            {"lod": lod, "count": count, "role": "preferred" if index == 0 else "fallback"}
            for index, (lod, count) in enumerate(levels)
        ],
        "fallback_used": any(count > 0 for _, count in fallback_levels),
        "preferred_lod_available": preferred_count > 0,
    }


def validate_dataset(
    terrain_receipt_path: Path,
    buildings_receipt_path: Path,
    roads_receipt_path: Path,
    markings_receipt_path: Path,
    bridges_receipt_path: Path | None = None,
    bridge_physics_receipt_path: Path | None = None,
    building_physics_classification_path: Path | None = None,
    building_physics_application_path: Path | None = None,
) -> dict:
    terrain = _load(terrain_receipt_path, "terrain")
    buildings = _load(buildings_receipt_path, "buildings")
    roads = _load(roads_receipt_path, "roads")
    markings = _load(markings_receipt_path, "road markings")
    bridges = (
        _load(bridges_receipt_path, "bridges")
        if bridges_receipt_path is not None else {
            "status": "not_available", "bridge_count": 0,
            "reason": "bridge conversion was not requested",
        }
    )
    bridge_physics = (
        _load(bridge_physics_receipt_path, "bridge physics")
        if bridge_physics_receipt_path is not None else {"status": "not_generated"}
    )
    building_physics = (
        _load(building_physics_classification_path, "building physics classification")
        if building_physics_classification_path is not None else None
    )
    building_physics_application = (
        _load(building_physics_application_path, "building physics application")
        if building_physics_application_path is not None else None
    )

    building_lods = buildings.get("buildings") or {}
    road_lods = roads.get("lod_polygon_counts") or {}
    marking_available = markings.get("status") == "available" and int(
        markings.get("polygon_count", 0)
    ) > 0
    bridge_available = bridges.get("status") == "available" and int(
        bridges.get("polygon_count", 0)
    ) > 0
    bridge_collision_available = (
        bridge_physics.get("status") == "available"
        and int(bridge_physics.get("physics_geom_count", 0)) > 0
    )
    building_resolution = _lod_resolution([
        ("LOD2", int(building_lods.get("lod2", 0))),
        ("LOD1", int(building_lods.get("lod1_fallback", 0))),
    ])
    road_resolution = _lod_resolution([
        ("LOD3", int(road_lods.get("lod3", 0))),
        ("LOD2", int(road_lods.get("lod2_fallback", 0))),
        ("LOD1", int(road_lods.get("lod1_fallback", 0))),
    ])
    report = {
        "schema_version": 1,
        "status": "ready",
        "components": {
            "terrain": {
                "status": "available",
                "source": "PLATEAU DEM",
                "grid": {"rows": terrain.get("nrow"), "columns": terrain.get("ncol")},
            },
            "buildings": {
                "status": "available",
                "lod2": int(building_lods.get("lod2", 0)),
                "lod1_fallback": int(building_lods.get("lod1_fallback", 0)),
                "lod_resolution": building_resolution,
                "skipped_building_count": int(
                    (buildings.get("extraction") or {}).get("skipped_building_count", 0)
                ),
                "extraction_issues": (buildings.get("extraction") or {}).get("issues", []),
                "physics_classification": (
                    {
                        "status": building_physics.get("status"),
                        "classification_only": building_physics.get("classification_only", True),
                        "physics_modified": building_physics.get("physics_modified", False),
                        "counts": building_physics.get("counts", {}),
                        "policy": building_physics.get("policy"),
                    }
                    if building_physics is not None else None
                ),
                "physics_application": (
                    {
                        "status": building_physics_application.get("status"),
                        "max_physics_level": building_physics_application.get(
                            "max_physics_level", 3
                        ),
                        "class_status": building_physics_application.get("class_status", {}),
                        "collider_geom_counts": building_physics_application.get(
                            "collider_geom_counts", {}
                        ),
                        "physics_modified_by_classification": building_physics_application.get(
                            "physics_modified_by_classification", False
                        ),
                        "policy": building_physics_application.get("policy"),
                    }
                    if building_physics_application is not None else None
                ),
            },
            "road_surfaces": {
                "status": "available",
                "lod3": int(road_lods.get("lod3", 0)),
                "lod2_fallback": int(road_lods.get("lod2_fallback", 0)),
                "lod1_fallback": int(road_lods.get("lod1_fallback", 0)),
                "surface_polygon_counts": roads.get("surface_polygon_counts", {}),
                "lod_resolution": road_resolution,
            },
            "road_markings": {
                "status": "available" if marking_available else "not_available",
                "source_lod": "LOD3" if marking_available else None,
                "feature_counts": markings.get("feature_counts", {}),
                "reason": markings.get("reason") if not marking_available else None,
                "preferred_lod": "LOD3",
                "effective_lod": "LOD3" if marking_available else None,
                "fallback_used": False,
                "missing_behavior": None if marking_available else "omitted_without_inference",
            },
            "bridges": {
                "status": "available" if bridge_available else "not_available",
                "source_lod": "LOD3" if bridge_available else None,
                "bridge_count": int(bridges.get("bridge_count", 0)),
                "polygon_count": int(bridges.get("polygon_count", 0)),
                "rejected_polygon_count": int(bridges.get("rejected_polygon_count", 0)),
                "reason": bridges.get("reason") if not bridge_available else None,
                "geometry_policy": bridges.get("geometry_policy"),
                "physics_collision": "available" if bridge_collision_available else "not_generated",
                "physics_geom_count": int(bridge_physics.get("physics_geom_count", 0)),
                "physics_reason": bridge_physics.get("reason") if not bridge_collision_available else None,
            },
        },
        "capabilities": {
            "terrain_collision": True,
            "building_collision": True,
            "road_surface_visualization": True,
            "road_marking_visualization": marking_available,
            "bridge_visualization": bridge_available,
            "bridge_collision": bridge_collision_available,
        },
        "policy": {
            "missing_lod3_road_markings": "omit without inference",
            "fallbacks_are_reported": True,
        },
        "summary": {
            "fallback_used": (
                building_resolution["fallback_used"] or road_resolution["fallback_used"]
            ),
            "unavailable_components": (
                ([] if marking_available else ["road_markings"])
                + ([] if bridge_available else ["bridges"])
            ),
            "warnings": (
                ["invalid LOD1 buildings were skipped with provenance"]
                if int((buildings.get("extraction") or {}).get("skipped_building_count", 0))
                else []
            ),
        },
    }
    return report


def format_report(report: dict) -> list[str]:
    components = report["components"]
    terrain = components["terrain"]
    buildings = components["buildings"]
    roads = components["road_surfaces"]
    markings = components["road_markings"]
    bridges = components.get("bridges", {"status": "not_available"})
    grid = terrain["grid"]
    def lod_line(label: str, component: dict, level_keys: list[tuple[str, str]]) -> str:
        resolution = component.get("lod_resolution") or _lod_resolution([
            (lod, int(component.get(key, 0))) for lod, key in level_keys
        ])
        levels = resolution.get("levels") or [
            {
                "lod": resolution["preferred_lod"],
                "count": resolution["preferred_count"],
                "role": "preferred",
            },
            {
                "lod": resolution["fallback_lod"],
                "count": resolution["fallback_count"],
                "role": "fallback",
            },
        ]
        available = [level for level in levels if int(level["count"]) > 0]
        if not available:
            return f"{label:<14}: NOT AVAILABLE"
        preferred = levels[0]
        if int(preferred["count"]) == 0 and len(available) == 1:
            chosen = available[0]
            unavailable = "/".join(
                level["lod"] for level in levels[:levels.index(chosen)]
            )
            return (
                f"{label:<14}: {chosen['lod']} "
                f"({unavailable} not available, fallback)"
            )
        if len(available) > 1:
            values = []
            for level in available:
                suffix = "" if level["role"] == "preferred" else " fallback"
                values.append(f"{level['lod']}{suffix} ({level['count']})")
            return f"{label:<14}: " + ", ".join(values)
        if int(preferred["count"]) > 0:
            return f"{label:<14}: {preferred['lod']} ({preferred['count']})"
        return f"{label:<14}: NOT AVAILABLE"

    lines = [
        f"{'Terrain':<14}: DEM hfield ({grid['rows']} x {grid['columns']})",
        lod_line("Buildings", buildings, [("LOD2", "lod2"), ("LOD1", "lod1_fallback")]),
        lod_line("Road surfaces", roads, [
            ("LOD3", "lod3"), ("LOD2", "lod2_fallback"), ("LOD1", "lod1_fallback")
        ]),
    ]
    classification = buildings.get("physics_classification")
    if classification:
        counts = classification.get("counts", {})
        lines.append(
            f"{'Building physics':<14}: classified "
            + ", ".join(f"{key}={int(counts.get(key, 0))}" for key in ("P0", "P1", "P2", "P3"))
        )
    application = buildings.get("physics_application")
    if application:
        statuses = application.get("class_status", {})
        lines.append(
            f"{'Building collider':<14}: "
            + ", ".join(f"{key}={statuses.get(key, 'unknown')}" for key in ("P0", "P1", "P2", "P3"))
        )
    if markings["status"] == "available":
        count = sum(int(value) for value in markings["feature_counts"].values())
        lines.append(f"{'Road markings':<14}: LOD3 ({count} features)")
    else:
        lines.append(
            f"{'Road markings':<14}: NOT AVAILABLE "
            "(LOD3 absent; no surface markings, no inference)"
        )
    if bridges["status"] == "available":
        suffix = (
            f"visual + collision {bridges['physics_geom_count']} geoms"
            if bridges.get("physics_collision") == "available" else "visual only"
        )
        lines.append(
            f"{'Bridges':<14}: LOD3 ({bridges['bridge_count']} bridges, "
            f"{bridges['polygon_count']} polygons; {suffix})"
        )
    else:
        lines.append(f"{'Bridges':<14}: NOT AVAILABLE (no inferred geometry)")
    return lines


def write_report(report: dict, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    text_path = output_path.with_suffix(".txt")
    text_path.write_text("\n".join(format_report(report)) + "\n", encoding="utf-8")
    return text_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input", type=Path,
        help="display an existing Dataset Validator JSON without regenerating it",
    )
    parser.add_argument("--terrain-receipt", type=Path)
    parser.add_argument("--buildings-receipt", type=Path)
    parser.add_argument("--roads-receipt", type=Path)
    parser.add_argument("--markings-receipt", type=Path)
    parser.add_argument("--bridges-receipt", type=Path)
    parser.add_argument("--bridge-physics-receipt", type=Path)
    parser.add_argument("--building-physics-classification", type=Path)
    parser.add_argument("--building-physics-application", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.input:
        report = _load(args.input, "dataset validation")
        output_path = None
    else:
        required = {
            "--terrain-receipt": args.terrain_receipt,
            "--buildings-receipt": args.buildings_receipt,
            "--roads-receipt": args.roads_receipt,
            "--markings-receipt": args.markings_receipt,
            "--out": args.out,
        }
        missing = [name for name, value in required.items() if value is None]
        if missing:
            parser.error("required when --input is omitted: " + ", ".join(missing))
        report = validate_dataset(
            args.terrain_receipt, args.buildings_receipt,
            args.roads_receipt, args.markings_receipt,
            args.bridges_receipt,
            args.bridge_physics_receipt,
            args.building_physics_classification,
            args.building_physics_application,
        )
        output_path = args.out
        text_path = write_report(report, output_path)
    for line in format_report(report):
        print(line)
    if output_path is not None:
        print(f"OK: dataset validation: {output_path}")
        print(f"OK: dataset validation text: {text_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
