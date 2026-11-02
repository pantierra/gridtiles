"""Load point files into a layer's base cells.

Records stream in batches into a temporary staging table via binary COPY; the aggregation into
cells happens in SQL (`gridtiles.ingest_points`).
"""

import json
import struct
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from os import PathLike
from pathlib import Path

import psycopg
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import pyogrio
from psycopg import sql

from gridtiles.spec import LayerSpec

BATCH_SIZE = 100_000
STAGING = "_gridtiles_staging"
LON_NAMES = "lon,lng,longitude,x"
LAT_NAMES = "lat,latitude,y"


class UnsupportedGeometry(ValueError):
    """The source holds a geometry type this loader cannot ingest."""


@dataclass(frozen=True)
class LoadReport:
    records_read: int
    records_loaded: int
    skipped_null_geometry: int
    cells_written: int


def layer_spec(conn: psycopg.Connection, layer: str) -> LayerSpec:
    row = conn.execute("select spec from gridtiles.layers where name = %s", (layer,)).fetchone()
    if row is None:
        raise LookupError(f"layer {layer!r} does not exist")
    return LayerSpec.model_validate(row[0])


def load_points(
    conn: psycopg.Connection,
    layer: str,
    path: str | PathLike[str],
    *,
    lon: str | None = None,
    lat: str | None = None,
    crs: str = "EPSG:4326",
) -> LoadReport:
    """Aggregate a point file into the layer's base cells.

    Reads CSV (`lon` / `lat` columns), GeoParquet, GeoJSON, Shapefile or anything else GDAL reads.
    `crs` applies when the source does not declare one. Repeated loads add up.
    """
    spec = layer_spec(conn, layer)
    sources = list(dict.fromkeys(s for v in spec.values if v.agg != "count" for s in (v.from_, v.weight) if s))
    read = loaded = 0
    with conn.transaction(), _open(Path(path), sources, lon, lat, crs) as (srid, batches):
        conn.execute(
            sql.SQL("create temp table {} (geom geometry{})").format(
                sql.Identifier(STAGING),
                sql.SQL("").join(sql.SQL(", {} double precision").format(sql.Identifier(s)) for s in sources),
            )
        )
        with conn.cursor().copy(sql.SQL("copy {} from stdin (format binary)").format(sql.Identifier(STAGING))) as copy:
            copy.set_types(["bytea"] + ["float8"] * len(sources))
            for batch in batches:
                values = [pc.cast(batch.column(s), pa.float64()).to_pylist() for s in sources]
                for i, wkb in enumerate(batch.column(0).to_pylist()):
                    if wkb is not None:
                        copy.write_row((_point_ewkb(wkb, srid, read + i), *(col[i] for col in values)))
                        loaded += 1
                read += batch.num_rows
        row = conn.execute("select gridtiles.ingest_points(%s, %s::regclass)", (layer, STAGING)).fetchone()
        conn.execute(sql.SQL("drop table {}").format(sql.Identifier(STAGING)))
    assert row is not None
    return LoadReport(read, loaded, read - loaded, row[0])


@contextmanager
def _open(
    path: Path, columns: list[str], lon: str | None, lat: str | None, crs: str
) -> Iterator[tuple[int, Iterator[pa.RecordBatch]]]:
    """Yield the source SRID and record batches whose first column is the geometry as WKB."""
    if path.suffix.lower() in (".parquet", ".geoparquet"):
        source = pq.ParquetFile(path)
        geo = json.loads(source.schema_arrow.metadata[b"geo"])
        geometry = geo["primary_column"]
        column = geo["columns"][geometry]
        # GeoParquet: no "crs" key means OGC:CRS84, null means unknown, otherwise PROJJSON.
        if "crs" not in column:
            srid = 4326
        elif column["crs"] is None:
            srid = _srid(crs)
        else:
            ident = column["crs"].get("id", {})
            srid = _srid(f"{ident.get('authority')}:{ident.get('code')}")
        yield srid, source.iter_batches(BATCH_SIZE, columns=[geometry, *columns])
        return
    options = {}
    if path.suffix.lower() == ".csv":
        options = {
            "X_POSSIBLE_NAMES": lon or LON_NAMES,
            "Y_POSSIBLE_NAMES": lat or LAT_NAMES,
            "AUTODETECT_TYPE": "YES",
            "KEEP_GEOM_COLUMNS": "NO",
        }
    with pyogrio.open_arrow(path, columns=columns, batch_size=BATCH_SIZE, use_pyarrow=True, **options) as (meta, reader):
        geometry = meta["geometry_name"] or "wkb_geometry"
        srid = _srid(meta["crs"] or crs)
        yield srid, (batch.select([geometry, *columns]) for batch in reader)


def _srid(crs: str) -> int:
    authority, _, code = crs.partition(":")
    if authority.upper() == "OGC" and code.upper() == "CRS84":
        return 4326
    if authority.upper() != "EPSG" or not code.isdigit():
        raise ValueError(f"CRS {crs!r} is not an EPSG code")
    return int(code)


def _point_ewkb(wkb: bytes, srid: int, row: int) -> bytes:
    """Check that WKB is a point and embed the SRID (EWKB), so PostGIS reads it directly."""
    order = "<" if wkb[0] == 1 else ">"
    (kind,) = struct.unpack_from(order + "I", wkb, 1)
    if (kind & 0xFFFF) % 1000 != 1:
        raise UnsupportedGeometry(f"row {row}: only points are supported, got WKB geometry type {kind}")
    return wkb[:1] + struct.pack(order + "II", kind | 0x20000000, srid) + wkb[5:]
