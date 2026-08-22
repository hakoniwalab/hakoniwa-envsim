import json
import struct
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "city_pipeline"))
import bridge2mjcf as module


def world_frame(path: Path, altitude_offset=0.0) -> Path:
    path.write_text(json.dumps({
        "schema_version": 1,
        "origin": {"latitude": 35.0, "longitude": 139.0, "altitude_offset_m": altitude_offset},
        "half_extent_m": {"north_south": 100.0, "east_west": 100.0},
        "coordinate_systems": {
            "mjcf": "X=North,Y=-East,Z=Up",
            "glb": "X=East,Y=Up,Z=-North",
        },
    }), encoding="utf-8")
    return path


def bridge_gml(path: Path, surfaces, bridge_count=1) -> Path:
    members = []
    for bridge_index in range(bridge_count):
        surface_xml = []
        for surface_index, points in enumerate(surfaces):
            values = " ".join(str(value) for point in points for value in point)
            surface_xml.append(f'''<brid:boundedBy><brid:OuterFloorSurface gml:id="surface-{bridge_index}-{surface_index}">
 <brid:lod3MultiSurface><gml:MultiSurface><gml:surfaceMember>
  <gml:Polygon gml:id="polygon-{bridge_index}-{surface_index}"><gml:exterior><gml:LinearRing>
   <gml:posList>{values}</gml:posList>
  </gml:LinearRing></gml:exterior></gml:Polygon>
 </gml:surfaceMember></gml:MultiSurface></brid:lod3MultiSurface>
</brid:OuterFloorSurface></brid:boundedBy>''')
        members.append(
            f'<core:cityObjectMember><brid:Bridge gml:id="bridge-{bridge_index}">'
            + "".join(surface_xml)
            + '</brid:Bridge></core:cityObjectMember>'
        )
    path.write_text(f'''<?xml version="1.0"?>
<core:CityModel xmlns:core="http://www.opengis.net/citygml/2.0"
 xmlns:gml="http://www.opengis.net/gml"
 xmlns:brid="http://www.opengis.net/citygml/bridge/2.0">
 <gml:boundedBy><gml:Envelope srsName="http://www.opengis.net/def/crs/EPSG/0/6697" srsDimension="3">
  <gml:lowerCorner>34 138 0</gml:lowerCorner><gml:upperCorner>36 140 20</gml:upperCorner>
 </gml:Envelope></gml:boundedBy>{''.join(members)}
</core:CityModel>''', encoding="utf-8")
    return path


def terrain_receipt(root: Path, altitude=0.0) -> Path:
    hfield = root / "terrain.hf"
    hfield.write_bytes(struct.pack("<ii4f", 2, 2, altitude, altitude, altitude, altitude))
    receipt = root / "terrain-receipt.json"
    receipt.write_text(json.dumps({
        "half_extent_m": {"north_south": 100.0, "east_west": 100.0},
        "altitude_offset_m": altitude,
        "hfield": {"path": str(hfield)},
    }), encoding="utf-8")
    return receipt


class BridgePhysicsTest(unittest.TestCase):
    def test_flat_bridge_preserves_top_and_leaves_under_bridge_space(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            flat = [[
                (35.0, 139.0, 5.0), (35.0, 139.00001, 5.0),
                (35.00001, 139.00001, 5.0), (35.00001, 139.0, 5.0),
                (35.0, 139.0, 5.0),
            ]]
            output = root / "bridges.xml"
            receipt_path = root / "receipt.json"
            result = module.convert(
                bridge_gml(root / "fixture_brid_6697_op.gml", flat),
                world_frame(root / "world-frame.json"), output, receipt_path,
                terrain_receipt(root), 0.02, 60.0,
            )
            self.assertEqual(result["status"], "available")
            self.assertEqual(result["physics_geom_count"], 2)
            parsed = ET.parse(output).getroot()
            self.assertEqual(len(parsed.findall("asset/mesh")), 2)
            for geom in parsed.findall("worldbody/geom"):
                self.assertEqual(geom.get("contype"), "1")
                self.assertEqual(geom.get("conaffinity"), "0")
            self.assertEqual(result["collision_filter"], {
                "mode": "all", "contype": "1", "conaffinity": "0",
            })
            vertices = np.asarray([
                float(value) for value in parsed.find("asset/mesh").get("vertex").split()
            ]).reshape((-1, 3))
            self.assertAlmostEqual(float(vertices[:3, 2].min()), 5.0, places=6)
            self.assertAlmostEqual(float(vertices[3:, 2].min()), 4.98, places=6)
            # The collision solid occupies only z=4.98..5.0; z=1 remains open.
            self.assertLess(1.0, float(vertices[:, 2].min()))

    def test_collision_mode_matches_building_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            flat = [[
                (35.0, 139.0, 5.0), (35.0, 139.00001, 5.0),
                (35.00001, 139.0, 5.0), (35.0, 139.0, 5.0),
            ]]
            for mode, expected in {
                "drone": ("1", "2"),
                "none": ("0", "0"),
            }.items():
                output = root / f"bridges-{mode}.xml"
                result = module.convert(
                    bridge_gml(root / f"fixture-{mode}_brid_6697_op.gml", flat),
                    world_frame(root / f"world-frame-{mode}.json"), output,
                    root / f"receipt-{mode}.json", terrain_receipt(root),
                    0.02, 60.0, mode,
                )
                geom = ET.parse(output).getroot().find("worldbody/geom")
                self.assertEqual((geom.get("contype"), geom.get("conaffinity")), expected)
                self.assertEqual(result["collision_filter"]["mode"], mode)

    def test_arch_surface_preserves_endpoint_and_center_heights(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            arch = [
                [(35.0, 139.0, 5), (35.0, 139.00001, 5),
                 (35.00001, 139.00001, 8), (35.00001, 139.0, 8), (35.0, 139.0, 5)],
                [(35.00001, 139.0, 8), (35.00001, 139.00001, 8),
                 (35.00002, 139.00001, 5), (35.00002, 139.0, 5), (35.00001, 139.0, 8)],
            ]
            result = module.convert(
                bridge_gml(root / "fixture_brid_6697_op.gml", arch),
                world_frame(root / "world-frame.json"), root / "bridges.xml", root / "receipt.json",
                terrain_receipt(root), 0.02, 89.0,
            )
            debug = json.loads(Path(result["debug"]).read_text(encoding="utf-8"))
            heights = [vertex[2] for piece in debug["pieces"] for vertex in piece["source_vertices"]]
            self.assertAlmostEqual(min(heights), 5.0)
            self.assertAlmostEqual(max(heights), 8.0)

    def test_vertical_outer_floor_surface_is_not_treated_as_walkable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vertical = [[
                (35.0, 139.0, 5), (35.0, 139.00001, 5),
                (35.0, 139.00001, 8), (35.0, 139.0, 8), (35.0, 139.0, 5),
            ]]
            result = module.convert(
                bridge_gml(root / "fixture_brid_6697_op.gml", vertical),
                world_frame(root / "world-frame.json"), root / "bridges.xml", root / "receipt.json",
                None, 0.02, 60.0,
            )
            self.assertEqual(result["status"], "scoped_out")
            self.assertEqual(result["reason"], "usable_bridge_surface_not_available")
            self.assertGreater(result["rejected_slope_triangle_count"], 0)

    def test_multiple_bridge_ids_and_piece_ids_are_deterministic(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            flat = [[
                (35.0, 139.0, 5), (35.0, 139.00001, 5),
                (35.00001, 139.0, 5), (35.0, 139.0, 5),
            ]]
            source = bridge_gml(root / "fixture_brid_6697_op.gml", flat, bridge_count=2)
            frame = world_frame(root / "world-frame.json")
            first = module.convert(source, frame, root / "first.xml", root / "first.json", None, 0.02, 60)
            second = module.convert(source, frame, root / "second.xml", root / "second.json", None, 0.02, 60)
            self.assertEqual(first["bridge_ids"], ["bridge-0", "bridge-1"])
            self.assertEqual(first["physics_geom_count"], 2)
            names1 = [mesh.get("name") for mesh in ET.parse(root / "first.xml").findall("asset/mesh")]
            names2 = [mesh.get("name") for mesh in ET.parse(root / "second.xml").findall("asset/mesh")]
            self.assertEqual(names1, names2)

    def test_endpoint_receipt_measures_source_height_without_correction(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            flat = [[
                (35.0, 139.0, 5), (35.0, 139.00001, 5),
                (35.00001, 139.0, 5), (35.0, 139.0, 5),
            ]]
            result = module.convert(
                bridge_gml(root / "fixture_brid_6697_op.gml", flat),
                world_frame(root / "world-frame.json", altitude_offset=1.0), root / "bridges.xml", root / "receipt.json",
                terrain_receipt(root, altitude=1.0), 0.02, 60,
            )
            validation = result["endpoint_height_validation"]
            self.assertEqual(validation["status"], "measured")
            self.assertFalse(validation["applied_correction"])
            self.assertAlmostEqual(validation["difference_m"]["median"], 4.0, places=6)
            self.assertEqual(result["corrections"], [])
            relationship = result["terrain_relationship_validation"]
            self.assertEqual(relationship["status"], "measured")
            self.assertEqual(relationship["below_dem_sample_count"], 0)


if __name__ == "__main__":
    unittest.main()
