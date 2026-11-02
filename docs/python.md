# Python reference

## `gridtiles.quadkey`

A pure-Python mirror of the SQL quadkey functions (see [sql.md](sql.md)), without the `qk_` prefix. Invalid input raises `ValueError`. `from_lonlat` clamps out-of-range latitudes and longitudes to the edge cells, as `qk_from_point` does.

```python
from gridtiles import quadkey

quadkey.from_xyz(3, 3, 5)  # 39
quadkey.to_text(3, 39)  # '213'
quadkey.to_xy(3, quadkey.from_text("213"))  # (3, 5)
quadkey.parent(3, 39, 1)  # 2
quadkey.child_range(1, 2, 3)  # range(32, 48)
quadkey.envelope(1, 1)  # (0.0, 0.0, 20037508.34…, 20037508.34…)
quadkey.from_lonlat(13.405, 52.52, 16)  # cell containing the point
```

## `gridtiles.spec`

`LayerSpec` and `ValueSpec` validate a layer spec: name `^[a-z][a-z0-9_]{0,47}$`, `0 ≤ min_zoom ≤ base_zoom ≤ 30`, `detail` in 0–8 (default 4), unique value names, `agg` one of `count`, `sum`, `min`, `max`, `mean`, and `weight` only with `mean`.

```python
from gridtiles.spec import LayerSpec

spec = LayerSpec.model_validate(
    {
        "name": "pop",
        "base_zoom": 16,
        "min_zoom": 2,
        "values": [{"name": "population", "from": "pop", "agg": "sum", "unit": "people"}],
    }
)
spec.detail  # 4
spec.model_dump_json()  # JSON accepted by gridtiles.create_layer
```

## `gridtiles.ingest`

`load(conn, layer, path, ...)` loads a file into an existing layer, detecting what it holds: points, polygons (or multipolygons) or a raster (`.tif`, `.tiff`, `.vrt`, `.jp2`, `.nc`). It rebuilds the coarser levels and stats unless `rollup=False`, and returns a `LoadReport` (`records_read`, `records_loaded`, `skipped_null_geometry`, `cells_written`). Repeated loads add up.

| Function | Reads | Values per base cell |
| --- | --- | --- |
| `load_points(conn, layer, path, *, lon=None, lat=None, crs="EPSG:4326", rollup=True)` | CSV (`lon` / `lat` columns, default: common names), GeoParquet (pyarrow), anything GDAL reads | each point adds its values |
| `load_polygons(conn, layer, path, *, crs="EPSG:4326", rollup=True, max_cells_per_feature=1_000_000)` | anything GDAL reads, GeoParquet | sums get value × covered fraction; means are weighted by covered area; min/max apply to every touched cell; `n` counts touching polygons |
| `load_raster(conn, layer, path, *, band=1, rollup=True)` | anything rasterio reads | each valid pixel is a point at its centre with the source column `value` |

`crs` applies when the source declares none. Null and empty geometries count as `skipped_null_geometry`. A geometry other than points and polygons, or a mix of both, raises `UnsupportedGeometry`; a polygon spanning more than `max_cells_per_feature` base cells raises `CellLimitExceeded` (`None`: no limit); pixels larger than half a base cell raise `ValueError` with a suggested base zoom.

The load and the rollup run in one transaction, so a failing rollup rolls back the load. Each rollup rebuilds every coarser level, so when loading many files pass `rollup=False` and call the SQL function once at the end: `conn.execute("select gridtiles.rollup(%s)", (layer,))`.

```python
import psycopg

from gridtiles.ingest import load

with psycopg.connect("postgresql://postgres:postgres@localhost:5439/postgres") as conn:
    print(load(conn, "pop", "tests/fixtures/points_small.csv"))
    # LoadReport(records_read=8, records_loaded=8, skipped_null_geometry=0, cells_written=3)
```
