import json
from pathlib import Path

import psycopg
import pytest
from psycopg.types.json import Jsonb

from gridtiles.db import install
from gridtiles.spec import LayerSpec

from ..conftest import FIXTURES, fresh_database
from .helpers import SPEC, create, source

LEGACY = (Path(__file__).parents[1] / "oracle" / "legacy.sql").read_text()


@pytest.fixture
def points(db: psycopg.Connection) -> psycopg.Connection:
    source(db, FIXTURES / "points_small.csv")
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


POINT_T = "create table t (geom geometry(Point, 4326), pop int)"
E = psycopg.errors


@pytest.mark.parametrize(
    ("ddl", "source", "error"),
    [
        (None, {"table": "missing"}, E.UndefinedTable),
        (None, {"table": "pop_points", "geometry": "shape"}, E.UndefinedColumn),
        ("create table t (geom text, pop int)", {"table": "t"}, E.DatatypeMismatch),
        ("create table t (geom geometry(LineString, 4326), pop int)", {"table": "t"}, E.FeatureNotSupported),
        ("create table t (geom geometry(Point), pop int)", {"table": "t"}, E.InvalidParameterValue),
        ("create table t (geom geometry, pop int)", {"table": "t", "srid": 4326}, E.InvalidParameterValue),
        (POINT_T, {"table": "t", "srid": 3857}, E.InvalidParameterValue),
        (POINT_T, {"table": "t", "kind": "polygon"}, E.InvalidParameterValue),
        ("create table t (geom geometry(Point, 4326), pop text)", {"table": "t"}, E.DatatypeMismatch),
        ("create table t (geom geometry(Point, 4326))", {"table": "t"}, E.UndefinedColumn),
        ("create table t (geom geometry(Point, 999999), pop int)", {"table": "t"}, E.InvalidParameterValue),
    ],
    ids=["no table", "no column", "not geometry", "lines", "no srid", "no kind", "srid differs", "kind differs",
         "text value", "no value column", "unknown srid"],
)  # fmt: skip
def test_bad_source_raises(points: psycopg.Connection, ddl: str | None, source: dict, error: type) -> None:
    if ddl:
        points.execute(ddl)
    spec = {**SPEC, "values": [{"name": "population", "from": "pop", "agg": "sum"}], "source": source}
    with pytest.raises(error), points.transaction():
        create(points, spec)


def test_untyped_column_with_kind_and_srid(points: psycopg.Connection) -> None:
    points.execute("create table t as select geom::geometry as geom, pop from pop_points")
    create(points, {**SPEC, "values": SPEC["values"][:1], "source": {"table": "t", "kind": "point", "srid": 4326}})
    assert points.execute("select srid, kind from gridtiles.layers").fetchone() == (4326, "point")


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


def test_missing_index_warns(points: psycopg.Connection) -> None:
    points.execute("create table t as select * from pop_points")
    notices: list[str] = []
    points.add_notice_handler(lambda d: notices.append(d.message_primary or ""))
    create(points, SPEC, "t")
    assert any("no GiST index" in n for n in notices)
    notices.clear()
    create(points, {**SPEC, "name": "indexed"})
    assert notices == []


def test_upgrade_from_materialized_schema(tmp_path: Path) -> None:
    """A database installed before live aggregation upgrades when it has no layers."""
    original = LEGACY.replace("gridtiles_oracle", "gridtiles")
    with psycopg.connect(fresh_database("gridtiles_upgrade"), autocommit=True) as conn:
        conn.execute("create extension postgis")
        conn.execute(original)
        conn.execute("select gridtiles.create_layer(%s)", (Jsonb(SPEC),))
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState, match="drop them"):
            install(conn)
        conn.execute("select gridtiles.drop_layer('pop')")
        install(conn)
        source(conn, FIXTURES / "points_small.csv")
        create(conn, SPEC)
        assert conn.execute("select count(*) from gridtiles.tiles_with_data('pop')").fetchone()[0] > 0
        assert conn.execute("select to_regproc('gridtiles.rollup')").fetchone() == (None,)
