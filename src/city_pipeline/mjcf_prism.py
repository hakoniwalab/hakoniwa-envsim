"""Shared helpers for thin triangular-prism MuJoCo collision meshes."""

from __future__ import annotations

import numpy as np


PRISM_FACES = np.asarray([
    [0, 1, 2], [5, 4, 3],
    [0, 3, 4], [0, 4, 1],
    [1, 4, 5], [1, 5, 2],
    [2, 5, 3], [2, 3, 0],
], dtype=int)


def _polygon_prism_faces(vertex_count: int) -> np.ndarray:
    if vertex_count < 3:
        raise ValueError("polygon prism requires at least three XYZ vertices")
    faces = []
    # The caller guarantees a convex source polygon, so a fan preserves its
    # exact top and bottom surfaces without introducing phantom volume.
    for index in range(1, vertex_count - 1):
        faces.append([0, index, index + 1])
        faces.append([
            vertex_count,
            vertex_count + index + 1,
            vertex_count + index,
        ])
    for index in range(vertex_count):
        following = (index + 1) % vertex_count
        faces.append([index, vertex_count + index, vertex_count + following])
        faces.append([index, vertex_count + following, following])
    return np.asarray(faces, dtype=int)


def polygon_prism(vertices: np.ndarray, thickness_m: float):
    """Extrude one planar convex XYZ polygon downward along world Z."""
    top = np.asarray(vertices, dtype=float)
    if top.ndim != 2 or top.shape[1] != 3 or len(top) < 3:
        raise ValueError("polygon prism requires at least three XYZ vertices")
    if thickness_m <= 0:
        raise ValueError("polygon prism thickness must be positive")
    bottom = top.copy()
    bottom[:, 2] -= thickness_m
    return np.vstack((top, bottom)), _polygon_prism_faces(len(top))


def polygon_prism_along_normal(vertices: np.ndarray, thickness_m: float):
    """Extrude one planar convex XYZ polygon along its source normal."""
    surface = np.asarray(vertices, dtype=float)
    if surface.ndim != 2 or surface.shape[1] != 3 or len(surface) < 3:
        raise ValueError("polygon prism requires at least three XYZ vertices")
    if thickness_m <= 0:
        raise ValueError("polygon prism thickness must be positive")
    normal = np.zeros(3, dtype=float)
    for index, current in enumerate(surface):
        normal += np.cross(current, surface[(index + 1) % len(surface)])
    length = float(np.linalg.norm(normal))
    if length <= 1e-12:
        raise ValueError("polygon prism source polygon is degenerate")
    offset = normal / length * thickness_m
    return np.vstack((surface, surface - offset)), _polygon_prism_faces(len(surface))


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
