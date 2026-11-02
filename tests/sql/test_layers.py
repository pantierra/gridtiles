import json

import psycopg
import pytest
from psycopg.types.json import Jsonb

from gridtiles.spec import LayerSpec

from ..conftest import FIXTURES

SPEC = json.loads((FIXTURES / "spec_pop.json").read_text())


def columns(db: psycopg.Connection, table: str) -> list[str]:
    rows = db.execute(
        "select column_name from information_schema.columns where table_schema = 'gridtiles' and table_name = %s"
        " order by ordinal_position",
        (table,),
    ).fetchall()
    return [r[0] for r in rows]


def create(db: psycopg.Connection, spec: dict) -> None:
    db.execute("select gridtiles.create_layer(%s)", (Jsonb(spec),))


def test_creates_cell_table(db: psycopg.Connection) -> None:
    create(db, SPEC)
    assert columns(db, "cells_pop") == ["z", "qk", "n", "population_sum", "completeness_sum", "completeness_w"]
    pkey = db.execute(
        "select pg_get_constraintdef(oid) from pg_constraint"
        " where conrelid = 'gridtiles.cells_pop'::regclass and contype = 'p'"
    ).fetchone()
    assert pkey == ("PRIMARY KEY (z, qk)",)
    assert db.execute("select zb, zmin, detail from gridtiles.layers where name = 'pop'").fetchone() == (16, 2, 4)


def test_spec_round_trips(db: psycopg.Connection) -> None:
    create(db, SPEC)
    (stored,) = db.execute("select spec from gridtiles.layers where name = 'pop'").fetchone()
    assert LayerSpec.model_validate(stored) == LayerSpec.model_validate(SPEC)


def test_duplicate_name_raises(db: psycopg.Connection) -> None:
    create(db, SPEC)
    with pytest.raises(psycopg.errors.UniqueViolation), db.transaction():
        create(db, SPEC)


@pytest.mark.parametrize("path", sorted((FIXTURES / "invalid_specs").glob("*.json")), ids=lambda p: p.stem)
def test_invalid_specs_rejected(db: psycopg.Connection, path) -> None:
    with pytest.raises(psycopg.errors.InvalidParameterValue), db.transaction():
        create(db, json.loads(path.read_text()))


def test_drop_layer_leaves_nothing(db: psycopg.Connection) -> None:
    def objects() -> set[str]:
        rows = db.execute(
            "select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace where n.nspname = 'gridtiles'"
            " union all select p.proname from pg_proc p join pg_namespace n on n.oid = p.pronamespace"
            " where n.nspname = 'gridtiles'"
        ).fetchall()
        return {r[0] for r in rows}

    before = objects()
    create(db, SPEC)
    db.execute("select gridtiles.drop_layer('pop')")
    assert objects() == before
    assert db.execute("select count(*) from gridtiles.layers").fetchone() == (0,)
