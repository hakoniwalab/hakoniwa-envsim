#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

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
