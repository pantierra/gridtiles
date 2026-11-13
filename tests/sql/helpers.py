"""Shared test helpers: source tables, layers over them, tiles."""

import json

import mapbox_vector_tile
import psycopg
from psycopg.types.json import Jsonb

from ..conftest import FIXTURES
from ..fixtures.sources import create_source

SPEC = json.loads((FIXTURES / "spec_pop.json").read_text())
SPEC_MINMAX = {
    **SPEC,
    "name": "minmax",
    "values": SPEC["values"]
    + [{"name": "pop_min", "from": "pop", "agg": "min"}, {"name": "pop_max", "from": "pop", "agg": "max"}],
}


def source(db: psycopg.Connection, name: str, table: str = "pop_points") -> None:
    create_source(db, name, table)


def create(db: psycopg.Connection, spec: dict, table: str | None = None) -> None:
    if table is not None:
        spec = {**spec, "source": {**spec["source"], "table": table}}
    db.execute("select gridtiles.create_layer(%s)", (Jsonb(spec),))


def tile(db: psycopg.Connection, z: int, x: int, y: int, layer: str = "pop", params: dict | None = None) -> bytes:
    row = db.execute("select gridtiles.mvt(%s, %s, %s, %s, %s)", (layer, z, x, y, Jsonb(params or {}))).fetchone()
    assert row is not None
    return bytes(row[0])


def features(data: bytes) -> list[dict]:
    if not data:
        return []
    decoded = mapbox_vector_tile.decode(data, default_options={"y_coord_down": True})
    assert set(decoded) == {"cells"}
    assert decoded["cells"]["extent"] == 4096
    return decoded["cells"]["features"]


def bbox(feature: dict) -> tuple[int, int, int, int]:
    xs, ys = zip(*feature["geometry"]["coordinates"][0])
    return min(xs), min(ys), max(xs), max(ys)
