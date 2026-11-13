import json
from pathlib import Path

import psycopg
import pytest

from gridtiles.spec import LayerSpec

from ..conftest import FIXTURES
from .helpers import SPEC, create, source


@pytest.fixture
def points(db: psycopg.Connection) -> psycopg.Connection:
    source(db, "points_small")
    return db


def test_registers_source(points: psycopg.Connection) -> None:
    create(points, SPEC)
    row = points.execute("select source, geometry, srid, kind, zb, zmin, detail from gridtiles.layers").fetchone()
    assert row == ("public.pop_points", "geom", 4326, "point", 16, 2, 4)


def test_spec_round_trips(points: psycopg.Connection) -> None:
    create(points, SPEC)
    (stored,) = points.execute("select spec from gridtiles.layers where name = 'pop'").fetchone()
    assert LayerSpec.model_validate(stored) == LayerSpec.model_validate(SPEC)


def test_creates_only_the_tile_function(points: psycopg.Connection) -> None:
    def objects() -> set[str]:
        rows = points.execute(
            "select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace where n.nspname = 'gridtiles'"
            " union all select p.proname from pg_proc p join pg_namespace n on n.oid = p.pronamespace"
            " where n.nspname = 'gridtiles'"
        ).fetchall()
        return {r[0] for r in rows}

    before = objects()
    create(points, SPEC)
    assert objects() - before == {"tile_pop"}
    points.execute("select gridtiles.drop_layer('pop')")
    assert objects() == before
    assert points.execute("select count(*) from pop_points").fetchone() == (8,)  # the source stays


def test_duplicate_name_raises(points: psycopg.Connection) -> None:
    create(points, SPEC)
    with pytest.raises(psycopg.errors.UniqueViolation), points.transaction():
        create(points, SPEC)


def test_drop_missing_layer_raises(db: psycopg.Connection) -> None:
    with pytest.raises(psycopg.errors.UndefinedObject):
        db.execute("select gridtiles.drop_layer('nope')")


@pytest.mark.parametrize("path", sorted((FIXTURES / "invalid_specs").glob("*.json")), ids=lambda p: p.stem)
def test_invalid_specs_rejected(points: psycopg.Connection, path: Path) -> None:
    with pytest.raises(psycopg.errors.InvalidParameterValue), points.transaction():
        create(points, json.loads(path.read_text()))


E = psycopg.errors


@pytest.mark.parametrize(
    ("ddl", "source", "error"),
    [
        (None, {"table": "missing"}, E.UndefinedTable),
        (None, {"table": "pop_points", "geometry": "shape"}, E.UndefinedColumn),
        ("create table t (geom text, pop int)", {"table": "t"}, E.DatatypeMismatch),
        ("create table t (geom geometry(LineString, 4326), pop int)", {"table": "t"}, E.DatatypeMismatch),
        ("create table t (geom geometry(Point), pop int)", {"table": "t"}, E.DatatypeMismatch),
        ("create table t (geom geometry, pop int)", {"table": "t"}, E.DatatypeMismatch),
        ("create table t (geom geometry(Point, 4326), pop text)", {"table": "t"}, E.DatatypeMismatch),
        ("create table t (geom geometry(Point, 4326))", {"table": "t"}, E.UndefinedColumn),
    ],
    ids=["no table", "no column", "not geometry", "lines", "no srid", "untyped", "text value", "no value column"],
)
def test_bad_source_raises(points: psycopg.Connection, ddl: str | None, source: dict, error: type) -> None:
    if ddl:
        points.execute(ddl)
    spec = {**SPEC, "values": [{"name": "population", "from": "pop", "agg": "sum"}], "source": source}
    with pytest.raises(error), points.transaction():
        create(points, spec)


def test_view_as_source(points: psycopg.Connection) -> None:
    points.execute("create view big as select * from pop_points where pop >= 40")
    create(points, SPEC, "big")
    (stats,) = points.execute("select gridtiles.stats('pop')").fetchone()
    assert stats["stats"]["population"]["total"] == 40 + 50 + 60 + 70 + 80


def test_schema_qualified_and_quoted_names(points: psycopg.Connection) -> None:
    points.execute('create schema "My Data"')
    points.execute('create table "My Data"."Points" as select geom as "the geom", pop as "Pop" from pop_points')
    points.execute('create index on "My Data"."Points" using gist ("the geom")')
    spec = {**SPEC, "values": [{"name": "population", "from": "Pop", "agg": "sum"}]}
    create(points, {**spec, "source": {"table": '"My Data"."Points"', "geometry": "the geom"}})
    assert points.execute("select source from gridtiles.layers").fetchone() == ('"My Data"."Points"',)
    (stats,) = points.execute("select gridtiles.stats('pop')").fetchone()
    assert stats["stats"]["population"]["total"] == 360
