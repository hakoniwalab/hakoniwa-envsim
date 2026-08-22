import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "city_pipeline"))
import city_dataset_validator as module


class CityDatasetValidatorTest(unittest.TestCase):
    def test_reports_lod_fallbacks_and_missing_markings_without_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def receipt(name, data):
                path = root / name
                path.write_text(json.dumps({"schema_version": 1, **data}), encoding="utf-8")
                return path

            report = module.validate_dataset(
                receipt("terrain.json", {"nrow": 101, "ncol": 101}),
                receipt("buildings.json", {"buildings": {"lod2": 4, "lod1_fallback": 3}}),
                receipt("roads.json", {
                    "lod_polygon_counts": {"lod3": 0, "lod2_fallback": 12},
                    "surface_polygon_counts": {"roadway": 12},
                }),
                receipt("markings.json", {
                    "status": "not_available", "polygon_count": 0,
                    "reason": "no matching LOD3 road-marking CityFurniture geometry",
                }),
            )
            self.assertEqual(report["status"], "ready")
            self.assertFalse(report["capabilities"]["road_marking_visualization"])
            self.assertTrue(report["summary"]["fallback_used"])
            self.assertEqual(report["summary"]["unavailable_components"], ["road_markings"])
            self.assertEqual(
                report["components"]["road_surfaces"]["lod_resolution"]["effective_lod"],
                "LOD2",
            )
            self.assertEqual(
                module.format_report(report),
                [
                    "Terrain       : DEM hfield (101 x 101)",
                    "Buildings     : LOD2 (4), LOD1 fallback (3)",
                    "Road surfaces : LOD2 (LOD3 not available, fallback)",
                    "Road markings : NOT AVAILABLE (LOD3 absent; no surface markings, no inference)",
                ],
            )
            output = root / "dataset-validation.json"
            text = module.write_report(report, output)
            self.assertTrue(output.is_file())
            self.assertEqual(text.name, "dataset-validation.txt")
            self.assertIn(
                "Road surfaces : LOD2 (LOD3 not available, fallback)",
                text.read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
