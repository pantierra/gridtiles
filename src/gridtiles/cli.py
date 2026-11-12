"""The `gridtiles` command line. Settings precedence: flags > gridtiles.toml > $GRIDTILES_DB."""

import json
import os
import signal
import subprocess
import threading
import tomllib
from datetime import timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Annotated, Any, NoReturn

import psycopg
import typer
from pydantic import ValidationError

from gridtiles import db, martin, viewer
from gridtiles.export import export_pmtiles
from gridtiles.importer import import_file
from gridtiles.spec import LayerSpec

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Quadkey grids served as zoomable vector tiles.")

Db = Annotated[
    str | None, typer.Option("--db", help="PostgreSQL URL [default: gridtiles.toml [backend] url, then $GRIDTILES_DB]")
]
STATS_MAX_AGE = timedelta(minutes=5)

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


def _layer_spec(layer: str, spec_file: Path | None, settings: dict[str, Any], source: dict[str, str]) -> LayerSpec:
    """The spec of a new layer, from a JSON file or from [grid], [source] and [[value]] in
    gridtiles.toml; `source` (from flags) overrides the spec's source keys.
    """
    if spec_file:
        try:
            data = json.loads(spec_file.read_text())
        except (OSError, json.JSONDecodeError) as e:
            _fail(f"Cannot read layer spec {spec_file}: {e}")
    elif "grid" in settings:
        data = {**settings["grid"], "source": settings.get("source", {}), "values": settings.get("value", [])}
    else:
        _fail("No layer spec: pass --spec or a gridtiles.toml with [grid] and [[value]].")
    if isinstance(data, dict) and source:
        data["source"] = {**(data.get("source") or {}), **source}
    try:
        return LayerSpec.model_validate({**data, "name": layer})
    except ValidationError as e:
        _fail(f"Invalid layer spec:\n{e}")


def _connect(url: str) -> psycopg.Connection:
    """A connection that prints server warnings (e.g. a missing index) to stderr."""
    conn = psycopg.connect(url)
    conn.add_notice_handler(lambda d: typer.echo(f"{d.severity}: {d.message_primary}", err=True))
    return conn


@app.command()
def init(db_url: Db = None, config: Config = None) -> None:
    """Install or upgrade the gridtiles schema."""
    with psycopg.connect(_db_url(db_url, _settings(config)), autocommit=True) as conn:
        typer.echo(f"gridtiles schema {db.install(conn)} installed")


@app.command("import")
def import_(
    src: Path,
    table: Annotated[str, typer.Option(help="Table to create, NAME or SCHEMA.NAME")],
    replace: Annotated[bool, typer.Option(help="Drop and recreate the table if it exists")] = False,
    append: Annotated[bool, typer.Option(help="Add the rows to an existing table")] = False,
    db_url: Db = None,
    config: Config = None,
) -> None:
    """Import a point, polygon or raster file into a plain table with a geometry column."""
    if replace and append:
        _fail("Pass --replace or --append, not both.")
    with _connect(_db_url(db_url, _settings(config))) as conn:
        try:
            report = import_file(conn, src, table, mode="replace" if replace else "append" if append else "create")
        except (ValueError, OSError) as e:  # UnsupportedGeometry, CRS, an existing table
            _fail(str(e))
        typer.echo(report)


@app.command()
def add(
    layer: str,
    source: Annotated[str | None, typer.Option(help="Source table [default: from the spec]")] = None,
    geometry: Annotated[str | None, typer.Option(help="Geometry column [default: from the spec, then geom]")] = None,
    spec: Annotated[Path | None, typer.Option(help="Layer spec JSON [default: gridtiles.toml]")] = None,
    db_url: Db = None,
    config: Config = None,
) -> None:
    """Register a layer: a live grid over a source table."""
    settings = _settings(config)
    overrides = {k: v for k, v in {"table": source, "geometry": geometry}.items() if v is not None}
    layer_spec = _layer_spec(layer, spec, settings, overrides)
    with _connect(_db_url(db_url, settings)) as conn:
        try:
            db.create_layer(conn, layer_spec)
        except psycopg.errors.UniqueViolation:
            _fail(f"Layer {layer!r} exists.")
        except psycopg.Error as e:  # the source is missing, untyped or has the wrong column types
            _fail(f"Cannot add layer {layer!r}: {e.diag.message_primary}")
    typer.echo(f"Added layer {layer} over {layer_spec.source.table}")


@app.command()
def drop(layer: str, db_url: Db = None, config: Config = None) -> None:
    """Unregister a layer; its source table stays."""
    with _connect(_db_url(db_url, _settings(config))) as conn:
        try:
            db.drop_layer(conn, layer)
        except psycopg.errors.UndefinedObject:
            _fail(f"Layer {layer!r} does not exist.")


@app.command("layers")
def list_layers(db_url: Db = None, config: Config = None) -> None:
    """List layers with their source, zooms and published tile zooms."""
    with psycopg.connect(_db_url(db_url, _settings(config))) as conn:
        for layer in db.layers(conn):
            s, (t0, t1) = layer.spec, layer.tile_zooms
            time = f"\ttime {s.source.time}" if s.source.time else ""
            typer.echo(f"{s.name}\t{s.source.table}\tz{s.min_zoom}-{s.base_zoom}\tdetail {s.detail}\ttiles z{t0}-{t1}{time}")


@app.command()
def stats(
    layer: str,
    at: Annotated[str | None, typer.Option(help="Time to describe [default: the latest, if any]")] = None,
    refresh: Annotated[bool, typer.Option(help="Recompute instead of reusing cached stats")] = False,
    db_url: Db = None,
    config: Config = None,
) -> None:
    """Print a layer's stats and bounds, as the viewer gets them, with when they were computed."""
    with _connect(_db_url(db_url, _settings(config))) as conn:
        try:
            params = {"at": at} if at else None
            (info,) = db.layers(conn, [layer], stats=True, max_age=timedelta(0) if refresh else None, params=params)
        except LookupError as e:
            _fail(str(e))
        except psycopg.errors.InvalidParameterValue as e:
            _fail(str(e.diag.message_primary))
    computed_at = info.computed_at.isoformat() if info.computed_at else None
    out = {"params": info.params, "computed_at": computed_at, "bounds": info.bounds, "stats": info.stats}
    typer.echo(json.dumps(out, indent=2))


@app.command()
def export(
    layer: Annotated[str, typer.Option(help="Layer to export")],
    output: Annotated[Path, typer.Option("--output", "-o", help="PMTiles file to write")],
    at: Annotated[str | None, typer.Option(help="Time to export [default: the latest, if any]")] = None,
    db_url: Db = None,
    config: Config = None,
) -> None:
    """Export a layer to one PMTiles archive plus the viewer, for static hosting."""
    with _connect(_db_url(db_url, _settings(config))) as conn:
        try:
            path = export_pmtiles(conn, layer, output, params={"at": at} if at else None)
        except LookupError as e:
            _fail(str(e))
        except psycopg.errors.InvalidParameterValue as e:
            _fail(str(e.diag.message_primary))
    typer.echo(f"Wrote {path} with index.html and config.json in {path.parent}")


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
        try:
            layers = db.layers(conn, layer)
        except LookupError as e:
            _fail(str(e))
    if not layers:
        _fail("No layers to serve: add one first.")
    names = [lay.name for lay in layers]
    config_path = martin.write_config(build / "martin.yaml", url, layers, martin_port)
    site = viewer.write(build / "viewer", layers, lambda lay: martin.tile_url(lay, martin_port))

    def live_config() -> dict[str, Any]:
        """Stats come from the source, cached for STATS_MAX_AGE."""
        with psycopg.connect(url) as conn:
            fresh = db.layers(conn, names, stats=True, max_age=STATS_MAX_AGE)
        return viewer.config(fresh, lambda lay: martin.tile_url(lay, martin_port))

    def stop(*_: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    tiles = subprocess.Popen(martin.command(config_path))
    server = ThreadingHTTPServer(("127.0.0.1", port), viewer.handler(site, live_config))
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
