#!/usr/bin/env python3
"""Convert source PLATEAU bridge geometry to a display-only GLB.

Each bridge is drawn from its LOD3 geometry when it has any, else from its
LOD2 geometry (the bounded surfaces and the construction elements; the LOD2
solid only refers to those surfaces). Many cities publish bridges at LOD2 only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import xml.etree.ElementTree as ET
import xml.sax
from collections import defaultdict
from pathlib import Path

import numpy as np
import trimesh
from trimesh.visual.material import PBRMaterial

from citygml2glb import GlbError, _polygon_rings, triangulate_rings
from geodesy import SUPPORTED_CRS, epsg_label, project_to_local_enu, srs_code
from world_frame import load_world_frame

GML = "http://www.opengis.net/gml"
BRID = "http://www.opengis.net/citygml/bridge/2.0"
GML_ID = f"{{{GML}}}id"
BRIDGE_TAG = f"{{{BRID}}}Bridge"
LOD3_TAGS = {f"{{{BRID}}}lod3Geometry", f"{{{BRID}}}lod3MultiSurface"}
LOD2_TAGS = {f"{{{BRID}}}lod2Geometry", f"{{{BRID}}}lod2MultiSurface"}
POLYGON_TAG = f"{{{GML}}}Polygon"
DEFAULT_RGBA = (150, 156, 164, 255)


class BridgeGlbError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def bridge_source_paths(source: Path) -> list[Path]:
    if source.is_file():
        return [source]
    if source.is_dir():
        return sorted(source.rglob("*brid*_op.gml"))
    raise BridgeGlbError(f"bridge source does not exist: {source}")


def bridges_with_lod3(path: Path) -> set[str]:
    """The ids of the bridges in a CityGML file that have LOD3 geometry."""
    found: set[str] = set()
    current = None
    for event, element in ET.iterparse(path, events=("start", "end")):
        if event == "start":
            if element.tag == BRIDGE_TAG:
                current = element.get(GML_ID, "unknown-bridge")
            elif element.tag in LOD3_TAGS and current is not None:
                found.add(current)
        elif element.tag == BRIDGE_TAG:
            current = None
            element.clear()
    return found


def bridge_lod_in_use(lod3_depth: int, lod2_depth: int, bridge: str | None, lod3_bridges: set[str]) -> int | None:
    """The LOD a polygon at this depth is drawn from, or None: LOD3, or LOD2
    for a bridge without LOD3."""
    if lod3_depth:
        return 3
    if lod2_depth and (bridge or "unknown-bridge") not in lod3_bridges:
        return 2
    return None


def validate_bridge_crs(path: Path) -> int:
    """The EPSG code of bridge data inside the Envsim 3D geographic CRS contract."""
    for _event, element in ET.iterparse(path, events=("start",)):
        if element.tag == f"{{{GML}}}Envelope":
            srs_name = element.get("srsName", "")
            dimension = element.get("srsDimension", "")
            epsg = srs_code(srs_name)
            if epsg not in SUPPORTED_CRS:
                supported = " or ".join(epsg_label(code) for code in SUPPORTED_CRS)
                raise BridgeGlbError(
                    f"{path}: bridge CityGML must use {supported}, got {srs_name!r}"
                )
            if dimension != "3":
                raise BridgeGlbError(
                    f"{path}: bridge CityGML must use srsDimension=3, got {dimension!r}"
                )
            return epsg
    raise BridgeGlbError(f"{path}: bridge CityGML has no gml:Envelope CRS contract")


class _MaterialHandler(xml.sax.handler.ContentHandler):
    def __init__(self):
        super().__init__()
        self.colors: dict[str, tuple[int, int, int, int]] = {}
        self.in_material = False
        self.capture: str | None = None
        self.text: list[str] = []
        self.diffuse: str | None = None
        self.targets: list[str] = []

    def startElement(self, name, attrs):  # noqa: N802 - SAX API
        local = name.split(":")[-1]
        if local == "X3DMaterial":
            self.in_material = True
            self.diffuse = None
            self.targets = []
        elif self.in_material and local in {"diffuseColor", "target"}:
            self.capture = local
            self.text = []
            if local == "target" and attrs.get("uri"):
                self.targets.append(attrs["uri"].removeprefix("#"))

    def characters(self, content):
        if self.capture:
            self.text.append(content)

    def endElement(self, name):  # noqa: N802 - SAX API
        local = name.split(":")[-1]
        if self.in_material and local == self.capture:
            value = "".join(self.text).strip()
            if local == "diffuseColor":
                self.diffuse = value
            elif local == "target" and value:
                self.targets.append(value.removeprefix("#"))
            self.capture = None
            self.text = []
        if local == "X3DMaterial":
            if self.diffuse:
                values = [min(1.0, max(0.0, float(value))) for value in self.diffuse.split()]
                if len(values) == 3:
                    rgba = tuple(round(value * 255) for value in values) + (255,)
                    for target in self.targets:
                        if target:
                            self.colors[target] = rgba
            self.in_material = False


def material_colors(path: Path) -> dict[str, tuple[int, int, int, int]]:
    handler = _MaterialHandler()
    xml.sax.parse(str(path), handler)
    return handler.colors


def _in_range(points, latitude, longitude, ns_m, ew_m, epsg=6697) -> bool:
    enu = project_to_local_enu(points, latitude, longitude, epsg)
    east = [point[0] for point in enu]
    north = [point[1] for point in enu]
    return (
        min(east) <= ew_m and max(east) >= -ew_m
        and min(north) <= ns_m and max(north) >= -ns_m
    )


def _glb_points(points, latitude, longitude, altitude_offset_m, epsg=6697):
    enu = project_to_local_enu(points, latitude, longitude, epsg)
    return [
        (east, altitude - altitude_offset_m, -north)
        for east, north, altitude in enu
    ]


def _append(batch, vertices, faces):
    offset = len(batch["vertices"])
    batch["vertices"].extend(vertices.tolist())
    batch["faces"].extend((face + offset).tolist() for face in faces)


def _extract_geometry(path, colors, frame, batches, epsg=6697):
    origin = frame["origin"]
    extent = frame["half_extent_m"]
    latitude = float(origin["latitude"])
    longitude = float(origin["longitude"])
    altitude_offset = float(origin["altitude_offset_m"])
    ns_m = float(extent["north_south"])
    ew_m = float(extent["east_west"])
    selected_bridges: set[str] = set()
    polygon_count = triangle_count = material_count = fallback_count = 0
    rejected_polygon_count = 0
    rejected_polygon_ids: list[str] = []
    stack: list[ET.Element] = []
    lod3_depth = lod2_depth = 0
    polygon_depth = 0
    current_bridge: str | None = None
    lod3_bridges = bridges_with_lod3(path)
    bridge_lods: dict[str, int] = {}

    for event, element in ET.iterparse(path, events=("start", "end")):
        if event == "start":
            stack.append(element)
            if element.tag == BRIDGE_TAG:
                current_bridge = element.get(GML_ID, "unknown-bridge")
            if element.tag in LOD3_TAGS:
                lod3_depth += 1
            if element.tag in LOD2_TAGS:
                lod2_depth += 1
            if element.tag == POLYGON_TAG:
                polygon_depth += 1
            continue

        if element.tag == POLYGON_TAG:
            lod = bridge_lod_in_use(lod3_depth, lod2_depth, current_bridge, lod3_bridges)
            if lod:
                parsed = _polygon_rings(element)
                if parsed and _in_range(parsed[0][1], latitude, longitude, ns_m, ew_m, epsg):
                    rings = [
                        _glb_points(points, latitude, longitude, altitude_offset, epsg)
                        for _, points in parsed
                    ]
                    polygon_id = element.get(GML_ID, "")
                    try:
                        vertices, faces = triangulate_rings(rings)
                    except (GlbError, ValueError):
                        rejected_polygon_count += 1
                        if len(rejected_polygon_ids) < 100:
                            rejected_polygon_ids.append(polygon_id or "<missing-id>")
                    else:
                        rgba = colors.get(polygon_id, DEFAULT_RGBA)
                        _append(batches[rgba], vertices, faces)
                        polygon_count += 1
                        triangle_count += len(faces)
                        if polygon_id in colors:
                            material_count += 1
                        else:
                            fallback_count += 1
                        if current_bridge:
                            selected_bridges.add(current_bridge)
                            bridge_lods[current_bridge] = lod
            polygon_depth -= 1
            if len(stack) >= 2:
                stack[-2].remove(element)
            element.clear()
        elif polygon_depth == 0:
            if element.tag == BRIDGE_TAG:
                current_bridge = None
            element.clear()
        if element.tag in LOD3_TAGS:
            lod3_depth -= 1
        if element.tag in LOD2_TAGS:
            lod2_depth -= 1
        stack.pop()

    return {
        "bridge_ids": selected_bridges,
        "bridge_lods": bridge_lods,
        "polygon_count": polygon_count,
        "triangle_count": triangle_count,
        "material_polygon_count": material_count,
        "fallback_polygon_count": fallback_count,
        "rejected_polygon_count": rejected_polygon_count,
        "rejected_polygon_ids": rejected_polygon_ids,
    }


def convert(
    source: Path,
    world_frame_path: Path,
    output: Path,
    receipt_path: Path | None = None,
    allow_empty: bool = False,
) -> dict:
    frame = load_world_frame(world_frame_path)
    sources = bridge_source_paths(source)
    batches = defaultdict(lambda: {"vertices": [], "faces": []})
    totals = {
        "bridge_ids": set(), "bridge_lods": {}, "polygon_count": 0, "triangle_count": 0,
        "material_polygon_count": 0, "fallback_polygon_count": 0,
        "rejected_polygon_count": 0, "rejected_polygon_ids": [],
    }
    for path in sources:
        epsg = validate_bridge_crs(path)
        result = _extract_geometry(path, material_colors(path), frame, batches, epsg)
        totals["bridge_ids"].update(result.pop("bridge_ids"))
        totals["bridge_lods"].update(result.pop("bridge_lods"))
        for key, value in result.items():
            if key == "rejected_polygon_ids":
                totals[key].extend(value[:max(0, 100 - len(totals[key]))])
            else:
                totals[key] += value

    receipt_path = receipt_path or output.with_name(output.stem + "-glb-receipt.json")
    receipt = {
        "schema_version": 1,
        "component": "plateau_bridges",
        "status": "available" if batches else "not_available",
        "sources": [{"path": str(path.resolve()), "sha256": _sha256(path)} for path in sources],
        "world_frame": str(world_frame_path.resolve()),
        "selection_policy": "bridge polygon intersects configured horizontal range",
        "lod_policy": "per bridge: LOD3 geometry when it has any, else LOD2",
        "lod3_bridge_count": sum(1 for lod in totals["bridge_lods"].values() if lod == 3),
        "lod2_bridge_count": sum(1 for lod in totals["bridge_lods"].values() if lod == 2),
        "geometry_policy": "source altitude preserved; no terrain draping or inferred bridge geometry",
        "material_policy": "PLATEAU X3DMaterial diffuseColor with documented fallback",
        "bridge_count": len(totals["bridge_ids"]),
        "bridge_ids": sorted(totals["bridge_ids"]),
        "polygon_count": totals["polygon_count"],
        "triangle_count": totals["triangle_count"],
        "material_polygon_count": totals["material_polygon_count"],
        "fallback_polygon_count": totals["fallback_polygon_count"],
        "rejected_polygon_count": totals["rejected_polygon_count"],
        "rejected_polygon_ids": totals["rejected_polygon_ids"],
        "fallback_rgba": list(DEFAULT_RGBA),
    }
    if not batches:
        if not allow_empty:
            raise BridgeGlbError("no PLATEAU LOD3 or LOD2 bridge geometry intersects the requested range")
        output.unlink(missing_ok=True)
        receipt["reason"] = "no matching LOD3 or LOD2 bridge geometry"
        receipt["output"] = None
        receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return receipt

    scene = trimesh.Scene()
    for index, (rgba, batch) in enumerate(sorted(batches.items())):
        mesh = trimesh.Trimesh(
            vertices=np.asarray(batch["vertices"], dtype=np.float32),
            faces=np.asarray(batch["faces"], dtype=np.int64),
            process=False,
        )
        mesh.visual = trimesh.visual.TextureVisuals(material=PBRMaterial(
            name=f"plateau-bridge-{index}", baseColorFactor=list(rgba),
            metallicFactor=0.0, roughnessFactor=1.0, doubleSided=True,
        ))
        scene.add_geometry(mesh, node_name=f"bridge-{index}", geom_name=f"bridge-{index}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(scene.export(file_type="glb"))
    receipt.update({
        "output": str(output.resolve()), "bytes": output.stat().st_size,
        "sha256": _sha256(output),
    })
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--world-frame", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--allow-empty", action="store_true")
    args = parser.parse_args()
    try:
        receipt = convert(
            args.source, args.world_frame, args.out, args.receipt, args.allow_empty
        )
        if receipt["status"] == "not_available":
            print("INFO: no LOD3 or LOD2 bridge data; bridge GLB was omitted")
        else:
            print(
                f"OK: PLATEAU bridge GLB: {args.out} ({receipt['bridge_count']} bridges: "
                f"{receipt['lod3_bridge_count']} LOD3, {receipt['lod2_bridge_count']} LOD2; "
                f"{receipt['triangle_count']} triangles)"
            )
        return 0
    except (BridgeGlbError, OSError, ET.ParseError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
