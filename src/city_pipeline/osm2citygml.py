#!/usr/bin/env python3
"""Convert OpenStreetMap buildings and roads to LOD1 CityGML (EPSG:4326).

CityGML is Envsim's intermediate representation for city data: PLATEAU
delivers it directly (up to LOD2/LOD3 with measured heights), and this tool
turns coarser worldwide map data into the same form, so one pipeline builds
the City World from either. The conversion rules (what is read, how missing
heights and widths are filled, ids and provenance) are specified in
docs/osm-to-citygml.md; tests/test_osm2citygml.py checks that document's
tables against the constants.

The work is split in three modules: osm_source.py reads the map data (the
area, Overpass, GeoJSON), osm_rules.py holds the conversion rules (what is a
building or a road, the heights and widths filled in), and this module writes
the CityGML, the receipt and the build manifest (and keeps the other two
modules' names, for existing callers).

Output (CityGML 2.0, ``latitude longitude height`` in EPSG:4326):

* ``<name>_bldg_op.gml``: one ``bldg:Building`` per OSM building footprint
  (a ``lod1Solid`` from its base to its height; courtyards kept as holes);
* ``<name>_tran_op.gml``: one ``tran:Road`` per OSM road, its centre line
  widened to a ``lod1MultiSurface`` polygon;
* ``<name>-osm-receipt.json``: counts, what was skipped or assumed, and the
  provenance (© OpenStreetMap contributors, ODbL-1.0, data timestamp, query).

Heights are above the ground (OSM has no elevation); Envsim builds such data
on flat ground (``city_world.terrain_uncovered_policy: constant``). Features
are not clipped here: the pipeline applies its own selection rules.

    osm2citygml.py --bbox S,W,N,E --overpass --out-dir work/osm/tokyo
    osm2citygml.py --bbox S,W,N,E --osm-json data.json --out-dir DIR --build-manifest DIR/hakoniwa-build.yaml
    osm2citygml.py --geojson data.geojson --out-dir DIR
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import hashlib
import json
import math
from pathlib import Path
import re
import sys
from xml.sax.saxutils import escape, quoteattr

from shapely.geometry import LineString, Polygon
from shapely.geometry.polygon import orient

sys.path.insert(0, str(Path(__file__).resolve().parent))
from geodesy import local_enu_to_geodetic, project_to_local_enu  # noqa: E402
# Reading the map data and the conversion rules live in their own modules;
# their names are kept here too (osm2citygml.Box, osm2citygml.HEIGHTS_BY_KIND, ...).
from osm_rules import (  # noqa: E402,F401
    DEFAULT_HEIGHT_M, DEFAULT_LANES, EXCLUDED_HIGHWAYS, HEIGHTS_BY_KIND, LANE_WIDTH_M, LANES_BY_CLASS,
    LEVEL_HEIGHT_M, MAX_HEIGHT_M, MAX_ROAD_WIDTH_M, MIN_HEIGHT_M, MIN_ROAD_WIDTH_M, ROOF_SLAB_M,
    building_base, building_height, is_building, is_road, parse_length, road_size,
)
from osm_source import (  # noqa: E402,F401
    DEFAULT_OVERPASS, EPSG, OVERPASS_TIMEOUT_S, TOOL_VERSION, Box, Feature, OsmConversionError,
    features_from_geojson, features_from_overpass, fetch_overpass, geojson_box, overpass_query,
)

SRS_NAME = "http://www.opengis.net/def/crs/EPSG/0/4326"
ATTRIBUTION = "© OpenStreetMap contributors"
LICENSE = "ODbL-1.0"

# A sanity bound on the area converted at once, metres per side.
MAX_SIDE_M = 2000.0
# Points closer than this are merged; smaller buildings and shorter roads are dropped.
# Footprints are otherwise kept as drawn: simplifying each one on its own would
# move the walls neighbours share (OSM draws them with the same nodes) and
# make them overlap.
MIN_STEP_M = 0.001
# Road centre lines (the surface layer, where overlaps do not matter) are simplified to this.
ROAD_SIMPLIFY_M = 0.025
MIN_BUILDING_AREA_M2 = 4.0
MIN_ROAD_LENGTH_M = 1.0

# Tags kept with each feature (as generic attributes) for provenance.
KEPT_TAGS = ("building", "highway", "name", "height", "min_height", "building:levels", "building:min_level",
             "roof:shape", "lanes", "width", "oneway", "surface", "bridge", "tunnel", "layer")

NAMESPACES = {
    "core": "http://www.opengis.net/citygml/2.0",
    "bldg": "http://www.opengis.net/citygml/building/2.0",
    "tran": "http://www.opengis.net/citygml/transportation/2.0",
    "gen": "http://www.opengis.net/citygml/generics/2.0",
    "gml": "http://www.opengis.net/gml",
}


@dataclass
class Report:
    buildings: int = 0
    roads: int = 0
    skipped: list[dict] = field(default_factory=list)
    assumed: dict = field(default_factory=lambda: {"building_height": 0, "road_width": 0, "road_lanes": 0})
    notes: list[str] = field(default_factory=list)

    def skip(self, feature: Feature, reason: str) -> None:
        self.skipped.append({"source": f"{feature.source_kind}/{feature.source_id}", "kind": feature.kind,
                             "reason": reason})

    def as_json(self) -> dict:
        return {"buildings": self.buildings, "roads": self.roads, "skipped": self.skipped,
                "assumed": self.assumed, "notes": self.notes}


# --- Writing CityGML --------------------------------------------------------------

def _pos(points) -> str:
    return " ".join(f"{lat:.9f} {lon:.9f} {z:.3f}" for lat, lon, z in points)


def _ring_xml(points) -> str:
    closed = list(points) + [points[0]]
    return f"<gml:LinearRing><gml:posList>{_pos(closed)}</gml:posList></gml:LinearRing>"


def _polygon_xml(exterior, interiors=()) -> str:
    inner = "".join(f"<gml:interior>{_ring_xml(ring)}</gml:interior>" for ring in interiors)
    return f"<gml:Polygon><gml:exterior>{_ring_xml(exterior)}</gml:exterior>{inner}</gml:Polygon>"


_XML_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")


def _text(value) -> str:
    """Text for an XML element: escaped, without characters XML 1.0 forbids
    (OSM tags occasionally carry control characters)."""
    return escape(_XML_ILLEGAL.sub("", str(value)))


def _generic(name: str, value) -> str:
    return (f"<gen:stringAttribute name={quoteattr(_XML_ILLEGAL.sub('', name))}><gen:value>{_text(value)}</gen:value>"
            f"</gen:stringAttribute>")


def _attributes(feature: Feature, extra: dict) -> str:
    items = {"source_provider": feature.provider, "source_kind": feature.source_kind,
             "source_id": feature.source_id, **extra}
    items.update({f"osm:{key}": feature.tags[key] for key in KEPT_TAGS if key in feature.tags})
    return "".join(_generic(key, value) for key, value in items.items())


def _solid_xml(polygon: Polygon, base: float, top: float, to_geo) -> str:
    """A closed LOD1 solid: bottom (facing down), top (facing up) and a wall per edge."""
    shape = orient(polygon, sign=1.0)  # exterior counter-clockwise, holes clockwise

    def ring(coords, z):
        return to_geo([(x, y, z) for x, y in list(coords)[:-1]])

    rings = [shape.exterior.coords, *[hole.coords for hole in shape.interiors]]
    faces = [
        _polygon_xml(list(reversed(ring(rings[0], base))), [list(reversed(ring(r, base))) for r in rings[1:]]),
        _polygon_xml(ring(rings[0], top), [ring(r, top) for r in rings[1:]]),
    ]
    for coords in rings:
        points = list(coords)[:-1]
        for index, (x1, y1) in enumerate(points):
            x2, y2 = points[(index + 1) % len(points)]
            faces.append(_polygon_xml(to_geo([(x1, y1, base), (x2, y2, base), (x2, y2, top), (x1, y1, top)])))
    members = "".join(f"<gml:surfaceMember>{face}</gml:surfaceMember>" for face in faces)
    return (f"<bldg:lod1Solid><gml:Solid><gml:exterior><gml:CompositeSurface>{members}"
            f"</gml:CompositeSurface></gml:exterior></gml:Solid></bldg:lod1Solid>")


def _document(members: list[str], bounds: list[float] | None, box: Box, top: float) -> str:
    """A CityModel of the members; its envelope bounds the coordinates written
    (features are not clipped to the bbox), or the bbox when there are none."""
    south, west, north, east = bounds or (box.south, box.west, box.north, box.east)
    declarations = " ".join(f'xmlns:{prefix}="{uri}"' for prefix, uri in NAMESPACES.items())
    envelope = (f'<gml:boundedBy><gml:Envelope srsName="{SRS_NAME}" srsDimension="3">'
                f"<gml:lowerCorner>{south:.9f} {west:.9f} 0.000</gml:lowerCorner>"
                f"<gml:upperCorner>{north:.9f} {east:.9f} {top:.3f}</gml:upperCorner>"
                f"</gml:Envelope></gml:boundedBy>")
    body = "\n".join(f"<core:cityObjectMember>{member}</core:cityObjectMember>" for member in members)
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<core:CityModel {declarations}>\n{envelope}\n'
            f"{body}\n</core:CityModel>\n")


def _feature_id(feature: Feature) -> str:
    prefix = {"openstreetmap": "osm", "geojson": "geojson"}.get(feature.provider, "map")
    kind = {"way": "w", "relation": "r", "node": "n"}.get(feature.source_kind, "f")
    return re.sub(r"[^A-Za-z0-9_.-]", "_", f"{prefix}_{kind}{feature.source_id}")


def _cross(o, a, b) -> float:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _ring(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """A ring without repeated points (closer than MIN_STEP_M) or corners on a
    straight line; no other point moves, so shared walls stay shared."""
    ring = []
    for point in points:
        if not ring or math.dist(ring[-1], point) > MIN_STEP_M:
            ring.append(point)
    while len(ring) > 1 and math.dist(ring[0], ring[-1]) <= MIN_STEP_M:
        ring.pop()
    changed = True
    while changed and len(ring) > 3:
        changed = False
        for index in range(len(ring)):
            a, b, c = ring[index - 1], ring[index], ring[(index + 1) % len(ring)]
            if abs(_cross(a, b, c)) <= 1e-9 * max(1.0, math.dist(a, c) ** 2):
                del ring[index]
                changed = True
                break
    return ring if len(ring) >= 3 else points


def convert(features: list[Feature], box: Box) -> tuple[str, str, Report, float]:
    """(buildings CityGML, roads CityGML, report, highest top) of map features."""
    north_south, east_west = box.half_extent_m()
    if 2 * max(north_south, east_west) > MAX_SIDE_M:
        raise OsmConversionError(f"the area is at most {MAX_SIDE_M:.0f} m on a side "
                                 f"(got {2 * east_west:.0f} x {2 * north_south:.0f} m)")
    lat0, lon0 = box.center

    def to_local(points):
        return [(x, y) for x, y, _ in project_to_local_enu([(lat, lon, 0.0) for lat, lon in points], lat0, lon0, EPSG)]

    bounds = {"building": None, "road": None}  # [south, west, north, east] of what each file holds
    current = ["building"]

    def to_geo(points):
        found = local_enu_to_geodetic(points, lat0, lon0, EPSG)
        known = bounds[current[0]]
        lats, lons = [lat for lat, _, _ in found], [lon for _, lon, _ in found]
        box_now = [min(lats), min(lons), max(lats), max(lons)]
        bounds[current[0]] = box_now if known is None else [min(known[0], box_now[0]), min(known[1], box_now[1]),
                                                             max(known[2], box_now[2]), max(known[3], box_now[3])]
        return found

    report = Report()
    buildings, roads = [], []
    top_all = 0.0
    used: set[str] = set()

    def unique(base: str) -> str:
        candidate, number = base, 2
        while candidate in used:
            candidate, number = f"{base}_{number}", number + 1
        used.add(candidate)
        return candidate

    order = {"building": 0, "road": 1}
    for feature in sorted(features, key=lambda f: (order[f.kind], f.source_kind,
                                                   int(f.source_id) if f.source_id.isdigit() else 0, f.source_id)):
        base_id = _feature_id(feature)
        current[0] = feature.kind
        name = feature.tags.get("name")
        name_xml = f"<gml:name>{_text(name)}</gml:name>" if name else ""
        report.notes.extend(f"{feature.source_kind}/{feature.source_id}: {note}" for note in feature.notes)
        if feature.kind == "building":
            if not feature.polygons:
                report.skip(feature, "its outline is incomplete in the data")
                continue
            height, height_source = building_height(feature.tags)
            height = round(height, 3)
            base = round(building_base(feature.tags, height), 3)
            kept = 0
            for number, (outer, inners) in enumerate(feature.polygons, 1):
                shape = Polygon(_ring(to_local(outer)), [_ring(to_local(ring)) for ring in inners])
                if not shape.is_valid:
                    shape = shape.buffer(0)
                if shape.geom_type != "Polygon" or shape.is_empty:
                    report.skip(feature, "its outline is not a simple polygon")
                    continue
                if shape.area < MIN_BUILDING_AREA_M2:
                    report.skip(feature, f"smaller than {MIN_BUILDING_AREA_M2} m²")
                    continue
                building_id = unique(base_id if len(feature.polygons) == 1 else f"{base_id}_p{number}")
                levels = parse_length(feature.tags.get("building:levels"))
                storeys = f"<bldg:storeysAboveGround>{int(levels)}</bldg:storeysAboveGround>" if levels else ""
                attributes = _attributes(feature, {"height_source": height_source, "base_m": f"{base:g}"})
                buildings.append(
                    f'<bldg:Building gml:id="{building_id}">{name_xml}{attributes}'
                    f'<bldg:measuredHeight uom="m">{height:g}</bldg:measuredHeight>{storeys}'
                    f"{_solid_xml(shape, base, height, to_geo)}</bldg:Building>")
                if inners and not shape.interiors:
                    report.notes.append(f"{feature.source_kind}/{feature.source_id}: courtyards were too small to keep")
                kept += 1
                top_all = max(top_all, height)
            if kept:
                report.buildings += kept
                report.assumed["building_height"] += int(height_source != "height") * kept
        else:
            width, lanes, width_source, lanes_source = road_size(feature.tags)
            kept = 0
            for number, line in enumerate(feature.lines, 1):
                path = LineString(to_local(line)).simplify(ROAD_SIMPLIFY_M)
                if path.length < MIN_ROAD_LENGTH_M:
                    continue
                area = path.buffer(width / 2, cap_style="flat", join_style="round")
                pieces = [area] if area.geom_type == "Polygon" else list(getattr(area, "geoms", []))
                surfaces = "".join(
                    "<gml:surfaceMember>" + _polygon_xml(
                        to_geo([(x, y, 0.0) for x, y in list(orient(piece, 1.0).exterior.coords)[:-1]]),
                        [to_geo([(x, y, 0.0) for x, y in list(hole.coords)[:-1]])
                         for hole in orient(piece, 1.0).interiors]) + "</gml:surfaceMember>"
                    for piece in pieces if piece.area > 0)
                road_id = unique(base_id if len(feature.lines) == 1 else f"{base_id}_{number}")
                attributes = _attributes(feature, {
                    "width_m": f"{width:g}", "lanes": lanes, "width_source": width_source,
                    "lanes_source": lanes_source})
                roads.append(f'<tran:Road gml:id="{road_id}">{name_xml}{attributes}'
                             f"<tran:lod1MultiSurface><gml:MultiSurface>{surfaces}</gml:MultiSurface>"
                             f"</tran:lod1MultiSurface></tran:Road>")
                kept += 1
            if not kept:
                report.skip(feature, f"shorter than {MIN_ROAD_LENGTH_M} m or incomplete in the data")
                continue
            report.roads += kept
            report.assumed["road_width"] += int(width_source != "width") * kept
            report.assumed["road_lanes"] += int(lanes_source != "lanes") * kept
    return (_document(buildings, bounds["building"], box, top_all), _document(roads, bounds["road"], box, 0.0),
            report, top_all)


def build_manifest(box: Box, source_dir: Path, name: str) -> str:
    """A hakoniwa-build.yaml that builds a flat-ground City World from the files."""
    north_south, east_west = box.half_extent_m()
    lat, lon = box.center
    return f"""version: 1
component: hakoniwa-envsim

pipeline:
  type: plateau-citygml-to-assets

source:
  kind: files
  path: {source_dir.resolve().as_posix()}
  feature_type: bldg
  feature_types:
    bldg: true
    tran: true
    dem: false
    frn: false
    brid: false

selection:
  center:
    latitude: {lat:.9f}
    longitude: {lon:.9f}
  half_extent_m:
    north_south: {north_south}
    east_west: {east_west}

glb:
  texture_mode: flat

city_world:
  enabled: true
  terrain_uncovered_policy: constant
  terrain_uncovered_elevation_m: 0

output:
  build_dir: {(source_dir.resolve() / 'build').as_posix()}
  install_dir: {(source_dir.resolve() / 'install').as_posix()}
  name: {name}
"""


def run(box: Box | None, out_dir: Path, name: str = "osm", *, overpass: bool = False,
        osm_json: dict | None = None, geojson: dict | None = None, endpoint: str | None = None,
        manifest: Path | None = None) -> dict:
    """Convert one source and write the CityGML files and the receipt; returns the receipt."""
    if geojson is not None:
        box = box or geojson_box(geojson)
        features, provider, data, timestamp, query = features_from_geojson(geojson), "geojson", geojson, None, None
    else:
        if box is None:
            raise OsmConversionError("--bbox is required for OpenStreetMap data")
        data = osm_json if osm_json is not None else fetch_overpass(box, endpoint) if overpass else None
        if data is None:
            raise OsmConversionError("give one source: --overpass, --osm-json or --geojson")
        features, provider = features_from_overpass(data), "openstreetmap"
        timestamp = (data.get("osm3s") or {}).get("timestamp_osm_base")
        query = overpass_query(box) if overpass else None
    buildings_gml, roads_gml, report, top = convert(features, box)
    if not report.buildings:
        raise OsmConversionError("no building in the area: nothing to build")
    out_dir.mkdir(parents=True, exist_ok=True)
    files = {"bldg": out_dir / f"{name}_bldg_op.gml"}
    files["bldg"].write_text(buildings_gml, encoding="utf-8")
    if report.roads:
        files["tran"] = out_dir / f"{name}_tran_op.gml"
        files["tran"].write_text(roads_gml, encoding="utf-8")
    data_bytes = json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")
    north_south, east_west = box.half_extent_m()
    receipt = {
        "schema_version": 1,
        "tool": f"osm2citygml {TOOL_VERSION}",
        "provider": provider,
        "source_crs": f"EPSG:{EPSG}",
        "bbox_deg": box.as_json(),
        "selection": {"center": {"latitude": box.center[0], "longitude": box.center[1]},
                      "half_extent_m": {"north_south": north_south, "east_west": east_west}},
        "files": {kind: {"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                  for kind, path in files.items()},
        "data_sha256": hashlib.sha256(data_bytes).hexdigest(),
        "maximum_height_m": top,
        **report.as_json(),
    }
    if provider == "openstreetmap":
        receipt.update(attribution=ATTRIBUTION, license=LICENSE)
    if timestamp:
        receipt["data_timestamp"] = timestamp
    if query:
        receipt["query"] = query
    (out_dir / f"{name}-osm-receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if manifest is not None:
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(build_manifest(box, out_dir, name), encoding="utf-8")
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bbox", help="south,west,north,east in degrees")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--overpass", action="store_true", help="fetch OpenStreetMap data from the Overpass API")
    source.add_argument("--osm-json", type=Path, help="an Overpass JSON file")
    source.add_argument("--geojson", type=Path, help="a GeoJSON FeatureCollection")
    parser.add_argument("--endpoint", help=f"Overpass API URL (default {DEFAULT_OVERPASS} or HAKONIWA_OVERPASS_URL)")
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--name", default="osm", help="file name prefix (default osm)")
    parser.add_argument("--save-data", type=Path, help="also keep the map data as fetched")
    parser.add_argument("--build-manifest", type=Path, help="also write a hakoniwa-build.yaml for these files")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", args.name):
        parser.error("--name may contain only letters, digits, dot, underscore and hyphen")
    read = lambda path: json.loads(path.read_text(encoding="utf-8"))  # noqa: E731
    try:
        box = Box.parse(args.bbox) if args.bbox else None
        osm_json = read(args.osm_json) if args.osm_json else None
        geojson = read(args.geojson) if args.geojson else None
        if args.overpass:
            osm_json = fetch_overpass(box, args.endpoint) if box else None
        receipt = run(box, args.out_dir, args.name, overpass=args.overpass, osm_json=osm_json,
                      geojson=geojson, endpoint=args.endpoint, manifest=args.build_manifest)
        if args.save_data and (osm_json or geojson) is not None:
            args.save_data.parent.mkdir(parents=True, exist_ok=True)
            args.save_data.write_text(json.dumps(osm_json or geojson, ensure_ascii=False), encoding="utf-8")
    except (OsmConversionError, OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"OK: {receipt['buildings']} buildings, {receipt['roads']} roads -> {args.out_dir}"
          f" (skipped {len(receipt['skipped'])}; heights assumed {receipt['assumed']['building_height']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
