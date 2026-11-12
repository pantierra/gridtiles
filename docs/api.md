# Query API

`gridtiles api` serves a read-only HTTP API over the database (OpenAPI docs at `/docs`). It listens on `127.0.0.1:8000` unless `--host` / `--port` say otherwise, and reads through a pool of up to 4 connections. In Python, `gridtiles.api.create_app(url)` returns the FastAPI app.

| Endpoint | Returns |
| --- | --- |
| `GET /layers` | Every layer's manifest (spec, stats, bounds, snapshots) |
| `GET /layers/{name}` | One layer's manifest |
| `GET /layers/{name}/cells/{qk}` | One cell: `qk`, `z` (the length of `qk`), `n` and its values as tile properties |
| `GET /layers/{name}/stats?prefix=` | Stats of the cells at `zoom` under a quadkey prefix |
| `GET /layers/{name}/stats?bbox=west,south,east,north` | Stats of the cells at `zoom` whose centroid lies in a WGS84 bbox |
| `POST /layers/{name}/stats` | The same for a GeoJSON Polygon or MultiPolygon body (edges straight in lon/lat) |

Stats take `zoom` (default: the base zoom) and, on snapshot layers, `snapshot` (default: the latest). They return `cells`, `n` and per value the merged value: sums and counts add up, means are re-weighted, min and max kept. Region stats state `"method": "centroid"`; clipping cells at the region's edge is not done. An unknown layer or cell is 404; a malformed quadkey, bbox, zoom or region is 422.

```bash
gridtiles api --db postgresql://postgres:postgres@localhost:5439/postgres --port 8000
curl localhost:8000/layers/pop/cells/1202102332221200
curl "localhost:8000/layers/pop/stats?prefix=120210233&zoom=14"
curl "localhost:8000/layers/pop/stats?bbox=13.3,52.4,13.5,52.55"
curl -X POST localhost:8000/layers/pop/stats -H 'content-type: application/json' \
  -d '{"type": "Polygon", "coordinates": [[[13.2, 52.4], [13.6, 52.38], [13.5, 52.62], [13.2, 52.4]]]}'
```
