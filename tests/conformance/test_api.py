"""The query API over every backend, against brute-force oracles in Python."""

import json
import math
import random

import pytest
import shapely
from fastapi.testclient import TestClient

from gridtiles import quadkey
from gridtiles.api import create_app
from gridtiles.backends.base import Backend

from .conftest import FIXTURES, rows
from .test_operations import SPEC

ZB = SPEC.base_zoom


@pytest.fixture
def client(backend: Backend) -> TestClient:
    backend.create_layer(SPEC)
    backend.load("pop", FIXTURES / "points_10k.parquet")
    return TestClient(create_app(backend))


def cells(backend: Backend) -> list[tuple]:
    return rows(
        backend, f"select qk, n, population_sum, completeness_sum, completeness_w from gridtiles.cells_pop where z = {ZB}"
    )


def expected(selected: list[tuple]) -> dict:
    n = sum(c[1] for c in selected)
    pop = sum(c[2] for c in selected) if selected else None
    weight = sum(c[4] for c in selected)
    mean = sum(c[3] for c in selected) / weight if weight else None
    return {"cells": len(selected), "n": n, "population": pop, "completeness": mean}


def check(body: dict, want: dict) -> None:
    assert (body["cells"], body["n"]) == (want["cells"], want["n"])
    for value in ("population", "completeness"):
        assert body["values"][value] == pytest.approx(want[value], rel=1e-9)


def centroid_lonlat(qk: int) -> tuple[float, float]:
    xmin, ymin, xmax, ymax = quadkey.envelope(ZB, qk)
    x, y = (xmin + xmax) / 2, (ymin + ymax) / 2
    return x * 180 / quadkey.HALF_WORLD, math.degrees(
        2 * math.atan(math.exp(y * math.pi / quadkey.HALF_WORLD)) - math.pi / 2
    )


def test_layers(client: TestClient) -> None:
    assert [layer["spec"]["name"] for layer in client.get("/layers").json()] == ["pop"]
    assert client.get("/layers/pop").json()["spec"]["base_zoom"] == ZB
    assert client.get("/layers/missing").status_code == 404


def test_cell(client: TestClient, backend: Backend) -> None:
    qk, n, pop, c_sum, c_w = random.Random(1).choice(cells(backend))
    text = quadkey.to_text(ZB, qk)
    body = client.get(f"/layers/pop/cells/{text}").json()
    assert (body["qk"], body["z"], body["n"]) == (text, ZB, n)
    assert body["values"] == pytest.approx({"population": pop, "completeness": c_sum / c_w}, rel=1e-12)
    assert client.get(f"/layers/pop/cells/{'0' * ZB}").status_code == 404
    assert client.get("/layers/pop/cells/1234").status_code == 422
    assert client.get(f"/layers/missing/cells/{text}").status_code == 404


def test_prefix_stats(client: TestClient, backend: Backend) -> None:
    all_cells = cells(backend)
    prefix = quadkey.to_text(ZB, all_cells[0][0])[:12]
    keys = quadkey.child_range(12, quadkey.from_text(prefix), ZB)
    body = client.get("/layers/pop/stats", params={"prefix": prefix}).json()
    check(body, expected([c for c in all_cells if c[0] in keys]))
    assert "method" not in body or body["method"] is None


def test_bbox_stats(client: TestClient, backend: Backend) -> None:
    west, south, east, north = 13.3, 52.4, 13.5, 52.55
    body = client.get("/layers/pop/stats", params={"bbox": f"{west},{south},{east},{north}"}).json()
    inside = [
        c for c in cells(backend) if west <= centroid_lonlat(c[0])[0] <= east and south <= centroid_lonlat(c[0])[1] <= north
    ]
    check(body, expected(inside))
    assert body["method"] == "centroid"


def test_polygon_stats(client: TestClient, backend: Backend) -> None:
    ring = [[13.2, 52.4], [13.6, 52.38], [13.5, 52.62], [13.2, 52.4]]
    body = client.post("/layers/pop/stats", json={"type": "Polygon", "coordinates": [ring]}).json()
    polygon = shapely.Polygon(ring)
    check(body, expected([c for c in cells(backend) if polygon.contains(shapely.Point(*centroid_lonlat(c[0])))]))
    assert body["method"] == "centroid"


def test_invalid_requests(client: TestClient) -> None:
    assert client.get("/layers/pop/stats").status_code == 422
    assert client.get("/layers/pop/stats", params={"bbox": "1,2,3"}).status_code == 422
    assert client.get("/layers/pop/stats", params={"prefix": "0", "zoom": 30}).status_code == 422
    assert client.get("/layers/missing/stats", params={"prefix": "0"}).status_code == 404


def test_openapi_matches_snapshot(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    schema["info"].pop("version")  # follows the release
    assert schema == json.loads((FIXTURES / "openapi.json").read_text())
