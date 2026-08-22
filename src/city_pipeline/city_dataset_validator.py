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


def _lod_resolution(preferred_lod: str, preferred_count: int,
                    fallback_lod: str, fallback_count: int) -> dict:
    if preferred_count > 0 and fallback_count > 0:
        effective_lod = "mixed"
    elif preferred_count > 0:
        effective_lod = preferred_lod
    elif fallback_count > 0:
        effective_lod = fallback_lod
    else:
        effective_lod = None
    return {
        "preferred_lod": preferred_lod,
        "effective_lod": effective_lod,
        "preferred_count": preferred_count,
        "fallback_lod": fallback_lod,
        "fallback_count": fallback_count,
        "fallback_used": fallback_count > 0,
        "preferred_lod_available": preferred_count > 0,
    }


def validate_dataset(
    terrain_receipt_path: Path,
    buildings_receipt_path: Path,
    roads_receipt_path: Path,
    markings_receipt_path: Path,
) -> dict:
    terrain = _load(terrain_receipt_path, "terrain")
    buildings = _load(buildings_receipt_path, "buildings")
    roads = _load(roads_receipt_path, "roads")
    markings = _load(markings_receipt_path, "road markings")

    building_lods = buildings.get("buildings") or {}
    road_lods = roads.get("lod_polygon_counts") or {}
    marking_available = markings.get("status") == "available" and int(
        markings.get("polygon_count", 0)
    ) > 0
    building_resolution = _lod_resolution(
        "LOD2", int(building_lods.get("lod2", 0)),
        "LOD1", int(building_lods.get("lod1_fallback", 0)),
    )
    road_resolution = _lod_resolution(
        "LOD3", int(road_lods.get("lod3", 0)),
        "LOD2", int(road_lods.get("lod2_fallback", 0)),
    )
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
            },
            "road_surfaces": {
                "status": "available",
                "lod3": int(road_lods.get("lod3", 0)),
                "lod2_fallback": int(road_lods.get("lod2_fallback", 0)),
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
        },
        "capabilities": {
            "terrain_collision": True,
            "building_collision": True,
            "road_surface_visualization": True,
            "road_marking_visualization": marking_available,
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
                [] if marking_available else ["road_markings"]
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
    grid = terrain["grid"]
    def lod_line(
        label: str, component: dict, preferred_lod: str, preferred_key: str,
        fallback_lod: str, fallback_key: str,
    ) -> str:
        resolution = component.get("lod_resolution") or _lod_resolution(
            preferred_lod, int(component.get(preferred_key, 0)),
            fallback_lod, int(component.get(fallback_key, 0)),
        )
        preferred = resolution["preferred_lod"]
        fallback = resolution["fallback_lod"]
        preferred_count = resolution["preferred_count"]
        fallback_count = resolution["fallback_count"]
        if preferred_count == 0 and fallback_count > 0:
            return f"{label:<14}: {fallback} ({preferred} not available, fallback)"
        if preferred_count > 0 and fallback_count > 0:
            return (
                f"{label:<14}: {preferred} ({preferred_count}), "
                f"{fallback} fallback ({fallback_count})"
            )
        if preferred_count > 0:
            return f"{label:<14}: {preferred} ({preferred_count})"
        return f"{label:<14}: NOT AVAILABLE"

    lines = [
        f"{'Terrain':<14}: DEM hfield ({grid['rows']} x {grid['columns']})",
        lod_line("Buildings", buildings, "LOD2", "lod2", "LOD1", "lod1_fallback"),
        lod_line("Road surfaces", roads, "LOD3", "lod3", "LOD2", "lod2_fallback"),
    ]
    if markings["status"] == "available":
        count = sum(int(value) for value in markings["feature_counts"].values())
        lines.append(f"{'Road markings':<14}: LOD3 ({count} features)")
    else:
        lines.append(
            f"{'Road markings':<14}: NOT AVAILABLE "
            "(LOD3 absent; no surface markings, no inference)"
        )
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
