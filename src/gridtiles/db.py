"""PostgreSQL access: schema install and the layer registry."""

from dataclasses import replace
from importlib.resources import files

import psycopg
from psycopg.types.json import Jsonb

from gridtiles.layer import Layer
from gridtiles.spec import LayerSpec


def install(conn: psycopg.Connection) -> str:
    """Apply the packaged `sql/*.sql` files in order (idempotent); returns the schema version."""
    for sql_file in sorted(files("gridtiles").joinpath("sql").iterdir(), key=lambda f: f.name):
        if sql_file.name.endswith(".sql"):
            conn.execute(sql_file.read_text())
    (version,) = conn.execute("select gridtiles.version()").fetchone() or ("",)
    return str(version)


def create_layer(conn: psycopg.Connection, spec: LayerSpec) -> None:
    """Register a layer over its source table; checks the source (see `gridtiles.create_layer`)."""
    conn.execute("select gridtiles.create_layer(%s)", (Jsonb(spec.model_dump(mode="json", exclude_none=True)),))


def layers(
    conn: psycopg.Connection, names: list[str] | None = None, *, stats: bool = False, params: dict[str, str] | None = None
) -> list[Layer]:
    """Registered layers, all or the named ones. With `stats`, each also gets the values of its
    time column and its stats and bounds (`gridtiles.stats`, which aggregates the whole source) for
    the tiles of `params`, by default the latest time of a layer with one.
    """
    rows = conn.execute(
        "select spec from gridtiles.layers where %(names)s::text[] is null or name = any(%(names)s) order by name",
        {"names": names},
    ).fetchall()
    found = [Layer(LayerSpec.model_validate(spec)) for (spec,) in rows]
    missing = set(names or []) - {layer.name for layer in found}
    if missing:
        raise LookupError(f"unknown layers: {', '.join(sorted(missing))}")
    return [_with_stats(conn, layer, params) for layer in found] if stats else found


def _with_stats(conn: psycopg.Connection, layer: Layer, params: dict[str, str] | None) -> Layer:
    (times,) = conn.execute("select gridtiles.times(%s)", (layer.name,)).fetchone() or (None,)
    if params is None:
        params = {"at": times[-1]} if times else {}
    (s,) = conn.execute("select gridtiles.stats(%s, %s)", (layer.name, Jsonb(params))).fetchone() or (None,)
    bounds = tuple(s["bounds"]) if s["bounds"] else None
    return replace(layer, stats=s["stats"], bounds=bounds, times=tuple(times) if times else None, params=params)
