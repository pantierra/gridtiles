"""Steps 2-8 through the backend interface, on every backend."""

import gzip
import json
from pathlib import Path

import mapbox_vector_tile
import mercantile
import pytest
from pmtiles.reader import MemorySource, Reader

from gridtiles import quadkey
from gridtiles.backends.base import Backend, LayerExists
from gridtiles.export import export_pmtiles
from gridtiles.sources import CellLimitExceeded, UnsupportedGeometry
from gridtiles.spec import LayerSpec

from ..fixtures.make_fixtures import dense_block, polygon_wkb, write_vector
from .conftest import FIXTURES, rows

SPEC = LayerSpec.model_validate_json((FIXTURES / "spec_pop.json").read_text())
MINMAX = SPEC.model_copy(
    update={
        "name": "minmax",
        "values": (
            *SPEC.values,
            *LayerSpec.model_validate(
                {
                    **SPEC.model_dump(by_alias=True),
                    "values": [
                        {"name": "pop_min", "from": "pop", "agg": "min"},
                        {"name": "pop_max", "from": "pop", "agg": "max"},
                    ],
                }
            ).values,
        ),
    }
)
RASTER = LayerSpec.model_validate(
    {"name": "raster", "base_zoom": 20, "min_zoom": 12, "values": [{"name": "v", "from": "value", "agg": "sum"}]}
)
BLOCK = mercantile.tile(13.405, 52.52, 12)


def totals(backend: Backend, layer: str, column: str) -> dict[int, tuple]:
    return {
        z: (n, s) for z, n, s in rows(backend, f"select z, sum(n), sum({column}) from gridtiles.cells_{layer} group by z")
    }


def test_layer_registry(backend: Backend) -> None:
    backend.create_layer(SPEC)
    (layer,) = backend.list_layers()
    assert layer.spec == SPEC and layer.tile_zooms == (0, 12)
    with pytest.raises(LayerExists):
        backend.create_layer(SPEC)
    with pytest.raises(LookupError):
        backend.list_layers(["missing"])
    backend.drop_layer("pop")
    assert backend.list_layers() == []


def test_points_small(backend: Backend) -> None:
    backend.create_layer(SPEC)
    report = backend.load("pop", FIXTURES / "points_small.csv")
    assert (report.records_read, report.records_loaded, report.cells_written) == (8, 8, 3)
    expected = json.loads((FIXTURES / "points_small.expected.json").read_text())
    for qk, n, pop, c_sum, c_w in rows(
        backend, "select qk, n, population_sum, completeness_sum, completeness_w from gridtiles.cells_pop where z = 16"
    ):
        e = expected[quadkey.to_text(16, qk)]
        assert (n, pop, c_sum, c_w) == pytest.approx(
            (e["n"], e["population_sum"], e["completeness_sum"], e["completeness_w"]), rel=1e-12
        )


def test_rollup_conserves_and_repeats(backend: Backend) -> None:
    backend.create_layer(MINMAX)
    report = backend.load("minmax", FIXTURES / "points_10k.parquet")
    assert (report.records_loaded, report.skipped_null_geometry) == (9800, 200)
    levels = totals(backend, "minmax", "population_sum")
    assert sorted(levels) == list(range(2, 17))
    assert {n for n, _ in levels.values()} == {9800}
    assert len({round(s, 6) for _, s in levels.values()}) == 1
    before = rows(backend, "select * from gridtiles.cells_minmax order by z, qk")
    backend.rollup("minmax")
    assert rows(backend, "select * from gridtiles.cells_minmax order by z, qk") == before
    (layer,) = backend.list_layers(["minmax"])
    assert layer.cells == len(before) and layer.bounds is not None
    assert set(layer.stats or {}) == {"population", "completeness", "pop_min", "pop_max"}
    assert {name for name, value in (layer.stats or {}).items() if value["total"] is not None} == {"population"}


def test_loading_twice_adds_up(backend: Backend) -> None:
    backend.create_layer(MINMAX)
    backend.load("minmax", FIXTURES / "points_small.csv")
    once = rows(backend, "select n, population_sum, pop_min_min, pop_max_max from gridtiles.cells_minmax order by z, qk")
    backend.load("minmax", FIXTURES / "points_small.csv")
    twice = rows(backend, "select n, population_sum, pop_min_min, pop_max_max from gridtiles.cells_minmax order by z, qk")
    assert twice == [(2 * n, 2 * p, lo, hi) for n, p, lo, hi in once]


def test_polygons(backend: Backend, tmp_path: Path) -> None:
    backend.create_layer(SPEC.model_copy(update={"name": "poly"}))
    qk = quadkey.from_lonlat(13.405, 52.52, 15)
    xmin, ymin, xmax, ymax = quadkey.envelope(15, qk)
    path = tmp_path / "four.gpkg"
    ring = [(xmin, ymin), (xmax, ymin), (xmax, ymax), (xmin, ymax)]
    write_vector(path, {"pop": [400], "osm_ratio": [0.5], "area": [1.0]}, [polygon_wkb(ring)], crs="EPSG:3857")
    backend.load("poly", path)
    cells = rows(backend, "select qk, n, population_sum from gridtiles.cells_poly where z = 16 order by qk")
    assert [c[0] for c in cells] == list(quadkey.child_range(15, qk, 16))
    assert all(n == 1 and p == pytest.approx(100, rel=1e-12) for _, n, p in cells)
    with pytest.raises(CellLimitExceeded):
        backend.load("poly", path, max_cells_per_feature=3)


@pytest.mark.parametrize(
    "ring",
    [
        [(13.40, 52.51), (13.41, 52.52), (13.41, 52.51), (13.40, 52.52)],  # self-intersecting: repaired
        [(10.0, -90.0), (10.01, -90.0), (10.01, -84.0), (10.0, -84.0)],  # reaches the pole: clamped
    ],
    ids=["bowtie", "pole"],
)
def test_awkward_polygons_keep_their_total(backend: Backend, tmp_path: Path, ring: list[tuple[float, float]]) -> None:
    backend.create_layer(SPEC.model_copy(update={"name": "poly", "base_zoom": 10}))
    path = tmp_path / "awkward.gpkg"
    write_vector(path, {"pop": [400], "osm_ratio": [0.5], "area": [1.0]}, [polygon_wkb(ring)])
    backend.load("poly", path)
    ((total,),) = rows(backend, "select sum(population_sum) from gridtiles.cells_poly where z = 10")
    assert total == pytest.approx(400)


def test_polygons_500_conserve_total(backend: Backend) -> None:
    backend.create_layer(SPEC.model_copy(update={"name": "poly"}))
    backend.load("poly", FIXTURES / "polygons_500.gpkg")
    import pyogrio

    total = sum(pyogrio.read_arrow(FIXTURES / "polygons_500.gpkg", columns=["pop"])[1]["pop"].to_pylist())
    for _, pop in totals(backend, "poly", "population_sum").values():
        assert pop == pytest.approx(total, rel=1e-6)


def test_mixed_geometry_raises(backend: Backend, tmp_path: Path) -> None:
    path = tmp_path / "mixed.geojson"
    properties = {"pop": 1, "osm_ratio": 0.5, "area": 1}
    point = {"type": "Feature", "properties": properties, "geometry": {"type": "Point", "coordinates": [13.4, 52.5]}}
    line = {
        "type": "Feature",
        "properties": properties,
        "geometry": {"type": "LineString", "coordinates": [[13.4, 52.5], [13.5, 52.6]]},
    }
    path.write_text(json.dumps({"type": "FeatureCollection", "features": [point, line]}))
    backend.create_layer(SPEC)
    with pytest.raises(UnsupportedGeometry, match="row 1"):
        backend.load("pop", path)


def test_raster(backend: Backend) -> None:
    import rasterio

    backend.create_layer(RASTER)
    report = backend.load("raster", FIXTURES / "raster_100.tif")
    with rasterio.open(FIXTURES / "raster_100.tif") as src:
        data = src.read(1)
    assert report.records_loaded == 8100
    ((total,),) = rows(backend, "select sum(v_sum) from gridtiles.cells_raster where z = 20")
    assert total == data[data != -9999].astype(float).sum()


def test_tiles(backend: Backend, tmp_path: Path) -> None:
    dense_block(tmp_path / "dense_block.parquet")
    backend.create_layer(SPEC)
    backend.load("pop", tmp_path / "dense_block.parquet")
    decoded = mapbox_vector_tile.decode(backend.tile("pop", 12, BLOCK.x, BLOCK.y), default_options={"y_coord_down": True})
    features = decoded["cells"]["features"]
    assert len(features) == 256 and decoded["cells"]["extent"] == 4096
    assert {len(f["properties"]["qk"]) for f in features} == {16}
    assert backend.tile("pop", 12, 0, 0) == b""
    over = mercantile.children(mercantile.children(BLOCK, zoom=16)[0], zoom=18)[0]
    (feature,) = mapbox_vector_tile.decode(backend.tile("pop", 18, over.x, over.y))["cells"]["features"]
    assert feature["properties"]["z"] == 16


def test_export(backend: Backend, tmp_path: Path) -> None:
    backend.create_layer(SPEC)
    backend.load("pop", FIXTURES / "points_10k.parquet")
    path = export_pmtiles(backend, "pop", tmp_path / "pop.pmtiles")
    reader = Reader(MemorySource(path.read_bytes()))
    tiles = list(backend.tiles_with_data("pop"))
    assert reader.header()["addressed_tiles_count"] == len(tiles)
    assert (reader.header()["min_zoom"], reader.header()["max_zoom"]) == (0, 12)
    for z, x, y in tiles[::10]:
        assert gzip.decompress(reader.get(z, x, y)) == backend.tile("pop", z, x, y)
    assert reader.metadata()["gridtiles"]["spec"] == SPEC.model_dump(mode="json", exclude_none=True)
