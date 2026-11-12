"""A registered layer as consumers (tile server, viewer, exporters) see it; storage-independent."""

from dataclasses import dataclass
from typing import Any

from gridtiles.spec import LayerSpec


@dataclass(frozen=True)
class Layer:
    spec: LayerSpec
    stats: dict[str, Any] | None  # per value: total, min, max, breaks (set by rollup)
    cells: int | None  # rows over all zooms (set by rollup)
    bounds: tuple[float, float, float, float] | None  # WGS84 [west, south, east, north] of the base cells

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def tile_zooms(self) -> tuple[int, int]:
        """Published tile zoom range; viewers overzoom beyond."""
        return max(0, self.spec.min_zoom - self.spec.detail), max(0, self.spec.base_zoom - self.spec.detail)
