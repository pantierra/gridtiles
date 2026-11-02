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

`ingest_points(name, staging regclass)` aggregates a staging table of points (a `geom` column plus one `double precision` column per value source and weight) into the layer's base cells at `z = base_zoom` and returns the number of cells written. Existing cells are merged, so repeated loads add up. Rows with a NULL geometry are skipped; a NULL value contributes nothing to its value's columns.

```sql
create temp table staging (geom geometry, pop double precision, osm_ratio double precision, area double precision);
insert into staging values ('SRID=4326;POINT(13.405 52.52)', 10, 0.5, 100);
select gridtiles.ingest_points('pop', 'staging');  -- 1
```
