-- Ingest: aggregate a staging table into a layer's base cells (z = zb). The staging table has a
-- `geom` column plus one double precision column per value source and weight. Repeated loads add
-- up: existing cells are merged per aggregator.

-- Insert aggregated base cells from `source`, a query with a `_cell` quadkey column, merging
-- with existing cells.
create or replace function gridtiles._insert_cells(l gridtiles.layers, source text, aggregates text) returns bigint
language plpgsql as $$
declare
    written bigint;
begin
    execute format(
        'insert into gridtiles.%I as t (z, qk, n%s) select %s, _cell, count(*)%s from (%s) s group by _cell
         on conflict (z, qk) do update set n = t.n + excluded.n%s',
        'cells_' || l.name, gridtiles._column_list(l.spec), l.zb, aggregates, source,
        gridtiles._merge_set(l.spec, 't', 'excluded')
    );
    get diagnostics written = row_count;
    return written;
end $$;

create or replace function gridtiles.ingest_points(name text, staging regclass) returns bigint
language plpgsql as $$
declare
    l gridtiles.layers;
begin
    select * into strict l from gridtiles.layers where layers.name = ingest_points.name;
    return gridtiles._insert_cells(l,
        format('select gridtiles.qk_from_point(geom, %s) as _cell, * from %s where geom is not null', l.zb, staging),
        gridtiles._aggregates(l.spec));
end $$;

-- Tile index range at zoom z covered by the bounding box of an EPSG:3857 geometry.
create or replace function gridtiles._bbox_tile_range(g geometry, z int, out x0 int, out x1 int, out y0 int, out y1 int)
language sql immutable strict parallel safe as $$
    select greatest(floor((ST_XMin(g) + 20037508.342789244) / size)::int, 0),
           least(floor((ST_XMax(g) + 20037508.342789244) / size)::int, top),
           greatest(floor((20037508.342789244 - ST_YMax(g)) / size)::int, 0),
           least(floor((20037508.342789244 - ST_YMin(g)) / size)::int, top)
    from (select 40075016.68557849::double precision / (1::bigint << z) as size, (1 << z) - 1 as top) k
$$;

-- Polygons are split over the base cells they overlap: sums get value x covered fraction, means
-- weigh by covered area, min/max apply to every touched cell, and n counts touching polygons.
-- A polygon whose bounding box spans more than max_cells base cells raises SQLSTATE 54000
-- (NULL: no limit).
create or replace function gridtiles.ingest_polygons(name text, staging regclass, max_cells bigint default 1000000)
returns bigint
language plpgsql as $$
declare
    l gridtiles.layers;
    widest bigint;
    polygons text;
begin
    select * into strict l from gridtiles.layers where layers.name = ingest_polygons.name;
    polygons := format('select *, ST_Transform(geom, 3857) as _g from %s where not coalesce(ST_IsEmpty(geom), true)', staging);
    if max_cells is not null then
        execute format(
            'select max((r.x1 - r.x0 + 1)::bigint * (r.y1 - r.y0 + 1)) from (%s) p, gridtiles._bbox_tile_range(p._g, %s) r',
            polygons, l.zb
        ) into widest;
        if widest > max_cells then
            raise exception 'a polygon spans % cells at z%, above max_cells_per_feature %', widest, l.zb, max_cells
                using errcode = '54000';
        end if;
    end if;
    return gridtiles._insert_cells(l,
        format(
            'select p.*, gridtiles._qk(t.x, u.y) as _cell, a._a / ST_Area(p._g) as _share, a._a as _area
             from (%s) p, gridtiles._bbox_tile_range(p._g, %s) r, generate_series(r.x0, r.x1) t(x), generate_series(r.y0, r.y1) u(y),
                 lateral (select ST_Area(ST_Intersection(p._g, ST_TileEnvelope(%s, t.x, u.y))) as _a) a
             where a._a > 0',
            polygons, l.zb, l.zb),
        gridtiles._aggregates(l.spec, '_share', '_area'));
end $$;
