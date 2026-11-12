import gzip
import json
import random
from pathlib import Path

import psycopg
import pytest
from pmtiles.reader import MemorySource, Reader
from pmtiles.tile import TileType

from gridtiles.backends.postgres import PostgresBackend
from gridtiles.export import export_pmtiles
from gridtiles.spec import LayerSpec

from ..conftest import FIXTURES, fresh_database


@pytest.fixture(scope="module")
def exported(tmp_path_factory: pytest.TempPathFactory) -> tuple[str, Path]:
    """A committed layer (exports read it from parallel connections) and its archive."""
    url = fresh_database("gridtiles_export")
    with PostgresBackend(url) as backend:
        backend.install()
        backend.create_layer(LayerSpec.model_validate_json((FIXTURES / "spec_pop.json").read_text()))
        backend.load("pop", FIXTURES / "points_10k.parquet")
        path = export_pmtiles(backend, "pop", tmp_path_factory.mktemp("site") / "pop.pmtiles")
    return url, path


def reader(path: Path) -> Reader:
    return Reader(MemorySource(path.read_bytes()))


def test_header(exported: tuple[str, Path]) -> None:
    url, path = exported
    r = reader(path)
    header = r.header()
    with PostgresBackend(url) as backend:
        (layer,) = backend.list_layers(["pop"])
    assert (header["min_zoom"], header["max_zoom"]) == layer.tile_zooms == (0, 12)
    assert header["tile_type"] == TileType.MVT
    west, south, east, north = layer.bounds
    assert header["min_lon_e7"] <= west * 1e7 and header["min_lat_e7"] <= south * 1e7
    assert header["max_lon_e7"] >= east * 1e7 and header["max_lat_e7"] >= north * 1e7


def test_tile_count_and_contents(exported: tuple[str, Path]) -> None:
    url, path = exported
    r = reader(path)
    with psycopg.connect(url) as conn:
        tiles = conn.execute(
            "select t, xy.x, xy.y from generate_series(0, 12) t,"
            " lateral (select distinct qk >> (2 * 4) as tile from gridtiles.cells_pop where z = t + 4) d,"
            " gridtiles.qk_to_xy(t, tile) xy"
        ).fetchall()
        assert r.header()["addressed_tiles_count"] == len(tiles)
        for z, x, y in random.Random(8).sample(tiles, min(200, len(tiles))):
            (expected,) = conn.execute("select gridtiles.mvt('pop', %s, %s, %s)", (z, x, y)).fetchone()
            assert gzip.decompress(r.get(z, x, y)) == bytes(expected)


def test_metadata_holds_manifest(exported: tuple[str, Path]) -> None:
    url, path = exported
    r = reader(path)
    meta = r.metadata()
    with psycopg.connect(url) as conn:
        spec, stats = conn.execute("select spec, stats from gridtiles.layers where name = 'pop'").fetchone()
    assert meta["gridtiles"]["spec"] == spec
    assert meta["gridtiles"]["stats"] == stats
    assert meta["vector_layers"][0]["id"] == "cells"


def test_viewer_files(exported: tuple[str, Path]) -> None:
    _, path = exported
    config = json.loads((path.parent / "config.json").read_text())
    assert config["layers"][0]["tiles"] == "pop.pmtiles"
    assert (path.parent / "index.html").exists()


def test_exports_are_identical(exported: tuple[str, Path], tmp_path: Path) -> None:
    url, path = exported
    with PostgresBackend(url) as backend:
        again = export_pmtiles(backend, "pop", tmp_path / "pop.pmtiles")
    assert again.read_bytes() == path.read_bytes()
