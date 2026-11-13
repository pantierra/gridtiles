import random

import mercantile
import psycopg


def one(db: psycopg.Connection, sql: str, *params: object) -> object:
    row = db.execute(sql, params).fetchone()
    assert row is not None
    return row[0]


def test_quadkey_matches_mercantile(db: psycopg.Connection) -> None:
    rng = random.Random(1)
    tiles = [mercantile.Tile(rng.randrange(2**z), rng.randrange(2**z), z) for z in (rng.randint(0, 30) for _ in range(1000))]
    rows = db.execute(
        "select gridtiles.quadkey(z, x, y) from unnest(%s::int[], %s::int[], %s::int[]) with ordinality t(z, x, y, i)"
        " order by i",
        ([t.z for t in tiles], [t.x for t in tiles], [t.y for t in tiles]),
    ).fetchall()
    assert [r[0] for r in rows] == [mercantile.quadkey(t) for t in tiles]
    assert one(db, "select gridtiles.quadkey(0, 0, 0)") == ""


def test_point_square(db: psycopg.Connection) -> None:
    lon, lat, z = 13.405, 52.52, 16
    expected = tuple(mercantile.tile(lon, lat, z)[:2])
    point = "ST_SetSRID(ST_MakePoint(%s, %s), 4326)"
    for geom in (point, f"ST_Transform({point}, 3857)", f"ST_Transform({point}, 25833)"):
        assert db.execute(f"select * from gridtiles.point_square({geom}, %s)", (lon, lat, z)).fetchone() == expected
    assert db.execute("select * from gridtiles.point_square('SRID=4326;POINT EMPTY', 5)").fetchone() == (None, None)


def test_point_square_clamps(db: psycopg.Connection) -> None:
    for wkt, square in [("POINT(180 90)", (3, 0)), ("POINT(-200 -90)", (0, 3)), ("POINT(0 0)", (2, 2))]:
        assert db.execute(f"select * from gridtiles.point_square('SRID=4326;{wkt}', 2)").fetchone() == square


def test_square_centres_map_back(db: psycopg.Connection) -> None:
    rng = random.Random(3)
    zs = [rng.randint(0, 30) for _ in range(1000)]
    mismatches = one(
        db,
        "select count(*) from unnest(%s::int[], %s::int[], %s::int[]) t(z, x, y),"
        " gridtiles.point_square(ST_Centroid(ST_TileEnvelope(z, x, y)), z) s where (s.x, s.y) <> (t.x, t.y)",
        zs,
        [rng.randrange(2**z) for z in zs],
        [rng.randrange(2**z) for z in zs],
    )
    assert mismatches == 0
