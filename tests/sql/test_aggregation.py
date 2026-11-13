"""What a tile contains, checked against brute-force queries over the source.

For a sample of published tiles, every feature must match a direct aggregation of the source
rows in its square: points by `point_square` (tests/point_square.sql, a PL/pgSQL reference of
the tile query's arithmetic), compared bit for bit; polygons by their covered area share (`ST_Intersection` with the
square, where tiles clip by box), compared to 1e-9.
"""

import math
import random
from pathlib import Path

import psycopg
import pytest

from .helpers import SPEC_MINMAX, create, features, source, tile

SPEC = {**SPEC_MINMAX, "min_zoom": 0}  # population (sum), completeness (mean by area), pop_min, pop_max
TILES_PER_ZOOM = {"point": 4, "polygon": 2}

# One row per square at zoom c inside tile z/x/y, from the points the square holds.
POINTS = """
select gridtiles.quadkey(%(c)s, (sq).x, (sq).y), count(*),
    sum(pop::float8 order by pop::float8),
    sum(osm_ratio::float8 * area::float8 order by osm_ratio::float8 * area::float8)
        / nullif(sum(case when osm_ratio is not null then area::float8 end
                     order by case when osm_ratio is not null then area::float8 end), 0),
    min(pop::float8), max(pop::float8)
from (select gridtiles.point_square(geom, %(c)s) as sq, * from pop_points where not ST_IsEmpty(geom)) s
where (sq).x >> (%(c)s - %(z)s) = %(x)s and (sq).y >> (%(c)s - %(z)s) = %(y)s
group by sq
"""

# One row per square at zoom c inside tile z/x/y, from the polygons overlapping it: sums by covered
# fraction, means weighted by covered area, n the polygons touching it.
POLYGONS = """
select gridtiles.quadkey(%(c)s, sx, sy), count(*), sum(pop * (a / total)),
    sum(ratio * w * a) / nullif(sum(case when ratio is not null then w * a end), 0), min(pop), max(pop)
from (
    select p.*, sx, sy, ST_Area(ST_Intersection(g, ST_TileEnvelope(%(c)s, sx, sy))) as a
    from (select g, ST_Area(g) as total, pop::float8 as pop, osm_ratio::float8 as ratio, area::float8 as w
          from (select gridtiles._mercator_polygon(geom) as g, * from pop_points offset 0) s offset 0) p,
        generate_series(%(x)s::int << (%(c)s - %(z)s), ((%(x)s::int + 1) << (%(c)s - %(z)s)) - 1) sx,
        generate_series(%(y)s::int << (%(c)s - %(z)s), ((%(y)s::int + 1) << (%(c)s - %(z)s)) - 1) sy
    where total > 0 and g && ST_TileEnvelope(%(c)s, sx, sy)
) h
where a > 0
group by sx, sy
"""
PROPERTIES = ("n", "population", "completeness", "pop_min", "pop_max")


def expected(db: psycopg.Connection, query: str, z: int, x: int, y: int, c: int) -> dict[str, dict]:
    rows = db.execute(query, {"z": z, "x": x, "y": y, "c": c}).fetchall()
    return {qk: {k: v for k, v in zip(PROPERTIES, values) if v is not None} for qk, *values in rows}


def assert_tiles(db: psycopg.Connection, spec: dict, kind: str = "point") -> None:
    """Every feature of a sample of tiles per published zoom equals the brute-force aggregation."""
    detail, rng = spec.get("detail", 4), random.Random(3)
    published = db.execute("select z, x, y from gridtiles.tiles_with_data(%s)", (spec["name"],)).fetchall()
    assert published
    for z in sorted({t[0] for t in published}):
        at_zoom = [t for t in published if t[0] == z]
        c = min(max(z + detail, spec["min_zoom"]), spec["base_zoom"])
        for _, x, y in rng.sample(at_zoom, min(TILES_PER_ZOOM[kind], len(at_zoom))):
            got = {f["properties"].pop("qk"): f["properties"] for f in features(tile(db, z, x, y, spec["name"]))}
            assert all(p.pop("z") == c for p in got.values())
            want = expected(db, POINTS if kind == "point" else POLYGONS, z, x, y, c)
            assert got.keys() == want.keys(), (z, x, y)
            if kind == "point":
                assert got == want, (z, x, y)
            else:
                for qk, props in want.items():
                    assert got[qk] == pytest.approx(props, rel=1e-9), (z, x, y, qk)


def assert_point_layer(db: psycopg.Connection, spec: dict = SPEC) -> None:
    """Tiles, the published tile set and stats against the source."""
    assert_tiles(db, spec)
    zooms = range(max(0, spec["min_zoom"] - spec["detail"]), spec["base_zoom"] - spec["detail"] + 1)
    expected_tiles = {
        (t, x, y)
        for t in zooms
        for x, y in db.execute(
            "select distinct (gridtiles.point_square(geom, %(t)s)).* from pop_points where not ST_IsEmpty(geom)",
            {"t": t},
        ).fetchall()
    }
    assert set(db.execute("select z, x, y from gridtiles.tiles_with_data(%s)", (spec["name"],)).fetchall()) == expected_tiles
    (stats,) = db.execute("select gridtiles.stats(%s)", (spec["name"],)).fetchone()
    (total,) = db.execute("select sum(pop) from pop_points where not ST_IsEmpty(geom)").fetchone()
    assert stats["stats"]["population"]["total"] == total


def test_points_10k(db: psycopg.Connection) -> None:
    source(db, "points_10k")  # 2% null geometries
    create(db, SPEC)
    assert_point_layer(db)


def test_points_small(db: psycopg.Connection) -> None:
    source(db, "points_small")  # one point without osm_ratio
    create(db, SPEC)
    assert_point_layer(db)


def test_dense_block(db: psycopg.Connection, tmp_path: Path) -> None:
    source(db, "dense_block")
    create(db, SPEC)
    assert_point_layer(db)


@pytest.mark.parametrize("srid", [3857, 25833])
def test_points_other_srids(db: psycopg.Connection, srid: int) -> None:
    """EPSG:3857 is read as is; any other SRID through its transform to 4326."""
    source(db, "points_10k", "wgs84")
    db.execute(
        f"create table pop_points as select ST_Transform(geom, {srid})::geometry(Point, {srid}) as geom,"
        " pop, osm_ratio, area from wgs84"
    )
    db.execute(f"create index on pop_points using gist ({'geom' if srid == 3857 else 'ST_Transform(geom, 4326)'})")
    create(db, {**SPEC, "min_zoom": 8})
    assert_point_layer(db, {**SPEC, "min_zoom": 8})


def test_edge_points(db: psycopg.Connection) -> None:
    """Points on square corners, at the world edges and beyond the Web Mercator square: the index
    filter's box is widened and reaches out at the edges, the assignment is exact.
    """
    db.execute("create table pop_points (geom geometry(Point, 4326), pop int, osm_ratio float8, area float8)")
    db.execute("create index on pop_points using gist (geom)")
    corners = [
        (lon, math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / 2**z)))))
        for z, x, y in [(16, 35205, 21495), (12, 2200, 1343), (5, 17, 10), (1, 1, 0)]
        for lon in (x / 2**z * 360 - 180, (x + 1) / 2**z * 360 - 180)
    ]
    extremes = [(-180, 0), (180, 0), (0, 85.0511287798), (0, -85.0511287798), (0, 89.9), (0, -90), (-200, 10), (200, -10)]
    for i, (lon, lat) in enumerate(corners + extremes):
        db.execute("insert into pop_points values (ST_SetSRID(ST_MakePoint(%s, %s), 4326), %s, 0.5, 1)", (lon, lat, i))
    # The corners as PostGIS projects them too, which may differ from the formula in the last bit.
    db.execute(
        "insert into pop_points select ST_Transform(ST_SetSRID(ST_MakePoint(ST_XMin(e), ST_YMax(e)), 3857), 4326), 1, 0.5, 1"
        " from (values (16, 35205, 21495), (12, 2200, 1343), (5, 17, 10)) t(z, x, y), ST_TileEnvelope(z, x, y) e"
    )
    create(db, SPEC)
    assert_point_layer(db)


def polygons_table(db: psycopg.Connection, srid: int = 4326) -> None:
    db.execute(f"create table pop_points (geom geometry(MultiPolygon, {srid}), pop bigint, osm_ratio float8, area float8)")
    db.execute(f"create index on pop_points using gist ({'geom' if srid in (3857, 4326) else 'ST_Transform(geom, 4326)'})")


def test_polygons_500(db: psycopg.Connection) -> None:
    source(db, "polygons_500")
    create(db, SPEC)
    assert_tiles(db, SPEC, "polygon")


@pytest.mark.parametrize("srid", [3857, 25833])
def test_polygons_other_srids(db: psycopg.Connection, srid: int) -> None:
    source(db, "polygons_500", "wgs84")
    polygons_table(db, srid)
    db.execute(f"insert into pop_points select ST_Transform(geom, {srid}), pop, osm_ratio, area from wgs84")
    create(db, {**SPEC, "min_zoom": 8})
    assert_tiles(db, {**SPEC, "min_zoom": 8}, "polygon")


def test_polygon_shapes(db: psycopg.Connection) -> None:
    """Whole and half squares, a large polygon over many tiles, a self-intersecting bowtie, and
    polygons beyond the Mercator limit and reaching the pole, whose whole value stays in the grid.
    """
    polygons_table(db)
    x, y = db.execute("select * from gridtiles.point_square('SRID=4326;POINT(13.405 52.52)', 16)").fetchone()
    for z, sx, sy, fraction in [(16, x, y, 1), (16, x + 1, y, 0.5), (15, x >> 1, y >> 1, 1), (9, x >> 7, y >> 7, 0.7)]:
        db.execute(
            "insert into pop_points select ST_Multi(ST_Transform(ST_MakeEnvelope(ST_XMin(e), ST_YMin(e),"
            " ST_XMin(e) + (ST_XMax(e) - ST_XMin(e)) * %s, ST_YMax(e), 3857), 4326)), 400, 0.5, 1"
            " from ST_TileEnvelope(%s, %s, %s) e",
            (fraction, z, sx, sy),
        )
    for wkt in [
        "POLYGON((13.40 52.51, 13.41 52.52, 13.41 52.51, 13.40 52.52, 13.40 52.51))",
        "POLYGON((10 84, 10.01 84, 10.01 87, 10 87, 10 84))",
        "POLYGON((10 -90, 10.01 -90, 10.01 -84, 10 -84, 10 -90))",
        "POLYGON((179.99 0, 180 0, 180 0.01, 179.99 0.01, 179.99 0))",
    ]:
        db.execute("insert into pop_points values (ST_Multi(ST_GeomFromText(%s, 4326)), 100, 0.25, 2)", (wkt,))
    spec = {**SPEC, "base_zoom": 14}
    create(db, spec)
    assert_tiles(db, spec, "polygon")
    (stats,) = db.execute("select gridtiles.stats('minmax')").fetchone()
    assert stats["stats"]["population"]["total"] == pytest.approx(4 * 400 + 4 * 100, rel=1e-9)
