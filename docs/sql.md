# SQL reference

Functions live in the `gridtiles` schema, installed from `sql/*.sql`. Requires PostgreSQL 16 or later (the SQL uses hexadecimal integer literals) and PostGIS.

## Quadkeys

Square `(x, y)` at zoom `z` is the tile `z/x/y` (XYZ addressing, y = 0 at the north edge, 0 ≤ z ≤ 30); its envelope is `ST_TileEnvelope(z, x, y)`. `quadkey(z, x, y)` returns its Bing quadkey, `z` base-4 digits, most significant first: the `qk` property of tile features.

```sql
select gridtiles.quadkey(3, 3, 5);  -- '213'
```

## Layers

A layer is a row in `gridtiles.layers`: a source relation (table, view or materialized view) and how to aggregate it. Nothing is copied; every tile aggregates the source on request, so a committed write shows on the next request.

`create_layer(spec jsonb)` validates the spec (SQLSTATE `22023` if invalid): a name `^[a-z][a-z0-9_]{0,47}$`, a `source` (`table`, `geometry` default `geom`, optional `time`), `0 ≤ min_zoom ≤ base_zoom ≤ 30`, `detail` in 0–8 (default 4), values with unique names of the same pattern, a `from` column, `agg` one of `count`, `sum`, `min`, `max`, `mean`, and a `weight` column only with `mean`. Then it checks the source: a missing table raises `42P01`, a missing column `42703`, and a column of the wrong type `42804`. The geometry column must be typed `geometry(Point|Polygon|MultiPolygon, <srid>)`, values must be numeric, and the optional `time` column a `date`, `timestamp` or `timestamptz`. It also creates `tile_<name>(z, x, y, query_params json)`, the entry point for Martin. `drop_layer(name)` removes both and leaves the source alone.

```sql
select gridtiles.create_layer('{
  "name": "pop", "source": {"table": "pop_points", "geometry": "geom", "time": "day"},
  "base_zoom": 16, "min_zoom": 2, "detail": 4,
  "values": [
    {"name": "population", "from": "pop", "agg": "sum", "unit": "people"},
    {"name": "completeness", "from": "osm_ratio", "agg": "mean", "weight": "area"}
  ]
}');
select gridtiles.drop_layer('pop');
```

Tiles filter the source by the tile's bounding box, so the source needs a GiST index on what the filter reads:

| Source SRID | Index |
| --- | --- |
| 4326 or 3857 | `create index on t using gist (geom)` |
| any other | `create index on t using gist (ST_Transform(geom, 4326))` |
| with a time column | also `create index on t (day)` |

## Vector tiles

`mvt(name, z, x, y, params jsonb default '{}', cached boolean default true)` returns one Mapbox Vector Tile with a layer `cells` (extent 4096, buffer 0). A tile at zoom `t` carries the squares at `c(t) = clamp(t + detail, min_zoom, base_zoom)`: with the default `detail = 4`, a 16 × 16 grid. Past the base zoom it carries the single ancestor square. Each feature is a square with the properties `qk` (text form), `z`, `n` and one per non-`count` value, in quadkey order. An empty tile is a zero-length `bytea`.

| `agg` | Tile property, over the source rows in a square |
| --- | --- |
| `count` | none: `n` is the row count |
| `sum` | sum of the value |
| `min`, `max` | minimum, maximum of the value |
| `mean` | mean of the value, weighted by `weight` (default 1) |

Points count in the square holding them; a point on a boundary belongs to the square east / south of it, and points beyond the Web Mercator square fall into the edge squares. Polygons are split over the squares they overlap by covered area (in EPSG:3857): sums get value × covered fraction, means are weighted by covered area, min/max apply to every touched square, and `n` counts the polygons touching a square. Invalid polygons are repaired with `ST_MakeValid` and clipped to the Web Mercator square (±85.0511°) first. Polygons crossing the antimeridian are not split. PostGIS raster sources are not supported.

Sums add their terms in ascending order, so a tile is reproducible bit for bit for the same source rows, whatever their physical order.

Published tile zooms are `[max(0, min_zoom - detail), max(0, base_zoom - detail)]`; viewers overzoom beyond.

## Parameters

`params` (Martin passes the URL query as `query_params`) filter the source's `time` column:

| Parameter | Tile of |
| --- | --- |
| `at=V` | the rows with `time = V` (a snapshot) |
| `start=V`, `end=V` | the rows with `start <= time < end`; either may be absent |
| `at=V&diff=W` | `at` minus `diff`, per square: counts and sums count a missing side as 0; means, minima and maxima exist only where both sides have data |

A malformed parameter (not a valid date or timestamp, `at` with `start`/`end`, `diff` without `at`, a time parameter on a layer without a time column) raises `22023` from `mvt`; `tile_<name>` returns an empty tile instead and logs a warning. Other errors, such as a dropped source, propagate. Other keys are ignored.

```sql
select length(gridtiles.mvt('pop', 12, 2200, 1343, '{"at": "2024-02-01", "diff": "2024-01-01"}'));
```

## Stats

`stats(name, params jsonb default '{}')` aggregates the whole source at the base zoom and returns `{"stats": {...}, "bounds": [west, south, east, north]}`: per value the `min`, `max` and 7 quantile `breaks` (1/8 … 7/8) of the tile property, and for `count` and `sum` values the `total`.

`times(name)` lists the distinct values of the time column as text (`null` without one or with more than 1000). `tiles_with_data(name, params default '{}')` lists `(z, x, y)` of every published tile with data.

## Cache

A tile at a low zoom aggregates much of the source; see [performance](performance.md). `refresh_cache(name, max_zoom)` stores the squares of levels `min_zoom` … `max_zoom` in `gridtiles.cache_<name>`, as tiles compute them; tiles at those levels then read the cache unless a parameter filters the source or `mvt` is called with `cached => false`, as exports do; `stats` and `tiles_with_data` always read the source. The cache lags the source until refreshed (`layers.cached_at` says when); `refresh_cache(name, null)` drops it. It returns the number of cached squares.

```sql
select gridtiles.refresh_cache('pop', 8);
```
