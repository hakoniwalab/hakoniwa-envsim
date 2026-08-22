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

    def test_writes_mujoco_custom_binary_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "terrain.hf"
            dem.write_hfield(path, 2, 3, [0, 1, 2, 3, 4, 5])
            data = path.read_bytes()
            self.assertEqual(struct.unpack("<ii", data[:8]), (2, 3))
            self.assertEqual(struct.unpack("<6f", data[8:]), (0, 1, 2, 3, 4, 5))


if __name__ == "__main__":
    unittest.main()
