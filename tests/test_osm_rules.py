"""The conversion rules of osm2citygml (src/city_pipeline/osm_rules.py) on
their own: which features are converted and what fills in what the map lacks.
The rules' tables are checked against docs/osm-to-citygml.md in
tests/test_osm2citygml.py (SpecTest)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "city_pipeline"))

import osm2citygml  # noqa: E402
import osm_rules  # noqa: E402
import osm_source  # noqa: E402


class WhatIsConvertedTest(unittest.TestCase):
    def test_buildings(self):
        self.assertTrue(osm_rules.is_building({"building": "yes"}))
        self.assertTrue(osm_rules.is_building({"building": "roof"}))
        self.assertFalse(osm_rules.is_building({"building": "no"}))
        self.assertFalse(osm_rules.is_building({"amenity": "parking"}))

    def test_roads(self):
        self.assertTrue(osm_rules.is_road({"highway": "residential"}))
        self.assertTrue(osm_rules.is_road({"highway": "primary", "bridge": "yes"}))
        for tags in ({"highway": "footway"}, {"highway": "steps"}, {"highway": "service", "area": "yes"},
                     {"highway": "primary", "tunnel": "yes"}, {"highway": "service", "tunnel": "building_passage"},
                     {"railway": "rail"}):
            with self.subTest(tags=tags):
                self.assertFalse(osm_rules.is_road(tags))


class FillingInTest(unittest.TestCase):
    def test_lengths_in_tags(self):
        self.assertEqual([osm_rules.parse_length(value) for value in (7, 2.5, "12 metres", "40'", True, "")],
                         [7.0, 2.5, 12.0, 40 * 0.3048, None, None])

    def test_building_height_from_the_height_then_levels_then_its_kind(self):
        self.assertEqual(osm_rules.building_height({"height": "21.5 m", "building:levels": "3"}), (21.5, "height"))
        self.assertEqual(osm_rules.building_height({"building:levels": "4"}), (4 * osm_rules.LEVEL_HEIGHT_M, "levels"))
        # A pitched roof adds a metre above the levels.
        self.assertEqual(osm_rules.building_height({"building:levels": "2", "roof:shape": "gabled"}),
                         (2 * osm_rules.LEVEL_HEIGHT_M + 1.0, "levels"))
        self.assertEqual(osm_rules.building_height({"building": "hospital"}),
                         (osm_rules.HEIGHTS_BY_KIND["hospital"], "default"))
        self.assertEqual(osm_rules.building_height({"building": "yes", "height": "tall"}),
                         (osm_rules.DEFAULT_HEIGHT_M, "default"))
        # Kept within the bounds.
        self.assertEqual(osm_rules.building_height({"height": "0.2"})[0], osm_rules.MIN_HEIGHT_M)
        self.assertEqual(osm_rules.building_height({"height": "900"})[0], osm_rules.MAX_HEIGHT_M)

    def test_building_base(self):
        self.assertEqual(osm_rules.building_base({"min_height": "4"}, 10.0), 4.0)
        self.assertEqual(osm_rules.building_base({"building:min_level": "2"}, 12.0), 2 * osm_rules.LEVEL_HEIGHT_M)
        self.assertEqual(osm_rules.building_base({"building": "roof"}, 6.0), 6.0 - osm_rules.ROOF_SLAB_M)
        self.assertEqual(osm_rules.building_base({}, 9.0), 0.0)
        # Never above the slab under its top, never below the ground.
        self.assertEqual(osm_rules.building_base({"min_height": "20"}, 9.0), 9.0 - osm_rules.ROOF_SLAB_M)
        self.assertEqual(osm_rules.building_base({"min_height": "-3"}, 9.0), 0.0)

    def test_road_size(self):
        self.assertEqual(osm_rules.road_size({"highway": "primary", "width": "11", "lanes": "3"}),
                         (11.0, 3, "width", "lanes"))
        self.assertEqual(osm_rules.road_size({"highway": "residential", "lanes": "1"}),
                         (osm_rules.LANE_WIDTH_M, 1, "lanes", "lanes"))
        self.assertEqual(osm_rules.road_size({"highway": "motorway"}),
                         (4 * osm_rules.LANE_WIDTH_M, 4, "default", "default"))
        self.assertEqual(osm_rules.road_size({"highway": "unknown_class"}),
                         (osm_rules.DEFAULT_LANES * osm_rules.LANE_WIDTH_M, osm_rules.DEFAULT_LANES, "default", "default"))
        self.assertEqual(osm_rules.road_size({"highway": "service", "width": "1"})[0], osm_rules.MIN_ROAD_WIDTH_M)
        self.assertEqual(osm_rules.road_size({"highway": "service", "width": "90"})[0], osm_rules.MAX_ROAD_WIDTH_M)


class ModulesTest(unittest.TestCase):
    def test_osm2citygml_keeps_the_names_of_the_split_modules(self):
        for name in ("HEIGHTS_BY_KIND", "LANES_BY_CLASS", "EXCLUDED_HIGHWAYS", "building_height", "road_size",
                     "parse_length"):
            self.assertIs(getattr(osm2citygml, name), getattr(osm_rules, name))
        for name in ("Box", "Feature", "OsmConversionError", "fetch_overpass", "overpass_query",
                     "features_from_overpass", "features_from_geojson", "geojson_box", "EPSG", "TOOL_VERSION"):
            self.assertIs(getattr(osm2citygml, name), getattr(osm_source, name))


if __name__ == "__main__":
    unittest.main()
