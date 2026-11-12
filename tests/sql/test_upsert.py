"""Upserts: any sequence of batches equals a full rebuild, only touched rows change, and
concurrent upserts serialize."""

import csv
import threading
from pathlib import Path

import psycopg
import pyarrow.parquet as pq
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from gridtiles import db as registry
from gridtiles.ingest import load, upsert
from gridtiles.spec import LayerSpec

from ..conftest import FIXTURES, fresh_database
from ..fixtures.make_fixtures import write_geoparquet

SPEC = LayerSpec.model_validate_json((FIXTURES / "spec_pop.json").read_text())
SMALL = LayerSpec.model_validate(
    {
        "name": "small",
        "base_zoom": 16,
        "min_zoom": 10,
        "values": [
            {"name": "pop", "from": "pop", "agg": "sum"},
            {"name": "low", "from": "pop", "agg": "min"},
            {"name": "ratio", "from": "osm_ratio", "agg": "mean", "weight": "area"},
        ],
    }
)
CELLS = "select z, qk, n, population_sum, completeness_sum, completeness_w from gridtiles.cells_{} order by z, qk"


def halves(tmp_path: Path) -> tuple[Path, Path]:
    table = pq.read_table(FIXTURES / "points_10k.parquet")
    paths = (tmp_path / "a.parquet", tmp_path / "b.parquet")
    for path, part in zip(paths, (table.slice(0, 5000), table.slice(5000))):
        columns = {c: part[c].to_pylist() for c in ("pop", "osm_ratio", "area")}
        write_geoparquet(path, columns, part["geometry"].to_pylist())
    return paths


def same_cells(a: list[tuple], b: list[tuple]) -> None:
    assert [r[:3] for r in a] == [r[:3] for r in b]
    for x, y in zip(a, b):
        assert x[3:] == pytest.approx(y[3:], rel=1e-12)


def test_append_equals_loading_at_once(db: psycopg.Connection, tmp_path: Path) -> None:
    a, b = halves(tmp_path)
    registry.create_layer(db, SPEC)
    registry.create_layer(db, SPEC.model_copy(update={"name": "both"}))
    load(db, "pop", a)
    report = upsert(db, "pop", b)
    assert report.records_loaded == len(pq.read_table(b).filter(pq.read_table(b)["geometry"].is_valid()))
    load(db, "both", FIXTURES / "points_10k.parquet")
    same_cells(db.execute(CELLS.format("pop")).fetchall(), db.execute(CELLS.format("both")).fetchall())


points = st.tuples(st.floats(13.40, 13.42), st.floats(52.51, 52.53), st.integers(0, 100), st.floats(0, 1), st.floats(1, 10))
batches = st.lists(
    st.tuples(st.sampled_from(["add", "replace"]), st.lists(points, min_size=1, max_size=20)), min_size=1, max_size=20
)


@settings(max_examples=15, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(batches=batches)
def test_any_upserts_equal_a_rebuild(db: psycopg.Connection, tmp_path: Path, batches: list) -> None:
    registry.create_layer(db, SMALL)
    try:
        for i, (mode, batch) in enumerate(batches):
            path = tmp_path / f"batch{i}.csv"
            with open(path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["lon", "lat", "pop", "osm_ratio", "area"])
                writer.writerows(batch)
            upsert(db, "small", path, mode=mode)
        upserted = db.execute("select * from gridtiles.cells_small order by z, qk").fetchall()
        db.execute("select gridtiles.rollup('small')")
        assert db.execute("select * from gridtiles.cells_small order by z, qk").fetchall() == upserted
    finally:
        db.execute("select gridtiles.drop_layer('small')")


def test_replace_overwrites_touched_cells(db: psycopg.Connection) -> None:
    registry.create_layer(db, SPEC)
    load(db, "pop", FIXTURES / "points_small.csv")
    upsert(db, "pop", FIXTURES / "points_small.csv", mode="replace", refresh_stats=True)
    assert db.execute("select sum(n) from gridtiles.cells_pop where z = 16").fetchone() == (8,)
    (layer,) = registry.layers(db, ["pop"])
    assert layer.stats is not None and layer.stats["population"]["total"] == 360


def test_untouched_rows_keep_their_version(db: psycopg.Connection) -> None:
    registry.create_layer(db, SPEC)
    load(db, "pop", FIXTURES / "points_10k.parquet")
    before = dict(db.execute("select (z, qk)::text, xmin::text from gridtiles.cells_pop").fetchall())
    upsert(db, "pop", FIXTURES / "points_small.csv")
    after = dict(db.execute("select (z, qk)::text, xmin::text from gridtiles.cells_pop").fetchall())
    changed = {k for k in before if after.get(k) != before[k]}
    assert 0 < len(changed) <= 3 * (16 - 2 + 1)  # 3 touched base cells and at most their ancestors
    assert len(before) - len(changed) > 0.9 * len(before)


def test_unknown_layer_raises(db: psycopg.Connection) -> None:
    with pytest.raises(LookupError):
        upsert(db, "missing", FIXTURES / "points_small.csv")


def test_invalid_mode_raises(db: psycopg.Connection) -> None:
    registry.create_layer(db, SPEC)
    with pytest.raises(psycopg.errors.InvalidParameterValue), db.transaction():
        db.execute("create temp table d (like gridtiles.cells_pop including all)")
        db.execute("select gridtiles.apply_delta('pop', 'd', 'merge')")


def test_concurrent_appends_serialize(tmp_path: Path) -> None:
    a, b = halves(tmp_path)
    urls = [fresh_database("gridtiles_concurrent"), fresh_database("gridtiles_sequential")]
    for url in urls:
        with psycopg.connect(url) as conn:
            registry.install(conn)
            registry.create_layer(conn, SPEC)

    def append(path: Path) -> None:
        with psycopg.connect(urls[0]) as conn:
            upsert(conn, "pop", path)

    threads = [threading.Thread(target=append, args=(p,)) for p in (a, b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with psycopg.connect(urls[1]) as conn:
        upsert(conn, "pop", a)
        upsert(conn, "pop", b)
    cells = []
    for url in urls:
        with psycopg.connect(url) as conn:
            cells.append(conn.execute(CELLS.format("pop")).fetchall())
    same_cells(*cells)
