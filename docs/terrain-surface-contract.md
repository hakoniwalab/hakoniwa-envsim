# City World terrain surface contract

## Authority

The MuJoCo hfield is the physical ground authority for a City World. Terrain
visualization, road visualization, and road-marking visualization use the same
piecewise-planar surface as that hfield.

All components consume the same row-major DEM samples and world frame:

```text
sampled DEM hfield
  -> MuJoCo hfield collision
  -> terrain.glb
  -> roads.glb + road_vertical_offset_m
  -> road-markings.glb + marking_vertical_offset_m
```

The generated receipts identify this policy as
`mujoco-hfield-triangles-v1`.

## Cell topology

For one row-major cell, use these names:

```text
TL (r+1,c) ---- TR (r+1,c+1)
    |          / |
    |        /   |
    |      /     |
BL (r,c)   ---- BR (r,c+1)
```

MuJoCo feeds `TL, BL, TR, BR` into its hfield triangle strip. The shared
diagonal is therefore `BL--TR`, and the two surface triangles are:

```text
(TL, BL, TR)
(BL, BR, TR)
```

`src/city_pipeline/terrain_surface.py` is the shared implementation of this
topology. Do not independently choose a terrain diagonal in a component
converter.

## Road and road-marking draping

Source polygons retain their horizontal outline, including holes. A polygon is
first clipped to the configured terrain bounds. Its bounding box determines the
only hfield cells that can intersect it. The converter then intersects the
polygon with each of the two MuJoCo triangles in those cells, triangulates the
clipped patches, and evaluates every output vertex on that triangle's plane.

The recorded visual offsets are applied vertically after surface evaluation:

- roads: `+0.030 m`;
- road markings: configured `marking_vertical_offset_m`, default `+0.055 m`.

Roads remain visual geometry. The conversion does not add duplicate road
collision on top of the hfield.

## Bilinear height queries

The existing `road_terrain_probe.terrain_height()` function remains a bilinear
query for compatibility with bridge endpoint diagnostics. A bilinear patch is
not identical to MuJoCo's two planar triangles when a cell's four samples are
non-coplanar. Terrain, road, and road-marking mesh generation must use
`TerrainSurface` instead.

Changing bridge diagnostics to the piecewise-planar contract requires a
separate impact assessment.

## Performance and determinism

The draper never evaluates every polygon against the complete terrain grid. It
derives candidate row and column ranges from each polygon bounding box.

Road and road-marking conversion accept `--workers`. `tools/hako.py` derives
this from `city_world.parallel_workers` and caps the internal process count at
four. Work is returned to source order before meshes are assembled, so one and
multiple workers generate byte-identical GLBs for the same inputs.

Receipts record the requested and effective worker counts, candidate cell
count, terrain triangle test count, clipped patch count, and final surface
triangle counts.

Increasing workers reduces wall-clock time only when the draping workload is large
enough to exceed process startup and transfer costs. `terrain_spacing_m`
remains the control for grid density, output triangle count, memory, and GLB
size.

## Scope boundary

This contract represents a single-valued height field. Bridges, tunnels,
overpasses, and other stacked surfaces cannot be represented by the DEM hfield
alone and retain their component-specific source geometry and validation.
