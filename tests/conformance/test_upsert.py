"""Upserts on every backend: any sequence of batches equals a full rebuild."""

import csv
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from gridtiles.backends.base import Backend
from gridtiles.spec import LayerSpec

from ..fixtures.make_fixtures import write_geoparquet
from .conftest import FIXTURES, rows
from .test_operations import SPEC

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


def halves(tmp_path: Path) -> tuple[Path, Path]:
    table = pq.read_table(FIXTURES / "points_10k.parquet")
    paths = (tmp_path / "a.parquet", tmp_path / "b.parquet")
    for path, part in zip(paths, (table.slice(0, 5000), table.slice(5000))):
        columns = {c: part[c].to_pylist() for c in ("pop", "osm_ratio", "area")}
        write_geoparquet(path, columns, part["geometry"].to_pylist())
    return paths


def test_append_equals_loading_at_once(backend: Backend, tmp_path: Path) -> None:
    a, b = halves(tmp_path)
    backend.create_layer(SPEC)
    backend.create_layer(SPEC.model_copy(update={"name": "both"}))
    backend.load("pop", a)
    report = backend.upsert("pop", b)
    assert report.records_loaded == len(pq.read_table(b).filter(pq.read_table(b)["geometry"].is_valid()))
    backend.load("both", FIXTURES / "points_10k.parquet")
    query = "select z, qk, n, population_sum, completeness_sum, completeness_w from gridtiles.cells_{} order by z, qk"
    appended, loaded = rows(backend, query.format("pop")), rows(backend, query.format("both"))
    assert [r[:3] for r in appended] == [r[:3] for r in loaded]
    for x, y in zip(appended, loaded):
        assert x[3:] == pytest.approx(y[3:], rel=1e-12)


points = st.tuples(st.floats(13.40, 13.42), st.floats(52.51, 52.53), st.integers(0, 100), st.floats(0, 1), st.floats(1, 10))
batches = st.lists(
    st.tuples(st.sampled_from(["add", "replace"]), st.lists(points, min_size=1, max_size=20)), min_size=1, max_size=20
)


@settings(max_examples=15, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(batches=batches)
def test_any_upserts_equal_a_rebuild(backend: Backend, tmp_path: Path, batches: list) -> None:
    backend.create_layer(SMALL)
    try:
        for i, (mode, batch) in enumerate(batches):
            path = tmp_path / f"batch{i}.csv"
            with open(path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["lon", "lat", "pop", "osm_ratio", "area"])
                writer.writerows(batch)
            backend.upsert("small", path, mode=mode)
        upserted = rows(backend, "select * from gridtiles.cells_small order by z, qk")
        backend.rollup("small")
        assert rows(backend, "select * from gridtiles.cells_small order by z, qk") == upserted
    finally:
        backend.drop_layer("small")


def test_replace_overwrites_touched_cells(backend: Backend) -> None:
    backend.create_layer(SPEC)
    backend.load("pop", FIXTURES / "points_small.csv")
    backend.upsert("pop", FIXTURES / "points_small.csv", mode="replace", refresh_stats=True)
    assert rows(backend, "select sum(n) from gridtiles.cells_pop where z = 16") == [(8,)]
    (layer,) = backend.list_layers(["pop"])
    assert layer.stats is not None and layer.stats["population"]["total"] == 360


def test_capabilities(backend: Backend) -> None:
    assert ("incremental" in backend.capabilities) == (type(backend).__name__ == "PostgresBackend")
