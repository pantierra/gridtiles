"""PostgreSQL access: schema install and the layer registry."""

from dataclasses import replace
from datetime import datetime, timedelta
from importlib.resources import files

import psycopg
from psycopg.types.json import Jsonb

from gridtiles.layer import Layer
from gridtiles.spec import LayerSpec


def install(conn: psycopg.Connection) -> str:
    """Apply the packaged `sql/*.sql` files in order (idempotent) and record the schema version."""
    for sql_file in sorted(files("gridtiles").joinpath("sql").iterdir(), key=lambda f: f.name):
        if sql_file.name.endswith(".sql"):
            conn.execute(sql_file.read_text())
    row = conn.execute(
        "insert into gridtiles.meta values ('schema_version', gridtiles.version())"
        " on conflict (key) do update set value = excluded.value returning value"
    ).fetchone()
    assert row is not None
    return str(row[0])


def create_layer(conn: psycopg.Connection, spec: LayerSpec) -> None:
    """Register a layer over its source table; checks the source (see `gridtiles.create_layer`)."""
    conn.execute("select gridtiles.create_layer(%s)", (Jsonb(spec.model_dump(mode="json", exclude_none=True)),))


def drop_layer(conn: psycopg.Connection, name: str) -> None:
    """Unregister a layer; its source table is left alone."""
    conn.execute("select gridtiles.drop_layer(%s)", (name,))


def layer_spec(conn: psycopg.Connection, name: str) -> LayerSpec:
    row = conn.execute("select spec from gridtiles.layers where name = %s", (name,)).fetchone()
    if row is None:
        raise LookupError(f"layer {name!r} does not exist")
    return LayerSpec.model_validate(row[0])


def layers(
    conn: psycopg.Connection,
    names: list[str] | None = None,
    *,
    stats: bool = False,
    max_age: timedelta | None = None,
    params: dict[str, str] | None = None,
) -> list[Layer]:
    """Registered layers, all or the named ones.

    With `stats`, each layer also gets its stats and bounds (`gridtiles.cached_stats`, reused if
    computed less than `max_age` ago; None: any cached result) and the values of its time column.
    The stats describe the tiles of `params`, by default the latest time of a layer with one.
    """
    rows = conn.execute(
        "select spec from gridtiles.layers where %(names)s::text[] is null or name = any(%(names)s) order by name",
        {"names": names},
    ).fetchall()
    found = [Layer(LayerSpec.model_validate(spec)) for (spec,) in rows]
    missing = set(names or []) - {layer.name for layer in found}
    if missing:
        raise LookupError(f"unknown layers: {', '.join(sorted(missing))}")
    return [_with_stats(conn, layer, max_age, params) for layer in found] if stats else found


def _with_stats(conn: psycopg.Connection, layer: Layer, max_age: timedelta | None, params: dict[str, str] | None) -> Layer:
    row = conn.execute("select gridtiles.times(%s)", (layer.name,)).fetchone()
    times = tuple(row[0]) if row and row[0] is not None else None
    if params is None:
        params = {"at": times[-1]} if times else {}
    row = conn.execute("select gridtiles.cached_stats(%s, %s, %s)", (layer.name, Jsonb(params), max_age)).fetchone()
    assert row is not None
    s = row[0]
    return replace(
        layer,
        stats=s["stats"],
        bounds=tuple(s["bounds"]) if s["bounds"] else None,
        times=times,
        params=params,
        computed_at=datetime.fromisoformat(s["computed_at"]),
    )
