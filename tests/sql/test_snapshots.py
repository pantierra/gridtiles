"""Snapshot layers and diffs (PostgreSQL)."""

import math
from datetime import date

import psycopg
import pytest

from gridtiles.backends.postgres import PostgresBackend
from gridtiles.spec import LayerSpec

from ..conftest import FIXTURES

SPEC = LayerSpec.model_validate_json((FIXTURES / "spec_pop.json").read_text())
OCT, NOV = date(2026, 10, 1), date(2026, 11, 1)
V1, V2 = FIXTURES / "points_10k.parquet", FIXTURES / "points_10k_v2.parquet"


@pytest.fixture
def layers(db: psycopg.Connection) -> PostgresBackend:
    backend = PostgresBackend(db)
    backend.create_layer(SPEC.model_copy(update={"name": "ts", "snapshots": True}))
    backend.load("ts", V1, snapshot=OCT)
    backend.load("ts", V2, snapshot=NOV)
    for name, path in (("v1", V1), ("v2", V2)):
        backend.create_layer(SPEC.model_copy(update={"name": name}))
        backend.load(name, path)
    return backend


def test_snapshot_tiles_equal_separate_layers(layers: PostgresBackend, db: psycopg.Connection) -> None:
    (ts,) = layers.list_layers(["ts"])
    assert ts.snapshots == ("2026-10-01", "2026-11-01")
    tiles = set(layers.tiles_with_data("v1")) | set(layers.tiles_with_data("v2"))
    assert set(layers.tiles_with_data("ts")) == set(layers.tiles_with_data("v2"))
    for z, x, y in tiles:
        assert layers.tile("ts", z, x, y, OCT) == layers.tile("v1", z, x, y)
        assert layers.tile("ts", z, x, y, NOV) == layers.tile("v2", z, x, y) == layers.tile("ts", z, x, y)
    z, x, y = min(tiles)
    (at,) = db.execute('select gridtiles.tile_ts_at(%s, %s, %s, \'{"snapshot": "2026-10-01"}\')', (z, x, y)).fetchone()
    assert bytes(at) == layers.tile("v1", z, x, y)


def test_snapshot_rules(layers: PostgresBackend) -> None:
    with pytest.raises(ValueError, match="name one"):
        layers.load("ts", V1)
    with pytest.raises(ValueError, match="has no snapshots"):
        layers.load("v1", V1, snapshot=OCT)
    with pytest.raises(ValueError, match="load a new snapshot"):
        layers.upsert("ts", V1)


def test_diff_matches_full_outer_join(layers: PostgresBackend, db: psycopg.Connection) -> None:
    layers.diff("ts@2026-10-01", "ts@2026-11-01", "delta")
    (delta,) = layers.list_layers(["delta"])
    assert delta.spec.diff == ("ts@2026-10-01", "ts@2026-11-01") and delta.stats is not None
    for z in (16, 13, 2):

        def side(day: date, z: int = z) -> dict[int, tuple]:
            rows = db.execute(
                "select qk, n, population_sum, completeness_sum / nullif(completeness_w, 0)"
                " from gridtiles.cells_ts where z = %s and snapshot = %s",
                (z, day),
            ).fetchall()
            return {qk: rest for qk, *rest in rows}

        a, b = side(OCT), side(NOV)
        got = {
            qk: rest
            for qk, *rest in db.execute(
                "select qk, n, population_sum, completeness_sum / completeness_w from gridtiles.cells_delta where z = %s",
                (z,),
            ).fetchall()
        }
        assert got.keys() == a.keys() | b.keys()
        for qk, (n, pop, mean) in got.items():
            (an, ap, am), (bn, bp, bm) = a.get(qk, (0, 0.0, None)), b.get(qk, (0, 0.0, None))
            assert n == bn - an
            assert pop == pytest.approx(bp - ap, rel=1e-12, abs=1e-9)
            expected = None if am is None or bm is None else bm - am
            assert (mean is None and expected is None) or math.isclose(mean, expected, rel_tol=1e-12, abs_tol=1e-12)
    with pytest.raises(ValueError, match="read-only"):
        layers.load("delta", V1)


def test_diff_of_layers(layers: PostgresBackend, db: psycopg.Connection) -> None:
    layers.diff("v1", "v2", "plain")
    total = "select sum(n) from gridtiles.cells_{} where z = 16"
    ((plain,),), ((v1,),), ((v2,),) = (db.execute(total.format(t)).fetchall() for t in ("plain", "v1", "v2"))
    assert plain == v2 - v1
    with pytest.raises(LookupError):
        layers.diff("v1", "missing", "nope")
