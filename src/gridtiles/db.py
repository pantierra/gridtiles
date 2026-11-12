"""PostgreSQL access: schema install and the layer registry."""

from datetime import date
from importlib.resources import files

import psycopg
from psycopg import errors, sql
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
    conn.execute("select gridtiles.create_layer(%s)", (Jsonb(spec.model_dump(mode="json", exclude_none=True)),))


def layer_spec(conn: psycopg.Connection, name: str) -> LayerSpec:
    row = conn.execute("select spec from gridtiles.layers where name = %s", (name,)).fetchone()
    if row is None:
        raise LookupError(f"layer {name!r} does not exist")
    return LayerSpec.model_validate(row[0])


def layers(conn: psycopg.Connection, names: list[str] | None = None) -> list[Layer]:
    """Registered layers, all or the named ones."""
    rows = conn.execute(
        "select spec, stats, cells, bounds from gridtiles.layers"
        " where %(names)s::text[] is null or name = any(%(names)s) order by name",
        {"names": names},
    ).fetchall()
    specs = [LayerSpec.model_validate(r[0]) for r in rows]
    found = [
        Layer(spec, stats, cells, tuple(b) if b else None, _snapshots(conn, spec))
        for spec, (_, stats, cells, b) in zip(specs, rows)
    ]
    missing = set(names or []) - {layer.name for layer in found}
    if missing:
        raise LookupError(f"unknown layers: {', '.join(sorted(missing))}")
    return found


def _snapshots(conn: psycopg.Connection, spec: LayerSpec) -> tuple[str, ...]:
    if not spec.snapshots:
        return ()
    table = sql.Identifier("gridtiles", f"cells_{spec.name}")
    query = sql.SQL("select distinct snapshot from {} where z = %s order by 1").format(table)
    return tuple(d.isoformat() for (d,) in conn.execute(query, (spec.min_zoom,)).fetchall())


def parse_ref(ref: str) -> tuple[str, date | None]:
    """ "pop@2026-10-01" -> ("pop", date(2026, 10, 1)); "pop" -> ("pop", None)."""
    name, _, day = ref.partition("@")
    return name, date.fromisoformat(day) if day else None


def diff(conn: psycopg.Connection, a: str, b: str, name: str) -> None:
    """Create the read-only layer `name` holding B - A of two "layer[@YYYY-MM-DD]" references.

    Raises LookupError for an unknown layer and ValueError for layers that cannot be compared or a
    name that is taken."""
    (a_name, a_day), (b_name, b_day) = parse_ref(a), parse_ref(b)
    try:
        with conn.transaction():
            conn.execute("select gridtiles.diff(%s, %s, %s, %s, %s)", (a_name, a_day, b_name, b_day, name))
    except errors.NoDataFound as e:
        raise LookupError(f"unknown layer in {a} or {b}") from e
    except errors.UniqueViolation as e:
        raise ValueError(f"layer {name!r} already exists") from e
    except errors.InvalidParameterValue as e:
        raise ValueError(str(e.diag.message_primary)) from e
