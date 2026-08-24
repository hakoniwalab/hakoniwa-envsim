"""Shared helpers for thin triangular-prism MuJoCo collision meshes."""

from __future__ import annotations

import numpy as np


# A -Z extrusion has only ``thickness * abs(normal.z)`` effective thickness.
# Below 5% it is a numerically narrow 3-D hull despite having non-zero rank.
MIN_WORLD_Z_EXTRUSION_NORMAL_COMPONENT = 0.05


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


def polygon_prism_for_surface(
    vertices: np.ndarray,
    thickness_m: float,
    *,
    prefer_world_z: bool,
):
    """Build a non-degenerate prism and report the extrusion direction used.

    Roof surfaces normally use world -Z so their collision top stays exactly at
    the source height.  Some real PLATEAU datasets nevertheless label nearly
    vertical polygons as RoofSurface.  Moving such a polygon along -Z leaves
    all vertices effectively coplanar and MuJoCo/QHull cannot compile it.  In
    that case we use the source normal, which guarantees the requested physical
    thickness without changing the source face.
    """
    surface = np.asarray(vertices, dtype=float)
    if not prefer_world_z:
        prism, faces = polygon_prism_along_normal(surface, thickness_m)
        return prism, faces, "surface-normal"

    normal = np.zeros(3, dtype=float)
    for index, current in enumerate(surface):
        normal += np.cross(current, surface[(index + 1) % len(surface)])
    length = float(np.linalg.norm(normal))
    if length <= 1e-12:
        raise ValueError("polygon prism source polygon is degenerate")
    world_z_normal_component = abs(float(normal[2] / length))
    if world_z_normal_component >= MIN_WORLD_Z_EXTRUSION_NORMAL_COMPONENT:
        prism, faces = polygon_prism(surface, thickness_m)
        return prism, faces, "world-z"
    prism, faces = polygon_prism_along_normal(surface, thickness_m)
    return prism, faces, "surface-normal-fallback"


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


def prism_as_box(surface_vertices, prism_vertices):
    """Return an exact oriented-box description for a rectangular prism.

    The test is intentionally strict: the source face must be a rectangle and
    every extrusion vector must be equal and perpendicular to both face axes.
    A skewed roof prism therefore remains a mesh.
    """
    surface = np.asarray(surface_vertices, dtype=float)
    prism = np.asarray(prism_vertices, dtype=float)
    if surface.ndim != 2 or surface.shape[1:] != (3,) or len(surface) < 4:
        return None
    if prism.shape != (2 * len(surface), 3):
        return None
    scale = max(float(np.ptp(prism, axis=0).max()), 1.0)
    tolerance = max(1e-6, scale * 1e-8)
    bottom = prism[len(surface):]
    if not np.allclose(prism[:len(surface)], surface, atol=tolerance, rtol=0):
        return None
    extrusion = bottom - surface
    if not np.allclose(extrusion, extrusion[0], atol=tolerance, rtol=0):
        return None
    corners = []
    for index, point in enumerate(surface):
        previous = surface[index - 1]
        following = surface[(index + 1) % len(surface)]
        incoming = point - previous
        outgoing = following - point
        denominator = float(np.linalg.norm(incoming) * np.linalg.norm(outgoing))
        if denominator <= tolerance * tolerance:
            return None
        if float(np.linalg.norm(np.cross(incoming, outgoing))) <= 1e-8 * denominator:
            continue
        corners.append(point)
    if len(corners) != 4:
        return None
    corners = np.asarray(corners, dtype=float)
    edge_x = corners[1] - corners[0]
    edge_y = corners[2] - corners[1]
    opposite_x = corners[3] - corners[2]
    opposite_y = corners[0] - corners[3]
    length_x = float(np.linalg.norm(edge_x))
    length_y = float(np.linalg.norm(edge_y))
    thickness = float(np.linalg.norm(extrusion[0]))
    if min(length_x, length_y, thickness) <= tolerance:
        return None
    if not np.allclose(edge_x, -opposite_x, atol=tolerance, rtol=0):
        return None
    if not np.allclose(edge_y, -opposite_y, atol=tolerance, rtol=0):
        return None
    orthogonal_tolerance = 1e-7
    if abs(float(edge_x @ edge_y)) > orthogonal_tolerance * length_x * length_y:
        return None
    if abs(float(edge_x @ extrusion[0])) > orthogonal_tolerance * length_x * thickness:
        return None
    if abs(float(edge_y @ extrusion[0])) > orthogonal_tolerance * length_y * thickness:
        return None
    return {
        "pos": corners.mean(axis=0) + extrusion[0] / 2,
        "size": np.asarray((length_x / 2, length_y / 2, thickness / 2)),
        "xyaxes": np.concatenate((edge_x / length_x, edge_y / length_y)),
    }


def format_numbers(values) -> str:
    return " ".join(f"{float(value):.9g}" for value in values)
