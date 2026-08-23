"""Shared helpers for thin triangular-prism MuJoCo collision meshes."""

from __future__ import annotations

import numpy as np


PRISM_FACES = np.asarray([
    [0, 1, 2], [5, 4, 3],
    [0, 3, 4], [0, 4, 1],
    [1, 4, 5], [1, 5, 2],
    [2, 5, 3], [2, 3, 0],
], dtype=int)


def triangular_prism(vertices: np.ndarray, thickness_m: float):
    """Return a watertight prism with an exact top triangle and downward thickness."""
    top = np.asarray(vertices, dtype=float)
    if top.shape != (3, 3):
        raise ValueError("triangular prism requires exactly three XYZ vertices")
    if thickness_m <= 0:
        raise ValueError("triangular prism thickness must be positive")
    bottom = top.copy()
    bottom[:, 2] -= thickness_m
    return np.vstack((top, bottom)), PRISM_FACES.copy()


def triangular_prism_along_normal(vertices: np.ndarray, thickness_m: float):
    """Return a watertight prism extruded perpendicular to an arbitrary triangle."""
    surface = np.asarray(vertices, dtype=float)
    if surface.shape != (3, 3):
        raise ValueError("triangular prism requires exactly three XYZ vertices")
    if thickness_m <= 0:
        raise ValueError("triangular prism thickness must be positive")
    normal = np.cross(surface[1] - surface[0], surface[2] - surface[0])
    length = float(np.linalg.norm(normal))
    if length <= 1e-12:
        raise ValueError("triangular prism source triangle is degenerate")
    offset = normal / length * thickness_m
    return np.vstack((surface, surface - offset)), PRISM_FACES.copy()


def format_numbers(values) -> str:
    return " ".join(f"{float(value):.9g}" for value in values)
