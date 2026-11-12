# gridtiles

Turn any point, polygon or raster dataset into a quadkey grid and serve it as zoomable vector tiles.

> [!WARNING]
> This project is in early development. The API is not stable yet.

## Installation

```bash
uv add gridtiles
```

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
| `make check` | ruff, mypy and all tests, including end-to-end |

Override the host ports with `GRIDTILES_DB_PORT` and `GRIDTILES_MARTIN_PORT`, and point tests elsewhere with `GRIDTILES_TEST_DSN` and `GRIDTILES_MARTIN_URL`.

The SQL in `sql/` installs the `gridtiles` schema; check the installed version with:

```sql
select gridtiles.version();
```

## License

[MIT](LICENSE)
