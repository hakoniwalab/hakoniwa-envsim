#!/usr/bin/env python3
"""The conversion rules of osm2citygml: which map features are buildings and
roads, and how what the map lacks is filled in (heights, bases, road widths
and lanes). docs/osm-to-citygml.md states these rules in tables, and
tests/test_osm2citygml.py checks the tables against the constants here: change
both together.
"""

from __future__ import annotations

import re

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


# --- Which features are converted ------------------------------------------------

def is_road(tags: dict) -> bool:
    highway = tags.get("highway")
    return bool(highway) and highway not in EXCLUDED_HIGHWAYS and tags.get("area") != "yes" \
        and tags.get("tunnel") not in ("yes", "building_passage")


def is_building(tags: dict) -> bool:
    return bool(tags.get("building")) and tags.get("building") != "no"


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
