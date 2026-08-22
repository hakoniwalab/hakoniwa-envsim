from __future__ import annotations

import importlib.util
import math
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from plateau_citygml import PlateauError, bounding_box, search_url, select_files


def load_pipeline_module():
    path = ROOT / "src" / "city_pipeline" / "gml_lod1_extract.py"
    spec = importlib.util.spec_from_file_location("gml_lod1_extract", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def load_obb2mjcf_module():
    path = ROOT / "src" / "city_pipeline" / "obb2mjcf.py"
    spec = importlib.util.spec_from_file_location("obb2mjcf", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class PlateauCityGmlTest(unittest.TestCase):
    def test_bbox_uses_half_extents(self):
        west, south, east, north = bounding_box(35.0, 139.0, 100.0, 200.0)
        self.assertAlmostEqual((north - south) * 111_320.0, 200.0, places=4)
        self.assertAlmostEqual((east - west) * 111_320.0 * __import__("math").cos(__import__("math").radians(35.0)), 400.0, places=4)

    def test_search_url_uses_official_range_condition(self):
        url = search_url("https://api.example", "bldg", (139.0, 35.0, 139.1, 35.1))
        self.assertIn("/datacatalog/citygml/r:139.0000000000,35.0000000000,139.1000000000,35.1000000000", url)
        self.assertTrue(url.endswith("?types=bldg"))

    def test_latest_selects_newest_city_dataset_and_lod1(self):
        def city(year, files):
            return {"cityCode": "13113", "cityName": "Shibuya", "year": year, "registrationYear": year, "spec": "5.0", "files": {"bldg": files}}
        payload = {"cities": [
            city(2024, [{"code": "old", "maxLod": 1, "url": "https://example/old.gml", "fileSize": 1}]),
            city(2025, [
                {"code": "mesh", "maxLod": 2, "url": "https://example/new.gml", "fileSize": 2},
                {"code": "no-lod1", "maxLod": 0, "url": "https://example/lod0.gml", "fileSize": 3},
            ]),
        ]}
        selected = select_files(payload, "bldg", "latest")
        self.assertEqual([item["code"] for item in selected], ["mesh"])
        self.assertEqual(selected[0]["year"], 2025)

    def test_non_https_asset_is_rejected(self):
        payload = {"cities": [{"cityCode": "1", "year": 2025, "registrationYear": 2025, "files": {"bldg": [
            {"code": "mesh", "maxLod": 1, "url": "http://example/a.gml", "fileSize": 1}
        ]}}]}
        with self.assertRaises(PlateauError):
            select_files(payload, "bldg", "latest")

    def test_local_enu_has_meter_scale_and_axis_direction(self):
        module = load_pipeline_module()
        center_lat, center_lon = 35.0, 139.0
        # Approximately 10 m east and 10 m north at this latitude.
        point = (
            center_lat + 10.0 / 111_320.0,
            center_lon + 10.0 / (111_320.0 * math.cos(math.radians(center_lat))),
            12.5,
        )
        east, north, altitude = module.project_epsg6697_to_local_enu([point], center_lat, center_lon)[0]
        self.assertAlmostEqual(east, 10.0, delta=0.05)
        self.assertAlmostEqual(north, 10.0, delta=0.05)
        self.assertEqual(altitude, 12.5)

    def test_local_enu_has_one_mjcf_axis_contract(self):
        module = load_obb2mjcf_module()
        pos_fn, yaw_fn, size_fn = module.coordinate_transform("local-enu")
        self.assertEqual(pos_fn(2.0, 3.0, 4.0), (3.0, -2.0, 4.0))
        self.assertEqual(size_fn(5.0, 6.0), (6.0, 5.0))
        self.assertAlmostEqual(yaw_fn(math.pi / 2.0), 90.0)
        with self.assertRaisesRegex(ValueError, "local-enu"):
            module.coordinate_transform("relative")

    def test_citygml_crs_contract_requires_epsg6697_and_three_dimensions(self):
        module = load_pipeline_module()
        valid = ET.fromstring(
            '<core:CityModel xmlns:core="http://www.opengis.net/citygml/2.0" '
            'xmlns:gml="http://www.opengis.net/gml"><gml:boundedBy>'
            '<gml:Envelope srsName="http://www.opengis.net/def/crs/EPSG/0/6697" '
            'srsDimension="3"/></gml:boundedBy></core:CityModel>'
        )
        module.validate_epsg6697_contract(valid, Path("valid.gml"))
        invalid = ET.fromstring(
            '<core:CityModel xmlns:core="http://www.opengis.net/citygml/2.0" '
            'xmlns:gml="http://www.opengis.net/gml"><gml:boundedBy>'
            '<gml:Envelope srsName="http://www.opengis.net/def/crs/EPSG/0/4326" '
            'srsDimension="3"/></gml:boundedBy></core:CityModel>'
        )
        with self.assertRaisesRegex(ValueError, "EPSG:6697"):
            module.validate_epsg6697_contract(invalid, Path("invalid.gml"))

    def test_overlapping_municipality_files_deduplicate_identical_buildings(self):
        module = load_pipeline_module()
        target = []
        polygon = {"id": "bldg-1", "vertices": [[0, 0], [1, 0], [0, 1]], "zmin": 2, "zmax": 5}
        self.assertEqual(module.merge_unique_footprints(target, [polygon], Path("city-a.gml")), 0)
        self.assertEqual(module.merge_unique_footprints(target, [polygon], Path("city-b.gml")), 1)
        self.assertEqual(len(target), 1)

    def test_overlapping_municipality_files_reject_conflicting_building_id(self):
        module = load_pipeline_module()
        target = []
        first = {"id": "bldg-1", "vertices": [[0, 0], [1, 0], [0, 1]], "zmin": 2, "zmax": 5}
        second = {"id": "bldg-1", "vertices": [[0, 0], [2, 0], [0, 1]], "zmin": 2, "zmax": 5}
        module.merge_unique_footprints(target, [first], Path("city-a.gml"))
        with self.assertRaisesRegex(ValueError, "conflicting PLATEAU building geometry"):
            module.merge_unique_footprints(target, [second], Path("city-b.gml"))


if __name__ == "__main__":
    unittest.main()
