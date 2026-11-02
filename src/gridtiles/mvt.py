"""Encode cells as a Mapbox Vector Tile (layer `cells`, extent 4096, buffer 0) in Python, for
backends without a native encoder. Geometry and property encoding match PostGIS `ST_AsMVT`: each
cell is a square clipped to the tile, starting at its top-right corner, clockwise (y down).
"""

import struct
from collections.abc import Iterable, Mapping

EXTENT = 4096


def encode(t: int, x: int, y: int, cells: Iterable[tuple[int, int, int, Mapping[str, object]]]) -> bytes:
    """Encode features for tile t/x/y. Each cell is (z, cell_x, cell_y, properties); None values
    are left out. An empty tile is b""."""
    keys: dict[str, int] = {}
    values: dict[bytes, int] = {}  # encoded Value message -> index
    features = bytearray()
    for z, cx, cy, properties in cells:
        tags: list[int] = []
        for key, value in properties.items():
            if value is None:
                continue
            tags.append(keys.setdefault(key, len(keys)))
            tags.append(values.setdefault(_encode_value(value), len(values)))
        feature = _packed(2, tags) + _varint_field(3, 3) + _packed(4, _square(t, x, y, z, cx, cy))
        features += _bytes_field(2, feature)
    if not features:
        return b""
    layer = (
        _bytes_field(1, b"cells")
        + features
        + b"".join(_bytes_field(3, k.encode()) for k in keys)
        + b"".join(_bytes_field(4, v) for v in values)
        + _varint_field(5, EXTENT)
        + _varint_field(15, 2)
    )
    return _bytes_field(3, layer)


def _square(t: int, x: int, y: int, z: int, cx: int, cy: int) -> list[int]:
    """Commands for cell z/cx/cy in tile t/x/y, clipped to the extent."""
    if z >= t:
        size = EXTENT >> (z - t)
        x0, y0 = (cx - (x << (z - t))) * size, (cy - (y << (z - t))) * size
        x1, y1 = x0 + size, y0 + size
    else:  # an ancestor covers the whole tile
        x0, y0, x1, y1 = 0, 0, EXTENT, EXTENT
    x0, y0, x1, y1 = max(x0, 0), max(y0, 0), min(x1, EXTENT), min(y1, EXTENT)
    zz = _zigzag
    return [9, zz(x1), zz(y0), 26, zz(0), zz(y1 - y0), zz(x0 - x1), zz(0), zz(0), zz(y0 - y1), 15]


def _encode_value(value: object) -> bytes:
    """A Value message, typed as PostGIS does: text as string, integers as uint (sint if
    negative), other numbers as double."""
    if isinstance(value, str):
        return _bytes_field(1, value.encode())
    if isinstance(value, bool):
        return _varint_field(7, int(value))
    if isinstance(value, int):
        return _varint_field(5, value) if value >= 0 else _varint_field(6, _zigzag(value))
    if isinstance(value, float):
        return bytes([3 << 3 | 1]) + struct.pack("<d", value)
    raise TypeError(f"cannot encode {value!r} as an MVT value")


def _zigzag(n: int) -> int:
    return (n << 1) ^ (n >> 63)


def _varint(n: int) -> bytes:
    out = bytearray()
    while n > 0x7F:
        out.append(n & 0x7F | 0x80)
        n >>= 7
    out.append(n)
    return bytes(out)


def _varint_field(field: int, n: int) -> bytes:
    return _varint(field << 3) + _varint(n)


def _bytes_field(field: int, data: bytes) -> bytes:
    return _varint(field << 3 | 2) + _varint(len(data)) + data


def _packed(field: int, numbers: list[int]) -> bytes:
    return _bytes_field(field, b"".join(_varint(n) for n in numbers))
