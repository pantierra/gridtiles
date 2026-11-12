"""The `gridtiles` command line. Settings precedence: flags > gridtiles.toml > $GRIDTILES_DB."""

import json
import os
import signal
import subprocess
import threading
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer
from pydantic import ValidationError

from gridtiles import martin, server, viewer
from gridtiles.backends.base import Backend, MissingCapability, UpsertMode, open_backend, require
from gridtiles.export import export_pmtiles
from gridtiles.sources import MAX_CELLS_PER_FEATURE, CellLimitExceeded
from gridtiles.spec import LayerSpec

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Quadkey grids served as zoomable vector tiles.")

Db = Annotated[
    str | None,
    typer.Option("--db", help="PostgreSQL URL or DuckDB file [default: gridtiles.toml [backend], then $GRIDTILES_DB]"),
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


@contextmanager
def _backend(flag: str | None, settings: dict[str, Any]) -> Iterator[Backend]:
    configured = settings.get("backend", {})
    url = flag or configured.get("url") or configured.get("path") or os.environ.get("GRIDTILES_DB")
    if not url:
        _fail("No database: pass --db, set [backend] in gridtiles.toml or $GRIDTILES_DB.")
    with open_backend(url, None if flag else configured.get("type")) as backend:
        yield backend


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
    with _backend(db_url, _settings(config)) as backend:
        typer.echo(f"gridtiles schema {backend.install()} installed")


@app.command()
def load(
    src: Path,
    layer: Annotated[str, typer.Option(help="Layer name; created if absent")],
    spec: Annotated[Path | None, typer.Option(help="Layer spec JSON for a new layer")] = None,
    force: Annotated[
        bool, typer.Option(help=f"Allow polygons spanning more than {MAX_CELLS_PER_FEATURE:,} base cells")
    ] = False,
    append: Annotated[bool, typer.Option(help="Add to the layer incrementally instead of rolling up fully")] = False,
    replace_cells: Annotated[bool, typer.Option(help="Overwrite the base cells the source touches, incrementally")] = False,
    refresh_stats: Annotated[bool, typer.Option(help="With --append / --replace-cells: refresh stats and bounds")] = False,
    snapshot: Annotated[str | None, typer.Option(help="Snapshot date (YYYY-MM-DD) on snapshot layers")] = None,
    db_url: Db = None,
    config: Config = None,
) -> None:
    """Load points, polygons or a raster into a layer and roll it up."""
    if append and replace_cells:
        _fail("Use either --append or --replace-cells.")
    if snapshot and (append or replace_cells):
        _fail("Snapshots are loaded, not upserted: drop --append / --replace-cells.")
    settings = _settings(config)
    with _backend(db_url, settings) as backend:
        try:
            backend.list_layers([layer])
            created = False
        except LookupError:
            backend.create_layer(_layer_spec(layer, spec, settings))
            created = True
        max_cells = None if force else MAX_CELLS_PER_FEATURE
        try:
            if append or replace_cells:
                mode: UpsertMode = "replace" if replace_cells else "add"
                report = backend.upsert(layer, src, mode=mode, refresh_stats=refresh_stats, max_cells_per_feature=max_cells)
            else:
                day = date.fromisoformat(snapshot) if snapshot else None
                report = backend.load(layer, src, max_cells_per_feature=max_cells, snapshot=day)
        except (CellLimitExceeded, MissingCapability, ValueError) as e:  # ValueError: UnsupportedGeometry, raster
            if created:  # leave no empty layer behind
                backend.drop_layer(layer)
            _fail(f"{e} (use --force)" if isinstance(e, CellLimitExceeded) else str(e))
        typer.echo(report)


@app.command("layers")
def list_layers(db_url: Db = None, config: Config = None) -> None:
    """List layers with their zooms and cell counts."""
    with _backend(db_url, _settings(config)) as backend:
        for layer in backend.list_layers():
            s, (t0, t1) = layer.spec, layer.tile_zooms
            typer.echo(f"{s.name}\tz{s.min_zoom}-{s.base_zoom}\tdetail {s.detail}\ttiles z{t0}-{t1}\t{layer.cells} cells")


@app.command()
def export(
    layer: Annotated[str, typer.Option(help="Layer to export")],
    output: Annotated[Path, typer.Option("--output", "-o", help="PMTiles file to write")],
    db_url: Db = None,
    config: Config = None,
) -> None:
    """Export a layer to one PMTiles archive plus the viewer, for static hosting."""
    with _backend(db_url, _settings(config)) as backend:
        try:
            path = export_pmtiles(backend, layer, output)
        except LookupError as e:
            _fail(str(e))
    typer.echo(f"Wrote {path} with index.html and config.json in {path.parent}")


@app.command()
def diff(
    a: Annotated[str, typer.Argument(help="Layer A, as NAME or NAME@YYYY-MM-DD")],
    b: Annotated[str, typer.Argument(help="Layer B, as NAME or NAME@YYYY-MM-DD")],
    name: Annotated[str, typer.Option("--as", help="Name of the new, read-only layer holding B - A")],
    db_url: Db = None,
    config: Config = None,
) -> None:
    """Store the per-cell difference B - A of two layers or snapshots as a new layer."""
    with _backend(db_url, _settings(config)) as backend:
        try:
            backend.diff(a, b, name)
        except (LookupError, ValueError, MissingCapability) as e:
            _fail(str(e))
    typer.echo(f"Created layer {name} = {b} - {a}")


@app.command()
def api(
    port: Annotated[int, typer.Option(help="API port")] = 8000,
    db_url: Db = None,
    config: Config = None,
) -> None:
    """Serve the query API (OpenAPI docs at /docs) until interrupted."""
    import uvicorn

    from gridtiles.api import create_app

    with _backend(db_url, _settings(config)) as backend:
        uvicorn.run(create_app(backend), host="0.0.0.0", port=port)


@app.command()
def serve(
    layer: Annotated[list[str] | None, typer.Option(help="Layers to serve [default: all]")] = None,
    live: Annotated[
        bool | None,
        typer.Option("--live/--embedded", help="Martin on the database, or tiles in-process [default: live if supported]"),
    ] = None,
    port: Annotated[int, typer.Option(help="Viewer port")] = 8080,
    martin_port: Annotated[int, typer.Option(help="Martin port (live)")] = 3000,
    build: Annotated[Path, typer.Option(help="Directory for generated files")] = Path("build"),
    db_url: Db = None,
    config: Config = None,
) -> None:
    """Serve tiles and the viewer until interrupted."""
    with _backend(db_url, _settings(config)) as backend:
        layers = backend.list_layers(layer)
        if not layers:
            _fail("No layers to serve: load one first.")
        live = "live" in backend.capabilities if live is None else live
        tiles = None
        if live:
            try:
                require(backend, "live", "serve --live")
            except MissingCapability as e:
                _fail(f"{e}; use --embedded")
            config_path = martin.write_config(build / "martin.yaml", backend.url, layers, martin_port)
            site = viewer.write(
                build / "viewer",
                layers,
                lambda lay: martin.tile_url(lay, martin_port),
                lambda lay: martin.snapshot_tile_url(lay, martin_port),
            )
            tiles = subprocess.Popen(martin.command(config_path))
        else:
            site = viewer.write(
                build / "viewer",
                layers,
                lambda lay: server.tile_url(lay.name, port),
                lambda lay: server.snapshot_tile_url(lay.name, port),
            )

        def stop(*_: object) -> None:
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, stop)
        httpd = server.make_server(backend, site, port)
        typer.echo(f"Viewer on http://localhost:{port}" + (f", tiles on http://localhost:{martin_port}" if live else ""))
        try:
            if tiles:
                threading.Thread(target=httpd.serve_forever, daemon=True).start()
                status = tiles.wait()  # returns only if Martin exits on its own, e.g. on a bad URL or a busy port
                _fail(f"Martin exited with status {status}")
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            if tiles:
                httpd.shutdown()  # stop the serving thread
            httpd.server_close()
            if tiles and tiles.poll() is None:
                tiles.terminate()
                try:
                    tiles.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    tiles.kill()
