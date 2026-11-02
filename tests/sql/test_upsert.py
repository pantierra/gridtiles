"""PostgreSQL specifics of upserts: only touched rows change, and concurrent upserts serialize."""

import threading
from pathlib import Path

import psycopg
import pytest

from gridtiles.backends.postgres import PostgresBackend
from gridtiles.spec import LayerSpec

from ..conformance.test_upsert import halves
from ..conftest import FIXTURES, fresh_database

SPEC = LayerSpec.model_validate_json((FIXTURES / "spec_pop.json").read_text())


def test_untouched_rows_keep_their_version(db: psycopg.Connection, tmp_path: Path) -> None:
    backend = PostgresBackend(db)
    backend.create_layer(SPEC)
    backend.load("pop", FIXTURES / "points_10k.parquet")
    before = dict(db.execute("select (z, qk)::text, xmin::text from gridtiles.cells_pop").fetchall())
    backend.upsert("pop", FIXTURES / "points_small.csv")
    after = dict(db.execute("select (z, qk)::text, xmin::text from gridtiles.cells_pop").fetchall())
    changed = {k for k in before if after.get(k) != before[k]}
    assert 0 < len(changed) <= 3 * (16 - 2 + 1)  # 3 touched base cells and at most their ancestors
    assert len(before) - len(changed) > 0.9 * len(before)


def test_invalid_mode_raises(db: psycopg.Connection) -> None:
    PostgresBackend(db).create_layer(SPEC)
    with pytest.raises(psycopg.errors.InvalidParameterValue), db.transaction():
        db.execute("create temp table d (like gridtiles.cells_pop including all)")
        db.execute("select gridtiles.apply_delta('pop', 'd', 'merge')")


def test_concurrent_appends_serialize(tmp_path: Path) -> None:
    a, b = halves(tmp_path)

    def cells(url: str) -> list[tuple]:
        with psycopg.connect(url) as conn:
            return conn.execute(
                "select z, qk, n, population_sum, completeness_sum from gridtiles.cells_pop order by z, qk"
            ).fetchall()

    urls = [fresh_database("gridtiles_concurrent"), fresh_database("gridtiles_sequential")]
    for url in urls:
        with PostgresBackend(url) as backend:
            backend.install()
            backend.create_layer(SPEC)

    def append(path: Path) -> None:
        with PostgresBackend(urls[0]) as backend:
            backend.upsert("pop", path)

    threads = [threading.Thread(target=append, args=(p,)) for p in (a, b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with PostgresBackend(urls[1]) as backend:
        backend.upsert("pop", a)
        backend.upsert("pop", b)
    concurrent, sequential = cells(urls[0]), cells(urls[1])
    assert [r[:3] for r in concurrent] == [r[:3] for r in sequential]
    for x, y in zip(concurrent, sequential):
        assert x[3:] == pytest.approx(y[3:], rel=1e-12)
