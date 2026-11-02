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
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pyogrio
import rasterio
from rasterio.transform import from_origin

HERE = Path(__file__).parent
BERLIN = (13.08, 52.33, 13.76, 52.68)  # lon_min, lat_min, lon_max, lat_max


def point_wkb(x: float, y: float) -> bytes:
    return struct.pack("<BIdd", 1, 1, x, y)


def polygon_wkb(ring: list[tuple[float, float]]) -> bytes:
    ring = [*ring, ring[0]]
    return struct.pack("<BII", 1, 3, 1) + struct.pack("<I", len(ring)) + b"".join(struct.pack("<dd", *p) for p in ring)


def write_vector(path: Path, columns: dict[str, list], wkb: list[bytes], crs: str = "EPSG:4326") -> None:
    """Write any GDAL vector format (by extension) from WKB geometries."""
    table = pa.table({**columns, "geometry": pa.array(wkb, pa.binary())})
    pyogrio.write_arrow(table, path, geometry_name="geometry", geometry_type="Unknown", crs=crs)


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


def dense_block(path: Path) -> None:
    """One point at the centre of each of the 256 z16 cells of the z12 tile around Berlin Mitte."""
    parent = mercantile.tile(13.405, 52.52, 12)
    points, pop = [], []
    for i, tile in enumerate(mercantile.children(parent, zoom=16)):
        b = mercantile.bounds(tile)
        points.append(point_wkb((b.west + b.east) / 2, (b.south + b.north) / 2))
        pop.append(i)
    write_geoparquet(path, {"pop": pop, "osm_ratio": [0.5] * 256, "area": [1.0] * 256}, points)


def points_10k_v2() -> None:
    """points_10k with 10% of values changed, 5% of points removed and 5% added (seed 43)."""
    rng = random.Random(43)
    columns, points = random_points(10_000, seed=42, null_share=0.02)
    keep = [i for i in range(10_000) if rng.random() >= 0.05]
    out: dict[str, list] = {name: [values[i] for i in keep] for name, values in columns.items()}
    kept = [points[i] for i in keep]
    for i in range(len(kept)):
        if rng.random() < 0.10:
            out["pop"][i] = rng.randint(0, 100)
            out["osm_ratio"][i] = rng.random()
    added, new = random_points(500, seed=44)
    for name in out:
        out[name] += added[name]
    write_geoparquet(HERE / "points_10k_v2.parquet", out, [None if p is None else point_wkb(*p) for p in kept + new])


def polygons_500() -> None:
    """500 random rectangles and triangles, up to ~0.02 degrees across."""
    rng = random.Random(42)
    wkb, columns = [], {"pop": [], "osm_ratio": [], "area": []}
    for i in range(500):
        x, y = rng.uniform(BERLIN[0], BERLIN[2] - 0.02), rng.uniform(BERLIN[1], BERLIN[3] - 0.02)
        w, h = rng.uniform(0.001, 0.02), rng.uniform(0.001, 0.02)
        ring = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)] if i % 2 else [(x, y), (x + w, y), (x, y + h)]
        wkb.append(polygon_wkb(ring))
        columns["pop"].append(rng.randint(0, 1000))
        columns["osm_ratio"].append(rng.random())
        columns["area"].append(rng.uniform(1, 500))
    path = HERE / "polygons_500.gpkg"
    path.unlink(missing_ok=True)
    write_vector(path, columns, wkb)


def raster_100() -> None:
    """100 x 100 EPSG:3857 pixels of 5 m near Berlin; value = row * 100 + col; 5-pixel nodata border."""
    data = (np.arange(100)[:, None] * 100 + np.arange(100)[None, :]).astype("int32")
    data[:5, :] = data[-5:, :] = data[:, :5] = data[:, -5:] = -9999
    profile = {
        "driver": "GTiff",
        "width": 100,
        "height": 100,
        "count": 1,
        "dtype": "int32",
        "crs": "EPSG:3857",
        "transform": from_origin(1492000.3, 6894000.7, 5, 5),
        "nodata": -9999,
    }
    with rasterio.open(HERE / "raster_100.tif", "w", **profile) as dst:
        dst.write(data, 1)


if __name__ == "__main__":
    points_small()
    points_10k()
    points_10k_v2()
    polygons_500()
    raster_100()
