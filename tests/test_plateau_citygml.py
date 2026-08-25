from __future__ import annotations

import importlib.util
import copy
import io
import json
import math
import subprocess
import struct
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

import trimesh

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from plateau_citygml import (
    PlateauError, bounding_box, download_file, request_catalog, request_dataset_catalog,
    search_url, select_files,
    second_mesh_codes, third_mesh_codes,
)


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


def load_citygml2glb_module():
    path = ROOT / "src" / "city_pipeline" / "citygml2glb.py"
    spec = importlib.util.spec_from_file_location("citygml2glb", path)
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


def load_collider_glb_module():
    path = ROOT / "src" / "city_pipeline" / "mjcf_colliders2glb.py"
    spec = importlib.util.spec_from_file_location("mjcf_colliders2glb", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class PlateauCityGmlTest(unittest.TestCase):
    def test_texture_decode_preserves_jpeg_export_format(self):
        module = load_citygml2glb_module()
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "texture.jpg"
            from PIL import Image
            Image.new("RGB", (2, 2), (10, 20, 30)).save(source, format="JPEG")
            decoded = module.load_texture_image(source)
            self.assertEqual(decoded.format, "JPEG")
            self.assertEqual(decoded.mode, "RGB")
            embedded = io.BytesIO()
            decoded.save(embedded, format="JPEG")
            self.assertEqual(embedded.getvalue(), source.read_bytes())

    def test_texture_resolver_populates_and_reuses_shared_source_cache(self):
        module = load_citygml2glb_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            gml = root / "job" / "source" / "city.gml"
            gml.parent.mkdir(parents=True)
            gml.write_text("fixture", encoding="utf-8")
            cached_gml = root / "cache" / "objects" / "source-key" / "city.gml"
            cached_gml.parent.mkdir(parents=True)
            cached_gml.write_text("fixture", encoding="utf-8")
            sources = {gml.resolve(): {
                "url": "https://assets.example/dataset/udx/bldg/city.gml",
                "cache_path": str(cached_gml),
            }}
            resolver = module.TextureResolver(sources, fetch=True, enabled=True)
            texture = resolver.resolve(gml, "city_appearance/wall.jpg")
            self.assertFalse(texture.is_file())
            with mock.patch("urllib.request.urlopen", return_value=io.BytesIO(b"jpeg-fixture")):
                resolver.fetch_pending()
            self.assertTrue(texture.is_file())
            self.assertIn(root / "cache" / "objects" / "source-key" / "textures", texture.parents)
            self.assertEqual(resolver.records[str(texture)]["mode"], "cache-populated")

            reused = module.TextureResolver(sources, fetch=True, enabled=True)
            with mock.patch("urllib.request.urlopen") as urlopen:
                self.assertEqual(reused.resolve(gml, "city_appearance/wall.jpg"), texture)
            urlopen.assert_not_called()
            self.assertEqual(reused.records[str(texture)]["mode"], "cache-reused")

    def test_texture_resolver_downloads_in_parallel_and_keeps_record_order(self):
        module = load_citygml2glb_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            gml = root / "job" / "source" / "city.gml"
            gml.parent.mkdir(parents=True)
            gml.write_text("fixture", encoding="utf-8")
            cached_gml = root / "cache" / "objects" / "source-key" / "city.gml"
            cached_gml.parent.mkdir(parents=True)
            cached_gml.write_text("fixture", encoding="utf-8")
            resolver = module.TextureResolver({gml.resolve(): {
                "url": "https://assets.example/dataset/udx/bldg/city.gml",
                "cache_path": str(cached_gml),
            }}, fetch=True, enabled=True, workers=4)
            for index in range(8):
                resolver.resolve(gml, f"appearance/wall-{index}.jpg")

            lock = threading.Lock()
            active = 0
            maximum_active = 0

            def urlopen(_request, timeout):
                nonlocal active, maximum_active
                self.assertEqual(timeout, 180)
                with lock:
                    active += 1
                    maximum_active = max(maximum_active, active)
                time.sleep(0.02)
                with lock:
                    active -= 1
                return io.BytesIO(b"jpeg-fixture")

            with mock.patch("urllib.request.urlopen", side_effect=urlopen):
                resolver.fetch_pending()

            self.assertGreater(maximum_active, 1)
            self.assertEqual(list(resolver.records), sorted(resolver.records))
            self.assertEqual(len(resolver.records), 8)
            self.assertFalse(list(root.rglob("*.part")))

    def test_texture_worker_count_is_bounded(self):
        module = load_citygml2glb_module()
        for invalid in (0, 17, 1.5, True):
            with self.assertRaisesRegex(ValueError, "texture workers"):
                module.TextureResolver({}, fetch=True, enabled=True, workers=invalid)

    def test_mjcf_collider_debug_glb_converts_box_mesh_and_hfield(self):
        converter = load_collider_glb_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            hfield = root / "terrain.hf"
            with hfield.open("wb") as stream:
                stream.write(struct.pack("<ii4f", 2, 2, 0.0, 1.0, 2.0, 3.0))
            source = root / "world.xml"
            source.write_text('''<mujoco><asset>
  <hfield name="terrain" file="terrain.hf" size="5 4 3 1"/>
  <mesh name="triangle" vertex="0 0 0 1 0 0 0 1 0" face="0 1 2"/>
</asset><worldbody>
  <geom name="ground" type="hfield" hfield="terrain"/>
  <body pos="1 2 3"><geom name="building" type="box" size="1 2 3"/></body>
  <geom name="roof" type="mesh" mesh="triangle"/>
</worldbody></mujoco>''', encoding="utf-8")
            output = root / "colliders.glb"
            receipt_path = root / "receipt.json"
            receipt = converter.convert_mjcf_colliders(source, output, receipt_path)
            scene = trimesh.load(output, force="scene")
            self.assertTrue(scene.geometry)
            self.assertEqual(receipt["geom_counts"], {"box": 1, "hfield": 1, "mesh": 1})
            self.assertEqual(receipt["triangle_count"], 15)
            self.assertTrue(receipt_path.is_file())

    def test_mjcf_collider_debug_glb_honors_xyaxes(self):
        converter = load_collider_glb_module()
        transform = converter._element_transform(ET.fromstring(
            '<geom pos="1 2 3" xyaxes="0 1 0 -1 0 0"/>'
        ))
        self.assertEqual(list(transform[:3, 3]), [1.0, 2.0, 3.0])
        self.assertEqual(list(transform[:3, 0]), [0.0, 1.0, 0.0])
        self.assertEqual(list(transform[:3, 1]), [-1.0, 0.0, 0.0])
        self.assertEqual(list(transform[:3, 2]), [0.0, -0.0, 1.0])

    def test_shared_download_cache_reuses_verified_object_across_build_roots(self):
        payload = b"cached-citygml"
        item = {
            "city_code": "22203",
            "year": 2023,
            "url": "https://assets.example/533856.gml",
            "file_size": len(payload),
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            responses = [io.BytesIO(payload)]
            with mock.patch("urllib.request.urlopen", side_effect=responses) as urlopen:
                first = download_file(
                    item, root / "job-1", cache_root=root / "cache",
                )
                second = download_file(
                    item, root / "job-2", cache_root=root / "cache",
                )

            self.assertEqual(urlopen.call_count, 1)
            self.assertEqual(first["mode"], "downloaded")
            self.assertFalse(first["cache"]["hit"])
            self.assertEqual(second["mode"], "cache-reused")
            self.assertTrue(second["cache"]["hit"])
            self.assertIn(
                second["cache"]["materialization"], {"hardlink", "copy"},
            )
            if second["cache"]["materialization"] == "hardlink":
                self.assertEqual(
                    Path(second["path"]).stat().st_ino,
                    Path(second["cache"]["path"]).stat().st_ino,
                )
            self.assertEqual(Path(first["path"]).read_bytes(), payload)
            self.assertEqual(Path(second["path"]).read_bytes(), payload)
            self.assertEqual(first["sha256"], second["sha256"])

    def test_shared_download_cache_redownloads_corrupt_object(self):
        payload = b"valid-citygml"
        item = {
            "city_code": "22203",
            "year": 2023,
            "url": "https://assets.example/533856.gml",
            "file_size": len(payload),
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with mock.patch(
                "urllib.request.urlopen",
                side_effect=[io.BytesIO(payload), io.BytesIO(payload)],
            ) as urlopen:
                first = download_file(
                    item, root / "job-1", cache_root=root / "cache",
                )
                Path(first["cache"]["path"]).write_bytes(b"corrupt")
                repaired = download_file(
                    item, root / "job-2", cache_root=root / "cache",
                )

            self.assertEqual(urlopen.call_count, 2)
            self.assertEqual(repaired["mode"], "downloaded")
            self.assertFalse(repaired["cache"]["hit"])
            self.assertEqual(Path(repaired["path"]).read_bytes(), payload)

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

    def test_third_mesh_bounds_round_trip_contains_source_bbox(self):
        from tools.plateau_citygml import third_mesh_bounds
        bbox = bounding_box(35.6625, 139.70625, 500.0, 500.0)
        for code in third_mesh_codes(bbox):
            west, south, east, north = third_mesh_bounds(code)
            self.assertLess(west, east)
            self.assertLess(south, north)
        west, south, east, north = third_mesh_bounds("53393586")
        self.assertAlmostEqual(west, 139.7)
        self.assertAlmostEqual(south, 35.65)
        self.assertAlmostEqual(east, 139.7125)
        self.assertAlmostEqual(north, 35.65833333333333)

    def test_bridge_search_uses_broader_second_mesh_catalog_index(self):
        bbox = bounding_box(34.39870318724743, 132.47669631395575, 100.0, 100.0)
        self.assertEqual(second_mesh_codes(bbox), ["513243"])
        url = search_url("https://api.example", "brid", bbox, mesh_level=2)
        self.assertIn("/datacatalog/citygml/m:513243?types=brid", url)

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

    def test_optional_feature_may_have_no_catalog_files(self):
        self.assertEqual(
            select_files({"cities": []}, "frn", "latest", allow_empty=True),
            [],
        )

    def test_bridge_selection_requires_lod3(self):
        payload = {"cities": [{
            "cityCode": "34100", "year": 2024, "registrationYear": 2024,
            "files": {"brid": [
                {"code": "lod2", "maxLod": 2, "url": "https://example/lod2.gml", "fileSize": 1},
                {"code": "lod3", "maxLod": 3, "url": "https://example/lod3.gml", "fileSize": 2},
            ]},
        }]}
        selected = select_files(payload, "brid", "latest", min_lod=3)
        self.assertEqual([item["code"] for item in selected], ["lod3"])

    def test_optional_catalog_404_is_reported_as_not_available(self):
        error = urllib.error.HTTPError(
            "https://api.example/frn", 404, "Not Found", {}, io.BytesIO()
        )
        with mock.patch("urllib.request.urlopen", side_effect=error):
            payload = request_catalog(
                "https://api.example/frn", allow_not_found=True
            )
        self.assertEqual(payload["cities"], [])
        self.assertEqual(payload["_catalog_status"]["status"], "not_available")

    def test_required_catalog_404_remains_an_error(self):
        error = urllib.error.HTTPError(
            "https://api.example/dem", 404, "Not Found", {}, io.BytesIO()
        )
        with mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(PlateauError):
                request_catalog("https://api.example/dem")

    def test_dataset_catalog_requires_citygml_array(self):
        response = mock.MagicMock()
        response.__enter__.return_value = io.StringIO(json.dumps({
            "citygml": [{
                "city_code": "22203", "city": "沼津市", "year": 2023,
                "spec": "3.4", "feature_types": ["bldg", "dem", "tran"],
            }],
        }))
        with mock.patch("urllib.request.urlopen", return_value=response):
            payload = request_dataset_catalog("https://api.example")
        self.assertEqual(payload["citygml"][0]["city_code"], "22203")

        invalid = mock.MagicMock()
        invalid.__enter__.return_value = io.StringIO(json.dumps({"datasets": []}))
        with mock.patch("urllib.request.urlopen", return_value=invalid):
            with self.assertRaisesRegex(PlateauError, "citygml array"):
                request_dataset_catalog("https://api.example")

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

    def test_lod1_extractor_records_invalid_building_when_issue_sink_is_provided(self):
        extractor = load_pipeline_module()
        source_text = (ROOT / "tests" / "fixtures" / "concave_bldg_6697_op.gml").read_text(
            encoding="utf-8"
        )
        valid_ring = (
            "35.681200 139.706720 10 35.681200 139.706742 10 "
            "35.681209 139.706742 10 35.681209 139.706731 10 "
            "35.681218 139.706731 10 35.681218 139.706720 10 "
            "35.681200 139.706720 10"
        )
        crossed_ring = (
            "35.681200 139.706720 10 35.681218 139.706742 10 "
            "35.681200 139.706742 10 35.681218 139.706720 10 "
            "35.681200 139.706720 10"
        )
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "invalid_bldg_6697_op.gml"
            source.write_text(source_text.replace(valid_ring, crossed_ring), encoding="utf-8")
            issues = []
            polygons = extractor.extract_buildings_lod1(
                source, local_origin=(35.681200, 139.706720), issues=issues,
            )
        self.assertEqual(polygons, [])
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["building_id"], "concave-building")
        self.assertEqual(issues[0]["reason_code"], "invalid_lod1_bottom_polygon")

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
            wall_output = json.loads(
                (Path(temporary) / "walls-0.2.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(wall_output["wall_roofs"]), 1)
            self.assertEqual(wall_output["wall_roofs"][0]["vertices"], polygon["vertices"])
            self.assertEqual(len(outputs[0.3]), 1)
            self.assertEqual(outputs[0.3][0]["mode"], "obb")
            obb_output = json.loads(
                (Path(temporary) / "walls-0.3.json").read_text(encoding="utf-8")
            )
            self.assertEqual(obb_output["wall_roofs"], [])
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

            mjcf = Path(temporary) / "buildings.xml"
            subprocess.run([
                sys.executable,
                str(ROOT / "src" / "city_pipeline" / "obb2mjcf.py"),
                "--inp", str(output), "--out", str(mjcf),
                "--collide", "drone", "--roof-thickness", "0.02",
            ], cwd=ROOT, check=True, capture_output=True, text=True)
            parsed = ET.parse(mjcf).getroot()
            roof_meshes = parsed.findall("asset/mesh")
            roof_geoms = [
                geom for geom in parsed.findall("worldbody/body/geom")
                if geom.get("name", "").startswith("roof_")
            ]
            self.assertEqual(len(roof_meshes), len(roof_geoms))
            self.assertGreater(len(roof_meshes), 0)
            self.assertTrue(all(
                (geom.get("contype"), geom.get("conaffinity")) == ("1", "2")
                for geom in roof_geoms
            ))

            # Triangulated roof area is exterior 4x4 minus the 2x2 courtyard.
            area = 0.0
            for mesh in roof_meshes:
                values = [float(value) for value in mesh.get("vertex").split()]
                top = [values[index:index + 3] for index in range(0, 9, 3)]
                area += abs(
                    (top[1][0] - top[0][0]) * (top[2][1] - top[0][1])
                    - (top[1][1] - top[0][1]) * (top[2][0] - top[0][0])
                ) / 2.0
                self.assertTrue(all(point[2] == 5.0 for point in top))
                bottom_z = [values[index] for index in (11, 14, 17)]
                self.assertTrue(all(value == 4.98 for value in bottom_z))
            self.assertAlmostEqual(area, 12.0)

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

    def test_source_cache_directory_is_optional_and_validated(self):
        hako = load_hako_module()
        config = copy.deepcopy(hako.DEFAULT_CONFIG)
        self.assertIsNone(hako.resolve_config(config)["source"]["cache_dir"])
        config["source"]["cache_dir"] = "/tmp/plateau-cache"
        self.assertEqual(
            hako.resolve_config(config)["source"]["cache_dir"],
            "/tmp/plateau-cache",
        )
        config["source"]["cache_dir"] = ""
        with self.assertRaisesRegex(hako.ConfigError, "source.cache_dir"):
            hako.resolve_config(config)

    def test_building_physics_level_is_bounded(self):
        hako = load_hako_module()
        for level in range(4):
            config = copy.deepcopy(hako.DEFAULT_CONFIG)
            config["mjcf"]["building_physics_level"] = level
            self.assertEqual(
                hako.resolve_config(config)["mjcf"]["building_physics_level"], level
            )
        config = copy.deepcopy(hako.DEFAULT_CONFIG)
        config["mjcf"]["building_physics_level"] = 4
        with self.assertRaisesRegex(hako.ConfigError, "building_physics_level"):
            hako.resolve_config(config)

    def test_city_world_parallel_workers_are_bounded(self):
        hako = load_hako_module()
        for workers in (1, 4, 16):
            config = copy.deepcopy(hako.DEFAULT_CONFIG)
            config["city_world"]["parallel_workers"] = workers
            self.assertEqual(
                hako.resolve_config(config)["city_world"]["parallel_workers"], workers
            )
        for invalid in (0, 17, 1.5, True):
            config = copy.deepcopy(hako.DEFAULT_CONFIG)
            config["city_world"]["parallel_workers"] = invalid
            with self.assertRaisesRegex(hako.ConfigError, "parallel_workers"):
                hako.resolve_config(config)

    def test_city_world_dem_parallel_workers_are_bounded(self):
        hako = load_hako_module()
        for workers in (1, 2, 4):
            config = copy.deepcopy(hako.DEFAULT_CONFIG)
            config["city_world"]["dem_parallel_workers"] = workers
            self.assertEqual(
                hako.resolve_config(config)["city_world"]["dem_parallel_workers"],
                workers,
            )
        for invalid in (0, 5, 1.5, True):
            config = copy.deepcopy(hako.DEFAULT_CONFIG)
            config["city_world"]["dem_parallel_workers"] = invalid
            with self.assertRaisesRegex(hako.ConfigError, "dem_parallel_workers"):
                hako.resolve_config(config)

    def test_city_world_terrain_uncovered_policy_is_guarded(self):
        hako = load_hako_module()
        for policy in ("error", "constant"):
            config = copy.deepcopy(hako.DEFAULT_CONFIG)
            config["city_world"]["terrain_uncovered_policy"] = policy
            self.assertEqual(
                hako.resolve_config(config)["city_world"]["terrain_uncovered_policy"],
                policy,
            )
        config = copy.deepcopy(hako.DEFAULT_CONFIG)
        config["city_world"]["terrain_uncovered_policy"] = "nearest"
        with self.assertRaisesRegex(hako.ConfigError, "terrain_uncovered_policy"):
            hako.resolve_config(config)

    def test_parallel_command_groups_preserve_dependencies_inside_each_group(self):
        hako = load_hako_module()
        observed = []
        with mock.patch.object(hako, "_run", side_effect=lambda command: observed.append(command[0])):
            hako._run_groups([[['a'], ['b']], [['c']], [['d']]], max_workers=3)
        self.assertEqual(sorted(observed), ['a', 'b', 'c', 'd'])
        self.assertLess(observed.index('a'), observed.index('b'))

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
            self.assertEqual(document["images"][0]["mimeType"], "image/jpeg")
            self.assertNotIn("uri", document["images"][0])
            self.assertIn(texture.read_bytes(), payload)
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

    def test_lod1_file_parallelism_preserves_deterministic_merge(self):
        fixture = ROOT / "tests" / "fixtures" / "tiny_bldg_6697_op.gml"
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source"
            source.mkdir()
            (source / "query_meta.json").write_text(json.dumps({
                "center_lat": 35.681236,
                "center_lon": 139.706763,
                "ns_m": 100,
                "ew_m": 100,
            }), encoding="utf-8")
            for index in range(2):
                (source / f"mesh-{index}_bldg_6697_op.gml").write_bytes(
                    fixture.read_bytes()
                )
            outputs = []
            for workers in (1, 2):
                output = Path(temporary) / f"lod1-{workers}.json"
                completed = subprocess.run([
                    sys.executable,
                    str(ROOT / "src" / "city_pipeline" / "gml_lod1_extract.py"),
                    "--in", str(source), "--out", str(output),
                    "--workers", str(workers),
                ], cwd=ROOT, capture_output=True, text=True, check=False)
                self.assertEqual(
                    completed.returncode, 0, completed.stdout + completed.stderr
                )
                outputs.append(json.loads(output.read_text(encoding="utf-8")))
            self.assertEqual(outputs[0], outputs[1])
            self.assertEqual(outputs[1]["deduplicated_buildings"], 1)

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
