import psycopg
import pytest
from psycopg.types.json import Jsonb

from ..conftest import FIXTURES
from .helpers import SPEC, create, source


@pytest.fixture
def layer(db: psycopg.Connection) -> psycopg.Connection:
    source(db, FIXTURES / "points_small.csv")
    create(db, SPEC)
    return db


def cached(db: psycopg.Connection, max_age: str | None = None, params: dict | None = None) -> dict:
    (s,) = db.execute("select gridtiles.cached_stats('pop', %s, %s)", (Jsonb(params or {}), max_age)).fetchone()
    return s


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


def test_cache_lags_until_refreshed(layer: psycopg.Connection) -> None:
    first = cached(layer)
    layer.execute("delete from pop_points where pop > 40")
    assert cached(layer) == first  # any cached result
    assert cached(layer, "1 hour") == first
    fresh = cached(layer, "0")
    assert fresh["stats"]["population"]["total"] == 100
    assert fresh["computed_at"] > first["computed_at"]
    assert cached(layer) == fresh


def test_cache_is_per_parameters_and_layer(layer: psycopg.Connection) -> None:
    cached(layer)
    with pytest.raises(psycopg.errors.InvalidParameterValue), layer.transaction():
        cached(layer, params={"at": "2024-01-01"})
    layer.execute("select gridtiles.drop_layer('pop')")
    assert layer.execute("select count(*) from gridtiles.stats_cache").fetchone() == (0,)
