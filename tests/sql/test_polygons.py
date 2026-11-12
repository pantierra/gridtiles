import math
import random
import struct
from pathlib import Path

import numpy as np
import psycopg
import pyogrio
import pytest
import rasterio
import shapely

from gridtiles import quadkey
from gridtiles.ingest import CellLimitExceeded, load, load_polygons, load_raster

from ..conftest import FIXTURES
from ..fixtures.make_fixtures import polygon_wkb, write_geoparquet, write_vector
from .test_ingest import SPEC, create

POLY_SPEC = {**SPEC, "name": "poly"}
RASTER_SPEC = {"name": "raster", "base_zoom": 20, "min_zoom": 12, "values": [{"name": "v", "from": "value", "agg": "sum"}]}
COLUMNS = {"pop": [400], "osm_ratio": [0.5], "area": [1.0]}


def cell_rows(db: psycopg.Connection, layer: str, z: int) -> dict[int, tuple]:
    rows = db.execute(f"select qk, n, population_sum from gridtiles.cells_{layer} where z = %s", (z,)).fetchall()
    return {r[0]: r[1:] for r in rows}


def mercator_polygon(path: Path, xmin: float, ymin: float, xmax: float, ymax: float) -> Path:
    write_vector(path, COLUMNS, [polygon_wkb([(xmin, ymin), (xmax, ymin), (xmax, ymax), (xmin, ymax)])], crs="EPSG:3857")
    return path


def test_empty_polygon_is_skipped(db: psycopg.Connection, tmp_path: Path) -> None:
    path = tmp_path / "empty.parquet"
    square = polygon_wkb([(13.40, 52.51), (13.41, 52.51), (13.41, 52.52), (13.40, 52.52)])
    empty = struct.pack("<BII", 1, 3, 0)  # a polygon without rings
    write_geoparquet(path, {k: v * 2 for k, v in COLUMNS.items()}, [square, empty])
    create(db, POLY_SPEC)
    report = load_polygons(db, "poly", path)
    assert (report.records_read, report.records_loaded, report.skipped_null_geometry) == (2, 1, 1)
    assert sum(pop for _, pop in cell_rows(db, "poly", 16).values()) == pytest.approx(400)


def test_self_intersecting_polygon_loads(db: psycopg.Connection, tmp_path: Path) -> None:
    bowtie = polygon_wkb([(13.40, 52.51), (13.41, 52.52), (13.41, 52.51), (13.40, 52.52)])
    path = tmp_path / "bowtie.gpkg"
    write_vector(path, COLUMNS, [bowtie])
    create(db, POLY_SPEC)
    load_polygons(db, "poly", path)
    assert sum(pop for _, pop in cell_rows(db, "poly", 16).values()) == pytest.approx(400)


def test_polygon_over_four_cells(db: psycopg.Connection, tmp_path: Path) -> None:
    qk = quadkey.from_lonlat(13.405, 52.52, 15)
    create(db, POLY_SPEC)
    load_polygons(db, "poly", mercator_polygon(tmp_path / "four.gpkg", *quadkey.envelope(15, qk)))
    cells = cell_rows(db, "poly", 16)
    assert set(cells) == set(quadkey.child_range(15, qk, 16))
    for n, pop in cells.values():
        assert n == 1
        assert pop == pytest.approx(100, rel=1e-12)


def test_polygon_over_half_a_cell(db: psycopg.Connection, tmp_path: Path) -> None:
    qk = quadkey.from_lonlat(13.405, 52.52, 16)
    xmin, ymin, xmax, ymax = quadkey.envelope(16, qk)
    create(db, POLY_SPEC)
    load_polygons(db, "poly", mercator_polygon(tmp_path / "half.gpkg", xmin, ymin, (xmin + xmax) / 2, ymax))
    assert cell_rows(db, "poly", 16) == {qk: (1, pytest.approx(400, rel=1e-9))}
    (w,) = db.execute("select completeness_w from gridtiles.cells_poly where z = 16").fetchone()
    assert w == pytest.approx((xmax - xmin) * (ymax - ymin) / 2, rel=1e-9)  # weight 1 x covered area


def test_polygons_500_conserve_total(db: psycopg.Connection) -> None:
    create(db, POLY_SPEC)
    report = load(db, "poly", FIXTURES / "polygons_500.gpkg")
    assert report.records_loaded == 500
    total = sum(pyogrio.read_arrow(FIXTURES / "polygons_500.gpkg", columns=["pop"])[1]["pop"].to_pylist())
    for z in (16, 2):
        (pop,) = db.execute("select sum(population_sum) from gridtiles.cells_poly where z = %s", (z,)).fetchone()
        assert pop == pytest.approx(total, rel=1e-6)


def test_area_weighted_means_match_shapely(db: psycopg.Connection) -> None:
    create(db, POLY_SPEC)
    load(db, "poly", FIXTURES / "polygons_500.gpkg")
    _, table = pyogrio.read_arrow(FIXTURES / "polygons_500.gpkg")
    to_mercator = lambda lon, lat: (  # noqa: E731
        lon * quadkey.HALF_WORLD / 180,
        math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)) * quadkey.HALF_WORLD / math.pi,
    )
    polygons = [
        shapely.transform(g, lambda c: np.array([to_mercator(*p) for p in c])) for g in shapely.from_wkb(table["geom"])
    ]
    ratio, area = table["osm_ratio"].to_pylist(), table["area"].to_pylist()
    rows = db.execute("select qk, completeness_sum / completeness_w from gridtiles.cells_poly where z = 16").fetchall()
    for qk, mean in random.Random(7).sample(rows, 50):
        cell = shapely.box(*quadkey.envelope(16, qk))
        parts = [(ratio[i], area[i], p.intersection(cell).area) for i, p in enumerate(polygons) if p.intersects(cell)]
        expected = sum(r * w * a for r, w, a in parts) / sum(w * a for _, w, a in parts)
        assert mean == pytest.approx(expected, rel=1e-9)


def test_cell_limit(db: psycopg.Connection, tmp_path: Path) -> None:
    path = mercator_polygon(tmp_path / "big.gpkg", *quadkey.envelope(10, quadkey.from_lonlat(13.405, 52.52, 10)))
    create(db, POLY_SPEC)
    with pytest.raises(CellLimitExceeded), db.transaction():
        load_polygons(db, "poly", path, max_cells_per_feature=1000)
    load_polygons(db, "poly", path, max_cells_per_feature=None)
    (n,) = db.execute("select count(*) from gridtiles.cells_poly where z = 16").fetchone()
    assert n == 4**6


def test_raster(db: psycopg.Connection) -> None:
    create(db, RASTER_SPEC)
    report = load(db, "raster", FIXTURES / "raster_100.tif")
    with rasterio.open(FIXTURES / "raster_100.tif") as src:
        data, transform = src.read(1), src.transform
    rows, cols = np.nonzero(data != -9999)
    assert report.records_loaded == len(rows) == 90 * 90
    (total,) = db.execute("select sum(v_sum) from gridtiles.cells_raster where z = 20").fetchone()
    assert total == data[data != -9999].astype(float).sum()

    size = quadkey.WORLD / 2**20
    oracle: dict[int, float] = {}
    for r, c in zip(rows, cols):
        x, y = transform @ (c + 0.5, r + 0.5)
        qk = quadkey.from_xyz(20, math.floor((x + quadkey.HALF_WORLD) / size), math.floor((quadkey.HALF_WORLD - y) / size))
        oracle[qk] = oracle.get(qk, 0) + float(data[r, c])
    assert dict(db.execute("select qk, v_sum from gridtiles.cells_raster where z = 20").fetchall()) == oracle


def test_raster_nan_pixels_are_skipped(db: psycopg.Connection, tmp_path: Path) -> None:
    path = tmp_path / "nan.tif"
    data = np.full((10, 10), 2.0, dtype="float32")
    data[0, :] = np.nan
    profile = {"driver": "GTiff", "width": 10, "height": 10, "count": 1, "dtype": "float32", "crs": "EPSG:3857"}
    with rasterio.open(path, "w", transform=rasterio.transform.from_origin(1492000.3, 6894000.7, 5, 5), **profile) as dst:
        dst.write(data, 1)  # no nodata value declared
    create(db, RASTER_SPEC)
    report = load_raster(db, "raster", path)
    assert report.records_loaded == 90
    (total,) = db.execute("select sum(v_sum) from gridtiles.cells_raster where z = 20").fetchone()
    assert total == 180


def test_raster_pixels_too_large(db: psycopg.Connection) -> None:
    create(db, {**RASTER_SPEC, "base_zoom": 23})
    with pytest.raises(ValueError, match="base_zoom"):
        load_raster(db, "raster", FIXTURES / "raster_100.tif")
