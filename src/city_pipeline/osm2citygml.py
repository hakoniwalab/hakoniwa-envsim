#!/usr/bin/env python3
"""Convert OpenStreetMap buildings and roads to LOD1 CityGML (EPSG:4326).

CityGML is Envsim's intermediate representation for city data: PLATEAU
delivers it directly (up to LOD2/LOD3 with measured heights), and this tool
turns coarser worldwide map data into the same form, so one pipeline builds
the City World from either. The conversion rules (what is read, how missing
heights and widths are filled, ids and provenance) are specified in
docs/osm-to-citygml.md; tests/test_osm2citygml.py checks that document's
tables against the constants here.

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
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from xml.sax.saxutils import escape, quoteattr

from shapely.geometry import LineString, Polygon
from shapely.geometry.polygon import orient

sys.path.insert(0, str(Path(__file__).resolve().parent))
from geodesy import local_enu_to_geodetic, project_to_local_enu  # noqa: E402

TOOL_VERSION = "1"
EPSG = 4326
SRS_NAME = "http://www.opengis.net/def/crs/EPSG/0/4326"
DEFAULT_OVERPASS = "https://overpass-api.de/api/interpreter"
OVERPASS_TIMEOUT_S = 90
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

# Heights: a building's height tag, else levels x this, else by its kind.
LEVEL_HEIGHT_M = 3.0
# A roof without walls (building=roof: a canopy) becomes a slab this thick under its height.
ROOF_SLAB_M = 0.5
DEFAULT_HEIGHT_M = 9.0
MIN_HEIGHT_M, MAX_HEIGHT_M = 1.0, 500.0
HEIGHTS_BY_KIND = {
    "house": 6.0, "detached": 6.0, "semidetached_house": 6.0, "terrace": 6.0, "bungalow": 4.0,
    "residential": 9.0, "apartments": 15.0, "dormitory": 12.0, "hotel": 20.0,
    "commercial": 12.0, "office": 15.0, "retail": 6.0, "supermarket": 6.0,
    "industrial": 8.0, "warehouse": 8.0, "factory": 10.0,
    "school": 12.0, "university": 15.0, "hospital": 18.0, "public": 12.0, "civic": 12.0,
    "church": 12.0, "temple": 8.0, "shrine": 6.0,
    "garage": 3.0, "garages": 3.0, "carport": 3.0, "shed": 3.0, "hut": 3.0, "kiosk": 3.0, "roof": 4.0,
}
# Roads: lanes (both directions) by highway class; width = width tag, else lanes x LANE_WIDTH_M.
LANE_WIDTH_M = 3.25
DEFAULT_LANES = 2
LANES_BY_CLASS = {
    "motorway": 4, "trunk": 4, "primary": 2, "secondary": 2, "tertiary": 2, "unclassified": 2,
    "residential": 2, "living_street": 1, "service": 1, "road": 2,
    "motorway_link": 1, "trunk_link": 1, "primary_link": 1, "secondary_link": 1, "tertiary_link": 1,
}
MIN_ROAD_WIDTH_M, MAX_ROAD_WIDTH_M = 2.5, 60.0
EXCLUDED_HIGHWAYS = {
    "footway", "path", "cycleway", "steps", "pedestrian", "track", "bridleway", "corridor", "platform",
    "construction", "proposed", "abandoned", "bus_stop", "elevator", "via_ferrata", "raceway", "escape",
    "bus_guideway", "services", "rest_area",
}
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


class OsmConversionError(RuntimeError):
    pass


@dataclass(frozen=True)
class Box:
    south: float
    west: float
    north: float
    east: float

    @staticmethod
    def parse(text: str) -> "Box":
        try:
            south, west, north, east = (float(part) for part in text.split(","))
        except ValueError as exc:
            raise OsmConversionError(f"bbox must be south,west,north,east in degrees: {text!r}") from exc
        return Box.of(south, west, north, east)

    @staticmethod
    def of(south, west, north, east) -> "Box":
        box = Box(float(south), float(west), float(north), float(east))
        if not (-90 <= box.south < box.north <= 90 and -180 <= box.west < box.east <= 180):
            raise OsmConversionError(f"bbox must have south < north within ±90 and west < east within ±180: {box}")
        return box

    @property
    def center(self) -> tuple[float, float]:
        return (self.south + self.north) / 2, (self.west + self.east) / 2

    def half_extent_m(self) -> tuple[float, float]:
        """(north_south, east_west) half sizes in metres at the centre."""
        lat0, lon0 = self.center
        (north_edge,), (east_edge,) = (
            project_to_local_enu([(self.north, lon0, 0.0)], lat0, lon0, EPSG),
            project_to_local_enu([(lat0, self.east, 0.0)], lat0, lon0, EPSG),
        )
        return round(north_edge[1], 3), round(east_edge[0], 3)

    def as_json(self) -> dict:
        return {"south": self.south, "west": self.west, "north": self.north, "east": self.east}


@dataclass
class Feature:
    """One map feature in degrees: a building's polygons or a road's lines."""

    kind: str  # "building" or "road"
    provider: str
    source_kind: str  # way, relation, feature
    source_id: str
    tags: dict
    polygons: list[tuple[list, list[list]]] = field(default_factory=list)  # [(outer, [inner, ...])] of (lat, lon)
    lines: list[list] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # what reading it left out


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


# --- Reading map data -------------------------------------------------------------

def _is_road(tags: dict) -> bool:
    highway = tags.get("highway")
    return bool(highway) and highway not in EXCLUDED_HIGHWAYS and tags.get("area") != "yes" \
        and tags.get("tunnel") not in ("yes", "building_passage")


def _is_building(tags: dict) -> bool:
    return bool(tags.get("building")) and tags.get("building") != "no"


def _rings(lines: list[list], notes: list[str] | None = None) -> list[list]:
    """Join way pieces end to end into closed rings (multipolygon members);
    pieces that close no ring are left out (and noted)."""
    lines = [list(line) for line in lines if line]
    rings = []
    while lines:
        ring = lines.pop(0)
        while ring[0] != ring[-1]:
            for index, line in enumerate(lines):
                if line[0] == ring[-1]:
                    ring += line[1:]
                elif line[-1] == ring[-1]:
                    ring += list(reversed(line))[1:]
                else:
                    continue
                del lines[index]
                break
            else:
                break  # an open ring: left out
        if ring[0] == ring[-1] and len(ring) >= 4:
            rings.append(ring)
        elif notes is not None:
            notes.append("a ring that does not close was left out")
    return rings


def _nest(outers: list[list], inners: list[list], notes: list[str] | None = None) -> list[tuple[list, list[list]]]:
    """Each inner ring under the outer ring that contains it (lat, lon rings);
    an inner ring inside no outer ring is left out (and noted)."""
    shapes = [Polygon([(lon, lat) for lat, lon in outer]) for outer in outers]
    nested = [(outer, []) for outer in outers]
    for inner in inners:
        point = Polygon([(lon, lat) for lat, lon in inner]).representative_point()
        for index, shape in enumerate(shapes):
            if shape.is_valid and shape.contains(point):
                nested[index][1].append(inner)
                break
        else:
            if notes is not None:
                notes.append("an inner ring inside no outer ring was left out")
    return nested


def features_from_overpass(data: dict) -> list[Feature]:
    """Buildings (closed ways, multipolygon relations with their courtyards) and
    roads (highway ways) from Overpass API JSON (``out body; >; out skel qt;``)."""
    if not isinstance(data, dict) or not isinstance(data.get("elements"), list):
        raise OsmConversionError("Overpass JSON must have an elements list")
    nodes, ways = {}, {}
    for element in data["elements"]:
        if element.get("type") == "node" and "lat" in element:
            nodes[element["id"]] = (element["lat"], element["lon"])
        elif element.get("type") == "way":
            ways[element["id"]] = element

    def points(way):
        found = [nodes.get(node) for node in way.get("nodes", [])]
        return None if any(point is None for point in found) else found

    features = []
    for element in data["elements"]:
        tags = element.get("tags") or {}
        if element.get("type") == "way":
            line = points(element)
            if _is_building(tags):
                feature = Feature("building", "openstreetmap", "way", str(element["id"]), tags)
                if line is not None and len(line) >= 4 and line[0] == line[-1]:
                    feature.polygons = [(line, [])]
                features.append(feature)
            elif _is_road(tags):
                features.append(Feature("road", "openstreetmap", "way", str(element["id"]), tags,
                                        lines=[line] if line else []))
        elif element.get("type") == "relation" and _is_building(tags) and tags.get("type") == "multipolygon":
            members = [m for m in element.get("members", []) if m.get("type") == "way" and m.get("ref") in ways]
            notes: list[str] = []
            outer = _rings([points(ways[m["ref"]]) for m in members if m.get("role") == "outer"], notes)
            inner = _rings([points(ways[m["ref"]]) for m in members if m.get("role") == "inner"], notes)
            features.append(Feature("building", "openstreetmap", "relation", str(element["id"]), tags,
                                    polygons=_nest(outer, inner, notes), notes=notes))
    return features


def features_from_geojson(data: dict) -> list[Feature]:
    """Buildings (Polygon / MultiPolygon with a building property) and roads
    (LineString / MultiLineString with a highway property); GeoJSON gives
    coordinates as [lon, lat]."""
    if not isinstance(data, dict) or data.get("type") != "FeatureCollection":
        raise OsmConversionError("GeoJSON must be a FeatureCollection")
    features = []
    for index, item in enumerate(data.get("features") or []):
        geometry = item.get("geometry") or {}
        tags = {key: value for key, value in (item.get("properties") or {}).items()
                if isinstance(value, (str, int, float)) and not isinstance(value, bool)}
        raw_id = item.get("id", tags.get("@id", tags.get("id", index)))
        match = re.fullmatch(r"(way|relation|node)/(\d+)", str(raw_id))
        source_kind, source_id = (match.group(1), match.group(2)) if match else ("feature", str(raw_id))
        kind = geometry.get("type")

        def latlon(coords):
            return [(float(lat), float(lon)) for lon, lat, *_ in coords]

        try:
            if _is_building(tags) and kind in ("Polygon", "MultiPolygon"):
                polygons = [geometry["coordinates"]] if kind == "Polygon" else geometry["coordinates"]
                features.append(Feature("building", "geojson", source_kind, source_id, tags, polygons=[
                    (latlon(polygon[0]), [latlon(ring) for ring in polygon[1:]]) for polygon in polygons if polygon]))
            elif _is_road(tags) and kind in ("LineString", "MultiLineString"):
                lines = [geometry["coordinates"]] if kind == "LineString" else geometry["coordinates"]
                features.append(Feature("road", "geojson", source_kind, source_id, tags,
                                        lines=[latlon(line) for line in lines]))
        except (KeyError, TypeError, ValueError) as exc:
            raise OsmConversionError(f"GeoJSON feature {index} has malformed coordinates: {exc}") from exc
    return features


def geojson_box(data: dict) -> Box:
    """The bbox of a FeatureCollection (its own bbox, else every coordinate)."""
    if isinstance(data.get("bbox"), list) and len(data["bbox"]) == 4:
        west, south, east, north = data["bbox"]
        return Box.of(south, west, north, east)
    lats, lons = [], []

    def walk(coords):
        if coords and isinstance(coords[0], (int, float)):
            lons.append(coords[0])
            lats.append(coords[1])
        else:
            for item in coords:
                walk(item)

    for item in data.get("features") or []:
        walk((item.get("geometry") or {}).get("coordinates") or [])
    if not lats:
        raise OsmConversionError("the GeoJSON has no coordinates to take the area from; pass --bbox")
    return Box.of(min(lats), min(lons), max(lats), max(lons))


def overpass_query(box: Box) -> str:
    area = f"{box.south},{box.west},{box.north},{box.east}"
    return (f"[out:json][timeout:{OVERPASS_TIMEOUT_S - 10}];\n"
            f"(\n  way[\"building\"]({area});\n  relation[\"building\"][\"type\"=\"multipolygon\"]({area});\n"
            f"  way[\"highway\"]({area});\n);\nout body;\n>;\nout skel qt;")


def fetch_overpass(box: Box, endpoint: str | None = None) -> dict:
    """Buildings and roads in the bbox from an Overpass API instance
    (HAKONIWA_OVERPASS_URL or ``endpoint`` instead of the public one)."""
    endpoint = endpoint or os.environ.get("HAKONIWA_OVERPASS_URL") or DEFAULT_OVERPASS
    request = Request(endpoint, data=urlencode({"data": overpass_query(box)}).encode(), method="POST", headers={
        "User-Agent": f"hakoniwa-envsim-osm2citygml/{TOOL_VERSION}",
        "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urlopen(request, timeout=OVERPASS_TIMEOUT_S) as response:
            return json.loads(response.read())
    except (OSError, ValueError) as exc:
        raise OsmConversionError(f"cannot get map data from {endpoint}: {exc}") from exc


# --- Filling in what the map lacks ------------------------------------------------

_LENGTH = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*(m|meters?|metres?|ft|feet|')?\s*$", re.IGNORECASE)


def parse_length(value) -> float | None:
    """'12', '12 m', '12,5m', "40'" / '40 ft' (feet) -> metres; None when unreadable."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    match = _LENGTH.match(str(value or "").replace(",", "."))
    if not match:
        return None
    number = float(match.group(1))
    return number * 0.3048 if (match.group(2) or "").lower() in ("ft", "feet", "'") else number


def building_height(tags: dict) -> tuple[float, str]:
    """(height, where it came from: height / levels / default)."""
    height = parse_length(tags.get("height"))
    if height and height > 0:
        source, value = "height", height
    else:
        levels = parse_length(tags.get("building:levels"))
        if levels and levels > 0:
            roof = 1.0 if tags.get("roof:shape") not in (None, "flat") else 0.0
            source, value = "levels", levels * LEVEL_HEIGHT_M + roof
        else:
            source, value = "default", HEIGHTS_BY_KIND.get(str(tags.get("building")), DEFAULT_HEIGHT_M)
    return min(max(value, MIN_HEIGHT_M), MAX_HEIGHT_M), source


def building_base(tags: dict, height: float) -> float:
    """Where a building starts above the ground: min_height, else its lowest
    level, else just under its roof for a canopy (building=roof); else 0."""
    base = parse_length(tags.get("min_height"))
    if base is None:
        levels = parse_length(tags.get("building:min_level"))
        base = levels * LEVEL_HEIGHT_M if levels is not None else None
    if base is None and tags.get("building") == "roof":
        base = height - ROOF_SLAB_M
    return max(0.0, min(base or 0.0, height - ROOF_SLAB_M))


def road_size(tags: dict) -> tuple[float, int, str, str]:
    """(width, lanes, width source, lanes source) of a road."""
    lanes = parse_length(tags.get("lanes"))
    if lanes and lanes >= 1:
        lanes, lanes_source = int(lanes), "lanes"
    else:
        lanes, lanes_source = LANES_BY_CLASS.get(str(tags.get("highway")), DEFAULT_LANES), "default"
    width = parse_length(tags.get("width"))
    if width and width > 0:
        width_source = "width"
    else:
        width, width_source = lanes * LANE_WIDTH_M, "lanes" if lanes_source == "lanes" else "default"
    return min(max(width, MIN_ROAD_WIDTH_M), MAX_ROAD_WIDTH_M), lanes, width_source, lanes_source


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
