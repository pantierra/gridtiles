"""DuckDB + spatial: the same cell tables in one `.duckdb` file, for laptops, CI and notebooks.

Quadkey functions are SQL macros mirroring `sql/010_quadkey.sql`; the per-aggregator SQL is
generated from `LayerSpec.columns()`; tiles are encoded in Python (`gridtiles.mvt`). Rollups are
full rebuilds, and there is no live tile server.
"""

import json
import threading
from collections.abc import Iterator
from datetime import date
from typing import Any, Self

import duckdb
import pyarrow as pa

from gridtiles import mvt, quadkey
from gridtiles import query as region_stats
from gridtiles.backends.base import Capability, LayerExists, MissingCapability, UpsertMode
from gridtiles.layer import Layer
from gridtiles.sources import (
    MAX_CELLS_PER_FEATURE,
    CellLimitExceeded,
    Kind,
    LoadReport,
    Records,
    SourcePath,
    open_records,
    source_columns,
)
from gridtiles.spec import Column, LayerSpec

HALF = quadkey.HALF_WORLD
WORLD = quadkey.WORLD
BREAKS = [i / 8 for i in range(1, 8)]

# Bit masks of the Morton interleave, as decimals (DuckDB reads 0x10 as 0 AS x10).
_SPREAD = [
    (16, 0x0000FFFF0000FFFF),
    (8, 0x00FF00FF00FF00FF),
    (4, 0x0F0F0F0F0F0F0F0F),
    (2, 0x3333333333333333),
    (1, 0x5555555555555555),
]
_COMPACT = [
    (1, 0x3333333333333333),
    (2, 0x0F0F0F0F0F0F0F0F),
    (4, 0x00FF00FF00FF00FF),
    (8, 0x0000FFFF0000FFFF),
    (16, 0x00000000FFFFFFFF),
]


def _nest(template: str, steps: list[tuple[int, int]], inner: str) -> str:
    for shift, mask in steps:
        inner = template.format(inner=inner, shift=shift, mask=mask)
    return inner


SPREAD = _nest("((({inner}) | (({inner}) << {shift})) & {mask})", _SPREAD, "v::bigint")
COMPACT = _nest("((({inner}) | (({inner}) >> {shift})) & {mask})", _COMPACT, f"(v::bigint & {0x5555555555555555})")
SCHEMA = f"""
create schema if not exists gridtiles;
create table if not exists gridtiles.meta (key varchar primary key, value varchar not null);
create table if not exists gridtiles.layers (
    name varchar primary key, spec json not null, zb smallint not null, zmin smallint not null,
    detail smallint not null, stats json, cells bigint, bounds double[],
    created_at timestamp default current_timestamp, updated_at timestamp default current_timestamp
);
create or replace macro gridtiles._spread(v) as {SPREAD};
create or replace macro gridtiles._compact(v) as {COMPACT};
create or replace macro gridtiles._qk(x, y) as gridtiles._spread(x) | (gridtiles._spread(y) << 1);
create or replace macro gridtiles._size(z) as {WORLD}::double / (1::bigint << z);
-- Tile index of a distance from the west / north edge, clamped to the grid.
create or replace macro gridtiles._index(m, z) as
    least(greatest(floor(m / gridtiles._size(z)), 0), (1::bigint << z) - 1)::bigint;
create or replace macro gridtiles._from_mercator(mx, my, z) as
    gridtiles._qk(gridtiles._index(mx + {HALF}, z), gridtiles._index({HALF} - my, z));
create or replace macro gridtiles._tile_envelope(z, x, y) as ST_MakeEnvelope(
    -{HALF} + x * gridtiles._size(z), {HALF} - (y + 1) * gridtiles._size(z),
    -{HALF} + (x + 1) * gridtiles._size(z), {HALF} - y * gridtiles._size(z));
"""


class DuckDBBackend:
    capabilities: frozenset[Capability] = frozenset()

    def __init__(self, path: str) -> None:
        self.url = path
        self.conn = duckdb.connect(path)
        self.conn.execute("install spatial; load spatial;")
        self._local = threading.local()
        self._layouts: dict[str, tuple[LayerSpec, dict[str, str]]] = {}

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.conn.close()

    def install(self) -> str:
        from gridtiles import __version__

        self.conn.execute(SCHEMA)
        self.conn.execute("insert or replace into gridtiles.meta values ('schema_version', ?)", [__version__])
        return __version__

    def create_layer(self, spec: LayerSpec) -> None:
        if spec.snapshots:
            raise MissingCapability("snapshot layers need a backend with 'snapshots' (PostgreSQL)")
        if self.conn.execute("select 1 from gridtiles.layers where name = ?", [spec.name]).fetchall():
            raise LayerExists(spec.name)
        self.conn.execute(
            "insert into gridtiles.layers (name, spec, zb, zmin, detail) values (?, ?, ?, ?, ?)",
            [spec.name, spec.model_dump_json(exclude_none=True), spec.base_zoom, spec.min_zoom, spec.detail],
        )
        self._create_cell_table(f"gridtiles.cells_{spec.name}", spec)

    def _create_cell_table(self, table: str, spec: LayerSpec, temporary: bool = False) -> None:
        columns = "".join(f", {c.name} double" for c in spec.columns())
        kind = "temp table" if temporary else "table"
        self.conn.execute(f"create {kind} {table} (z smallint, qk bigint, n bigint not null{columns}, primary key (z, qk))")

    def drop_layer(self, name: str) -> None:
        self.spec(name)
        self._layouts.pop(name, None)
        self.conn.execute("delete from gridtiles.layers where name = ?", [name])
        self.conn.execute(f"drop table gridtiles.cells_{name}")

    def spec(self, name: str, conn: duckdb.DuckDBPyConnection | None = None) -> LayerSpec:
        rows = (conn or self.conn).execute("select spec from gridtiles.layers where name = ?", [name]).fetchall()
        if not rows:
            raise LookupError(f"layer {name!r} does not exist")
        return LayerSpec.model_validate_json(rows[0][0])

    def list_layers(self, names: list[str] | None = None) -> list[Layer]:
        rows = self.conn.execute("select spec, stats, cells, bounds from gridtiles.layers order by name").fetchall()
        found = [
            Layer(LayerSpec.model_validate_json(spec), json.loads(stats) if stats else None, cells, tuple(b) if b else None)
            for spec, stats, cells, b in rows
        ]
        if names is None:
            return found
        missing = set(names) - {layer.name for layer in found}
        if missing:
            raise LookupError(f"unknown layers: {', '.join(sorted(missing))}")
        return [layer for layer in found if layer.name in names]

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
        if snapshot is not None:
            raise MissingCapability("snapshots need a backend with 'snapshots' (PostgreSQL)")
        spec = self.spec(layer)
        self.conn.begin()
        try:
            with open_records(path, spec, kind=kind, lon=lon, lat=lat, crs=crs, band=band) as records:
                cells = self._ingest(spec, records, source_columns(spec), max_cells_per_feature)
            if rollup:
                self.rollup(layer)
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise
        return LoadReport(records.read, records.loaded, records.read - records.loaded, cells)

    def _ingest(
        self, spec: LayerSpec, records: Records, columns: list[str], max_cells: int | None, target: str | None = None
    ) -> int:
        """Aggregate records into base cells of the layer, or of `target` (a table shaped like it)."""
        rows = list(records)
        staging = pa.table(
            {
                "wkb": pa.array([r[0] for r in rows], pa.binary()),
                **{c: pa.array([r[i + 1] for r in rows], pa.float64()) for i, c in enumerate(columns)},
            }
        )
        self.conn.register("_staging", staging)
        try:
            if records.kind == "polygon":
                return self._ingest_polygons(spec, max_cells, target)
            point = "ST_GeomFromWKB(wkb)"
            cell = f"gridtiles._from_mercator(ST_X({point}), ST_Y({point}), {spec.base_zoom})"
            return self._insert_cells(spec, f"select {cell} as _cell, * from _staging", _aggregates(spec), target)
        finally:
            self.conn.unregister("_staging")

    def _ingest_polygons(self, spec: LayerSpec, max_cells: int | None, target: str | None) -> int:
        """Split polygons over the base cells their bounding box covers; mirrors `ingest_polygons`."""
        zb = spec.base_zoom
        self.conn.execute(f"""
            create or replace temp table _polygons as
            select *,
                gridtiles._index(ST_XMin(_g) + {HALF}, {zb}) as _x0, gridtiles._index(ST_XMax(_g) + {HALF}, {zb}) as _x1,
                gridtiles._index({HALF} - ST_YMax(_g), {zb}) as _y0, gridtiles._index({HALF} - ST_YMin(_g), {zb}) as _y1
            from (select *, ST_GeomFromWKB(wkb) as _g from _staging)""")
        try:
            if max_cells is not None:
                ((widest,),) = self.conn.execute("select max((_x1 - _x0 + 1) * (_y1 - _y0 + 1)) from _polygons").fetchall()
                if widest and widest > max_cells:
                    raise CellLimitExceeded(
                        f"a polygon spans {widest} cells at z{zb}, above max_cells_per_feature {max_cells}"
                    )
            source = f"""
                select *, gridtiles._qk(_x, _y) as _cell, _a / ST_Area(_g) as _share, _a as _area
                from (
                    select *, ST_Area(ST_Intersection(_g, gridtiles._tile_envelope({zb}, _x, _y))) as _a
                    from (select *, unnest(range(_y0, _y1 + 1)) as _y
                          from (select *, unnest(range(_x0, _x1 + 1)) as _x from _polygons))
                )
                where _a > 0"""
            return self._insert_cells(spec, source, _aggregates(spec, "_share", "_area"), target)
        finally:
            self.conn.execute("drop table _polygons")

    def _insert_cells(self, spec: LayerSpec, source: str, aggregates: str, target: str | None = None) -> int:
        """Aggregate `source` (with a `_cell` column) into base cells, merging with existing ones."""
        self.conn.execute(f"""
            create or replace temp table _cells as
            select {spec.base_zoom}::smallint as z, _cell as qk, count(*) as n{aggregates}
            from ({source}) group by _cell""")
        try:
            self._merge_cells(spec, "_cells", target or f"gridtiles.cells_{spec.name}", "add")
            return self._count("_cells", schema="temp")
        finally:
            self.conn.execute("drop table _cells")

    def _merge_cells(self, spec: LayerSpec, source: str, target: str, mode: UpsertMode) -> None:
        """Insert cells from `source` into `target`: merged per aggregator ("add") or overwriting ("replace")."""
        cols = spec.columns()
        names = "".join(f", {c.name}" for c in cols)
        if mode == "add":
            assign = "n = n + excluded.n" + "".join(f", {c.name} = {_merge(c, c.name, 'excluded.' + c.name)}" for c in cols)
        else:
            assign = "n = excluded.n" + "".join(f", {c.name} = excluded.{c.name}" for c in cols)
        self.conn.execute(f"""
            insert into {target} (z, qk, n{names}) select z, qk, n{names} from {source}
            on conflict (z, qk) do update set {assign}""")

    def _count(self, table: str, schema: str = "gridtiles") -> int:
        ((n,),) = self.conn.execute(f"select count(*) from {schema}.{table}").fetchall()
        return int(n)

    def rollup(self, layer: str) -> None:
        """Rebuild levels zb - 1 .. zmin from their children, then stats and bounds; mirrors `rollup`."""
        self._rebuild_levels(self.spec(layer))
        self.refresh_stats(layer)

    def _rebuild_levels(self, spec: LayerSpec) -> None:
        cells, zb = f"gridtiles.cells_{spec.name}", spec.base_zoom
        names = "".join(f", {c.name}" for c in spec.columns())
        merges = "".join(f", {c.merge}({c.name}{' order by qk' if c.merge == 'sum' else ''})" for c in spec.columns())
        self.conn.execute(f"delete from {cells} where z < {zb}")
        for z in range(zb - 1, spec.min_zoom - 1, -1):
            self.conn.execute(f"""
                insert into {cells} (z, qk, n{names})
                select {z}, qk >> 2, sum(n){merges} from {cells} where z = {z + 1} group by qk >> 2""")

    def refresh_stats(self, layer: str) -> None:
        """Stats, row count and bounds; mirrors `gridtiles.refresh_stats`."""
        spec = self.spec(layer)
        cells, zb = f"gridtiles.cells_{layer}", spec.base_zoom
        stats = {}
        for name, expr in _properties(spec).items():
            ((total, low, high, breaks),) = self.conn.execute(f"""
                select sum(v), min(v), max(v), quantile_cont(v, {BREAKS})
                from (select {expr} as v from {cells} where z = {zb})""").fetchall()
            stats[name] = {"total": total, "min": low, "max": high, "breaks": breaks}
        ((bounds,),) = self.conn.execute(f"""
            select [ST_XMin(nw), ST_YMin(se), ST_XMax(se), ST_YMax(nw)]
            from (
                select ST_Transform(gridtiles._tile_envelope({zb}, x0, y0), 'EPSG:3857', 'EPSG:4326', always_xy := true) nw,
                       ST_Transform(gridtiles._tile_envelope({zb}, x1, y1), 'EPSG:3857', 'EPSG:4326', always_xy := true) se
                from (
                    select min(gridtiles._compact(qk)) x0, min(gridtiles._compact(qk >> 1)) y0,
                           max(gridtiles._compact(qk)) x1, max(gridtiles._compact(qk >> 1)) y1
                    from {cells} where z = {zb}
                )
            )""").fetchall()
        self.conn.execute(
            "update gridtiles.layers set stats = ?, cells = ?, bounds = ?, updated_at = current_timestamp where name = ?",
            [json.dumps(stats), self._count(f"cells_{layer}"), bounds, layer],
        )

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
        """Merge the batch into the base cells, then rebuild all levels (not incremental)."""
        spec = self.spec(layer)
        self.conn.begin()
        try:
            self._create_cell_table("_delta", spec, temporary=True)
            with open_records(path, spec, kind=kind, lon=lon, lat=lat, crs=crs, band=band) as records:
                self._ingest(spec, records, source_columns(spec), max_cells_per_feature, target="_delta")
            self._merge_cells(spec, "_delta", f"gridtiles.cells_{layer}", mode)
            written = self._count("_delta", schema="temp")
            self.conn.execute("drop table _delta")
            self._rebuild_levels(spec)
            if refresh_stats:
                self.refresh_stats(layer)
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise
        return LoadReport(records.read, records.loaded, records.read - records.loaded, written)

    def tile(self, layer: str, z: int, x: int, y: int, snapshot: date | None = None) -> bytes:
        """Cells at c(t) in the tile's key range, encoded in Python; mirrors `gridtiles.mvt`."""
        if snapshot is not None:
            raise MissingCapability("snapshots need a backend with 'snapshots' (PostgreSQL)")
        cursor = self._cursor()
        spec, properties = self._tile_layout(layer, cursor)
        tile = quadkey.from_xyz(z, x, y)
        c = spec.cell_zoom(z)
        if c >= z:
            keys = quadkey.child_range(z, tile, c)
        else:
            ancestor = quadkey.parent(z, tile, c)
            keys = range(ancestor, ancestor + 1)
        rows = cursor.execute(
            f"select qk, n{''.join(f', {e}' for e in properties.values())} from gridtiles.cells_{layer}"
            " where z = ? and qk >= ? and qk < ? order by qk",
            [c, keys.start, keys.stop],
        ).fetchall()
        features = (
            (c, *quadkey.to_xy(c, qk), {"qk": quadkey.to_text(c, qk), "z": c, "n": n, **dict(zip(properties, values))})
            for qk, n, *values in rows
        )
        return mvt.encode(z, x, y, features)

    def _tile_layout(self, layer: str, cursor: duckdb.DuckDBPyConnection) -> tuple[LayerSpec, dict[str, str]]:
        """Spec and tile-property expressions of a layer, cached: tiles are requested many times."""
        if layer not in self._layouts:
            spec = self.spec(layer, cursor)
            self._layouts[layer] = spec, {k: v for k, v in _properties(spec).items() if v != "n"}
        return self._layouts[layer]

    def _cursor(self) -> duckdb.DuckDBPyConnection:
        """A cursor per thread: DuckDB connections are not safe for concurrent use."""
        if not hasattr(self._local, "cursor"):
            self._local.cursor = self.conn.cursor()
        cursor: duckdb.DuckDBPyConnection = self._local.cursor
        return cursor

    def tiles_with_data(self, layer: str) -> Iterator[tuple[int, int, int]]:
        (info,) = self.list_layers([layer])
        spec = info.spec
        for t in range(info.tile_zooms[0], info.tile_zooms[1] + 1):
            c = spec.cell_zoom(t)
            rows = self.conn.execute(
                f"select distinct qk >> {2 * (c - t)} as tile from gridtiles.cells_{layer} where z = {c} order by tile"
            ).fetchall()
            yield from ((t, *quadkey.to_xy(t, tile)) for (tile,) in rows)

    def diff(self, a: str, b: str, name: str) -> None:
        raise MissingCapability("diffs need a backend with 'snapshots' (PostgreSQL)")

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
        cursor = self._cursor()
        spec = self.spec(layer, cursor)
        q = region_stats.build(spec, prefix=prefix, bbox=bbox, polygon=polygon, zoom=zoom, snapshot=snapshot)
        ((row),) = cursor.execute(q.sql).fetchall()
        return region_stats.result(spec, q, row)

    def manifest(self, layer: str) -> dict[str, Any]:
        (info,) = self.list_layers([layer])
        return info.manifest()


def _merge(column: Column, a: str, b: str) -> str:
    """SQL merging two partial aggregates (NULL means no data); mirrors `gridtiles._merge`."""
    if column.merge == "sum":
        return f"coalesce({a} + {b}, {a}, {b})"
    return f"{'least' if column.merge == 'min' else 'greatest'}({a}, {b})"


def _aggregates(spec: LayerSpec, share: str = "1", area: str = "1") -> str:
    """Comma-led aggregates of source records, named as the columns; mirrors `gridtiles._aggregates`."""
    out = []
    for c in spec.columns():
        v = c.value
        src, w = _quote(v.from_), _quote(v.weight) if v.weight else "1"
        if c.part == "weight":
            expr = f"sum(case when {src} is not null then {w} * {area} end)"
        elif v.agg == "mean":
            expr = f"sum({src} * {w} * {area})"
        elif v.agg == "sum":
            expr = f"sum({src} * {share})"
        else:
            expr = f"{c.merge}({src})"
        out.append(f", {expr} as {c.name}")
    return "".join(out)


def _property(agg: str, name: str) -> str:
    if agg == "count":
        return "n"
    if agg == "mean":
        return f"{name}_sum / nullif({name}_w, 0)"
    return f"{name}_{agg}"


def _properties(spec: LayerSpec) -> dict[str, str]:
    """Tile property expression per value; mirrors `gridtiles._properties`."""
    return {v.name: _property(v.agg, v.name) for v in spec.values}


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'
