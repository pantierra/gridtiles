"""The query API against brute-force oracles in Python."""

import json
import math
import random
from collections.abc import Iterator
from datetime import date

import psycopg
import pytest
import shapely
from fastapi.testclient import TestClient

from gridtiles import db, quadkey
from gridtiles.api import create_app
from gridtiles.ingest import load
from gridtiles.spec import LayerSpec

from ..conftest import FIXTURES, fresh_database

SPEC = LayerSpec.model_validate_json((FIXTURES / "spec_pop.json").read_text())
ZB = SPEC.base_zoom


@pytest.fixture(scope="module")
def url() -> str:
    """A committed database: the API reads through its own connections."""
    url = fresh_database("gridtiles_api")
    with psycopg.connect(url) as conn:
        db.install(conn)
        db.create_layer(conn, SPEC)
        load(conn, "pop", FIXTURES / "points_10k.parquet")
        db.create_layer(conn, SPEC.model_copy(update={"name": "ts", "snapshots": True}))
        load(conn, "ts", FIXTURES / "points_10k.parquet", snapshot=date(2026, 10, 1))
        load(conn, "ts", FIXTURES / "points_10k_v2.parquet", snapshot=date(2026, 11, 1))
    return url


@pytest.fixture(scope="module")
def client(url: str) -> Iterator[TestClient]:
    with TestClient(create_app(url)) as client:
        yield client


def cells(url: str, layer: str = "pop", where: str = "") -> list[tuple]:
    with psycopg.connect(url) as conn:
        return conn.execute(
            f"select qk, n, population_sum, completeness_sum, completeness_w from gridtiles.cells_{layer}"
            f" where z = {ZB}{where}"
        ).fetchall()


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
    assert [layer["spec"]["name"] for layer in client.get("/layers").json()] == ["pop", "ts"]
    assert client.get("/layers/pop").json()["spec"]["base_zoom"] == ZB
    assert client.get("/layers/ts").json()["snapshots"] == ["2026-10-01", "2026-11-01"]
    assert client.get("/layers/missing").status_code == 404


def test_cell(client: TestClient, url: str) -> None:
    qk, n, pop, c_sum, c_w = random.Random(1).choice(cells(url))
    text = quadkey.to_text(ZB, qk)
    body = client.get(f"/layers/pop/cells/{text}").json()
    assert (body["qk"], body["z"], body["n"]) == (text, ZB, n)
    assert body["values"] == pytest.approx({"population": pop, "completeness": c_sum / c_w}, rel=1e-12)
    assert client.get(f"/layers/pop/cells/{'0' * ZB}").status_code == 404
    assert client.get("/layers/pop/cells/1234").status_code == 422
    assert client.get(f"/layers/missing/cells/{text}").status_code == 404


def test_prefix_stats(client: TestClient, url: str) -> None:
    all_cells = cells(url)
    prefix = quadkey.to_text(ZB, all_cells[0][0])[:12]
    keys = quadkey.child_range(12, quadkey.from_text(prefix), ZB)
    body = client.get("/layers/pop/stats", params={"prefix": prefix}).json()
    check(body, expected([c for c in all_cells if c[0] in keys]))
    assert "method" not in body or body["method"] is None


def test_bbox_stats(client: TestClient, url: str) -> None:
    west, south, east, north = 13.3, 52.4, 13.5, 52.55
    body = client.get("/layers/pop/stats", params={"bbox": f"{west},{south},{east},{north}"}).json()
    inside = [
        c for c in cells(url) if west <= centroid_lonlat(c[0])[0] <= east and south <= centroid_lonlat(c[0])[1] <= north
    ]
    check(body, expected(inside))
    assert body["method"] == "centroid"


def test_polygon_stats(client: TestClient, url: str) -> None:
    ring = [[13.2, 52.4], [13.6, 52.38], [13.5, 52.62], [13.2, 52.4]]
    body = client.post("/layers/pop/stats", json={"type": "Polygon", "coordinates": [ring]}).json()
    polygon = shapely.Polygon(ring)
    check(body, expected([c for c in cells(url) if polygon.contains(shapely.Point(*centroid_lonlat(c[0])))]))
    assert body["method"] == "centroid"


def test_snapshot_stats(client: TestClient, url: str) -> None:
    for day in (None, "2026-10-01"):
        params = {"prefix": ""} | ({"snapshot": day} if day else {})
        body = client.get("/layers/ts/stats", params=params).json()
        check(body, expected(cells(url, "ts", f" and snapshot = '{day or '2026-11-01'}'")))
    assert client.get("/layers/pop/stats", params={"prefix": "", "snapshot": "2026-10-01"}).status_code == 422


def test_invalid_requests(client: TestClient) -> None:
    assert client.get("/layers/pop/stats").status_code == 422
    assert client.get("/layers/pop/stats", params={"bbox": "1,2,3"}).status_code == 422
    assert client.get("/layers/pop/stats", params={"prefix": "0", "zoom": 30}).status_code == 422
    assert client.get("/layers/missing/stats", params={"prefix": "0"}).status_code == 404


def test_openapi_matches_snapshot(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    schema["info"].pop("version")  # follows the release
    assert schema == json.loads((FIXTURES / "openapi.json").read_text())
