"""Martin tile server: config generation and how to run it."""

import json
import shutil
from pathlib import Path
from typing import Any

from gridtiles.layer import Layer

IMAGE = "ghcr.io/maplibre/martin:1.16.0"


def config(db_url: str, layers: list[Layer], port: int) -> dict[str, Any]:
    """One function source per layer, serving the published tile zoom range."""
    return {
        "listen_addresses": f"127.0.0.1:{port}",
        "postgres": {
            "connection_string": db_url,
            "auto_publish": False,
            "functions": {
                f"tile_{layer.name}": {
                    "schema": "gridtiles",
                    "function": f"tile_{layer.name}",
                    "minzoom": layer.tile_zooms[0],
                    "maxzoom": layer.tile_zooms[1],
                }
                for layer in layers
            },
        },
    }


def write_config(path: Path, db_url: str, layers: list[Layer], port: int) -> Path:
    """Write the config as JSON, which Martin reads as YAML."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config(db_url, layers, port), indent=2) + "\n")
    return path


def command(config_path: Path) -> list[str]:
    """A local `martin` binary if there is one, otherwise the pinned Docker image on the host network.

    Martin's tile cache is off: tiles change as soon as an upsert commits.
    """
    options = ["--cache-size", "0"]
    if binary := shutil.which("martin"):
        return [binary, "--config", str(config_path), *options]
    mount = f"{config_path.resolve()}:/config/martin.yaml:ro"
    return ["docker", "run", "--rm", "--network", "host", "-v", mount, IMAGE, "--config", "/config/martin.yaml", *options]


def tile_url(layer: Layer, port: int) -> str:
    return f"http://localhost:{port}/tile_{layer.name}/{{z}}/{{x}}/{{y}}"
