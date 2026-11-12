import os
import urllib.request

import pytest

pytestmark = pytest.mark.e2e

MARTIN_URL = os.environ.get("GRIDTILES_MARTIN_URL", "http://localhost:3000")


def test_martin_is_healthy() -> None:
    with urllib.request.urlopen(f"{MARTIN_URL}/health", timeout=10) as response:
        assert response.status == 200
