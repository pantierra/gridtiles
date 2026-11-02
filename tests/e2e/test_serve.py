"""The journey on each backend: init -> load -> serve, then tiles over HTTP and the viewer in a browser.

PostgreSQL is served live (Martin on the database) and embedded (tiles from the serve process).
"""

import json
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import mercantile
import pytest
from playwright.sync_api import Page, sync_playwright

from gridtiles.backends.base import open_backend

from ..conftest import FIXTURES, fresh_database

pytestmark = pytest.mark.e2e

ARTIFACTS = Path("build/e2e")
BLOCK = mercantile.tile(13.405, 52.52, 12)
PORTS = {"postgres": (8081, 3001), "postgres-embedded": (8082, None)}


@dataclass
class Journey:
    backend: str
    db: str
    viewer: str  # base URL of the viewer
    tiles: str  # base URL of the layer's tiles, e.g. .../tile_pop or .../tiles/pop
    expected: bytes  # tile 12/BLOCK from the backend, before serving


def get(url: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as e:
        return e.code, b""


def wait_for(url: str, deadline: float) -> None:
    while time.monotonic() < deadline:
        try:
            if get(url)[0] == 200:
                return
        except OSError:
            pass
        time.sleep(0.5)
    raise TimeoutError(url)


@pytest.fixture(scope="module", params=list(PORTS))
def journey(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Journey]:
    """Run the three commands from an empty store; the map must render within 2 minutes."""
    start = time.monotonic()
    tmp = tmp_path_factory.mktemp(request.param)
    db = fresh_database("gridtiles_e2e_" + request.param.replace("-", "_"))
    cli = ["uv", "run", "gridtiles"]
    subprocess.run([*cli, "init", "--db", db], check=True)
    load = [*cli, "load", str(FIXTURES / "points_10k.parquet"), "--layer", "pop", "--spec", str(FIXTURES / "spec_pop.json")]
    subprocess.run([*load, "--db", db], check=True)
    if request.param == "postgres":  # a layer with two snapshots, for the time slider
        spec = tmp / "spec_ts.json"
        spec.write_text(json.dumps({**json.loads((FIXTURES / "spec_pop.json").read_text()), "snapshots": True}))
        for path, day in (("points_10k.parquet", "2026-10-01"), ("points_10k_v2.parquet", "2026-11-01")):
            ts = [*cli, "load", str(FIXTURES / path), "--layer", "ts", "--spec", str(spec), "--snapshot", day]
            subprocess.run([*ts, "--db", db], check=True)
    with open_backend(db) as backend:
        expected = backend.tile("pop", 12, BLOCK.x, BLOCK.y)

    port, martin_port = PORTS[request.param]
    serve = [*cli, "serve", "--db", db, "--port", str(port), "--build", str(tmp / "build")]
    if martin_port:
        serve += ["--live", "--martin-port", str(martin_port)]
        tiles = f"http://localhost:{martin_port}/tile_pop"
    else:
        serve += ["--embedded"]
        tiles = f"http://localhost:{port}/tiles/pop"
    server = subprocess.Popen(serve)
    try:
        if martin_port:
            wait_for(f"http://localhost:{martin_port}/health", start + 120)
        wait_for(f"http://localhost:{port}/config.json", start + 120)
        yield Journey(request.param, db, f"http://localhost:{port}", tiles, expected)
    finally:
        server.terminate()
        server.wait(timeout=30)
    assert time.monotonic() - start < 120


@pytest.fixture(scope="module")
def page(journey: Journey) -> Iterator[Page]:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1024, "height": 768})
        page.goto(journey.viewer)
        page.wait_for_function("window.map && window.current && window.map.loaded() && window.map.areTilesLoaded()")
        yield page
        browser.close()


def test_martin_catalog_lists_layer(journey: Journey) -> None:
    if journey.backend != "postgres":
        pytest.skip("Martin serves live backends only")
    status, body = get(journey.tiles.rsplit("/", 1)[0] + "/catalog")
    assert status == 200 and b"tile_pop" in body


def test_tile_over_http_matches_backend(journey: Journey) -> None:
    status, body = get(f"{journey.tiles}/12/{BLOCK.x}/{BLOCK.y}")
    assert status == 200 and len(body) > 0
    assert body == journey.expected


def test_empty_tile_is_204(journey: Journey) -> None:
    assert get(f"{journey.tiles}/12/0/0")[0] == 204


def test_viewer_renders_cells(journey: Journey, page: Page) -> None:
    assert page.evaluate("window.map.queryRenderedFeatures({layers: ['cells']}).length") > 0
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=ARTIFACTS / f"viewer-{journey.backend}.png")


def test_hover_shows_quadkey(page: Page) -> None:
    qk, x, y = page.evaluate(
        """() => {
            const f = window.map.queryRenderedFeatures({layers: ['cells']})[0];
            const [[w, s], , [e, n]] = f.geometry.coordinates[0];
            const p = window.map.project([(w + e) / 2, (s + n) / 2]);
            return [f.properties.qk, p.x, p.y];
        }"""
    )
    page.mouse.move(x, y)
    page.wait_for_selector(".maplibregl-popup")
    assert qk in page.inner_text(".maplibregl-popup")


def test_legend_shows_breakpoints(page: Page) -> None:
    classes = page.locator("#legend .class")
    assert classes.count() == 8
    first = page.evaluate(
        "window.current.stats.population.breaks[0].toLocaleString(undefined, {maximumSignificantDigits: 4})"
    )
    assert first in classes.nth(0).inner_text()


def test_time_slider_changes_features(journey: Journey, page: Page) -> None:
    if journey.backend != "postgres":
        pytest.skip("snapshots need PostgreSQL")
    rendered = "JSON.stringify(window.map.queryRenderedFeatures({layers: ['cells']}).map((f) => f.properties))"
    page.select_option("#layer", label="ts")
    page.wait_for_function("window.current.name === 'ts' && window.map.loaded() && window.map.areTilesLoaded()")
    assert page.is_visible("#time") and page.inner_text("#snapshot-label") == "2026-11-01"
    latest = page.evaluate(rendered)
    page.evaluate(
        "() => { const s = document.getElementById('snapshot'); s.value = 0; s.dispatchEvent(new Event('input')); }"
    )
    assert page.inner_text("#snapshot-label") == "2026-10-01"
    page.wait_for_function(f"(latest) => {rendered} !== latest", arg=latest)  # times out if nothing changes
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=ARTIFACTS / "viewer-snapshot.png")


def test_upsert_shows_immediately(journey: Journey, tmp_path: Path) -> None:
    """Runs last in the module: it changes the data the tests above read."""
    db = journey.db
    append = ["uv", "run", "gridtiles", "load", str(FIXTURES / "points_small.csv"), "--layer", "pop", "--append"]
    subprocess.run([*append, "--db", db], check=True)
    with open_backend(db) as backend:
        updated = backend.tile("pop", 12, BLOCK.x, BLOCK.y)
    assert updated != journey.expected
    assert get(f"{journey.tiles}/12/{BLOCK.x}/{BLOCK.y}") == (200, updated)
