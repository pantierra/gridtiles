"""Import a point or polygon file into a plain table: a `geom` column (Point or MultiPolygon, in the
file's SRID), the file's attribute columns, and the GiST index tiles filter on. Any other tool that
writes such a table (ogr2ogr, GeoPandas) works as well.
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from os import PathLike
from pathlib import Path
from typing import Any

import psycopg
import pyarrow as pa
import pyarrow.parquet as pq
import pyogrio
from psycopg import sql

BATCH_SIZE = 100_000
STAGING = sql.Identifier("_gridtiles_staging")
ROWS = sql.Identifier("_gridtiles_rows")
COPY_TYPES = {"boolean": "bool", "bigint": "int8", "double precision": "float8"}
KINDS = {"point": {"ST_Point"}, "polygon": {"ST_Polygon", "ST_MultiPolygon"}}


class UnsupportedGeometry(ValueError):
    """The file holds geometries other than points or polygons, or both."""


@dataclass(frozen=True)
class ImportReport:
    records_read: int
    records_imported: int  # records with a non-empty geometry
    kind: str  # point or polygon
    columns: tuple[str, ...]  # attribute columns, besides geom


def import_file(conn: psycopg.Connection, path: str | PathLike[str], table: str, *, replace: bool = False) -> ImportReport:
    """Import CSV (lon / lat columns, by common names), GeoParquet or anything GDAL reads into `table`
    (NAME or SCHEMA.NAME). A file without a CRS is read as EPSG:4326. An existing table raises
    ValueError, unless `replace` drops it first.
    """
    parts = table.split(".")
    if len(parts) > 2 or not all(parts):
        raise ValueError(f"table {table!r} is not a name or schema.name")
    target = sql.Identifier(*parts)
    with conn.transaction():
        with _open(Path(path)) as (srid, columns, batches):
            _stage(conn, columns, batches)
        # A view decoding the geometry: EWKB keeps its own SRID; NULL and NaN points (GDAL's POINT EMPTY)
        # are empty. Rows keep the file's order.
        conn.execute(
            sql.SQL("create temp view {} as select (select case when ST_SRID(x) = 0 then ST_SetSRID(x, {}) else x end"
                    " from ST_Force2D(ST_GeomFromEWKB(wkb)) x) as g, * from {}").format(ROWS, srid, STAGING)
        )  # fmt: skip
        types = _one(
            conn, sql.SQL("select array_agg(distinct ST_GeometryType(g)) from {} where not ST_IsEmpty(g)").format(ROWS)
        )
        kind = next((k for k, allowed in KINDS.items() if set(types or []) <= allowed), None)
        if kind is None:
            raise UnsupportedGeometry(f"only points or only polygons are supported, got {', '.join(sorted(types))}")
        if replace:
            conn.execute(sql.SQL("drop table if exists {}").format(target))
        elif _one(conn, sql.SQL("select to_regclass({})").format(sql.Literal(target.as_string(conn)))):
            raise ValueError(f"table {target.as_string(conn)} exists")
        typed = f"g::geometry(Point, {srid})" if kind == "point" else f"ST_Multi(g)::geometry(MultiPolygon, {srid})"
        names = sql.SQL("").join(sql.SQL(", {}").format(sql.Identifier(c)) for c in columns)
        conn.execute(
            sql.SQL("create table {} as select {} as geom{} from {} where not ST_IsEmpty(g)").format(
                target, sql.SQL(typed), names, ROWS
            )
        )
        read = _one(conn, sql.SQL("select count(*) from {}").format(STAGING))
        conn.execute(sql.SQL("drop table {} cascade").format(STAGING))
        # Tiles filter on the geometry in EPSG:4326 and 3857, on its 4326 transform in any other SRID.
        indexed = "geom" if srid in (3857, 4326) else "ST_Transform(geom, 4326)"
        conn.execute(sql.SQL("create index on {} using gist ({})").format(target, sql.SQL(indexed)))
        conn.execute(sql.SQL("analyze {}").format(target))
        imported = _one(conn, sql.SQL("select count(*) from {}").format(target))
    return ImportReport(read, imported, kind, tuple(columns))


def _one(conn: psycopg.Connection, query: sql.Composed) -> Any:
    row = conn.execute(query).fetchone()
    return row[0] if row else None


def _stage(conn: psycopg.Connection, columns: dict[str, str], batches: Iterator[pa.RecordBatch]) -> None:
    """COPY (wkb, *columns) rows into a temporary staging table."""
    defs = sql.SQL("").join(sql.SQL(", {} {}").format(sql.Identifier(c), sql.SQL(t)) for c, t in columns.items())
    conn.execute(sql.SQL("create temp table {} (wkb bytea{})").format(STAGING, defs))
    with conn.cursor().copy(sql.SQL("copy {} from stdin (format binary)").format(STAGING)) as copy:
        copy.set_types(["bytea"] + [COPY_TYPES.get(t, t) for t in columns.values()])
        for batch in batches:
            for row in zip(*(batch.column(i).to_pylist() for i in range(batch.num_columns))):
                copy.write_row(row)


@contextmanager
def _open(path: Path) -> Iterator[tuple[int, dict[str, str], Iterator[pa.RecordBatch]]]:
    """Yield the file's SRID, the imported columns and their PostgreSQL types, and record batches
    of the geometry as WKB followed by those columns.
    """
    if path.suffix.lower() in (".parquet", ".geoparquet"):
        source = pq.ParquetFile(path)
        metadata = source.schema_arrow.metadata or {}
        if b"geo" not in metadata:
            raise ValueError(f"{path} is not GeoParquet (no 'geo' metadata); use CSV with lon/lat columns instead")
        geo = json.loads(metadata[b"geo"])
        geometry = geo["primary_column"]
        column = geo["columns"][geometry]
        if column.get("encoding", "").upper() != "WKB":
            raise UnsupportedGeometry(f"{path}: only WKB geometry encoding is supported, got {column.get('encoding')!r}")
        # GeoParquet: no "crs" key means OGC:CRS84, null means unknown, otherwise PROJJSON.
        ident = (column.get("crs") or {}).get("id", {})
        srid = _srid(f"{ident['authority']}:{ident['code']}") if ident else 4326
        columns = _columns(source.schema_arrow, geometry)
        yield srid, columns, source.iter_batches(BATCH_SIZE, columns=[geometry, *columns])
        return
    options = {"X_POSSIBLE_NAMES": "lon,lng,longitude,x", "Y_POSSIBLE_NAMES": "lat,latitude,y", "AUTODETECT_TYPE": "YES",
               "KEEP_GEOM_COLUMNS": "NO"} if path.suffix.lower() == ".csv" else {}  # fmt: skip
    with pyogrio.open_arrow(path, batch_size=BATCH_SIZE, use_pyarrow=True, **options) as (meta, reader):
        geometry = meta["geometry_name"] or "wkb_geometry"
        columns = _columns(reader.schema, geometry)
        yield _srid(meta["crs"] or "EPSG:4326"), columns, (batch.select([geometry, *columns]) for batch in reader)


def _columns(schema: pa.Schema, geometry: str) -> dict[str, str]:
    """The attribute columns to import and their PostgreSQL types; others (lists, structs) are skipped."""
    types = [
        (pa.types.is_boolean, "boolean"),
        (pa.types.is_integer, "bigint"),
        (lambda t: pa.types.is_floating(t) or pa.types.is_decimal(t), "double precision"),
        (lambda t: pa.types.is_string(t) or pa.types.is_large_string(t), "text"),
        (pa.types.is_date, "date"),
        (lambda t: pa.types.is_timestamp(t) and t.tz is None, "timestamp"),
        (pa.types.is_timestamp, "timestamptz"),
    ]
    columns = {f.name: next((pg for test, pg in types if test(f.type)), None) for f in schema if f.name != geometry}
    if "geom" in columns:
        raise ValueError("the file has an attribute column named 'geom', which the table needs for the geometry")
    return {name: pg for name, pg in columns.items() if pg is not None}


def _srid(crs: str) -> int:
    authority, _, code = crs.partition(":")
    if authority.upper() == "OGC" and code.upper() == "CRS84":
        return 4326
    if authority.upper() != "EPSG" or not code.isdigit():
        raise ValueError(f"CRS {crs!r} is not an EPSG code")
    return int(code)
