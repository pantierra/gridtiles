# gridtiles

**A tile-based discrete global grid system, optimized for visualization.**

Serve any PostGIS table of points or polygons as a quadkey grid of zoomable vector tiles, aggregated live on every request. [Why a quadkey grid?](docs/concept.md)

![The viewer on 1 million points and 14,000 polygons: monthly snapshots, zooming, hovering, a live insert and polygons](docs/demo.gif)

One million points and 14,000 polygons in PostGIS, aggregated on each tile request: a time slider over monthly snapshots (`?at=`), squares cut fresh at every zoom, a live `INSERT` that shows on the next request, and polygons split over squares by covered area.

## Installation

```bash
uv add gridtiles
```

## Quickstart

With PostgreSQL + PostGIS running (`make up` starts one on port 5439):

```bash
export GRIDTILES_DB=postgresql://postgres:postgres@localhost:5439/postgres
gridtiles init
ogr2ogr -f PostgreSQL "PG:$GRIDTILES_DB" points.gpkg -nln pop_points -lco GEOMETRY_NAME=geom  # or any table with a geometry column
gridtiles add pop --spec spec.json   # spec.json names the source table and values
gridtiles serve                      # open http://localhost:8080
```

## Documentation

- [Concept: a global grid built for rendering](docs/concept.md)
- [Command line](docs/cli.md)
- [SQL reference](docs/sql.md)
- [Python reference](docs/python.md)
- [Performance](docs/performance.md)

## Development

Requires [uv](https://docs.astral.sh/uv/) and Docker with Compose.

```bash
uv sync
uv run pre-commit install
make up      # start PostGIS (port 5439)
make check   # lint, type check, unit + SQL tests, end-to-end tests
make down
```

| Command | Does |
| --- | --- |
| `make up` / `make down` | Start / stop PostGIS, waiting until healthy |
| `make test` | Unit and SQL tests |
| `make e2e` | End-to-end tests: `gridtiles serve` (Martin from a local binary or Docker) and the viewer in a browser |
| `make check` | ruff, mypy and all tests, including end-to-end |
| `make bench` | Tile latency per zoom on 10 million generated points and 100,000 polygons |

Override the host port with `GRIDTILES_DB_PORT`, and point tests elsewhere with `GRIDTILES_TEST_DSN`.

The SQL in `src/gridtiles/sql/` installs the `gridtiles` schema (`gridtiles init`); check the installed version with:

```sql
select gridtiles.version();
```

## License

[MIT](LICENSE)
