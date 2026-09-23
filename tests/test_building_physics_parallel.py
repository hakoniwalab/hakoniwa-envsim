import json
import sys
import tempfile
import unittest
from pathlib import Path


PIPELINE = Path(__file__).parents[1] / "src" / "city_pipeline"
sys.path.insert(0, str(PIPELINE))
import building_lod2_colliders as colliders  # noqa: E402


class BuildingPhysicsParallelTest(unittest.TestCase):
    def test_parallel_sources_match_serial_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = Path(__file__).parent / "fixtures" / "p1_bldg_6697_op.gml"
            source_a = root / "a.gml"
            source_b = root / "b.gml"
            source_a.write_bytes(fixture.read_bytes())
            source_b.write_text(
                fixture.read_text(encoding="utf-8").replace(
                    "p1-building", "p1-building-2"
                ),
                encoding="utf-8",
            )
            selection = root / "selection.json"
            selection.write_text(json.dumps({
                "origin": {"lat": 35.681210, "lon": 139.706730},
                "polygons": [
                    {
                        "id": "p1-building", "source_gml": str(source_a),
                        "vertices": [[-1, -1], [1, -1], [1, 1], [-1, 1]],
                        "interior_rings": [], "zmin": 10, "zmax": 25,
                    },
                    {
                        "id": "p1-building-2", "source_gml": str(source_b),
                        "vertices": [[-1, -1], [1, -1], [1, 1], [-1, 1]],
                        "interior_rings": [], "zmin": 10, "zmax": 25,
                    },
                ],
            }), encoding="utf-8")
            classification = root / "classification.json"
            classification.write_text(json.dumps({
                "buildings": [
                    {"building_id": "p1-building", "class": "P1"},
                    {"building_id": "p1-building-2", "class": "P1"},
                ]
            }), encoding="utf-8")
            frame = root / "world-frame.json"
            frame.write_text(json.dumps({
                "schema_version": 1,
                "origin": {
                    "latitude": 35.681210, "longitude": 139.706730,
                    "altitude_offset_m": 10,
                },
                "half_extent_m": {"north_south": 100, "east_west": 100},
                "coordinate_systems": {
                    "mjcf": "X=North,Y=-East,Z=Up",
                    "glb": "X=East,Y=Up,Z=-North",
                },
            }), encoding="utf-8")

            serial = colliders.prepare_classes_geometry(
                selection, classification, frame, class_ids=("P1",),
                roof_thickness_m=0.02, workers=1,
            )["P1"]
            parallel = colliders.prepare_classes_geometry(
                selection, classification, frame, class_ids=("P1",),
                roof_thickness_m=0.02, workers=2,
            )["P1"]

            serialize = lambda pieces: json.dumps(
                pieces,
                default=lambda value: value.tolist(),
                sort_keys=True,
            )
            self.assertEqual(serialize(serial.pieces), serialize(parallel.pieces))
            self.assertEqual(
                serial.skipped_degenerate_by_surface,
                parallel.skipped_degenerate_by_surface,
            )
            self.assertEqual(
                serial.collider_optimization,
                parallel.collider_optimization,
            )


if __name__ == "__main__":
    unittest.main()
