"""The `gridtiles` command line. Settings precedence: flags > gridtiles.toml > $GRIDTILES_DB."""

import json
import os
import signal
import subprocess
import threading
import tomllib
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Annotated, Any, NoReturn

import psycopg
import typer
from pydantic import ValidationError

from gridtiles import db, martin, viewer
from gridtiles.ingest import load_points
from gridtiles.spec import LayerSpec

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Quadkey grids served as zoomable vector tiles.")

Db = Annotated[
    str | None, typer.Option("--db", help="PostgreSQL URL [default: gridtiles.toml [backend] url, then $GRIDTILES_DB]")
]
Config = Annotated[Path | None, typer.Option(help="Settings file [default: ./gridtiles.toml if present]")]


def _fail(message: str) -> NoReturn:
    typer.echo(message, err=True)
    raise typer.Exit(2)


def _settings(path: Path | None) -> dict[str, Any]:
    path = path or (Path("gridtiles.toml") if Path("gridtiles.toml").exists() else None)
    try:
        return tomllib.loads(path.read_text()) if path else {}
    except tomllib.TOMLDecodeError as e:
        _fail(f"Invalid {path}: {e}")


def _db_url(flag: str | None, settings: dict[str, Any]) -> str:
    url = flag or settings.get("backend", {}).get("url") or os.environ.get("GRIDTILES_DB")
    return url or _fail("No database: pass --db, set [backend] url in gridtiles.toml or $GRIDTILES_DB.")


def _layer_spec(layer: str, spec_file: Path | None, settings: dict[str, Any]) -> LayerSpec:
    """The spec of a new layer, from a JSON file or from [grid] and [[value]] in gridtiles.toml."""
    if spec_file:
        try:
            data = json.loads(spec_file.read_text())
        except (OSError, json.JSONDecodeError) as e:
            _fail(f"Cannot read layer spec {spec_file}: {e}")
    elif "grid" in settings:
        data = {**settings["grid"], "values": settings.get("value", [])}
    else:
        _fail(f"Layer {layer!r} does not exist: pass --spec or a gridtiles.toml with [grid] and [[value]].")
    try:
        return LayerSpec.model_validate({**data, "name": layer})
    except ValidationError as e:
        _fail(f"Invalid layer spec:\n{e}")


@app.command()
def init(db_url: Db = None, config: Config = None) -> None:
    """Install or upgrade the gridtiles schema."""
    with psycopg.connect(_db_url(db_url, _settings(config)), autocommit=True) as conn:
        typer.echo(f"gridtiles schema {db.install(conn)} installed")


@app.command()
def load(
    src: Path,
    layer: Annotated[str, typer.Option(help="Layer name; created if absent")],
    spec: Annotated[Path | None, typer.Option(help="Layer spec JSON for a new layer")] = None,
    db_url: Db = None,
    config: Config = None,
) -> None:
    """Load a data file into a layer and roll it up."""
    settings = _settings(config)
    with psycopg.connect(_db_url(db_url, settings)) as conn:
        try:
            db.layer_spec(conn, layer)
        except LookupError:
            db.create_layer(conn, _layer_spec(layer, spec, settings))
        typer.echo(load_points(conn, layer, src))


@app.command("layers")
def list_layers(db_url: Db = None, config: Config = None) -> None:
    """List layers with their zooms and cell counts."""
    with psycopg.connect(_db_url(db_url, _settings(config))) as conn:
        for layer in db.layers(conn):
            s, (t0, t1) = layer.spec, layer.tile_zooms
            typer.echo(f"{s.name}\tz{s.min_zoom}-{s.base_zoom}\tdetail {s.detail}\ttiles z{t0}-{t1}\t{layer.cells} cells")


@app.command()
def serve(
    layer: Annotated[list[str] | None, typer.Option(help="Layers to serve [default: all]")] = None,
    port: Annotated[int, typer.Option(help="Viewer port")] = 8080,
    martin_port: Annotated[int, typer.Option(help="Martin port")] = 3000,
    build: Annotated[Path, typer.Option(help="Directory for generated files")] = Path("build"),
    db_url: Db = None,
    config: Config = None,
) -> None:
    """Serve live tiles with Martin and the viewer, until interrupted."""
    url = _db_url(db_url, _settings(config))
    with psycopg.connect(url) as conn:
        layers = db.layers(conn, layer)
    if not layers:
        _fail("No layers to serve: load one first.")
    config_path = martin.write_config(build / "martin.yaml", url, layers, martin_port)
    site = viewer.write(build / "viewer", layers, lambda lay: martin.tile_url(lay, martin_port))

    def stop(*_: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    tiles = subprocess.Popen(martin.command(config_path))
    server = ThreadingHTTPServer(("127.0.0.1", port), partial(SimpleHTTPRequestHandler, directory=site))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    typer.echo(f"Viewer on http://localhost:{port}, tiles on http://localhost:{martin_port}")
    try:
        status = tiles.wait()  # returns only if Martin exits on its own, e.g. on a bad URL or a busy port
        _fail(f"Martin exited with status {status}")
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        if tiles.poll() is None:
            tiles.terminate()
            try:
                tiles.wait(timeout=10)
            except subprocess.TimeoutExpired:
                tiles.kill()
