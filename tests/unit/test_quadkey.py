import random

import mercantile
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from gridtiles import quadkey

many = settings(max_examples=10_000)


@st.composite
def cells(draw: st.DrawFn, min_z: int = 0) -> tuple[int, int, int]:
    z = draw(st.integers(min_z, quadkey.MAX_ZOOM))
    return z, draw(st.integers(0, 2**z - 1)), draw(st.integers(0, 2**z - 1))


def test_reference_vector() -> None:
    assert quadkey.from_xyz(3, 3, 5) == 39
    assert quadkey.to_text(3, 39) == "213"
    assert quadkey.from_text("213") == 39


def test_zoom_extremes() -> None:
    assert quadkey.from_xyz(0, 0, 0) == 0
    assert quadkey.to_text(0, 0) == ""
    assert quadkey.from_text("") == 0
    top = 2**30 - 1
    assert quadkey.from_xyz(30, top, top) == 4**30 - 1


@many
@given(cells())
def test_xyz_round_trip(cell: tuple[int, int, int]) -> None:
    z, x, y = cell
    assert quadkey.to_xy(z, quadkey.from_xyz(z, x, y)) == (x, y)


@many
@given(cells())
def test_text_round_trip(cell: tuple[int, int, int]) -> None:
    z, x, y = cell
    qk = quadkey.from_xyz(z, x, y)
    text = quadkey.to_text(z, qk)
    assert len(text) == z
    assert quadkey.from_text(text) == qk


@many
@given(cells(min_z=1), st.data())
def test_parent_of_child(cell: tuple[int, int, int], data: st.DataObject) -> None:
    z, x, y = cell
    z_to = data.draw(st.integers(0, z))
    parent = quadkey.parent(z, quadkey.from_xyz(z, x, y), z_to)
    shift = z - z_to
    assert parent == quadkey.from_xyz(z_to, x >> shift, y >> shift)


@many
@given(cells(), st.data())
def test_child_range_contains_child(cell: tuple[int, int, int], data: st.DataObject) -> None:
    z, x, y = cell
    z_from = data.draw(st.integers(0, z))
    child = quadkey.from_xyz(z, x, y)
    parent = quadkey.parent(z, child, z_from)
    assert child in quadkey.child_range(z_from, parent, z)


def test_from_lonlat_matches_mercantile() -> None:
    rng = random.Random(42)
    checked = 0
    for _ in range(10_000):
        lon, lat = rng.uniform(-180, 180), rng.uniform(-85, 85)
        for z in range(25):
            tile = mercantile.tile(lon, lat, z)
            b = mercantile.bounds(tile)
            if min(abs(lon - b.west), abs(lon - b.east), abs(lat - b.south), abs(lat - b.north)) < 1e-9:
                continue
            assert quadkey.from_lonlat(lon, lat, z) == quadkey.from_xyz(z, tile.x, tile.y)
            checked += 1
    assert checked > 240_000


def test_from_lonlat_clamps() -> None:
    assert quadkey.to_xy(2, quadkey.from_lonlat(180, 90, 2)) == (3, 0)
    assert quadkey.to_xy(2, quadkey.from_lonlat(-180, -90, 2)) == (0, 3)


def test_envelope() -> None:
    r = quadkey.HALF_WORLD
    assert quadkey.envelope(0, 0) == pytest.approx((-r, -r, r, r))
    assert quadkey.envelope(1, quadkey.from_xyz(1, 1, 0)) == pytest.approx((0, 0, r, r))


@pytest.mark.parametrize(
    "call",
    [
        lambda: quadkey.from_xyz(3, 8, 0),
        lambda: quadkey.from_xyz(3, 0, -1),
        lambda: quadkey.from_xyz(31, 0, 0),
        lambda: quadkey.from_xyz(-1, 0, 0),
        lambda: quadkey.to_xy(2, 16),
        lambda: quadkey.to_text(31, 0),
        lambda: quadkey.from_text("124"),
        lambda: quadkey.from_text("0" * 31),
        lambda: quadkey.parent(3, 0, 4),
        lambda: quadkey.child_range(3, 0, 2),
        lambda: quadkey.from_lonlat(0, 0, 31),
    ],
)
def test_invalid_input_raises(call) -> None:
    with pytest.raises(ValueError):
        call()
