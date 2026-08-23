#!/usr/bin/env python3
"""Classify selected PLATEAU buildings and select a Physics strategy.

The human-readable normative description of the rules and known omissions is
docs/building-physics-classification.md. Keep it synchronized with this module.

This stage assigns each selected building to one deterministic class using
CityGML semantic surfaces.  Downstream generation applies the strategy selected
here, while a color-coded GLB allows the decision to be reviewed against the
source geometry.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from xml.etree import ElementTree as ET

import numpy as np
import trimesh

sys.path.insert(0, str(Path(__file__).resolve().parent))
from citygml2glb import (  # noqa: E402
    GML_ID,
    NS,
    _append_lod1_part,
    _lod2_polygons,
    _polygon_rings,
    _three_coordinates,
    triangulate_rings,
)
from gml_lod1_extract import validate_epsg6697_contract  # noqa: E402
from world_frame import load_world_frame  # noqa: E402


CLASS_DEFINITIONS = {
    "P0": {
        "label": "LOD1 prism sufficient",
        "collision_strategy": "LOD1 OBB or footprint walls plus roof",
    },
    "P1": {
        "label": "top geometry requires LOD2",
        "collision_strategy": "thin convex prisms from LOD2 WallSurface and RoofSurface",
    },
    "P2": {
        "label": "height-dependent building profile",
        "collision_strategy": "thin convex prisms from the complete LOD2 wall/roof profile",
    },
    "P3": {
        "label": "void or overhang must be preserved",
        "collision_strategy": (
            "LOD2 Wall/Roof plus OuterCeiling/OuterFloor surface prisms "
            "preserving traversable space"
        ),
    },
}

CLASS_COLORS = {
    "P0": [104, 176, 112, 255],
    "P1": [244, 196, 74, 255],
    "P2": [238, 126, 48, 255],
    "P3": [157, 92, 204, 255],
}

SURFACE_TAGS = (
    "RoofSurface",
    "WallSurface",
    "GroundSurface",
    "OuterFloorSurface",
    "OuterCeilingSurface",
    "ClosureSurface",
)

B="{http://www.opengis.net/citygml/building/2.0}"
GML_POLYGON = "{http://www.opengis.net/gml}Polygon"
GML_POSLIST = "{http://www.opengis.net/gml}posList"


class ClassificationError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def classify_metrics(
    metrics: dict, *, roof_relief_m: float, profile_ratio: float, max_level: int = 3
) -> tuple[str, list[str]]:
    """Assign exactly one class, ordered from strongest geometric evidence."""
    if max_level not in range(4):
        raise ClassificationError("max_level must be an integer in [0, 3]")
    if max_level >= 3 and (
        metrics["outer_ceiling_polygons"] > 0 or metrics["outer_floor_polygons"] > 0
    ):
        reasons = []
        if metrics["outer_ceiling_polygons"]:
            reasons.append("OuterCeilingSurface indicates an underside or overhang")
        if metrics["outer_floor_polygons"]:
            reasons.append("OuterFloorSurface indicates an elevated exterior floor")
        return "P3", reasons

    if max_level >= 2 and metrics["building_part_count"] > 0:
        return "P2", ["BuildingPart changes the building profile by height"]
    if max_level >= 2 and metrics["ground_polygons"] > metrics["lod1_part_count"]:
        return "P2", ["multiple LOD2 ground surfaces indicate a compound footprint"]
    edge_limit = max(
        metrics["lod1_edge_count"] + 4,
        int(np.ceil(metrics["lod1_edge_count"] * profile_ratio)),
    )
    if max_level >= 2 and metrics["wall_polygons"] > edge_limit:
        return "P2", [
            f"LOD2 wall surface count {metrics['wall_polygons']} exceeds profile limit {edge_limit}"
        ]

    if max_level >= 1 and metrics["building_installation_count"] > 0:
        return "P1", ["BuildingInstallation requires geometry above the LOD1 roof"]
    if max_level >= 1 and metrics["roof_polygons"] > metrics["lod1_part_count"]:
        return "P1", ["multiple LOD2 roof surfaces are not represented by a flat LOD1 roof"]
    if max_level >= 1 and metrics["roof_relief_m"] > roof_relief_m:
        return "P1", [
            f"roof relief {metrics['roof_relief_m']:.3f} m exceeds {roof_relief_m:.3f} m"
        ]
    return "P0", ["LOD2 semantics show no material departure from the LOD1 prism"]


def _selection(path: Path):
    data = json.loads(path.read_text(encoding="utf-8"))
    by_source = defaultdict(lambda: defaultdict(list))
    zmins = []
    for part in data.get("polygons", []):
        source = Path(part["source_gml"]).resolve()
        building_id = part["id"].split("__part_", 1)[0]
        by_source[source][building_id].append(part)
        zmins.append(float(part["zmin"]))
    if not zmins:
        raise ClassificationError("selection contains no buildings")
    origin = data.get("origin") or {}
    return by_source, float(origin["lat"]), float(origin["lon"]), min(zmins)


def _semantic_metrics(building, parts: list[dict]) -> dict:
    counts = {}
    z_values = defaultdict(list)
    for tag in SURFACE_TAGS:
        surfaces = list(building.iter(B + tag)) if building is not None else []
        polygons = [polygon for surface in surfaces for polygon in surface.iter(GML_POLYGON)]
        counts[tag] = len(polygons)
        for polygon in polygons:
            for position in polygon.iter(GML_POSLIST):
                if not position.text:
                    continue
                values = [float(value) for value in position.text.split()]
                z_values[tag].extend(values[2::3])

    roof_z = z_values["RoofSurface"]
    return {
        "lod1_part_count": len(parts),
        "lod1_edge_count": sum(
            len(part.get("vertices", []))
            + sum(len(ring) for ring in part.get("interior_rings", []))
            for part in parts
        ),
        "building_part_count": len(list(building.iter(B + "BuildingPart"))) if building is not None else 0,
        "building_installation_count": len(
            list(building.iter(B + "BuildingInstallation"))
        ) if building is not None else 0,
        "roof_polygons": counts["RoofSurface"],
        "wall_polygons": counts["WallSurface"],
        "ground_polygons": counts["GroundSurface"],
        "outer_floor_polygons": counts["OuterFloorSurface"],
        "outer_ceiling_polygons": counts["OuterCeilingSurface"],
        "closure_polygons": counts["ClosureSurface"],
        "roof_relief_m": (max(roof_z) - min(roof_z)) if roof_z else 0.0,
        "lod1_z_min_m": min(float(part["zmin"]) for part in parts),
        "lod1_z_max_m": max(float(part["zmax"]) for part in parts),
    }


def _append_debug_geometry(batches, class_id, building, parts, center_lat, center_lon, z_offset):
    emitted = 0
    for polygon in _lod2_polygons(building) if building is not None else []:
        parsed = _polygon_rings(polygon)
        if not parsed:
            continue
        rings = [_three_coordinates(ring, center_lat, center_lon, z_offset) for _, ring in parsed]
        vertices, faces = triangulate_rings(rings)
        batch = batches[class_id]
        offset = len(batch["vertices"])
        batch["vertices"].extend(vertices.tolist())
        batch["faces"].extend((face + offset).tolist() for face in faces)
        emitted += 1
    if not emitted:
        temporary = defaultdict(lambda: {"vertices": [], "faces": [], "uv": []})
        for part in parts:
            _append_lod1_part(temporary, part, z_offset)
        batch = batches[class_id]
        for value in temporary.values():
            offset = len(batch["vertices"])
            batch["vertices"].extend(value["vertices"])
            batch["faces"].extend((np.asarray(face) + offset).tolist() for face in value["faces"])


def classify_selection(
    selection_path: Path,
    output_path: Path,
    debug_glb_path: Path,
    *,
    altitude_offset_m: float | None = None,
    roof_relief_m: float = 0.5,
    profile_ratio: float = 1.5,
    max_level: int = 3,
) -> dict:
    selected, center_lat, center_lon, selection_z_offset = _selection(selection_path)
    z_offset = selection_z_offset if altitude_offset_m is None else float(altitude_offset_m)
    records = []
    batches = defaultdict(lambda: {"vertices": [], "faces": []})

    for source, ids in sorted(selected.items(), key=lambda item: str(item[0])):
        root = ET.parse(source).getroot()
        validate_epsg6697_contract(root, source)
        indexed = {element.get(GML_ID): element for element in root.findall(".//bldg:Building", NS)}
        for building_id, parts in sorted(ids.items()):
            building = indexed.get(building_id)
            metrics = _semantic_metrics(building, parts)
            class_id, reasons = classify_metrics(
                metrics, roof_relief_m=roof_relief_m, profile_ratio=profile_ratio,
                max_level=max_level,
            )
            records.append({
                "building_id": building_id,
                "source_gml": str(source),
                "class": class_id,
                "label": CLASS_DEFINITIONS[class_id]["label"],
                "reasons": reasons,
                "evidence": metrics,
                "collision_strategy": CLASS_DEFINITIONS[class_id]["collision_strategy"],
            })
            _append_debug_geometry(
                batches, class_id, building, parts, center_lat, center_lon, z_offset
            )

    scene = trimesh.Scene()
    for class_id in CLASS_DEFINITIONS:
        batch = batches[class_id]
        if not batch["vertices"] or not batch["faces"]:
            continue
        material = trimesh.visual.material.PBRMaterial(
            name=f"building-physics-{class_id}",
            baseColorFactor=CLASS_COLORS[class_id],
            metallicFactor=0.0,
            roughnessFactor=1.0,
            doubleSided=True,
        )
        mesh = trimesh.Trimesh(
            vertices=np.asarray(batch["vertices"], dtype=np.float32),
            faces=np.asarray(batch["faces"], dtype=np.int64),
            visual=trimesh.visual.texture.TextureVisuals(material=material),
            process=False,
        )
        scene.add_geometry(mesh, node_name=class_id, geom_name=class_id)
    if not scene.geometry:
        raise ClassificationError("classification debug GLB contains no geometry")
    debug_glb_path.parent.mkdir(parents=True, exist_ok=True)
    debug_glb_path.write_bytes(scene.export(file_type="glb"))

    counts = Counter(record["class"] for record in records)
    report = {
        "schema_version": 1,
        "status": "classified",
        "policy": "lod1-lod2-building-physics-classification-v1",
        "max_level": max_level,
        "precedence": [f"P{level}" for level in range(max_level, -1, -1)],
        "classification_only": True,
        "physics_modified": False,
        "source_crs": "EPSG:6697",
        "coordinate_system": "query-centered local ENU; debug GLB X=East,Y=Up,Z=-North",
        "thresholds": {
            "roof_relief_m": roof_relief_m,
            "wall_to_lod1_edge_ratio": profile_ratio,
        },
        "classes": CLASS_DEFINITIONS,
        "counts": {class_id: counts.get(class_id, 0) for class_id in CLASS_DEFINITIONS},
        "building_count": len(records),
        "buildings": records,
        "debug_glb": {
            "path": str(debug_glb_path),
            "bytes": debug_glb_path.stat().st_size,
            "sha256": sha256_file(debug_glb_path),
            "colors_rgba": CLASS_COLORS,
        },
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--debug-glb", type=Path, required=True)
    parser.add_argument("--world-frame", type=Path)
    parser.add_argument("--roof-relief-m", type=float, default=0.5)
    parser.add_argument("--profile-ratio", type=float, default=1.5)
    parser.add_argument("--max-level", type=int, choices=range(4), default=3)
    args = parser.parse_args()
    try:
        altitude_offset = None
        if args.world_frame:
            altitude_offset = load_world_frame(args.world_frame)["origin"]["altitude_offset_m"]
        report = classify_selection(
            args.selection,
            args.out,
            args.debug_glb,
            altitude_offset_m=altitude_offset,
            roof_relief_m=args.roof_relief_m,
            profile_ratio=args.profile_ratio,
            max_level=args.max_level,
        )
        print(f"Building Physics classes: {report['counts']}")
        print(f"Classification: {args.out}")
        print(f"Debug GLB: {args.debug_glb}")
        return 0
    except (ClassificationError, ValueError, OSError, ET.ParseError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
