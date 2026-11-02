"""Read point, polygon and raster sources as records for any backend.

A record is `(wkb, *values)`: the geometry as 2D WKB in EPSG:3857, then one float (or None) per
requested source column. Every source is projected here, once, with the spherical formula and
latitude clamp of `quadkey.to_mercator`, so all backends see identical coordinates. Vector files
stream in batches (GeoParquet via pyarrow, everything else via GDAL); rasters become points at
their valid pixel centres with the source column `value`.
"""

import json
import math
import struct
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from os import PathLike
from pathlib import Path
from typing import Literal

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import pyogrio
import rasterio
import rasterio.warp

from gridtiles.quadkey import WORLD, to_mercator
from gridtiles.spec import LayerSpec

BATCH_SIZE = 100_000
LON_NAMES = "lon,lng,longitude,x"
LAT_NAMES = "lat,latitude,y"
RASTER_SUFFIXES = (".tif", ".tiff", ".vrt", ".jp2", ".nc")
RASTER_SOURCE = "value"  # the source column name of a raster band
MAX_CELLS_PER_FEATURE = 1_000_000

Kind = Literal["point", "polygon"]
Record = tuple[object, ...]
SourcePath = str | PathLike[str]
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


class Records:
    """Records of one source, in EPSG:3857. Iterate once; afterwards `kind`, `read` and `loaded`
    are final. The first geometry decides the kind (unless `expect` fixes it); every row must match.
    """

    def __init__(self, srid: int, batches: Iterator[pa.RecordBatch], columns: list[str], expect: Kind | None) -> None:
        self.kind = expect
        self.read = self.loaded = 0
        self._srid, self._batches, self._columns = srid, batches, columns

    def __iter__(self) -> Iterator[Record]:
        for batch in self._batches:
            values = [pc.cast(batch.column(c), pa.float64()).to_pylist() for c in self._columns]
            for i, wkb in enumerate(batch.column(0).to_pylist()):
                if wkb is not None:
                    yield (self._mercator(wkb, self.read + i), *(col[i] for col in values))
                    self.loaded += 1
            self.read += batch.num_rows

    def _mercator(self, wkb: bytes, row: int) -> bytes:
        base, parts, xs, ys = _parse(wkb, row)
        kind = _WKB_KINDS[base]
        if self.kind is None:
            self.kind = kind
        elif kind != self.kind:
            raise UnsupportedGeometry(f"row {row}: a {kind} among {self.kind}s")
        if self._srid not in (3857, 4326):
            xs, ys = rasterio.warp.transform(f"EPSG:{self._srid}", "EPSG:4326", xs, ys)
        coords = iter(zip(xs, ys) if self._srid == 3857 else (to_mercator(x, y) for x, y in zip(xs, ys)))
        return _build(base, parts, coords)


def source_columns(spec: LayerSpec) -> list[str]:
    """Source columns a layer's values read: each value's `from` and `weight`."""
    return list(dict.fromkeys(s for v in spec.values if v.agg != "count" for s in (v.from_, v.weight) if s))


def is_raster(path: SourcePath) -> bool:
    return Path(path).suffix.lower() in RASTER_SUFFIXES


@contextmanager
def read_vector(
    path: SourcePath,
    columns: list[str],
    *,
    expect: Kind | None = None,
    lon: str | None = None,
    lat: str | None = None,
    crs: str = "EPSG:4326",
) -> Iterator[Records]:
    """Records of a vector file. `crs` applies when the source declares none."""
    path = Path(path)
    if path.suffix.lower() in (".parquet", ".geoparquet"):
        source = pq.ParquetFile(path)
        geo = json.loads(source.schema_arrow.metadata[b"geo"])
        geometry = geo["primary_column"]
        column = geo["columns"][geometry]
        # GeoParquet: no "crs" key means OGC:CRS84, null means unknown, otherwise PROJJSON.
        if "crs" not in column:
            srid = 4326
        elif column["crs"] is None:
            srid = srid_of(crs)
        else:
            ident = column["crs"].get("id", {})
            srid = srid_of(f"{ident.get('authority')}:{ident.get('code')}")
        yield Records(srid, source.iter_batches(BATCH_SIZE, columns=[geometry, *columns]), columns, expect)
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
        batches = (batch.select([geometry, *columns]) for batch in reader)
        yield Records(srid_of(meta["crs"] or crs), batches, columns, expect)


@contextmanager
def open_records(
    path: SourcePath,
    spec: LayerSpec,
    *,
    kind: Kind | None = None,
    lon: str | None = None,
    lat: str | None = None,
    crs: str = "EPSG:4326",
    band: int = 1,
) -> Iterator[Records]:
    """Records of any source for a layer: a raster (by extension) or a vector file."""
    columns = source_columns(spec)
    if is_raster(path):
        yield read_raster(path, columns, spec.base_zoom, band=band)
    else:
        with read_vector(path, columns, expect=kind, lon=lon, lat=lat, crs=crs) as records:
            yield records


def read_raster(path: SourcePath, columns: list[str], base_zoom: int, *, band: int = 1) -> Records:
    """Valid pixels of a band as points at their centres, with the source column `value`.

    Raises ValueError if pixels are larger than half a base cell; use a lower base zoom then.
    """
    if set(columns) - {RASTER_SOURCE}:
        raise ValueError(f"raster layers read the source column {RASTER_SOURCE!r}, not {columns}")
    with rasterio.open(path) as src:
        srid = src.crs.to_epsg() if src.crs else None
        if srid is None:
            raise ValueError(f"{path}: raster CRS is not an EPSG code")
        _check_pixel_size(src, base_zoom)
        data = src.read(band, masked=True)
        rows, cols = np.nonzero(~np.ma.getmaskarray(data))
        xs, ys = rasterio.transform.xy(src.transform, rows, cols, offset="center")
    wkb = [struct.pack("<BIdd", 1, 1, x, y) for x, y in zip(xs, ys)]
    batch = pa.record_batch({"geometry": wkb, **({RASTER_SOURCE: data.compressed().astype(float)} if columns else {})})
    return Records(srid, iter([batch]), columns, "point")


Parts = list[list[int]]  # ring sizes per polygon part; empty for a point


def _parse(wkb: bytes, row: int) -> tuple[int, Parts, list[float], list[float]]:
    """Base type (1, 3 or 6), polygon parts and x / y coordinates of ISO WKB or EWKB (Z/M dropped)."""
    xs: list[float] = []
    ys: list[float] = []
    parts: Parts = []

    def geometry(pos: int) -> tuple[int, int]:
        order = "<" if wkb[pos] == 1 else ">"
        (code,) = struct.unpack_from(order + "I", wkb, pos + 1)
        pos += 9 if code & 0x20000000 else 5  # EWKB carries an SRID
        flavour, base = divmod(code & 0xFFFF, 1000)
        dims = 2 + (bool(code & 0x80000000) or flavour in (1, 3)) + (bool(code & 0x40000000) or flavour in (2, 3))

        def count(pos: int) -> tuple[int, int]:
            return struct.unpack_from(order + "I", wkb, pos)[0], pos + 4

        def coords(n: int, pos: int) -> int:
            for _ in range(n):
                x, y = struct.unpack_from(order + "dd", wkb, pos)
                xs.append(x)
                ys.append(y)
                pos += 8 * dims
            return pos

        if base == 1:
            return base, coords(1, pos)
        if base == 3:
            rings, pos = count(pos)
            parts.append([])
            for _ in range(rings):
                m, pos = count(pos)
                parts[-1].append(m)
                pos = coords(m, pos)
            return base, pos
        if base == 6:
            n, pos = count(pos)
            for _ in range(n):
                _, pos = geometry(pos)
            return base, pos
        raise UnsupportedGeometry(f"row {row}: only points and polygons are supported, got WKB geometry type {code}")

    base, _ = geometry(0)
    return base, parts, xs, ys


def _build(base: int, parts: Parts, coords: Iterator[tuple[float, float]]) -> bytes:
    """2D little-endian WKB: a point, or a multipolygon of `parts`, with coordinates in order."""
    if base == 1:
        return struct.pack("<BIdd", 1, 1, *next(coords))
    polygons = (
        struct.pack("<BII", 1, 3, len(rings))
        + b"".join(struct.pack("<I", m) + b"".join(struct.pack("<dd", *next(coords)) for _ in range(m)) for m in rings)
        for rings in parts
    )
    return struct.pack("<BII", 1, 6, len(parts)) + b"".join(polygons)


def srid_of(crs: str) -> int:
    authority, _, code = crs.partition(":")
    if authority.upper() == "OGC" and code.upper() == "CRS84":
        return 4326
    if authority.upper() != "EPSG" or not code.isdigit():
        raise ValueError(f"CRS {crs!r} is not an EPSG code")
    return int(code)


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
