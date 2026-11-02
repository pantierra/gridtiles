"""PostgreSQL and DuckDB hold the same cells and serve the same tiles."""

import gzip
import random
from collections.abc import Iterator
from pathlib import Path

import mapbox_vector_tile
import pytest
from pmtiles.reader import MemorySource, Reader

from gridtiles.backends.base import Backend
from gridtiles.export import export_pmtiles

from .conftest import FIXTURES, open_fresh, rows
from .test_operations import SPEC

LAYERS = {"pop": "points_10k.parquet", "poly": "polygons_500.gpkg"}


@pytest.fixture(scope="module")
def pair(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Backend, Backend]]:
    tmp = tmp_path_factory.mktemp("cross")
    backends = [open_fresh(kind, tmp, "gridtiles_cross") for kind in ("postgres", "duckdb")]
    for backend in backends:
        for name, source in LAYERS.items():
            backend.create_layer(SPEC.model_copy(update={"name": name}))
            backend.load(name, FIXTURES / source)
    yield backends[0], backends[1]
    for backend in backends:
        backend.__exit__(None, None, None)


def decoded(data: bytes) -> list[dict]:
    if not data:
        return []
    return mapbox_vector_tile.decode(data, default_options={"y_coord_down": True})["cells"]["features"]


def same_features(a: bytes, b: bytes) -> None:
    fa, fb = decoded(a), decoded(b)
    assert [f["geometry"] for f in fa] == [f["geometry"] for f in fb]
    for x, y in zip(fa, fb):
        assert x["properties"].pop("qk") == y["properties"].pop("qk")
        assert x["properties"] == pytest.approx(y["properties"], rel=1e-9)


@pytest.mark.parametrize("layer", LAYERS)
def test_cells_match(pair: tuple[Backend, Backend], layer: str) -> None:
    query = f"select z, qk, n, population_sum, completeness_sum, completeness_w from gridtiles.cells_{layer} order by z, qk"
    pg, duck = (rows(b, query) for b in pair)
    assert [r[:3] for r in pg] == [r[:3] for r in duck]
    for a, b in zip(pg, duck):
        assert b[3:] == pytest.approx(a[3:], rel=1e-9)


@pytest.mark.parametrize("layer", LAYERS)
def test_tiles_match(pair: tuple[Backend, Backend], layer: str) -> None:
    pg, duck = pair
    tiles = list(pg.tiles_with_data(layer))
    assert tiles == list(duck.tiles_with_data(layer))
    rng = random.Random(9)
    sample = [rng.choice(tiles) for _ in range(250)]
    sample += [(z + 6, x << 6 | rng.randrange(64), y << 6 | rng.randrange(64)) for z, x, y in sample]  # overzoom
    for z, x, y in sample:
        same_features(pg.tile(layer, z, x, y), duck.tile(layer, z, x, y))


def test_exports_decode_identically(pair: tuple[Backend, Backend], tmp_path: Path) -> None:
    readers = [
        Reader(MemorySource(export_pmtiles(b, "pop", tmp_path / kind / "pop.pmtiles").read_bytes()))
        for b, kind in zip(pair, ("pg", "duck"))
    ]
    assert readers[0].header()["addressed_tiles_count"] == readers[1].header()["addressed_tiles_count"]
    for z, x, y in pair[0].tiles_with_data("pop"):
        same_features(*(gzip.decompress(r.get(z, x, y)) for r in readers))
