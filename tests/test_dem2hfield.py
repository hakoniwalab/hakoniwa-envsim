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
