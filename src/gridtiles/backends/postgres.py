"""PostgreSQL + PostGIS, the reference backend: cells, rollups and MVT encoding run in the database
(the SQL in `gridtiles/sql/`). Records reach it by binary COPY into a temporary staging table.
"""

import struct
import threading
from collections.abc import Iterable, Iterator
from datetime import date
from importlib.resources import files
from typing import Any, Self
from urllib.parse import quote

import psycopg
from psycopg import errors, sql
from psycopg.types.json import Jsonb

from gridtiles import query as region_stats
from gridtiles.backends.base import Capability, LayerExists, UpsertMode, parse_ref
from gridtiles.layer import Layer
from gridtiles.sources import (
    MAX_CELLS_PER_FEATURE,
    CellLimitExceeded,
    Kind,
    LoadReport,
    Record,
    Records,
    SourcePath,
    open_records,
    source_columns,
)
from gridtiles.spec import LayerSpec

STAGING = "_gridtiles_staging"
DELTA = "_gridtiles_delta"


def install(conn: psycopg.Connection) -> str:
    """Apply the packaged `sql/*.sql` files in order (idempotent) and record the schema version."""
    for sql_file in sorted(files("gridtiles").joinpath("sql").iterdir(), key=lambda f: f.name):
        if sql_file.name.endswith(".sql"):
            conn.execute(sql_file.read_text())
    row = conn.execute(
        "insert into gridtiles.meta values ('schema_version', gridtiles.version())"
        " on conflict (key) do update set value = excluded.value returning value"
    ).fetchone()
    assert row is not None
    return str(row[0])


class PostgresBackend:
    capabilities: frozenset[Capability] = frozenset({"live", "incremental", "native_mvt", "snapshots"})

    def __init__(self, target: str | psycopg.Connection) -> None:
        """Open a URL (autocommit; each tile thread gets its own connection) or wrap a connection
        (its transaction is used; tiles share it)."""
        if isinstance(target, str):
            self.conn, self._own = psycopg.connect(target, autocommit=True), True
        else:
            self.conn, self._own = target, False
        info = self.conn.info
        self.url = f"postgresql://{quote(info.user)}:{quote(info.password)}@{info.host}:{info.port}/{quote(info.dbname)}"
        self._local = threading.local()
        self._tile_conns: list[psycopg.Connection] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        for conn in self._tile_conns:
            conn.close()
        if self._own:
            self.conn.close()

    def install(self) -> str:
        return install(self.conn)

    def create_layer(self, spec: LayerSpec) -> None:
        try:
            with self.conn.transaction():
                self.conn.execute(
                    "select gridtiles.create_layer(%s)", (Jsonb(spec.model_dump(mode="json", exclude_none=True)),)
                )
        except errors.UniqueViolation as e:
            raise LayerExists(spec.name) from e

    def drop_layer(self, name: str) -> None:
        with self.conn.transaction():
            self.conn.execute("select gridtiles.drop_layer(%s)", (name,))

    def spec(self, name: str) -> LayerSpec:
        row = self.conn.execute("select spec from gridtiles.layers where name = %s", (name,)).fetchone()
        if row is None:
            raise LookupError(f"layer {name!r} does not exist")
        return LayerSpec.model_validate(row[0])

    def list_layers(self, names: list[str] | None = None) -> list[Layer]:
        rows = self.conn.execute(
            "select spec, stats, cells, bounds from gridtiles.layers"
            " where %(names)s::text[] is null or name = any(%(names)s) order by name",
            {"names": names},
        ).fetchall()
        specs = [LayerSpec.model_validate(r[0]) for r in rows]
        found = [
            Layer(spec, stats, cells, tuple(b) if b else None, self._snapshots(spec))
            for spec, (_, stats, cells, b) in zip(specs, rows)
        ]
        missing = set(names or []) - {layer.name for layer in found}
        if missing:
            raise LookupError(f"unknown layers: {', '.join(sorted(missing))}")
        return found

    def _snapshots(self, spec: LayerSpec) -> tuple[str, ...]:
        if not spec.snapshots:
            return ()
        table = sql.Identifier("gridtiles", f"cells_{spec.name}")
        query = sql.SQL("select distinct snapshot from {} where z = %s order by 1").format(table)
        return tuple(d.isoformat() for (d,) in self.conn.execute(query, (spec.min_zoom,)).fetchall())

    def load(
        self,
        layer: str,
        path: SourcePath,
        *,
        kind: Kind | None = None,
        lon: str | None = None,
        lat: str | None = None,
        crs: str = "EPSG:4326",
        band: int = 1,
        rollup: bool = True,
        max_cells_per_feature: int | None = MAX_CELLS_PER_FEATURE,
        snapshot: date | None = None,
    ) -> LoadReport:
        spec = self.spec(layer)
        with self.conn.transaction():
            with open_records(path, spec, kind=kind, lon=lon, lat=lat, crs=crs, band=band) as records:
                cells = self._ingest(layer, records, source_columns(spec), max_cells_per_feature, snapshot=snapshot)
            if rollup:
                self.conn.execute("select gridtiles.rollup(%s, %s)", (layer, snapshot))
        return LoadReport(records.read, records.loaded, records.read - records.loaded, cells)

    def upsert(
        self,
        layer: str,
        path: SourcePath,
        *,
        mode: UpsertMode = "add",
        refresh_stats: bool = False,
        kind: Kind | None = None,
        lon: str | None = None,
        lat: str | None = None,
        crs: str = "EPSG:4326",
        band: int = 1,
        max_cells_per_feature: int | None = MAX_CELLS_PER_FEATURE,
    ) -> LoadReport:
        spec = self.spec(layer)
        if spec.snapshots:
            raise ValueError(f"layer {layer!r} has snapshots: load a new snapshot instead of upserting")
        delta = sql.Identifier(DELTA)
        with self.conn.transaction():
            cells = sql.Identifier("gridtiles", f"cells_{layer}")
            self.conn.execute(sql.SQL("create temp table {} (like {} including all)").format(delta, cells))
            with open_records(path, spec, kind=kind, lon=lon, lat=lat, crs=crs, band=band) as records:
                self._ingest(layer, records, source_columns(spec), max_cells_per_feature, target=DELTA)
            row = self.conn.execute("select gridtiles.apply_delta(%s, %s::regclass, %s)", (layer, DELTA, mode)).fetchone()
            self.conn.execute(sql.SQL("drop table {}").format(delta))
            if refresh_stats:
                self.conn.execute("select gridtiles.refresh_stats(%s)", (layer,))
        assert row is not None
        return LoadReport(records.read, records.loaded, records.read - records.loaded, int(row[0]))

    def _ingest(
        self,
        layer: str,
        records: Records,
        columns: list[str],
        max_cells: int | None,
        target: str | None = None,
        snapshot: date | None = None,
    ) -> int:
        """COPY records into a staging table, then aggregate it into base cells (or `target`) in SQL."""
        staging = sql.Identifier(STAGING)
        typed = sql.SQL("").join(sql.SQL(", {} double precision").format(sql.Identifier(c)) for c in columns)
        self.conn.execute(sql.SQL("create temp table {} (geom geometry{})").format(staging, typed))
        with self.conn.cursor().copy(sql.SQL("copy {} from stdin (format binary)").format(staging)) as copy:
            copy.set_types(["bytea"] + ["float8"] * len(columns))
            for record in _with_srid(records, 3857):  # sources deliver EPSG:3857
                copy.write_row(record)
        try:
            if records.kind == "polygon":
                query = "select gridtiles.ingest_polygons(%s, %s::regclass, %s, %s::regclass, %s)"
                row = self.conn.execute(query, (layer, STAGING, max_cells, target, snapshot)).fetchone()
            else:
                query = "select gridtiles.ingest_points(%s, %s::regclass, %s::regclass, %s)"
                row = self.conn.execute(query, (layer, STAGING, target, snapshot)).fetchone()
        except errors.ProgramLimitExceeded as e:
            raise CellLimitExceeded(str(e.diag.message_primary)) from e
        except (errors.InvalidParameterValue, errors.FeatureNotSupported) as e:
            raise ValueError(str(e.diag.message_primary)) from e
        self.conn.execute(sql.SQL("drop table {}").format(staging))
        assert row is not None
        return int(row[0])

    def rollup(self, layer: str) -> None:
        self.conn.execute("select gridtiles.rollup(%s)", (layer,))

    def tile(self, layer: str, z: int, x: int, y: int, snapshot: date | None = None) -> bytes:
        query = "select gridtiles.mvt(%s, %s, %s, %s, %s)"
        row = self._tile_conn().execute(query, (layer, z, x, y, snapshot)).fetchone()
        assert row is not None
        return bytes(row[0])

    def _tile_conn(self) -> psycopg.Connection:
        if not self._own:
            return self.conn
        if not hasattr(self._local, "conn"):
            self._local.conn = psycopg.connect(self.url, autocommit=True)
            self._tile_conns.append(self._local.conn)
        conn: psycopg.Connection = self._local.conn
        return conn

    def tiles_with_data(self, layer: str) -> Iterator[tuple[int, int, int]]:
        """Tiles with data in the published zoom range; of the latest snapshot on snapshot layers."""
        (info,) = self.list_layers([layer])
        spec, table = info.spec, sql.Identifier("gridtiles", f"cells_{layer}")
        latest = sql.SQL(" and snapshot = %(latest)s" if info.snapshots else "")
        for t in range(info.tile_zooms[0], info.tile_zooms[1] + 1):
            c = spec.cell_zoom(t)
            query = sql.SQL(
                "select xy.x, xy.y from (select distinct qk >> %(shift)s as tile from {} where z = %(c)s{}) d,"
                " gridtiles.qk_to_xy(%(t)s, tile) xy order by tile"
            ).format(table, latest)
            params = {"shift": 2 * (c - t), "c": c, "t": t, "latest": info.snapshots[-1] if info.snapshots else None}
            yield from ((t, x, y) for x, y in self.conn.execute(query, params).fetchall())

    def diff(self, a: str, b: str, name: str) -> None:
        (a_name, a_day), (b_name, b_day) = parse_ref(a), parse_ref(b)
        try:
            with self.conn.transaction():
                self.conn.execute("select gridtiles.diff(%s, %s, %s, %s, %s)", (a_name, a_day, b_name, b_day, name))
        except errors.NoDataFound as e:
            raise LookupError(f"unknown layer in {a} or {b}") from e
        except errors.UniqueViolation as e:
            raise LayerExists(name) from e
        except errors.InvalidParameterValue as e:
            raise ValueError(str(e.diag.message_primary)) from e

    def query(
        self,
        layer: str,
        *,
        prefix: str | None = None,
        bbox: tuple[float, float, float, float] | None = None,
        polygon: dict[str, Any] | None = None,
        zoom: int | None = None,
        snapshot: date | None = None,
    ) -> dict[str, Any]:
        spec = self.spec(layer)
        q = region_stats.build(spec, prefix=prefix, bbox=bbox, polygon=polygon, zoom=zoom, snapshot=snapshot)
        row = self._tile_conn().execute(q.sql.encode()).fetchone()
        assert row is not None
        return region_stats.result(spec, q, row)

    def manifest(self, layer: str) -> dict[str, Any]:
        (info,) = self.list_layers([layer])
        return info.manifest()


def _with_srid(records: Iterable[Record], srid: int) -> Iterator[Record]:
    """Embed the SRID into each WKB (EWKB), so PostGIS reads geometries directly."""
    for wkb, *values in records:
        assert isinstance(wkb, bytes)
        order = "<" if wkb[0] == 1 else ">"
        (code,) = struct.unpack_from(order + "I", wkb, 1)
        yield (wkb[:1] + struct.pack(order + "II", code | 0x20000000, srid) + wkb[5:], *values)
