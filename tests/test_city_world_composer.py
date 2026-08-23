#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import trimesh

SCRIPT = Path(__file__).parents[1] / "src" / "city_pipeline" / "city_world_composer.py"
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("city_world_composer", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
composer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(composer)


class CityWorldComposerTest(unittest.TestCase):
    def test_composes_mjcf_and_rebases_hfield_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            terrain = root / "terrain"
            buildings = root / "buildings"
            world = root / "world"
            terrain.mkdir(); buildings.mkdir(); world.mkdir()
            (terrain / "terrain.hf").write_bytes(b"test")
            (terrain / "terrain.xml").write_text(
                '<mujoco><asset><hfield name="ground" file="terrain.hf"/></asset>'
                '<worldbody><geom name="terrain" type="hfield" hfield="ground"/></worldbody></mujoco>',
                encoding="utf-8",
            )
            (buildings / "buildings.xml").write_text(
                '<mujoco><size nconmax="10"/><worldbody>'
                '<body name="building"><geom name="wall" type="box" size="1 1 1"/></body>'
                '</worldbody></mujoco>',
                encoding="utf-8",
            )
            output = world / "city-world.xml"
            counts = composer.compose_mjcf(
                terrain / "terrain.xml", buildings / "buildings.xml", output
            )
            self.assertEqual(counts, {"terrain": 1, "buildings": 1, "total": 2})
            parsed = ET.parse(output).getroot()
            self.assertEqual(parsed.find("asset/hfield").get("file"), "../terrain/terrain.hf")
            self.assertIsNotNone(parsed.find("worldbody/geom[@name='terrain']"))
            self.assertIsNotNone(parsed.find("worldbody/body[@name='building']"))

    def test_composes_three_glb_components(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = []
            for index in range(3):
                path = root / f"component-{index}.glb"
                scene = trimesh.Scene(trimesh.creation.box(extents=(1, 1, 1)))
                path.write_bytes(scene.export(file_type="glb"))
                inputs.append(path)
            output = root / "world.glb"
            counts = composer.compose_glb(inputs, output)
            self.assertEqual(sum(counts.values()), 3)
            self.assertEqual(len(trimesh.load(output, force="scene").geometry), 3)

    def test_composes_independent_bridge_physics_component(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            terrain = root / "terrain.xml"
            buildings = root / "buildings.xml"
            bridges = root / "bridges.xml"
            output = root / "world" / "city-world.xml"
            terrain.write_text('<mujoco><worldbody><geom name="terrain" type="box" size="1 1 1"/></worldbody></mujoco>')
            buildings.write_text('<mujoco><worldbody><geom name="building" type="box" size="1 1 1"/></worldbody></mujoco>')
            bridges.write_text(
                '<mujoco><asset><mesh name="bridge_mesh" '
                'vertex="0 0 1 1 0 1 0 1 1 0 0 .9 1 0 .9 0 1 .9" '
                'face="0 1 2 5 4 3 0 3 4 0 4 1 1 4 5 1 5 2 2 5 3 2 3 0"/>'
                '</asset><worldbody><geom name="bridge" type="mesh" mesh="bridge_mesh"/></worldbody></mujoco>'
            )
            counts = composer.compose_mjcf(terrain, buildings, output, [bridges])
            self.assertEqual(
                counts, {"terrain": 1, "buildings": 1, "bridges": 1, "total": 3}
            )
            parsed = ET.parse(output).getroot()
            self.assertIsNotNone(parsed.find("asset/mesh[@name='bridge_mesh']"))
            self.assertIsNotNone(parsed.find("worldbody/geom[@name='bridge']"))


if __name__ == "__main__":
    unittest.main()
