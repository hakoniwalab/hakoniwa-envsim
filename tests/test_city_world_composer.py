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
            composer.compose_mjcf(terrain / "terrain.xml", buildings / "buildings.xml", output)
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


if __name__ == "__main__":
    unittest.main()
