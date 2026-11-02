"""Generate the deterministic test fixtures. Run: uv run python tests/fixtures/make_fixtures.py

Expected values are computed with mercantile, independently of gridtiles.
"""

import csv
import json
import random
import struct
from collections import defaultdict
from pathlib import Path

import mercantile
import pyarrow as pa
import pyarrow.parquet as pq

HERE = Path(__file__).parent
BERLIN = (13.08, 52.33, 13.76, 52.68)  # lon_min, lat_min, lon_max, lat_max


def point_wkb(x: float, y: float) -> bytes:
    return struct.pack("<BIdd", 1, 1, x, y)


def write_geoparquet(path: Path, columns: dict[str, list], wkb: list[bytes | None], epsg: int = 4326) -> None:
    column: dict = {"encoding": "WKB", "geometry_types": ["Point"]}
    if epsg != 4326:  # absent means OGC:CRS84
        column["crs"] = {"id": {"authority": "EPSG", "code": epsg}}
    geo = {"version": "1.0.0", "primary_column": "geometry", "columns": {"geometry": column}}
    table = pa.table({**columns, "geometry": pa.array(wkb, pa.binary())})
    pq.write_table(table.replace_schema_metadata({"geo": json.dumps(geo)}), path)


def random_points(n: int, seed: int, null_share: float = 0.0) -> tuple[dict[str, list], list[tuple[float, float] | None]]:
    rng = random.Random(seed)
    points: list[tuple[float, float] | None] = []
    columns: dict[str, list] = {"pop": [], "osm_ratio": [], "area": []}
    for _ in range(n):
        lonlat = (rng.uniform(BERLIN[0], BERLIN[2]), rng.uniform(BERLIN[1], BERLIN[3]))
        points.append(None if rng.random() < null_share else lonlat)
        columns["pop"].append(rng.randint(0, 100))
        columns["osm_ratio"].append(rng.random())
        columns["area"].append(rng.uniform(1, 500))
    return columns, points


def points_small() -> None:
    """8 points in 3 z16 cells around Berlin Mitte; one point has no osm_ratio."""
    tiles = [mercantile.tile(13.405, 52.52, 16), mercantile.tile(13.41, 52.52, 16), mercantile.tile(13.405, 52.51, 16)]
    rows = []
    for i, offsets in enumerate([(0.2, 0.3, 0.7), (0.4, 0.6), (0.5, 0.25, 0.75)]):
        b = mercantile.bounds(tiles[i])
        for f in offsets:
            lon, lat = b.west + f * (b.east - b.west), b.south + (1 - f) * (b.north - b.south)
            rows.append(
                {
                    "lon": lon,
                    "lat": lat,
                    "pop": 10 * (len(rows) + 1),
                    "osm_ratio": round(0.1 * (len(rows) + 1), 1),
                    "area": 100,
                }
            )
    rows[4]["osm_ratio"] = None
    with open(HERE / "points_small.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows({k: "" if v is None else v for k, v in r.items()} for r in rows)

    cells: dict[str, dict] = defaultdict(
        lambda: {"n": 0, "population_sum": 0.0, "completeness_sum": 0.0, "completeness_w": 0.0}
    )
    for r in rows:
        c = cells[mercantile.quadkey(mercantile.tile(r["lon"], r["lat"], 16))]
        c["n"] += 1
        c["population_sum"] += r["pop"]
        if r["osm_ratio"] is not None:
            c["completeness_sum"] += r["osm_ratio"] * r["area"]
            c["completeness_w"] += r["area"]
    for c in cells.values():
        c["completeness"] = c["completeness_sum"] / c["completeness_w"]
    (HERE / "points_small.expected.json").write_text(json.dumps(dict(sorted(cells.items())), indent=2) + "\n")


def points_10k() -> None:
    columns, points = random_points(10_000, seed=42, null_share=0.02)
    write_geoparquet(HERE / "points_10k.parquet", columns, [None if p is None else point_wkb(*p) for p in points])


if __name__ == "__main__":
    points_small()
    points_10k()
