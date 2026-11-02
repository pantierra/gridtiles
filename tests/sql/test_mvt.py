import random
import statistics
import time
from pathlib import Path

import mapbox_vector_tile
import mercantile
import psycopg
import pytest

from gridtiles import quadkey
from gridtiles.backends.postgres import PostgresBackend

from ..fixtures.make_fixtures import dense_block, point_wkb, random_points, write_geoparquet
from .test_ingest import SPEC, create

BLOCK = mercantile.tile(13.405, 52.52, 12)


@pytest.fixture
def block(db: psycopg.Connection, tmp_path: Path) -> psycopg.Connection:
    dense_block(tmp_path / "dense_block.parquet")
    create(db, SPEC)
    PostgresBackend(db).load("pop", tmp_path / "dense_block.parquet")
    return db


def tile(db: psycopg.Connection, z: int, x: int, y: int, layer: str = "pop") -> bytes:
    row = db.execute("select gridtiles.mvt(%s, %s, %s, %s)", (layer, z, x, y)).fetchone()
    assert row is not None
    return bytes(row[0])


def features(data: bytes) -> list[dict]:
    decoded = mapbox_vector_tile.decode(data, default_options={"y_coord_down": True})
    assert set(decoded) == {"cells"}
    assert decoded["cells"]["extent"] == 4096
    return decoded["cells"]["features"]


def bbox(feature: dict) -> tuple[int, int, int, int]:
    xs, ys = zip(*feature["geometry"]["coordinates"][0])
    return min(xs), min(ys), max(xs), max(ys)


def test_dense_block_tile(block: psycopg.Connection) -> None:
    feats = features(tile(block, 12, BLOCK.x, BLOCK.y))
    assert len(feats) == 256
    boxes = {bbox(f) for f in feats}
    assert all(x1 - x0 == 256 and y1 - y0 == 256 and x0 % 256 == 0 and y0 % 256 == 0 for x0, y0, x1, y1 in boxes)
    assert len(boxes) == 256  # 256 distinct squares of 256 x 256 tile the 4096 extent exactly
    assert sum((x1 - x0) * (y1 - y0) for x0, y0, x1, y1 in boxes) == 4096 * 4096


def test_feature_properties_match_rows(block: psycopg.Connection) -> None:
    rows = {
        r[0]: r[1:]
        for r in block.execute(
            "select gridtiles.qk_to_text(z, qk), z, n, population_sum, completeness_sum / completeness_w"
            " from gridtiles.cells_pop where z = 16"
        ).fetchall()
    }
    for f in features(tile(block, 12, BLOCK.x, BLOCK.y)):
        p = f["properties"]
        assert len(p["qk"]) == 16
        assert (p["z"], p["n"], p["population"], p["completeness"]) == pytest.approx(rows[p["qk"]])
        x0, y0, _, _ = bbox(f)
        cx, cy = quadkey.to_xy(16, quadkey.from_text(p["qk"]))
        assert (x0 // 256, y0 // 256) == (cx - BLOCK.x * 16, cy - BLOCK.y * 16)


def test_overzoom_returns_ancestor(block: psycopg.Connection) -> None:
    cell = mercantile.children(BLOCK, zoom=16)[0]
    t = mercantile.children(cell, zoom=18)[0]
    feats = features(tile(block, 18, t.x, t.y))
    assert len(feats) == 1
    assert bbox(feats[0]) == (0, 0, 4096, 4096)
    p = feats[0]["properties"]
    assert p["qk"] == mercantile.quadkey(cell)
    (pop,) = block.execute(
        "select population_sum from gridtiles.cells_pop where z = 16 and qk = %s", (quadkey.from_text(p["qk"]),)
    ).fetchone()
    assert p["population"] == pop


def test_empty_tile_is_zero_length(block: psycopg.Connection) -> None:
    assert tile(block, 12, 0, 0) == b""


def test_tile_below_range_clamps_to_min_zoom(db: psycopg.Connection, tmp_path: Path) -> None:
    dense_block(tmp_path / "dense_block.parquet")
    create(db, {**SPEC, "name": "coarse", "min_zoom": 8})
    PostgresBackend(db).load("coarse", tmp_path / "dense_block.parquet")
    t = mercantile.tile(13.405, 52.52, 2)
    feats = features(tile(db, 2, t.x, t.y, "coarse"))
    assert feats and {f["properties"]["z"] for f in feats} == {8}


def test_tile_function_per_layer(block: psycopg.Connection) -> None:
    (data,) = block.execute("select gridtiles.tile_pop(12, %s, %s)", (BLOCK.x, BLOCK.y)).fetchone()
    assert bytes(data) == tile(block, 12, BLOCK.x, BLOCK.y)


def test_query_uses_primary_key(block: psycopg.Connection) -> None:
    block.execute("set local enable_seqscan = off")  # the gate: an index path exists; plan choice on tiny tables aside
    (query,) = block.execute("select gridtiles._mvt_query('pop', 12, %s, %s)", (BLOCK.x, BLOCK.y)).fetchone()
    plan = "\n".join(r[0] for r in block.execute("explain " + query).fetchall())
    assert "cells_pop_pkey" in plan
    assert "Seq Scan" not in plan


@pytest.mark.perf
def test_perf_tiles(db: psycopg.Connection, tmp_path: Path) -> None:
    columns, points = random_points(1_000_000, seed=7)
    path = tmp_path / "points_1m.parquet"
    write_geoparquet(path, columns, [point_wkb(*p) for p in points if p is not None])
    create(db, SPEC)
    PostgresBackend(db).load("pop", path)
    rng = random.Random(5)
    timings = []
    for _ in range(500):
        z = rng.randint(10, 14)
        t = mercantile.tile(rng.uniform(13.08, 13.76), rng.uniform(52.33, 52.68), z)
        start = time.perf_counter()
        tile(db, z, t.x, t.y)
        timings.append(time.perf_counter() - start)
    p95 = statistics.quantiles(timings, n=20)[-1]
    print(f"p95 {p95 * 1000:.1f} ms over 500 tiles")
    assert p95 < 0.05
