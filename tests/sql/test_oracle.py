"""Live tiles against the oracle: the materialized load + rollup path they replace.

tests/oracle/legacy.sql installs that path in the schema gridtiles_oracle. For each dataset, both
must publish the same tiles with the same squares, quadkeys, zooms, counts, minima and maxima.
Sums and means differ in the last bits below the base zoom (the rollup adds children level by
level, a live tile adds a square's rows in one pass), so they are compared to 1e-12.
"""

import math
from pathlib import Path

import psycopg
import pytest
from psycopg.types.json import Jsonb

from ..conftest import FIXTURES
from ..fixtures.make_fixtures import dense_block
from .helpers import SPEC, SPEC_MINMAX, create, features, source, tile

LEGACY = (Path(__file__).parents[1] / "oracle" / "legacy.sql").read_text()
REL = 1e-12


@pytest.fixture
def db(db: psycopg.Connection) -> psycopg.Connection:
    db.execute(LEGACY)
    return db


def oracle(db: psycopg.Connection, spec: dict, table: str) -> None:
    """Load `table` into the oracle layer of the same name and roll it up."""
    db.execute("select gridtiles_oracle.create_layer(%s)", (Jsonb(spec),))
    columns = sorted({c for v in spec["values"] if v["agg"] != "count" for c in (v["from"], v.get("weight")) if c})
    casts = "".join(f", {c}::double precision as {c}" for c in columns)
    db.execute(f"create temp table _oracle_staging as select geom{casts} from {table} order by ctid")
    db.execute("select gridtiles_oracle.ingest_points(%s, '_oracle_staging')", (spec["name"],))
    db.execute("drop table _oracle_staging")
    db.execute("select gridtiles_oracle.rollup(%s)", (spec["name"],))


def oracle_tiles(db: psycopg.Connection, spec: dict) -> set[tuple[int, int, int]]:
    """Tiles with data, as the materialized export found them: ancestors of the cells at c(t)."""
    detail = spec.get("detail", 4)
    tiles = set()
    for t in range(max(0, spec["min_zoom"] - detail), max(0, spec["base_zoom"] - detail) + 1):
        c = min(max(t + detail, spec["min_zoom"]), spec["base_zoom"])
        rows = db.execute(
            f"select xy.x, xy.y from (select distinct qk >> %(shift)s as tile from gridtiles_oracle.cells_{spec['name']}"
            " where z = %(c)s) d, gridtiles_oracle.qk_to_xy(%(t)s, tile) xy",
            {"shift": 2 * (c - t), "c": c, "t": t},
        ).fetchall()
        tiles |= {(t, x, y) for x, y in rows}
    return tiles


def assert_same_tile(live: bytes, expected: bytes) -> None:
    got = {f["properties"]["qk"]: f for f in features(live)}
    want = {f["properties"]["qk"]: f for f in features(expected)}
    assert got.keys() == want.keys()
    for qk, f in want.items():
        g = got[qk]
        assert g["geometry"] == f["geometry"]
        assert g["properties"].keys() == f["properties"].keys()
        for key, value in f["properties"].items():
            if isinstance(value, float):
                assert g["properties"][key] == pytest.approx(value, rel=REL), (qk, key)
            else:
                assert g["properties"][key] == value, (qk, key)


def assert_matches_oracle(db: psycopg.Connection, spec: dict) -> int:
    name = spec["name"]
    tiles = oracle_tiles(db, spec)
    assert set(db.execute("select z, x, y from gridtiles.tiles_with_data(%s)", (name,)).fetchall()) == tiles
    for z, x, y in sorted(tiles):
        (expected,) = db.execute("select gridtiles_oracle.mvt(%s, %s, %s, %s)", (name, z, x, y)).fetchone()
        assert_same_tile(tile(db, z, x, y, name), bytes(expected))

    (live,) = db.execute("select gridtiles.stats(%s)", (name,)).fetchone()
    stats, bounds = db.execute("select stats, bounds from gridtiles_oracle.layers where name = %s", (name,)).fetchone()
    assert live["bounds"] == bounds
    assert live["stats"].keys() == stats.keys()
    for value, s in stats.items():
        for key, v in s.items():
            assert live["stats"][value][key] == (pytest.approx(v, rel=REL) if v is not None else None), (value, key)
    return len(tiles)


@pytest.mark.parametrize("mode", ["ordered", "numeric", "plain"])
def test_points_10k(db: psycopg.Connection, mode: str) -> None:
    source(db, FIXTURES / "points_10k.parquet")
    create(db, SPEC_MINMAX, "pop_points")
    oracle(db, SPEC_MINMAX, "pop_points")
    db.execute("select set_config('gridtiles.sum_mode', %s, true)", (mode,))
    assert assert_matches_oracle(db, SPEC_MINMAX) > 100


def test_points_small(db: psycopg.Connection) -> None:
    source(db, FIXTURES / "points_small.csv")
    create(db, SPEC)
    oracle(db, SPEC, "pop_points")
    assert_matches_oracle(db, SPEC)


def test_dense_block(db: psycopg.Connection, tmp_path: Path) -> None:
    dense_block(tmp_path / "dense_block.parquet")
    source(db, tmp_path / "dense_block.parquet")
    create(db, SPEC)
    oracle(db, SPEC, "pop_points")
    assert_matches_oracle(db, SPEC)


@pytest.mark.parametrize("srid", [3857, 25833])
def test_other_srids(db: psycopg.Connection, srid: int) -> None:
    """EPSG:3857 is read as is; any other SRID through its transform to 4326."""
    source(db, FIXTURES / "points_10k.parquet", "wgs84")
    db.execute(
        f"create table pop_points as select ST_Transform(geom, {srid})::geometry(Point, {srid}) as geom,"
        " pop, osm_ratio, area from wgs84 order by ctid"
    )
    index = "geom" if srid == 3857 else "ST_Transform(geom, 4326)"
    db.execute(f"create index on pop_points using gist ({index})")
    create(db, SPEC)
    oracle(db, SPEC, "pop_points")
    assert_matches_oracle(db, SPEC)


def test_edge_points(db: psycopg.Connection) -> None:
    """Points on square boundaries, at the world edges and beyond the Web Mercator square land in
    the same squares as before: the index filter's box is widened, the assignment is exact.
    """
    db.execute("create table pop_points (geom geometry(Point, 4326), pop int, osm_ratio float8, area float8)")
    db.execute("create index on pop_points using gist (geom)")
    corners = [
        (lon, lat)
        for z, x, y in [(16, 35205, 21495), (12, 2200, 1343), (5, 17, 10), (1, 1, 0)]
        for lon in (x / 2**z * 360 - 180, (x + 1) / 2**z * 360 - 180)
        for lat in (math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / 2**z)))),)
    ]
    extremes = [(-180, 0), (180, 0), (0, 85.0511287798), (0, -85.0511287798), (0, 89.9), (0, -90), (-200, 10), (200, -10)]
    for i, (lon, lat) in enumerate(corners + extremes):
        db.execute(
            "insert into pop_points values (ST_SetSRID(ST_MakePoint(%s, %s), 4326), %s, 0.5, 1)", (float(lon), float(lat), i)
        )
    # Also the corners as PostGIS projects them, which may differ from the formula in the last bit.
    db.execute(
        "insert into pop_points select ST_Transform(ST_SetSRID(ST_MakePoint(ST_XMin(e), ST_YMax(e)), 3857), 4326), 1, 0.5, 1"
        " from (values (16, 35205, 21495), (12, 2200, 1343), (5, 17, 10)) t(z, x, y), ST_TileEnvelope(z, x, y) e"
    )
    spec = {**SPEC, "min_zoom": 0}
    create(db, spec)
    oracle(db, spec, "pop_points")
    assert_matches_oracle(db, spec)
