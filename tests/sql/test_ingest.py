import json
import math
import struct
import time
from pathlib import Path

import psycopg
import pyarrow.parquet as pq
import pytest
from psycopg.types.json import Jsonb

from gridtiles import quadkey
from gridtiles.backends.postgres import PostgresBackend
from gridtiles.sources import UnsupportedGeometry

from ..conftest import FIXTURES
from ..fixtures.make_fixtures import point_wkb, random_points, write_geoparquet

SPEC = json.loads((FIXTURES / "spec_pop.json").read_text())
SPEC_MINMAX = {
    **SPEC,
    "name": "minmax",
    "values": SPEC["values"]
    + [{"name": "pop_min", "from": "pop", "agg": "min"}, {"name": "pop_max", "from": "pop", "agg": "max"}],
}


def create(db: psycopg.Connection, spec: dict) -> None:
    db.execute("select gridtiles.create_layer(%s)", (Jsonb(spec),))


def cells(db: psycopg.Connection, layer: str) -> dict[tuple[int, int], tuple]:
    rows = db.execute(f"select * from gridtiles.cells_{layer} order by z, qk").fetchall()
    return {(r[0], r[1]): r[2:] for r in rows}


def source_points() -> list[tuple[float, float] | None]:
    wkb = pq.read_table(FIXTURES / "points_10k.parquet", columns=["geometry"])["geometry"].to_pylist()
    return [None if g is None else struct.unpack("<dd", g[5:21]) for g in wkb]


def test_points_small(db: psycopg.Connection) -> None:
    create(db, SPEC)
    report = PostgresBackend(db).load("pop", FIXTURES / "points_small.csv")
    assert (report.records_read, report.records_loaded, report.skipped_null_geometry, report.cells_written) == (8, 8, 0, 3)
    expected = json.loads((FIXTURES / "points_small.expected.json").read_text())
    rows = db.execute(
        "select gridtiles.qk_to_text(z, qk), n, population_sum, completeness_sum, completeness_w,"
        " completeness_sum / completeness_w from gridtiles.cells_pop where z = 16"
    ).fetchall()
    assert {r[0] for r in rows} == set(expected)
    for text, n, pop, c_sum, c_w, mean in rows:
        e = expected[text]
        assert n == e["n"]
        assert pop == pytest.approx(e["population_sum"], rel=1e-12)
        assert (c_sum, c_w) == pytest.approx((e["completeness_sum"], e["completeness_w"]), rel=1e-12)
        assert mean == pytest.approx(e["completeness"], rel=1e-12)


def test_points_10k_totals_and_cells(db: psycopg.Connection) -> None:
    create(db, SPEC)
    report = PostgresBackend(db).load("pop", FIXTURES / "points_10k.parquet")
    points = source_points()
    table = pq.read_table(FIXTURES / "points_10k.parquet")
    loaded = [i for i, p in enumerate(points) if p is not None]
    assert report.records_read == 10_000
    assert report.records_loaded == len(loaded)
    assert report.skipped_null_geometry == 10_000 - len(loaded)

    n, pop = db.execute("select sum(n), sum(population_sum) from gridtiles.cells_pop where z = 16").fetchone()
    assert n == len(loaded)
    pops = table["pop"].to_pylist()
    assert pop == pytest.approx(math.fsum(pops[i] for i in loaded), rel=1e-9)

    oracle: dict[int, int] = {}
    for i in loaded:
        qk = quadkey.from_lonlat(*points[i], 16)
        oracle[qk] = oracle.get(qk, 0) + 1
    assert {qk: v[0] for (z, qk), v in cells(db, "pop").items() if z == 16} == oracle


def test_loading_twice_adds_up(db: psycopg.Connection) -> None:
    create(db, SPEC_MINMAX)
    PostgresBackend(db).load("minmax", FIXTURES / "points_10k.parquet")
    once = cells(db, "minmax")
    PostgresBackend(db).load("minmax", FIXTURES / "points_10k.parquet")
    twice = cells(db, "minmax")
    assert once.keys() == twice.keys()
    for key, (n, pop, c_sum, c_w, pmin, pmax) in once.items():
        assert twice[key] == pytest.approx((2 * n, 2 * pop, 2 * c_sum, 2 * c_w, pmin, pmax), rel=1e-12)


def test_epsg3857_gives_identical_cells(db: psycopg.Connection, tmp_path: Path) -> None:
    table = pq.read_table(FIXTURES / "points_10k.parquet")
    mercator = [
        None
        if p is None
        else point_wkb(
            p[0] * quadkey.HALF_WORLD / 180,
            math.log(math.tan(math.pi / 4 + math.radians(p[1]) / 2)) * quadkey.HALF_WORLD / math.pi,
        )
        for p in source_points()
    ]
    path = tmp_path / "points_3857.parquet"
    write_geoparquet(path, {c: table[c].to_pylist() for c in ("pop", "osm_ratio", "area")}, mercator, epsg=3857)

    create(db, SPEC)
    create(db, {**SPEC, "name": "pop3857"})
    PostgresBackend(db).load("pop", FIXTURES / "points_10k.parquet")
    PostgresBackend(db).load("pop3857", path)
    expected, actual = cells(db, "pop"), cells(db, "pop3857")
    assert actual.keys() == expected.keys()
    for key, values in expected.items():
        assert actual[key] == pytest.approx(values, rel=1e-12)


def test_polygon_raises(db: psycopg.Connection, tmp_path: Path) -> None:
    path = tmp_path / "polygons.geojson"
    feature = {"type": "Feature", "properties": {"pop": 1, "osm_ratio": 0.5, "area": 1}}
    point = {**feature, "geometry": {"type": "Point", "coordinates": [13.4, 52.5]}}
    polygon = {
        **feature,
        "geometry": {"type": "Polygon", "coordinates": [[[13.4, 52.5], [13.5, 52.5], [13.5, 52.6], [13.4, 52.5]]]},
    }
    path.write_text(json.dumps({"type": "FeatureCollection", "features": [point, polygon]}))
    create(db, SPEC)
    with pytest.raises(UnsupportedGeometry, match="row 1"):
        PostgresBackend(db).load("pop", path)


@pytest.mark.perf
def test_perf_1m_points(db: psycopg.Connection, tmp_path: Path) -> None:
    columns, points = random_points(1_000_000, seed=7)
    path = tmp_path / "points_1m.parquet"
    write_geoparquet(path, columns, [point_wkb(*p) for p in points if p is not None])
    create(db, SPEC)
    start = time.perf_counter()
    report = PostgresBackend(db).load("pop", path)
    elapsed = time.perf_counter() - start
    print(f"loaded {report.records_loaded} points in {elapsed:.1f} s")
    assert elapsed < 60
