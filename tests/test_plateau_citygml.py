from __future__ import annotations

import importlib.util
import copy
import json
import math
import subprocess
import struct
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from plateau_citygml import PlateauError, bounding_box, search_url, select_files, third_mesh_codes


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


def load_gml2obb_module():
    path = ROOT / "src" / "city_pipeline" / "gml2obb.py"
    spec = importlib.util.spec_from_file_location("gml2obb", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def load_hako_module():
    path = ROOT / "tools" / "hako.py"
    spec = importlib.util.spec_from_file_location("envsim_hako", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class PlateauCityGmlTest(unittest.TestCase):
    def test_bbox_uses_half_extents(self):
        west, south, east, north = bounding_box(35.0, 139.0, 100.0, 200.0)
        self.assertAlmostEqual((north - south) * 111_320.0, 200.0, places=4)
        self.assertAlmostEqual((east - west) * 111_320.0 * __import__("math").cos(__import__("math").radians(35.0)), 400.0, places=4)

    def test_search_url_enumerates_every_intersecting_third_mesh(self):
        bbox = bounding_box(35.6625, 139.70625, 500.0, 500.0)
        self.assertEqual(third_mesh_codes(bbox), ["53393586", "53393596", "53394506"])
        url = search_url("https://api.example", "bldg", bbox)
        self.assertIn("/datacatalog/citygml/m:53393586,53393596,53394506", url)
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

    def test_lod1_extractor_preserves_original_concave_footprint(self):
        extractor = load_pipeline_module()
        converter = load_gml2obb_module()
        polygons = extractor.extract_buildings_lod1(
            ROOT / "tests" / "fixtures" / "concave_bldg_6697_op.gml",
            local_origin=(35.681200, 139.706720),
        )
        self.assertEqual(len(polygons), 1)
        self.assertEqual(len(polygons[0]["vertices"]), 6)
        points = __import__("numpy").array(polygons[0]["vertices"])
        area = converter.polygon_area(points)
        _, _, _, _, obb_area = converter.min_area_rect_calipers(points)
        self.assertAlmostEqual(converter.obb_empty_area_ratio(area, obb_area), 0.25, delta=0.01)

    def test_waste_threshold_selects_obb_or_original_boundary_walls(self):
        polygon = {
            "id": "l-shape",
            "vertices": [[0, 0], [2, 0], [2, 1], [1, 1], [1, 2], [0, 2]],
            "interior_rings": [],
            "zmin": 0,
            "zmax": 5,
        }
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "polygons.json"
            source.write_text(json.dumps({
                "coordinate_system": "local-enu",
                "polygons": [polygon],
            }), encoding="utf-8")
            outputs = {}
            for threshold in (0.2, 0.3, 1.0):
                output = Path(temporary) / f"walls-{threshold}.json"
                subprocess.run([
                    sys.executable,
                    str(ROOT / "src" / "city_pipeline" / "gml2obb.py"),
                    "--in", str(source), "--out", str(output),
                    "--waste-threshold", str(threshold),
                ], cwd=ROOT, check=True, capture_output=True, text=True)
                outputs[threshold] = json.loads(output.read_text(encoding="utf-8"))["results"]

            self.assertEqual(len(outputs[0.2]), 6)
            self.assertTrue(all(record["mode"] == "wall" for record in outputs[0.2]))
            self.assertEqual([record["edge_index"] for record in outputs[0.2]], list(range(6)))
            self.assertEqual(len(outputs[0.3]), 1)
            self.assertEqual(outputs[0.3][0]["mode"], "obb")
            self.assertEqual(len(outputs[1.0]), 1)
            self.assertAlmostEqual(outputs[1.0][0]["waste_ratio"], 0.25)

    def test_wall_mode_preserves_interior_ring_boundaries(self):
        polygon = {
            "id": "courtyard",
            "vertices": [[0, 0], [4, 0], [4, 4], [0, 4]],
            "interior_rings": [[[1, 1], [1, 3], [3, 3], [3, 1]]],
            "zmin": 0,
            "zmax": 5,
        }
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "polygons.json"
            output = Path(temporary) / "walls.json"
            source.write_text(json.dumps({
                "coordinate_system": "local-enu",
                "polygons": [polygon],
            }), encoding="utf-8")
            subprocess.run([
                sys.executable,
                str(ROOT / "src" / "city_pipeline" / "gml2obb.py"),
                "--in", str(source), "--out", str(output),
                "--waste-threshold", "0.2",
            ], cwd=ROOT, check=True, capture_output=True, text=True)
            records = json.loads(output.read_text(encoding="utf-8"))["results"]
            self.assertEqual(len(records), 8)
            self.assertEqual(sum(r["boundary_kind"] == "exterior" for r in records), 4)
            self.assertEqual(sum(r["boundary_kind"] == "interior" for r in records), 4)
            self.assertAlmostEqual(records[0]["waste_ratio"], 0.25)

    def test_waste_threshold_configuration_is_a_zero_to_one_ratio(self):
        hako = load_hako_module()
        for value in (0.0, 1.0):
            config = copy.deepcopy(hako.DEFAULT_CONFIG)
            config["geometry"]["waste_threshold"] = value
            self.assertEqual(hako.resolve_config(config)["geometry"]["waste_threshold"], value)
        for value in (-0.01, 1.01):
            config = copy.deepcopy(hako.DEFAULT_CONFIG)
            config["geometry"]["waste_threshold"] = value
            with self.assertRaisesRegex(hako.ConfigError, r"\[0, 1\]"):
                hako.resolve_config(config)

    def test_direct_glb_embeds_lod2_texture_and_uses_lod1_fallback(self):
        pipeline = load_pipeline_module()
        fixture = ROOT / "tests" / "fixtures" / "tiny_mixed_lod_6697_op.gml"
        with tempfile.TemporaryDirectory() as temporary:
            source_root = Path(temporary) / "source"
            source_root.mkdir()
            gml = source_root / fixture.name
            gml.write_bytes(fixture.read_bytes())
            texture = source_root / "tiny_mixed_lod_6697_appearance" / "roof.jpg"
            texture.parent.mkdir()
            from PIL import Image
            Image.new("RGB", (2, 2), (220, 80, 40)).save(texture, format="JPEG")
            selection = Path(temporary) / "selection.json"
            polygons = pipeline.extract_buildings_lod1(
                gml, local_origin=(35.681200, 139.706720)
            )
            for polygon in polygons:
                polygon["source_gml"] = str(gml)
            selection.write_text(json.dumps({
                "coordinate_system": "local-enu",
                "origin": {"lat": 35.681200, "lon": 139.706720},
                "polygons": polygons,
            }), encoding="utf-8")
            manifest = Path(temporary) / "download-manifest.json"
            manifest.write_text(json.dumps({"files": [{
                "path": str(gml),
                "url": "https://assets.example.invalid/dataset/udx/bldg/tiny_mixed_lod_6697_op.gml",
            }]}), encoding="utf-8")
            glb = Path(temporary) / "city.glb"
            receipt = Path(temporary) / "city-glb-receipt.json"
            completed = subprocess.run([
                sys.executable,
                str(ROOT / "src" / "city_pipeline" / "citygml2glb.py"),
                "--selection", str(selection),
                "--out", str(glb),
                "--receipt", str(receipt),
                "--download-manifest", str(manifest),
            ], cwd=ROOT, capture_output=True, text=True, check=False)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            evidence = json.loads(receipt.read_text(encoding="utf-8"))
            self.assertEqual(evidence["buildings"], {"lod2": 1, "lod1_fallback": 1})
            self.assertEqual(evidence["surfaces"]["textured"], 1)
            self.assertEqual(len(evidence["textures"]), 1)
            self.assertGreater(evidence["triangles"], 2)
            payload = glb.read_bytes()
            self.assertEqual(payload[:4], b"glTF")
            json_length, json_type = struct.unpack_from("<II", payload, 12)
            self.assertEqual(json_type, 0x4E4F534A)
            document = json.loads(payload[20:20 + json_length].decode("utf-8"))
            self.assertTrue(document.get("images"))
            self.assertIn("bufferView", document["images"][0])
            self.assertNotIn("uri", document["images"][0])
            scene = __import__("trimesh").load(glb, force="scene")
            self.assertTrue(scene.geometry)

    def test_conversion_entrypoint_rejects_malformed_crs_contracts(self):
        fixture = (ROOT / "tests" / "fixtures" / "tiny_bldg_6697_op.gml").read_text(
            encoding="utf-8"
        )
        cases = {
            "wrong_epsg": (
                fixture.replace("/6697\"", "/4326\"", 1),
                "CityGML must declare EPSG:6697",
            ),
            "two_dimensions": (
                fixture.replace('srsDimension="3"', 'srsDimension="2"', 1),
                "EPSG:6697 CityGML must declare srsDimension=3",
            ),
        }
        for name, (gml_text, expected_error) in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                source = Path(temporary) / "source"
                source.mkdir()
                (source / "query_meta.json").write_text(
                    json.dumps({
                        "center_lat": 35.681236,
                        "center_lon": 139.706763,
                        "ns_m": 100,
                        "ew_m": 100,
                    }),
                    encoding="utf-8",
                )
                (source / "invalid_bldg_6697_op.gml").write_text(
                    gml_text,
                    encoding="utf-8",
                )
                completed = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / "src" / "city_pipeline" / "gml_lod1_extract.py"),
                        "--in",
                        str(source),
                        "--out",
                        str(Path(temporary) / "lod1.json"),
                    ],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn(expected_error, completed.stdout + completed.stderr)

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
