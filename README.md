# gridtiles

Turn any point, polygon or raster dataset into a quadkey grid and serve it as zoomable vector tiles.

> [!WARNING]
> This project is in early development. The API is not stable yet.

## Installation

```bash
uv add gridtiles
```

## Quickstart

With PostgreSQL + PostGIS running (`make up` starts one on port 5439), or with a DuckDB file instead (`GRIDTILES_DB=cells.duckdb`):

```bash
export GRIDTILES_DB=postgresql://postgres:postgres@localhost:5439/postgres
gridtiles init
gridtiles load points.parquet --layer pop --spec spec.json
gridtiles serve                              # open http://localhost:8080
gridtiles export --layer pop -o site/pop.pmtiles   # static site for any host with range requests
```

## Documentation

- [Command line](docs/cli.md)
- [SQL reference](docs/sql.md)
- [Python reference](docs/python.md)
- [Query API](docs/api.md)

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
| `make db-reset` | Recreate the `gridtiles_test` database and apply `sql/*.sql` |
| `make test` | Unit and SQL tests |
| `make e2e` | End-to-end tests against the running services |
| `make check` | ruff, mypy, `test` and `e2e` |

Override the host ports with `GRIDTILES_DB_PORT` and `GRIDTILES_MARTIN_PORT`, and point tests elsewhere with `GRIDTILES_TEST_DSN` and `GRIDTILES_MARTIN_URL`.

The SQL in `src/gridtiles/sql/` installs the `gridtiles` schema (`gridtiles init`); check the installed version with:

```sql
select gridtiles.version();
```

## License

[MIT](LICENSE)
