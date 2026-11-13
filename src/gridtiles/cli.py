"""The `gridtiles` command line."""

import json
import signal
import subprocess
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Annotated, Any, NoReturn

import psycopg
import typer
from pydantic import ValidationError

from gridtiles import db, martin, viewer
from gridtiles.export import export_pmtiles
from gridtiles.spec import LayerSpec

STATS_MAX_AGE = 300

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Quadkey grids served as zoomable vector tiles.")

Db = Annotated[str, typer.Option("--db", envvar="GRIDTILES_DB", help="PostgreSQL URL")]


def _fail(message: str) -> NoReturn:
    typer.echo(message, err=True)
    raise typer.Exit(2)


def _layer_spec(layer: str, spec_file: Path) -> LayerSpec:
    try:
        data = json.loads(spec_file.read_text())
    except (OSError, json.JSONDecodeError) as e:
        _fail(f"Cannot read layer spec {spec_file}: {e}")
    try:
        return LayerSpec.model_validate({**data, "name": layer})
    except ValidationError as e:
        _fail(f"Invalid layer spec:\n{e}")


@app.command()
def init(db_url: Db) -> None:
    """Install the gridtiles schema."""
    with psycopg.connect(db_url, autocommit=True) as conn:
        typer.echo(f"gridtiles schema {db.install(conn)} installed")


@app.command()
def add(
    db_url: Db,
    layer: str,
    spec: Annotated[Path, typer.Option(help="Layer spec JSON")],
) -> None:
    """Register a layer: a live grid over the source table its spec names."""
    layer_spec = _layer_spec(layer, spec)
    with psycopg.connect(db_url) as conn:
        try:
            db.create_layer(conn, layer_spec)
        except psycopg.errors.UniqueViolation:
            _fail(f"Layer {layer!r} exists.")
        except psycopg.Error as e:  # the source is missing or has the wrong column types
            _fail(f"Cannot add layer {layer!r}: {e.diag.message_primary}")
    typer.echo(f"Added layer {layer} over {layer_spec.source.table}")


@app.command("layers")
def list_layers(db_url: Db) -> None:
    """List layers with their source, zooms and published tile zooms."""
    with psycopg.connect(db_url) as conn:
        for layer in db.layers(conn):
            s, (t0, t1) = layer.spec, layer.tile_zooms
            time = f"\ttime {s.source.time}" if s.source.time else ""
            typer.echo(f"{s.name}\t{s.source.table}\tz{s.min_zoom}-{s.base_zoom}\tdetail {s.detail}\ttiles z{t0}-{t1}{time}")


@app.command()
def cache(
    db_url: Db,
    layer: str,
    max_zoom: Annotated[int | None, typer.Option(help="Cache the squares of levels up to this zoom")] = None,
    drop: Annotated[bool, typer.Option(help="Drop the cache")] = False,
) -> None:
    """Cache or drop the coarse levels of a layer: tiles there read the cache, which lags the source
    until refreshed. Run again to refresh."""
    if (max_zoom is None) == (not drop):
        _fail("Pass --max-zoom or --drop.")
    with psycopg.connect(db_url) as conn:
        try:
            row = conn.execute("select gridtiles.refresh_cache(%s, %s)", (layer, max_zoom)).fetchone()
        except psycopg.errors.NoDataFound:
            _fail(f"Layer {layer!r} does not exist.")
        except psycopg.errors.InvalidParameterValue as e:
            _fail(str(e.diag.message_primary))
    typer.echo(f"Dropped the cache of {layer}" if drop else f"Cached {row and row[0]} squares of {layer} up to z{max_zoom}")


@app.command()
def export(
    db_url: Db,
    layer: Annotated[str, typer.Option(help="Layer to export")],
    output: Annotated[Path, typer.Option("--output", "-o", help="PMTiles file to write")],
    at: Annotated[str | None, typer.Option(help="Time to export (the at parameter) [default: all rows]")] = None,
) -> None:
    """Export a layer to one PMTiles archive plus the viewer, for static hosting."""
    with psycopg.connect(db_url) as conn:
        try:
            path = export_pmtiles(conn, layer, output, params={"at": at} if at else None)
        except LookupError as e:
            _fail(str(e))
        except psycopg.errors.InvalidParameterValue as e:
            _fail(str(e.diag.message_primary))
    typer.echo(f"Wrote {path} with index.html and config.json in {path.parent}")


@app.command()
def serve(
    db_url: Db,
    layer: Annotated[list[str] | None, typer.Option(help="Layers to serve [default: all]")] = None,
    port: Annotated[int, typer.Option(help="Viewer port")] = 8080,
    martin_port: Annotated[int, typer.Option(help="Martin port")] = 3000,
    build: Annotated[Path, typer.Option(help="Directory for generated files")] = Path("build"),
) -> None:
    """Serve live tiles with Martin and the viewer, until interrupted."""
    with psycopg.connect(db_url) as conn:
        try:
            layers = db.layers(conn, layer)
        except LookupError as e:
            _fail(str(e))
    if not layers:
        _fail("No layers to serve: add one first.")
    names = [lay.name for lay in layers]
    config_path = martin.write_config(build / "martin.yaml", db_url, layers, martin_port)
    site = viewer.write(build / "viewer", layers, lambda lay: martin.tile_url(lay, martin_port))

    def viewer_config() -> dict[str, Any]:
        with psycopg.connect(db_url) as conn:
            fresh = db.layers(conn, names, stats=True)
        return viewer.config(fresh, lambda lay: martin.tile_url(lay, martin_port))

    def stop(*_: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    tiles = subprocess.Popen(martin.command(config_path))
    server = ThreadingHTTPServer(("127.0.0.1", port), viewer.handler(site, viewer_config, STATS_MAX_AGE))
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
