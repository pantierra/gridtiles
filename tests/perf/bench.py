"""Tile latency per zoom on generated data. Run: uv run python -m tests.perf.bench [--points N]

Generates (once, in the database `gridtiles_live_bench`) clustered points and polygons around 300
random centres, then times `gridtiles.mvt` on tiles holding data at every published zoom.
Each tile is requested twice: `first` includes reading pages from disk, `p50`/`p95` are repeats.
Each layer runs `live`, then `cached` with the levels up to z8 cached (`gridtiles.refresh_cache`).
"""

import argparse
import statistics
import time

import psycopg

from gridtiles.db import create_layer, install
from gridtiles.spec import LayerSpec

from ..conftest import ADMIN_DSN

DB = "gridtiles_live_bench"
VALUES = [
    {"name": "population", "from": "pop", "agg": "sum"},
    {"name": "completeness", "from": "osm_ratio", "agg": "mean", "weight": "area"},
]

# Normal deviates by Box-Muller; one centre per row by index, 10% of rows uniform over the world.
POINTS = """
create table {table} as
select ST_SetSRID(ST_MakePoint(
           case when u then -180 + 360 * random()
                else c.lon + c.sigma * sqrt(-2 * ln(1 - random())) * cos(2 * pi() * random()) end,
           case when u then degrees(asin(-0.95 + 1.9 * random()))
                else greatest(-85, least(85, c.lat + c.sigma * sqrt(-2 * ln(1 - random())) * cos(2 * pi() * random()))) end),
       4326)::geometry(Point, 4326) as geom,
       (random() * 100)::int as pop, random() as osm_ratio, 1 + random() * 499 as area
from generate_series(1, {n}) g
join _centres c on c.i = g % 300,
lateral (select random() < 0.1 as u) r
"""
# Squares and detailed circles; 1% are large (up to ~2 degrees) with 1024 vertices.
POLYGONS = """
create table {table} as
select ST_Multi(case when big then ST_Buffer(p, 0.2 + 1.8 * random(), 256)
                     when i % 2 = 0 then ST_Expand(p, 0.001 + 0.05 * random())
                     else ST_Buffer(p, 0.001 + 0.05 * random(), 8) end)::geometry(MultiPolygon, 4326) as geom,
       pop, osm_ratio, area
from (select row_number() over () as i, geom as p, random() < 0.01 as big, pop, osm_ratio, area
      from {points} limit {n}) s
"""


def ensure_data(conn: psycopg.Connection, points: int, polygons: int) -> None:
    exists = conn.execute("select to_regclass('bench_points') is not null").fetchone() == (True,)
    if exists and conn.execute("select count(*) from bench_points").fetchone() == (points,):
        return
    print(f"generating {points:,} points and {polygons:,} polygons ...", flush=True)
    conn.execute("drop table if exists bench_points, bench_polygons, _centres")
    conn.execute("set max_parallel_workers_per_gather = 0")  # one process: setseed makes random() repeatable
    conn.execute("select setseed(0.42)")
    conn.execute(
        "create table _centres as select i, -180 + 360 * random() as lon, degrees(asin(-0.85 + 1.7 * random())) as lat,"
        " 0.05 + 1.5 * random() as sigma from generate_series(0, 299) i"
    )
    conn.execute(POINTS.format(table="bench_points", n=points))
    conn.execute(POLYGONS.format(table="bench_polygons", points="bench_points", n=polygons))
    conn.execute("reset max_parallel_workers_per_gather")
    for table in ("bench_points", "bench_polygons"):
        conn.execute(f"create index on {table} using gist (geom)")
        conn.execute(f"vacuum analyze {table}")


def ensure_layers(conn: psycopg.Connection) -> None:
    conn.execute("select gridtiles.drop_layer(name) from gridtiles.layers")
    for name, table in (("points", "bench_points"), ("polygons", "bench_polygons")):
        spec = {"name": name, "source": {"table": table}, "base_zoom": 16, "min_zoom": 2, "values": VALUES}
        create_layer(conn, LayerSpec.model_validate(spec))


def sample_tiles(conn: psycopg.Connection, table: str, z: int, k: int) -> list[tuple[int, int]]:
    """Up to k distinct tiles at zoom z holding a sampled row: busy places come up more often."""
    rows = conn.execute(
        f"select distinct (gridtiles.point_square(ST_PointOnSurface(geom), %s)).* from {table}"
        " tablesample system (0.1) repeatable (7) limit %s",
        (z, k),
    ).fetchall()
    return [(x, y) for x, y in rows]


CACHE_ZOOM = 8


def run(conn: psycopg.Connection, layer: str, table: str, cached: bool, k: int) -> None:
    start = time.perf_counter()
    (squares,) = conn.execute("select gridtiles.refresh_cache(%s, %s)", (layer, CACHE_ZOOM if cached else None)).fetchone()
    if cached:
        print(f"cached {squares:,} squares up to z{CACHE_ZOOM} in {time.perf_counter() - start:.0f} s", flush=True)
    for z in range(13):
        firsts, repeats, sizes = [], [], []
        for x, y in sample_tiles(conn, table, z, k):
            for attempt in range(2):
                start = time.perf_counter()
                (data,) = conn.execute("select gridtiles.mvt(%s, %s, %s, %s)", (layer, z, x, y)).fetchone()
                (firsts if attempt == 0 else repeats).append(time.perf_counter() - start)
            sizes.append(len(data))
        p95 = statistics.quantiles(repeats, n=20)[-1] if len(repeats) > 1 else repeats[0]
        print(
            f"| {layer} | {'cached' if cached else 'live'} | {z} | {len(repeats)}"
            f" | {max(firsts) * 1000:.0f} | {statistics.median(repeats) * 1000:.0f} | {p95 * 1000:.0f}"
            f" | {statistics.mean(sizes) / 1024:.0f} |",
            flush=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--points", type=int, default=10_000_000)
    parser.add_argument("--polygons", type=int, default=100_000)
    parser.add_argument("--tiles", type=int, default=20, help="tiles per zoom")
    parser.add_argument("--layers", default="points,polygons")
    args = parser.parse_args()

    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        if not admin.execute("select 1 from pg_database where datname = %s", (DB,)).fetchone():
            admin.execute(f"create database {DB}")
    url = psycopg.conninfo.make_conninfo(ADMIN_DSN, dbname=DB)
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("drop schema if exists gridtiles cascade")  # the generated data lives in public
        install(conn)
        ensure_data(conn, args.points, args.polygons)
        ensure_layers(conn)
        print("| layer | variant | z | tiles | first ms | p50 ms | p95 ms | KiB |")
        print("| --- | --- | --- | --- | --- | --- | --- | --- |")
        for layer in args.layers.split(","):
            for cached in (False, True):
                run(conn, layer, f"bench_{layer}", cached, args.tiles)


if __name__ == "__main__":
    main()
