"""Layer spec: which source to aggregate into a layer's squares, how, and at which zooms.

The shape only; `gridtiles.create_layer` validates the values (names, zoom ranges, aggregators).
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_by_name=True, serialize_by_alias=True)


class ValueSpec(_Model):
    name: str
    from_: str = Field(alias="from")
    agg: Literal["count", "sum", "min", "max", "mean"]
    unit: str | None = None
    weight: str | None = None  # agg mean only


class SourceSpec(_Model):
    """The relation a layer reads: a typed point or polygon `geometry` column, and optionally a
    date or timestamp column `time` that tile parameters filter on.
    """

    table: str
    geometry: str = "geom"
    time: str | None = None


class LayerSpec(_Model):
    name: str
    source: SourceSpec
    base_zoom: int
    min_zoom: int
    detail: int = 4
    values: tuple[ValueSpec, ...]
