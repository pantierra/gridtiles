"""Static export: one PMTiles archive plus the viewer, servable from any static host."""

import gzip
import math
from os import PathLike
from pathlib import Path

import psycopg
from pmtiles.tile import Compression, TileType, zxy_to_tileid
from pmtiles.writer import Writer
from psycopg.types.json import Jsonb

from gridtiles import __version__, db, viewer
from gridtiles.layer import Layer


def manifest(layer: Layer) -> dict[str, object]:
    """What a consumer needs to style and identify a layer; stored in the archive metadata."""
    return {
        "version": __version__,
        "spec": layer.spec.model_dump(mode="json", exclude_none=True),
        "params": layer.params,
        "stats": layer.stats,
        "bounds": layer.bounds,
    }


def export_pmtiles(
    conn: psycopg.Connection,
    layer: str,
    out_path: str | PathLike[str],
    *,
    params: dict[str, str] | None = None,
) -> Path:
    """Write every tile with data in the published zoom range to a PMTiles archive (gzip-compressed
    MVT), with `index.html` and `config.json` next to it; layers exported into the same folder
    share the viewer. The archive is a snapshot of the source at export time, of the tiles of
    `params` (e.g. `{"at": "2024-01-01"}`; by default all rows). Exports of the same data
    are identical.
    """
    (info,) = db.layers(conn, [layer], stats=True, params=params)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = conn.execute("select z, x, y from gridtiles.tiles_with_data(%s, %s)", (layer, Jsonb(info.params))).fetchall()
    with open(out, "wb") as f:
        writer = Writer(f)
        for z, x, y in sorted(rows, key=lambda t: zxy_to_tileid(*t)):  # in tile-id order: a clustered archive
            row = conn.execute(
                "select gridtiles.mvt(%s, %s, %s, %s, %s, cached => false)", (layer, z, x, y, Jsonb(info.params))
            ).fetchone()
            if row and row[0]:  # a polygon touching a tile only in a sliver below float precision gives none
                writer.write_tile(zxy_to_tileid(z, x, y), gzip.compress(bytes(row[0]), mtime=0))
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


def _fields(layer: Layer) -> dict[str, str]:
    values = {v.name: "Number" for v in layer.spec.values if v.agg != "count"}
    return {"qk": "String", "z": "Number", "n": "Number", **values}
