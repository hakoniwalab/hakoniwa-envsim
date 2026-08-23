#!/usr/bin/env python3
"""Convert generated City World MJCF collision geometry to a debug GLB."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import struct
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

import numpy as np
import trimesh


class ColliderGlbError(RuntimeError):
    pass


MJCF_TO_THREE = np.asarray([
    [0.0, -1.0, 0.0, 0.0],
    [0.0, 0.0, 1.0, 0.0],
    [-1.0, 0.0, 0.0, 0.0],
    [0.0, 0.0, 0.0, 1.0],
])


def _floats(value: str | None, count: int, default: tuple[float, ...]) -> list[float]:
    values = list(default) if value is None else [float(item) for item in value.split()]
    if len(values) != count:
        raise ColliderGlbError(f"expected {count} numeric values, found {len(values)}")
    return values


def _element_transform(element: ET.Element) -> np.ndarray:
    position = _floats(element.get("pos"), 3, (0.0, 0.0, 0.0))
    euler = _floats(element.get("euler"), 3, (0.0, 0.0, 0.0))
    matrix = trimesh.transformations.euler_matrix(
        *(math.radians(value) for value in euler), axes="sxyz",
    )
    matrix[:3, 3] = position
    return matrix


def _inline_mesh(asset: ET.Element) -> trimesh.Trimesh:
    if asset.get("file"):
        raise ColliderGlbError("file-backed MJCF mesh is outside the generated City World contract")
    vertices = np.asarray(_floats(asset.get("vertex"), len((asset.get("vertex") or "").split()), ()), dtype=float)
    faces = np.asarray([int(value) for value in (asset.get("face") or "").split()], dtype=int)
    if not len(vertices) or len(vertices) % 3 or not len(faces) or len(faces) % 3:
        raise ColliderGlbError(f"invalid inline mesh asset: {asset.get('name', '<unnamed>')}")
    return trimesh.Trimesh(
        vertices=vertices.reshape((-1, 3)), faces=faces.reshape((-1, 3)), process=False,
    )


def _hfield_mesh(asset: ET.Element, xml_path: Path) -> trimesh.Trimesh:
    source = (xml_path.parent / (asset.get("file") or "")).resolve()
    if not source.is_file():
        raise ColliderGlbError(f"hfield data was not found: {source}")
    raw = source.read_bytes()
    if len(raw) < 8:
        raise ColliderGlbError(f"hfield header is truncated: {source}")
    nrow, ncol = struct.unpack_from("<ii", raw)
    expected = 8 + nrow * ncol * 4
    if nrow < 2 or ncol < 2 or len(raw) != expected:
        raise ColliderGlbError(f"invalid hfield dimensions or byte length: {source}")
    samples = np.asarray(struct.unpack_from(f"<{nrow * ncol}f", raw, 8), dtype=float).reshape((nrow, ncol))
    size_x, size_y, size_z, _base = _floats(asset.get("size"), 4, ())
    minimum = float(samples.min())
    maximum = float(samples.max())
    heights = np.zeros_like(samples) if maximum == minimum else (samples - minimum) / (maximum - minimum) * size_z
    vertices = []
    for row in range(nrow):
        y = -size_y + 2.0 * size_y * row / (nrow - 1)
        for col in range(ncol):
            x = -size_x + 2.0 * size_x * col / (ncol - 1)
            vertices.append((x, y, float(heights[row, col])))
    faces = []
    for row in range(nrow - 1):
        for col in range(ncol - 1):
            first = row * ncol + col
            faces.extend(((first, first + 1, first + ncol + 1), (first, first + ncol + 1, first + ncol)))
    return trimesh.Trimesh(vertices=vertices, faces=faces, process=False)


def convert_mjcf_colliders(xml_path: Path, output: Path, receipt_path: Path) -> dict:
    root = ET.parse(xml_path).getroot()
    assets = root.find("asset")
    worldbody = root.find("worldbody")
    if assets is None or worldbody is None:
        raise ColliderGlbError("MJCF must contain asset and worldbody")
    mesh_assets = {
        element.get("name"): _inline_mesh(element)
        for element in assets.findall("mesh")
        if element.get("name")
    }
    hfield_assets = {
        element.get("name"): _hfield_mesh(element, xml_path)
        for element in assets.findall("hfield")
        if element.get("name")
    }
    converted: list[trimesh.Trimesh] = []
    counts: Counter[str] = Counter()

    def visit(parent: ET.Element, parent_transform: np.ndarray) -> None:
        for element in parent:
            if element.tag == "body":
                visit(element, parent_transform @ _element_transform(element))
                continue
            if element.tag != "geom":
                continue
            geom_type = element.get("type", "sphere")
            if geom_type == "box":
                size = np.asarray(_floats(element.get("size"), 3, ()), dtype=float)
                mesh = trimesh.creation.box(extents=size * 2.0)
            elif geom_type == "mesh":
                name = element.get("mesh")
                if name not in mesh_assets:
                    raise ColliderGlbError(f"unknown MJCF mesh asset: {name}")
                mesh = mesh_assets[name].copy()
            elif geom_type == "hfield":
                name = element.get("hfield")
                if name not in hfield_assets:
                    raise ColliderGlbError(f"unknown MJCF hfield asset: {name}")
                mesh = hfield_assets[name].copy()
            else:
                raise ColliderGlbError(f"unsupported generated collider type: {geom_type}")
            mesh.apply_transform(parent_transform @ _element_transform(element))
            mesh.apply_transform(MJCF_TO_THREE)
            mesh.visual.vertex_colors = np.tile(
                np.asarray([255, 47, 146, 92], dtype=np.uint8),
                (len(mesh.vertices), 1),
            )
            converted.append(mesh)
            counts[geom_type] += 1

    visit(worldbody, np.eye(4))
    if not converted:
        raise ColliderGlbError("MJCF contains no collision geometry")
    combined = trimesh.util.concatenate(converted)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(combined.export(file_type="glb"))
    receipt = {
        "schema_version": 1,
        "source_mjcf": str(xml_path.resolve()),
        "coordinate_transform": "MJCF(X=North,Y=-East,Z=Up)->Three.js(X=East,Y=Up,Z=-North)",
        "geom_counts": dict(sorted(counts.items())),
        "triangle_count": int(len(combined.faces)),
        "output": {
            "path": str(output.resolve()),
            "bytes": output.stat().st_size,
            "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        },
        "purpose": "debug visualization only; MuJoCo remains the collision authority",
    }
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--in", dest="source", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    receipt = convert_mjcf_colliders(args.source, args.out, args.receipt)
    print(f"OK: collider debug GLB: {args.out} ({receipt['triangle_count']} triangles)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
