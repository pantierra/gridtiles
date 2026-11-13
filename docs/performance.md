# Performance

A tile aggregates the source rows inside it on request. At high zooms that is a few rows found through the spatial index; at low zooms a tile covers much of the table. `make bench` (`tests/perf/bench.py`) measures it: 10 million clustered points and 100,000 polygons (squares and circles up to 0.05°, 1% up to 2° with 1,024 vertices) around 300 random centres, 20 tiles with data per zoom (fewer where fewer exist), each requested twice.

Numbers below: PostgreSQL 16 and PostGIS 3.4 in Docker with stock settings (128 MB shared buffers, 2 parallel workers per query), 16 cores, 30 GB RAM. Times in ms of the second request; layers with `base_zoom` 16, `min_zoom` 2, `detail` 4, a sum and a weighted mean.

| Tile zoom | Points p50 | Points p95 | Polygons p50 | Polygons p95 |
| --- | --- | --- | --- | --- |
| 0 | 8,274 | 8,274 | 2,500 | 2,500 |
| 1 | 2,629 | 6,896 | 2,073 | 2,666 |
| 2 | 2,061 | 3,428 | 853 | 876 |
| 3 | 616 | 1,072 | 886 | 978 |
| 4 | 248 | 508 | 930 | 942 |
| 5 | 58 | 235 | 402 | 946 |
| 6 | 35 | 110 | 318 | 1,061 |
| 7 | 9 | 57 | 582 | 1,067 |
| 8 | 12 | 44 | 378 | 1,165 |
| 9 | 8 | 12 | 429 | 1,195 |
| 10 | 6 | 14 | 401 | 830 |
| 11 | 4 | 11 | 414 | 611 |
| 12 | 2 | 5 | 404 | 535 |

- **Points** are fast from zoom 5 on. Below, a tile aggregates up to the whole table: 10 million points take 8 s, in parallel workers (15 s without).
- **Polygons** cost what their geometry costs: every polygon touching a tile is repaired (`ST_MakeValid`), projected and clipped to each square on every request, so a tile over large, detailed polygons takes around 0.4 s at any zoom.
- **The cache** (`gridtiles cache LAYER --max-zoom 8`) stores the squares of the coarse levels; tiles there then take 1–5 ms for both layers. Building it took 55 s for the points and 21 s for the polygons. It lags the source until refreshed, and requests with time parameters bypass it.

Two measurements decided the design. Sums add their terms in ascending order, so tiles are reproducible bit for bit; summing them exactly as `numeric` instead, which would allow parallel aggregation, was slower (14 s at zoom 0). JIT compilation took around 150 ms per polygon tile and did not speed up points, so the tile function runs without it.
