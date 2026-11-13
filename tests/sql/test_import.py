import datetime as dt
import json
import math
import struct
import time
from pathlib import Path

import psycopg
import pyarrow as pa
import pyarrow.parquet as pq
import pyogrio
import pytest

from gridtiles.importer import UnsupportedGeometry, import_file

from ..conftest import FIXTURES
from ..fixtures.make_fixtures import point_wkb, polygon_wkb, random_points, write_geoparquet, write_vector

COLUMNS = {"pop": [400], "osm_ratio": [0.5], "area": [1.0]}


def column_types(db: psycopg.Connection, table: str) -> dict[str, str]:
    rows = db.execute(
        "select attname, format_type(atttypid, atttypmod) from pg_attribute"
        " where attrelid = %s::regclass and attnum > 0 and not attisdropped order by attnum",
        (table,),
    ).fetchall()
    return dict(rows)


def indexes(db: psycopg.Connection, table: str) -> list[str]:
    rows = db.execute("select indexdef from pg_indexes where tablename = %s", (table,)).fetchall()
    return [r[0] for r in rows]


def test_points_small_csv(db: psycopg.Connection) -> None:
    report = import_file(db, FIXTURES / "points_small.csv", "pts")
    assert (report.records_read, report.records_imported, report.kind) == (8, 8, "point")
    assert column_types(db, "pts") == {
        "geom": "geometry(Point,4326)",
        "pop": "bigint",
        "osm_ratio": "double precision",
        "area": "bigint",
    }
    assert db.execute("select count(*), count(osm_ratio), sum(pop) from pts").fetchone() == (8, 7, 360)
    assert indexes(db, "pts") == ["CREATE INDEX pts_geom_idx ON public.pts USING gist (geom)"]


def test_points_10k_parquet(db: psycopg.Connection) -> None:
    report = import_file(db, FIXTURES / "points_10k.parquet", "pts")
    table = pq.read_table(FIXTURES / "points_10k.parquet")
    loaded = [i for i, g in enumerate(table["geometry"].to_pylist()) if g is not None]
    assert (report.records_read, report.records_imported) == (10_000, len(loaded))
    assert report.columns == ("pop", "osm_ratio", "area")
    # Rows keep the file order and its exact values.
    rows = db.execute("select ST_X(geom), ST_Y(geom), pop, osm_ratio from pts order by ctid").fetchall()
    geoms, pops, ratios = table["geometry"].to_pylist(), table["pop"].to_pylist(), table["osm_ratio"].to_pylist()
    assert rows == [(*struct.unpack("<dd", geoms[i][5:21]), pops[i], ratios[i]) for i in loaded]


def test_existing_table(db: psycopg.Connection) -> None:
    import_file(db, FIXTURES / "points_small.csv", "pts")
    with pytest.raises(ValueError, match="exists"):
        import_file(db, FIXTURES / "points_small.csv", "pts")
    db.execute("delete from pts")
    import_file(db, FIXTURES / "points_small.csv", "pts", replace=True)
    assert db.execute("select count(*) from pts").fetchone() == (8,)


def test_schema_qualified_table(db: psycopg.Connection) -> None:
    db.execute("create schema data")
    import_file(db, FIXTURES / "points_small.csv", "data.pts")
    assert db.execute("select count(*) from data.pts").fetchone() == (8,)
    with pytest.raises(ValueError, match="schema.name"):
        import_file(db, FIXTURES / "points_small.csv", "a.b.c")


def test_other_crs_gets_an_expression_index(db: psycopg.Connection, tmp_path: Path) -> None:
    path = tmp_path / "utm.gpkg"
    write_vector(path, COLUMNS, [point_wkb(390000, 5820000)], crs="EPSG:25833")
    import_file(db, path, "utm")
    assert column_types(db, "utm")["geom"] == "geometry(Point,25833)"
    assert indexes(db, "utm") == ["CREATE INDEX utm_st_transform_idx ON public.utm USING gist (st_transform(geom, 4326))"]


def test_polygons_become_multipolygons(db: psycopg.Connection) -> None:
    report = import_file(db, FIXTURES / "polygons_500.gpkg", "polys")
    assert (report.records_imported, report.kind) == (500, "polygon")
    assert column_types(db, "polys")["geom"] == "geometry(MultiPolygon,4326)"
    total = sum(pyogrio.read_arrow(FIXTURES / "polygons_500.gpkg", columns=["pop"])[1]["pop"].to_pylist())
    assert db.execute("select sum(pop) from polys").fetchone() == (total,)


def test_empty_geometries_are_skipped(db: psycopg.Connection, tmp_path: Path) -> None:
    path = tmp_path / "empty.parquet"
    empty = struct.pack("<BIdd", 1, 1, math.nan, math.nan)
    write_geoparquet(path, {"pop": [1, 2], "osm_ratio": [0.5, 0.5], "area": [1.0, 1.0]}, [point_wkb(13.4, 52.5), empty])
    report = import_file(db, path, "pts")
    assert (report.records_read, report.records_imported) == (2, 1)

    path = tmp_path / "empty_polygon.parquet"
    square = polygon_wkb([(13.40, 52.51), (13.41, 52.51), (13.41, 52.52), (13.40, 52.52)])
    write_geoparquet(path, {k: v * 2 for k, v in COLUMNS.items()}, [square, struct.pack("<BII", 1, 3, 0)])
    report = import_file(db, path, "polys")
    assert (report.records_read, report.records_imported) == (2, 1)


def test_ewkb_with_srid_is_kept(db: psycopg.Connection, tmp_path: Path) -> None:
    path = tmp_path / "ewkb.parquet"
    write_geoparquet(path, COLUMNS, [struct.pack("<BIIdd", 1, 1 | 0x20000000, 4326, 13.4, 52.5)])
    import_file(db, path, "pts")
    assert db.execute("select ST_AsEWKT(geom) from pts").fetchone() == ("SRID=4326;POINT(13.4 52.5)",)


def test_mixed_geometries_raise(db: psycopg.Connection, tmp_path: Path) -> None:
    path = tmp_path / "mixed.geojson"
    feature = {"type": "Feature", "properties": {"pop": 1}}
    point = {**feature, "geometry": {"type": "Point", "coordinates": [13.4, 52.5]}}
    polygon = {
        **feature,
        "geometry": {"type": "Polygon", "coordinates": [[[13.4, 52.5], [13.5, 52.5], [13.5, 52.6], [13.4, 52.5]]]},
    }
    path.write_text(json.dumps({"type": "FeatureCollection", "features": [point, polygon]}))
    with pytest.raises(UnsupportedGeometry, match="ST_Point, ST_Polygon"):
        import_file(db, path, "pts")
    assert db.execute("select to_regclass('pts')").fetchone() == (None,)


def test_plain_parquet_raises(db: psycopg.Connection, tmp_path: Path) -> None:
    path = tmp_path / "plain.parquet"
    pq.write_table(pa.table({"lon": [13.4], "lat": [52.5], "pop": [1]}), path)
    with pytest.raises(ValueError, match="not GeoParquet"):
        import_file(db, path, "pts")


def test_native_geoparquet_encoding_raises(db: psycopg.Connection, tmp_path: Path) -> None:
    path = tmp_path / "native.parquet"
    geo = {"version": "1.1.0", "primary_column": "geometry", "columns": {"geometry": {"encoding": "point"}}}
    table = pa.table({"pop": [1], "geometry": [{"x": 13.4, "y": 52.5}]})
    pq.write_table(table.replace_schema_metadata({"geo": json.dumps(geo)}), path)
    with pytest.raises(UnsupportedGeometry, match="encoding"):
        import_file(db, path, "pts")


def test_attribute_named_geom_raises(db: psycopg.Connection, tmp_path: Path) -> None:
    path = tmp_path / "geom.parquet"
    write_geoparquet(path, {"geom": [1]}, [point_wkb(13.4, 52.5)])
    with pytest.raises(ValueError, match="'geom'"):
        import_file(db, path, "pts")


def test_column_types(db: psycopg.Connection, tmp_path: Path) -> None:
    path = tmp_path / "types.parquet"
    columns = {
        "flag": [True],
        "label": ["a"],
        "day": pa.array([dt.date(2024, 5, 1)], pa.date32()),
        "at": pa.array([dt.datetime(2024, 5, 1, 12, tzinfo=dt.UTC)], pa.timestamp("us", tz="UTC")),
        "nested": [[1, 2]],
    }
    write_geoparquet(path, columns, [point_wkb(13.4, 52.5)])
    report = import_file(db, path, "pts")
    assert report.columns == ("flag", "label", "day", "at")  # lists are not imported
    assert column_types(db, "pts") == {
        "geom": "geometry(Point,4326)",
        "flag": "boolean",
        "label": "text",
        "day": "date",
        "at": "timestamp with time zone",
    }


@pytest.mark.perf
def test_perf_1m_points(db: psycopg.Connection, tmp_path: Path) -> None:
    columns, points = random_points(1_000_000, seed=7)
    path = tmp_path / "points_1m.parquet"
    write_geoparquet(path, columns, [point_wkb(*p) for p in points if p is not None])
    start = time.perf_counter()
    report = import_file(db, path, "pts")
    elapsed = time.perf_counter() - start
    print(f"imported {report.records_imported} points in {elapsed:.1f} s")
    assert elapsed < 60
