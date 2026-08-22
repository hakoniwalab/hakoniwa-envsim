#!/usr/bin/env python3
"""Extract a query-centered PLATEAU DEM window as a MuJoCo height field."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
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


def extract_triangles(path: Path, latitude: float, longitude: float, ns_m: float, ew_m: float):
    west, south, east, north = geographic_bounds(latitude, longitude, ns_m, ew_m)
    triangles = []
    envelope_seen = False
    for event, element in ET.iterparse(path, events=("start", "end")):
        if event == "start" and element.tag == f"{{{GML}}}Envelope" and not envelope_seen:
            envelope_seen = True
            if element.get("srsName") != EXPECTED_CRS or element.get("srsDimension") != "3":
                raise DemError("PLATEAU DEM must use three-dimensional EPSG:6697")
        if event != "end" or element.tag != f"{{{GML}}}posList":
            continue
        values = [float(value) for value in (element.text or "").split()]
        element.clear()
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
    if not envelope_seen:
        raise DemError("PLATEAU DEM has no CRS envelope")
    if not triangles:
        raise DemError("PLATEAU DEM contains no triangle intersecting the requested range")
    return triangles


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


def sample_heightfield(
    triangles,
    ns_m: float,
    ew_m: float,
    spacing_m: float,
    max_gap_fill_distance_m: float = 0.0,
):
    ncol = round((2.0 * ns_m) / spacing_m) + 1
    nrow = round((2.0 * ew_m) / spacing_m) + 1
    if not math.isclose((ncol - 1) * spacing_m, 2.0 * ns_m, abs_tol=1e-8):
        raise DemError("north/south extent must be divisible by grid spacing")
    if not math.isclose((nrow - 1) * spacing_m, 2.0 * ew_m, abs_tol=1e-8):
        raise DemError("east/west extent must be divisible by grid spacing")
    samples = [math.nan] * (nrow * ncol)
    for triangle in triangles:
        xs = [point[0] for point in triangle]
        ys = [point[1] for point in triangle]
        col_first = max(0, math.ceil((min(xs) + ns_m) / spacing_m - 1e-9))
        col_last = min(ncol - 1, math.floor((max(xs) + ns_m) / spacing_m + 1e-9))
        row_first = max(0, math.ceil((min(ys) + ew_m) / spacing_m - 1e-9))
        row_last = min(nrow - 1, math.floor((max(ys) + ew_m) / spacing_m + 1e-9))
        for row in range(row_first, row_last + 1):
            y = -ew_m + row * spacing_m
            for col in range(col_first, col_last + 1):
                x = -ns_m + col * spacing_m
                height = _barycentric_height(x, y, triangle)
                if height is not None:
                    samples[row * ncol + col] = height
    missing = [index for index, value in enumerate(samples) if not math.isfinite(value)]
    gap_report = {"source_missing_samples": len(missing), "maximum_fill_distance_m": 0.0}
    if missing and max_gap_fill_distance_m > 0:
        valid = [index for index, value in enumerate(samples) if math.isfinite(value)]
        original = list(samples)
        for index in missing:
            row, col = divmod(index, ncol)
            nearest = sorted(
                (
                    (math.hypot(row - (candidate // ncol), col - (candidate % ncol)) * spacing_m, candidate)
                    for candidate in valid
                ),
                key=lambda item: item[0],
            )[:4]
            distance = nearest[0][0]
            gap_report["maximum_fill_distance_m"] = max(
                gap_report["maximum_fill_distance_m"], distance
            )
            if distance > max_gap_fill_distance_m:
                continue
            if distance == 0:
                samples[index] = original[nearest[0][1]]
            else:
                weights = [(1.0 / item[0], original[item[1]]) for item in nearest]
                samples[index] = sum(weight * value for weight, value in weights) / sum(
                    weight for weight, _ in weights
                )
        missing = [index for index, value in enumerate(samples) if not math.isfinite(value)]
    if missing:
        coordinates = [
            (
                -ns_m + (index % ncol) * spacing_m,
                -ew_m + (index // ncol) * spacing_m,
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
    args = parser.parse_args()
    # Retain neighboring TIN triangles beyond the sampled rectangle. The
    # geographic bbox is only a discovery guard; exact clipping happens in
    # local MuJoCo coordinates during grid sampling.
    extraction_margin = 2.0 * args.spacing
    triangles = extract_triangles(
        args.source,
        args.latitude,
        args.longitude,
        args.north_south + extraction_margin,
        args.east_west + extraction_margin,
    )
    nrow, ncol, samples, gap_report = sample_heightfield(
        triangles,
        args.north_south,
        args.east_west,
        args.spacing,
        max_gap_fill_distance_m=20.0,
    )
    hfield = args.out.with_suffix(".hf")
    digest = write_hfield(hfield, nrow, ncol, samples)
    minimum, maximum = write_mjcf(
        args.out, hfield, nrow, ncol, samples, args.north_south, args.east_west
    )
    receipt = {
        "schema_version": 1,
        "source": str(args.source.resolve()),
        "center": {"latitude": args.latitude, "longitude": args.longitude},
        "half_extent_m": {"north_south": args.north_south, "east_west": args.east_west},
        "coordinate_system": "X=North,Y=-East,Z=Up",
        "spacing_m": args.spacing,
        "triangle_extraction_margin_m": extraction_margin,
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
