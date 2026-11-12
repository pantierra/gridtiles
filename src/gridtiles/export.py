"""Static export: one PMTiles archive plus the viewer, servable from any static host."""

import gzip
import math
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from itertools import batched
from os import PathLike
from pathlib import Path
from typing import Self

import psycopg
from pmtiles.tile import Compression, TileType, zxy_to_tileid
from pmtiles.writer import Writer
from psycopg import sql
from psycopg.conninfo import make_conninfo

from gridtiles import __version__, db, viewer
from gridtiles.layer import Layer

BATCH = 256


def manifest(layer: Layer) -> dict[str, object]:
    """What a consumer needs to style and identify a layer; stored in the archive metadata."""
    return {
        "version": __version__,
        "spec": layer.spec.model_dump(mode="json", exclude_none=True),
        "stats": layer.stats,
        "bounds": layer.bounds,
        "snapshots": list(layer.snapshots),
    }


def export_pmtiles(conn: psycopg.Connection, layer: str, out_path: str | PathLike[str], *, workers: int = 4) -> Path:
    """Write every tile with data in the published zoom range to a PMTiles archive (gzip-compressed
    MVT; the latest snapshot on snapshot layers), with `index.html` and `config.json` next to it;
    layers exported into the same folder share the viewer. Exports of the same data are identical.
    """
    (info,) = db.layers(conn, [layer])
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    tiles = sorted(_tiles_with_data(conn, info), key=lambda t: zxy_to_tileid(*t))
    with open(out, "wb") as f, _TileFetcher(conn, layer) as fetch, ThreadPoolExecutor(workers) as pool:
        writer = Writer(f)
        # map() yields batches in order, so tiles are written clustered while workers fetch ahead.
        for batch in pool.map(fetch, batched(tiles, BATCH)):
            for (z, x, y), data in batch:
                writer.write_tile(zxy_to_tileid(z, x, y), gzip.compress(data, mtime=0))
        west, south, east, north = info.bounds or (-180.0, -85.0511287798, 180.0, 85.0511287798)
        min_zoom, max_zoom = info.tile_zooms
        writer.finalize(
            {
                "tile_type": TileType.MVT,
                "tile_compression": Compression.GZIP,
                "min_lon_e7": math.floor(west * 1e7),
                "min_lat_e7": math.floor(south * 1e7),
                "max_lon_e7": math.ceil(east * 1e7),
                "max_lat_e7": math.ceil(north * 1e7),
                "center_zoom": min_zoom,
                "center_lon_e7": int((west + east) / 2 * 1e7),
                "center_lat_e7": int((south + north) / 2 * 1e7),
            },
            {
                "name": layer,
                "format": "pbf",
                "minzoom": min_zoom,
                "maxzoom": max_zoom,
                "vector_layers": [{"id": "cells", "minzoom": min_zoom, "maxzoom": max_zoom, "fields": _fields(info)}],
                "gridtiles": manifest(info),
            },
        )
    viewer.write(out.parent, [info], lambda _: out.name, merge=True)
    return out


def _tiles_with_data(conn: psycopg.Connection, layer: Layer) -> Iterator[tuple[int, int, int]]:
    """(z, x, y) of every tile with data: the distinct ancestors at zoom t of the cells at c(t); of the
    latest snapshot on snapshot layers, which is what `gridtiles.mvt` serves by default."""
    spec, table = layer.spec, sql.Identifier("gridtiles", f"cells_{layer.name}")
    latest = sql.SQL(" and snapshot = %(latest)s" if layer.snapshots else "")
    for t in range(layer.tile_zooms[0], layer.tile_zooms[1] + 1):
        c = min(max(t + spec.detail, spec.min_zoom), spec.base_zoom)
        query = sql.SQL(
            "select xy.x, xy.y from (select distinct qk >> %(shift)s as tile from {} where z = %(c)s{}) d,"
            " gridtiles.qk_to_xy(%(t)s, tile) xy"
        ).format(table, latest)
        params = {"shift": 2 * (c - t), "c": c, "t": t, "latest": layer.snapshots[-1] if layer.snapshots else None}
        yield from ((t, x, y) for x, y in conn.execute(query, params))


class _TileFetcher:
    """Fetch batches of tiles from worker threads, one connection per thread."""

    def __init__(self, conn: psycopg.Connection, layer: str) -> None:
        self.conninfo = make_conninfo(conn.info.dsn, password=conn.info.password)
        self.layer = layer
        self.local = threading.local()
        self.connections: list[psycopg.Connection] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        for conn in self.connections:
            conn.close()

    def __call__(self, tiles: tuple[tuple[int, int, int], ...]) -> list[tuple[tuple[int, int, int], bytes]]:
        if not hasattr(self.local, "conn"):
            self.local.conn = psycopg.connect(self.conninfo, autocommit=True)
            self.connections.append(self.local.conn)
        rows = self.local.conn.execute(
            "select gridtiles.mvt(%s, z, x, y)"
            " from unnest(%s::int[], %s::int[], %s::int[]) with ordinality t(z, x, y, i) order by i",
            (self.layer, *map(list, zip(*tiles))),
        ).fetchall()
        return [(tile, bytes(row[0])) for tile, row in zip(tiles, rows)]


def _fields(layer: Layer) -> dict[str, str]:
    values = {v.name: "Number" for v in layer.spec.values if v.agg != "count"}
    return {"qk": "String", "z": "Number", "n": "Number", **values}
