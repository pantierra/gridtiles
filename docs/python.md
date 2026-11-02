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

## Backends

Every store implements the `Backend` protocol (`gridtiles.backends.base`); the CLI and exporter use nothing else. `open_backend(url)` opens PostgreSQL for a `postgresql://` URL or a libpq `key=value` string.

| Operation | Does |
| --- | --- |
| `install()` | Create or upgrade the schema; returns its version |
| `create_layer(spec)`, `drop_layer(name)` | Register or remove a layer (`LayerExists` if taken) |
| `list_layers(names=None)` | `Layer`s with spec, stats, cell count and bounds; `LookupError` for unknown names |
| `load(layer, path, *, kind=None, lon=None, lat=None, crs="EPSG:4326", band=1, rollup=True, max_cells_per_feature=1_000_000)` | Aggregate points, polygons or a raster into base cells, then roll up; returns a `LoadReport` |
| `upsert(layer, path, *, mode="add", refresh_stats=False, ...)` | Apply a batch incrementally: `add` merges, `replace` overwrites the touched base cells; the result equals a full rollup. Stats, cell count and bounds only with `refresh_stats` |
| `rollup(layer)` | Rebuild the coarser levels, stats and bounds |
| `tile(layer, z, x, y)` | One MVT (`b""` if empty); thread-safe |
| `tiles_with_data(layer)` | `(z, x, y)` of every tile with data in the published zoom range |
| `manifest(layer)` | Version, spec, stats and bounds of a layer |

Capabilities tell what else a backend offers: `live` (an external tile server reads the store), `incremental` (upserts touch only what changed), `native_mvt` (the store encodes tiles itself).

PostgreSQL + PostGIS has `live`, `incremental` and `native_mvt`: upserts touch only the changed cells and their ancestors, and tiles come from `gridtiles.mvt` (`ST_AsMVT`) in SQL, served by Martin or embedded.

### Sources

`load` reads GeoParquet (pyarrow), anything GDAL reads (CSV with `lon` / `lat` columns, GeoJSON, Shapefile, GeoPackage…) and rasters (`.tif`, `.tiff`, `.vrt`, `.jp2`, `.nc`). Every geometry is projected once to EPSG:3857 with the spherical Web Mercator formula and latitude clamp of `quadkey.to_mercator`, so all backends see the same coordinates. `crs` applies when the source declares none.

| Source | Values per base cell |
| --- | --- |
| Points | each point adds its values |
| Polygons (and multipolygons) | sums get value × covered fraction; means are weighted by covered area; min/max apply to every touched cell; `n` counts touching polygons |
| Raster band `band` | each valid pixel is a point at its centre with the source column `value` |

Null and empty geometries count as `skipped_null_geometry`. A geometry other than points and polygons, or a mix of both, raises `UnsupportedGeometry`; a polygon spanning more than `max_cells_per_feature` base cells raises `CellLimitExceeded` (`None`: no limit); pixels larger than half a base cell raise `ValueError` with a suggested base zoom. A raster's whole band is held in memory, plus Python objects for every valid pixel (several GB for 100 million pixels), so split very large rasters into tiles first.

The load and the rollup run in one transaction, so a failing rollup rolls back the load. Each rollup rebuilds every coarser level, so when loading many files pass `rollup=False` and call `backend.rollup(layer)` once at the end.

```python
from gridtiles.backends.base import open_backend
from gridtiles.spec import LayerSpec

with open_backend("postgresql://postgres:postgres@localhost:5439/postgres") as backend:
    backend.install()
    backend.create_layer(LayerSpec.model_validate_json(open("tests/fixtures/spec_pop.json").read()))
    print(backend.load("pop", "tests/fixtures/points_small.csv"))
    # LoadReport(records_read=8, records_loaded=8, skipped_null_geometry=0, cells_written=3)
    tile = backend.tile("pop", 12, 2200, 1343)
```

## `gridtiles.export`

`export_pmtiles(backend, layer, out_path, *, workers=4)` writes the layer's tiles to a PMTiles archive, fetched by `workers` threads, and the viewer next to it. The archive metadata holds the layer manifest under `gridtiles`.

```python
from gridtiles.backends.base import open_backend
from gridtiles.export import export_pmtiles

with open_backend("postgresql://postgres:postgres@localhost:5439/postgres") as backend:
    export_pmtiles(backend, "pop", "site/pop.pmtiles")  # site/pop.pmtiles, site/index.html, site/config.json
```
