import json
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "city_pipeline"))
import building_road_passages as module  # noqa: E402


def box(center, half):
    cx, cy, cz = center
    hx, hy, hz = half
    corners = np.array([[cx + sx * hx, cy + sy * hy, cz + sz * hz] for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)])
    return module.Polyhedron([corners[list(face)] for face in module.BOX_FACES])


class ConvexClippingTest(unittest.TestCase):
    def test_a_box_is_split_by_a_plane_into_two_capped_parts(self):
        cube = box((0, 0, 0), (1, 1, 1))
        upper = module.clip(cube, (0, 0, 1), (0, 0, 0.5))
        lower = module.clip(cube, (0, 0, -1), (0, 0, 0.5))
        self.assertAlmostEqual(upper.volume, 2.0, places=6)
        self.assertAlmostEqual(lower.volume, 6.0, places=6)
        self.assertIsNone(module.clip(cube, (0, 0, 1), (0, 0, 5.0)))

    def test_a_passage_takes_the_space_under_its_top_inside_the_triangle(self):
        # A 20 x 2 x 10 wall (x -10..10) across a road; the passage covers x -3..3 up to z 4.5.
        wall = box((0, 0, 5), (10, 1, 5))
        triangles = [((-3, -5), (3, -5), (3, 5)), ((-3, -5), (3, 5), (-3, 5))]
        pieces, removed = wall, 0.0
        pieces = [wall]
        for triangle in triangles:
            next_pieces = []
            for piece in pieces:
                outside, gone = module.subtract_prism(piece, triangle, 4.5)
                removed += gone
                next_pieces.extend(outside)
            pieces = next_pieces
        self.assertAlmostEqual(removed, 6 * 2 * 4.5, places=4)
        self.assertAlmostEqual(sum(p.volume for p in pieces), 20 * 2 * 10 - 6 * 2 * 4.5, places=4)
        # Nothing of the wall remains below 4.5 m between x -3 and 3.
        for piece in pieces:
            lo, hi = piece.bounds
            self.assertFalse(lo[2] < 4.5 - 1e-6 and lo[0] < 3 - 1e-6 and hi[0] > -3 + 1e-6)

    def test_the_mjcf_is_rewritten_with_convex_pieces_and_a_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            frame = root / "world-frame.json"
            frame.write_text(json.dumps({
                "schema_version": 1, "origin": {"latitude": 35.0, "longitude": 139.0, "altitude_offset_m": 0.0},
                "half_extent_m": {"north_south": 100.0, "east_west": 100.0},
                "coordinate_systems": {"mjcf": "X=North,Y=-East,Z=Up", "glb": "X=East,Y=Up,Z=-North"}}), encoding="utf-8")
            # A carriageway 8 m wide (east-west, y) running north through x -20..20 at z 0.
            values = " ".join(f"{lat} {lon} 0.0" for lat, lon in (
                (35.0 - 20 / 111320, 139.0 - 4 / 91290), (35.0 + 20 / 111320, 139.0 - 4 / 91290),
                (35.0 + 20 / 111320, 139.0 + 4 / 91290), (35.0 - 20 / 111320, 139.0 + 4 / 91290),
                (35.0 - 20 / 111320, 139.0 - 4 / 91290)))
            (root / "53390000_tran_6697_op.gml").write_text(f'''<?xml version="1.0"?>
<core:CityModel xmlns:core="http://www.opengis.net/citygml/2.0" xmlns:gml="http://www.opengis.net/gml"
 xmlns:tran="http://www.opengis.net/citygml/transportation/2.0">
 <gml:boundedBy><gml:Envelope srsName="http://www.opengis.net/def/crs/EPSG/0/6697" srsDimension="3">
  <gml:lowerCorner>34 138 0</gml:lowerCorner><gml:upperCorner>36 140 20</gml:upperCorner></gml:Envelope></gml:boundedBy>
 <core:cityObjectMember><tran:Road gml:id="road-1"><tran:trafficArea><tran:TrafficArea gml:id="ta-1">
  <tran:function>1000</tran:function>
  <tran:lod3MultiSurface><gml:MultiSurface><gml:surfaceMember><gml:Polygon gml:id="poly-1"><gml:exterior><gml:LinearRing>
   <gml:posList>{values}</gml:posList></gml:LinearRing></gml:exterior></gml:Polygon></gml:surfaceMember></gml:MultiSurface></tran:lod3MultiSurface>
 </tran:TrafficArea></tran:trafficArea></tran:Road></core:cityObjectMember></core:CityModel>''', encoding="utf-8")
            mjcf = root / "buildings.xml"
            # A wall across the road (30 m along y, 0.2 thick, 12 m high) and a building clear of it.
            mjcf.write_text('''<mujoco model="t"><asset/><worldbody>
<geom name="wall" type="box" pos="0 0 6" size="0.1 15 6" rgba="1 0 0 1" contype="1" conaffinity="0"/>
<geom name="far" type="box" pos="60 0 5" size="5 5 5" rgba="1 0 0 1"/>
</worldbody></mujoco>''', encoding="utf-8")
            report = module.apply(mjcf, root, frame, 4.5, 0.2)
            self.assertEqual([item["geom"] for item in report["geoms_cut"]], ["wall"])
            self.assertAlmostEqual(report["volume_removed_m3"], 0.2 * (8 - 0.4) * 4.5, delta=0.2)
            tree = ET.parse(mjcf)
            names = [g.get("name") for g in tree.getroot().find("worldbody").findall("geom")]
            self.assertIn("far", names)
            self.assertNotIn("wall", names)
            self.assertTrue(all(n.startswith("wall_pass") for n in names if n != "far"))
            self.assertEqual([g.get("contype") for g in tree.getroot().find("worldbody").findall("geom") if g.get("name") != "far"][0], "1")


if __name__ == "__main__":
    unittest.main()
