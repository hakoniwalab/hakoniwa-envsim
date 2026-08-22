import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path

import trimesh

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "city_pipeline"))
import city_furniture2glb as module


class CityFurnitureGlbTest(unittest.TestCase):
    def test_exports_only_actual_marking_geometry_and_material(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "frn.gml"
            source.write_text("""<?xml version="1.0"?>
<core:CityModel xmlns:core="http://www.opengis.net/citygml/2.0"
 xmlns:gml="http://www.opengis.net/gml"
 xmlns:frn="http://www.opengis.net/citygml/cityfurniture/2.0"
 xmlns:app="http://www.opengis.net/citygml/appearance/2.0">
 <core:cityObjectMember><frn:CityFurniture gml:id="marking">
  <frn:class>1000</frn:class><frn:function>1110</frn:function>
  <frn:lod3Geometry><gml:MultiSurface><gml:surfaceMember>
   <gml:Polygon gml:id="paint"><gml:exterior><gml:LinearRing>
    <gml:posList>35.0 139.0 10 35.0 139.00001 10 35.00001 139.00001 10 35.00001 139.0 10 35.0 139.0 10</gml:posList>
   </gml:LinearRing></gml:exterior></gml:Polygon>
  </gml:surfaceMember></gml:MultiSurface></frn:lod3Geometry>
 </frn:CityFurniture></core:cityObjectMember>
 <app:appearanceMember><app:Appearance><app:surfaceDataMember><app:X3DMaterial>
  <app:diffuseColor>1 0.5 0</app:diffuseColor><app:target>#paint</app:target>
 </app:X3DMaterial></app:surfaceDataMember></app:Appearance></app:appearanceMember>
</core:CityModel>""", encoding="utf-8")
            frame = root / "world-frame.json"
            frame.write_text(json.dumps({
                "schema_version": 1,
                "origin": {"latitude": 35.0, "longitude": 139.0, "altitude_offset_m": 5.0},
                "half_extent_m": {"north_south": 100.0, "east_west": 100.0},
                "coordinate_systems": {
                    "mjcf": "X=North,Y=-East,Z=Up",
                    "glb": "X=East,Y=Up,Z=-North"
                },
            }), encoding="utf-8")
            output = root / "markings.glb"
            hfield = root / "terrain.hf"
            hfield.write_bytes(struct.pack("<ii4f", 2, 2, 5.0, 5.0, 5.0, 5.0))
            terrain_receipt = root / "terrain-receipt.json"
            terrain_receipt.write_text(json.dumps({
                "hfield": {"path": str(hfield)}
            }), encoding="utf-8")
            receipt = module.convert(source, frame, terrain_receipt, output)

            self.assertEqual(receipt["feature_counts"], {"1110": 1})
            self.assertEqual(receipt["polygon_count"], 1)
            self.assertEqual(receipt["material_polygon_count"], 1)
            self.assertEqual(receipt["fallback_polygon_count"], 0)
            self.assertEqual(receipt["marking_vertical_offset_m"], 0.055)
            scene = trimesh.load(output, force="scene")
            self.assertEqual(len(scene.geometry), 1)
            material = next(iter(scene.geometry.values())).visual.material
            self.assertTrue(material.doubleSided)
            self.assertEqual(tuple(material.baseColorFactor), (255, 128, 0, 255))
            self.assertAlmostEqual(next(iter(scene.geometry.values())).bounds[0][1], 0.055)


if __name__ == "__main__":
    unittest.main()
