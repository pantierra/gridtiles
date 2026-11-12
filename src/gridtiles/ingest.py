"""Load point, polygon and raster files into a layer's base cells.

Records stream in batches into a temporary staging table via binary COPY (a geometry plus one
column per value source); the aggregation into cells happens in SQL (`gridtiles.ingest_points`,
`gridtiles.ingest_polygons`). Rasters become points at their pixel centres.
"""

import json
import math
import struct
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from os import PathLike
from pathlib import Path
from typing import Literal

import numpy as np
import psycopg
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import pyogrio
import rasterio
import rasterio.warp
from psycopg import errors, sql

from gridtiles.db import layer_spec
from gridtiles.quadkey import WORLD

BATCH_SIZE = 100_000
STAGING = "_gridtiles_staging"
DELTA = "_gridtiles_delta"
RESERVED = {"geom"}  # the staging geometry; names starting with "_" are reserved for ingest columns as well
LON_NAMES = "lon,lng,longitude,x"
LAT_NAMES = "lat,latitude,y"
RASTER_SUFFIXES = (".tif", ".tiff", ".vrt", ".jp2", ".nc")
RASTER_SOURCE = "value"  # the source column name of a raster band
MAX_CELLS_PER_FEATURE = 1_000_000

Kind = Literal["point", "polygon"]
UpsertMode = Literal["add", "replace"]
_WKB_KINDS: dict[int, Kind] = {1: "point", 3: "polygon", 6: "polygon"}


class UnsupportedGeometry(ValueError):
    """The source holds a geometry type this loader cannot ingest."""


class CellLimitExceeded(ValueError):
    """A polygon spans more base cells than `max_cells_per_feature` allows."""


@dataclass(frozen=True)
class LoadReport:
    records_read: int
    records_loaded: int
    skipped_null_geometry: int
    cells_written: int


def load(
    conn: psycopg.Connection,
    layer: str,
    path: str | PathLike[str],
    *,
    lon: str | None = None,
    lat: str | None = None,
    crs: str = "EPSG:4326",
    band: int = 1,
    rollup: bool = True,
    max_cells_per_feature: int | None = MAX_CELLS_PER_FEATURE,
    snapshot: date | None = None,
) -> LoadReport:
    """Load points, polygons or a raster, detected from the source; on snapshot layers into the
    cells of `snapshot` (required there), rolling up only that snapshot."""
    return _load(conn, layer, Path(path), lon, lat, crs, band, rollup, max_cells_per_feature, None, snapshot)


def upsert(
    conn: psycopg.Connection,
    layer: str,
    path: str | PathLike[str],
    *,
    mode: UpsertMode = "add",
    refresh_stats: bool = False,
    lon: str | None = None,
    lat: str | None = None,
    crs: str = "EPSG:4326",
    band: int = 1,
    max_cells_per_feature: int | None = MAX_CELLS_PER_FEATURE,
) -> LoadReport:
    """Apply a batch to an existing layer incrementally: "add" merges it like `load`, "replace"
    overwrites the base cells it touches. Only those cells and their ancestors are rewritten, and
    the result equals a full rollup. Stats, cell count and bounds are refreshed only with
    `refresh_stats` (they scan the base level)."""
    if layer_spec(conn, layer).snapshots:  # LookupError for an unknown layer
        raise ValueError(f"layer {layer!r} has snapshots: load a new snapshot instead of upserting")
    delta = sql.Identifier(DELTA)
    with conn.transaction():
        cells = sql.Identifier("gridtiles", f"cells_{layer}")
        conn.execute(sql.SQL("create temp table {} (like {} including all)").format(delta, cells))
        report = _load(conn, layer, Path(path), lon, lat, crs, band, False, max_cells_per_feature, DELTA, None)
        row = conn.execute("select gridtiles.apply_delta(%s, %s::regclass, %s)", (layer, DELTA, mode)).fetchone()
        conn.execute(sql.SQL("drop table {}").format(delta))
        if refresh_stats:
            conn.execute("select gridtiles.refresh_stats(%s)", (layer,))
    assert row is not None
    return LoadReport(report.records_read, report.records_loaded, report.skipped_null_geometry, int(row[0]))


def _load(
    conn: psycopg.Connection,
    layer: str,
    path: Path,
    lon: str | None,
    lat: str | None,
    crs: str,
    band: int,
    rollup: bool,
    max_cells: int | None,
    target: str | None,
    snapshot: date | None,
) -> LoadReport:
    """`load`, writing base cells into `target` (a table shaped like the cell table) if given."""
    if path.suffix.lower() in RASTER_SUFFIXES:
        return _load_raster(conn, layer, path, band, rollup, target, snapshot)
    return _load_vector(conn, layer, path, None, lon, lat, crs, rollup, max_cells, target, snapshot)


def load_points(
    conn: psycopg.Connection,
    layer: str,
    path: str | PathLike[str],
    *,
    lon: str | None = None,
    lat: str | None = None,
    crs: str = "EPSG:4326",
    rollup: bool = True,
) -> LoadReport:
    """Aggregate a point file into the layer's base cells.

    Reads CSV (`lon` / `lat` columns), GeoParquet, GeoJSON, Shapefile or anything else GDAL reads.
    `crs` applies when the source does not declare one. Repeated loads add up. With `rollup`, the
    coarser levels and the layer stats are rebuilt afterwards.
    """
    return _load_vector(conn, layer, Path(path), "point", lon, lat, crs, rollup, None)


def load_polygons(
    conn: psycopg.Connection,
    layer: str,
    path: str | PathLike[str],
    *,
    crs: str = "EPSG:4326",
    rollup: bool = True,
    max_cells_per_feature: int | None = MAX_CELLS_PER_FEATURE,
) -> LoadReport:
    """Split polygons over the base cells they overlap, by covered area.

    Raises `CellLimitExceeded` for a polygon spanning more than `max_cells_per_feature` base cells
    (None: no limit). On polygon layers `n` counts touching polygons, not records.
    """
    return _load_vector(conn, layer, Path(path), "polygon", None, None, crs, rollup, max_cells_per_feature)


def load_raster(
    conn: psycopg.Connection, layer: str, path: str | PathLike[str], *, band: int = 1, rollup: bool = True
) -> LoadReport:
    """Load each valid pixel of a band as a point at the pixel centre, with the source column `value`.

    Raises ValueError if pixels are larger than half a base cell; use a lower base zoom then.
    """
    return _load_raster(conn, layer, Path(path), band, rollup, None, None)


def _load_raster(
    conn: psycopg.Connection,
    layer: str,
    path: Path,
    band: int,
    rollup: bool,
    target: str | None,
    snapshot: date | None,
) -> LoadReport:
    spec = layer_spec(conn, layer)
    sources = _sources(conn, layer)
    if set(sources) - {RASTER_SOURCE}:
        raise ValueError(f"raster layers read the source column {RASTER_SOURCE!r}, not {sources}")
    with rasterio.open(path) as src:
        srid = src.crs.to_epsg() if src.crs else None
        if srid is None:
            raise ValueError(f"{path}: raster CRS is not an EPSG code")
        data = np.ma.masked_invalid(src.read(band, masked=True))  # nodata, and NaN in float rasters without one
        rows, cols = np.nonzero(~np.ma.getmaskarray(data))
        xs, ys = rasterio.transform.xy(src.transform, rows, cols, offset="center")
        _check_pixel_size(src, spec.base_zoom)
    values = data.compressed().astype(float).tolist()
    records = (
        (struct.pack("<BIIdd", 1, 0x20000001, srid, x, y), *([v] if sources else [])) for x, y, v in zip(xs, ys, values)
    )
    with conn.transaction():
        _stage(conn, sources, records)
        cells = _ingest(conn, layer, "point", None, rollup, target, snapshot)
    return LoadReport(len(values), len(values), 0, cells)


def _check_pixel_size(src: rasterio.DatasetReader, base_zoom: int) -> None:
    """Pixel size in metres (EPSG:3857) at the raster centre vs. half the base cell size."""
    cx, cy = src.width / 2, src.height / 2
    (ax, ay), (bx, by) = src.transform @ (cx, cy), src.transform @ (cx + 1, cy + 1)
    (x0, x1), (y0, y1) = rasterio.warp.transform(src.crs, "EPSG:3857", [ax, bx], [ay, by])
    pixel = max(abs(x1 - x0), abs(y1 - y0))
    cell = WORLD / 2**base_zoom
    if pixel > cell / 2:
        suggestion = math.floor(math.log2(WORLD / (2 * pixel)))
        raise ValueError(
            f"pixels ({pixel:.1f} m) exceed half a z{base_zoom} cell ({cell:.1f} m): use base_zoom <= {suggestion}"
        )


def _sources(conn: psycopg.Connection, layer: str) -> list[str]:
    """Source columns a layer's values read: each value's `from` and `weight`."""
    spec = layer_spec(conn, layer)
    sources = list(dict.fromkeys(s for v in spec.values if v.agg != "count" for s in (v.from_, v.weight) if s))
    if clash := [s for s in sources if s in RESERVED or s.startswith("_")]:
        raise ValueError(f"source columns {clash} are reserved: {sorted(RESERVED)} and names starting with '_'")
    return sources


def _load_vector(
    conn: psycopg.Connection,
    layer: str,
    path: Path,
    kind: Kind | None,
    lon: str | None,
    lat: str | None,
    crs: str,
    rollup: bool,
    max_cells: int | None,
    target: str | None = None,
    snapshot: date | None = None,
) -> LoadReport:
    sources = _sources(conn, layer)
    counts = {"read": 0, "loaded": 0}
    found: list[Kind] = [kind] if kind else []

    def records(srid: int, batches: Iterable[pa.RecordBatch]) -> Iterator[tuple[object, ...]]:
        for batch in batches:
            values = [pc.cast(batch.column(s), pa.float64()).to_pylist() for s in sources]
            for i, wkb in enumerate(batch.column(0).to_pylist()):
                if wkb is not None:
                    row_kind, ewkb = _ewkb(wkb, srid, counts["read"] + i)
                    if not found:
                        found.append(row_kind)
                    elif row_kind != found[0]:
                        raise UnsupportedGeometry(f"row {counts['read'] + i}: a {row_kind} among {found[0]}s")
                    if ewkb is None:
                        continue
                    counts["loaded"] += 1
                    yield (ewkb, *(col[i] for col in values))
            counts["read"] += batch.num_rows

    with conn.transaction(), _open(path, sources, lon, lat, crs) as (srid, batches):
        _stage(conn, sources, records(srid, batches))
        cells = _ingest(conn, layer, found[0] if found else "point", max_cells, rollup, target, snapshot)
    return LoadReport(counts["read"], counts["loaded"], counts["read"] - counts["loaded"], cells)


def _stage(conn: psycopg.Connection, sources: list[str], records: Iterable[tuple[object, ...]]) -> None:
    """COPY (ewkb, *sources) records into a temporary staging table."""
    staging = sql.Identifier(STAGING)
    columns = sql.SQL("").join(sql.SQL(", {} double precision").format(sql.Identifier(s)) for s in sources)
    conn.execute(sql.SQL("create temp table {} (geom geometry{})").format(staging, columns))
    with conn.cursor().copy(sql.SQL("copy {} from stdin (format binary)").format(staging)) as copy:
        copy.set_types(["bytea"] + ["float8"] * len(sources))
        for record in records:
            copy.write_row(record)


def _ingest(
    conn: psycopg.Connection,
    layer: str,
    kind: Kind,
    max_cells: int | None,
    rollup: bool,
    target: str | None = None,
    snapshot: date | None = None,
) -> int:
    """Aggregate the staging table into base cells (or `target`), drop it and optionally roll up."""
    try:
        if kind == "polygon":
            query = "select gridtiles.ingest_polygons(%s, %s::regclass, %s, %s::regclass, %s)"
            row = conn.execute(query, (layer, STAGING, max_cells, target, snapshot)).fetchone()
        else:
            query = "select gridtiles.ingest_points(%s, %s::regclass, %s::regclass, %s)"
            row = conn.execute(query, (layer, STAGING, target, snapshot)).fetchone()
    except errors.ProgramLimitExceeded as e:
        message = str(e.diag.message_primary)
        if "max_cells_per_feature" not in message:  # another limit, e.g. an index row too large
            raise
        raise CellLimitExceeded(message) from e
    except (errors.InvalidParameterValue, errors.FeatureNotSupported) as e:  # snapshot missing, a diff layer
        raise ValueError(str(e.diag.message_primary)) from e
    conn.execute(sql.SQL("drop table {}").format(sql.Identifier(STAGING)))
    if rollup:
        conn.execute("select gridtiles.rollup(%s, %s)", (layer, snapshot))
    assert row is not None
    return int(row[0])


@contextmanager
def _open(
    path: Path, columns: list[str], lon: str | None, lat: str | None, crs: str
) -> Iterator[tuple[int, Iterator[pa.RecordBatch]]]:
    """Yield the source SRID and record batches whose first column is the geometry as WKB."""
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
