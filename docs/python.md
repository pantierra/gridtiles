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

`LayerSpec` validates a layer spec: name `^[a-z][a-z0-9_]{0,47}$`, a `source` (`table`, `geometry` default `geom`, optional `time`), `0 ≤ min_zoom ≤ base_zoom ≤ 30`, `detail` in 0–8 (default 4), unique value names, `agg` one of `count`, `sum`, `min`, `max`, `mean`, and `weight` only with `mean`.

```python
from gridtiles.spec import LayerSpec

spec = LayerSpec.model_validate(
    {
        "name": "pop",
        "source": {"table": "pop_points"},
        "base_zoom": 16,
        "min_zoom": 2,
        "values": [{"name": "population", "from": "pop", "agg": "sum", "unit": "people"}],
    }
)
```

## `gridtiles.db`

| Function | Does |
| --- | --- |
| `install(conn)` | Install the SQL; returns the schema version |
| `create_layer(conn, spec)`, `drop_layer(conn, name)` | Register or remove a layer (see [sql.md](sql.md#layers)) |
| `layers(conn, names=None, *, stats=False, params=None)` | Registered layers; with `stats`, also their stats and bounds, time values and the parameters the stats describe (default: the latest time) |

## `gridtiles.importer`

`import_file(conn, path, table, *, replace=False)` creates a plain table from a point or polygon file: a `geom` column (`Point` or `MultiPolygon`, in the file's SRID; EPSG:4326 if it declares none), the file's boolean, integer, float, text, date and timestamp columns, and the GiST index tiles need. It reads CSV (`lon` / `lat` columns, by common names), GeoParquet and anything GDAL reads. An existing table raises `ValueError` unless `replace` drops it first. Null and empty geometries are skipped; anything but points or polygons, or a mix of both, raises `UnsupportedGeometry`. It returns an `ImportReport` (`records_read`, `records_imported`, `kind`, `columns`).

```python
import psycopg

from gridtiles import db
from gridtiles.importer import import_file
from gridtiles.spec import LayerSpec

with psycopg.connect("postgresql://postgres:postgres@localhost:5439/postgres") as conn:
    import_file(conn, "tests/fixtures/points_small.csv", "pop_points")
    db.create_layer(conn, LayerSpec.model_validate_json(open("tests/fixtures/spec_pop.json").read()))
```

## `gridtiles.export`

`export_pmtiles(conn, layer, out_path, *, params=None, workers=4)` writes the layer's tiles of `params` (default: the latest time, if any) to a PMTiles archive, fetching them over `workers` parallel connections, and writes the viewer next to it; exports into one folder share the viewer. `manifest(layer)` is the metadata stored under the archive's `gridtiles` key.

```python
import psycopg

from gridtiles.export import export_pmtiles

with psycopg.connect("postgresql://postgres:postgres@localhost:5439/postgres") as conn:
    export_pmtiles(conn, "pop", "site/pop.pmtiles")  # site/pop.pmtiles, site/index.html, site/config.json
```
