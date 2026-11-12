"""Layer spec: what to aggregate into a layer's cells and at which zooms.

`gridtiles.create_layer` in SQL repeats the critical checks, so a spec is safe from either side.
"""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from gridtiles.quadkey import MAX_ZOOM

NAME_PATTERN = r"^[a-z][a-z0-9_]{0,47}$"

Agg = Literal["count", "sum", "min", "max", "mean"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_by_name=True, serialize_by_alias=True)


class ValueSpec(_Model):
    name: str = Field(pattern=NAME_PATTERN)
    from_: str = Field(alias="from")
    agg: Agg
    unit: str | None = None
    weight: str | None = None

    @model_validator(mode="after")
    def _weight_only_with_mean(self) -> Self:
        if self.weight is not None and self.agg != "mean":
            raise ValueError(f"value {self.name!r}: weight is only allowed with agg 'mean'")
        return self


class LayerSpec(_Model):
    name: str = Field(pattern=NAME_PATTERN)
    base_zoom: int = Field(ge=0, le=MAX_ZOOM)
    min_zoom: int = Field(ge=0, le=MAX_ZOOM)
    detail: int = Field(default=4, ge=0, le=8)
    values: tuple[ValueSpec, ...]

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.min_zoom > self.base_zoom:
            raise ValueError(f"min_zoom {self.min_zoom} above base_zoom {self.base_zoom}")
        names = [v.name for v in self.values]
        if len(names) != len(set(names)):
            raise ValueError(f"duplicate value names in {names}")
        return self

    def cell_zoom(self, t: int) -> int:
        """Zoom of the cells a tile at zoom t carries: clamp(t + detail, min_zoom, base_zoom)."""
        return min(max(t + self.detail, self.min_zoom), self.base_zoom)
