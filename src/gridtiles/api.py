"""The query API: layers, cells and region statistics over any backend (`gridtiles api`)."""

from datetime import date
from typing import Annotated, Any, Literal

from fastapi import Body, FastAPI, HTTPException, Path, Query
from pydantic import BaseModel

from gridtiles import __version__
from gridtiles.backends.base import Backend, MissingCapability

Quadkey = Annotated[str, Path(pattern=r"^[0-3]{0,30}$", description="Quadkey text; its length is the zoom")]
Zoom = Annotated[int | None, Query(description="Zoom of the cells to sum [default: the base zoom]")]
Snapshot = Annotated[date | None, Query(description="Snapshot [default: the latest] on snapshot layers")]


class Cell(BaseModel):
    qk: str
    z: int
    n: int
    values: dict[str, float | None]


class Stats(BaseModel):
    layer: str
    zoom: int
    cells: int
    n: int
    values: dict[str, float | None]
    method: Literal["centroid"] | None = None


class Polygon(BaseModel):
    """A GeoJSON Polygon or MultiPolygon in WGS84."""

    type: Literal["Polygon", "MultiPolygon"]
    coordinates: list[Any]


def create_app(backend: Backend) -> FastAPI:
    app = FastAPI(title="gridtiles", version=__version__, summary="Stats of quadkey cells by prefix, bbox or polygon.")

    def stats(name: str, **region: Any) -> dict[str, Any]:
        try:
            return backend.query(name, **region)
        except LookupError as e:
            raise HTTPException(404, str(e)) from e
        except (ValueError, MissingCapability) as e:
            raise HTTPException(422, str(e)) from e

    @app.get("/layers")
    def list_layers() -> list[dict[str, Any]]:
        """Every layer's manifest."""
        return [layer.manifest() for layer in backend.list_layers()]

    @app.get("/layers/{name}")
    def get_layer(name: str) -> dict[str, Any]:
        """A layer's manifest: spec, stats, bounds and snapshots."""
        try:
            (layer,) = backend.list_layers([name])
        except LookupError as e:
            raise HTTPException(404, str(e)) from e
        return layer.manifest()

    @app.get("/layers/{name}/cells/{qk}")
    def get_cell(name: str, qk: Quadkey, snapshot: Snapshot = None) -> Cell:
        """One cell, its values as the tile properties."""
        result = stats(name, prefix=qk, zoom=len(qk), snapshot=snapshot)
        if not result["cells"]:
            raise HTTPException(404, f"no cell {qk!r} in layer {name!r}")
        return Cell(qk=qk, z=len(qk), n=result["n"], values=result["values"])

    @app.get("/layers/{name}/stats")
    def get_stats(
        name: str,
        prefix: Annotated[str | None, Query(pattern=r"^[0-3]{0,30}$", description="Quadkey prefix")] = None,
        bbox: Annotated[str | None, Query(description="west,south,east,north in WGS84")] = None,
        zoom: Zoom = None,
        snapshot: Snapshot = None,
    ) -> Stats:
        """Stats of the cells in a quadkey prefix, or whose centroid lies in a bbox."""
        box = None
        if bbox is not None:
            try:
                west, south, east, north = (float(v) for v in bbox.split(","))
            except ValueError as e:
                raise HTTPException(422, "bbox must be west,south,east,north") from e
            box = (west, south, east, north)
        return Stats(**stats(name, prefix=prefix, bbox=box, zoom=zoom, snapshot=snapshot))

    @app.post("/layers/{name}/stats")
    def post_stats(name: str, polygon: Annotated[Polygon, Body()], zoom: Zoom = None, snapshot: Snapshot = None) -> Stats:
        """Stats of the cells whose centroid lies in a GeoJSON polygon."""
        return Stats(**stats(name, polygon=polygon.model_dump(), zoom=zoom, snapshot=snapshot))

    return app
