"""Class-specific LOD2 building collider helpers.

P1 and P2 replace a legacy LOD1 approximation with thin convex prisms derived
from source LOD2 WallSurface and RoofSurface triangles. P3 includes the same
outer profile plus source OuterCeilingSurface and OuterFloorSurface triangles
so that overhang undersides are represented without filling the space below.
GroundSurface is omitted because the city terrain owns the floor.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import NamedTuple
from xml.etree import ElementTree as ET

import numpy as np

from citygml2glb import GML_ID, NS, _polygon_rings, triangulate_rings
from geodesy import project_epsg6697_to_local_enu
from mjcf_prism import triangular_prism, triangular_prism_along_normal
from gml_lod1_extract import validate_epsg6697_contract
from world_frame import load_world_frame

B = "{http://www.opengis.net/citygml/building/2.0}"
GML_POLYGON = "{http://www.opengis.net/gml}Polygon"
MIN_TRIANGLE_AREA_M2 = 1e-6
MIN_TRIANGLE_SHAPE_RATIO = 1e-6
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


def _selected_class(selection_path: Path, classification_path: Path, class_id: str):
    selection = json.loads(selection_path.read_text(encoding="utf-8"))
    classification = json.loads(classification_path.read_text(encoding="utf-8"))
    selected_ids = {
        record["building_id"] for record in classification.get("buildings", [])
        if record.get("class") == class_id
    }
    by_source = defaultdict(lambda: defaultdict(list))
    for part in selection.get("polygons", []):
        building_id = part["id"].split("__part_", 1)[0]
        if building_id in selected_ids:
            by_source[Path(part["source_gml"]).resolve()][building_id].append(part)
    missing = sorted(
        selected_ids
        - {building_id for values in by_source.values() for building_id in values}
    )
    if missing:
        raise BuildingLod2ColliderError(
            f"{class_id} buildings missing from LOD1 selection: {missing}"
        )
    origin = selection.get("origin") or {}
    return by_source, float(origin["lat"]), float(origin["lon"])


def _surface_pieces(
    by_source, center_lat, center_lon, frame, thickness_m: float, class_id: str
):
    offset = float(frame["origin"]["altitude_offset_m"])
    pieces = []
    per_building = CounterLike()
    skipped = defaultdict(int)
    for source, buildings in sorted(by_source.items(), key=lambda item: str(item[0])):
        root = ET.parse(source).getroot()
        validate_epsg6697_contract(root, source)
        indexed = {element.get(GML_ID): element for element in root.findall(".//bldg:Building", NS)}
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
                            enu = project_epsg6697_to_local_enu(points, center_lat, center_lon)
                            rings.append([
                                (north, -east, altitude - offset)
                                for east, north, altitude in enu
                            ])
                        vertices, faces = triangulate_rings(rings)
                        for face in faces:
                            triangle = np.asarray(vertices[face], dtype=float)
                            edges = [
                                float(np.linalg.norm(triangle[(index + 1) % 3] - triangle[index]))
                                for index in range(3)
                            ]
                            doubled_area = float(np.linalg.norm(np.cross(
                                triangle[1] - triangle[0], triangle[2] - triangle[0]
                            )))
                            max_edge = max(edges)
                            shape_ratio = (
                                doubled_area / (max_edge * max_edge) if max_edge else 0.0
                            )
                            if (
                                doubled_area / 2.0 < MIN_TRIANGLE_AREA_M2
                                or shape_ratio < MIN_TRIANGLE_SHAPE_RATIO
                            ):
                                skipped[surface_kind] += 1
                                continue
                            prism, prism_faces = (
                                triangular_prism(triangle, thickness_m)
                                if surface_kind == "RoofSurface"
                                else triangular_prism_along_normal(triangle, thickness_m)
                            )
                            piece_index = per_building.next(building_id)
                            pieces.append({
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
                            })
            if per_building.value(building_id) == 0:
                raise BuildingLod2ColliderError(
                    f"{class_id} building has no usable source collision surfaces: {building_id}"
                )
    return PreparedSurfaceGeometry(
        pieces=pieces,
        skipped_degenerate_by_surface=dict(sorted(skipped.items())),
    )


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
):
    if class_id not in CLASS_SURFACE_KINDS:
        raise BuildingLod2ColliderError(f"unsupported LOD2 collider class: {class_id}")
    if roof_thickness_m <= 0:
        raise BuildingLod2ColliderError(
            f"{class_id} surface collision thickness must be positive"
        )
    by_source, center_lat, center_lon = _selected_class(
        selection_path, classification_path, class_id
    )
    frame = load_world_frame(world_frame_path)
    geometry = _surface_pieces(
        by_source, center_lat, center_lon, frame, roof_thickness_m, class_id
    )
    return geometry


def prepare_p1_geometry(
    selection_path: Path,
    classification_path: Path,
    world_frame_path: Path,
    *,
    roof_thickness_m: float,
):
    return prepare_class_geometry(
        selection_path,
        classification_path,
        world_frame_path,
        class_id="P1",
        roof_thickness_m=roof_thickness_m,
    )


def prepare_p2_geometry(
    selection_path: Path,
    classification_path: Path,
    world_frame_path: Path,
    *,
    roof_thickness_m: float,
):
    return prepare_class_geometry(
        selection_path,
        classification_path,
        world_frame_path,
        class_id="P2",
        roof_thickness_m=roof_thickness_m,
    )


def prepare_p3_geometry(
    selection_path: Path,
    classification_path: Path,
    world_frame_path: Path,
    *,
    roof_thickness_m: float,
):
    return prepare_class_geometry(
        selection_path,
        classification_path,
        world_frame_path,
        class_id="P3",
        roof_thickness_m=roof_thickness_m,
    )
