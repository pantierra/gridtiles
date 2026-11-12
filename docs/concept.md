# Concept: a global grid built for rendering

gridtiles stores data in a discrete global grid: a hierarchy of cells that covers the world, where every cell has an ID. H3 and S2 work that way too. The difference is what the grid is built for. H3, S2 and most other global grids aim for cells of (nearly) equal area, which serves analysis. gridtiles uses the quadkey grid of web map tiles instead, which serves rendering. It gives up equal area for two properties:

- **Every tile is one key range.** Reading a tile is a single range scan on the primary key.
- **Every cell is a ready-made square.** Its geometry follows from its key, so no geometry is stored, and the square drops into a vector tile as is.

## The grid

A cell is a Web Mercator (EPSG:3857) tile, identified by its zoom `z` and its quadkey. Each cell splits into exactly four children at `z + 1`, and the hierarchy reaches zoom 30. The quadkey is stored as a `bigint` Morton code: x and y interleave bit by bit, so its base-4 digits are the familiar quadkey string (`"1202"`), most significant first.

This gives three useful properties:

- **Parent and children are bit shifts.** A cell's parent is `qk >> 2`. Its descendants at a finer zoom form one contiguous range, from `qk << 2k` up to, but not including, `(qk + 1) << 2k`.
- **Containment is exact.** Children tile their parent with no gaps or overlaps. So a rollup (sum, count, min, max, weighted mean) built level by level from the children equals one computed directly from the data, bit for bit.
- **Map tiles and cells share one address space.** A map tile `z/x/y` is itself a cell. Its contents at any finer zoom are a single key range.

## From data to tiles

A layer aggregates its data into cells at a base zoom (for example 16, cells of roughly 600 m at the equator). It then rolls them up to every coarser zoom down to its minimum. A tile at zoom `t` carries the cells `detail` zooms finer, `c(t) = clamp(t + detail, min_zoom, base_zoom)`. With the default `detail = 4`, that's a 16 × 16 grid of squares in every tile, at every zoom.

So drawing a tile takes one indexed range read (`z = c and qk >= lo and qk < hi`) plus arithmetic to place each square. No stored geometry is read, simplified or searched spatially. Past the base zoom, a tile shows the single ancestor cell that covers it, cut to the tile. PostGIS encodes the tile with `ST_AsMVT`, and Martin serves it. The same tiles also export to a static PMTiles archive.

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
