#!/usr/bin/env python3
"""Component-owned CLI for PLATEAU CityGML to MuJoCo wall conversion."""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Mapping

from plateau_citygml import (
    PlateauError,
    bounding_box,
    download_file,
    request_catalog,
    search_url,
    select_files,
    sha256_file,
    third_mesh_codes,
)

ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / "src" / "city_pipeline"
DEFAULT_MANIFEST = ROOT / "hakoniwa-build.yaml"

DEFAULT_CONFIG: dict[str, Any] = {
    "version": 1,
    "component": "hakoniwa-envsim",
    "pipeline": {"type": "plateau-citygml-to-assets"},
    "source": {
        "api_base_url": "https://api.plateauview.mlit.go.jp",
        "feature_type": "bldg",
        "feature_types": {"bldg": True, "tran": False, "dem": False, "frn": False},
        "year": "latest",
    },
    "selection": {
        "center": {"latitude": 35.681236, "longitude": 139.706763},
        "half_extent_m": {"north_south": 100.0, "east_west": 100.0},
    },
    "geometry": {
        "base_epsilon_m": 0.2,
        "waste_threshold": 1.0,
        "wall_thickness_m": 0.1,
    },
    "mjcf": {"model_name": "plateau_city", "collision": "all", "floor": False},
    "glb": {
        "enabled": True,
        "lod_policy": "highest_available",
        "texture_mode": "embedded-if-available",
    },
    "city_world": {
        "enabled": False,
        "terrain_spacing_m": 2.0,
        "marking_vertical_offset_m": 0.055,
    },
    "output": {
        "build_dir": ".hako/build/plateau-city-mjcf",
        "install_dir": ".hako/install",
        "name": "plateau-city",
    },
}


class ConfigError(RuntimeError):
    pass


def _strip_comment(text: str) -> str:
    quote: str | None = None
    escaped = False
    output: list[str] = []
    for char in text:
        if escaped:
            output.append(char); escaped = False
        elif char == "\\" and quote:
            output.append(char); escaped = True
        elif char in {"'", '"'}:
            quote = None if quote == char else char if quote is None else quote
            output.append(char)
        elif char == "#" and quote is None:
            break
        else:
            output.append(char)
    return "".join(output).rstrip()


def _parse_scalar(text: str) -> Any:
    value = text.strip()
    if not value:
        return {}
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"null", "~"}:
        return None
    if value.startswith(("'", '"')):
        if len(value) < 2 or value[-1] != value[0]:
            raise ConfigError(f"unterminated quoted scalar: {value}")
        return json.loads(value) if value[0] == '"' else value[1:-1].replace("''", "'")
    try:
        return int(value)
    except ValueError:
        try:
            return float(value)
        except ValueError:
            return value


def load_simple_yaml(path: Path) -> dict[str, Any]:
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if "\t" in raw:
            raise ConfigError(f"{path}:{lineno}: tabs are not allowed")
        line = _strip_comment(raw)
        if not line.strip():
            continue
        stripped = line.lstrip(" ")
        indent = len(line) - len(stripped)
        if stripped.startswith("-") or ":" not in stripped:
            raise ConfigError(f"{path}:{lineno}: expected a mapping entry")
        key, raw_value = stripped.split(":", 1)
        while stack and indent <= stack[-1][0]:
            stack.pop()
        if not key.strip() or not stack:
            raise ConfigError(f"{path}:{lineno}: invalid mapping")
        parent = stack[-1][1]
        if key.strip() in parent:
            raise ConfigError(f"{path}:{lineno}: duplicate key: {key.strip()}")
        value = _parse_scalar(raw_value)
        parent[key.strip()] = value
        if isinstance(value, dict):
            stack.append((indent, value))
    return root


def _merge_known(defaults: Mapping[str, Any], overrides: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    unknown = sorted(set(overrides) - set(defaults))
    if unknown:
        raise ConfigError(f"unknown key(s) under {prefix or 'root'}: {', '.join(unknown)}")
    result: dict[str, Any] = {}
    for key, default in defaults.items():
        value = overrides.get(key, default)
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(default, Mapping):
            if not isinstance(value, Mapping):
                raise ConfigError(f"{path} must be a mapping")
            result[key] = _merge_known(default, value, path)
        else:
            result[key] = value
    return result


def resolve_config(raw: Mapping[str, Any]) -> dict[str, Any]:
    cfg = _merge_known(DEFAULT_CONFIG, raw)
    if cfg["version"] != 1 or cfg["component"] != "hakoniwa-envsim":
        raise ConfigError("version must be 1 and component must be hakoniwa-envsim")
    if cfg["pipeline"]["type"] != "plateau-citygml-to-assets":
        raise ConfigError("pipeline.type must be plateau-citygml-to-assets")
    if cfg["source"]["feature_type"] != "bldg":
        raise ConfigError("source.feature_type currently supports only bldg")
    feature_types = cfg["source"]["feature_types"]
    if not all(isinstance(feature_types[name], bool) for name in ("bldg", "tran", "dem", "frn")):
        raise ConfigError("source.feature_types values must be boolean")
    if not feature_types["bldg"]:
        raise ConfigError("source.feature_types.bldg must be true")
    if not isinstance(cfg["source"]["api_base_url"], str) or not cfg["source"]["api_base_url"].startswith("https://"):
        raise ConfigError("source.api_base_url must be an HTTPS URL")
    year = cfg["source"]["year"]
    if year != "latest" and (not isinstance(year, int) or isinstance(year, bool) or year < 2000):
        raise ConfigError("source.year must be latest or a four-digit year")
    lat = cfg["selection"]["center"]["latitude"]
    lon = cfg["selection"]["center"]["longitude"]
    if not isinstance(lat, (int, float)) or not -90 <= lat <= 90:
        raise ConfigError("selection.center.latitude must be in [-90, 90]")
    if not isinstance(lon, (int, float)) or not -180 <= lon <= 180:
        raise ConfigError("selection.center.longitude must be in [-180, 180]")
    for key in ("north_south", "east_west"):
        value = cfg["selection"]["half_extent_m"][key]
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            raise ConfigError(f"selection.half_extent_m.{key} must be positive")
    for key in ("base_epsilon_m", "wall_thickness_m"):
        value = cfg["geometry"][key]
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            raise ConfigError(f"geometry.{key} must be positive")
    waste_threshold = cfg["geometry"]["waste_threshold"]
    if (
        not isinstance(waste_threshold, (int, float))
        or isinstance(waste_threshold, bool)
        or not 0 <= waste_threshold <= 1
    ):
        raise ConfigError("geometry.waste_threshold must be in [0, 1]")
    if cfg["mjcf"]["collision"] not in {"all", "drone", "none"}:
        raise ConfigError("mjcf.collision must be all, drone, or none")
    if not isinstance(cfg["mjcf"]["floor"], bool):
        raise ConfigError("mjcf.floor must be true or false")
    if not isinstance(cfg["glb"]["enabled"], bool):
        raise ConfigError("glb.enabled must be true or false")
    if cfg["glb"]["lod_policy"] != "highest_available":
        raise ConfigError("glb.lod_policy currently supports only highest_available")
    if cfg["glb"]["texture_mode"] not in {"embedded-if-available", "flat"}:
        raise ConfigError("glb.texture_mode must be embedded-if-available or flat")
    if not isinstance(cfg["city_world"]["enabled"], bool):
        raise ConfigError("city_world.enabled must be true or false")
    for key in ("terrain_spacing_m", "marking_vertical_offset_m"):
        value = cfg["city_world"][key]
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            raise ConfigError(f"city_world.{key} must be positive")
    if cfg["city_world"]["enabled"] and not all(feature_types.values()):
        raise ConfigError("city_world.enabled requires bldg, tran, dem, and frn feature types")
    for key in ("build_dir", "install_dir", "name"):
        if not isinstance(cfg["output"][key], str) or not cfg["output"][key]:
            raise ConfigError(f"output.{key} must be a non-empty string")
    if re.fullmatch(r"[A-Za-z0-9_.-]+", cfg["output"]["name"]) is None:
        raise ConfigError("output.name may contain only letters, digits, dot, underscore, and hyphen")
    return cfg


def _path(value: str) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def load_config(manifest: Path) -> dict[str, Any]:
    if not manifest.is_file():
        raise ConfigError(f"build manifest not found: {manifest}")
    return resolve_config(load_simple_yaml(manifest))


def _run(command: list[str]) -> None:
    print("+", " ".join(command))
    subprocess.run(command, cwd=ROOT, check=True)


def _query_meta(cfg: dict[str, Any]) -> dict[str, Any]:
    center = cfg["selection"]["center"]
    extent = cfg["selection"]["half_extent_m"]
    bbox = bounding_box(center["latitude"], center["longitude"], extent["north_south"], extent["east_west"])
    return {
        "center_lat": center["latitude"], "center_lon": center["longitude"],
        "ns_m": extent["north_south"], "ew_m": extent["east_west"],
        "bbox": {"west": bbox[0], "south": bbox[1], "east": bbox[2], "north": bbox[3]},
        "third_mesh_codes": third_mesh_codes(bbox),
    }


def configure(manifest: Path) -> int:
    cfg = load_config(manifest)
    build_dir = _path(cfg["output"]["build_dir"])
    (ROOT / ".hako").mkdir(exist_ok=True)
    build_dir.mkdir(parents=True, exist_ok=True)
    resolved = {"manifest": str(manifest.resolve()), "config": cfg, "query": _query_meta(cfg)}
    target = ROOT / ".hako" / "resolved-build.json"
    target.write_text(json.dumps(resolved, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"OK: resolved manifest: {target}")
    print(f"OK: build directory: {build_dir}")
    return 0


def doctor(manifest: Path) -> int:
    errors: list[str] = []
    try:
        cfg = load_config(manifest)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if sys.version_info < (3, 10):
        errors.append(f"Python 3.10 or newer is required; found {sys.version.split()[0]}")
    # Geometry dependencies are required contracts, not optional accelerators:
    # Shapely preserves ordered footprints and Earcut/Trimesh produce GLB.
    for module in ("numpy", "shapely", "mapbox_earcut", "trimesh", "PIL"):
        if importlib.util.find_spec(module) is None:
            errors.append(f"Python package is missing: {module}; run: python -m pip install -r requirements.txt")
    scripts = ["gml_lod1_extract.py", "gml2obb.py", "obb2mjcf.py", "citygml2glb.py"]
    if cfg["city_world"]["enabled"]:
        scripts.extend([
            "dem2hfield.py", "road_terrain_probe.py", "city_furniture2glb.py",
            "city_world_composer.py", "city_dataset_validator.py", "world_frame.py",
        ])
    for script in scripts:
        if not (PIPELINE / script).is_file():
            errors.append(f"pipeline tool is missing: src/city_pipeline/{script}")
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    query = _query_meta(cfg)
    bbox = query["bbox"]
    print(f"OK: Python {sys.version.split()[0]} and conversion dependencies")
    print(f"OK: PLATEAU center lat={query['center_lat']} lon={query['center_lon']}")
    print(f"OK: query bbox west={bbox['west']:.8f} south={bbox['south']:.8f} east={bbox['east']:.8f} north={bbox['north']:.8f}")
    print("OK: output coordinates use query-centered local ENU meters")
    print("INFO: PLATEAU Distribution Service is an external trial API; build records resolved URLs and SHA-256 values")
    return 0


def _convert(
    cfg: dict[str, Any],
    source_root: Path,
    build_dir: Path,
    download_manifest: Path | None = None,
    offline: bool = True,
) -> dict[str, Path]:
    output_name = cfg["output"]["name"]
    lod1 = build_dir / f"{output_name}-lod1.json"
    walls = build_dir / f"{output_name}-walls.json"
    geometry = cfg["geometry"]
    _run([
        sys.executable, str(PIPELINE / "gml_lod1_extract.py"),
        "--in", str(source_root), "--out", str(lod1),
        "--base-eps", str(geometry["base_epsilon_m"]),
    ])
    _run([
        sys.executable, str(PIPELINE / "gml2obb.py"),
        "--in", str(lod1), "--out", str(walls),
        "--waste-threshold", str(geometry["waste_threshold"]),
        "--wall-thickness", str(geometry["wall_thickness_m"]),
    ])
    city_world = cfg["city_world"]
    if city_world["enabled"]:
        components = build_dir / "components"
        terrain_dir = components / "terrain"
        roads_dir = components / "roads"
        markings_dir = components / "road-markings"
        buildings_dir = components / "buildings"
        world_dir = build_dir / "world"
        for directory in (terrain_dir, roads_dir, markings_dir, buildings_dir, world_dir):
            directory.mkdir(parents=True, exist_ok=True)

        terrain_xml = terrain_dir / "terrain.xml"
        _run([
            sys.executable, str(PIPELINE / "dem2hfield.py"),
            "--in", str(source_root), "--out", str(terrain_xml),
            "--latitude", str(cfg["selection"]["center"]["latitude"]),
            "--longitude", str(cfg["selection"]["center"]["longitude"]),
            "--north-south", str(cfg["selection"]["half_extent_m"]["north_south"]),
            "--east-west", str(cfg["selection"]["half_extent_m"]["east_west"]),
            "--spacing", str(city_world["terrain_spacing_m"]),
        ])
        world_frame = terrain_dir / "world-frame.json"
        terrain_receipt = terrain_dir / "terrain-receipt.json"

        buildings_xml = buildings_dir / "buildings.xml"
        command = [
            sys.executable, str(PIPELINE / "obb2mjcf.py"),
            "--inp", str(walls), "--zsrc", str(lod1), "--out", str(buildings_xml),
            "--model-name", cfg["mjcf"]["model_name"],
            "--collide", cfg["mjcf"]["collision"],
            "--world-frame", str(world_frame),
        ]
        if cfg["mjcf"]["floor"]:
            command.append("--floor")
        _run(command)

        buildings_glb = buildings_dir / "buildings.glb"
        buildings_glb_receipt = buildings_dir / "buildings-glb-receipt.json"
        glb_command = [
            sys.executable, str(PIPELINE / "citygml2glb.py"),
            "--selection", str(lod1), "--out", str(buildings_glb),
            "--receipt", str(buildings_glb_receipt),
            "--texture-mode", cfg["glb"]["texture_mode"],
            "--world-frame", str(world_frame),
        ]
        if download_manifest is not None:
            glb_command.extend(["--download-manifest", str(download_manifest)])
        if not offline and cfg["glb"]["texture_mode"] != "flat":
            glb_command.append("--fetch-textures")
        _run(glb_command)

        terrain_glb = terrain_dir / "terrain.glb"
        roads_glb = roads_dir / "roads.glb"
        _run([
            sys.executable, str(PIPELINE / "road_terrain_probe.py"),
            "--roads", str(source_root), "--terrain-receipt", str(terrain_receipt),
            "--terrain-out", str(terrain_glb), "--roads-out", str(roads_glb),
        ])
        markings_glb = markings_dir / "road-markings.glb"
        _run([
            sys.executable, str(PIPELINE / "city_furniture2glb.py"),
            "--source", str(source_root), "--world-frame", str(world_frame),
            "--terrain-receipt", str(terrain_receipt), "--out", str(markings_glb),
            "--marking-vertical-offset", str(city_world["marking_vertical_offset_m"]),
            "--allow-empty",
        ])
        compose_command = [
            sys.executable, str(PIPELINE / "city_world_composer.py"),
            "--world-frame", str(world_frame),
            "--terrain-xml", str(terrain_xml), "--buildings-xml", str(buildings_xml),
            "--terrain-glb", str(terrain_glb), "--roads-glb", str(roads_glb),
            "--buildings-glb", str(buildings_glb),
            "--out-dir", str(world_dir),
        ]
        if markings_glb.is_file():
            compose_command.extend(["--extra-glb", str(markings_glb)])
        _run(compose_command)
        dataset_validation = world_dir / "dataset-validation.json"
        _run([
            sys.executable, str(PIPELINE / "city_dataset_validator.py"),
            "--terrain-receipt", str(terrain_receipt),
            "--buildings-receipt", str(buildings_glb_receipt),
            "--roads-receipt", str(roads_dir / "roads-glb-receipt.json"),
            "--markings-receipt", str(markings_dir / "road-markings-glb-receipt.json"),
            "--out", str(dataset_validation),
        ])
        return {
            "mjcf": world_dir / "city-world.xml",
            "glb": world_dir / "city-world.glb",
            "world_receipt": world_dir / "city-world-receipt.json",
            "world_frame": world_frame,
            "terrain_receipt": terrain_receipt,
            "buildings_glb_receipt": buildings_glb_receipt,
            "road_markings_receipt": markings_dir / "road-markings-glb-receipt.json",
            "dataset_validation": dataset_validation,
        }

    mjcf = build_dir / f"{output_name}.xml"
    command = [
        sys.executable, str(PIPELINE / "obb2mjcf.py"),
        "--inp", str(walls), "--out", str(mjcf),
        "--model-name", cfg["mjcf"]["model_name"],
        "--collide", cfg["mjcf"]["collision"],
    ]
    if cfg["mjcf"]["floor"]:
        command.append("--floor")
    _run(command)
    outputs = {"mjcf": mjcf}
    if cfg["glb"]["enabled"]:
        glb = build_dir / f"{output_name}.glb"
        glb_receipt = build_dir / f"{output_name}-glb-receipt.json"
        glb_command = [
            sys.executable, str(PIPELINE / "citygml2glb.py"),
            "--selection", str(lod1), "--out", str(glb),
            "--receipt", str(glb_receipt),
            "--texture-mode", cfg["glb"]["texture_mode"],
        ]
        if download_manifest is not None:
            glb_command.extend(["--download-manifest", str(download_manifest)])
        if not offline and cfg["glb"]["texture_mode"] != "flat":
            glb_command.append("--fetch-textures")
        _run(glb_command)
        outputs.update({"glb": glb, "glb_receipt": glb_receipt})
    return outputs


def build(manifest: Path, offline: bool = False) -> int:
    cfg = load_config(manifest)
    if doctor(manifest) != 0:
        return 1
    configure(manifest)
    build_dir = _path(cfg["output"]["build_dir"])
    source_root = build_dir / "source"
    source_root.mkdir(parents=True, exist_ok=True)
    meta = _query_meta(cfg)
    (source_root / "query_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    downloaded = []
    catalog_status: dict[str, dict[str, Any]] = {}
    enabled_features = [
        name for name, enabled in cfg["source"]["feature_types"].items() if enabled
    ]
    bbox = tuple(meta["bbox"][key] for key in ("west", "south", "east", "north"))
    for feature_type in enabled_features:
        response_path = build_dir / f"plateau-catalog-response-{feature_type}.json"
        query_path = build_dir / f"plateau-catalog-query-{feature_type}.json"
        if offline:
            if not response_path.is_file():
                raise ConfigError(f"offline build requires cached catalog response: {response_path}")
            if not query_path.is_file():
                raise ConfigError(f"offline build requires cached catalog query contract: {query_path}")
            cached_query = json.loads(query_path.read_text(encoding="utf-8"))
            if cached_query.get("third_mesh_codes") != meta["third_mesh_codes"]:
                raise ConfigError(
                    f"cached {feature_type} catalog query does not cover the current third-level meshes"
                )
            payload = json.loads(response_path.read_text(encoding="utf-8"))
        else:
            url = search_url(cfg["source"]["api_base_url"], feature_type, bbox)
            print(f"INFO: querying PLATEAU {feature_type} catalog: {url}")
            payload = request_catalog(
                url,
                allow_not_found=cfg["city_world"]["enabled"] and feature_type == "frn",
            )
            response_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            query_path.write_text(json.dumps({
                "schema_version": 1,
                "feature_type": feature_type,
                "url": url,
                "third_mesh_codes": meta["third_mesh_codes"],
            }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        selected = select_files(
            payload, feature_type, cfg["source"]["year"],
            allow_empty=cfg["city_world"]["enabled"] and feature_type == "frn",
        )
        if feature_type == "frn" and not selected:
            print(
                "INFO: no CityFurniture dataset is available; "
                "road markings will be omitted and reported by Dataset Validator"
            )
        catalog_status[feature_type] = payload.get(
            "_catalog_status",
            {"status": "available" if selected else "not_available"},
        )
        for index, item in enumerate(selected, 1):
            item = {**item, "feature_type": feature_type}
            print(
                f"INFO: {feature_type} CityGML {index}/{len(selected)} "
                f"city={item['city_code']} year={item['year']} "
                f"mesh={item['code']} size={item['file_size']}"
            )
            if offline:
                expected = source_root / f"{item['city_code']}-{item['year']}" / Path(
                    urllib.parse.urlparse(item["url"]).path
                ).name
                if not expected.is_file():
                    raise ConfigError(f"offline build requires downloaded CityGML: {expected}")
            downloaded.append(download_file(item, source_root) if not offline else {
                **item, "path": str(expected), "bytes": expected.stat().st_size,
                "sha256": sha256_file(expected), "mode": "offline-reused"
            })
    download_manifest = {
        "schema_version": 1,
        "query": meta,
        "catalog_status": catalog_status,
        "files": downloaded,
    }
    download_manifest_path = build_dir / "download-manifest.json"
    download_manifest_path.write_text(json.dumps(download_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    outputs = _convert(
        cfg, source_root, build_dir,
        download_manifest=download_manifest_path,
        offline=offline,
    )
    mjcf = outputs["mjcf"]
    root = ET.parse(mjcf).getroot()
    geom_count = len(root.findall(".//geom"))
    if geom_count == 0:
        raise ConfigError("conversion produced no MuJoCo geoms")
    receipt = {
        "schema_version": 1, "component": "hakoniwa-envsim",
        "pipeline": cfg["pipeline"]["type"], "manifest": str(manifest.resolve()),
        "outputs": {kind: str(path) for kind, path in outputs.items()},
        "geom_count": geom_count,
        "source_files": len(downloaded), "feature_types": enabled_features,
        "year_policy": cfg["source"]["year"],
    }
    (build_dir / "build-receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    label = "MuJoCo City World" if cfg["city_world"]["enabled"] else "MuJoCo wall model"
    print(f"OK: {label}: {mjcf} ({geom_count} geoms)")
    return 0


def install(manifest: Path) -> int:
    cfg = load_config(manifest)
    build_dir = _path(cfg["output"]["build_dir"])
    receipt = build_dir / "build-receipt.json"
    if cfg["city_world"]["enabled"]:
        world = build_dir / "world"
        components = build_dir / "components"
        if not receipt.is_file() or not (world / "city-world.xml").is_file() or not (
            world / "city-world.glb"
        ).is_file():
            print("ERROR: City World build output is missing; run build first", file=sys.stderr)
            return 1
        destination = (
            _path(cfg["output"]["install_dir"]) / "share" / "hakoniwa-envsim"
            / "city" / cfg["output"]["name"]
        )
        if destination.exists():
            shutil.rmtree(destination)
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copytree(world, destination / "world")
        shutil.copytree(components, destination / "components")
        for name in ("build-receipt.json", "download-manifest.json"):
            shutil.copy2(build_dir / name, destination / name)
        print(f"OK: installed PLATEAU City World: {destination}")
        return 0
    source = build_dir / f"{cfg['output']['name']}.xml"
    glb = build_dir / f"{cfg['output']['name']}.glb"
    glb_receipt = build_dir / f"{cfg['output']['name']}-glb-receipt.json"
    if not receipt.is_file() or not source.is_file() or (cfg["glb"]["enabled"] and not glb.is_file()):
        print("ERROR: build output is missing; run build first", file=sys.stderr)
        return 1
    destination = _path(cfg["output"]["install_dir"]) / "share" / "hakoniwa-envsim" / "city" / cfg["output"]["name"]
    destination.mkdir(parents=True, exist_ok=True)
    names = [source.name, "build-receipt.json", "download-manifest.json"]
    if cfg["glb"]["enabled"]:
        names.extend([glb.name, glb_receipt.name])
    for name in names:
        shutil.copy2(build_dir / name, destination / name)
    print(f"OK: installed PLATEAU assets: {destination}")
    return 0


def smoke() -> int:
    if doctor(DEFAULT_MANIFEST) != 0:
        return 1
    fixture = ROOT / "tests" / "fixtures" / "tiny_bldg_6697_op.gml"
    with tempfile.TemporaryDirectory(prefix="hakoniwa-envsim-smoke-") as temporary:
        temp = Path(temporary)
        source = temp / "source"
        source.mkdir()
        shutil.copy2(fixture, source / fixture.name)
        source.joinpath("query_meta.json").write_text(json.dumps({
            "center_lat": 35.681236, "center_lon": 139.706763,
            "ns_m": 100, "ew_m": 100,
        }), encoding="utf-8")
        cfg = load_config(DEFAULT_MANIFEST)
        outputs = _convert(cfg, source, temp)
        mjcf = outputs["mjcf"]
        geoms = ET.parse(mjcf).getroot().findall(".//geom")
        if not geoms:
            print("ERROR: smoke conversion produced no geoms", file=sys.stderr)
            return 1
        if cfg["glb"]["enabled"]:
            scene = __import__("trimesh").load(outputs["glb"], force="scene")
            if not scene.geometry:
                print("ERROR: smoke conversion produced an empty GLB", file=sys.stderr)
                return 1
        print(f"OK: offline PLATEAU asset smoke produced {len(geoms)} MuJoCo geom(s) and GLB")
    return 0


def test() -> int:
    completed = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py", "-v"],
        cwd=ROOT, check=False,
    )
    return completed.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="Operate hakoniwa-envsim PLATEAU conversion")
    parser.add_argument("command", choices=("doctor", "configure", "build", "install", "test", "smoke"))
    parser.add_argument("--config", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--offline", action="store_true", help="build from cached catalog response and CityGML files")
    args = parser.parse_args()
    try:
        if args.command == "doctor": return doctor(args.config)
        if args.command == "configure": return configure(args.config)
        if args.command == "build": return build(args.config, offline=args.offline)
        if args.command == "install": return install(args.config)
        if args.command == "test": return test()
        return smoke()
    except (ConfigError, PlateauError, OSError, subprocess.CalledProcessError, ET.ParseError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
