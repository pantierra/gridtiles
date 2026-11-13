# gridtiles

**A tile-based discrete global grid system, optimized for visualization.**

Serve any PostGIS table of points or polygons as a quadkey grid of zoomable vector tiles, aggregated live on every request. [Why a quadkey grid?](docs/concept.md)

## Installation

```bash
uv add gridtiles
```

## Quickstart

With PostgreSQL + PostGIS running (`make up` starts one on port 5439):

```bash
export GRIDTILES_DB=postgresql://postgres:postgres@localhost:5439/postgres
gridtiles init
gridtiles import points.parquet --table pop_points   # or use any table with a geometry column
gridtiles add pop --spec spec.json                   # spec.json names the source table and values
gridtiles serve                                      # open http://localhost:8080
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
make up      # start PostGIS (port 5439) and Martin (port 3000)
make check   # lint, type check, unit + SQL tests, end-to-end tests
make down
```

| Command | Does |
| --- | --- |
| `make up` / `make down` | Start / stop the compose services, waiting until healthy |
| `make db-reset` | Recreate the `gridtiles_test` database and install the schema (`gridtiles init`) |
| `make test` | Unit and SQL tests |
| `make e2e` | End-to-end tests against the running services |
| `make check` | ruff, mypy and all tests, including end-to-end |
| `make bench` | Tile latency per zoom on 10 million generated points and 100,000 polygons |

Override the host ports with `GRIDTILES_DB_PORT` and `GRIDTILES_MARTIN_PORT`, and point tests elsewhere with `GRIDTILES_TEST_DSN` and `GRIDTILES_MARTIN_URL`.

The SQL in `src/gridtiles/sql/` installs the `gridtiles` schema (`gridtiles init`); check the installed version with:

```sql
select gridtiles.version();
```

## License

[MIT](LICENSE)
