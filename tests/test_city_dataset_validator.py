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
            self.assertFalse(report["capabilities"]["bridge_visualization"])
            self.assertFalse(report["capabilities"]["bridge_collision"])
            self.assertTrue(report["summary"]["fallback_used"])
            self.assertEqual(
                report["summary"]["unavailable_components"],
                ["road_markings", "bridges"],
            )
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
                    "Bridges       : NOT AVAILABLE (no inferred geometry)",
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

    def test_reports_lod1_road_surface_as_last_resort_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def receipt(name, data):
                path = root / name
                path.write_text(json.dumps({"schema_version": 1, **data}), encoding="utf-8")
                return path

            report = module.validate_dataset(
                receipt("terrain.json", {"nrow": 11, "ncol": 11}),
                receipt("buildings.json", {"buildings": {"lod2": 0, "lod1_fallback": 5}}),
                receipt("roads.json", {
                    "lod_polygon_counts": {
                        "lod3": 0, "lod2_fallback": 0, "lod1_fallback": 2,
                    },
                    "surface_polygon_counts": {"roadway": 2},
                }),
                receipt("markings.json", {
                    "status": "not_available", "polygon_count": 0,
                    "reason": "no CityFurniture dataset",
                }),
            )
            lines = module.format_report(report)
        self.assertIn("Buildings     : LOD1 (LOD2 not available, fallback)", lines)
        self.assertIn(
            "Road surfaces : LOD1 (LOD3/LOD2 not available, fallback)", lines
        )
        self.assertEqual(
            report["components"]["road_surfaces"]["lod_resolution"]["effective_lod"],
            "LOD1",
        )
        self.assertEqual(
            report["components"]["road_surfaces"]["lod_resolution"]["fallback_lod"],
            "LOD2",
        )

    def test_reports_lod3_bridge_as_visual_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def receipt(name, data):
                path = root / name
                path.write_text(json.dumps({"schema_version": 1, **data}), encoding="utf-8")
                return path

            report = module.validate_dataset(
                receipt("terrain.json", {"nrow": 11, "ncol": 11}),
                receipt("buildings.json", {"buildings": {"lod2": 1, "lod1_fallback": 0}}),
                receipt("roads.json", {
                    "lod_polygon_counts": {"lod3": 1},
                    "surface_polygon_counts": {"roadway": 1},
                }),
                receipt("markings.json", {"status": "available", "polygon_count": 1,
                    "feature_counts": {"1110": 1}}),
                receipt("bridges.json", {"status": "available", "bridge_count": 1,
                    "polygon_count": 42, "rejected_polygon_count": 2,
                    "geometry_policy": "source altitude preserved"}),
            )
            self.assertTrue(report["capabilities"]["bridge_visualization"])
            self.assertFalse(report["capabilities"]["bridge_collision"])
            self.assertEqual(report["components"]["bridges"]["physics_collision"], "not_generated")
            self.assertEqual(report["components"]["bridges"]["rejected_polygon_count"], 2)
            self.assertIn(
                "Bridges       : LOD3 (1 bridges, 42 polygons; visual only)",
                module.format_report(report),
            )

    def test_reports_bridge_visual_and_collision_capabilities_separately(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def receipt(name, data):
                path = root / name
                path.write_text(json.dumps({"schema_version": 1, **data}), encoding="utf-8")
                return path

            report = module.validate_dataset(
                receipt("terrain.json", {"nrow": 11, "ncol": 11}),
                receipt("buildings.json", {"buildings": {"lod2": 1, "lod1_fallback": 0}}),
                receipt("roads.json", {"lod_polygon_counts": {"lod3": 1}}),
                receipt("markings.json", {"status": "not_available", "polygon_count": 0}),
                receipt("bridges.json", {"status": "available", "bridge_count": 1,
                    "polygon_count": 42}),
                receipt("bridge-physics.json", {"status": "available",
                    "physics_geom_count": 17}),
            )
            self.assertTrue(report["capabilities"]["bridge_visualization"])
            self.assertTrue(report["capabilities"]["bridge_collision"])
            self.assertEqual(report["components"]["bridges"]["physics_geom_count"], 17)
            self.assertIn("visual + collision 17 geoms", module.format_report(report)[-1])

    def test_reports_building_physics_classes_without_claiming_collider_change(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def receipt(name, data):
                path = root / name
                path.write_text(json.dumps({"schema_version": 1, **data}), encoding="utf-8")
                return path

            report = module.validate_dataset(
                receipt("terrain.json", {"nrow": 11, "ncol": 11}),
                receipt("buildings.json", {"buildings": {"lod2": 9, "lod1_fallback": 0}}),
                receipt("roads.json", {"lod_polygon_counts": {"lod3": 1}}),
                receipt("markings.json", {"status": "not_available", "polygon_count": 0}),
                building_physics_classification_path=receipt(
                    "classification.json",
                    {
                        "status": "classified",
                        "classification_only": True,
                        "physics_modified": False,
                        "policy": "lod1-lod2-building-physics-classification-v1",
                        "counts": {"P0": 4, "P1": 1, "P2": 1, "P3": 3},
                    },
                ),
                building_physics_application_path=receipt(
                    "application.json",
                    {
                        "status": "partial",
                        "policy": "incremental-building-physics-v1",
                        "physics_modified_by_classification": False,
                        "class_status": {
                            "P0": "applied", "P1": "pending",
                            "P2": "pending", "P3": "pending",
                        },
                    },
                ),
            )
            classification = report["components"]["buildings"]["physics_classification"]
            self.assertTrue(classification["classification_only"])
            self.assertFalse(classification["physics_modified"])
            self.assertIn(
                "Building physics: classified P0=4, P1=1, P2=1, P3=3",
                module.format_report(report),
            )
            self.assertIn(
                "Building collider: P0=applied, P1=pending, P2=pending, P3=pending",
                module.format_report(report),
            )


if __name__ == "__main__":
    unittest.main()
