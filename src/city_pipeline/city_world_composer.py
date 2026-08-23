#!/usr/bin/env python3
"""Compose independently generated terrain, road, and building city assets."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import xml.etree.ElementTree as ET
from pathlib import Path

import trimesh

from world_frame import load_world_frame


class ComposerError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _merge_size(roots) -> ET.Element | None:
    values = {}
    for _, root in roots:
        size = root.find("size")
        if size is None:
            continue
        for key, raw in size.attrib.items():
            try:
                value = float(raw)
            except ValueError as exc:
                raise ComposerError(f"non-numeric MJCF size attribute: {key}={raw!r}") from exc
            values[key] = max(values.get(key, value), value)
    if not values:
        return None
    return ET.Element("size", {
        key: str(int(value)) if value.is_integer() else str(value)
        for key, value in values.items()
    })


def _copy_with_rebased_files(element: ET.Element, source_xml: Path, output_xml: Path):
    cloned = copy.deepcopy(element)
    for descendant in cloned.iter():
        file_name = descendant.get("file")
        if not file_name:
            continue
        source = (source_xml.parent / file_name).resolve()
        if not source.is_file():
            raise ComposerError(f"MJCF asset file does not exist: {source}")
        descendant.set("file", os.path.relpath(source, output_xml.parent.resolve()))
    return cloned


def compose_mjcf(
    terrain_xml: Path,
    buildings_xml: Path,
    output_xml: Path,
    extra_mjcf: list[Path] | None = None,
) -> dict[str, int]:
    roots = [(terrain_xml, ET.parse(terrain_xml).getroot()),
             (buildings_xml, ET.parse(buildings_xml).getroot())]
    roots.extend((path, ET.parse(path).getroot()) for path in (extra_mjcf or []))
    geom_counts = {
        "terrain": len(roots[0][1].findall(".//geom")),
        "buildings": len(roots[1][1].findall(".//geom")),
    }
    for index, (path, root) in enumerate(roots[2:], 1):
        key = path.stem or f"extra_{index}"
        if key in geom_counts:
            key = f"{key}_{index}"
        geom_counts[key] = len(root.findall(".//geom"))
    geom_counts["total"] = sum(geom_counts.values())
    for source, root in roots:
        if root.tag != "mujoco":
            raise ComposerError(f"MJCF component root must be <mujoco>: {source}")
        unsupported = [child.tag for child in root if child.tag not in {"size", "asset", "worldbody"}]
        if unsupported:
            raise ComposerError(f"unsupported MJCF component sections in {source}: {unsupported}")

    output_xml.parent.mkdir(parents=True, exist_ok=True)
    result = ET.Element("mujoco", {"model": "plateau_city_world"})
    size = _merge_size(roots)
    if size is not None:
        result.append(size)
    asset = ET.SubElement(result, "asset")
    worldbody = ET.SubElement(result, "worldbody")
    names = set()
    for source, root in roots:
        for section_name, destination in (("asset", asset), ("worldbody", worldbody)):
            section = root.find(section_name)
            if section is None:
                continue
            for child in section:
                cloned = _copy_with_rebased_files(child, source, output_xml)
                for descendant in cloned.iter():
                    name = descendant.get("name")
                    key = (descendant.tag, name)
                    if name and key in names:
                        raise ComposerError(f"duplicate MJCF {descendant.tag} name: {name}")
                    if name:
                        names.add(key)
                destination.append(cloned)
    if not len(asset):
        result.remove(asset)
    ET.indent(result, space="  ")
    ET.ElementTree(result).write(output_xml, encoding="unicode", xml_declaration=False)
    with output_xml.open("a", encoding="utf-8") as stream:
        stream.write("\n")
    return geom_counts


def _as_scene(path: Path) -> trimesh.Scene:
    loaded = trimesh.load(path, force="scene")
    if not isinstance(loaded, trimesh.Scene) or not loaded.geometry:
        raise ComposerError(f"GLB contains no scene geometry: {path}")
    return loaded


def compose_glb(component_paths: list[Path], output_glb: Path) -> dict:
    result = trimesh.Scene()
    component_counts = {}
    for component_index, path in enumerate(component_paths):
        scene = _as_scene(path)
        count = 0
        for node_name in scene.graph.nodes_geometry:
            transform, geometry_name = scene.graph[node_name]
            geometry = scene.geometry[geometry_name].copy()
            geometry.apply_transform(transform)
            prefix = f"component-{component_index}-{path.stem}"
            result.add_geometry(
                geometry,
                node_name=f"{prefix}-{node_name}",
                geom_name=f"{prefix}-{geometry_name}-{count}",
            )
            count += 1
        component_counts[str(path.resolve())] = count
    if not result.geometry:
        raise ComposerError("no GLB component geometry was composed")
    output_glb.parent.mkdir(parents=True, exist_ok=True)
    output_glb.write_bytes(result.export(file_type="glb"))
    return component_counts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--world-frame", type=Path, required=True)
    parser.add_argument("--terrain-xml", type=Path, required=True)
    parser.add_argument("--buildings-xml", type=Path, required=True)
    parser.add_argument("--terrain-glb", type=Path, required=True)
    parser.add_argument("--roads-glb", type=Path, required=True)
    parser.add_argument("--buildings-glb", type=Path, required=True)
    parser.add_argument("--extra-mjcf", type=Path, action="append", default=[])
    parser.add_argument("--extra-glb", type=Path, action="append", default=[])
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()

    frame = load_world_frame(args.world_frame)
    output_xml = args.out_dir / "city-world.xml"
    output_glb = args.out_dir / "city-world.glb"
    mjcf_geom_counts = compose_mjcf(
        args.terrain_xml, args.buildings_xml, output_xml, args.extra_mjcf
    )
    components = [args.terrain_glb, args.roads_glb, args.buildings_glb, *args.extra_glb]
    geometry_counts = compose_glb(components, output_glb)
    receipt = {
        "schema_version": 1,
        "world_frame": str(args.world_frame.resolve()),
        "coordinate_frame": frame,
        "mjcf": {"path": str(output_xml.resolve()), "sha256": _sha256(output_xml)},
        "glb": {
            "path": str(output_glb.resolve()),
            "bytes": output_glb.stat().st_size,
            "sha256": _sha256(output_glb),
        },
        "components": {
            "terrain_xml": str(args.terrain_xml.resolve()),
            "buildings_xml": str(args.buildings_xml.resolve()),
            "extra_mjcf": [str(path.resolve()) for path in args.extra_mjcf],
            "mjcf_geom_counts": mjcf_geom_counts,
            "glb_geometry_counts": geometry_counts,
        },
    }
    receipt_path = args.out_dir / "city-world-receipt.json"
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"OK: city world MJCF: {output_xml}")
    print(f"OK: city world GLB: {output_glb}")
    print(f"OK: city world receipt: {receipt_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
