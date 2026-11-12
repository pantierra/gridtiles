# SQL reference

Functions live in the `gridtiles` schema, installed from `sql/*.sql`. Requires PostgreSQL 16 or later (the SQL uses hexadecimal integer literals) and PostGIS.

## Quadkeys

A cell is identified by `(z, qk)`: `qk` is a `bigint` Morton code with x in the even bits and y in the odd bits, using XYZ tile addressing (y = 0 at the north edge, 0 ≤ z ≤ 30). Out-of-range input raises SQLSTATE `22003`; a quadkey string with a digit outside 0–3 raises `22P02`.

`qk_from_point` clamps rather than wraps: latitudes beyond the Web Mercator limit (±85.0511°) and longitudes outside ±180° map to the edge row or column. A point on a cell boundary belongs to the cell east / south of it.

| Function | Returns |
| --- | --- |
| `qk_from_xyz(z, x, y)` | `bigint` |
| `qk_to_xy(z, qk)` | `(x int, y int)` |
| `qk_to_text(z, qk)` | Bing quadkey string, exactly z digits |
| `qk_from_text(t)` | `bigint`, at zoom `length(t)` |
| `qk_parent(z, qk, z_to)` | ancestor at `z_to ≤ z` |
| `qk_child_range(z, qk, z_to)` | half-open `int8range` of descendants at `z_to ≥ z` |
| `qk_envelope(z, qk)` | cell polygon in EPSG:3857 |
| `qk_from_point(geom, z)` | cell containing a point of any SRID; `NULL` if empty |

```sql
select gridtiles.qk_from_xyz(3, 3, 5);                       -- 39
select gridtiles.qk_to_text(3, 39);                          -- '213'
select * from gridtiles.qk_to_xy(3, gridtiles.qk_from_text('213'));  -- (3, 5)
select gridtiles.qk_parent(3, 39, 1);                        -- 2
select gridtiles.qk_child_range(1, 2, 3);                    -- [32,48)
select gridtiles.qk_from_point('SRID=4326;POINT(13.405 52.52)', 16);
select ST_AsText(gridtiles.qk_envelope(1, 1));
```

## Layers

A layer is a row in `gridtiles.layers` plus a cell table `gridtiles.cells_<name>` with primary key `(z, qk)`, a record count `n` and the stored columns of each value:

| `agg` | Stored columns | Tile property `<v>` |
| --- | --- | --- |
| `count` | uses `n` | — (`n`) |
| `sum` | `<v>_sum` | `<v>_sum` |
| `min` | `<v>_min` | `<v>_min` |
| `max` | `<v>_max` | `<v>_max` |
| `mean` | `<v>_sum`, `<v>_w` (weight, or 1 per record) | `<v>_sum / <v>_w` |

`create_layer(spec jsonb)` validates the [layer spec](python.md#gridtilesspec) (SQLSTATE `22023` if invalid) and creates both; `drop_layer(name)` removes both.

```sql
select gridtiles.create_layer('{
  "name": "pop", "base_zoom": 16, "min_zoom": 2, "detail": 4,
  "values": [
    {"name": "population", "from": "pop", "agg": "sum", "unit": "people"},
    {"name": "completeness", "from": "osm_ratio", "agg": "mean", "weight": "area"}
  ]
}');
\d gridtiles.cells_pop   -- z, qk, n, population_sum, completeness_sum, completeness_w
select gridtiles.drop_layer('pop');
```

## Ingest

`ingest_points(name, staging regclass)` aggregates a staging table of points (a `geom` column plus one `double precision` column per value source and weight) into the layer's base cells at `z = base_zoom` and returns the number of cells written. Existing cells are merged, so repeated loads add up. Rows with a NULL or empty geometry are skipped; a NULL value contributes nothing to its value's columns.

`ingest_polygons(name, staging regclass, max_cells bigint default 1000000)` splits each polygon over the base cells it overlaps (in EPSG:3857): sums get value × covered fraction, means are weighted by covered area, min/max apply to every touched cell, and `n` counts touching polygons. A polygon whose bounding box spans more than `max_cells` base cells raises SQLSTATE `54000` (`NULL`: no limit). Invalid polygons (e.g. self-intersecting) are repaired with `ST_MakeValid` first, then clipped to the Web Mercator square (±85.0511°), so a polygon's whole value lands on the cells that exist. Areas, and so shares and mean weights, are measured in EPSG:3857: within a large polygon at high latitude they favour the poleward part. A polygon reaching ±90° cannot be projected and aborts the load; polygons crossing the antimeridian are not split.

```sql
create temp table staging (geom geometry, pop double precision, osm_ratio double precision, area double precision);
insert into staging values ('SRID=4326;POINT(13.405 52.52)', 10, 0.5, 100);
select gridtiles.ingest_points('pop', 'staging');  -- 1

truncate staging;
insert into staging values ('SRID=4326;POLYGON((13.40 52.51, 13.41 52.51, 13.41 52.52, 13.40 52.51))', 400, 0.5, 100);
select gridtiles.ingest_polygons('pop', 'staging');  -- cells written
```

## Rollup

`rollup(name)` rebuilds every level from `base_zoom - 1` down to `min_zoom` from its children (`qk >> 2`), merging each stored column per its aggregator, then writes `layers.stats`: per value the `min`, `max` and 7 quantile `breaks` (1/8 … 7/8) of the tile property at the base zoom, and for `count` and `sum` values the `total` (`null` for `mean`, `min` and `max`). Sums run in quadkey order, so a rebuild is reproducible bit for bit.

```sql
select gridtiles.rollup('pop');
select z, count(*), sum(n) from gridtiles.cells_pop group by z order by z;
select stats->'population' from gridtiles.layers where name = 'pop';
```

## Vector tiles

`mvt(name, z, x, y)` returns one Mapbox Vector Tile with a layer `cells` (extent 4096, buffer 0). A tile at zoom `t` carries the cells at `c(t) = clamp(t + detail, min_zoom, base_zoom)`: with the default `detail = 4`, a 16 × 16 grid of sub-cells, read as one contiguous key range. Past the base zoom it carries the single ancestor cell. Each feature is a cell square with the properties `qk` (text form), `z`, `n` and one property per non-`count` value. An empty tile is a zero-length `bytea`.

`create_layer` also creates `tile_<name>(z, x, y)`, the entry point for tile servers such as Martin.

```sql
select length(gridtiles.mvt('pop', 12, 2200, 1343));
select length(gridtiles.tile_pop(12, 2200, 1343));
```

Published tile zooms are `[max(0, min_zoom - detail), max(0, base_zoom - detail)]`; viewers overzoom beyond.
