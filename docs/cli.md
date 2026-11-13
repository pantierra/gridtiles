# Command line

Every command takes `--db URL`, by default `$GRIDTILES_DB`.

| Command | Does |
| --- | --- |
| `gridtiles init` | Install the schema (idempotent) |
| `gridtiles add LAYER --spec FILE` | Register a layer over the table its JSON spec names |
| `gridtiles layers` | List layers with source, zooms and published tile zooms |
| `gridtiles cache LAYER --max-zoom N \| --drop` | Cache or drop the coarse levels of a layer (see [sql.md](sql.md#cache)); run again to refresh |
| `gridtiles serve [--layer NAME ...]` | Serve live tiles with Martin and the viewer until interrupted |
| `gridtiles export --layer NAME -o FILE.pmtiles [--at V]` | Write a PMTiles archive plus `index.html` and `config.json` next to it |

An invalid or unreadable spec, a missing or unusable source or a missing database exits with status 2, as does `serve` when Martin exits on its own (e.g. on a bad database URL or a busy port).

```bash
export GRIDTILES_DB=postgresql://postgres:postgres@localhost:5439/postgres
gridtiles init
ogr2ogr -f PostgreSQL "PG:$GRIDTILES_DB" points.gpkg -nln pop_points -lco GEOMETRY_NAME=geom
gridtiles add pop --spec tests/fixtures/spec_pop.json
gridtiles serve    # viewer on http://localhost:8080, tiles on http://localhost:3000
gridtiles export --layer pop -o site/pop.pmtiles
```

Any tool that writes a PostGIS table with a typed geometry column works as a source. ogr2ogr types the column from the file and creates the spatial index; for polygons, add `-nlt MULTIPOLYGON` so polygons and multipolygons share one type.

`export` writes every tile with data in the published zoom range (gzip-compressed MVT) to one archive whose metadata holds the layer manifest (`gridtiles`: version, spec, params, stats, bounds). It is a snapshot of the source at export time. Upload the folder to any static host that answers HTTP range requests (S3, R2, GitHub Pages; Python's `http.server` does not). Exports of the same data are byte-identical.

`serve` writes `build/martin.yaml` (one function source `tile_<name>` per layer, serving the published tile zooms, with Martin's tile cache off so tiles follow the source) and `build/viewer/`, then runs a local `martin` binary, or the pinned Martin Docker image on the host network when there is none. `--port`, `--martin-port` and `--build` change the defaults.

Both servers listen on `127.0.0.1` only. `build/martin.yaml` holds the database URL including its password, so keep `--build` out of version control and shared folders. Docker host networking needs Linux; on macOS or Windows, install a local `martin` binary. The viewer loads MapLibre and the PMTiles reader from unpkg (pinned, with subresource integrity), so it needs internet access.

## Viewer

The viewer styles the layer's first value in 8 classes on the `stats` breakpoints, which `serve` recomputes at most every 5 minutes. On a layer with a time column, it shows the latest time and a slider over the others. Sums and counts are shown per base cell (`value / 4^(base_zoom - z)`), so the base-zoom breakpoints hold at every zoom. Hovering a cell shows its quadkey, zoom, `n` and values. With several layers, a selector switches between them.
