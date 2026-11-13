import random

import mercantile
import psycopg
import pytest
from psycopg import errors


def random_cells(n: int, seed: int) -> tuple[list[int], list[int], list[int]]:
    """n random cells as (zs, xs, ys) columns, ready for unnest."""
    rng = random.Random(seed)
    zs = [rng.randint(0, 30) for _ in range(n)]
    return zs, [rng.randrange(2**z) for z in zs], [rng.randrange(2**z) for z in zs]


def one(db: psycopg.Connection, sql: str, *params: object) -> object:
    row = db.execute(sql, params).fetchone()
    assert row is not None
    return row[0]


def test_reference_vector(db: psycopg.Connection) -> None:
    assert one(db, "select gridtiles.qk_from_xyz(3, 3, 5)") == 39
    assert one(db, "select gridtiles.qk_to_text(3, 39)") == "213"
    assert db.execute("select * from gridtiles.qk_to_xy(3, 39)").fetchone() == (3, 5)


def test_zoom_extremes(db: psycopg.Connection) -> None:
    assert one(db, "select gridtiles.qk_from_xyz(0, 0, 0)") == 0
    assert one(db, "select gridtiles.qk_to_text(0, 0)") == ""
    assert one(db, "select gridtiles.qk_from_xyz(30, %s, %s)", 2**30 - 1, 2**30 - 1) == 4**30 - 1


def test_matches_mercantile(db: psycopg.Connection) -> None:
    """Quadkey strings and tile indices against an independent implementation (mercantile)."""
    rows = db.execute(
        "select z, x, y, gridtiles.qk_to_text(z, qk), (gridtiles.qk_to_xy(z, qk)).*"
        " from unnest(%s::int[], %s::int[], %s::int[]) as t(z, x, y), gridtiles.qk_from_xyz(z, x, y) qk",
        random_cells(1000, seed=1),
    ).fetchall()
    assert len(rows) == 1000
    for z, x, y, text, rx, ry in rows:
        assert text == mercantile.quadkey(mercantile.Tile(x, y, z))
        assert (rx, ry) == (x, y)


def test_envelope_matches_tile_envelope(db: psycopg.Connection) -> None:
    mismatches = one(
        db,
        """
        select count(*) from unnest(%s::int[], %s::int[], %s::int[]) as t(z, x, y)
        where not ST_Equals(gridtiles.qk_envelope(z, gridtiles.qk_from_xyz(z, x, y)), ST_TileEnvelope(z, x, y))
        """,
        *random_cells(1000, seed=2),
    )
    assert mismatches == 0


def test_envelope_centroid_maps_back(db: psycopg.Connection) -> None:
    mismatches = one(
        db,
        """
        select count(*) from (
            select z, gridtiles.qk_from_xyz(z, x, y) as qk from unnest(%s::int[], %s::int[], %s::int[]) as t(z, x, y)
        ) s
        where gridtiles.qk_from_point(ST_Centroid(gridtiles.qk_envelope(z, qk)), z) <> qk
        """,
        *random_cells(1000, seed=3),
    )
    assert mismatches == 0


def test_from_point_any_srid(db: psycopg.Connection) -> None:
    lon, lat, z = 13.405, 52.52, 16
    tile = mercantile.tile(lon, lat, z)
    (expected,) = db.execute("select gridtiles.qk_from_xyz(%s, %s, %s)", (z, tile.x, tile.y)).fetchone()
    point = "ST_SetSRID(ST_MakePoint(%s, %s), 4326)"
    assert one(db, f"select gridtiles.qk_from_point({point}, %s)", lon, lat, z) == expected
    assert one(db, f"select gridtiles.qk_from_point(ST_Transform({point}, 3857), %s)", lon, lat, z) == expected
    assert one(db, "select gridtiles.qk_from_point('SRID=4326;POINT EMPTY'::geometry, 5)") is None


def test_from_point_clamps(db: psycopg.Connection) -> None:
    assert db.execute(
        "select * from gridtiles.qk_to_xy(2, gridtiles.qk_from_point('SRID=4326;POINT(180 90)'::geometry, 2))"
    ).fetchone() == (3, 0)


@pytest.mark.parametrize(
    ("sql", "error"),
    [
        ("select gridtiles.qk_from_xyz(3, 8, 0)", errors.NumericValueOutOfRange),
        ("select gridtiles.qk_from_xyz(3, 0, -1)", errors.NumericValueOutOfRange),
        ("select gridtiles.qk_from_xyz(31, 0, 0)", errors.NumericValueOutOfRange),
        ("select gridtiles.qk_from_xyz(-1, 0, 0)", errors.NumericValueOutOfRange),
        ("select gridtiles.qk_to_xy(2, 16)", errors.NumericValueOutOfRange),
        ("select gridtiles.qk_to_text(31, 0)", errors.NumericValueOutOfRange),
        ("select gridtiles.qk_envelope(1, 4)", errors.NumericValueOutOfRange),
        ("select gridtiles.qk_from_point('SRID=4326;POINT(0 0)'::geometry, 31)", errors.NumericValueOutOfRange),
    ],
)
def test_invalid_input_raises(db: psycopg.Connection, sql: str, error: type[Exception]) -> None:
    with pytest.raises(error), db.transaction():
        db.execute(sql)
