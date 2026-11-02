"""Static export: one PMTiles archive plus the viewer, servable from any static host."""

import gzip
import math
from concurrent.futures import ThreadPoolExecutor
from os import PathLike
from pathlib import Path

from pmtiles.tile import Compression, TileType, zxy_to_tileid
from pmtiles.writer import Writer

from gridtiles import viewer
from gridtiles.backends.base import Backend
from gridtiles.layer import Layer


def export_pmtiles(backend: Backend, layer: str, out_path: str | PathLike[str], *, workers: int = 4) -> Path:
    """Write every tile with data in the published zoom range to a PMTiles archive (gzip-compressed
    MVT, fetched by `workers` threads), with `index.html` and `config.json` next to it; layers
    exported into the same folder share the viewer. Exports of the same data are byte-identical.
    """
    (info,) = backend.list_layers([layer])
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    tiles = sorted(backend.tiles_with_data(layer), key=lambda t: zxy_to_tileid(*t))
    with open(out, "wb") as f, ThreadPoolExecutor(workers) as pool:
        writer = Writer(f)
        # map() yields in order, so tiles are written clustered while workers fetch ahead.
        for (z, x, y), data in zip(tiles, pool.map(lambda t: backend.tile(layer, *t), tiles)):
            writer.write_tile(zxy_to_tileid(z, x, y), gzip.compress(data, mtime=0))
        writer.finalize(_header(info), _metadata(info))
    viewer.write(out.parent, [info], lambda _: out.name, merge=True)
    return out


def _header(layer: Layer) -> dict[str, object]:
    west, south, east, north = layer.bounds or (-180.0, -85.0511287798, 180.0, 85.0511287798)
    return {
        "tile_type": TileType.MVT,
        "tile_compression": Compression.GZIP,
        "min_lon_e7": math.floor(west * 1e7),
        "min_lat_e7": math.floor(south * 1e7),
        "max_lon_e7": math.ceil(east * 1e7),
        "max_lat_e7": math.ceil(north * 1e7),
        "center_zoom": layer.tile_zooms[0],
        "center_lon_e7": round((west + east) / 2 * 1e7),
        "center_lat_e7": round((south + north) / 2 * 1e7),
    }


def _metadata(layer: Layer) -> dict[str, object]:
    min_zoom, max_zoom = layer.tile_zooms
    fields = {
        "qk": "String",
        "z": "Number",
        "n": "Number",
        **{v.name: "Number" for v in layer.spec.values if v.agg != "count"},
    }
    return {
        "name": layer.name,
        "format": "pbf",
        "minzoom": min_zoom,
        "maxzoom": max_zoom,
        "vector_layers": [{"id": "cells", "minzoom": min_zoom, "maxzoom": max_zoom, "fields": fields}],
        "gridtiles": layer.manifest(),
    }
