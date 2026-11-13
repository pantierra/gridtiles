"""Deterministic source tables for tests: points and polygons around Berlin, created in PostGIS."""

import random

import mercantile
import psycopg
from psycopg import sql

BERLIN = (13.08, 52.33, 13.76, 52.68)  # lon_min, lat_min, lon_max, lat_max
Row = tuple[str | None, int, float | None, float]  # EWKT geometry, pop, osm_ratio, area


def points_small() -> list[Row]:
    """8 points in 3 z16 cells around Berlin Mitte; one point has no osm_ratio. pop sums to 360."""
    tiles = [mercantile.tile(13.405, 52.52, 16), mercantile.tile(13.41, 52.52, 16), mercantile.tile(13.405, 52.51, 16)]
    rows: list[Row] = []
    for tile, offsets in zip(tiles, [(0.2, 0.3, 0.7), (0.4, 0.6), (0.5, 0.25, 0.75)]):
        b = mercantile.bounds(tile)
        for f in offsets:
            point = _point(b.west + f * (b.east - b.west), b.south + (1 - f) * (b.north - b.south))
            i = len(rows) + 1
            rows.append((point, 10 * i, None if i == 5 else round(0.1 * i, 1), 100.0))
    return rows


def points_10k() -> list[Row]:
    """10,000 random points, 2% without a geometry."""
    rng = random.Random(42)
    rows: list[Row] = []
    for _ in range(10_000):
        lon, lat = rng.uniform(BERLIN[0], BERLIN[2]), rng.uniform(BERLIN[1], BERLIN[3])
        point = None if rng.random() < 0.02 else _point(lon, lat)
        rows.append((point, rng.randint(0, 100), rng.random(), rng.uniform(1, 500)))
    return rows


def dense_block() -> list[Row]:
    """One point at the centre of each of the 256 z16 cells of the z12 tile around Berlin Mitte; pop = index."""
    children = mercantile.children(mercantile.tile(13.405, 52.52, 12), zoom=16)
    bounds = map(mercantile.bounds, children)
    return [(_point((b.west + b.east) / 2, (b.south + b.north) / 2), i, 0.5, 1.0) for i, b in enumerate(bounds)]


def polygons_500() -> list[Row]:
    """500 random rectangles and triangles, up to ~0.02 degrees across."""
    rng = random.Random(42)
    rows: list[Row] = []
    for i in range(500):
        x, y = rng.uniform(BERLIN[0], BERLIN[2] - 0.02), rng.uniform(BERLIN[1], BERLIN[3] - 0.02)
        w, h = rng.uniform(0.001, 0.02), rng.uniform(0.001, 0.02)
        ring = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)] if i % 2 else [(x, y), (x + w, y), (x, y + h)]
        coordinates = ", ".join(f"{px!r} {py!r}" for px, py in [*ring, ring[0]])
        rows.append((f"SRID=4326;MULTIPOLYGON((({coordinates})))", rng.randint(0, 1000), rng.random(), rng.uniform(1, 500)))
    return rows


SOURCES = {"points_small": points_small, "points_10k": points_10k, "dense_block": dense_block, "polygons_500": polygons_500}


def create_source(conn: psycopg.Connection, name: str, table: str = "pop_points") -> None:
    """Create `table` (geom, pop, osm_ratio, area) from a named source, with its GiST index."""
    kind = "MultiPolygon" if name.startswith("polygons") else "Point"
    target = sql.Identifier(table)
    conn.execute(
        sql.SQL("create table {} (geom geometry({}, 4326), pop bigint, osm_ratio float8, area float8)").format(
            target, sql.SQL(kind)
        )
    )
    with conn.cursor().copy(sql.SQL("copy {} from stdin").format(target)) as copy:
        for row in SOURCES[name]():
            copy.write_row(row)
    conn.execute(sql.SQL("create index on {} using gist (geom)").format(target))
    conn.execute(sql.SQL("analyze {}").format(target))


def _point(lon: float, lat: float) -> str:
    return f"SRID=4326;POINT({lon!r} {lat!r})"
