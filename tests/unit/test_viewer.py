import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import psycopg
import pytest

from gridtiles import viewer
from gridtiles.layer import Layer
from gridtiles.spec import LayerSpec
from gridtiles.viewer import _Refreshed

from ..conftest import FIXTURES

LAYER = Layer(LayerSpec.model_validate_json((FIXTURES / "spec_pop.json").read_text()), times=("2024-01-01",))


def test_config() -> None:
    (entry,) = viewer.config([LAYER], lambda layer: f"/tiles/{layer.name}")["layers"]
    assert (entry["tiles"], entry["minzoom"], entry["maxzoom"], entry["times"]) == ("/tiles/pop", 0, 12, ["2024-01-01"])


def test_config_json_is_built_per_request(tmp_path: Path) -> None:
    site = viewer.write(tmp_path, [LAYER], lambda _: "static")
    calls = []

    def live() -> dict:
        calls.append(1)
        return {"layers": [], "call": len(calls)}

    server = ThreadingHTTPServer(("127.0.0.1", 0), viewer.handler(site, live, max_age=0))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        for call in (1, 2):
            with urllib.request.urlopen(f"{base}/config.json") as response:
                assert json.load(response)["call"] == call
        with urllib.request.urlopen(f"{base}/index.html") as response:
            assert b"maplibregl" in response.read()
    finally:
        server.shutdown()


def test_refreshed(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [0.0]
    monkeypatch.setattr("gridtiles.viewer.time.monotonic", lambda: clock[0])
    builds: list[int] = []
    fail = [False]

    def build() -> dict:
        if fail[0]:
            raise psycopg.OperationalError("database down")
        builds.append(1)
        return {"build": len(builds)}

    refreshed = _Refreshed(build, 300)
    assert refreshed() == {"build": 1}
    clock[0] = 299
    assert refreshed() == {"build": 1}  # still fresh
    clock[0], fail[0] = 301, True
    assert refreshed() == {"build": 1}  # the last result while a rebuild fails
    fail[0] = False
    assert refreshed() == {"build": 2}
    with pytest.raises(psycopg.OperationalError):
        fail[0] = True
        _Refreshed(build, 300)()  # nothing to fall back to
