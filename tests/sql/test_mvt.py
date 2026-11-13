from pathlib import Path

import mercantile
import psycopg
import pytest

from .helpers import SPEC, bbox, create, features, source, tile

BLOCK = mercantile.tile(13.405, 52.52, 12)


@pytest.fixture
def block(db: psycopg.Connection, tmp_path: Path) -> psycopg.Connection:
    source(db, "dense_block")
    create(db, SPEC)
    return db


def test_dense_block_tile(block: psycopg.Connection) -> None:
    data = tile(block, 12, BLOCK.x, BLOCK.y)
    feats = features(data)
    assert len(feats) == 256
    boxes = {bbox(f) for f in feats}
    assert all(x1 - x0 == 256 and y1 - y0 == 256 and x0 % 256 == 0 and y0 % 256 == 0 for x0, y0, x1, y1 in boxes)
    assert len(boxes) == 256  # 256 distinct squares of 256 x 256 tile the 4096 extent exactly
    assert [f["properties"]["qk"] for f in feats] == sorted(f["properties"]["qk"] for f in feats)

    # The per-layer tile function (Martin's source) returns the same tile.
    (via_function,) = block.execute("select gridtiles.tile_pop(12, %s, %s, '{}')", (BLOCK.x, BLOCK.y)).fetchone()
    assert bytes(via_function) == data


def test_tile_query_uses_the_index(block: psycopg.Connection) -> None:
    # Seq scans are disabled, since tiny tables rightly prefer one; this proves the index path exists.
    block.execute("set local enable_seqscan = off")
    (query,) = block.execute("select gridtiles._mvt_query('pop', 12, %s, %s)", (BLOCK.x, BLOCK.y)).fetchone()
    plan = "\n".join(r[0] for r in block.execute("explain " + query).fetchall())
    assert "pop_points_geom_idx" in plan
    assert "Seq Scan" not in plan


def test_feature_positions_and_values(block: psycopg.Connection) -> None:
    for f in features(tile(block, 12, BLOCK.x, BLOCK.y)):
        p = f["properties"]
        assert (len(p["qk"]), p["z"], p["n"], p["completeness"]) == (16, 16, 1, 0.5)
        cx, cy, _ = mercantile.quadkey_to_tile(p["qk"])
        x0, y0, _, _ = bbox(f)
        assert (x0 // 256, y0 // 256) == (cx - BLOCK.x * 16, cy - BLOCK.y * 16)
        assert p["population"] == list(mercantile.children(BLOCK, zoom=16)).index(mercantile.quadkey_to_tile(p["qk"]))


def test_overzoom_returns_ancestor(block: psycopg.Connection) -> None:
    cell = mercantile.children(BLOCK, zoom=16)[5]
    t = mercantile.children(cell, zoom=18)[0]
    feats = features(tile(block, 18, t.x, t.y))
    assert len(feats) == 1
    assert bbox(feats[0]) == (0, 0, 4096, 4096)
    assert feats[0]["properties"]["qk"] == mercantile.quadkey(cell)
    assert feats[0]["properties"]["population"] == 5


def test_empty_tile_is_zero_length(block: psycopg.Connection) -> None:
    assert tile(block, 12, 0, 0) == b""


def test_tile_below_range_clamps_to_min_zoom(db: psycopg.Connection, tmp_path: Path) -> None:
    source(db, "dense_block")
    create(db, {**SPEC, "name": "coarse", "min_zoom": 8})
    t = mercantile.tile(13.405, 52.52, 2)
    feats = features(tile(db, 2, t.x, t.y, "coarse"))
    assert feats and {f["properties"]["z"] for f in feats} == {8}


def test_writes_show_on_the_next_request(block: psycopg.Connection) -> None:
    cell = mercantile.children(BLOCK, zoom=16)[0]
    w, s, e, n = mercantile.bounds(cell)
    block.execute(
        "insert into pop_points (geom, pop, osm_ratio, area) values (ST_SetSRID(ST_MakePoint(%s, %s), 4326), 100, 0.5, 1)",
        ((w + e) / 2, (s + n) / 2),
    )
    by_qk = {f["properties"]["qk"]: f["properties"] for f in features(tile(block, 12, BLOCK.x, BLOCK.y))}
    assert (by_qk[mercantile.quadkey(cell)]["n"], by_qk[mercantile.quadkey(cell)]["population"]) == (2, 100)
    block.execute("delete from pop_points")
    assert tile(block, 12, BLOCK.x, BLOCK.y) == b""


def test_source_columns_named_like_the_query(db: psycopg.Connection) -> None:
    """Source columns are aliased, so names the tile query uses internally are fine."""
    db.execute('create table t (geom geometry(Point, 4326), _g int, _x int, n int, _s1 int, "a b" int, geom2 int)')
    db.execute("create index on t using gist (geom)")
    db.execute("insert into t values ('SRID=4326;POINT(13.405 52.52)', 1, 2, 3, 4, 5, 6)")
    values = [{"name": f"v{i}", "from": c, "agg": "sum"} for i, c in enumerate(["_g", "_x", "n", "_s1", "a b", "geom2"])]
    create(db, {**SPEC, "source": {"table": "t"}, "values": values})
    (f,) = features(tile(db, 12, BLOCK.x, BLOCK.y))
    assert [f["properties"][f"v{i}"] for i in range(6)] == [1, 2, 3, 4, 5, 6]
    assert f["properties"]["n"] == 1


def test_null_and_empty_geometries_are_skipped(block: psycopg.Connection) -> None:
    before = tile(block, 12, BLOCK.x, BLOCK.y)
    block.execute("insert into pop_points (geom, pop) values (null, 1), ('SRID=4326;POINT EMPTY', 1)")
    assert tile(block, 12, BLOCK.x, BLOCK.y) == before
    (stats,) = block.execute("select gridtiles.stats('pop')").fetchone()
    assert stats["stats"]["population"]["total"] == sum(range(256))


def test_bad_address_raises(block: psycopg.Connection) -> None:
    with pytest.raises(psycopg.errors.NumericValueOutOfRange):
        tile(block, 12, 4096, 0)


def test_unknown_layer_raises(db: psycopg.Connection) -> None:
    with pytest.raises(psycopg.errors.NoDataFound):
        tile(db, 0, 0, 0, "nope")


def test_dropped_source_raises(block: psycopg.Connection) -> None:
    block.execute("drop table pop_points")
    with pytest.raises(psycopg.errors.UndefinedTable), block.transaction():
        tile(block, 12, BLOCK.x, BLOCK.y)
    with pytest.raises(psycopg.errors.UndefinedTable):  # only parameter errors become empty tiles
        block.execute("select gridtiles.tile_pop(12, %s, %s, '{}')", (BLOCK.x, BLOCK.y))


def test_recreated_source_is_used(block: psycopg.Connection) -> None:
    block.execute("alter table pop_points rename to old")
    block.execute("create table pop_points as select * from old where pop < 10")
    assert len(features(tile(block, 12, BLOCK.x, BLOCK.y))) == 10


def test_unknown_parameters_are_ignored(block: psycopg.Connection) -> None:
    assert tile(block, 12, BLOCK.x, BLOCK.y, params={"key": "x", "v": "2"}) == tile(block, 12, BLOCK.x, BLOCK.y)


def test_malformed_parameters(block: psycopg.Connection) -> None:
    with pytest.raises(psycopg.errors.InvalidParameterValue), block.transaction():
        block.execute("select gridtiles.mvt('pop', 12, %s, %s, '[1]')", (BLOCK.x, BLOCK.y))
    notices: list[str] = []
    block.add_notice_handler(lambda d: notices.append(d.message_primary or ""))
    (data,) = block.execute("select gridtiles.tile_pop(12, %s, %s, '[1]')", (BLOCK.x, BLOCK.y)).fetchone()
    assert bytes(data) == b""
    assert notices == ["tile_pop: tile parameters must be a JSON object, got [1]"]
    (data,) = block.execute("select gridtiles.tile_pop(12, %s, %s, null)", (BLOCK.x, BLOCK.y)).fetchone()
    assert bytes(data) == tile(block, 12, BLOCK.x, BLOCK.y)


def test_cache(db: psycopg.Connection) -> None:
    source(db, "points_10k")
    create(db, SPEC)
    tiles = [(z, *mercantile.tile(13.4, 52.5, z)[:2]) for z in range(9)]
    live = [tile(db, *t) for t in tiles]
    assert db.execute("select gridtiles.refresh_cache('pop', 8)").fetchone()[0] > 0
    assert [tile(db, *t) for t in tiles] == live

    # Tiles at cached levels (c = z + 4 <= 8) lag the source until the next refresh.
    db.execute("delete from pop_points where pop > 50")
    assert [tile(db, *t) == old for t, old in zip(tiles, live)] == [True] * 5 + [False] * 4
    # Exports (cached => false), stats and tiles_with_data read the source.
    (fresh,) = db.execute("select gridtiles.mvt('pop', 0, 0, 0, '{}', cached => false)").fetchone()
    assert bytes(fresh) != live[0]
    (stats,) = db.execute("select gridtiles.stats('pop')").fetchone()
    (total,) = db.execute("select sum(pop) from pop_points where geom is not null").fetchone()
    assert stats["stats"]["population"]["total"] == total
    db.execute("select gridtiles.refresh_cache('pop', 8)")
    assert tile(db, *tiles[0]) != live[0]

    with pytest.raises(psycopg.errors.InvalidParameterValue, match="max_zoom 1 is below min_zoom 2"), db.transaction():
        db.execute("select gridtiles.refresh_cache('pop', 1)")
    db.execute("select gridtiles.refresh_cache('pop', null)")
    assert db.execute("select to_regclass('gridtiles.cache_pop'), cache_zoom from gridtiles.layers").fetchone() == (
        None,
        None,
    )


def test_cache_is_bypassed_with_parameters(db: psycopg.Connection) -> None:
    source(db, "points_small")
    db.execute("alter table pop_points add column day date default '2024-01-01'")
    create(db, {**SPEC, "source": {"table": "pop_points", "time": "day"}})
    db.execute("select gridtiles.refresh_cache('pop', 16)")
    db.execute("delete from pop_points")
    t = mercantile.tile(13.405, 52.52, 12)
    assert tile(db, 12, t.x, t.y) != b""
    assert tile(db, 12, t.x, t.y, params={"at": "2024-01-01"}) == b""
    db.execute("select gridtiles.drop_layer('pop')")
    assert db.execute("select to_regclass('gridtiles.cache_pop')").fetchone() == (None,)


def test_polygon_counts_once_per_square(db: psycopg.Connection) -> None:
    """A polygon over one z15 square: four z16 squares with a quarter each, or one z15 square
    with n = 1, not the sum of its children's counts.
    """
    db.execute("create table polys (geom geometry(MultiPolygon, 3857), pop int, osm_ratio float8, area float8)")
    db.execute("create index on polys using gist (geom)")
    x, y, _ = mercantile.tile(13.405, 52.52, 15)
    db.execute("insert into polys select ST_Multi(ST_TileEnvelope(15, %s, %s)), 400, 0.5, 1", (x, y))
    create(db, SPEC, "polys")
    base = features(tile(db, 12, x >> 3, y >> 3))
    assert sorted((f["properties"]["n"], f["properties"]["population"]) for f in base) == [(1, pytest.approx(100))] * 4
    (coarse,) = features(tile(db, 11, x >> 4, y >> 4))
    assert (coarse["properties"]["z"], coarse["properties"]["n"]) == (15, 1)
    assert coarse["properties"]["population"] == pytest.approx(400)
    assert coarse["properties"]["completeness"] == pytest.approx(0.5)
