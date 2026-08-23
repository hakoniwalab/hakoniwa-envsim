#!/usr/bin/env python3
"""Convert selected PLATEAU CityGML buildings directly into an embedded GLB.

LOD2 surfaces and CityGML Appearance UVs are preferred. Buildings without
LOD2 fall back to an LOD1 solid built from the same footprint selection used
by the MuJoCo pipeline. Output vertices use Three.js-native axes corresponding
to Hakoniwa ROS coordinates: X=East, Y=Up, Z=-North.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import struct
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path
from xml.etree import ElementTree as ET

import mapbox_earcut
import numpy as np
import trimesh
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gml_lod1_extract import (  # noqa: E402
    NS as BASE_NS,
    parse_poslist,
    project_epsg6697_to_local_enu,
    validate_epsg6697_contract,
)
from world_frame import load_world_frame  # noqa: E402

NS = {
    **BASE_NS,
    "app": "http://www.opengis.net/citygml/appearance/2.0",
}
GML_ID = "{http://www.opengis.net/gml}id"


class GlbError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _open_values(values, paired=None):
    output = list(values)
    paired_output = list(paired) if paired is not None else None
    if len(output) > 1 and np.allclose(output[0], output[-1], rtol=0.0, atol=1e-10):
        output.pop()
        if paired_output is not None and len(paired_output) == len(values):
            paired_output.pop()
    return output, paired_output


def _parse_uv(text: str) -> list[tuple[float, float]]:
    values = [float(value) for value in text.split()]
    if len(values) % 2:
        raise GlbError("CityGML textureCoordinates must contain UV pairs")
    uv = [(values[index], 1.0 - values[index + 1]) for index in range(0, len(values), 2)]
    if len(uv) > 1 and np.allclose(uv[0], uv[-1], rtol=0.0, atol=1e-10):
        uv.pop()
    return uv


def appearance_map(root) -> dict[str, tuple[str, dict[str, list[tuple[float, float]]]]]:
    mapping = {}
    for texture in root.findall(".//app:ParameterizedTexture", NS):
        image_uri = texture.findtext("app:imageURI", default="", namespaces=NS).strip()
        if not image_uri:
            continue
        for target in texture.findall("app:target", NS):
            polygon_id = target.get("uri", "").removeprefix("#")
            rings = {}
            for coordinates in target.findall(".//app:textureCoordinates", NS):
                ring_id = coordinates.get("ring", "").removeprefix("#")
                if ring_id and coordinates.text:
                    rings[ring_id] = _parse_uv(coordinates.text)
            if polygon_id and rings:
                mapping[polygon_id] = (image_uri, rings)
    return mapping


def _polygon_rings(element) -> list[tuple[str, list[tuple[float, float, float]]]]:
    rings = []
    exterior = element.find("gml:exterior/gml:LinearRing", NS)
    if exterior is None:
        return []
    for ring in [exterior, *element.findall("gml:interior/gml:LinearRing", NS)]:
        pos = ring.find("gml:posList", NS)
        if pos is None or not pos.text:
            return []
        points, _ = _open_values(parse_poslist(pos.text))
        if len(points) < 3:
            return []
        rings.append((ring.get(GML_ID, ""), points))
    return rings


def _lod2_polygons(building) -> list:
    output = []
    seen = set()
    for query in (
        ".//bldg:lod2MultiSurface//gml:Polygon",
        ".//bldg:lod2Geometry//gml:Polygon",
        ".//bldg:lod2Solid//gml:Polygon",
    ):
        for polygon in building.findall(query, NS):
            key = polygon.get(GML_ID) or id(polygon)
            if key not in seen:
                seen.add(key)
                output.append(polygon)
    return output


def _surface_projection(points: list[tuple[float, float, float]]) -> tuple[int, int]:
    normal = np.zeros(3)
    values = np.asarray(points, dtype=float)
    for index, current in enumerate(values):
        following = values[(index + 1) % len(values)]
        normal += np.cross(current, following)
    drop = int(np.argmax(np.abs(normal)))
    return tuple(axis for axis in range(3) if axis != drop)


def triangulate_rings(rings: list[list[tuple[float, float, float]]]):
    if not rings or len(rings[0]) < 3:
        raise GlbError("polygon has no triangulatable exterior ring")
    axes = _surface_projection(rings[0])
    flattened = [point for ring in rings for point in ring]
    projected = np.asarray([[point[axes[0]], point[axes[1]]] for point in flattened], dtype=np.float64)
    ends = []
    count = 0
    for ring in rings:
        count += len(ring)
        ends.append(count)
    indices = mapbox_earcut.triangulate_float64(projected, np.asarray(ends, dtype=np.uint32))
    if not len(indices):
        raise GlbError("polygon triangulation produced no faces")
    return np.asarray(flattened, dtype=float), indices.reshape((-1, 3))


class TextureResolver:
    def __init__(self, source_urls: dict[Path, str], fetch: bool, enabled: bool):
        self.source_urls = source_urls
        self.fetch = fetch
        self.enabled = enabled
        self.records = {}

    def resolve(self, gml_path: Path, image_uri: str) -> Path | None:
        if not self.enabled:
            return None
        parsed_ref = urllib.parse.urlparse(image_uri)
        parts = Path(parsed_ref.path).parts
        if parsed_ref.scheme or parsed_ref.netloc or not image_uri or ".." in parts:
            raise GlbError(f"unsafe CityGML texture reference: {image_uri!r}")
        destination = (gml_path.parent / Path(*parts)).resolve()
        try:
            destination.relative_to(gml_path.parent.resolve())
        except ValueError as exc:
            raise GlbError(f"texture escapes its CityGML source directory: {image_uri}") from exc
        base_url = self.source_urls.get(gml_path.resolve())
        url = urllib.parse.urljoin(base_url, image_uri) if base_url else None
        reused = destination.is_file()
        if not reused:
            if not self.fetch:
                return None
            if not url or urllib.parse.urlparse(url).scheme != "https":
                raise GlbError(f"cannot resolve an HTTPS texture URL for {gml_path}: {image_uri}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_suffix(destination.suffix + ".part")
            request = urllib.request.Request(url, headers={"User-Agent": "hakoniwa-envsim/plateau-glb"})
            try:
                with urllib.request.urlopen(request, timeout=180) as response, temporary.open("wb") as output:
                    while chunk := response.read(1024 * 1024):
                        output.write(chunk)
                os.replace(temporary, destination)
            except Exception as exc:
                temporary.unlink(missing_ok=True)
                raise GlbError(f"PLATEAU texture download failed: {url}: {exc}") from exc
        key = str(destination)
        self.records[key] = {
            "path": key,
            "source_gml": str(gml_path),
            "image_uri": image_uri,
            "url": url,
            "bytes": destination.stat().st_size,
            "sha256": sha256_file(destination),
            "mime_type": mimetypes.guess_type(destination.name)[0] or "application/octet-stream",
            "mode": "reused" if reused and self.fetch else "offline-reused" if reused else "downloaded",
        }
        return destination


def _source_urls(download_manifest: Path | None) -> dict[Path, str]:
    if download_manifest is None or not download_manifest.is_file():
        return {}
    data = json.loads(download_manifest.read_text(encoding="utf-8"))
    return {Path(item["path"]).resolve(): item["url"] for item in data.get("files", [])}


def _three_coordinates(points, center_lat, center_lon, z_offset):
    enu = project_epsg6697_to_local_enu(points, center_lat, center_lon)
    return [(east, altitude - z_offset, -north) for east, north, altitude in enu]


def _append_surface(batches, key, vertices, faces, uvs=None):
    batch = batches[key]
    offset = len(batch["vertices"])
    batch["vertices"].extend(vertices)
    batch["faces"].extend((face + offset).tolist() for face in faces)
    batch["uv"].extend(uvs if uvs is not None else [(0.0, 0.0)] * len(vertices))


def _append_lod1_part(batches, part, z_offset):
    exterior = [tuple(point) for point in part["vertices"]]
    holes = [[tuple(point) for point in ring] for ring in part.get("interior_rings", [])]
    zmin, zmax = float(part["zmin"]), float(part["zmax"])
    rings_top = [[(east, zmax - z_offset, -north) for east, north in ring] for ring in [exterior, *holes]]
    vertices, faces = triangulate_rings(rings_top)
    _append_surface(batches, None, vertices.tolist(), faces)
    rings_bottom = [[(east, zmin - z_offset, -north) for east, north in ring] for ring in [exterior, *holes]]
    vertices, faces = triangulate_rings(rings_bottom)
    _append_surface(batches, None, vertices.tolist(), faces[:, ::-1])
    for ring in [exterior, *holes]:
        for index, current in enumerate(ring):
            following = ring[(index + 1) % len(ring)]
            wall = [
                (current[0], zmin - z_offset, -current[1]),
                (following[0], zmin - z_offset, -following[1]),
                (following[0], zmax - z_offset, -following[1]),
                (current[0], zmax - z_offset, -current[1]),
            ]
            _append_surface(batches, None, wall, np.asarray([[0, 1, 2], [0, 2, 3]]))


def _selection(selection_path: Path):
    data = json.loads(selection_path.read_text(encoding="utf-8"))
    by_source = defaultdict(lambda: defaultdict(list))
    zmins = []
    for part in data.get("polygons", []):
        source = Path(part["source_gml"]).resolve()
        building_id = part["id"].split("__part_", 1)[0]
        by_source[source][building_id].append(part)
        zmins.append(float(part["zmin"]))
    if not zmins:
        raise GlbError("selection contains no buildings")
    origin = data.get("origin") or {}
    extraction = {
        "skipped_building_count": int(data.get("skipped_buildings", 0)),
        "issues": data.get("building_extraction_issues", []),
    }
    return by_source, float(min(zmins)), float(origin["lat"]), float(origin["lon"]), extraction


def build_glb(
    selection_path: Path,
    output_path: Path,
    receipt_path: Path,
    download_manifest: Path | None,
    fetch_textures: bool,
    texture_mode: str,
    altitude_offset_m: float | None = None,
) -> dict:
    selected, selection_z_offset, center_lat, center_lon, extraction = _selection(selection_path)
    z_offset = selection_z_offset if altitude_offset_m is None else float(altitude_offset_m)
    resolver = TextureResolver(_source_urls(download_manifest), fetch_textures, texture_mode != "flat")
    batches = defaultdict(lambda: {"vertices": [], "faces": [], "uv": []})
    lod1_buildings = lod2_buildings = textured_surfaces = flat_surfaces = 0

    for gml_path, buildings in selected.items():
        root = ET.parse(gml_path).getroot()
        validate_epsg6697_contract(root, gml_path)
        appearances = appearance_map(root)
        indexed = {building.get(GML_ID): building for building in root.findall(".//bldg:Building", NS)}
        for building_id, parts in buildings.items():
            building = indexed.get(building_id)
            polygons = _lod2_polygons(building) if building is not None else []
            emitted = 0
            for polygon in polygons:
                polygon_id = polygon.get(GML_ID, "")
                parsed = _polygon_rings(polygon)
                if not parsed:
                    continue
                rings_geo = [ring for _, ring in parsed]
                rings_three = [
                    _three_coordinates(ring, center_lat, center_lon, z_offset) for ring in rings_geo
                ]
                vertices, faces = triangulate_rings(rings_three)
                texture_path = None
                uv_values = None
                appearance = appearances.get(polygon_id)
                if appearance is not None:
                    image_uri, uv_by_ring = appearance
                    candidate_uv = []
                    complete = True
                    for (ring_id, ring_geo) in parsed:
                        uv = uv_by_ring.get(ring_id)
                        if uv is None:
                            complete = False
                            break
                        _, uv = _open_values(ring_geo, uv)
                        if uv is None or len(uv) != len(ring_geo):
                            complete = False
                            break
                        candidate_uv.extend(uv)
                    if complete:
                        texture_path = resolver.resolve(gml_path, image_uri)
                        uv_values = candidate_uv if texture_path is not None else None
                _append_surface(batches, str(texture_path) if texture_path else None, vertices.tolist(), faces, uv_values)
                textured_surfaces += texture_path is not None
                flat_surfaces += texture_path is None
                emitted += 1
            if emitted:
                lod2_buildings += 1
            else:
                lod1_buildings += 1
                for part in parts:
                    _append_lod1_part(batches, part, z_offset)

    scene = trimesh.Scene()
    total_triangles = 0
    all_vertices = []
    for index, (texture_key, batch) in enumerate(sorted(batches.items(), key=lambda item: item[0] or "")):
        vertices = np.asarray(batch["vertices"], dtype=np.float32)
        faces = np.asarray(batch["faces"], dtype=np.int64)
        if not len(vertices) or not len(faces):
            continue
        if texture_key:
            image = Image.open(texture_key).convert("RGB")
            material = trimesh.visual.material.PBRMaterial(
                name=Path(texture_key).stem,
                baseColorTexture=image,
                metallicFactor=0.0,
                roughnessFactor=1.0,
                doubleSided=True,
            )
            visual = trimesh.visual.texture.TextureVisuals(
                uv=np.asarray(batch["uv"], dtype=np.float32), material=material
            )
        else:
            material = trimesh.visual.material.PBRMaterial(
                name="plateau-flat",
                baseColorFactor=[0.72, 0.74, 0.78, 1.0],
                metallicFactor=0.0,
                roughnessFactor=1.0,
                doubleSided=True,
            )
            visual = trimesh.visual.texture.TextureVisuals(material=material)
        mesh = trimesh.Trimesh(vertices=vertices, faces=faces, visual=visual, process=False)
        scene.add_geometry(mesh, node_name=f"plateau-{index:04d}", geom_name=f"plateau-{index:04d}")
        total_triangles += len(faces)
        all_vertices.append(vertices)
    if not scene.geometry:
        raise GlbError("GLB conversion produced no geometry")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(scene.export(file_type="glb"))
    values = np.concatenate(all_vertices)
    receipt = {
        "schema_version": 1,
        "output": str(output_path),
        "bytes": output_path.stat().st_size,
        "sha256": sha256_file(output_path),
        "coordinate_system": {
            "glb": "X=East,Y=Up,Z=-North",
            "hakoniwa_ros": "X=North,Y=-East,Z=Up",
            "origin": {"latitude": center_lat, "longitude": center_lon, "altitude_offset_m": z_offset},
        },
        "buildings": {"lod2": lod2_buildings, "lod1_fallback": lod1_buildings},
        "extraction": extraction,
        "surfaces": {"textured": int(textured_surfaces), "flat": int(flat_surfaces)},
        "triangles": total_triangles,
        "meshes": len(scene.geometry),
        "textures": list(resolver.records.values()),
        "bounds": {"min": values.min(axis=0).tolist(), "max": values.max(axis=0).tolist()},
    }
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--selection", type=Path, required=True, help="gml_lod1_extract JSON used by the MJCF path")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--download-manifest", type=Path)
    parser.add_argument("--fetch-textures", action="store_true")
    parser.add_argument("--texture-mode", choices=("embedded-if-available", "flat"), default="embedded-if-available")
    parser.add_argument("--world-frame", type=Path,
                        help="Shared city world-frame.json; uses its common altitude offset")
    args = parser.parse_args()
    try:
        altitude_offset = None
        if args.world_frame:
            altitude_offset = load_world_frame(args.world_frame)["origin"]["altitude_offset_m"]
        receipt = build_glb(
            args.selection, args.out, args.receipt, args.download_manifest,
            args.fetch_textures, args.texture_mode, altitude_offset,
        )
        print(f"[OK] GLB: {args.out} ({receipt['triangles']} triangles, {len(receipt['textures'])} textures)")
        return 0
    except (GlbError, OSError, ET.ParseError, ValueError) as exc:
        print(f"[ERR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
