# Command line

Every command takes `--db URL` and `--config FILE`. Settings precedence: flags, then `gridtiles.toml` (in the working directory unless `--config` names one), then `$GRIDTILES_DB`.

| Command | Does |
| --- | --- |
| `gridtiles init` | Install or upgrade the schema; records its version in `gridtiles.meta` |
| `gridtiles load SRC --layer NAME [--spec FILE] [--force]` | Create the layer if absent (from `--spec` or `gridtiles.toml`), load points, polygons or a raster, roll up |
| `gridtiles layers` | List layers with zooms, published tile zooms and cell counts |
| `gridtiles serve [--layer NAME ...]` | Serve live tiles with Martin and the viewer until interrupted |

An invalid spec, a missing layer spec, a missing database, an unsupported geometry or a polygon spanning more than 1,000,000 base cells (without `--force`) exits with status 2.

```bash
export GRIDTILES_DB=postgresql://postgres:postgres@localhost:5439/postgres
gridtiles init
gridtiles load tests/fixtures/points_10k.parquet --layer pop --spec tests/fixtures/spec_pop.json
gridtiles layers   # pop  z2-16  detail 4  tiles z0-12  ... cells
gridtiles serve    # viewer on http://localhost:8080, tiles on http://localhost:3000
```

`serve` writes `build/martin.yaml` (one function source `tile_<name>` per layer, serving the published tile zooms) and `build/viewer/`, then runs a local `martin` binary, or the pinned Martin Docker image on the host network when there is none. `--port`, `--martin-port` and `--build` change the defaults.

## gridtiles.toml

```toml
[backend]
url = "postgresql://postgres:postgres@localhost:5439/postgres"

[grid]
base_zoom = 16
min_zoom = 2
detail = 4            # sub-cells per tile = 2^detail per side

[[value]]
name = "population"
from = "pop"
agg = "sum"
unit = "people"

[[value]]
name = "completeness"
from = "osm_ratio"
agg = "mean"
weight = "area"
```

## Viewer

The viewer styles the layer's first value in 8 classes on the `stats` breakpoints. Sums and counts are shown per base cell (`value / 4^(base_zoom - z)`), so the base-zoom breakpoints hold at every zoom. Hovering a cell shows its quadkey, zoom, `n` and values. With several layers, a selector switches between them.
