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
    *,
    merge: bool = False,
) -> Path:
    """Write `index.html` and `config.json` into `directory`. With `merge`, layers already in an
    existing `config.json` stay (replaced by name), so several exports can share one folder."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "index.html").write_bytes(files("gridtiles.viewer").joinpath("index.html").read_bytes())
    new = config(layers, tiles, snapshot_tiles)
    path = directory / "config.json"
    if merge and path.exists():
        names = {layer["name"] for layer in new["layers"]}
        kept = [layer for layer in json.loads(path.read_text())["layers"] if layer["name"] not in names]
        new["layers"] = sorted(kept + new["layers"], key=lambda layer: layer["name"])
    path.write_text(json.dumps(new, indent=2) + "\n")
    return directory
