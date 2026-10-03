#!/usr/bin/env python3
"""Extract a query-centered PLATEAU DEM window as a MuJoCo height field."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import math
import mmap
import os
import re
import struct
import xml.etree.ElementTree as ET
from pathlib import Path

from geodesy import SUPPORTED_CRS, epsg_label, project_to_local_enu, srs_code
from world_frame import create_world_frame, write_world_frame

GML = "http://www.opengis.net/gml"


class DemError(RuntimeError):
    pass


def geographic_bounds(latitude: float, longitude: float, ns_m: float, ew_m: float):
    lat_delta = ns_m / 111_320.0
    lon_delta = ew_m / (111_320.0 * math.cos(math.radians(latitude)))
    return longitude - lon_delta, latitude - lat_delta, longitude + lon_delta, latitude + lat_delta


def _dem_header(path: Path, west: float, south: float, east: float, north: float):
    """Validate CRS, return the document GML prefix, bbox intersection and EPSG code."""
    gml_prefix = None
    envelope_seen = False
    epsg = None
    with path.open("rb") as stream:
        parser = ET.iterparse(stream, events=("start-ns", "start", "end"))
        for event, payload in parser:
            if event == "start-ns":
                prefix, uri = payload
                if uri == GML:
                    gml_prefix = prefix
                continue
            element = payload
            if event == "start" and element.tag == f"{{{GML}}}Envelope" and not envelope_seen:
                envelope_seen = True
                epsg = srs_code(element.get("srsName", ""))
                if epsg not in SUPPORTED_CRS or element.get("srsDimension") != "3":
                    supported = " or ".join(epsg_label(code) for code in SUPPORTED_CRS)
                    raise DemError(f"DEM must use three-dimensional {supported}")
                continue
            if event != "end" or element.tag != f"{{{GML}}}Envelope" or not envelope_seen:
                continue
            lower = element.find(f"{{{GML}}}lowerCorner")
            upper = element.find(f"{{{GML}}}upperCorner")
            if lower is None or upper is None:
                break
            lower_values = [float(value) for value in (lower.text or "").split()]
            upper_values = [float(value) for value in (upper.text or "").split()]
            if len(lower_values) < 2 or len(upper_values) < 2:
                break
            file_south, file_west = lower_values[:2]
            file_north, file_east = upper_values[:2]
            intersects = not (
                file_north < south or file_south > north
                or file_east < west or file_west > east
            )
            return gml_prefix, intersects, epsg
    if not envelope_seen:
        raise DemError("DEM CityGML has no CRS envelope")
    raise DemError("DEM CityGML has an invalid CRS envelope")


def _iter_pos_lists(path: Path, gml_prefix: str | None):
    """Yield numeric posList tokens without building the unrelated XML tree."""
    qualified = f"{gml_prefix}:posList" if gml_prefix else "posList"
    opening = f"<{qualified}".encode("ascii")
    closing = f"</{qualified}>".encode("ascii")
    valid_after_name = b" \t\r\n>"
    with path.open("rb") as stream:
        with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as contents:
            position = 0
            while True:
                start = contents.find(opening, position)
                if start < 0:
                    return
                after_name = start + len(opening)
                if after_name >= len(contents) or contents[after_name] not in valid_after_name:
                    position = after_name
                    continue
                text_start = contents.find(b">", after_name)
                if text_start < 0:
                    raise DemError(f"unterminated {qualified} opening tag")
                text_start += 1
                text_end = contents.find(closing, text_start)
                if text_end < 0:
                    raise DemError(f"unterminated {qualified} element")
                yield contents[text_start:text_end].split()
                position = text_end + len(closing)


def extract_triangles(path: Path, latitude: float, longitude: float, ns_m: float, ew_m: float):
    west, south, east, north = geographic_bounds(latitude, longitude, ns_m, ew_m)
    triangles = []
    gml_prefix, intersects, epsg = _dem_header(path, west, south, east, north)
    if not intersects:
        return []
    for tokens in _iter_pos_lists(path, gml_prefix):
        values = [float(value) for value in tokens]
        if len(values) < 9 or len(values) % 3:
            continue
        points = [values[index:index + 3] for index in range(0, len(values), 3)]
        if len(points) >= 4 and points[0] == points[-1]:
            points.pop()
        if len(points) != 3:
            continue
        lats = [point[0] for point in points]
        lons = [point[1] for point in points]
        if max(lats) < south or min(lats) > north or max(lons) < west or min(lons) > east:
            continue
        enu = project_to_local_enu(points, latitude, longitude, epsg)
        # MuJoCo hfield axes: X=North, Y=-East, Z=Up.
        triangles.append(tuple((north_m, -east_m, altitude) for east_m, north_m, altitude in enu))
    return triangles


def extract_sources_parallel(
    sources: list[Path],
    latitude: float,
    longitude: float,
    ns_m: float,
    ew_m: float,
    workers: int,
):
    """Extract independent DEM files concurrently while preserving source order."""
    if workers <= 1 or len(sources) <= 1:
        results = []
        for index, source in enumerate(sources, 1):
            print(
                "[HAKO_PROGRESS] " + json.dumps({
                    "phase": "terrain_extract", "current": index - 1,
                    "total": len(sources), "source": source.name,
                }, separators=(",", ":")),
                flush=True,
            )
            results.append(extract_triangles(source, latitude, longitude, ns_m, ew_m))
        return results

    results: list[list | None] = [None] * len(sources)
    effective_workers = min(workers, len(sources))
    with concurrent.futures.ProcessPoolExecutor(max_workers=effective_workers) as executor:
        futures = {
            executor.submit(
                extract_triangles, source, latitude, longitude, ns_m, ew_m,
            ): (index, source)
            for index, source in enumerate(sources)
        }
        pending = set(futures)
        completed = 0
        while pending:
            done, pending = concurrent.futures.wait(
                pending,
                timeout=5.0,
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            if not done:
                print(
                    "[HAKO_PROGRESS] " + json.dumps({
                        "phase": "terrain_extract", "current": completed,
                        "total": len(sources), "message": "DEM source extraction in progress",
                    }, separators=(",", ":")),
                    flush=True,
                )
                continue
            for future in done:
                index, source = futures[future]
                results[index] = future.result()
                completed += 1
                print(
                    "[HAKO_PROGRESS] " + json.dumps({
                        "phase": "terrain_extract", "current": completed,
                        "total": len(sources), "source": source.name,
                        "triangle_count": len(results[index]),
                    }, separators=(",", ":")),
                    flush=True,
                )
    return [result if result is not None else [] for result in results]


def source_paths(source: Path) -> list[Path]:
    if source.is_file():
        return [source]
    if source.is_dir():
        paths = sorted(source.rglob("*dem*_op.gml"))
        if paths:
            return paths
    raise DemError(f"no DEM CityGML source (*dem*_op.gml) found: {source}")


def _barycentric_height(x: float, y: float, triangle, epsilon: float = 1e-8):
    (x1, y1, z1), (x2, y2, z2), (x3, y3, z3) = triangle
    denominator = (y2 - y3) * (x1 - x3) + (x3 - x2) * (y1 - y3)
    if abs(denominator) <= epsilon:
        return None
    a = ((y2 - y3) * (x - x3) + (x3 - x2) * (y - y3)) / denominator
    b = ((y3 - y1) * (x - x3) + (x1 - x3) * (y - y3)) / denominator
    c = 1.0 - a - b
    if a < -epsilon or b < -epsilon or c < -epsilon:
        return None
    return a * z1 + b * z2 + c * z3


def _neighbor_offsets(
    max_distance_m: float,
    row_spacing_m: float,
    col_spacing_m: float,
):
    row_radius = math.floor(max_distance_m / row_spacing_m + 1e-9)
    col_radius = math.floor(max_distance_m / col_spacing_m + 1e-9)
    offsets = []
    for row_delta in range(-row_radius, row_radius + 1):
        for col_delta in range(-col_radius, col_radius + 1):
            distance = math.hypot(
                row_delta * row_spacing_m,
                col_delta * col_spacing_m,
            )
            if distance <= max_distance_m + 1e-9:
                offsets.append((distance, row_delta, col_delta))
    return sorted(offsets)


def _fill_small_gaps(
    samples,
    missing,
    nrow: int,
    ncol: int,
    row_spacing_m: float,
    col_spacing_m: float,
    max_distance_m: float,
):
    """Fill from the four nearest original samples within a bounded grid radius."""
    original = list(samples)
    offsets = _neighbor_offsets(max_distance_m, row_spacing_m, col_spacing_m)
    maximum_fill_distance = 0.0
    for completed, index in enumerate(missing, 1):
        row, col = divmod(index, ncol)
        nearest = []
        for distance, row_delta, col_delta in offsets:
            candidate_row = row + row_delta
            candidate_col = col + col_delta
            if not (0 <= candidate_row < nrow and 0 <= candidate_col < ncol):
                continue
            candidate = candidate_row * ncol + candidate_col
            if math.isfinite(original[candidate]):
                nearest.append((distance, candidate))
                if len(nearest) == 4:
                    break
        if not nearest:
            continue
        distance = nearest[0][0]
        maximum_fill_distance = max(maximum_fill_distance, distance)
        if distance == 0:
            samples[index] = original[nearest[0][1]]
        else:
            weights = [(1.0 / item[0], original[item[1]]) for item in nearest]
            samples[index] = sum(weight * value for weight, value in weights) / sum(
                weight for weight, _ in weights
            )
        if completed % 10000 == 0:
            print(
                "[HAKO_PROGRESS] " + json.dumps({
                    "phase": "terrain_gap_fill", "current": completed,
                    "total": len(missing),
                }, separators=(",", ":")),
                flush=True,
            )
    return maximum_fill_distance


def sample_heightfield(
    triangles,
    ns_m: float,
    ew_m: float,
    spacing_m: float,
    max_gap_fill_distance_m: float = 0.0,
    uncovered_policy: str = "error",
    uncovered_elevation_m: float = 0.0,
):
    if uncovered_policy not in {"error", "constant"}:
        raise ValueError("uncovered_policy must be error or constant")
    if not math.isfinite(uncovered_elevation_m):
        raise ValueError("uncovered_elevation_m must be finite")
    # A browser-drawn selection is not generally divisible by the requested
    # spacing. Preserve the exact bbox and choose enough intervals that the
    # effective spacing never becomes coarser than the configured maximum.
    ncol = max(1, math.ceil((2.0 * ns_m) / spacing_m - 1e-12)) + 1
    nrow = max(1, math.ceil((2.0 * ew_m) / spacing_m - 1e-12)) + 1
    col_spacing_m = (2.0 * ns_m) / (ncol - 1)
    row_spacing_m = (2.0 * ew_m) / (nrow - 1)
    samples = [math.nan] * (nrow * ncol)
    for triangle in triangles:
        xs = [point[0] for point in triangle]
        ys = [point[1] for point in triangle]
        col_first = max(0, math.ceil((min(xs) + ns_m) / col_spacing_m - 1e-9))
        col_last = min(ncol - 1, math.floor((max(xs) + ns_m) / col_spacing_m + 1e-9))
        row_first = max(0, math.ceil((min(ys) + ew_m) / row_spacing_m - 1e-9))
        row_last = min(nrow - 1, math.floor((max(ys) + ew_m) / row_spacing_m + 1e-9))
        for row in range(row_first, row_last + 1):
            y = -ew_m + row * row_spacing_m
            for col in range(col_first, col_last + 1):
                x = -ns_m + col * col_spacing_m
                height = _barycentric_height(x, y, triangle)
                if height is not None:
                    samples[row * ncol + col] = height
    missing = [index for index, value in enumerate(samples) if not math.isfinite(value)]
    gap_report = {
        "source_missing_samples": len(missing),
        "maximum_fill_distance_m": 0.0,
        "uncovered_policy": uncovered_policy,
        "constant_filled_samples": 0,
        "constant_fill_elevation_m": (
            uncovered_elevation_m if uncovered_policy == "constant" else None
        ),
        "effective_spacing_m": {
            "north_south": col_spacing_m,
            "east_west": row_spacing_m,
        },
    }
    if missing and max_gap_fill_distance_m > 0:
        print(
            "[HAKO_PROGRESS] " + json.dumps({
                "phase": "terrain_gap_fill", "current": 0, "total": len(missing),
            }, separators=(",", ":")),
            flush=True,
        )
        gap_report["maximum_fill_distance_m"] = _fill_small_gaps(
            samples, missing, nrow, ncol,
            row_spacing_m, col_spacing_m, max_gap_fill_distance_m,
        )
        missing = [index for index, value in enumerate(samples) if not math.isfinite(value)]
    gap_report["remaining_after_nearby_fill_samples"] = len(missing)
    if missing and uncovered_policy == "constant":
        for index in missing:
            samples[index] = uncovered_elevation_m
        gap_report["constant_filled_samples"] = len(missing)
        missing = []
    if missing:
        coordinates = [
            (
                -ns_m + (index % ncol) * col_spacing_m,
                -ew_m + (index // ncol) * row_spacing_m,
            )
            for index in missing[:12]
        ]
        raise DemError(
            f"height field has {len(missing)} uncovered samples; "
            f"first MuJoCo (X,Y) coordinates={coordinates}"
        )
    return nrow, ncol, samples, gap_report


# --- Carving the DEM with the LOD3 road surfaces ------------------------------------------
#
# PLATEAU's DEM does not follow roads cut into the ground (Shinjuku's sunken
# roads under its pedestrian bridges: the DEM fills them up to the ground
# around, up to about 6 m above the measured road), so roads draped on it rose
# into the bridges and cars could not pass. Where a LOD3 road surface (its
# traffic and auxiliary traffic areas, measured in 3D) lies below the DEM, the
# hfield samples under it are lowered to the road. Roads above the DEM
# (viaducts) leave the ground as it is, and a road far below it (deeper than
# max_depth_m: a tunnel, an underpass) is left out and counted.

TRAN_LOD3 = "{http://www.opengis.net/citygml/transportation/2.0}lod3MultiSurface"
POLYGON = f"{{{GML}}}Polygon"


def road_source_paths(source: Path) -> list[Path]:
    if source.is_file():
        return [source] if "_tran_" in source.name else []
    return sorted(source.rglob("*_tran_*_op.gml")) if source.is_dir() else []


def extract_road_triangles(paths: list[Path], latitude: float, longitude: float, ns_m: float, ew_m: float):
    """The LOD3 road surfaces near the window as MuJoCo-frame triangles (X=North, Y=-East, Z=altitude)."""
    from citygml2glb import GlbError, _polygon_rings, triangulate_rings

    west, south, east, north = geographic_bounds(latitude, longitude, ns_m, ew_m)
    triangles = []
    for path in paths:
        epsg = 6697
        with path.open("rb") as stream:
            head = stream.read(65536).decode("utf-8", "replace")
        name = re.search(r'srsName="([^"]+)"', head)
        found = srs_code(name.group(1)) if name else None
        if found in SUPPORTED_CRS:
            epsg = found
        depth = 0
        for event, element in ET.iterparse(path, events=("start", "end")):
            if element.tag == TRAN_LOD3:
                depth += 1 if event == "start" else -1
                continue
            if event != "end" or element.tag != POLYGON:
                continue
            if depth:
                rings = _polygon_rings(element)
                if rings:
                    lats = [point[0] for _, ring in rings for point in ring]
                    lons = [point[1] for _, ring in rings for point in ring]
                    if not (max(lats) < south or min(lats) > north or max(lons) < west or min(lons) > east):
                        local = [[(n, -e, z) for e, n, z in project_to_local_enu(ring, latitude, longitude, epsg)]
                                 for _, ring in rings]
                        try:
                            vertices, faces = triangulate_rings(local)
                        except (GlbError, ValueError):
                            pass
                        else:
                            triangles.extend(tuple(tuple(float(v) for v in vertices[i]) for i in face) for face in faces)
            element.clear()
    return triangles


def carve_by_roads(samples, nrow: int, ncol: int, ns_m: float, ew_m: float, road_triangles,
                   tolerance_m: float = 0.2, max_depth_m: float = 8.0) -> dict:
    """Lower the samples under the road triangles that lie below the DEM (in place); a report."""
    col_spacing_m = (2.0 * ns_m) / (ncol - 1)
    row_spacing_m = (2.0 * ew_m) / (nrow - 1)
    lowest: dict[int, float] = {}
    too_deep: set[int] = set()
    for triangle in road_triangles:
        xs = [point[0] for point in triangle]
        ys = [point[1] for point in triangle]
        col_first = max(0, math.ceil((min(xs) + ns_m) / col_spacing_m - 1e-9))
        col_last = min(ncol - 1, math.floor((max(xs) + ns_m) / col_spacing_m + 1e-9))
        row_first = max(0, math.ceil((min(ys) + ew_m) / row_spacing_m - 1e-9))
        row_last = min(nrow - 1, math.floor((max(ys) + ew_m) / row_spacing_m + 1e-9))
        for row in range(row_first, row_last + 1):
            y = -ew_m + row * row_spacing_m
            for col in range(col_first, col_last + 1):
                height = _barycentric_height(-ns_m + col * col_spacing_m, y, triangle)
                if height is None:
                    continue
                index = row * ncol + col
                depth = samples[index] - height
                if depth <= tolerance_m:
                    continue
                if depth > max_depth_m:
                    too_deep.add(index)
                    continue
                lowest[index] = min(lowest.get(index, height), height)
    depths = []
    for index, height in lowest.items():
        depths.append(samples[index] - height)
        samples[index] = height
    too_deep -= set(lowest)
    return {
        "policy": "lower the DEM to LOD3 road surfaces below it",
        "road_triangle_count": len(road_triangles),
        "tolerance_m": tolerance_m,
        "max_depth_m": max_depth_m,
        "carved_sample_count": len(depths),
        "max_carved_depth_m": max(depths, default=0.0),
        "mean_carved_depth_m": sum(depths) / len(depths) if depths else 0.0,
        "skipped_deeper_sample_count": len(too_deep),
    }


# --- Lowering the DEM under bridges (optional, inferred) -------------------------------------
#
# The DEM also fills the space under some bridges up to their deck (around
# Shinjuku: a road bridge on 都庁通り stands on a DEM bank at its deck height,
# the lower street beside it 5 m down). Nothing measures the ground there, so
# this is an inference, off unless asked: under a bridge's floor, samples the
# DEM holds within near_m of the deck are lowered to the low ground just
# outside the bridge (a low percentile of the samples ring_m around it), by at
# most max_depth_m. Cars on the bridge stand on its deck collision.


def bridge_floor_triangles(source: Path, latitude: float, longitude: float, ns_m: float, ew_m: float):
    """{bridge id: [MuJoCo-frame triangles (X=North, Y=-East, Z=altitude)]} of the bridges' floors
    (LOD3, else LOD2 OuterFloorSurface; bridge2mjcf's selection)."""
    from bridge2mjcf import extract_prisms

    paths = sorted(source.rglob("*brid*_op.gml")) if source.is_dir() else []
    if not paths:
        return {}
    frame = {"origin": {"latitude": latitude, "longitude": longitude, "altitude_offset_m": 0.0},
             "half_extent_m": {"north_south": ns_m, "east_west": ew_m}}
    pieces, _boundary, _counts = extract_prisms(source, frame, 0.02, 60.0)
    floors: dict[str, list] = {}
    seen: set = set()  # a floor read twice (the same tile in two source folders) counts once
    for piece in pieces:
        triangle = tuple(tuple(v) for v in piece["source_vertices"])
        key = (piece["bridge_id"], tuple(sorted(tuple(round(v, 4) for v in point) for point in triangle)))
        if key not in seen:
            seen.add(key)
            floors.setdefault(piece["bridge_id"], []).append(triangle)
    return floors


def carve_under_bridges(samples, nrow: int, ncol: int, ns_m: float, ew_m: float, floors: dict,
                        near_m: float = 1.0, ring_m: float = 6.0, max_depth_m: float = 8.0,
                        tolerance_m: float = 0.2) -> dict:
    """Lower the DEM under the bridges' floors to the low ground around them (in place); a report."""
    col_spacing_m = (2.0 * ns_m) / (ncol - 1)
    row_spacing_m = (2.0 * ew_m) / (nrow - 1)
    reach_cols = max(1, round(ring_m / col_spacing_m))
    reach_rows = max(1, round(ring_m / row_spacing_m))
    report = {"policy": "lower the DEM under bridge floors to the low ground around them (inferred)",
              "near_m": near_m, "ring_m": ring_m, "max_depth_m": max_depth_m, "bridges": {}}
    lowered_total = 0
    for bridge, triangles in sorted(floors.items()):
        deck: dict[int, float] = {}
        for triangle in triangles:
            xs = [point[0] for point in triangle]
            ys = [point[1] for point in triangle]
            col_first = max(0, math.ceil((min(xs) + ns_m) / col_spacing_m - 1e-9))
            col_last = min(ncol - 1, math.floor((max(xs) + ns_m) / col_spacing_m + 1e-9))
            row_first = max(0, math.ceil((min(ys) + ew_m) / row_spacing_m - 1e-9))
            row_last = min(nrow - 1, math.floor((max(ys) + ew_m) / row_spacing_m + 1e-9))
            for row in range(row_first, row_last + 1):
                for col in range(col_first, col_last + 1):
                    height = _barycentric_height(-ns_m + col * col_spacing_m, -ew_m + row * row_spacing_m, triangle)
                    if height is not None:
                        index = row * ncol + col
                        deck[index] = min(deck.get(index, height), height)
        if not deck:
            continue
        ring = set()
        for index in deck:
            row, col = divmod(index, ncol)
            for r in range(max(0, row - reach_rows), min(nrow, row + reach_rows + 1)):
                for c in range(max(0, col - reach_cols), min(ncol, col + reach_cols + 1)):
                    if r * ncol + c not in deck:
                        ring.add(r * ncol + c)
        if not ring:
            continue
        around = sorted(samples[index] for index in ring)
        target = around[len(around) // 5]  # the low ground around (20th percentile)
        lowered = []
        for index, height in deck.items():
            current = samples[index]
            if current < height - near_m or current - target <= tolerance_m:
                continue  # clear of the deck already, or no lower ground around
            new = max(target, current - max_depth_m)
            lowered.append(current - new)
            samples[index] = new
        lowered_total += len(lowered)
        report["bridges"][bridge] = {"samples_under": len(deck), "lowered": len(lowered),
                                     "ground_around_m": target, "max_lowered_m": max(lowered, default=0.0)}
    report["lowered_sample_count"] = lowered_total
    return report


# --- Joining the DEM to the bridges' edges -------------------------------------------------
#
# Where a bridge's floor meets the ground (its ends, or its sides on an
# embankment) the DEM steps up or down to it, and a car cannot drive on or
# off. Along each floor's outer edges, the samples near the edge take the
# edge's height (the LOD3 / LOD2 floor is measured) and ease back into the DEM
# over blend_m, when the ground just outside is within max_step_m of the edge;
# a side high above a road below is left alone. Under the floor, the samples
# within ABUTMENT_M of such an edge are filled up to just under the floor (an
# abutment), so no gap is left under a bridge's end; further in, the ground
# under the floor is left as it is. Optional.

UNDER_FLOOR_GAP_M = 0.05  # the ground filled under a floor stays this far below it
ABUTMENT_M = 2.0


def _floor_boundary(triangles) -> list:
    """The outer edges of a floor's triangles (those on one triangle only), as
    (a, b, c): the edge's 3D points and its triangle's third point (inside)."""
    edges: dict = {}
    for triangle in triangles:
        for i in range(3):
            a, b, c = triangle[i], triangle[(i + 1) % 3], triangle[(i + 2) % 3]
            key = tuple(sorted((tuple(round(v, 4) for v in a), tuple(round(v, 4) for v in b))))
            edges.setdefault(key, []).append((a, b, c))
    return [pair[0] for pair in edges.values() if len(pair) == 1]


def _bilinear(samples, nrow: int, ncol: int, ns_m: float, ew_m: float, x: float, y: float) -> float:
    col_spacing_m = (2.0 * ns_m) / (ncol - 1)
    row_spacing_m = (2.0 * ew_m) / (nrow - 1)
    c = min(max((x + ns_m) / col_spacing_m, 0.0), ncol - 1.0)
    r = min(max((y + ew_m) / row_spacing_m, 0.0), nrow - 1.0)
    c0, r0 = min(int(c), ncol - 2), min(int(r), nrow - 2)
    fc, fr = c - c0, r - r0
    at = lambda row, col: samples[row * ncol + col]
    return ((at(r0, c0) * (1 - fc) + at(r0, c0 + 1) * fc) * (1 - fr)
            + (at(r0 + 1, c0) * (1 - fc) + at(r0 + 1, c0 + 1) * fc) * fr)


def blend_to_bridge_edges(samples, nrow: int, ncol: int, ns_m: float, ew_m: float, floors: dict,
                          blend_m: float = 6.0, max_step_m: float = 2.5, tolerance_m: float = 0.05,
                          keep=frozenset(), reference=None) -> dict:
    """Ease the DEM to the bridges' floor edges where the ground meets them (in place); a report.

    Every point along a floor's outer edges (every 0.5 m) whose ground 1 m
    outside (the DEM as it was) is within max_step_m of it is joined. A sample
    outside every floor takes the nearest joined point's height, eased back to
    its own over blend_m, when its own height is within max_step_m of that
    point (a road on another level stays). A sample under a floor within
    ABUTMENT_M of one of its joined points is filled to just under it.
    Samples in `keep` (lowered to a road surface) are left as they are.

    Under a floor that is within max_step_m of the ground (`reference`: the
    DEM before carve_under_bridges; a car cannot pass under it), every sample
    is seated: filled or lowered to just under the floor, so the floor lies
    on the ground with no pit or gap beside or under it."""
    original = list(samples)
    reference = original if reference is None else list(reference)
    col_spacing_m = (2.0 * ns_m) / (ncol - 1)
    row_spacing_m = (2.0 * ew_m) / (nrow - 1)
    reach_cols = max(1, math.ceil(blend_m / col_spacing_m))
    reach_rows = max(1, math.ceil(blend_m / row_spacing_m))
    report = {"policy": "ease the DEM to the bridges' floor edges where the ground meets them",
              "blend_m": blend_m, "max_step_m": max_step_m, "abutment_m": ABUTMENT_M, "bridges": {}}
    under: dict[int, set] = {}  # sample -> the bridges over it
    lowest_floor: dict[int, float] = {}  # sample -> the lowest floor over it
    seat: dict[int, tuple[float, str]] = {}  # sample -> (the floor it lies under, near the ground; its bridge)
    for bridge, triangles in floors.items():
        for triangle in triangles:
            xs = [point[0] for point in triangle]
            ys = [point[1] for point in triangle]
            for row in range(max(0, math.ceil((min(ys) + ew_m) / row_spacing_m - 1e-9)),
                             min(nrow - 1, math.floor((max(ys) + ew_m) / row_spacing_m + 1e-9)) + 1):
                for col in range(max(0, math.ceil((min(xs) + ns_m) / col_spacing_m - 1e-9)),
                                 min(ncol - 1, math.floor((max(xs) + ns_m) / col_spacing_m + 1e-9)) + 1):
                    height = _barycentric_height(-ns_m + col * col_spacing_m, -ew_m + row * row_spacing_m, triangle)
                    if height is None:
                        continue
                    index = row * ncol + col
                    under.setdefault(index, set()).add(bridge)
                    lowest_floor[index] = min(height, lowest_floor.get(index, math.inf))
                    if abs(height - reference[index]) <= max_step_m and (
                            index not in seat or abs(height - reference[index]) < abs(seat[index][0] - reference[index])):
                        seat[index] = (height, bridge)
    nearest: dict[int, tuple[float, float, str]] = {}  # sample -> (distance, joined point's height, bridge)
    for bridge, triangles in sorted(floors.items()):
        joined = left = 0
        for a, b, c in _floor_boundary(triangles):
            dx, dy = b[0] - a[0], b[1] - a[1]
            length = math.hypot(dx, dy)
            if length < 1e-6:
                continue
            nx, ny = dy / length, -dx / length
            if (c[0] - a[0]) * nx + (c[1] - a[1]) * ny > 0:  # the normal points into the floor: turn it
                nx, ny = -nx, -ny
            steps = max(1, math.ceil(length / 0.5))
            for step in range(steps + 1):
                t = step / steps
                px, py, pz = a[0] + dx * t, a[1] + dy * t, a[2] + (b[2] - a[2]) * t
                if abs(px) > ns_m or abs(py) > ew_m:
                    continue
                ground = _bilinear(reference, nrow, ncol, ns_m, ew_m, px + nx, py + ny)
                if abs(ground - pz) > max_step_m:
                    left += 1
                    continue
                joined += 1
                col0 = round((px + ns_m) / col_spacing_m)
                row0 = round((py + ew_m) / row_spacing_m)
                for row in range(max(0, row0 - reach_rows), min(nrow, row0 + reach_rows + 1)):
                    for col in range(max(0, col0 - reach_cols), min(ncol, col0 + reach_cols + 1)):
                        index = row * ncol + col
                        if index in keep:
                            continue
                        distance = math.hypot(-ns_m + col * col_spacing_m - px, -ew_m + row * row_spacing_m - py)
                        if distance > blend_m or (index in nearest and nearest[index][0] <= distance):
                            continue
                        over = under.get(index)
                        if over is not None and bridge in over and distance > ABUTMENT_M:
                            continue  # under its own floor: only the abutment
                        if (over is None or bridge not in over) and abs(reference[index] - pz) > max_step_m:
                            continue  # ground on another level (its own abutment is filled whatever is below)
                        nearest[index] = (distance, pz, bridge)
        if joined or left:
            report["bridges"][bridge] = {"edge_points_joined": joined, "edge_points_left": left,
                                         "samples_changed": 0, "max_change_m": 0.0}
    for index, (height, bridge) in seat.items():
        seated = height - UNDER_FLOOR_GAP_M
        if abs(seated - original[index]) <= tolerance_m:
            continue
        entry = report["bridges"].setdefault(bridge, {"edge_points_joined": 0, "edge_points_left": 0,
                                                      "samples_changed": 0, "max_change_m": 0.0})
        entry["samples_changed"] += 1
        entry["max_change_m"] = max(entry["max_change_m"], abs(seated - original[index]))
        samples[index] = seated
    report["samples_seated"] = len(seat)
    for index, (distance, edge_z, bridge) in nearest.items():
        if index in seat:
            continue
        if index in under and bridge in under[index]:  # the abutment: only ever raised, to just under the floor
            eased = edge_z - UNDER_FLOOR_GAP_M
            if eased <= original[index] + tolerance_m:
                continue
        elif index in under:  # under another (higher) floor, beside this one's joined edge: raised, never above it
            eased = min(edge_z + (original[index] - edge_z) * (distance / blend_m),
                        lowest_floor[index] - UNDER_FLOOR_GAP_M)
            if eased <= original[index] + tolerance_m:
                continue
        else:
            eased = edge_z + (original[index] - edge_z) * (distance / blend_m)
            if abs(eased - original[index]) <= tolerance_m:
                continue
        entry = report["bridges"][bridge]
        entry["samples_changed"] += 1
        entry["max_change_m"] = max(entry["max_change_m"], abs(eased - original[index]))
        samples[index] = eased
    report["samples_changed"] = sum(item["samples_changed"] for item in report["bridges"].values())
    return report


def write_hfield(path: Path, nrow: int, ncol: int, samples) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        stream.write(struct.pack("<ii", nrow, ncol))
        stream.write(struct.pack(f"<{len(samples)}f", *samples))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_mjcf(path: Path, hfield_path: Path, nrow: int, ncol: int, samples, ns_m: float, ew_m: float):
    minimum = min(samples)
    maximum = max(samples)
    # MuJoCo needs a positive elevation range; flat ground (no DEM) gets a
    # token 1 mm range and all-equal samples, so its surface stays at z = 0.
    elevation = max(maximum - minimum, 0.001)
    relative = Path(hfield_path.name)
    text = f'''<mujoco model="plateau_terrain_probe">
  <asset>
    <hfield name="plateau_terrain" file="{relative}" size="{ns_m:.6f} {ew_m:.6f} {elevation:.6f} 1.0"/>
  </asset>
  <worldbody>
    <geom name="plateau_ground" type="hfield" hfield="plateau_terrain" pos="0 0 0" rgba="0.55 0.55 0.55 1"/>
  </worldbody>
</mujoco>
'''
    path.write_text(text, encoding="utf-8")
    return minimum, maximum


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--in", dest="source", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True, help="output MJCF path")
    parser.add_argument("--latitude", type=float, required=True)
    parser.add_argument("--longitude", type=float, required=True)
    parser.add_argument("--north-south", type=float, default=100.0)
    parser.add_argument("--east-west", type=float, default=100.0)
    parser.add_argument("--spacing", type=float, default=2.0)
    parser.add_argument(
        "--uncovered-policy", choices=("error", "constant"), default="error",
        help="handling for samples still uncovered after nearby gap fill (default: error)",
    )
    parser.add_argument(
        "--uncovered-elevation", type=float, default=0.0,
        help="elevation in metres used by --uncovered-policy constant (default: 0)",
    )
    parser.add_argument(
        "--allow-missing-dem", action="store_true",
        help="with --uncovered-policy constant, build flat ground when there is no DEM at all "
             "(hako.py passes it only for local CityGML without a DEM)",
    )
    parser.add_argument(
        "--workers", type=int, default=min(2, os.cpu_count() or 1),
        help="parallel DEM source extraction processes (default: up to 2)",
    )
    parser.add_argument("--no-road-carve", action="store_true",
                        help="keep the DEM as it is under LOD3 roads below it (default: lower it to the roads)")
    parser.add_argument("--road-carve-max-depth", type=float, default=8.0,
                        help="lower the DEM by at most this much (a road deeper is a tunnel or an underpass)")
    parser.add_argument("--road-carve-tolerance", type=float, default=0.2,
                        help="leave the DEM when a road is less than this below it")
    parser.add_argument("--bridge-blend", action="store_true",
                        help="ease the DEM to the bridges' floor edges where the ground meets them (default off)")
    parser.add_argument("--bridge-blend-distance", type=float, default=6.0,
                        help="metres over which the DEM eases back from a floor edge (default 6)")
    parser.add_argument("--bridge-carve", action="store_true",
                        help="also lower the DEM under bridges to the low ground around them (inferred; default off)")
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be at least 1")
    # Retain neighboring TIN triangles beyond the sampled rectangle. The
    # geographic bbox is only a discovery guard; exact clipping happens in
    # local MuJoCo coordinates during grid sampling.
    extraction_margin = 2.0 * args.spacing
    try:
        sources = source_paths(args.source)
    except DemError:
        # Data without a DEM (e.g. CityGML converted from OpenStreetMap): flat
        # ground, only when asked for; a PLATEAU build missing its DEM fails.
        if not (args.allow_missing_dem and args.uncovered_policy == "constant"):
            raise
        sources = []
        print("INFO: no DEM source; the terrain is flat at the uncovered elevation")
    extracted = [] if not sources else extract_sources_parallel(
        sources,
        args.latitude,
        args.longitude,
        args.north_south + extraction_margin,
        args.east_west + extraction_margin,
        args.workers,
    )
    triangles = [triangle for result in extracted for triangle in result]
    if not triangles and sources:
        raise DemError("PLATEAU DEM contains no triangle intersecting the requested range")
    nrow, ncol, samples, gap_report = sample_heightfield(
        triangles,
        args.north_south,
        args.east_west,
        args.spacing,
        max_gap_fill_distance_m=20.0,
        uncovered_policy=args.uncovered_policy,
        uncovered_elevation_m=args.uncovered_elevation,
    )
    road_carving = {"policy": "off"}
    before_roads = list(samples)
    if sources and not args.no_road_carve:
        roads = road_source_paths(args.source)
        road_triangles = extract_road_triangles(
            roads, args.latitude, args.longitude,
            args.north_south + extraction_margin, args.east_west + extraction_margin,
        ) if roads else []
        road_carving = carve_by_roads(
            samples, nrow, ncol, args.north_south, args.east_west, road_triangles,
            args.road_carve_tolerance, args.road_carve_max_depth,
        )
        road_carving["sources"] = [str(path.resolve()) for path in roads]
        print(f"OK: road carving: {road_carving['carved_sample_count']} samples lowered "
              f"(max {road_carving['max_carved_depth_m']:.2f} m), "
              f"{road_carving['skipped_deeper_sample_count']} deeper left out")
    bridge_carving = {"policy": "off"}
    before_bridges = list(samples)
    if sources and args.bridge_carve:
        floors = bridge_floor_triangles(args.source, args.latitude, args.longitude, args.north_south, args.east_west)
        bridge_carving = carve_under_bridges(samples, nrow, ncol, args.north_south, args.east_west, floors)
        print(f"OK: bridge carving: {bridge_carving['lowered_sample_count']} samples lowered under "
              f"{len(bridge_carving['bridges'])} bridges")
    bridge_blend = {"policy": "off"}
    if sources and args.bridge_blend:
        floors = bridge_floor_triangles(args.source, args.latitude, args.longitude, args.north_south, args.east_west)
        on_roads = frozenset(index for index, (was, now) in enumerate(zip(before_roads, samples)) if now < was)
        bridge_blend = blend_to_bridge_edges(samples, nrow, ncol, args.north_south, args.east_west, floors,
                                             args.bridge_blend_distance, keep=on_roads, reference=before_bridges)
        print(f"OK: bridge edges: {bridge_blend['samples_changed']} samples eased to the floor edges of "
              f"{len(bridge_blend['bridges'])} bridges")
    hfield = args.out.with_suffix(".hf")
    digest = write_hfield(hfield, nrow, ncol, samples)
    minimum, maximum = write_mjcf(
        args.out, hfield, nrow, ncol, samples, args.north_south, args.east_west
    )
    receipt = {
        "schema_version": 1,
        "sources": [str(path.resolve()) for path in sources],
        "center": {"latitude": args.latitude, "longitude": args.longitude},
        "half_extent_m": {"north_south": args.north_south, "east_west": args.east_west},
        "coordinate_system": "X=North,Y=-East,Z=Up",
        "spacing_m": args.spacing,
        "effective_spacing_m": gap_report["effective_spacing_m"],
        "triangle_extraction_margin_m": extraction_margin,
        "parallel_workers": max(1, min(args.workers, len(sources))),
        "dem": "available" if sources else "not_available",
        "nrow": nrow,
        "ncol": ncol,
        "triangle_count": len(triangles),
        "gap_fill": gap_report,
        "road_carving": road_carving,
        "bridge_carving": bridge_carving,
        "bridge_blend": bridge_blend,
        "minimum_altitude_m": minimum,
        "maximum_altitude_m": maximum,
        "altitude_offset_m": minimum,
        "hfield": {"path": str(hfield.resolve()), "sha256": digest},
        "mjcf": str(args.out.resolve()),
    }
    receipt_path = args.out.with_name(args.out.stem + "-receipt.json")
    world_frame_path = args.out.with_name("world-frame.json")
    receipt["world_frame"] = str(world_frame_path.resolve())
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    write_world_frame(world_frame_path, create_world_frame(receipt))
    print(f"OK: {nrow}x{ncol} hfield, triangles={len(triangles)}, altitude={minimum:.3f}..{maximum:.3f}")
    print(f"OK: MJCF: {args.out}")
    print(f"OK: receipt: {receipt_path}")
    print(f"OK: world frame: {world_frame_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
