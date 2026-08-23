"""MuJoCo 3.9.x bridge collision acceptance test.

Set MUJOCO_ROOT to an unpacked MuJoCo distribution.  The ordinary unit suite
skips this native integration test when that external runtime is unavailable.
"""

import json
import os
import platform
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "city_pipeline"))
import bridge2mjcf
import building_lod2_colliders
import obb2mjcf
from tests.test_bridge2mjcf import bridge_gml, world_frame


MUJOCO_ROOT = Path(os.environ["MUJOCO_ROOT"]).resolve() if os.environ.get("MUJOCO_ROOT") else None


@unittest.skipUnless(MUJOCO_ROOT and (MUJOCO_ROOT / "include/mujoco/mujoco.h").is_file(),
                     "MUJOCO_ROOT with MuJoCo headers is required")
class BridgeMuJoCoAcceptanceTest(unittest.TestCase):
    def _compile_probe(self, root: Path) -> Path:
        output = root / "mujoco_contact_probe"
        library_args = ["-L", str(MUJOCO_ROOT / "lib"), "-lmujoco"]
        if platform.system() == "Darwin":
            libraries = sorted((MUJOCO_ROOT / "lib").glob("libmujoco.*.dylib"))
            if not libraries:
                self.fail(f"MuJoCo dylib was not found under {MUJOCO_ROOT / 'lib'}")
            library_args = [str(libraries[-1])]
        command = [
            os.environ.get("CXX", "c++"), "-std=c++17",
            str(ROOT / "tests/native/mujoco_contact_probe.cpp"),
            "-I", str(MUJOCO_ROOT / "include"), *library_args, "-o", str(output),
        ]
        subprocess.run(command, check=True, capture_output=True, text=True)
        return output

    def _probe_model(self, bridge_xml: Path, output: Path, probe_z: float,
                     probe_x: float = 0.0, probe_y: float = 0.0):
        root = ET.parse(bridge_xml).getroot()
        worldbody = root.find("worldbody")
        body = ET.SubElement(worldbody, "body", {
            "name": "probe", "pos": f"{probe_x} {probe_y} {probe_z}",
        })
        ET.SubElement(body, "freejoint")
        ET.SubElement(body, "geom", {"name": "probe_geom", "type": "sphere", "size": "0.1"})
        ET.indent(root, space="  ")
        ET.ElementTree(root).write(output, encoding="unicode")

    def test_flat_bridge_has_top_contact_and_open_underpass(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            # A triangle centered around the ENU origin at source altitude 5m.
            surfaces = [[
                (34.99999, 138.99999, 5), (34.99999, 139.00001, 5),
                (35.00001, 139.0, 5), (34.99999, 138.99999, 5),
            ]]
            bridge_xml = root / "bridges.xml"
            bridge2mjcf.convert(
                bridge_gml(root / "fixture_brid_6697_op.gml", surfaces),
                world_frame(root / "world-frame.json"), bridge_xml, root / "receipt.json",
                None, 0.02, 60,
            )
            under = root / "under.xml"
            top = root / "top.xml"
            self._probe_model(bridge_xml, under, 1.0)
            self._probe_model(bridge_xml, top, 5.05)
            probe = self._compile_probe(root)
            environment = os.environ.copy()
            variable = "DYLD_LIBRARY_PATH" if platform.system() == "Darwin" else "LD_LIBRARY_PATH"
            environment[variable] = str(MUJOCO_ROOT / "lib")
            under_result = subprocess.run(
                [str(probe), str(under), "NONE"], env=environment,
                capture_output=True, text=True,
            )
            self.assertEqual(under_result.returncode, 0, under_result.stdout + under_result.stderr)
            top_result = subprocess.run(
                [str(probe), str(top), "bridge_piece_000000"], env=environment,
                capture_output=True, text=True,
            )
            self.assertEqual(top_result.returncode, 0, top_result.stdout + top_result.stderr)
            self.assertIn("MuJoCo 3.9.", top_result.stdout)

    def test_wall_mode_roof_contacts_over_footprint_and_preserves_concavity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            roof = {
                "id": "l-shape",
                "vertices": [[0, 0], [2, 0], [2, 1], [1, 1], [1, 2], [0, 2]],
                "interior_rings": [],
                "zmin": 0,
                "zmax": 5,
            }
            model = obb2mjcf.make_mjcf(
                [], wall_roofs=[roof], roof_thickness_m=0.02,
                pos_fn=lambda east, north, up: (north, -east, up),
            )
            source = root / "roof.xml"
            ET.indent(model, space="  ")
            ET.ElementTree(model).write(source, encoding="unicode")

            occupied = root / "occupied.xml"
            concavity = root / "concavity.xml"
            # ENU (0.5, 0.5) lies on the L-shaped roof; (1.5, 1.5) is its missing corner.
            self._probe_model(source, occupied, 5.05, 0.5, -0.5)
            self._probe_model(source, concavity, 5.05, 1.5, -1.5)
            probe = self._compile_probe(root)
            environment = os.environ.copy()
            variable = "DYLD_LIBRARY_PATH" if platform.system() == "Darwin" else "LD_LIBRARY_PATH"
            environment[variable] = str(MUJOCO_ROOT / "lib")
            occupied_result = subprocess.run(
                [str(probe), str(occupied), "ANY"], env=environment,
                capture_output=True, text=True,
            )
            self.assertEqual(
                occupied_result.returncode, 0,
                occupied_result.stdout + occupied_result.stderr,
            )
            concavity_result = subprocess.run(
                [str(probe), str(concavity), "NONE"], env=environment,
                capture_output=True, text=True,
            )
            self.assertEqual(
                concavity_result.returncode, 0,
                concavity_result.stdout + concavity_result.stderr,
            )

    def test_p3_outer_ceiling_has_contact_without_filling_space_below(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = ROOT / "tests/fixtures/p1_bldg_6697_op.gml"
            selection = root / "selection.json"
            selection.write_text(json.dumps({
                "origin": {"lat": 35.681210, "lon": 139.706730},
                "polygons": [{
                    "id": "p1-building",
                    "source_gml": str(source),
                    "vertices": [[-1, -1], [1, -1], [1, 1], [-1, 1]],
                    "interior_rings": [],
                    "zmin": 10,
                    "zmax": 21,
                }],
            }), encoding="utf-8")
            classification = root / "classification.json"
            classification.write_text(json.dumps({
                "buildings": [{"building_id": "p1-building", "class": "P3"}]
            }), encoding="utf-8")
            frame = root / "world-frame.json"
            frame.write_text(json.dumps({
                "schema_version": 1,
                "origin": {
                    "latitude": 35.681210,
                    "longitude": 139.706730,
                    "altitude_offset_m": 10,
                },
                "half_extent_m": {"north_south": 100, "east_west": 100},
                "coordinate_systems": {
                    "mjcf": "X=North,Y=-East,Z=Up",
                    "glb": "X=East,Y=Up,Z=-North",
                },
            }), encoding="utf-8")
            geometry = building_lod2_colliders.prepare_p3_geometry(
                selection, classification, frame, roof_thickness_m=0.02
            )
            model = ET.Element("mujoco")
            ET.SubElement(model, "asset")
            ET.SubElement(model, "worldbody")
            obb2mjcf.add_lod2_surface_pieces(
                model, geometry.pieces, "all", (0.62, 0.36, 0.80, 1.0)
            )
            source_xml = root / "p3.xml"
            ET.indent(model, space="  ")
            ET.ElementTree(model).write(source_xml, encoding="unicode")

            under = root / "under.xml"
            underside = root / "underside.xml"
            self._probe_model(source_xml, under, 1.0)
            self._probe_model(source_xml, underside, 4.95)
            probe = self._compile_probe(root)
            environment = os.environ.copy()
            variable = "DYLD_LIBRARY_PATH" if platform.system() == "Darwin" else "LD_LIBRARY_PATH"
            environment[variable] = str(MUJOCO_ROOT / "lib")
            under_result = subprocess.run(
                [str(probe), str(under), "NONE"], env=environment,
                capture_output=True, text=True,
            )
            self.assertEqual(
                under_result.returncode, 0,
                under_result.stdout + under_result.stderr,
            )
            underside_result = subprocess.run(
                [str(probe), str(underside), "ANY"], env=environment,
                capture_output=True, text=True,
            )
            self.assertEqual(
                underside_result.returncode, 0,
                underside_result.stdout + underside_result.stderr,
            )


if __name__ == "__main__":
    unittest.main()
