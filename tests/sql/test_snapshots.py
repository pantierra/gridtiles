"""Snapshot layers and diffs."""

import math
from datetime import date

import psycopg
import pytest

from gridtiles import db as registry
from gridtiles.export import _tiles_with_data
from gridtiles.ingest import load, upsert
from gridtiles.spec import LayerSpec

from ..conftest import FIXTURES

SPEC = LayerSpec.model_validate_json((FIXTURES / "spec_pop.json").read_text())
OCT, NOV = date(2026, 10, 1), date(2026, 11, 1)
V1, V2 = FIXTURES / "points_10k.parquet", FIXTURES / "points_10k_v2.parquet"


def tile(db: psycopg.Connection, layer: str, z: int, x: int, y: int, snapshot: date | None = None) -> bytes:
    row = db.execute("select gridtiles.mvt(%s, %s, %s, %s, %s)", (layer, z, x, y, snapshot)).fetchone()
    assert row is not None
    return bytes(row[0])


def tiles(db: psycopg.Connection, layer: str) -> set[tuple[int, int, int]]:
    (info,) = registry.layers(db, [layer])
    return set(_tiles_with_data(db, info))


@pytest.fixture
def layers(db: psycopg.Connection) -> psycopg.Connection:
    registry.create_layer(db, SPEC.model_copy(update={"name": "ts", "snapshots": True}))
    load(db, "ts", V1, snapshot=OCT)
    load(db, "ts", V2, snapshot=NOV)
    for name, path in (("v1", V1), ("v2", V2)):
        registry.create_layer(db, SPEC.model_copy(update={"name": name}))
        load(db, name, path)
    return db


def test_snapshot_tiles_equal_separate_layers(layers: psycopg.Connection) -> None:
    db = layers
    (ts,) = registry.layers(db, ["ts"])
    assert ts.snapshots == ("2026-10-01", "2026-11-01")
    assert tiles(db, "ts") == tiles(db, "v2")  # the latest snapshot
    for z, x, y in tiles(db, "v1") | tiles(db, "v2"):
        assert tile(db, "ts", z, x, y, OCT) == tile(db, "v1", z, x, y)
        assert tile(db, "ts", z, x, y, NOV) == tile(db, "v2", z, x, y) == tile(db, "ts", z, x, y)
    z, x, y = min(tiles(db, "v1"))
    (at,) = db.execute('select gridtiles.tile_ts_at(%s, %s, %s, \'{"snapshot": "2026-10-01"}\')', (z, x, y)).fetchone()
    assert bytes(at) == tile(db, "v1", z, x, y)


def test_snapshot_rules(layers: psycopg.Connection) -> None:
    db = layers
    with pytest.raises(ValueError, match="name one"):
        load(db, "ts", V1)
    with pytest.raises(ValueError, match="has no snapshots"):
        load(db, "v1", V1, snapshot=OCT)
    with pytest.raises(ValueError, match="load a new snapshot"):
        upsert(db, "ts", V1)


def test_diff_matches_full_outer_join(layers: psycopg.Connection) -> None:
    db = layers
    registry.diff(db, "ts@2026-10-01", "ts@2026-11-01", "delta")
    (delta,) = registry.layers(db, ["delta"])
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
    mismatched = "select count(*) from gridtiles.cells_delta where (completeness_sum is null) <> (completeness_w is null)"
    assert db.execute(mismatched).fetchone() == (0,)  # a mean's weight exists exactly where its difference does
    with pytest.raises(ValueError, match="read-only"):
        load(db, "delta", V1)


def test_diff_of_layers(layers: psycopg.Connection) -> None:
    db = layers
    registry.diff(db, "v1", "v2", "plain")
    total = "select sum(n) from gridtiles.cells_{} where z = 16"
    ((plain,),), ((v1,),), ((v2,),) = (db.execute(total.format(t)).fetchall() for t in ("plain", "v1", "v2"))
    assert plain == v2 - v1
    with pytest.raises(LookupError):
        registry.diff(db, "v1", "missing", "nope")


def test_parse_ref() -> None:
    assert registry.parse_ref("pop") == ("pop", None)
    assert registry.parse_ref("pop@2026-10-01") == ("pop", OCT)
