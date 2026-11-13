import psycopg
import pytest

from ..conftest import FIXTURES
from .helpers import SPEC, create, source


@pytest.fixture
def layer(db: psycopg.Connection) -> psycopg.Connection:
    source(db, FIXTURES / "points_small.csv")
    create(db, SPEC)
    return db


def test_stats(layer: psycopg.Connection) -> None:
    (s,) = layer.execute("select gridtiles.stats('pop')").fetchone()
    assert s["stats"]["population"]["total"] == 360
    assert s["stats"]["completeness"]["total"] is None
    assert len(s["stats"]["population"]["breaks"]) == 7
    west, south, east, north = s["bounds"]
    assert 13.40 < west < east < 13.42 and 52.50 < south < north < 52.53


def test_empty_source(layer: psycopg.Connection) -> None:
    layer.execute("delete from pop_points")
    (s,) = layer.execute("select gridtiles.stats('pop')").fetchone()
    assert s["bounds"] is None and s["stats"]["population"]["total"] is None
