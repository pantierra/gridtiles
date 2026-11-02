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
    snapshots: tuple[str, ...] = ()  # ISO dates, oldest first, on snapshot layers

    @property
    def name(self) -> str:
        return self.spec.name

    def manifest(self) -> dict[str, Any]:
        """What a consumer needs to style and identify the layer (stored in PMTiles metadata)."""
        from gridtiles import __version__

        return {
            "version": __version__,
            "spec": self.spec.model_dump(mode="json", exclude_none=True),
            "stats": self.stats,
            "bounds": self.bounds,
            "snapshots": list(self.snapshots),
        }

    @property
    def tile_zooms(self) -> tuple[int, int]:
        """Published tile zoom range; viewers overzoom beyond."""
        return max(0, self.spec.min_zoom - self.spec.detail), max(0, self.spec.base_zoom - self.spec.detail)
