"""Region statistics over a layer's cells, as plain SQL any backend can run.

A region is a quadkey prefix (its descendants), a WGS84 bbox or a GeoJSON polygon (edges straight in
lon/lat); bbox and polygon regions take the cells at the chosen zoom whose centroid lies inside
(`"method": "centroid"`).
Values are merged like a rollup: sums and counts add up, means are re-weighted, min and max kept.
"""

import math
from dataclasses import dataclass
from datetime import date
from typing import Any

from gridtiles import quadkey
from gridtiles.quadkey import HALF_WORLD, WORLD, to_mercator
from gridtiles.spec import LayerSpec


@dataclass(frozen=True)
class Query:
    sql: str
    zoom: int
    method: str | None


def build(
    spec: LayerSpec,
    *,
    prefix: str | None = None,
    bbox: tuple[float, float, float, float] | None = None,
    polygon: dict[str, Any] | None = None,
    zoom: int | None = None,
    snapshot: date | None = None,
) -> Query:
    """The stats query; raises ValueError for an invalid region, zoom or snapshot."""
    if sum(r is not None for r in (prefix, bbox, polygon)) != 1:
        raise ValueError("give exactly one of prefix, bbox and polygon")
    z = spec.base_zoom if zoom is None else zoom
    if not spec.min_zoom <= z <= spec.base_zoom:
        raise ValueError(f"zoom {z} outside the layer's zooms [{spec.min_zoom}, {spec.base_zoom}]")
    table = f"gridtiles.cells_{spec.name}"
    where = [f"z = {z}"]
    if spec.snapshots:
        where.append(
            f"snapshot = '{snapshot.isoformat()}'" if snapshot else f"snapshot = (select max(snapshot) from {table})"
        )
    elif snapshot:
        raise ValueError(f"layer {spec.name!r} has no snapshots")

    method = None
    if prefix is not None:
        if len(prefix) > z:
            raise ValueError(f"prefix {prefix!r} is finer than zoom {z}")
        keys = quadkey.child_range(len(prefix), quadkey.from_text(prefix), z)
        where.append(f"qk >= {keys.start} and qk < {keys.stop}")
    else:
        method = "centroid"
        x, y = "gridtiles._compact(qk)", "gridtiles._compact(qk >> 1)"
        size = WORLD / 2**z
        if bbox is not None:
            west, south, east, north = bbox
            (mx0, my0), (mx1, my1) = to_mercator(west, south), to_mercator(east, north)
            # cell (i, j) has its centroid at (-H + (i + 0.5) size, H - (j + 0.5) size)
            i0, i1 = math.ceil((mx0 + HALF_WORLD) / size - 0.5), math.floor((mx1 + HALF_WORLD) / size - 0.5)
            j0, j1 = math.ceil((HALF_WORLD - my1) / size - 0.5), math.floor((HALF_WORLD - my0) / size - 0.5)
            where.append(f"{x} between {i0} and {i1} and {y} between {j0} and {j1}")
        else:
            assert polygon is not None
            # GeoJSON edges are straight in lon/lat: test centroids there (inverse spherical Mercator).
            mx, my = f"(-{HALF_WORLD} + ({x} + 0.5) * {size!r})", f"({HALF_WORLD} - ({y} + 0.5) * {size!r})"
            lon, lat = f"{mx} * 180 / {HALF_WORLD}", f"degrees(2 * atan(exp({my} * pi() / {HALF_WORLD})) - pi() / 2)"
            where.append(f"ST_Contains(ST_GeomFromText('{_wkt(polygon)}'), ST_Point({lon}, {lat}))")

    values = "".join(f", {_merged(v.agg, v.name)}" for v in spec.values)
    sql = f"select count(*), coalesce(sum(n), 0){values} from {table} where {' and '.join(where)}"
    return Query(sql, z, method)


def result(spec: LayerSpec, query: Query, row: tuple[Any, ...], **region: Any) -> dict[str, Any]:
    cells, n, *values = row
    out: dict[str, Any] = {"layer": spec.name, "zoom": query.zoom, **region, "cells": int(cells), "n": int(n)}
    out["values"] = {v.name: (float(x) if x is not None else None) for v, x in zip(spec.values, values)}
    if query.method:
        out["method"] = query.method
    return out


def _merged(agg: str, name: str) -> str:
    if agg == "count":
        return "coalesce(sum(n), 0)"
    if agg == "mean":
        return f"sum({name}_sum) / nullif(sum({name}_w), 0)"
    if agg == "sum":
        return f"sum({name}_sum)"
    return f"{agg}({name}_{agg})"


def _wkt(geometry: dict[str, Any]) -> str:
    """WKT of a GeoJSON Polygon or MultiPolygon (or a Feature holding one)."""
    if geometry.get("type") == "Feature":
        geometry = geometry["geometry"]
    kind = geometry.get("type")
    if kind not in ("Polygon", "MultiPolygon"):
        raise ValueError(f"expected a GeoJSON Polygon or MultiPolygon, got {kind!r}")
    polygons = [geometry["coordinates"]] if kind == "Polygon" else geometry["coordinates"]

    def ring(points: list[list[float]]) -> str:
        return "(" + ", ".join(f"{float(lon)!r} {float(lat)!r}" for lon, lat, *_ in points) + ")"

    return "MULTIPOLYGON(" + ", ".join("(" + ", ".join(ring(r) for r in rings) + ")" for rings in polygons) + ")"
