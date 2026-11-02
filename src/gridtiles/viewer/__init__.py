"""The single-page MapLibre viewer and the `config.json` it reads."""

import json
from collections.abc import Callable
from importlib.resources import files
from pathlib import Path
from typing import Any

from gridtiles.layer import Layer


def config(
    layers: list[Layer], tiles: Callable[[Layer], str], snapshot_tiles: Callable[[Layer], str] | None = None
) -> dict[str, Any]:
    """`tiles` gives each layer's XYZ URL template or PMTiles path; `snapshot_tiles` a template with
    `{snapshot}` for layers with snapshots (the viewer then shows a time slider)."""
    return {
        "layers": [
            {
                "name": layer.name,
                "tiles": tiles(layer),
                "minzoom": layer.tile_zooms[0],
                "maxzoom": layer.tile_zooms[1],
                "base_zoom": layer.spec.base_zoom,
                "bounds": layer.bounds,
                "values": [{"name": v.name, "agg": v.agg, "unit": v.unit} for v in layer.spec.values],
                "stats": layer.stats,
                "snapshots": list(layer.snapshots),
                "snapshot_tiles": snapshot_tiles(layer) if snapshot_tiles and layer.snapshots else None,
            }
            for layer in layers
        ]
    }


def write(
    directory: Path,
    layers: list[Layer],
    tiles: Callable[[Layer], str],
    snapshot_tiles: Callable[[Layer], str] | None = None,
) -> Path:
    """Write `index.html` and `config.json` into `directory`."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "index.html").write_bytes(files("gridtiles.viewer").joinpath("index.html").read_bytes())
    (directory / "config.json").write_text(json.dumps(config(layers, tiles, snapshot_tiles), indent=2) + "\n")
    return directory
