"""MuJoCo-compatible piecewise-planar surface for sampled DEM height fields."""

from __future__ import annotations

import concurrent.futures
import math
from dataclasses import dataclass, field
from typing import Iterable, Sequence

import mapbox_earcut
import numpy as np
from shapely import wkb
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon, box


SURFACE_POLICY = "mujoco-hfield-triangles-v1"
_MIN_PATCH_AREA_M2 = 1e-10


@dataclass
class DrapedMesh:
    vertices: list[tuple[float, float, float]] = field(default_factory=list)
    faces: list[tuple[int, int, int]] = field(default_factory=list)
    candidate_cell_count: int = 0
    terrain_triangle_test_count: int = 0
    clipped_patch_count: int = 0


@dataclass(frozen=True)
class TerrainSurface:
    """One hfield surface using the same cell diagonal as MuJoCo.

    Coordinates use the Hakoniwa/MuJoCo City World contract:
    X=North, Y=-East, Z=absolute elevation.
    """

    samples: tuple[float, ...]
    nrow: int
    ncol: int
    north_south_m: float
    east_west_m: float

    def __post_init__(self) -> None:
        if self.nrow < 2 or self.ncol < 2:
            raise ValueError("terrain surface requires at least a 2x2 grid")
        if len(self.samples) != self.nrow * self.ncol:
            raise ValueError("terrain sample count does not match nrow*ncol")
        if self.north_south_m <= 0 or self.east_west_m <= 0:
            raise ValueError("terrain half extents must be positive")
        if not all(math.isfinite(value) for value in self.samples):
            raise ValueError("terrain samples must be finite")

    @classmethod
    def from_samples(
        cls, samples: Sequence[float], nrow: int, ncol: int,
        north_south_m: float, east_west_m: float,
    ) -> "TerrainSurface":
        return cls(
            tuple(float(value) for value in samples), nrow, ncol,
            float(north_south_m), float(east_west_m),
        )

    @property
    def column_spacing_m(self) -> float:
        return 2.0 * self.north_south_m / (self.ncol - 1)

    @property
    def row_spacing_m(self) -> float:
        return 2.0 * self.east_west_m / (self.nrow - 1)

    @property
    def bounds(self) -> Polygon:
        return box(
            -self.north_south_m, -self.east_west_m,
            self.north_south_m, self.east_west_m,
        )

    def _sample(self, row: int, col: int) -> float:
        return self.samples[row * self.ncol + col]

    def _cell_coordinates(self, row: int, col: int) -> tuple[float, float, float, float]:
        x0 = -self.north_south_m + col * self.column_spacing_m
        y0 = -self.east_west_m + row * self.row_spacing_m
        return x0, y0, x0 + self.column_spacing_m, y0 + self.row_spacing_m

    def grid_mesh(self) -> tuple[np.ndarray, np.ndarray]:
        """Return hfield vertices/faces with MuJoCo's triangle-strip diagonal."""
        vertices = [
            (
                -self.north_south_m + col * self.column_spacing_m,
                -self.east_west_m + row * self.row_spacing_m,
                self._sample(row, col),
            )
            for row in range(self.nrow)
            for col in range(self.ncol)
        ]
        faces = []
        for row in range(self.nrow - 1):
            for col in range(self.ncol - 1):
                lower_left = row * self.ncol + col
                upper_left = lower_left + self.ncol
                lower_right = lower_left + 1
                upper_right = upper_left + 1
                # MuJoCo feeds TL, BL, TR, BR into a triangle strip.  Both
                # rendering and hfield collision therefore share BL--TR.
                faces.extend((
                    (upper_left, lower_left, upper_right),
                    (lower_left, lower_right, upper_right),
                ))
        return np.asarray(vertices, dtype=float), np.asarray(faces, dtype=np.int64)

    def _cell_for_point(self, x: float, y: float) -> tuple[int, int, float, float]:
        column = (x + self.north_south_m) / self.column_spacing_m
        row = (y + self.east_west_m) / self.row_spacing_m
        column = min(max(column, 0.0), self.ncol - 1.0)
        row = min(max(row, 0.0), self.nrow - 1.0)
        col_index = min(int(column), self.ncol - 2)
        row_index = min(int(row), self.nrow - 2)
        return row_index, col_index, column - col_index, row - row_index

    def height_at(self, x: float, y: float) -> float:
        """Evaluate the exact MuJoCo hfield triangle plane at one XY point."""
        row, col, dc, dr = self._cell_for_point(x, y)
        if dr >= dc:
            return self._triangle_height(row, col, True, x, y)
        return self._triangle_height(row, col, False, x, y)

    def _triangle_height(
        self, row: int, col: int, upper: bool, x: float, y: float,
    ) -> float:
        x0, y0, _x1, _y1 = self._cell_coordinates(row, col)
        dc = (x - x0) / self.column_spacing_m
        dr = (y - y0) / self.row_spacing_m
        bottom_left = self._sample(row, col)
        top_right = self._sample(row + 1, col + 1)
        if upper:
            top_left = self._sample(row + 1, col)
            return (
                bottom_left
                + dr * (top_left - bottom_left)
                + dc * (top_right - top_left)
            )
        bottom_right = self._sample(row, col + 1)
        return (
            bottom_left
            + dc * (bottom_right - bottom_left)
            + dr * (top_right - bottom_right)
        )

    def _candidate_cells(self, geometry) -> Iterable[tuple[int, int]]:
        minimum_x, minimum_y, maximum_x, maximum_y = geometry.bounds
        col_first = max(
            0, int(math.floor((minimum_x + self.north_south_m) / self.column_spacing_m)),
        )
        col_last = min(
            self.ncol - 2,
            int(math.ceil((maximum_x + self.north_south_m) / self.column_spacing_m)) - 1,
        )
        row_first = max(
            0, int(math.floor((minimum_y + self.east_west_m) / self.row_spacing_m)),
        )
        row_last = min(
            self.nrow - 2,
            int(math.ceil((maximum_y + self.east_west_m) / self.row_spacing_m)) - 1,
        )
        if col_last < col_first or row_last < row_first:
            return
        for row in range(row_first, row_last + 1):
            for col in range(col_first, col_last + 1):
                yield row, col

    def _cell_triangles(self, row: int, col: int):
        x0, y0, x1, y1 = self._cell_coordinates(row, col)
        return (
            (True, Polygon(((x0, y1), (x0, y0), (x1, y1)))),
            (False, Polygon(((x0, y0), (x1, y0), (x1, y1)))),
        )

    def drape_polygon(self, polygon, vertical_offset_m: float = 0.0) -> DrapedMesh:
        if not math.isfinite(vertical_offset_m):
            raise ValueError("vertical offset must be finite")
        clipped_source = polygon.intersection(self.bounds)
        result = DrapedMesh()
        if clipped_source.is_empty:
            return result
        for row, col in self._candidate_cells(clipped_source):
            result.candidate_cell_count += 1
            for upper, terrain_triangle in self._cell_triangles(row, col):
                result.terrain_triangle_test_count += 1
                clipped = clipped_source.intersection(terrain_triangle)
                for patch in _polygon_parts(clipped):
                    if patch.area <= _MIN_PATCH_AREA_M2:
                        continue
                    coordinates, faces = _triangulate_polygon(patch)
                    if not len(faces):
                        continue
                    base = len(result.vertices)
                    result.vertices.extend((
                        float(x), float(y),
                        self._triangle_height(row, col, upper, float(x), float(y))
                        + vertical_offset_m,
                    ) for x, y in coordinates)
                    result.faces.extend(
                        (base + int(a), base + int(b), base + int(c))
                        for a, b, c in faces
                    )
                    result.clipped_patch_count += 1
        return result


def _polygon_parts(geometry) -> Iterable[Polygon]:
    if isinstance(geometry, Polygon):
        yield geometry
    elif isinstance(geometry, (MultiPolygon, GeometryCollection)):
        for part in geometry.geoms:
            yield from _polygon_parts(part)


def _triangulate_polygon(polygon: Polygon) -> tuple[np.ndarray, np.ndarray]:
    rings = [polygon.exterior, *polygon.interiors]
    points: list[tuple[float, float]] = []
    ends = []
    for ring in rings:
        coordinates = list(ring.coords)[:-1]
        if len(coordinates) < 3:
            continue
        points.extend((float(x), float(y)) for x, y in coordinates)
        ends.append(len(points))
    if not points or not ends:
        return np.empty((0, 2), dtype=float), np.empty((0, 3), dtype=np.int64)
    coordinates = np.asarray(points, dtype=np.float64)
    indices = mapbox_earcut.triangulate_float64(
        coordinates, np.asarray(ends, dtype=np.uint32),
    )
    faces = indices.reshape((-1, 3)).astype(np.int64, copy=False)
    # Keep upward winding in the MuJoCo X/Y plane.  The GLB axis transform
    # used by City World preserves orientation.
    for face in faces:
        first, second, third = coordinates[face]
        signed_area = (
            (second[0] - first[0]) * (third[1] - first[1])
            - (second[1] - first[1]) * (third[0] - first[0])
        )
        if signed_area < 0:
            face[1], face[2] = face[2], face[1]
    return coordinates, faces


_PROCESS_SURFACE: TerrainSurface | None = None


def _initialize_process(surface: TerrainSurface) -> None:
    global _PROCESS_SURFACE
    _PROCESS_SURFACE = surface


def _drape_batch(batch):
    if _PROCESS_SURFACE is None:
        raise RuntimeError("terrain surface worker is not initialized")
    return [
        (index, _PROCESS_SURFACE.drape_polygon(wkb.loads(payload), vertical_offset_m))
        for index, payload, vertical_offset_m in batch
    ]


def drape_polygons(
    surface: TerrainSurface,
    polygons: Sequence,
    vertical_offset_m: float,
    workers: int = 1,
) -> tuple[list[DrapedMesh], dict]:
    """Drape polygons deterministically, optionally using multiple processes."""
    if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
        raise ValueError("workers must be a positive integer")
    effective_workers = min(workers, len(polygons)) if polygons else 0
    if effective_workers <= 1:
        results = [surface.drape_polygon(polygon, vertical_offset_m) for polygon in polygons]
    else:
        records = [
            (index, polygon.wkb, vertical_offset_m)
            for index, polygon in enumerate(polygons)
        ]
        batch_size = max(1, math.ceil(len(records) / (effective_workers * 4)))
        batches = [
            records[index:index + batch_size]
            for index in range(0, len(records), batch_size)
        ]
        indexed: list[tuple[int, DrapedMesh]] = []
        with concurrent.futures.ProcessPoolExecutor(
            max_workers=effective_workers,
            initializer=_initialize_process,
            initargs=(surface,),
        ) as executor:
            for completed in executor.map(_drape_batch, batches):
                indexed.extend(completed)
        indexed.sort(key=lambda item: item[0])
        results = [mesh for _, mesh in indexed]
    return results, {
        "surface_policy": SURFACE_POLICY,
        "requested_workers": workers,
        "effective_workers": effective_workers,
        "candidate_cell_count": sum(mesh.candidate_cell_count for mesh in results),
        "terrain_triangle_test_count": sum(
            mesh.terrain_triangle_test_count for mesh in results
        ),
        "clipped_patch_count": sum(mesh.clipped_patch_count for mesh in results),
    }
