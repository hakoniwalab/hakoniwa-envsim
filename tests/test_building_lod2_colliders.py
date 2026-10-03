import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "city_pipeline"))

import building_lod2_colliders as colliders  # noqa: E402


class BuildingInstallationTest(unittest.TestCase):
    def test_installations_are_read_and_level_decks_extrude_upright(self):
        for class_id in ("P1", "P2", "P3"):
            self.assertIn("BuildingInstallation", colliders.CLASS_SURFACE_KINDS[class_id])
        deck = [(0.0, 0.0, 248.4), (10.0, 0.0, 248.4), (10.0, 8.0, 248.4), (0.0, 8.0, 248.4)]
        mast = [(0.0, 0.0, 246.7), (1.0, 0.0, 246.7), (1.0, 0.0, 265.3), (0.0, 0.0, 265.3)]
        self.assertTrue(colliders._extrude_vertically("BuildingInstallation", deck))
        self.assertFalse(colliders._extrude_vertically("BuildingInstallation", mast))
        self.assertTrue(colliders._extrude_vertically("RoofSurface", mast))
        self.assertFalse(colliders._extrude_vertically("WallSurface", deck))


if __name__ == "__main__":
    unittest.main()
