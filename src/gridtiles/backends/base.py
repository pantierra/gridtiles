"""The backend interface: every store implements these operations; the CLI and exporter use only
this module. Implementations are resolved by name, so callers never import them directly.
"""

import re
from collections.abc import Iterator
from datetime import date
from importlib import import_module
from typing import Any, Literal, Protocol, Self

from gridtiles.layer import Layer
from gridtiles.sources import MAX_CELLS_PER_FEATURE, Kind, LoadReport, SourcePath
from gridtiles.spec import LayerSpec

Capability = Literal["live", "incremental", "native_mvt", "snapshots"]
UpsertMode = Literal["add", "replace"]
"""live: an external tile server can read the store; incremental: upserts touch only what changed;
native_mvt: the store encodes tiles itself; snapshots: layers keep dated versions, and diffs."""

BACKENDS = {
    "postgres": "gridtiles.backends.postgres:PostgresBackend",
    "duckdb": "gridtiles.backends.duckdb:DuckDBBackend",
}


class MissingCapability(RuntimeError):
    """The backend cannot do what was asked."""


class LayerExists(ValueError):
    """A layer with this name is already registered."""


class Backend(Protocol):
    capabilities: frozenset[Capability]
    url: str  # where the data lives: a PostgreSQL URL or a DuckDB file path

    def __enter__(self) -> Self: ...
    def __exit__(self, *exc: object) -> None: ...

    def install(self) -> str:
        """Create or upgrade the gridtiles schema (idempotent); returns its version."""
        ...

    def create_layer(self, spec: LayerSpec) -> None: ...
    def drop_layer(self, name: str) -> None: ...
    def list_layers(self, names: list[str] | None = None) -> list[Layer]:
        """All or the named layers; raises LookupError for an unknown name."""
        ...

    def load(
        self,
        layer: str,
        path: SourcePath,
        *,
        kind: Kind | None = None,
        lon: str | None = None,
        lat: str | None = None,
        crs: str = "EPSG:4326",
        band: int = 1,
        rollup: bool = True,
        max_cells_per_feature: int | None = MAX_CELLS_PER_FEATURE,
        snapshot: date | None = None,
    ) -> LoadReport:
        """Aggregate points, polygons or a raster (detected, or forced by `kind`) into base cells;
        on snapshot layers into the cells of `snapshot`."""
        ...

    def rollup(self, layer: str) -> None: ...
    def upsert(
        self,
        layer: str,
        path: SourcePath,
        *,
        mode: UpsertMode = "add",
        refresh_stats: bool = False,
        kind: Kind | None = None,
        lon: str | None = None,
        lat: str | None = None,
        crs: str = "EPSG:4326",
        band: int = 1,
        max_cells_per_feature: int | None = MAX_CELLS_PER_FEATURE,
    ) -> LoadReport:
        """Apply a batch to the base cells and update what depends on them; the result equals a
        full rollup. "add" merges like `load`; "replace" overwrites the base cells the batch touches.
        Stats, cell count and bounds are refreshed only with `refresh_stats` (they scan the base level).
        """
        ...

    def tile(self, layer: str, z: int, x: int, y: int, snapshot: date | None = None) -> bytes:
        """One MVT (of `snapshot`, or the latest); empty bytes for a tile without data. Safe to call
        from several threads."""
        ...

    def tiles_with_data(self, layer: str) -> Iterator[tuple[int, int, int]]:
        """(z, x, y) of every tile with data in the published zoom range."""
        ...

    def diff(self, a: str, b: str, name: str) -> None:
        """Create the read-only layer `name` holding B - A of two "layer[@YYYY-MM-DD]" references."""
        ...

    def query(
        self,
        layer: str,
        *,
        prefix: str | None = None,
        bbox: tuple[float, float, float, float] | None = None,
        polygon: dict[str, Any] | None = None,
        zoom: int | None = None,
        snapshot: date | None = None,
    ) -> dict[str, Any]:
        """Stats of the cells at `zoom` (default: base zoom) in a region: a quadkey prefix, a WGS84
        bbox (west, south, east, north) or a GeoJSON polygon; see `gridtiles.query`."""
        ...

    def manifest(self, layer: str) -> dict[str, Any]: ...


def open_backend(url: str, kind: str | None = None) -> Backend:
    """Open the backend named `kind` ("postgres" or "duckdb") on `url`. By default a postgres:// or
    postgresql:// URL or a libpq "key=value" string opens PostgreSQL, anything else a DuckDB file."""
    kind = kind or kind_of(url)
    if kind not in BACKENDS:
        raise ValueError(f"unknown backend {kind!r}; one of {', '.join(BACKENDS)}")
    module, _, cls = BACKENDS[kind].partition(":")
    backend: Backend = getattr(import_module(module), cls)(url)
    return backend


def kind_of(url: str) -> str:
    """ "postgres" for a postgres(ql):// URL or a libpq "key=value" string, else "duckdb" (a file path,
    which may contain "=" in a directory name)."""
    libpq = re.match(r"^\s*\w+\s*=", url)
    return "postgres" if url.startswith(("postgres://", "postgresql://")) or libpq else "duckdb"


def require(backend: Backend, capability: Capability, action: str) -> None:
    if capability not in backend.capabilities:
        raise MissingCapability(f"{action} needs a backend with {capability!r}; {type(backend).__name__} has none")


def parse_ref(ref: str) -> tuple[str, date | None]:
    """ "pop@2026-10-01" -> ("pop", date(2026, 10, 1)); "pop" -> ("pop", None)."""
    name, _, day = ref.partition("@")
    return name, date.fromisoformat(day) if day else None
