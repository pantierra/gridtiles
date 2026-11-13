# Concept: a global grid built for rendering

gridtiles aggregates data into a discrete global grid: a hierarchy of cells that covers the world, where every cell has an ID. H3 and S2 work that way too. The difference is what the grid is built for. H3, S2 and most other global grids aim for cells of (nearly) equal area, which serves analysis. gridtiles uses the quadkey grid of web map tiles instead, which serves rendering. It gives up equal area for two properties:

- **Every tile cuts its own cells.** A tile's cells follow from `z/x/y` alone, so the grid is never stored.
- **Every cell is a ready-made square.** Its geometry follows from its key, and the square drops into a vector tile as is.

## The grid

A cell is a Web Mercator (EPSG:3857) tile `z/x/y`, identified by its quadkey: `z` base-4 digits, most significant first (`"1202"` is a cell at zoom 4), each digit naming one of the four children of the cell before it. Each cell splits into exactly four children at `z + 1`, and the hierarchy reaches zoom 30.

This gives three useful properties:

- **Parent and children are prefixes.** A cell's parent is its quadkey without the last digit; its descendants at any finer zoom are the quadkeys it prefixes, one contiguous range in quadkey order.
- **Containment is exact.** Children tile their parent with no gaps or overlaps, so a point belongs to exactly one cell at every zoom.
- **Map tiles and cells share one address space.** A map tile `z/x/y` is itself a cell. Its contents at any finer zoom are a single key range.

## From data to tiles

A layer is a view over any table with a geometry column; the table stays the single source of truth. A tile at zoom `t` carries the cells `detail` zooms finer, `c(t) = clamp(t + detail, min_zoom, base_zoom)`: with the default `detail = 4`, a 16 × 16 grid of squares in every tile, at every zoom. Past the base zoom (for example 16, cells of roughly 600 m at the equator), a tile shows the single ancestor cell that covers it.

Drawing a tile reads the source rows inside it through a spatial index and aggregates them into its squares on request: a point by arithmetic on its coordinates, a polygon by its covered area in each square. A write to the table shows on the next request, and a time column turns into snapshots and differences by query parameter. PostGIS encodes the tile with `ST_AsMVT`, and Martin serves it. The same tiles also export to a static PMTiles archive.

The price is at low zooms, where one tile aggregates much of the table; see [performance](performance.md).

## What it gives up

- **Equal area.** A cell's ground area shrinks with `cos²(latitude)`. A base cell at 60° covers a quarter of the ground of one at the equator, and one at 75° about a fifteenth. Counts and sums per cell are therefore not densities; compare cells at similar latitudes, or divide by ground area. The viewer shows sums and counts per base cell, so colours stay comparable across zooms. They aren't comparable across latitudes.
- **The poles.** Web Mercator stops at ±85.05°. Points beyond it fall into the edge cells, and WGS84 polygons are clipped to it.
- **Squares, not hexagons.** A square has four edge neighbours and four corner neighbours at a different distance, while a hexagon has six at equal distance. That matters for neighbourhood analysis and smoothing, not for display.

## When to use which

| | Quadkey grid (gridtiles) | H3 |
| --- | --- | --- |
| Cell shape | Square in Web Mercator | Hexagon (plus 12 pentagons) |
| Cell area | Shrinks with `cos²(latitude)` | Roughly equal everywhere |
| Children | Exactly 4, tiling the parent | 7, only approximately covering the parent |
| A map tile is | One key range | Many cells found by spatial lookup, some crossing the tile edge |
| Cell geometry | Computed from the key | Computed polygon per cell |
| Built for | Rendering zoomable maps | Spatial analysis, neighbourhoods, mobility |

Use gridtiles to put gridded data (population, completeness or coverage indicators, aggregated sensor or raster values) on an interactive map at every zoom, quickly and cheaply. Use H3 or S2 when the analysis itself needs equal-area cells or uniform neighbourhoods. Nothing stops you from doing that analysis in H3 and then loading the results into gridtiles to display them.
