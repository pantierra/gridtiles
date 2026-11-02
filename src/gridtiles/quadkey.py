"""Quadkeys as integer Morton codes; mirrors `sql/010_quadkey.sql` exactly.

A cell is identified by (z, qk): x in the even bits, y in the odd bits, XYZ tile addressing.
"""

import math

MAX_ZOOM = 30
HALF_WORLD = 20037508.342789244  # EPSG:3857 half extent in metres
WORLD = 40075016.68557849
MAX_LAT = 85.0511287798


def _check_zoom(z: int, lo: int = 0, hi: int = MAX_ZOOM) -> None:
    if not lo <= z <= hi:
        raise ValueError(f"zoom {z} outside [{lo}, {hi}]")


def _check_cell(z: int, qk: int) -> None:
    _check_zoom(z)
    if not 0 <= qk < 4**z:
        raise ValueError(f"quadkey {qk} outside [0, 4^{z})")


def _spread(v: int) -> int:
    """Interleave zero bits: bit i of v moves to bit 2i."""
    v = (v | v << 16) & 0x0000FFFF0000FFFF
    v = (v | v << 8) & 0x00FF00FF00FF00FF
    v = (v | v << 4) & 0x0F0F0F0F0F0F0F0F
    v = (v | v << 2) & 0x3333333333333333
    return (v | v << 1) & 0x5555555555555555


def _compact(v: int) -> int:
    """Inverse of `_spread`: keep the even bits, packed."""
    v &= 0x5555555555555555
    v = (v | v >> 1) & 0x3333333333333333
    v = (v | v >> 2) & 0x0F0F0F0F0F0F0F0F
    v = (v | v >> 4) & 0x00FF00FF00FF00FF
    v = (v | v >> 8) & 0x0000FFFF0000FFFF
    return (v | v >> 16) & 0x00000000FFFFFFFF


def _qk(x: int, y: int) -> int:
    return _spread(x) | _spread(y) << 1


def from_xyz(z: int, x: int, y: int) -> int:
    _check_zoom(z)
    if not (0 <= x < 2**z and 0 <= y < 2**z):
        raise ValueError(f"tile {x}/{y} outside [0, 2^{z})")
    return _qk(x, y)


def to_xy(z: int, qk: int) -> tuple[int, int]:
    _check_cell(z, qk)
    return _compact(qk), _compact(qk >> 1)


def to_text(z: int, qk: int) -> str:
    """Bing quadkey string: exactly z base-4 digits, most significant first."""
    _check_cell(z, qk)
    return "".join(str(qk >> 2 * i & 3) for i in reversed(range(z)))


def from_text(text: str) -> int:
    """Parse a quadkey string; its zoom is its length."""
    _check_zoom(len(text))
    if text.strip("0123"):
        raise ValueError(f"quadkey {text!r} has a digit outside 0-3")
    return int(text, 4) if text else 0


def parent(z: int, qk: int, z_to: int) -> int:
    _check_cell(z, qk)
    _check_zoom(z_to, 0, z)
    return qk >> 2 * (z - z_to)


def child_range(z: int, qk: int, z_to: int) -> range:
    """Half-open range of descendant quadkeys at zoom z_to."""
    _check_cell(z, qk)
    _check_zoom(z_to, z)
    shift = 2 * (z_to - z)
    return range(qk << shift, (qk + 1) << shift)


def envelope(z: int, qk: int) -> tuple[float, float, float, float]:
    """Cell bounds (xmin, ymin, xmax, ymax) in EPSG:3857, as `ST_TileEnvelope`."""
    x, y = to_xy(z, qk)
    size = WORLD / 2**z
    return -HALF_WORLD + x * size, HALF_WORLD - (y + 1) * size, -HALF_WORLD + (x + 1) * size, HALF_WORLD - y * size


def to_mercator(lon: float, lat: float) -> tuple[float, float]:
    """WGS84 -> EPSG:3857 (spherical), latitude clamped to the Web Mercator limit."""
    lat = math.radians(max(-MAX_LAT, min(MAX_LAT, lat)))
    return lon * HALF_WORLD / 180, math.log(math.tan(math.pi / 4 + lat / 2)) * HALF_WORLD / math.pi


def from_lonlat(lon: float, lat: float, z: int) -> int:
    """Cell containing a WGS84 point; latitude is clamped to the Web Mercator limit."""
    _check_zoom(z)
    return _from_mercator(*to_mercator(lon, lat), z)


def _from_mercator(mx: float, my: float, z: int) -> int:
    """EPSG:3857 metres -> cell; points on a boundary go east / south, the edges are clamped."""
    size = WORLD / 2**z
    top = 2**z - 1
    x = min(max(math.floor((mx + HALF_WORLD) / size), 0), top)
    y = min(max(math.floor((HALF_WORLD - my) / size), 0), top)
    return _qk(x, y)
