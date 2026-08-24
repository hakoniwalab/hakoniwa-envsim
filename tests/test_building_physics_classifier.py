import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

import numpy as np
import trimesh


SCRIPT = Path(__file__).parents[1] / "src" / "city_pipeline" / "building_physics_classifier.py"
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("building_physics_classifier", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

OBB_SCRIPT = SCRIPT.parent / "obb2mjcf.py"
OBB_SPEC = importlib.util.spec_from_file_location("obb2mjcf_application", OBB_SCRIPT)
OBB_MODULE = importlib.util.module_from_spec(OBB_SPEC)
OBB_SPEC.loader.exec_module(OBB_MODULE)

LOD2_SCRIPT = SCRIPT.parent / "building_lod2_colliders.py"
LOD2_SPEC = importlib.util.spec_from_file_location("building_lod2_colliders_test", LOD2_SCRIPT)
LOD2_MODULE = importlib.util.module_from_spec(LOD2_SPEC)
LOD2_SPEC.loader.exec_module(LOD2_MODULE)

PROFILER_SCRIPT = SCRIPT.parent / "building_collider_tolerance_profiler.py"
PROFILER_SPEC = importlib.util.spec_from_file_location(
    "building_collider_tolerance_profiler_test", PROFILER_SCRIPT
)
PROFILER_MODULE = importlib.util.module_from_spec(PROFILER_SPEC)
PROFILER_SPEC.loader.exec_module(PROFILER_MODULE)


def metrics(**overrides):
    values = {
        "lod1_part_count": 1,
        "lod1_edge_count": 4,
        "building_part_count": 0,
        "building_installation_count": 0,
        "roof_polygons": 1,
        "wall_polygons": 4,
        "ground_polygons": 1,
        "outer_floor_polygons": 0,
        "outer_ceiling_polygons": 0,
        "closure_polygons": 0,
        "roof_relief_m": 0.0,
        "lod1_z_min_m": 0.0,
        "lod1_z_max_m": 10.0,
    }
    values.update(overrides)
    return values


class BuildingPhysicsClassifierTest(unittest.TestCase):
    def classify(self, **overrides):
        return MODULE.classify_metrics(
            metrics(**overrides), roof_relief_m=0.5, profile_ratio=1.5
        )

    def test_roof_prism_falls_back_to_normal_for_vertical_surface(self):
        vertical_roof = np.asarray([
            [-41.7297393, 22.3065285, 25.7431918],
            [-41.7297394, 22.3065285, 24.6432067],
            [-40.5056186, 22.0999629, 25.3677918],
            [-40.5056189, 22.0999630, 25.7431916],
        ])
        prism, _, mode = LOD2_MODULE.polygon_prism_for_surface(
            vertical_roof, 0.02, prefer_world_z=True
        )
        self.assertEqual(mode, "surface-normal-fallback")
        singular_values = np.linalg.svd(
            prism - prism.mean(axis=0), compute_uv=False
        )
        self.assertGreater(singular_values[-1], 1e-4)

        nearly_vertical_roof = np.asarray([
            [0, 0, 0], [0.02, 0, 2], [0.02, 1, 2], [0, 1, 0],
        ])
        _, _, near_mode = LOD2_MODULE.polygon_prism_for_surface(
            nearly_vertical_roof, 0.02, prefer_world_z=True
        )
        self.assertEqual(near_mode, "surface-normal-fallback")

    def test_roof_prism_keeps_world_z_for_horizontal_and_sloped_surfaces(self):
        for roof in (
            np.asarray([[0, 0, 5], [2, 0, 5], [2, 2, 5], [0, 2, 5.0]]),
            np.asarray([[0, 0, 5], [2, 0, 5.5], [2, 2, 5.5], [0, 2, 5.0]]),
        ):
            prism, _, mode = LOD2_MODULE.polygon_prism_for_surface(
                roof, 0.02, prefer_world_z=True
            )
            self.assertEqual(mode, "world-z")
            np.testing.assert_allclose(prism[len(roof):, 2], roof[:, 2] - 0.02)

    def test_simple_prism_is_p0(self):
        class_id, reasons = self.classify()
        self.assertEqual(class_id, "P0")
        self.assertIn("LOD1 prism", reasons[0])

    def test_multiple_roofs_are_p1(self):
        class_id, reasons = self.classify(roof_polygons=3, wall_polygons=6)
        self.assertEqual(class_id, "P1")
        self.assertIn("roof", reasons[0])

    def test_height_dependent_profile_is_p2(self):
        class_id, reasons = self.classify(wall_polygons=12)
        self.assertEqual(class_id, "P2")
        self.assertIn("profile limit", reasons[0])

    def test_outer_ceiling_has_highest_priority_and_is_p3(self):
        class_id, reasons = self.classify(
            outer_ceiling_polygons=1,
            building_part_count=2,
            roof_polygons=8,
        )
        self.assertEqual(class_id, "P3")
        self.assertIn("OuterCeilingSurface", reasons[0])

    def test_max_level_limits_the_precedence_chain(self):
        sample = metrics(
            outer_ceiling_polygons=1,
            wall_polygons=20,
            roof_polygons=3,
        )
        for max_level, expected in {0: "P0", 1: "P1", 2: "P2", 3: "P3"}.items():
            actual, _ = MODULE.classify_metrics(
                sample,
                roof_relief_m=0.5,
                profile_ratio=1.5,
                max_level=max_level,
            )
            self.assertEqual(actual, expected)

    def test_all_declared_classes_have_a_collision_strategy(self):
        self.assertEqual(set(MODULE.CLASS_DEFINITIONS), {"P0", "P1", "P2", "P3"})
        for definition in MODULE.CLASS_DEFINITIONS.values():
            self.assertTrue(definition["collision_strategy"])

    def test_p0_application_is_audited_without_changing_pending_classes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            classification = root / "classification.json"
            classification.write_text(json.dumps({
                "buildings": [
                    {"building_id": "simple", "class": "P0"},
                    {"building_id": "roof", "class": "P1"},
                ]
            }), encoding="utf-8")
            mjcf = ET.Element("mujoco")
            world = ET.SubElement(mjcf, "worldbody")
            ET.SubElement(world, "geom", {"name": "geom_simple"})
            ET.SubElement(world, "geom", {"name": "geom_roof"})
            output = root / "buildings.xml"
            output.write_text(ET.tostring(mjcf, encoding="unicode"), encoding="utf-8")
            receipt_path = root / "application.json"
            receipt = OBB_MODULE.write_physics_application_receipt(
                classification,
                [{"id": "simple"}, {"id": "roof"}],
                mjcf,
                output,
                receipt_path,
            )
            self.assertEqual(receipt["class_status"]["P0"], "applied")
            self.assertEqual(receipt["class_status"]["P1"], "pending")
            self.assertFalse(receipt["physics_modified_by_classification"])
            self.assertEqual(
                receipt["collider_geom_counts"],
                {"total": 2, "by_class": {"P0": 1, "P1": 1, "P2": 0, "P3": 0}},
            )
            self.assertTrue(receipt_path.is_file())

    def test_p1_uses_source_wall_and_roof_surfaces_without_lod1_zmax(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = Path(__file__).parent / "fixtures" / "p1_bldg_6697_op.gml"
            selection = root / "selection.json"
            selection.write_text(json.dumps({
                "origin": {"lat": 35.681210, "lon": 139.706730},
                "polygons": [{
                    "id": "p1-building",
                    "source_gml": str(source),
                    "vertices": [[-1, -1], [1, -1], [1, 1], [-1, 1]],
                    "interior_rings": [],
                    "zmin": 10,
                    "zmax": 25,
                }],
            }), encoding="utf-8")
            classification = root / "classification.json"
            classification.write_text(json.dumps({
                "buildings": [{"building_id": "p1-building", "class": "P1"}]
            }), encoding="utf-8")
            frame = root / "world-frame.json"
            frame.write_text(json.dumps({
                "schema_version": 1,
                "origin": {
                    "latitude": 35.681210,
                    "longitude": 139.706730,
                    "altitude_offset_m": 10,
                },
                "half_extent_m": {"north_south": 100, "east_west": 100},
                "coordinate_systems": {
                    "mjcf": "X=North,Y=-East,Z=Up",
                    "glb": "X=East,Y=Up,Z=-North",
                },
            }), encoding="utf-8")
            prepared = LOD2_MODULE.prepare_p1_geometry(
                selection, classification, frame, roof_thickness_m=0.02
            )
            pieces = prepared.pieces
            self.assertEqual({piece["surface_kind"] for piece in pieces}, {
                "WallSurface", "RoofSurface"
            })
            # Four planar wall quads become one prism each. The deliberately
            # twisted roof remains two triangles rather than being flattened.
            self.assertEqual(len(pieces), 6)
            self.assertEqual(
                prepared.collider_optimization["triangles_before"], 10
            )
            self.assertEqual(prepared.collider_optimization["colliders_after"], 6)
            self.assertEqual(
                prepared.collider_optimization["triangles_eliminated"], 4
            )
            self.assertAlmostEqual(
                prepared.collider_optimization["reduction_ratio"], 0.4
            )
            self.assertEqual(
                prepared.collider_optimization["fallback_polygon_counts"],
                {"non_planar": 1},
            )
            source_max_z = max(
                vertex[2] for piece in pieces for vertex in piece["source_vertices"]
            )
            self.assertAlmostEqual(source_max_z, 11.0, places=4)
            self.assertLess(source_max_z, 15.0)  # LOD1 zmax-offset must not leak into P1.
            wall = next(piece for piece in pieces if piece["surface_kind"] == "WallSurface")
            wall_vertices = wall["vertices"]
            offset = wall_vertices[0] - wall_vertices[len(wall["source_vertices"])]
            self.assertAlmostEqual(float(np.linalg.norm(offset)), 0.02, places=6)
            edge_a = wall_vertices[1] - wall_vertices[0]
            edge_b = wall_vertices[2] - wall_vertices[0]
            self.assertAlmostEqual(float(np.dot(offset, edge_a)), 0.0, places=6)
            self.assertAlmostEqual(float(np.dot(offset, edge_b)), 0.0, places=6)
            wall_mesh = trimesh.Trimesh(
                vertices=wall["vertices"], faces=wall["faces"], process=False
            )
            self.assertTrue(wall_mesh.is_watertight)

            classification.write_text(json.dumps({
                "buildings": [{"building_id": "p1-building", "class": "P2"}]
            }), encoding="utf-8")
            p2_geometry = LOD2_MODULE.prepare_p2_geometry(
                selection, classification, frame, roof_thickness_m=0.02
            )
            p2_pieces = p2_geometry.pieces
            self.assertEqual(len(p2_pieces), 6)
            self.assertTrue(all(piece["id"].startswith("p2_surface_") for piece in p2_pieces))

            classification.write_text(json.dumps({
                "buildings": [{"building_id": "p1-building", "class": "P3"}]
            }), encoding="utf-8")
            p3_geometry = LOD2_MODULE.prepare_p3_geometry(
                selection, classification, frame, roof_thickness_m=0.02
            )
            self.assertEqual(len(p3_geometry.pieces), 7)
            self.assertEqual(
                {piece["surface_kind"] for piece in p3_geometry.pieces},
                {"WallSurface", "RoofSurface", "OuterCeilingSurface"},
            )
            outer_ceilings = [
                piece for piece in p3_geometry.pieces
                if piece["surface_kind"] == "OuterCeilingSurface"
            ]
            self.assertEqual(len(outer_ceilings), 1)
            self.assertEqual(len(outer_ceilings[0]["source_vertices"]), 4)
            underside_z = {
                round(float(vertex[2]), 6)
                for vertex in outer_ceilings[0]["source_vertices"]
            }
            self.assertEqual(len(underside_z), 1)
            underside_mesh = trimesh.Trimesh(
                vertices=outer_ceilings[0]["vertices"],
                faces=outer_ceilings[0]["faces"],
                process=False,
            )
            self.assertTrue(underside_mesh.is_watertight)
            self.assertEqual(
                p3_geometry.collider_optimization["fallback_polygon_counts"],
                {"non_planar": 1},
            )
            self.assertEqual(
                p3_geometry.collider_optimization["triangles_before"], 12
            )
            self.assertEqual(
                p3_geometry.collider_optimization["colliders_after"], 7
            )
            self.assertEqual(
                p3_geometry.collider_optimization["merged_group_count"], 5
            )
            self.assertAlmostEqual(
                p3_geometry.collider_optimization["reduction_ratio"], 5 / 12
            )

    def test_convex_planar_ring_rejects_concavity_and_non_planarity(self):
        convex, reason = LOD2_MODULE._convex_planar_ring([
            (0, 0, 2), (2, 0, 2), (2, 1, 2), (0, 1, 2),
        ])
        self.assertIsNone(reason)
        self.assertEqual(convex.shape, (4, 3))

        merged, reason = LOD2_MODULE._convex_planar_ring([
            (0, 0, 2), (2, 0, 2), (1, 0.5, 2), (2, 1, 2), (0, 1, 2),
        ])
        self.assertIsNone(merged)
        self.assertEqual(reason, "concave")

    def test_coplanar_union_merges_only_an_exact_convex_union(self):
        def piece(identifier, ring):
            prism, faces = LOD2_MODULE.polygon_prism(ring, 0.02)
            return {
                "id": identifier,
                "building_id": "building",
                "surface_kind": "RoofSurface",
                "source_polygon_id": identifier,
                "source_vertices": ring,
                "vertices": prism,
                "faces": faces,
                "_coplanar_union_eligible": True,
            }

        stats = {
            "convex_merge_count": 0,
            "convex_merge_colliders_eliminated": 0,
        }
        merged = LOD2_MODULE._apply_coplanar_union([
            piece("left", [[0, 0, 2], [1, 0, 2], [1, 1, 2], [0, 1, 2]]),
            piece("right", [[1, 0, 2], [2, 0, 2], [2, 1, 2], [1, 1, 2]]),
        ], 0.02, stats)
        self.assertEqual(len(merged), 1)
        self.assertEqual(stats["convex_merge_count"], 1)
        self.assertEqual(stats["convex_merge_colliders_eliminated"], 1)
        self.assertEqual(len(merged[0]["source_vertices"]), 4)

        stats = {
            "convex_merge_count": 0,
            "convex_merge_colliders_eliminated": 0,
        }
        concave = LOD2_MODULE._apply_coplanar_union([
            piece("bottom", [[0, 0, 2], [2, 0, 2], [2, 1, 2], [0, 1, 2]]),
            piece("upper-left", [[0, 1, 2], [1, 1, 2], [1, 2, 2], [0, 2, 2]]),
        ], 0.02, stats)
        self.assertEqual(len(concave), 2)
        self.assertEqual(stats["convex_merge_count"], 0)
        self.assertEqual(stats["convex_merge_rejected_non_convex_count"], 1)

        merged, reason = LOD2_MODULE._convex_planar_ring([
            (0, 0, 2), (2, 0, 2), (2, 1, 2.1), (0, 1, 2),
        ])
        self.assertIsNone(merged)
        self.assertEqual(reason, "non_planar")

    def test_convex_decompose_adds_triangle_recomposition(self):
        def triangle(identifier, ring):
            prism, faces = LOD2_MODULE.triangular_prism(ring, 0.02)
            return {
                "id": identifier,
                "building_id": "building",
                "surface_kind": "RoofSurface",
                "source_polygon_id": "concave-source",
                "source_vertices": ring,
                "vertices": prism,
                "faces": faces,
                "_coplanar_union_eligible": True,
                "_coplanar_union_scope": "concave-source",
            }

        pieces = [
            triangle("a", [[0, 0, 2], [1, 0, 2], [1, 1, 2]]),
            triangle("b", [[0, 0, 2], [1, 1, 2], [0, 1, 2]]),
        ]
        safe_stats = {
            "convex_merge_count": 0,
            "convex_merge_colliders_eliminated": 0,
        }
        coplanar = LOD2_MODULE._apply_coplanar_union(
            [dict(piece) for piece in pieces], 0.02, safe_stats,
            include_triangulated_fallback=False,
        )
        self.assertEqual(len(coplanar), 2)

        decompose_stats = {
            "convex_merge_count": 0,
            "convex_merge_colliders_eliminated": 0,
        }
        decomposed = LOD2_MODULE._apply_coplanar_union(
            [dict(piece) for piece in pieces], 0.02, decompose_stats,
            include_triangulated_fallback=True,
        )
        self.assertEqual(len(decomposed), 1)
        self.assertEqual(decompose_stats["convex_merge_count"], 1)

    def test_rectangular_prism_uses_box_but_skewed_roof_stays_mesh(self):
        rectangle = np.asarray([
            [0, 0, 2], [2, 0, 2], [2, 1, 2], [0, 1, 2],
        ], dtype=float)
        prism, faces = LOD2_MODULE.polygon_prism(rectangle, 0.02)
        self.assertIsNotNone(OBB_MODULE.prism_as_box(rectangle, prism))

        tilted = rectangle.copy()
        tilted[2:, 2] = 3
        skewed_prism, _ = LOD2_MODULE.polygon_prism(tilted, 0.02)
        self.assertIsNone(OBB_MODULE.prism_as_box(tilted, skewed_prism))

        root = ET.Element("mujoco")
        ET.SubElement(root, "worldbody")
        OBB_MODULE.add_lod2_surface_pieces(root, [{
            "id": "rectangular-roof",
            "source_vertices": rectangle,
            "vertices": prism,
            "faces": faces,
        }], "all", (1, 1, 1, 1))
        geom = root.find("./worldbody/geom")
        self.assertEqual(geom.get("type"), "box")
        self.assertIsNone(root.find("asset"))

        rectangle_with_midpoints = np.asarray([
            [0, 0, 2], [1, 0, 2], [2, 0, 2],
            [2, 1, 2], [1, 1, 2], [0, 1, 2],
        ], dtype=float)
        midpoint_prism, _ = LOD2_MODULE.polygon_prism(
            rectangle_with_midpoints, 0.02
        )
        midpoint_box = OBB_MODULE.prism_as_box(
            rectangle_with_midpoints, midpoint_prism
        )
        self.assertIsNotNone(midpoint_box)
        self.assertTrue(np.allclose(midpoint_box["pos"], [1, 0.5, 1.99]))

    def test_tolerance_profiler_estimates_only_merges_within_displacement(self):
        def roof(identifier, x0, x1, z):
            ring = np.asarray([
                [x0, 0, z], [x1, 0, z], [x1, 1, z], [x0, 1, z],
            ], dtype=float)
            prism, faces = LOD2_MODULE.polygon_prism(ring, 0.02)
            return {
                "id": identifier,
                "building_id": "building",
                "surface_kind": "RoofSurface",
                "source_vertices": ring,
                "vertices": prism,
                "faces": faces,
            }

        pieces = [roof("left", 0, 1, 0.0), roof("right", 1, 2, 0.08)]
        rejected = PROFILER_MODULE.profile_pieces(
            pieces, tolerance_m=0.03, normal_tolerance_deg=2.0,
            thickness_m=0.02,
        )
        self.assertEqual(rejected["colliders_after"], 2)
        self.assertEqual(rejected["box_after"], 2)

        accepted = PROFILER_MODULE.profile_pieces(
            pieces, tolerance_m=0.05, normal_tolerance_deg=2.0,
            thickness_m=0.02,
        )
        self.assertEqual(accepted["colliders_after"], 1)
        self.assertEqual(accepted["colliders_eliminated"], 1)
        self.assertEqual(
            accepted["colliders_eliminated_by_surface"], {"RoofSurface": 1}
        )
        self.assertEqual(accepted["box_after"], 1)
        self.assertAlmostEqual(accepted["maximum_displacement_m"], 0.04)

        separated = [roof("left", 0, 1, 0.0), roof("right", 1.01, 2.01, 0.0)]
        no_gap_fill = PROFILER_MODULE.profile_pieces(
            separated, tolerance_m=0.05, normal_tolerance_deg=2.0,
            thickness_m=0.02,
        )
        self.assertEqual(no_gap_fill["colliders_after"], 2)

        triangle_ring = np.asarray([
            [1, 0, 0], [2, 0.5, 0], [1, 1, 0],
        ], dtype=float)
        triangle_prism, triangle_faces = LOD2_MODULE.polygon_prism(
            triangle_ring, 0.02
        )
        arrow = [pieces[0], {
            "id": "tip",
            "building_id": "building",
            "surface_kind": "RoofSurface",
            "source_vertices": triangle_ring,
            "vertices": triangle_prism,
            "faces": triangle_faces,
        }]
        mesh_merge = PROFILER_MODULE.profile_pieces(
            arrow, tolerance_m=0.0, normal_tolerance_deg=2.0,
            thickness_m=0.02,
        )
        self.assertEqual(mesh_merge["colliders_after"], 1)
        preserved = PROFILER_MODULE.profile_pieces(
            arrow, tolerance_m=0.0, normal_tolerance_deg=2.0,
            thickness_m=0.02, preserve_box_primitives=True,
        )
        self.assertEqual(preserved["colliders_after"], 2)
        self.assertEqual(preserved["box_after"], 1)

    def test_tolerant_planar_production_merges_only_wall_faces(self):
        def piece(identifier, x, y0, y1):
            ring = np.asarray([
                [x, y0, 0], [x, y1, 0], [x, y1, 2], [x, y0, 2],
            ], dtype=float)
            prism, faces, _ = LOD2_MODULE.polygon_prism_for_surface(
                ring, 0.02, prefer_world_z=False
            )
            return {
                "id": identifier,
                "building_id": "building",
                "surface_kind": "WallSurface",
                "source_vertices": ring,
                "vertices": prism,
                "faces": faces,
            }

        walls = [piece("wall-a", 0.00, 0, 1), piece("wall-b", 0.08, 1, 2)]
        reduced, stats = LOD2_MODULE.reduce_tolerant_planar(
            walls,
            thickness_m=0.02,
            tolerance_m=0.05,
            surface_kinds=("WallSurface",),
            preserve_box_primitives=True,
        )
        self.assertEqual(len(reduced), 1)
        self.assertEqual(stats["colliders_eliminated"], 1)
        self.assertAlmostEqual(stats["maximum_displacement_m"], 0.04)
        self.assertEqual(stats["box_before"], 2)
        self.assertEqual(stats["box_after"], 1)

    def test_p2_application_receipt_records_source_replacement(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            classification = root / "classification.json"
            classification.write_text(json.dumps({
                "buildings": [{"building_id": "profile", "class": "P2"}]
            }), encoding="utf-8")
            mjcf = ET.Element("mujoco")
            world = ET.SubElement(mjcf, "worldbody")
            ET.SubElement(world, "geom", {
                "name": "p2_surface_profile_piece_0000",
                "type": "box",
            })
            output = root / "buildings.xml"
            output.write_text(ET.tostring(mjcf, encoding="unicode"), encoding="utf-8")
            receipt = OBB_MODULE.write_physics_application_receipt(
                classification,
                [{"id": "profile"}],
                mjcf,
                output,
                root / "application.json",
                p2_surface_piece_count=1,
                p2_collider_optimization={
                    "triangles_before": 2,
                    "colliders_after": 1,
                    "triangles_eliminated": 1,
                    "reduction_ratio": 0.5,
                },
            )
            self.assertEqual(receipt["class_status"]["P2"], "applied")
            self.assertTrue(receipt["physics_modified_by_classification"])
            self.assertEqual(receipt["collider_geom_counts"]["by_class"]["P2"], 1)
            self.assertEqual(
                receipt["collider_geom_types"]["by_class"]["P2"],
                {"box": 1, "mesh": 0},
            )
            self.assertEqual(
                receipt["buildings"][0]["actual_strategy"],
                "lod2-height-profile-surface-prisms",
            )
            self.assertEqual(
                receipt["collider_optimization"]["P2"]["reduction_ratio"], 0.5
            )

    def test_p3_application_receipt_records_void_preserving_strategy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            classification = root / "classification.json"
            classification.write_text(json.dumps({
                "buildings": [{"building_id": "overhang", "class": "P3"}]
            }), encoding="utf-8")
            mjcf = ET.Element("mujoco")
            world = ET.SubElement(mjcf, "worldbody")
            ET.SubElement(world, "geom", {"name": "p3_surface_overhang_piece_0000"})
            output = root / "buildings.xml"
            output.write_text(ET.tostring(mjcf, encoding="unicode"), encoding="utf-8")
            receipt = OBB_MODULE.write_physics_application_receipt(
                classification,
                [{"id": "overhang"}],
                mjcf,
                output,
                root / "application.json",
                p3_surface_piece_count=1,
            )
            self.assertEqual(receipt["status"], "complete")
            self.assertEqual(receipt["class_status"]["P3"], "applied")
            self.assertEqual(
                receipt["buildings"][0]["actual_strategy"],
                "lod2-void-preserving-exterior-surface-prisms",
            )


if __name__ == "__main__":
    unittest.main()
