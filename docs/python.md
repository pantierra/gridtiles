# Python reference

## `gridtiles.spec`

`LayerSpec`, `SourceSpec` and `ValueSpec` model a layer spec; `gridtiles.create_layer` checks its values (see [sql.md](sql.md#layers)).

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
| `create_layer(conn, spec)` | Register a layer (see [sql.md](sql.md#layers)); `gridtiles.drop_layer(name)` in SQL removes one |
| `layers(conn, names=None, *, stats=False, params=None)` | Registered layers; with `stats`, also their stats and bounds for the tiles of `params` |

## `gridtiles.export`

`export_pmtiles(conn, layer, out_path, *, params=None)` writes the layer's tiles of `params` (default: all rows) to a PMTiles archive and writes the viewer next to it; exports into one folder share the viewer. `manifest(layer)` is the metadata stored under the archive's `gridtiles` key.

```python
import psycopg

from gridtiles.export import export_pmtiles

with psycopg.connect("postgresql://postgres:postgres@localhost:5439/postgres") as conn:
    export_pmtiles(conn, "pop", "site/pop.pmtiles")  # site/pop.pmtiles, site/index.html, site/config.json
```
