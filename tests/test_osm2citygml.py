"""OpenStreetMap -> CityGML LOD1 (src/city_pipeline/osm2citygml.py), the
EPSG:4326 input path and the local-files source, from map data made up here in
local metres (no network)."""

from __future__ import annotations

import json
import math
import re
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "city_pipeline"))
sys.path.insert(0, str(ROOT / "tools"))

import geodesy  # noqa: E402
import gml_lod1_extract  # noqa: E402
import osm2citygml  # noqa: E402
import road_terrain_probe  # noqa: E402

BOX = osm2citygml.Box.of(35.0, 135.0, 35.001, 135.0012)  # about 109 m x 111 m
CENTER = BOX.center
SPEC = (ROOT / "docs" / "osm-to-citygml.md").read_text(encoding="utf-8")
L_SHAPE = [(-30, 10), (-10, 10), (-10, 15), (-25, 15), (-25, 30), (-30, 30)]


def latlon(x, y):
    lat, lon, _ = geodesy.local_enu_to_geodetic([(x, y, 0.0)], *CENTER, 4326)[0]
    return lat, lon


class Overpass:
    """Overpass JSON built from shapes in local metres."""

    def __init__(self):
        self.elements, self.next_node = [], 1

    def way(self, way_id, points, tags, closed=False):
        ids = []
        for x, y in points:
            lat, lon = latlon(x, y)
            self.elements.append({"type": "node", "id": self.next_node, "lat": lat, "lon": lon})
            ids.append(self.next_node)
            self.next_node += 1
        self.elements.append({"type": "way", "id": way_id, "nodes": ids + ids[:1] if closed else ids, "tags": tags})

    def data(self):
        return {"osm3s": {"timestamp_osm_base": "2026-09-01T00:00:00Z"}, "elements": self.elements}


def sample():
    osm = Overpass()
    osm.way(101, L_SHAPE, {"building": "yes", "height": "12 m", "name": "L"}, closed=True)
    osm.way(102, [(0, 10), (10, 10), (10, 20), (0, 20)], {"building": "apartments", "building:levels": "4"}, closed=True)
    osm.way(103, [(20, 10), (30, 10), (30, 20), (20, 20)], {"building": "roof", "height": "6"}, closed=True)
    osm.way(104, [(35, -10), (45, -10), (45, 0), (35, 0)], {"building": "warehouse"}, closed=True)
    osm.way(106, [(0, -20), (1, -20), (1, -19), (0, -19)], {"building": "shed"}, closed=True)  # 1 m²: too small
    # A courtyard building from two half rings and an inner ring.
    osm.way(201, [(-30, -40), (-10, -40), (-10, -20)], {})
    osm.way(202, [(-10, -20), (-30, -20), (-30, -40)], {})
    osm.way(203, [(-25, -35), (-15, -35), (-15, -25), (-25, -25), (-25, -35)], {})
    osm.elements.append({"type": "relation", "id": 300, "tags": {"type": "multipolygon", "building": "school"},
                         "members": [{"type": "way", "ref": 201, "role": "outer"},
                                     {"type": "way", "ref": 202, "role": "outer"},
                                     {"type": "way", "ref": 203, "role": "inner"}]})
    osm.way(401, [(-45, 0), (0, 0), (20, -30)], {"highway": "primary", "name": "Main"})
    osm.way(402, [(40, -50), (40, 50)], {"highway": "residential", "lanes": "3", "width": "8"})
    osm.way(403, [(-40, -50), (-40, 50)], {"highway": "footway"})  # not a road for cars
    osm.elements.append({"type": "way", "id": 107, "nodes": [9990, 9991, 9992, 9990], "tags": {"building": "yes"}})
    return osm.data()


class ConversionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.out = Path(cls.directory.name)
        cls.receipt = osm2citygml.run(BOX, cls.out, "sample", osm_json=sample(), manifest=cls.out / "hakoniwa-build.yaml")
        cls.bldg = cls.out / "sample_bldg_op.gml"
        cls.tran = cls.out / "sample_tran_op.gml"
        records = gml_lod1_extract.extract_buildings_lod1(cls.bldg, local_origin=CENTER)
        cls.buildings = {record["id"]: record for record in records}

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_the_citygml_declares_three_dimensional_epsg4326(self):
        for path in (self.bldg, self.tran):
            root = ET.parse(path).getroot()
            self.assertEqual(gml_lod1_extract.validate_crs_contract(root, path), 4326)
        self.assertEqual(self.receipt["source_crs"], "EPSG:4326")

    def test_a_footprint_comes_back_through_the_pipeline_extractor(self):
        house = self.buildings["osm_w101"]
        self.assertEqual(len(house["vertices"]), 6)
        for expected in L_SHAPE:
            self.assertLess(min(math.dist(expected, point) for point in house["vertices"]), 0.005)
        self.assertEqual((house["zmin"], house["zmax"]), (0.0, 12.0))
        self.assertEqual(house["source_crs"], "EPSG:4326")

    def test_missing_heights_are_filled_and_canopies_raised(self):
        self.assertEqual(self.buildings["osm_w102"]["zmax"], 12.0)  # 4 levels x 3 m
        self.assertEqual(self.buildings["osm_w104"]["zmax"], osm2citygml.HEIGHTS_BY_KIND["warehouse"])
        canopy = self.buildings["osm_w103"]
        self.assertEqual((canopy["zmin"], canopy["zmax"]), (6.0 - osm2citygml.ROOF_SLAB_M, 6.0))
        self.assertEqual(self.receipt["assumed"], {"building_height": 3, "road_width": 1, "road_lanes": 1})

    def test_courtyards_are_kept_as_holes(self):
        school = self.buildings["osm_r300"]
        self.assertEqual(len(school["interior_rings"]), 1)
        from shapely.geometry import Polygon
        self.assertAlmostEqual(Polygon(school["vertices"], school["interior_rings"]).area, 300.0, delta=0.5)

    def test_provenance_and_attributes_travel_with_each_building(self):
        root = ET.parse(self.bldg).getroot()
        ns = {"bldg": osm2citygml.NAMESPACES["bldg"], "gen": osm2citygml.NAMESPACES["gen"],
              "gml": osm2citygml.NAMESPACES["gml"]}
        building = root.find(".//bldg:Building[@gml:id='osm_w101']", ns)
        attributes = {item.get("name"): item.findtext("gen:value", namespaces=ns)
                      for item in building.findall("gen:stringAttribute", ns)}
        self.assertEqual(attributes["source_provider"], "openstreetmap")
        self.assertEqual(attributes["source_id"], "101")
        self.assertEqual(attributes["height_source"], "height")
        self.assertEqual(attributes["osm:height"], "12 m")
        self.assertEqual(building.findtext("gml:name", namespaces=ns), "L")
        self.assertEqual(building.findtext("bldg:measuredHeight", namespaces=ns), "12")
        self.assertEqual(self.receipt["attribution"], osm2citygml.ATTRIBUTION)
        self.assertEqual(self.receipt["license"], "ODbL-1.0")
        self.assertEqual(self.receipt["data_timestamp"], "2026-09-01T00:00:00Z")

    def test_roads_are_widened_centre_lines(self):
        roads = road_terrain_probe.extract_lod1_roads(self.tran, *CENTER, 200.0, 200.0)
        areas = {}
        for road_id, polygon in roads:
            key = road_id.rsplit("-", 2)[0]
            areas[key] = areas.get(key, 0.0) + polygon.area
        main_length = 45 + math.hypot(20, 30)
        # Width 6.5 m (2 lanes x 3.25 m); the round bend adds a little.
        self.assertAlmostEqual(areas["osm_w401"], main_length * 6.5, delta=main_length * 6.5 * 0.02)
        self.assertAlmostEqual(areas["osm_w402"], 100 * 8.0, delta=1.0)
        self.assertNotIn("osm_w403", areas)

    def test_what_was_left_out_is_reported(self):
        skipped = {item["source"]: item["reason"] for item in self.receipt["skipped"]}
        self.assertEqual(set(skipped), {"way/106", "way/107"})
        self.assertEqual((self.receipt["buildings"], self.receipt["roads"]), (5, 2))

    def test_the_output_is_deterministic(self):
        with tempfile.TemporaryDirectory() as other:
            osm2citygml.run(BOX, Path(other), "sample", osm_json=sample())
            for name in ("sample_bldg_op.gml", "sample_tran_op.gml"):
                self.assertEqual((Path(other) / name).read_bytes(), (self.out / name).read_bytes())

    def test_a_city_world_builds_from_the_local_files_on_flat_ground(self):
        manifest = self.out / "hakoniwa-build.yaml"
        completed = subprocess.run([sys.executable, str(ROOT / "tools" / "hako.py"), "build", "--config", str(manifest)],
                                   capture_output=True, text=True, check=False, timeout=600)
        self.assertEqual(completed.returncode, 0, completed.stdout[-3000:] + completed.stderr[-3000:])
        world = self.out / "build" / "world"
        mjcf = ET.parse(world / "city-world.xml").getroot()
        bodies = {body.get("name") for body in mjcf.iter("body")}
        self.assertTrue({"body_osm_w101", "body_osm_w103", "body_osm_r300"} <= bodies)
        canopy = next(body for body in mjcf.iter("body") if body.get("name") == "body_osm_w103").find("geom")
        self.assertAlmostEqual(float(canopy.get("pos").split()[2]), 6.0 - osm2citygml.ROOF_SLAB_M / 2, delta=0.01)
        validation = json.loads((world / "dataset-validation.json").read_text(encoding="utf-8"))
        self.assertEqual(validation["components"]["terrain"]["dem"], "not_available")
        manifest_record = json.loads((self.out / "build" / "download-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual({item["mode"] for item in manifest_record["files"]}, {"local"})
        self.assertTrue((world / "city-world.glb").is_file())


class SharedWallTest(unittest.TestCase):
    def test_neighbours_sharing_a_wall_do_not_overlap(self):
        # OSM draws a shared wall with the same nodes, here one slightly off the
        # straight line: both footprints keep it, so they only touch.
        osm = Overpass()
        wall = [(10, 0), (10.02, 5), (10, 10)]
        osm.way(1, [(0, 0), *wall, (0, 10)], {"building": "yes"}, closed=True)
        osm.way(2, [(20, 0), (20, 10), *reversed(wall)], {"building": "yes"}, closed=True)
        with tempfile.TemporaryDirectory() as directory:
            osm2citygml.run(BOX, Path(directory), "wall", osm_json=osm.data())
            records = gml_lod1_extract.extract_buildings_lod1(Path(directory) / "wall_bldg_op.gml", local_origin=CENTER)
        from shapely.geometry import Polygon
        first, second = (Polygon(record["vertices"]) for record in records)
        self.assertEqual((len(first.exterior.coords) - 1, len(second.exterior.coords) - 1), (5, 5))
        self.assertLess(first.intersection(second).area, 1e-6)


class GeoJsonTest(unittest.TestCase):
    def test_buildings_with_holes_and_roads_from_a_feature_collection(self):
        outer = [list(reversed(latlon(x, y))) for x, y in [(0, 0), (20, 0), (20, 20), (0, 20), (0, 0)]]
        hole = [list(reversed(latlon(x, y))) for x, y in [(5, 5), (5, 15), (15, 15), (15, 5), (5, 5)]]
        road = [list(reversed(latlon(x, y))) for x, y in [(-30, -20), (30, -20)]]
        data = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "id": "way/7", "properties": {"building": "office", "height": 30},
             "geometry": {"type": "Polygon", "coordinates": [outer, hole]}},
            {"type": "Feature", "properties": {"highway": "tertiary"},
             "geometry": {"type": "LineString", "coordinates": road}},
        ]}
        with tempfile.TemporaryDirectory() as directory:
            receipt = osm2citygml.run(BOX, Path(directory), "geo", geojson=data)
            records = gml_lod1_extract.extract_buildings_lod1(Path(directory) / "geo_bldg_op.gml", local_origin=CENTER)
        self.assertEqual((receipt["provider"], receipt["buildings"], receipt["roads"]), ("geojson", 1, 1))
        self.assertNotIn("attribution", receipt)
        self.assertEqual(records[0]["id"], "geojson_w7")
        self.assertEqual((records[0]["zmax"], len(records[0]["interior_rings"])), (30.0, 1))


class CommandAndConfigTest(unittest.TestCase):
    def test_the_command_converts_a_saved_overpass_file(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "osm.json"
            source.write_text(json.dumps(sample()), encoding="utf-8")
            bbox = f"{BOX.south},{BOX.west},{BOX.north},{BOX.east}"
            completed = subprocess.run([sys.executable, str(ROOT / "src" / "city_pipeline" / "osm2citygml.py"),
                                        "--bbox", bbox, "--osm-json", str(source), "--out-dir", directory],
                                       capture_output=True, text=True, check=False, timeout=60)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("5 buildings, 2 roads", completed.stdout)

    def test_a_too_large_area_is_refused(self):
        with self.assertRaisesRegex(osm2citygml.OsmConversionError, "2000 m"):
            osm2citygml.convert([], osm2citygml.Box.of(35.0, 135.0, 35.1, 135.1))

    def test_the_source_kind_is_checked(self):
        import hako

        base = {"version": 1, "component": "hakoniwa-envsim"}
        with self.assertRaisesRegex(hako.ConfigError, "requires source.path"):
            hako.resolve_config({**base, "source": {"kind": "files"}})
        with self.assertRaisesRegex(hako.ConfigError, "only with source.kind files"):
            hako.resolve_config({**base, "source": {"path": "somewhere"}})
        # Local files need buildings and roads for a City World, not a DEM or markings.
        cfg = hako.resolve_config({**base, "source": {"kind": "files", "path": "x", "feature_types": {
            "bldg": True, "tran": True, "dem": False, "frn": False, "brid": False}},
            "city_world": {"enabled": True, "terrain_uncovered_policy": "constant"}})
        self.assertEqual(cfg["source"]["kind"], "files")
        with self.assertRaisesRegex(hako.ConfigError, "bldg, tran, dem, and frn|bldg, tran, dem, frn"):
            hako.resolve_config({**base, "city_world": {"enabled": True}})

    def test_lengths_in_tags(self):
        self.assertEqual([osm2citygml.parse_length(value) for value in ("12", "12 m", "12,5m", "10 ft", "tall", None)],
                         [12.0, 12.0, 12.5, 3.048, None, None])


class SpecTest(unittest.TestCase):
    """docs/osm-to-citygml.md is the conversion spec: its tables must match the code."""

    @staticmethod
    def block(name):
        match = re.search(rf"<!-- {name} -->(.*?)<!-- /{name} -->", SPEC, re.S)
        assert match, name
        return match.group(1)

    def table(self, name):
        return {key: float(value) for key, value in re.findall(r"^\| `([^`]+)` \| ([0-9.]+) \|$", self.block(name), re.M)}

    def test_tables(self):
        self.assertEqual(self.table("heights-by-kind"), osm2citygml.HEIGHTS_BY_KIND)
        self.assertEqual(self.table("lanes-by-class"),
                         {key: float(value) for key, value in osm2citygml.LANES_BY_CLASS.items()})
        self.assertEqual(set(re.findall(r"`([^`]+)`", self.block("excluded-highways"))), osm2citygml.EXCLUDED_HIGHWAYS)

    def test_constants_named_in_the_text(self):
        for text, value in (("**3 m**", osm2citygml.LEVEL_HEIGHT_M), ("**9 m**", osm2citygml.DEFAULT_HEIGHT_M),
                            ("**0.5 m**", osm2citygml.ROOF_SLAB_M), ("**3.25 m**", osm2citygml.LANE_WIDTH_M),
                            ("**2**", osm2citygml.DEFAULT_LANES), ("**2000 m**", osm2citygml.MAX_SIDE_M),
                            ("**1 mm**", osm2citygml.MIN_STEP_M * 1000), ("**2.5 cm**", osm2citygml.ROAD_SIMPLIFY_M * 100), ("**4 m²**", osm2citygml.MIN_BUILDING_AREA_M2),
                            ("**1 m**", osm2citygml.MIN_ROAD_LENGTH_M), ("**EPSG:4326**", osm2citygml.EPSG)):
            with self.subTest(text=text):
                self.assertIn(text, SPEC)
                self.assertEqual(float(re.search(r"[0-9.]+", text).group()), value)
        self.assertIn(f"{osm2citygml.MIN_ROAD_WIDTH_M}〜{osm2citygml.MAX_ROAD_WIDTH_M:g} m", SPEC)
        self.assertIn(osm2citygml.overpass_query(osm2citygml.Box.of(1, 2, 3, 4))
                      .replace("1.0,2.0,3.0,4.0", "{s},{w},{n},{e}"), SPEC)


if __name__ == "__main__":
    unittest.main()
