import gzip
import json
import random
from pathlib import Path

import psycopg
import pytest
from pmtiles.reader import MemorySource, Reader
from pmtiles.tile import TileType

from gridtiles import db
from gridtiles.export import export_pmtiles
from gridtiles.importer import import_file
from gridtiles.spec import LayerSpec

from ..conftest import FIXTURES, fresh_database


@pytest.fixture(scope="module")
def exported(tmp_path_factory: pytest.TempPathFactory) -> tuple[str, Path]:
    """A committed layer (exports read it from parallel connections) and its archive."""
    url = fresh_database("gridtiles_export")
    with psycopg.connect(url) as conn:
        db.install(conn)
        import_file(conn, FIXTURES / "points_10k.parquet", "pop_points")
        db.create_layer(conn, LayerSpec.model_validate_json((FIXTURES / "spec_pop.json").read_text()))
    with psycopg.connect(url) as conn:
        path = export_pmtiles(conn, "pop", tmp_path_factory.mktemp("site") / "pop.pmtiles")
    return url, path


def reader(path: Path) -> Reader:
    return Reader(MemorySource(path.read_bytes()))


def test_header(exported: tuple[str, Path]) -> None:
    url, path = exported
    r = reader(path)
    header = r.header()
    with psycopg.connect(url) as conn:
        (layer,) = db.layers(conn, ["pop"], stats=True)
    assert (header["min_zoom"], header["max_zoom"]) == layer.tile_zooms == (0, 12)
    assert header["tile_type"] == TileType.MVT
    west, south, east, north = layer.bounds
    assert header["min_lon_e7"] <= west * 1e7 and header["min_lat_e7"] <= south * 1e7
    assert header["max_lon_e7"] >= east * 1e7 and header["max_lat_e7"] >= north * 1e7


def test_tile_count_and_contents(exported: tuple[str, Path]) -> None:
    url, path = exported
    r = reader(path)
    with psycopg.connect(url) as conn:
        tiles = conn.execute("select z, x, y from gridtiles.tiles_with_data('pop')").fetchall()
        assert r.header()["addressed_tiles_count"] == len(tiles)
        for z, x, y in random.Random(8).sample(tiles, min(200, len(tiles))):
            (expected,) = conn.execute("select gridtiles.mvt('pop', %s, %s, %s)", (z, x, y)).fetchone()
            assert gzip.decompress(r.get(z, x, y)) == bytes(expected)


def test_metadata_holds_manifest(exported: tuple[str, Path]) -> None:
    url, path = exported
    r = reader(path)
    meta = r.metadata()
    with psycopg.connect(url) as conn:
        spec, stats = conn.execute("select spec, gridtiles.stats(name) from gridtiles.layers where name = 'pop'").fetchone()
    assert meta["gridtiles"]["spec"] == spec
    assert meta["gridtiles"]["stats"] == stats["stats"]
    assert meta["gridtiles"]["bounds"] == stats["bounds"]
    assert meta["vector_layers"][0]["id"] == "cells"


def test_viewer_files(exported: tuple[str, Path]) -> None:
    _, path = exported
    config = json.loads((path.parent / "config.json").read_text())
    assert config["layers"][0]["tiles"] == "pop.pmtiles"
    assert (path.parent / "index.html").exists()


def test_exports_are_identical(exported: tuple[str, Path], tmp_path: Path) -> None:
    url, path = exported
    with psycopg.connect(url) as conn:
        again = export_pmtiles(conn, "pop", tmp_path / "pop.pmtiles")
    assert again.read_bytes() == path.read_bytes()


def test_exports_share_a_folder(tmp_path: Path) -> None:
    url = fresh_database("gridtiles_export_shared")
    spec = LayerSpec.model_validate_json((FIXTURES / "spec_pop.json").read_text())
    with psycopg.connect(url) as conn:
        db.install(conn)
        import_file(conn, FIXTURES / "points_small.csv", "pop_points")
        for name in ("pop", "other"):
            db.create_layer(conn, spec.model_copy(update={"name": name}))
    with psycopg.connect(url) as conn:
        for name in ("pop", "other"):
            export_pmtiles(conn, name, tmp_path / f"{name}.pmtiles")
    config = json.loads((tmp_path / "config.json").read_text())
    assert [(layer["name"], layer["tiles"]) for layer in config["layers"]] == [
        ("other", "other.pmtiles"),
        ("pop", "pop.pmtiles"),
    ]


def test_export_of_a_time(tmp_path: Path) -> None:
    """A layer with a time column exports the latest time unless told otherwise."""
    url = fresh_database("gridtiles_export_time")
    spec = LayerSpec.model_validate_json((FIXTURES / "spec_pop.json").read_text())
    with psycopg.connect(url) as conn:
        db.install(conn)
        import_file(conn, FIXTURES / "points_small.csv", "pop_points")
        conn.execute("alter table pop_points add column day date")
        conn.execute("update pop_points set day = case when pop <= 40 then date '2024-01-01' else date '2024-02-01' end")
        db.create_layer(conn, spec.model_copy(update={"source": spec.source.model_copy(update={"time": "day"})}))
    with psycopg.connect(url) as conn:
        latest = export_pmtiles(conn, "pop", tmp_path / "latest" / "pop.pmtiles")
        first = export_pmtiles(conn, "pop", tmp_path / "first" / "pop.pmtiles", params={"at": "2024-01-01"})
    for path, at, total in ((latest, "2024-02-01", 50 + 60 + 70 + 80), (first, "2024-01-01", 10 + 20 + 30 + 40)):
        meta = reader(path).metadata()["gridtiles"]
        assert meta["params"] == {"at": at}
        assert meta["stats"]["population"]["total"] == total
        assert json.loads((path.parent / "config.json").read_text())["layers"][0]["times"] is None
