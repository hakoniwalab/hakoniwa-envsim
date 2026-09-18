#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

from shapely.geometry import Polygon, box

SCRIPT = Path(__file__).parents[1] / "src" / "city_pipeline" / "road_terrain_probe.py"
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("road_terrain_probe", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
road = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(road)


class RoadTerrainProbeTest(unittest.TestCase):
    def test_bilinear_height_uses_mujoco_xy_contract(self):
        # row is Y, column is X: z = X + 2Y over [-1,1]^2.
        samples = [-3.0, -2.0, -1.0, -1.0, 0.0, 1.0, 1.0, 2.0, 3.0]
        self.assertAlmostEqual(road.terrain_height(0.5, -0.5, samples, 3, 3, 1, 1), -0.5)
        self.assertAlmostEqual(road.terrain_height(-0.5, 0.5, samples, 3, 3, 1, 1), 0.5)

    def test_mujoco_surface_uses_bottom_left_to_top_right_diagonal(self):
        # Row-major corners: BL=0, BR=0, TL=0, TR=1.  At the center,
        # bilinear interpolation is 0.25 and the old opposite diagonal is 0;
        # MuJoCo's BL--TR triangle-strip diagonal is 0.5.
        surface = road.TerrainSurface.from_samples([0, 0, 0, 1], 2, 2, 1, 1)
        _vertices, faces = surface.grid_mesh()
        self.assertEqual(faces.tolist(), [[2, 0, 3], [0, 1, 3]])
        self.assertAlmostEqual(surface.height_at(0, 0), 0.5)
        self.assertAlmostEqual(
            road.terrain_height(0, 0, [0, 0, 0, 1], 2, 2, 1, 1),
            0.25,
        )

    def test_road_is_split_at_each_mujoco_terrain_triangle(self):
        surface = road.TerrainSurface.from_samples(
            [0, 0, 0, 0, 1, 0, 0, 0, 2], 3, 3, 1, 1
        )
        polygon = box(-0.9, -0.9, 0.9, 0.9)
        mesh = surface.drape_polygon(polygon, vertical_offset_m=0.03)

        self.assertEqual(mesh.candidate_cell_count, 4)
        self.assertGreater(len(mesh.faces), 2)
        for first, second, third in mesh.faces:
            triangle = [mesh.vertices[index] for index in (first, second, third)]
            x = sum(vertex[0] for vertex in triangle) / 3.0
            y = sum(vertex[1] for vertex in triangle) / 3.0
            z = sum(vertex[2] for vertex in triangle) / 3.0
            self.assertAlmostEqual(z, surface.height_at(x, y) + 0.03, places=9)

    def test_drape_preserves_hole_and_clips_to_terrain_bounds(self):
        surface = road.TerrainSurface.from_samples([0] * 9, 3, 3, 1, 1)
        polygon = Polygon(
            [(-2, -2), (2, -2), (2, 2), (-2, 2)],
            [[(-0.4, -0.4), (0.4, -0.4), (0.4, 0.4), (-0.4, 0.4)]],
        )
        mesh = surface.drape_polygon(polygon)
        triangle_area = 0.0
        for face in mesh.faces:
            points = [(mesh.vertices[index][0], mesh.vertices[index][1]) for index in face]
            triangle_area += Polygon(points).area
        expected = polygon.intersection(surface.bounds).area
        self.assertAlmostEqual(triangle_area, expected, places=9)

    def test_parallel_drape_preserves_source_order_and_geometry(self):
        surface = road.TerrainSurface.from_samples(
            [0, 0, 0, 0, 1, 0, 0, 0, 2], 3, 3, 1, 1
        )
        polygons = [box(-0.9, -0.9, 0.2, 0.2), box(-0.2, -0.2, 0.9, 0.9)]
        serial, serial_stats = road.drape_polygons(surface, polygons, 0.03, workers=1)
        parallel, parallel_stats = road.drape_polygons(surface, polygons, 0.03, workers=2)

        self.assertEqual(serial, parallel)
        self.assertEqual(serial_stats["candidate_cell_count"], parallel_stats["candidate_cell_count"])
        self.assertEqual(parallel_stats["requested_workers"], 2)
        self.assertEqual(parallel_stats["effective_workers"], 2)

    def test_extracts_lod2_vehicle_sidewalk_and_island_classes(self):
        feature = '''
          <tran:{kind} gml:id="{name}">
            <tran:function>{function}</tran:function>
            <tran:lod2MultiSurface><gml:MultiSurface><gml:surfaceMember>
              <gml:Polygon><gml:exterior><gml:LinearRing><gml:posList>
                35.66249 139.70624 0 35.66249 139.70626 0
                35.66251 139.70626 0 35.66251 139.70624 0
                35.66249 139.70624 0
              </gml:posList></gml:LinearRing></gml:exterior></gml:Polygon>
            </gml:surfaceMember></gml:MultiSurface></tran:lod2MultiSurface>
          </tran:{kind}>
        '''
        members = [
            feature.format(kind="TrafficArea", name="road", function="1000"),
            feature.format(kind="TrafficArea", name="crossing", function="1020"),
            feature.format(kind="TrafficArea", name="walk", function="2000"),
            feature.format(kind="AuxiliaryTrafficArea", name="island", function="3000"),
        ]
        xml = '''<core:CityModel xmlns:core="http://www.opengis.net/citygml/2.0"
          xmlns:gml="http://www.opengis.net/gml"
          xmlns:tran="http://www.opengis.net/citygml/transportation/2.0">
          <tran:Road gml:id="r">{}</tran:Road></core:CityModel>'''.format("".join(members))
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "roads.gml"
            path.write_text(xml, encoding="utf-8")
            surfaces = road.extract_transport_surfaces(
                path, 35.6625, 139.70625, 100.0, 100.0
            )
        self.assertEqual({key: len(value) for key, value in surfaces.items()}, {
            "roadway": 1,
            "lane": 0,
            "intersection": 1,
            "sidewalk": 1,
            "island": 1,
        })

    def test_lod1_road_is_used_when_semantic_lod2_and_lod3_are_absent(self):
        xml = '''<core:CityModel xmlns:core="http://www.opengis.net/citygml/2.0"
          xmlns:gml="http://www.opengis.net/gml"
          xmlns:tran="http://www.opengis.net/citygml/transportation/2.0">
          <tran:Road gml:id="lod1-road">
            <tran:lod1MultiSurface><gml:MultiSurface><gml:surfaceMember>
              <gml:Polygon><gml:exterior><gml:LinearRing><gml:posList>
                35.66249 139.70624 0 35.66249 139.70626 0
                35.66251 139.70626 0 35.66251 139.70624 0
                35.66249 139.70624 0
              </gml:posList></gml:LinearRing></gml:exterior></gml:Polygon>
            </gml:surfaceMember></gml:MultiSurface></tran:lod1MultiSurface>
          </tran:Road></core:CityModel>'''
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "tiny_tran_lod1_op.gml"
            path.write_text(xml, encoding="utf-8")
            _paths, surfaces, evidence = road.extract_all_transport_surfaces(
                path, 35.6625, 139.70625, 100.0, 100.0
            )
        self.assertEqual(len(surfaces["roadway"]), 1)
        self.assertEqual(evidence, {
            "lod3": 0, "lod2_fallback": 0, "lod1_fallback": 1,
        })


if __name__ == "__main__":
    unittest.main()
