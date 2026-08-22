#!/usr/bin/env python3
"""Shared coordinate-frame contract for independently generated city assets."""

from __future__ import annotations

import json
from pathlib import Path


class WorldFrameError(ValueError):
    pass


def create_world_frame(terrain_receipt: dict) -> dict:
    center = terrain_receipt["center"]
    extent = terrain_receipt["half_extent_m"]
    return {
        "schema_version": 1,
        "origin": {
            "latitude": float(center["latitude"]),
            "longitude": float(center["longitude"]),
            "altitude_offset_m": float(terrain_receipt["altitude_offset_m"]),
        },
        "half_extent_m": {
            "north_south": float(extent["north_south"]),
            "east_west": float(extent["east_west"]),
        },
        "coordinate_systems": {
            "mjcf": "X=North,Y=-East,Z=Up",
            "glb": "X=East,Y=Up,Z=-North",
        },
        "altitude_reference": "PLATEAU EPSG:6697 height minus origin.altitude_offset_m",
    }


def validate_world_frame(data: dict) -> dict:
    if data.get("schema_version") != 1:
        raise WorldFrameError("world-frame schema_version must be 1")
    origin = data.get("origin") or {}
    extent = data.get("half_extent_m") or {}
    for key in ("latitude", "longitude", "altitude_offset_m"):
        if not isinstance(origin.get(key), (int, float)):
            raise WorldFrameError(f"world-frame origin.{key} must be numeric")
    for key in ("north_south", "east_west"):
        if not isinstance(extent.get(key), (int, float)) or extent[key] <= 0:
            raise WorldFrameError(f"world-frame half_extent_m.{key} must be positive")
    expected = {
        "mjcf": "X=North,Y=-East,Z=Up",
        "glb": "X=East,Y=Up,Z=-North",
    }
    if data.get("coordinate_systems") != expected:
        raise WorldFrameError("world-frame coordinate_systems contract mismatch")
    return data


def load_world_frame(path: Path) -> dict:
    return validate_world_frame(json.loads(path.read_text(encoding="utf-8")))


def write_world_frame(path: Path, data: dict) -> None:
    validate_world_frame(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
