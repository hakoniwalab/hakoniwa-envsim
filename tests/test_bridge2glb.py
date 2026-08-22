import json
import sys
import tempfile
import unittest
from pathlib import Path

import trimesh

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "city_pipeline"))
import bridge2glb as module


def world_frame(path: Path) -> Path:
    path.write_text(json.dumps({
        "schema_version": 1,
        "origin": {
            "latitude": 35.0,
            "longitude": 139.0,
            "altitude_offset_m": 5.0,
        },
        "half_extent_m": {"north_south": 100.0, "east_west": 100.0},
        "coordinate_systems": {
            "mjcf": "X=North,Y=-East,Z=Up",
            "glb": "X=East,Y=Up,Z=-North",
        },
    }), encoding="utf-8")
    return path


def bridge_gml(path: Path, *, srs: str = "http://www.opengis.net/def/crs/EPSG/0/6697") -> Path:
    path.write_text(f'''<?xml version="1.0"?>
<core:CityModel xmlns:core="http://www.opengis.net/citygml/2.0"
 xmlns:gml="http://www.opengis.net/gml"
 xmlns:brid="http://www.opengis.net/citygml/bridge/2.0"
 xmlns:app="http://www.opengis.net/citygml/appearance/2.0">
 <gml:boundedBy><gml:Envelope srsName="{srs}" srsDimension="3">
  <gml:lowerCorner>35.0 139.0 10</gml:lowerCorner>
  <gml:upperCorner>35.00001 139.00001 10</gml:upperCorner>
 </gml:Envelope></gml:boundedBy>
 <core:cityObjectMember><brid:Bridge gml:id="bridge-1">
  <brid:lod3Geometry><gml:MultiSurface><gml:surfaceMember>
   <gml:Polygon gml:id="deck"><gml:exterior><gml:LinearRing>
    <gml:posList>35.0 139.0 10 35.0 139.00001 10 35.00001 139.00001 10 35.00001 139.0 10 35.0 139.0 10</gml:posList>
   </gml:LinearRing></gml:exterior></gml:Polygon>
  </gml:surfaceMember></gml:MultiSurface></brid:lod3Geometry>
 </brid:Bridge></core:cityObjectMember>
 <app:appearanceMember><app:Appearance><app:surfaceDataMember><app:X3DMaterial>
  <app:diffuseColor>0.2 0.4 0.6</app:diffuseColor><app:target>#deck</app:target>
 </app:X3DMaterial></app:surfaceDataMember></app:Appearance></app:appearanceMember>
</core:CityModel>''', encoding="utf-8")
    return path


class BridgeGlbTest(unittest.TestCase):
    def test_exports_lod3_bridge_with_source_altitude_and_material(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = bridge_gml(root / "tiny_brid_6697_op.gml")
            frame = world_frame(root / "world-frame.json")
            output = root / "bridges.glb"
            receipt = module.convert(source, frame, output)

            self.assertEqual(receipt["status"], "available")
            self.assertEqual(receipt["bridge_count"], 1)
            self.assertEqual(receipt["bridge_ids"], ["bridge-1"])
            self.assertEqual(receipt["polygon_count"], 1)
            self.assertEqual(receipt["material_polygon_count"], 1)
            self.assertEqual(receipt["fallback_polygon_count"], 0)
            self.assertEqual(receipt["rejected_polygon_count"], 0)
            self.assertIn("source altitude preserved", receipt["geometry_policy"])

            scene = trimesh.load(output, force="scene")
            mesh = next(iter(scene.geometry.values()))
            self.assertEqual(tuple(mesh.visual.material.baseColorFactor), (51, 102, 153, 255))
            self.assertAlmostEqual(mesh.bounds[0][1], 5.0, places=5)
            self.assertAlmostEqual(mesh.bounds[1][1], 5.0, places=5)

    def test_allow_empty_reports_absence_without_inventing_geometry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            output = root / "bridges.glb"
            receipt = module.convert(
                source, world_frame(root / "world-frame.json"), output,
                allow_empty=True,
            )
            self.assertEqual(receipt["status"], "not_available")
            self.assertFalse(output.exists())
            self.assertEqual(receipt["geometry_policy"],
                "source altitude preserved; no terrain draping or inferred bridge geometry")

    def test_rejects_non_epsg6697_bridge_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = bridge_gml(
                root / "tiny_brid_6697_op.gml",
                srs="http://www.opengis.net/def/crs/EPSG/0/4326",
            )
            with self.assertRaisesRegex(module.BridgeGlbError, "EPSG:6697"):
                module.convert(
                    source, world_frame(root / "world-frame.json"),
                    root / "bridges.glb",
                )

    def test_reports_one_degenerate_surface_without_discarding_bridge(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = bridge_gml(root / "tiny_brid_6697_op.gml")
            text = source.read_text(encoding="utf-8")
            degenerate = '''<gml:surfaceMember>
   <gml:Polygon gml:id="degenerate"><gml:exterior><gml:LinearRing>
    <gml:posList>35.0 139.0 10 35.0 139.0 10 35.0 139.0 10 35.0 139.0 10</gml:posList>
   </gml:LinearRing></gml:exterior></gml:Polygon>
  </gml:surfaceMember>'''
            source.write_text(
                text.replace("</gml:MultiSurface>", degenerate + "</gml:MultiSurface>"),
                encoding="utf-8",
            )
            receipt = module.convert(
                source, world_frame(root / "world-frame.json"), root / "bridges.glb"
            )
            self.assertEqual(receipt["status"], "available")
            self.assertEqual(receipt["polygon_count"], 1)
            self.assertEqual(receipt["rejected_polygon_count"], 1)
            self.assertEqual(receipt["rejected_polygon_ids"], ["degenerate"])


if __name__ == "__main__":
    unittest.main()
