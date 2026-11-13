"""The single-page MapLibre viewer, the `config.json` it reads, and a server for it."""

import json
import threading
import time
from collections.abc import Callable
from functools import partial
from http.server import SimpleHTTPRequestHandler
from importlib.resources import files
from pathlib import Path
from typing import Any

import psycopg

from gridtiles.layer import Layer


def config(layers: list[Layer], tiles: Callable[[Layer], str]) -> dict[str, Any]:
    """`tiles` gives each layer's XYZ URL template or PMTiles path."""
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
            }
            for layer in layers
        ]
    }


def write(directory: Path, layers: list[Layer], tiles: Callable[[Layer], str], *, merge: bool = False) -> Path:
    """Write `index.html` and `config.json` into `directory`. With `merge`, layers already in an
    existing `config.json` stay (replaced by name), so several exports can share one folder.
    """
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "index.html").write_bytes(files("gridtiles.viewer").joinpath("index.html").read_bytes())
    new = config(layers, tiles)
    path = directory / "config.json"
    if merge and path.exists():
        names = {layer["name"] for layer in new["layers"]}
        kept = [layer for layer in json.loads(path.read_text())["layers"] if layer["name"] not in names]
        new["layers"] = sorted(kept + new["layers"], key=lambda layer: layer["name"])
    path.write_text(json.dumps(new, indent=2) + "\n")
    return directory


class _Refreshed:
    """`build()`, rebuilt at most every `seconds` by one thread at a time; the last result while a
    rebuild fails."""

    def __init__(self, build: Callable[[], dict[str, Any]], seconds: float) -> None:
        self.build, self.seconds = build, seconds
        self.lock = threading.Lock()
        self.value: dict[str, Any] | None = None
        self.at = 0.0

    def __call__(self) -> dict[str, Any]:
        with self.lock:
            if self.value is None or time.monotonic() - self.at > self.seconds:
                try:
                    self.value, self.at = self.build(), time.monotonic()
                except psycopg.Error:
                    if self.value is None:
                        raise
            return self.value


class _Handler(SimpleHTTPRequestHandler):
    """Static files, except `config.json`, built per request."""

    def __init__(self, *args: Any, live_config: Callable[[], dict[str, Any]], **kwargs: Any) -> None:
        self.live_config = live_config
        super().__init__(*args, **kwargs)

    def do_GET(self) -> None:
        if self.path.split("?")[0] != "/config.json":
            return super().do_GET()
        try:
            body = (json.dumps(self.live_config()) + "\n").encode()
        except (psycopg.Error, LookupError) as e:  # e.g. the database is unreachable: tell the browser
            self.send_error(503, f"config.json: {e}")
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def handler(directory: Path, build: Callable[[], dict[str, Any]], max_age: float) -> Callable[..., SimpleHTTPRequestHandler]:
    """A request handler serving `directory`, with `config.json` from `build()`, rebuilt at most
    every `max_age` seconds (the last config while a rebuild fails)."""
    return partial(_Handler, directory=str(directory), live_config=_Refreshed(build, max_age))
