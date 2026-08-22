# PLATEAU road and terrain hypothesis validation

## Goal

Validate, on a 200 m by 200 m area centered at the Hakoniwa Map Viewer
origin, that PLATEAU transportation and terrain models can produce one
aligned MuJoCo scene:

- `tran` LOD1 polygons define the horizontal road footprint.
- `dem` LOD1 TIN triangles define ground elevation.
- the DEM is sampled into a MuJoCo `hfield`.
- road polygons are draped onto the sampled terrain.
- terrain, roads, and buildings share one horizontal origin and one altitude
  offset.

This is a hypothesis-validation task. It does not yet extend the production
one-kilometer Recipe.

## Validation conditions

The first Shibuya probe validated terrain/road/building alignment:

- center latitude: `35.6625`
- center longitude: `139.70625`
- north/south half extent: `100 m`
- east/west half extent: `100 m`
- terrain grid spacing: `2 m`
- expected height-field samples: `101 x 101`
- source CRS: `EPSG:6697`, three-dimensional `latitude longitude altitude`
- local source coordinates: `East, North, Up`
- MuJoCo coordinates: `X=North, Y=-East, Z=Up`

The second probe deliberately selects an area containing actual PLATEAU lane
and road-marking data:

- area: south of Numazu Station;
- center latitude: `35.0988`;
- center longitude: `138.8587`;
- north/south and east/west half extent: `100 m`;
- third mesh: `52385618`;
- road markings are exported only from source `frn:CityFurniture` LOD3
  polygons. No lane or marking geometry is inferred.

## Evidence already confirmed

- The 2025 Shibuya `tran` file contains LOD1 road polygons whose altitude is
  zero, so LOD1 does not provide usable terrain height.
- The corresponding `dem` file contains `dem:TINRelief` triangles with real
  altitude values.
- Some roads have higher LOD geometry with altitude, but higher LOD is not
  available for every road. LOD1 draped onto DEM therefore provides the
  uniform baseline contract.

## Prototype outputs

- a compact terrain-selection receipt recording source, geographic bounds,
  grid size, spacing, missing-sample count, minimum altitude, and maximum
  altitude;
- a MuJoCo XML containing an `asset/hfield` and a collision-enabled hfield
  geom;
- a road-selection file containing only polygons intersecting the 200 m by
  200 m validation area;
- a display artifact showing the draped roads and terrain together;
- a validation report for axis orientation, center coverage, elevation range,
  and road-to-terrain residual.

## Coordinate and altitude contract

The DEM minimum altitude inside the selected area becomes the prototype scene
altitude offset. The same offset must be applied to terrain, roads, buildings,
and later GLB output. Building-specific minimum-Z normalization must not be
used once terrain is composed into the scene.

## Component and composition contract

Terrain, roads, and buildings are generated as independent artifacts. They
must all consume the same `world-frame.json`; no component may choose its own
vertical zero after that frame has been created.

```text
output/
├── components/
│   ├── terrain/
│   │   ├── terrain.xml
│   │   ├── terrain.hf
│   │   ├── terrain.glb
│   │   └── world-frame.json
│   ├── roads/
│   │   └── roads.glb
│   ├── road-markings/
│   │   └── road-markings.glb
│   └── buildings/
│       ├── buildings.xml
│       └── buildings.glb
└── world/
    ├── city-world.xml
    ├── city-world.glb
    └── city-world-receipt.json
```

The composed MJCF contains the terrain hfield and building collision geometry.
Road polygons are visual geometry only: terrain remains the single physical
ground surface, avoiding overlapping contacts and artificial road-edge steps.
The composed GLB contains terrain, roads, actual road markings, and buildings.

For the first visual contract, PLATEAU LOD2 traffic areas are draped on the
same DEM-derived surface and styled by `tran:function`:

- `1000`: roadway;
- `1010`: lane;
- `1020`: roadway intersection;
- `1030`: roadway;
- `2000`: sidewalk;
- auxiliary `3000`: traffic island or median.

Road markings are a separate visual component. Their geometry comes directly
from traffic-facility `frn:CityFurniture` features and their color comes from
the corresponding PLATEAU `app:X3DMaterial`. Supported source functions cover
lane, center, boundary, edge and stop lines, crosswalks, directive markings,
and regulatory markings. The output receipt records exact feature counts,
polygon counts, source/material provenance, and any material fallback.
Road-marking materials are rendered double-sided because PLATEAU polygon
winding does not guarantee an upward-facing normal; this changes visibility,
not the source geometry.
The horizontal marking outlines remain the source CityFurniture geometry, but
their display altitude is draped onto the shared DEM. This avoids inter-layer
altitude discrepancies hiding the markings below the road surface. A small,
recorded vertical display offset places road paint above the visual road mesh.

No synthetic sidewalk height is added. The DEM remains the common geometric
height source for both the GLB and MJCF. Procedural curb collision geometry is
deferred until a concrete simulation use case requires detail that the DEM and
height-field sampling do not preserve.

## Acceptance criteria

- The DEM source is parsed incrementally; the complete CityGML document is not
  materialized as an XML tree.
- The generated hfield has `101 x 101` finite samples and no unexplained gap.
- MuJoCo loads the generated XML without an hfield schema or size error.
- The center, north, east, south, and west height samples map to the expected
  MuJoCo directions.
- Every selected LOD1 road vertex receives an interpolated DEM height.
- Duplicated municipality or mesh coverage does not produce duplicated road
  surfaces.
- Source URLs, byte sizes, and SHA-256 values are recorded before promoting
  the prototype into the Business Pack Recipe.

## Deliberately deferred

- the full one-kilometer Shibuya build;
- detailed road materials beyond the semantic category colors;
- procedural curb or sidewalk collision geometry;
- bridges, tunnels, and stacked road surfaces that cannot be represented by a
  single-valued height field;
- a public compatibility promise for PLATEAU feature types other than `bldg`,
  `tran`, and `dem`;
- changes to the Business Pack catalog and production Recipe.
