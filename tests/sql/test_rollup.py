import random

import psycopg
import pytest

from gridtiles.backends.postgres import PostgresBackend

from ..conftest import FIXTURES
from .test_ingest import SPEC_MINMAX, create

VALUE_COLUMNS = ["population_sum", "completeness_sum", "completeness_w", "pop_min_min", "pop_max_max"]
ZB, ZMIN = SPEC_MINMAX["base_zoom"], SPEC_MINMAX["min_zoom"]


@pytest.fixture
def layer(db: psycopg.Connection) -> psycopg.Connection:
    create(db, SPEC_MINMAX)
    PostgresBackend(db).load("minmax", FIXTURES / "points_10k.parquet")
    return db


def checksum(db: psycopg.Connection) -> str:
    row = db.execute("select md5(string_agg(c::text, ',' order by z, qk)) from gridtiles.cells_minmax c").fetchone()
    assert row is not None
    return row[0]


def test_every_level_conserves_totals(layer: psycopg.Connection) -> None:
    rows = layer.execute(
        "select z, sum(n), sum(population_sum), sum(completeness_sum), sum(completeness_w)"
        " from gridtiles.cells_minmax group by z order by z"
    ).fetchall()
    assert [r[0] for r in rows] == list(range(ZMIN, ZB + 1))
    base = rows[-1][1:]
    for row in rows:
        assert row[1] == base[0]
        assert row[2:] == pytest.approx(base[1:], rel=1e-12)


def test_parents_equal_brute_force(layer: psycopg.Connection) -> None:
    rng = random.Random(4)
    parents = layer.execute(f"select z, qk from gridtiles.cells_minmax where z < {ZB}").fetchall()
    for z, qk in rng.sample(parents, 200):
        stored = layer.execute(
            f"select n, {', '.join(VALUE_COLUMNS)} from gridtiles.cells_minmax where z = %s and qk = %s", (z, qk)
        ).fetchone()
        brute = layer.execute(
            "select sum(n)::bigint, sum(population_sum), sum(completeness_sum), sum(completeness_w), min(pop_min_min),"
            " max(pop_max_max) from gridtiles.cells_minmax where z = %s and qk <@ gridtiles.qk_child_range(%s, %s, %s)",
            (ZB, z, qk, ZB),
        ).fetchone()
        assert stored == pytest.approx(brute, rel=1e-12)


def test_rollup_is_idempotent(layer: psycopg.Connection) -> None:
    before = checksum(layer)
    layer.execute("select gridtiles.rollup('minmax')")
    assert checksum(layer) == before


def test_level_shapes(layer: psycopg.Connection) -> None:
    counts = dict(layer.execute("select z, count(*) from gridtiles.cells_minmax group by z").fetchall())
    assert min(counts) == ZMIN
    for z in range(ZMIN + 1, ZB + 1):
        assert counts[z - 1] >= counts[z] / 4


def test_stats(layer: psycopg.Connection) -> None:
    (stats,) = layer.execute("select stats from gridtiles.layers where name = 'minmax'").fetchone()
    assert set(stats) == {"population", "completeness", "pop_min", "pop_max"}
    for value in stats.values():
        breaks = value["breaks"]
        assert len(breaks) == 7
        assert breaks == sorted(breaks)
        assert value["min"] <= breaks[0] and breaks[-1] <= value["max"]
    (total,) = layer.execute(f"select sum(population_sum) from gridtiles.cells_minmax where z = {ZB}").fetchone()
    assert stats["population"]["total"] == pytest.approx(total)


def test_bounds_cover_the_data(layer: psycopg.Connection) -> None:
    ((west, south, east, north),) = layer.execute("select bounds from gridtiles.layers where name = 'minmax'").fetchone()
    assert 13.07 < west < 13.09 and 52.32 < south < 52.34 and 13.75 < east < 13.77 and 52.67 < north < 52.69
