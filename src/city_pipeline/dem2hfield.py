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
import struct
import xml.etree.ElementTree as ET
from pathlib import Path

from geodesy import project_epsg6697_to_local_enu
from world_frame import create_world_frame, write_world_frame

GML = "http://www.opengis.net/gml"
EXPECTED_CRS = "http://www.opengis.net/def/crs/EPSG/0/6697"


class DemError(RuntimeError):
    pass


def geographic_bounds(latitude: float, longitude: float, ns_m: float, ew_m: float):
    lat_delta = ns_m / 111_320.0
    lon_delta = ew_m / (111_320.0 * math.cos(math.radians(latitude)))
    return longitude - lon_delta, latitude - lat_delta, longitude + lon_delta, latitude + lat_delta


def _dem_header(path: Path, west: float, south: float, east: float, north: float):
    """Validate CRS, return the document GML prefix and bbox intersection."""
    gml_prefix = None
    envelope_seen = False
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
                if element.get("srsName") != EXPECTED_CRS or element.get("srsDimension") != "3":
                    raise DemError("PLATEAU DEM must use three-dimensional EPSG:6697")
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
            return gml_prefix, intersects
    if not envelope_seen:
        raise DemError("PLATEAU DEM has no CRS envelope")
    raise DemError("PLATEAU DEM has an invalid CRS envelope")


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
    gml_prefix, intersects = _dem_header(path, west, south, east, north)
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
        enu = project_epsg6697_to_local_enu(points, latitude, longitude)
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
    raise DemError(f"no PLATEAU DEM CityGML source found: {source}")


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


def write_hfield(path: Path, nrow: int, ncol: int, samples) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        stream.write(struct.pack("<ii", nrow, ncol))
        stream.write(struct.pack(f"<{len(samples)}f", *samples))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_mjcf(path: Path, hfield_path: Path, nrow: int, ncol: int, samples, ns_m: float, ew_m: float):
    minimum = min(samples)
    maximum = max(samples)
    elevation = maximum - minimum
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
        "--workers", type=int, default=min(2, os.cpu_count() or 1),
        help="parallel DEM source extraction processes (default: up to 2)",
    )
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be at least 1")
    # Retain neighboring TIN triangles beyond the sampled rectangle. The
    # geographic bbox is only a discovery guard; exact clipping happens in
    # local MuJoCo coordinates during grid sampling.
    extraction_margin = 2.0 * args.spacing
    sources = source_paths(args.source)
    extracted = extract_sources_parallel(
        sources,
        args.latitude,
        args.longitude,
        args.north_south + extraction_margin,
        args.east_west + extraction_margin,
        args.workers,
    )
    triangles = [triangle for result in extracted for triangle in result]
    if not triangles:
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
        "parallel_workers": min(args.workers, len(sources)),
        "nrow": nrow,
        "ncol": ncol,
        "triangle_count": len(triangles),
        "gap_fill": gap_report,
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
