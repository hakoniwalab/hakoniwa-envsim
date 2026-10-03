#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import math
import struct
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "src" / "city_pipeline" / "dem2hfield.py"
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("dem2hfield", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
dem = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dem)


class DemToHeightfieldTest(unittest.TestCase):
    def test_samples_planar_tin_in_mujoco_axes(self):
        # z = 10 + x + 2*y over X=North, Y=-East.
        triangles = [
            ((-1.0, -1.0, 7.0), (1.0, -1.0, 9.0), (1.0, 1.0, 13.0)),
            ((-1.0, -1.0, 7.0), (1.0, 1.0, 13.0), (-1.0, 1.0, 11.0)),
        ]
        nrow, ncol, samples, gaps = dem.sample_heightfield(triangles, 1.0, 1.0, 1.0)
        self.assertEqual((nrow, ncol), (3, 3))
        self.assertEqual(samples, [7.0, 8.0, 9.0, 9.0, 10.0, 11.0, 11.0, 12.0, 13.0])
        self.assertEqual(gaps["source_missing_samples"], 0)

    def test_road_surfaces_below_the_dem_lower_it_within_the_depth_limit(self):
        # A 3 x 3 grid at 10 m; one road triangle over the whole window.
        def road(z):
            return [((-2.0, -2.0, z), (2.0, -2.0, z), (2.0, 2.0, z)), ((-2.0, -2.0, z), (2.0, 2.0, z), (-2.0, 2.0, z))]

        samples = [10.0] * 9
        report = dem.carve_by_roads(samples, 3, 3, 1.0, 1.0, road(6.0))  # a road cut 4 m into the DEM
        self.assertEqual(samples, [6.0] * 9)
        self.assertEqual((report["carved_sample_count"], round(report["max_carved_depth_m"], 3)), (9, 4.0))
        samples = [10.0] * 9
        dem.carve_by_roads(samples, 3, 3, 1.0, 1.0, road(12.0))  # a viaduct: the ground stays
        self.assertEqual(samples, [10.0] * 9)
        samples = [10.0] * 9
        report = dem.carve_by_roads(samples, 3, 3, 1.0, 1.0, road(-5.0))  # 15 m below: a tunnel, left out
        self.assertEqual(samples, [10.0] * 9)
        self.assertEqual((report["carved_sample_count"], report["skipped_deeper_sample_count"]), (0, 9))
        samples = [10.0] * 9
        dem.carve_by_roads(samples, 3, 3, 1.0, 1.0, road(9.9))  # within the tolerance: the DEM stays
        self.assertEqual(samples, [10.0] * 9)

    def test_the_dem_under_a_bridge_is_lowered_to_the_ground_around_it(self):
        # A 9 x 9 grid (1 m) at 5 m, a 10 m bank under a deck at 10 m over the middle 3 x 3.
        samples = [5.0] * 81
        for row in range(3, 6):
            for col in range(3, 6):
                samples[row * 9 + col] = 10.0
        deck = [((-1.2, -1.2, 10.0), (1.2, -1.2, 10.0), (1.2, 1.2, 10.0)),
                ((-1.2, -1.2, 10.0), (1.2, 1.2, 10.0), (-1.2, 1.2, 10.0))]
        report = dem.carve_under_bridges(samples, 9, 9, 4.0, 4.0, {"bridge-1": deck}, ring_m=2.0)
        self.assertEqual([samples[row * 9 + col] for row in range(3, 6) for col in range(3, 6)], [5.0] * 9)
        self.assertEqual(report["bridges"]["bridge-1"]["lowered"], 9)
        # Ground well below the deck already: left as it is.
        samples = [5.0] * 81
        dem.carve_under_bridges(samples, 9, 9, 4.0, 4.0, {"bridge-1": deck}, ring_m=2.0)
        self.assertEqual(samples, [5.0] * 81)

    def test_the_dem_eases_to_a_bridge_floors_edge(self):
        # A 21 x 21 grid (1 m) at 8 m; a deck at 10 m over x -3..3, y -3..3.
        def deck(z):
            return [((-3.0, -3.0, z), (3.0, -3.0, z), (3.0, 3.0, z)), ((-3.0, -3.0, z), (3.0, 3.0, z), (-3.0, 3.0, z))]

        samples = [8.0] * 441
        report = dem.blend_to_bridge_edges(samples, 21, 21, 10.0, 10.0, {"bridge-1": deck(10.0)}, blend_m=4.0)
        at = lambda x, y: samples[(y + 10) * 21 + (x + 10)]
        self.assertAlmostEqual(at(4, 0), 10.0 + (8.0 - 10.0) * 1 / 4)   # 1 m outside the edge: most of the way up
        self.assertAlmostEqual(at(6, 0), 10.0 + (8.0 - 10.0) * 3 / 4)   # 3 m outside: nearly the DEM
        self.assertEqual(at(8, 0), 8.0)                                 # beyond the blend: the DEM
        self.assertEqual(at(0, 0), 8.0)                                 # under the deck, away from its edges: not touched
        self.assertGreater(report["samples_changed"], 0)
        # A deck high above the ground (a side over a road below) is left alone.
        samples = [8.0] * 441
        report = dem.blend_to_bridge_edges(samples, 21, 21, 10.0, 10.0, {"bridge-1": deck(15.0)}, blend_m=4.0)
        self.assertEqual(samples, [8.0] * 441)
        self.assertEqual(report["bridges"]["bridge-1"]["edge_points_joined"], 0)

    def test_the_dem_joins_a_bridge_end_and_leaves_its_side_over_a_drop(self):
        # 21 x 21 samples, 1 m apart (x, y in -10..10). West half (x < 0): ground at 10. East half: a
        # valley at 4. A deck at 10.5 runs east from x = 0 over the valley (y -2..2).
        n = 21
        samples = [10.0 if col < 10 else 4.0 for row in range(n) for col in range(n)]
        deck = [((0.0, -2.0, 10.5), (10.0, -2.0, 10.5), (10.0, 2.0, 10.5)),
                ((0.0, -2.0, 10.5), (10.0, 2.0, 10.5), (0.0, 2.0, 10.5))]
        report = dem.blend_to_bridge_edges(samples, n, n, 10.0, 10.0, {"bridge-1": deck}, blend_m=4.0)

        def at(x, y):
            return samples[(y + 10) * n + (x + 10)]

        self.assertGreater(at(-1, 0), 10.3)             # the ground at the deck's end rises to it
        self.assertLess(abs(at(-8, 0) - 10.0), 0.05)    # and is itself again 8 m away
        self.assertAlmostEqual(at(5, 4), 4.0)           # the valley beside the deck stays (a 6.5 m drop)
        self.assertAlmostEqual(at(5, -4), 4.0)
        self.assertGreater(at(1, 0), 10.0)              # no gap under the deck's end
        self.assertLessEqual(at(1, 0), 10.5 - dem.UNDER_FLOOR_GAP_M + 1e-9)
        self.assertAlmostEqual(at(8, 0), 4.0)           # the valley under the deck's span stays
        self.assertGreater(report["bridges"]["bridge-1"]["edge_points_left"], 0)
        # A sample lowered to a road surface is kept.
        samples = [10.0 if col < 10 else 4.0 for row in range(n) for col in range(n)]
        dem.blend_to_bridge_edges(samples, n, n, 10.0, 10.0, {"bridge-1": deck}, blend_m=4.0, keep={10 * n + 9})
        self.assertEqual(at(-1, 0), 10.0)
        self.assertGreater(report["samples_changed"], 0)

    def test_rejects_uncovered_grid_samples(self):
        triangles = [((-1.0, -1.0, 0.0), (0.0, -1.0, 0.0), (-1.0, 0.0, 0.0))]
        with self.assertRaisesRegex(dem.DemError, "uncovered"):
            dem.sample_heightfield(triangles, 1.0, 1.0, 1.0)

    def test_fills_only_small_source_gaps_and_reports_them(self):
        triangles = [
            ((-1.0, -1.0, 0.0), (1.0, -1.0, 0.0), (-1.0, 1.0, 0.0)),
        ]
        nrow, ncol, samples, gaps = dem.sample_heightfield(
            triangles, 1.0, 1.0, 1.0, max_gap_fill_distance_m=2.0
        )
        self.assertEqual((nrow, ncol, len(samples)), (3, 3, 9))
        self.assertGreater(gaps["source_missing_samples"], 0)
        self.assertLessEqual(gaps["maximum_fill_distance_m"], 2.0)

    def test_optionally_fills_remaining_uncovered_samples_at_constant_elevation(self):
        triangles = [
            ((-1.0, -1.0, 5.0), (0.0, -1.0, 5.0), (-1.0, 0.0, 5.0)),
        ]
        nrow, ncol, samples, gaps = dem.sample_heightfield(
            triangles, 1.0, 1.0, 1.0,
            uncovered_policy="constant", uncovered_elevation_m=0.0,
        )
        self.assertEqual((nrow, ncol, len(samples)), (3, 3, 9))
        self.assertEqual(gaps["remaining_after_nearby_fill_samples"], 6)
        self.assertEqual(gaps["constant_filled_samples"], 6)
        self.assertEqual(gaps["constant_fill_elevation_m"], 0.0)
        self.assertEqual(samples.count(0.0), 6)

    def test_preserves_arbitrary_bbox_with_spacing_as_maximum(self):
        # Browser selections can have decimal extents that are not divisible
        # by the configured target spacing.
        triangles = [
            ((-1.3, -1.7, 0.0), (1.3, -1.7, 2.6), (1.3, 1.7, 6.0)),
            ((-1.3, -1.7, 0.0), (1.3, 1.7, 6.0), (-1.3, 1.7, 3.4)),
        ]
        nrow, ncol, samples, gaps = dem.sample_heightfield(
            triangles, 1.3, 1.7, 1.0
        )

        self.assertEqual((nrow, ncol), (5, 4))
        self.assertAlmostEqual(samples[0], 0.0)
        self.assertAlmostEqual(samples[-1], 6.0)
        effective = gaps["effective_spacing_m"]
        self.assertAlmostEqual(effective["north_south"], 2.6 / 3.0)
        self.assertAlmostEqual(effective["east_west"], 3.4 / 4.0)
        self.assertLessEqual(effective["north_south"], 1.0)
        self.assertLessEqual(effective["east_west"], 1.0)

    def test_skips_dem_file_when_envelope_is_outside_selection(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "outside_dem_6697_op.gml"
            source.write_text('''<core:CityModel
  xmlns:core="http://www.opengis.net/citygml/2.0"
  xmlns:gml="http://www.opengis.net/gml">
  <gml:boundedBy><gml:Envelope
    srsName="http://www.opengis.net/def/crs/EPSG/0/6697" srsDimension="3">
    <gml:lowerCorner>36.0 140.0 0</gml:lowerCorner>
    <gml:upperCorner>36.1 140.1 10</gml:upperCorner>
  </gml:Envelope></gml:boundedBy>
  <gml:posList>36.0 140.0 0 36.0 140.1 0 36.1 140.0 0</gml:posList>
</core:CityModel>''', encoding="utf-8")
            triangles = dem.extract_triangles(source, 35.0, 139.0, 100.0, 100.0)
        self.assertEqual(triangles, [])

    def test_extracts_multiline_poslist_with_nonstandard_gml_prefix(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "prefixed_dem_6697_op.gml"
            source.write_text('''<core:CityModel
  xmlns:core="http://www.opengis.net/citygml/2.0"
  xmlns:geo="http://www.opengis.net/gml">
  <geo:boundedBy><geo:Envelope
    srsName="http://www.opengis.net/def/crs/EPSG/0/6697" srsDimension="3">
    <geo:lowerCorner>34.999 138.999 0</geo:lowerCorner>
    <geo:upperCorner>35.001 139.001 10</geo:upperCorner>
  </geo:Envelope></geo:boundedBy>
  <geo:Triangle><geo:posList srsDimension="3">
    35.0 139.0 1 35.0001 139.0 2 35.0 139.0001 3 35.0 139.0 1
  </geo:posList></geo:Triangle>
</core:CityModel>''', encoding="utf-8")
            triangles = dem.extract_triangles(source, 35.0, 139.0, 100.0, 100.0)

        self.assertEqual(len(triangles), 1)
        self.assertEqual(len(triangles[0]), 3)

    def test_writes_mujoco_custom_binary_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "terrain.hf"
            dem.write_hfield(path, 2, 3, [0, 1, 2, 3, 4, 5])
            data = path.read_bytes()
            self.assertEqual(struct.unpack("<ii", data[:8]), (2, 3))
            self.assertEqual(struct.unpack("<6f", data[8:]), (0, 1, 2, 3, 4, 5))


if __name__ == "__main__":
    unittest.main()
