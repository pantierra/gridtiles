"""A registered layer as consumers (tile server, viewer, exporters) see it."""

from dataclasses import dataclass, field
from typing import Any

from gridtiles.spec import LayerSpec


@dataclass(frozen=True)
class Layer:
    spec: LayerSpec
    stats: dict[str, Any] | None = None  # per value: total, min, max, breaks (from gridtiles.stats)
    bounds: tuple[float, float, float, float] | None = None  # WGS84 [west, south, east, north] of the base squares
    params: dict[str, str] = field(default_factory=dict)  # tile parameters the stats describe

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def tile_zooms(self) -> tuple[int, int]:
        """Published tile zoom range; viewers overzoom beyond."""
        return max(0, self.spec.min_zoom - self.spec.detail), max(0, self.spec.base_zoom - self.spec.detail)
