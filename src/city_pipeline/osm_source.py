#!/usr/bin/env python3
"""Reading OpenStreetMap data for osm2citygml: the area (Box), the Overpass
query and fetch, and the features (buildings' polygons with their courtyards,
roads' centre lines) read from Overpass JSON or GeoJSON, in degrees.

What counts as a building or a road is a conversion rule (osm_rules.py);
turning features into CityGML is osm2citygml.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from shapely.geometry import Polygon

sys.path.insert(0, str(Path(__file__).resolve().parent))
from geodesy import project_to_local_enu  # noqa: E402
from osm_rules import is_building, is_road  # noqa: E402

# The converter's version (osm2citygml's receipts; the Overpass User-Agent).
TOOL_VERSION = "1"
# OpenStreetMap and GeoJSON coordinates: WGS 84 latitude / longitude.
EPSG = 4326
DEFAULT_OVERPASS = "https://overpass-api.de/api/interpreter"
OVERPASS_TIMEOUT_S = 90


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
            if is_building(tags):
                feature = Feature("building", "openstreetmap", "way", str(element["id"]), tags)
                if line is not None and len(line) >= 4 and line[0] == line[-1]:
                    feature.polygons = [(line, [])]
                features.append(feature)
            elif is_road(tags):
                features.append(Feature("road", "openstreetmap", "way", str(element["id"]), tags,
                                        lines=[line] if line else []))
        elif element.get("type") == "relation" and is_building(tags) and tags.get("type") == "multipolygon":
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
            if is_building(tags) and kind in ("Polygon", "MultiPolygon"):
                polygons = [geometry["coordinates"]] if kind == "Polygon" else geometry["coordinates"]
                features.append(Feature("building", "geojson", source_kind, source_id, tags, polygons=[
                    (latlon(polygon[0]), [latlon(ring) for ring in polygon[1:]]) for polygon in polygons if polygon]))
            elif is_road(tags) and kind in ("LineString", "MultiLineString"):
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
