"""The v0.1 journey: init -> load -> serve, then Martin over HTTP and the viewer in a browser."""

import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import mercantile
import psycopg
import pytest
from playwright.sync_api import Page, sync_playwright

from ..conftest import FIXTURES, fresh_database

pytestmark = pytest.mark.e2e

MARTIN_PORT, VIEWER_PORT = 3001, 8081
ARTIFACTS = Path("build/e2e")
BLOCK = mercantile.tile(13.405, 52.52, 12)


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


@pytest.fixture(scope="module")
def journey(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Run the three commands from a fresh database; the map must render within 2 minutes."""
    start = time.monotonic()
    url = fresh_database("gridtiles_e2e")
    cli = ["uv", "run", "gridtiles"]
    subprocess.run([*cli, "init", "--db", url], check=True)
    load = [*cli, "load", str(FIXTURES / "points_10k.parquet"), "--layer", "pop", "--spec", str(FIXTURES / "spec_pop.json")]
    subprocess.run([*load, "--db", url], check=True)
    build = tmp_path_factory.mktemp("build")
    serve = [
        *cli,
        "serve",
        "--db",
        url,
        "--port",
        str(VIEWER_PORT),
        "--martin-port",
        str(MARTIN_PORT),
        "--build",
        str(build),
    ]
    server = subprocess.Popen(serve)
    try:
        wait_for(f"http://localhost:{MARTIN_PORT}/health", start + 120)
        wait_for(f"http://localhost:{VIEWER_PORT}/config.json", start + 120)
        yield url
    finally:
        server.terminate()
        server.wait(timeout=30)
    assert time.monotonic() - start < 120


@pytest.fixture(scope="module")
def page(journey: str) -> Iterator[Page]:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1024, "height": 768})
        page.goto(f"http://localhost:{VIEWER_PORT}/")
        page.wait_for_function("window.map && window.current && window.map.loaded() && window.map.areTilesLoaded()")
        yield page
        browser.close()


def test_catalog_lists_layer(journey: str) -> None:
    status, body = get(f"http://localhost:{MARTIN_PORT}/catalog")
    assert status == 200 and b"tile_pop" in body


def test_tile_over_http_matches_sql(journey: str) -> None:
    status, body = get(f"http://localhost:{MARTIN_PORT}/tile_pop/12/{BLOCK.x}/{BLOCK.y}")
    with psycopg.connect(journey) as conn:
        row = conn.execute("select gridtiles.mvt('pop', 12, %s, %s)", (BLOCK.x, BLOCK.y)).fetchone()
    assert status == 200 and row is not None and len(body) > 0
    assert body == bytes(row[0])


def test_empty_tile_is_204(journey: str) -> None:
    assert get(f"http://localhost:{MARTIN_PORT}/tile_pop/12/0/0")[0] == 204


def test_viewer_renders_cells(page: Page) -> None:
    assert page.evaluate("window.map.queryRenderedFeatures({layers: ['cells']}).length") > 0
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=ARTIFACTS / "viewer.png")


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


def test_upsert_shows_immediately(journey: str) -> None:
    """Runs last in the module: it changes the data the tests above read."""

    def tile() -> bytes:
        with psycopg.connect(journey) as conn:
            row = conn.execute("select gridtiles.mvt('pop', 12, %s, %s)", (BLOCK.x, BLOCK.y)).fetchone()
        assert row is not None
        return bytes(row[0])

    before = tile()
    append = ["uv", "run", "gridtiles", "load", str(FIXTURES / "points_small.csv"), "--layer", "pop", "--append"]
    subprocess.run([*append, "--db", journey], check=True)
    updated = tile()
    assert updated != before
    assert get(f"http://localhost:{MARTIN_PORT}/tile_pop/12/{BLOCK.x}/{BLOCK.y}") == (200, updated)
