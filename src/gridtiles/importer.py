"""Import point, polygon and raster files into a plain source table.

The table gets a `geom` column (Point or MultiPolygon, in the file's SRID) and the file's
attribute columns, plus the GiST index tiles filter on. Nothing is aggregated: register a layer
over the table with `gridtiles.db.create_layer`. Rasters become points at their pixel centres
with the column `value`. Any other tool that writes a table with a geometry column (ogr2ogr,
GeoPandas) works as well.
"""

import json
import math
import struct
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from os import PathLike
from pathlib import Path
from typing import Literal

import numpy as np
import psycopg
import pyarrow as pa
import pyarrow.parquet as pq
import pyogrio
import rasterio
from psycopg import sql

BATCH_SIZE = 100_000
STAGING = "_gridtiles_staging"
GEOMETRY = "geom"
LON_NAMES = "lon,lng,longitude,x"
LAT_NAMES = "lat,latitude,y"
RASTER_SUFFIXES = (".tif", ".tiff", ".vrt", ".jp2", ".nc")
RASTER_COLUMN = "value"

Kind = Literal["point", "polygon"]
Mode = Literal["create", "replace", "append"]
_WKB_KINDS: dict[int, Kind] = {1: "point", 3: "polygon", 6: "polygon"}
_COPY_TYPES = {"boolean": "bool", "bigint": "int8", "double precision": "float8"}


class UnsupportedGeometry(ValueError):
    """The source holds a geometry type this importer cannot handle."""


@dataclass(frozen=True)
class ImportReport:
    records_read: int
    records_imported: int
    skipped_null_geometry: int
    kind: Kind
    columns: tuple[str, ...]  # attribute columns, besides `geom`


def import_file(
    conn: psycopg.Connection,
    path: str | PathLike[str],
    table: str,
    *,
    lon: str | None = None,
    lat: str | None = None,
    crs: str = "EPSG:4326",
    band: int = 1,
    mode: Mode = "create",
) -> ImportReport:
    """Import a file into `table` (optionally schema-qualified, `schema.name`).

    Reads CSV (`lon` / `lat` columns, default: common names), GeoParquet, anything GDAL reads, and
    rasters (`.tif`, `.tiff`, `.vrt`, `.jp2`, `.nc`, one `band`). `crs` applies when the source
    declares none. `mode`: `create` fails if the table exists, `replace` drops it first, `append`
    adds rows to an existing table with the same columns.
    """
    target = _identifier(table)
    path = Path(path)
    found: list[Kind] = []
    counts = {"read": 0, "imported": 0}
    with conn.transaction():
        if path.suffix.lower() in RASTER_SUFFIXES:
            srid, columns, read, records = _raster(path, band)
            found.append("point")
            counts.update(read=read, imported=read)
            _stage(conn, columns, records)
        else:
            with _open(path, lon, lat, crs) as (srid, columns, batches):
                _stage(conn, columns, _vector_records(srid, columns, batches, found, counts))
        kind = found[0] if found else "point"
        _write(conn, target, kind, srid, columns, mode)
    return ImportReport(counts["read"], counts["imported"], counts["read"] - counts["imported"], kind, tuple(columns))


def _identifier(table: str) -> sql.Identifier:
    parts = table.split(".")
    if len(parts) > 2 or not all(parts):
        raise ValueError(f"table {table!r} is not a name or schema.name")
    return sql.Identifier(*parts)


def _pg_type(t: pa.DataType) -> str | None:
    """The PostgreSQL type of an Arrow column, or None if it is not imported."""
    if pa.types.is_boolean(t):
        return "boolean"
    if pa.types.is_integer(t):
        return "bigint"
    if pa.types.is_floating(t) or pa.types.is_decimal(t):
        return "double precision"
    if pa.types.is_string(t) or pa.types.is_large_string(t):
        return "text"
    if pa.types.is_date(t):
        return "date"
    if pa.types.is_timestamp(t):
        return "timestamptz" if t.tz else "timestamp"
    return None


def _vector_records(
    srid: int,
    columns: dict[str, str],
    batches: Iterable[pa.RecordBatch],
    found: list[Kind],
    counts: dict[str, int],
) -> Iterator[tuple[object, ...]]:
    for batch in batches:
        values = [batch.column(name).to_pylist() for name in columns]
        for i, wkb in enumerate(batch.column(0).to_pylist()):
            if wkb is not None:
                row_kind, ewkb = _ewkb(wkb, srid, counts["read"] + i)
                if not found:
                    found.append(row_kind)
                elif row_kind != found[0]:
                    raise UnsupportedGeometry(f"row {counts['read'] + i}: a {row_kind} among {found[0]}s")
                if ewkb is None:
                    continue
                counts["imported"] += 1
                yield (ewkb, *(col[i] for col in values))
        counts["read"] += batch.num_rows


def _raster(path: Path, band: int) -> tuple[int, dict[str, str], int, Iterator[tuple[object, ...]]]:
    """Each valid pixel as a point at its centre with the column `value`."""
    with rasterio.open(path) as src:
        srid = src.crs.to_epsg() if src.crs else None
        if srid is None:
            raise ValueError(f"{path}: raster CRS is not an EPSG code")
        data = np.ma.masked_invalid(src.read(band, masked=True))  # nodata, and NaN in float rasters without one
        rows, cols = np.nonzero(~np.ma.getmaskarray(data))
        xs, ys = rasterio.transform.xy(src.transform, rows, cols, offset="center")
    values = data.compressed().astype(float).tolist()
    records = ((struct.pack("<BIIdd", 1, 0x20000001, srid, x, y), v) for x, y, v in zip(xs, ys, values))
    return srid, {RASTER_COLUMN: "double precision"}, len(values), records


def _stage(conn: psycopg.Connection, columns: dict[str, str], records: Iterable[tuple[object, ...]]) -> None:
    """COPY (ewkb, *columns) records into a temporary staging table."""
    staging = sql.Identifier(STAGING)
    defs = sql.SQL("").join(sql.SQL(", {} {}").format(sql.Identifier(c), sql.SQL(t)) for c, t in columns.items())
    conn.execute(sql.SQL("create temp table {} (geom geometry{})").format(staging, defs))
    with conn.cursor().copy(sql.SQL("copy {} from stdin (format binary)").format(staging)) as copy:
        copy.set_types(["bytea"] + [_COPY_TYPES.get(t, t) for t in columns.values()])
        for record in records:
            copy.write_row(record)


def _write(
    conn: psycopg.Connection, target: sql.Identifier, kind: Kind, srid: int, columns: dict[str, str], mode: Mode
) -> None:
    """Move the staging rows into the target table, typed and 2D, and index it."""
    exists = conn.execute("select to_regclass(%s) is not null", (target.as_string(conn),)).fetchone() == (True,)
    if mode == "create" and exists:
        raise ValueError(f"table {target.as_string(conn)} exists: replace or append to it")
    if mode == "append" and not exists:
        raise ValueError(f"table {target.as_string(conn)} does not exist")
    if mode == "replace":
        conn.execute(sql.SQL("drop table if exists {}").format(target))
    geometry_type = "Point" if kind == "point" else "MultiPolygon"
    names = sql.SQL("").join(sql.SQL(", {}").format(sql.Identifier(c)) for c in columns)
    if mode != "append":
        defs = sql.SQL("").join(sql.SQL(", {} {}").format(sql.Identifier(c), sql.SQL(t)) for c, t in columns.items())
        conn.execute(
            sql.SQL("create table {} ({} geometry({}, {}){})").format(
                target, sql.Identifier(GEOMETRY), sql.SQL(geometry_type), sql.Literal(srid), defs
            )
        )
    geom = "ST_Force2D(geom)" if kind == "point" else "ST_Multi(ST_Force2D(geom))"
    conn.execute(
        sql.SQL("insert into {} ({}{}) select {}{} from {}").format(
            target, sql.Identifier(GEOMETRY), names, sql.SQL(geom), names, sql.Identifier(STAGING)
        )
    )
    conn.execute(sql.SQL("drop table {}").format(sql.Identifier(STAGING)))
    if mode != "append":
        # Tiles filter on the geometry in EPSG:4326 and 3857, on its 4326 transform in any other SRID.
        indexed = "geom" if srid in (3857, 4326) else "ST_Transform(geom, 4326)"
        conn.execute(sql.SQL("create index on {} using gist ({})").format(target, sql.SQL(indexed)))
    conn.execute(sql.SQL("analyze {}").format(target))


@contextmanager
def _open(
    path: Path, lon: str | None, lat: str | None, crs: str
) -> Iterator[tuple[int, dict[str, str], Iterator[pa.RecordBatch]]]:
    """Yield the source SRID, the imported columns and their PostgreSQL types, and record
    batches whose first column is the geometry as WKB followed by those columns.
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
        if "crs" not in column:
            srid = 4326
        elif column["crs"] is None:
            srid = _srid(crs)
        else:
            ident = column["crs"].get("id", {})
            srid = _srid(f"{ident.get('authority')}:{ident.get('code')}")
        columns = _columns(source.schema_arrow, geometry)
        yield srid, columns, source.iter_batches(BATCH_SIZE, columns=[geometry, *columns])
        return
    options = {}
    if path.suffix.lower() == ".csv":
        options = {
            "X_POSSIBLE_NAMES": lon or LON_NAMES,
            "Y_POSSIBLE_NAMES": lat or LAT_NAMES,
            "AUTODETECT_TYPE": "YES",
            "KEEP_GEOM_COLUMNS": "NO",
        }
    with pyogrio.open_arrow(path, batch_size=BATCH_SIZE, use_pyarrow=True, **options) as (meta, reader):
        geometry = meta["geometry_name"] or "wkb_geometry"
        columns = _columns(reader.schema, geometry)
        yield _srid(meta["crs"] or crs), columns, (batch.select([geometry, *columns]) for batch in reader)


def _columns(schema: pa.Schema, geometry: str) -> dict[str, str]:
    columns = {f.name: t for f in schema if f.name != geometry and (t := _pg_type(f.type)) is not None}
    if GEOMETRY in columns:
        raise ValueError(f"the source has an attribute column named {GEOMETRY!r}, which the table needs for the geometry")
    return columns


def _srid(crs: str) -> int:
    authority, _, code = crs.partition(":")
    if authority.upper() == "OGC" and code.upper() == "CRS84":
        return 4326
    if authority.upper() != "EPSG" or not code.isdigit():
        raise ValueError(f"CRS {crs!r} is not an EPSG code")
    return int(code)


def _ewkb(wkb: bytes, srid: int, row: int) -> tuple[Kind, bytes | None]:
    """Classify WKB as point or polygon and embed the SRID (EWKB), so PostGIS reads it directly.

    The bytes are None for an empty geometry: a point with NaN coordinates (as GDAL writes POINT
    EMPTY) or a polygon without rings. EWKB input that already carries an SRID is passed through.
    """
    order = "<" if wkb[0] == 1 else ">"
    (code,) = struct.unpack_from(order + "I", wkb, 1)
    kind = _WKB_KINDS.get((code & 0xFFFF) % 1000)
    if kind is None:
        raise UnsupportedGeometry(f"row {row}: only points and polygons are supported, got WKB geometry type {code}")
    has_srid = code & 0x20000000
    body = 9 if has_srid else 5
    if kind == "point":
        empty = math.isnan(struct.unpack_from(order + "d", wkb, body)[0])
    else:
        empty = struct.unpack_from(order + "I", wkb, body)[0] == 0
    if empty:
        return kind, None
    if has_srid:
        return kind, wkb
    return kind, wkb[:1] + struct.pack(order + "II", code | 0x20000000, srid) + wkb[5:]
