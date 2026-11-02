# SQL reference

Functions live in the `gridtiles` schema, installed from `sql/*.sql`.

## Quadkeys

A cell is identified by `(z, qk)`: `qk` is a `bigint` Morton code with x in the even bits and y in the odd bits, using XYZ tile addressing (y = 0 at the north edge, 0 ≤ z ≤ 30). Out-of-range input raises SQLSTATE `22003`; a quadkey string with a digit outside 0–3 raises `22P02`.

| Function | Returns |
| --- | --- |
| `qk_from_xyz(z, x, y)` | `bigint` |
| `qk_to_xy(z, qk)` | `(x int, y int)` |
| `qk_to_text(z, qk)` | Bing quadkey string, exactly z digits |
| `qk_from_text(t)` | `bigint`, at zoom `length(t)` |
| `qk_parent(z, qk, z_to)` | ancestor at `z_to ≤ z` |
| `qk_child_range(z, qk, z_to)` | half-open `int8range` of descendants at `z_to ≥ z` |
| `qk_envelope(z, qk)` | cell polygon in EPSG:3857 |
| `qk_from_point(geom, z)` | cell containing a point of any SRID; `NULL` if empty |

```sql
select gridtiles.qk_from_xyz(3, 3, 5);                       -- 39
select gridtiles.qk_to_text(3, 39);                          -- '213'
select * from gridtiles.qk_to_xy(3, gridtiles.qk_from_text('213'));  -- (3, 5)
select gridtiles.qk_parent(3, 39, 1);                        -- 2
select gridtiles.qk_child_range(1, 2, 3);                    -- [32,48)
select gridtiles.qk_from_point('SRID=4326;POINT(13.405 52.52)', 16);
select ST_AsText(gridtiles.qk_envelope(1, 1));
```
