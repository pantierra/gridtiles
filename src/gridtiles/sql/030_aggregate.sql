-- Live aggregation: SQL builders that aggregate a layer's source into the squares of one region
-- at zoom c. Tiles (040_mvt.sql), stats and the export's tile list all run the query built here.
-- Square (_x, _y) at zoom c is ST_TileEnvelope(c, _x, _y); its quadkey is _qk(_x, _y).

-- A region: the tile index ranges [x0, x1] x [y0, y1] at zoom c of the squares a request covers,
-- and the source filter selecting their rows. With c >= z the region is tile z/x/y; with c < z
-- (overzoom) the single ancestor square at c. The filter is `geom && box` in the source SRID
-- (EPSG:4326 or 3857), or `ST_Transform(geom, 4326) && box` for any other SRID, so a GiST index
-- (on the geometry, or on that expression) drives it. The box is widened slightly to absorb
-- rounding, and reaches out without limit at the world edge, where points beyond the Web Mercator
-- square are clamped in; the exact assignment drops the extra rows. The whole world needs no filter.
create or replace function gridtiles._region(l gridtiles.layers, c int, z int, x int, y int,
    out x0 int, out x1 int, out y0 int, out y1 int, out filter text)
language plpgsql immutable parallel safe as $$
declare
    r int := least(z, c);
    rx bigint := x >> (z - r);
    ry bigint := y >> (z - r);
    n bigint := 1::bigint << r;
    far constant double precision := 1e10;
    west double precision;
    east double precision;
    north double precision;
    south double precision;
begin
    x0 := rx << (c - r);
    x1 := ((rx + 1) << (c - r)) - 1;
    y0 := ry << (c - r);
    y1 := ((ry + 1) << (c - r)) - 1;
    if r = 0 then
        filter := 'true';
        return;
    end if;
    if l.srid = 3857 then
        west := -20037508.342789244 + rx * 40075016.68557849 / n - 1;
        east := -20037508.342789244 + (rx + 1) * 40075016.68557849 / n + 1;
        north := 20037508.342789244 - ry * 40075016.68557849 / n + 1;
        south := 20037508.342789244 - (ry + 1) * 40075016.68557849 / n - 1;
    else
        west := rx * 360.0 / n - 180 - 1e-7;
        east := (rx + 1) * 360.0 / n - 180 + 1e-7;
        north := degrees(atan(sinh(pi() * (1 - 2.0 * ry / n)))) + 1e-7;
        south := degrees(atan(sinh(pi() * (1 - 2.0 * (ry + 1) / n)))) - 1e-7;
    end if;
    if rx = 0 then west := -far; end if;
    if rx = n - 1 then east := far; end if;
    if ry = 0 then north := far; end if;
    if ry = n - 1 then south := -far; end if;
    filter := format('%s && ST_MakeEnvelope(%s, %s, %s, %s, %s)',
        case when l.srid in (3857, 4326) then quote_ident(l.geometry) else format('ST_Transform(%I, 4326)', l.geometry) end,
        west, south, east, north, case when l.srid = 3857 then 3857 else 4326 end);
end $$;

-- The square index (_x, _y) at zoom c of each point, as SQL over `_g`, the point in EPSG:3857 or
-- 4326 (_source_sql transforms any other SRID to 4326 first). These are the float operations of
-- qk_from_point, inlined: same operations, same squares, without a PL/pgSQL call per row.
create or replace function gridtiles._square_sql(srid int, c int, out sx text, out sy text)
language plpgsql immutable strict parallel safe as $$
declare
    mx text := 'ST_X(_g)';
    my text := 'ST_Y(_g)';
    size text := format('(40075016.68557849::double precision / (1::bigint << %s))', c);
    top text := format('((1::bigint << %s) - 1)', c);
begin
    if srid <> 3857 then
        mx := 'ST_X(_g) * 20037508.342789244 / 180';
        my := 'ln(tan(pi() / 4 + radians(greatest(-85.0511287798, least(85.0511287798, ST_Y(_g)))) / 2))'
              ' * 20037508.342789244 / pi()';
    end if;
    sx := format('least(greatest(floor(((%s) + 20037508.342789244) / %s), 0), %s)::int', mx, size, top);
    sy := format('least(greatest(floor((20037508.342789244 - (%s)) / %s), 0), %s)::int', my, size, top);
end $$;

-- The source rows of a region: `_g`, the geometry ready to aggregate (a point in EPSG:3857 or
-- 4326, see _square_sql; a polygon from _mercator_polygon), and each source column as double
-- precision under its alias (_sources), so source column names never clash with the query's own.
create or replace function gridtiles._source_sql(l gridtiles.layers, filter text) returns text
language sql stable parallel safe as $$
    select format('select %s as _g%s from %s where %s and not ST_IsEmpty(%I)',
        case when l.kind = 'polygon' then format('gridtiles._mercator_polygon(%I)', l.geometry)
             when l.srid in (3857, 4326) then quote_ident(l.geometry)
             else format('ST_Transform(%I, 4326)', l.geometry) end,
        coalesce((select string_agg(format(', %I::double precision as %s', s.col, s.alias), '' order by s.alias)
                  from gridtiles._sources(l.spec) s), ''),
        l.source, filter, l.geometry)
$$;

-- A polygon in EPSG:3857, ready to split: ST_MakeValid repairs self-intersections (which
-- ST_Intersection would reject), keeping the polygonal part; WGS84 input is clipped to the Mercator
-- latitudes before projecting, since the poles have no Mercator coordinates; the result is clipped
-- to the Web Mercator square, so a polygon's whole value lands on squares that exist.
create or replace function gridtiles._mercator_polygon(geom geometry) returns geometry
language sql immutable strict parallel safe as $$
    select ST_Intersection(
        ST_Transform(case when ST_SRID(valid) = 4326
                          then ST_Intersection(valid, ST_MakeEnvelope(-180, -85.06, 180, 85.06, 4326))
                          else valid end, 3857),
        ST_MakeEnvelope(-20037508.342789244, -20037508.342789244, 20037508.342789244, 20037508.342789244, 3857))
    from (select ST_CollectionExtract(ST_MakeValid(geom), 3) as valid) v
$$;

-- Tile index range at zoom z covered by the bounding box of an EPSG:3857 geometry.
create or replace function gridtiles._bbox_tile_range(g geometry, z int, out x0 int, out x1 int, out y0 int, out y1 int)
language sql immutable strict parallel safe as $$
    select greatest(floor((ST_XMin(g) + 20037508.342789244) / size)::int, 0),
           least(floor((ST_XMax(g) + 20037508.342789244) / size)::int, top),
           greatest(floor((20037508.342789244 - ST_YMax(g)) / size)::int, 0),
           least(floor((20037508.342789244 - ST_YMin(g)) / size)::int, top)
    from (select 40075016.68557849::double precision / (1::bigint << z) as size, (1 << z) - 1 as top) k
$$;

-- A sum over the rows of a square. Float sums depend on their order; `gridtiles.sum_mode` picks
-- how they are made reproducible: 'ordered' (default) sums the terms in ascending order,
-- 'numeric' sums them exactly as numeric (order-independent, so parallel aggregation applies;
-- each term is rounded to 15 significant digits), 'plain' leaves the order to the executor.
create or replace function gridtiles._sum_sql(term text) returns text
language sql stable parallel safe as $$
    select case coalesce(nullif(current_setting('gridtiles.sum_mode', true), ''), 'ordered')
        when 'numeric' then format('sum((%s)::numeric)::double precision', term)
        when 'plain' then format('sum(%s)', term)
        else format('sum(%1$s order by %1$s)', term)
    end
$$;

-- Comma-led tile properties of a layer's values over the aggregated rows: sums add each value
-- times `share` (1 for a point), means weigh it by its weight times `area` (1 for a point), as
-- the materialized ingest did; min/max ignore both. Count values are the property `n`.
create or replace function gridtiles._values_sql(spec jsonb, share text default '1', area text default '1') returns text
language sql stable parallel safe as $$
    select coalesce(string_agg(', ' || case v->>'agg'
        when 'sum' then gridtiles._sum_sql(format('%s * %s', s.alias, share))
        when 'mean' then format('%s / nullif(%s, 0)',
            gridtiles._sum_sql(format('%s * %s * %s', s.alias, w, area)),
            gridtiles._sum_sql(format('case when %s is not null then %s * %s end', s.alias, w, area)))
        else format('%s(%s)', v->>'agg', s.alias)
    end || ' as ' || quote_ident(v->>'name'), '' order by e.i), '')
    from jsonb_array_elements(spec->'values') with ordinality e(v, i)
    join gridtiles._sources(spec) s on s.col = v->>'from'
    left join lateral (select coalesce((select alias from gridtiles._sources(spec) where col = v->>'weight'), '1')) x(w) on true
    where v->>'agg' <> 'count'
$$;

-- One row per square of the region with data: _x, _y, n and one column per non-count value.
-- Points: each point counts in the square holding it. Polygons: each polygon is split over the
-- squares it overlaps by covered area (in EPSG:3857), as the materialized ingest did: sums get
-- value x covered fraction, means are weighted by covered area, min/max apply to every touched
-- square, and n counts the polygons touching a square; a zero-area touch does not count. A
-- polygon is clipped to the region once (unless it lies inside), then intersected per square;
-- its share divides by the area of the whole polygon. `offset 0` keeps the planner from inlining
-- a subquery's expressions into every reference, which would repair, project and clip a polygon
-- again for every square.
create or replace function gridtiles._aggregate_sql(l gridtiles.layers, c int, z int, x int, y int) returns text
language plpgsql stable parallel safe as $$
declare
    reg record := gridtiles._region(l, c, z, x, y);
    sq record;
    r int := least(z, c);
    envelope text := format('ST_TileEnvelope(%s, %s, %s)', r, x >> (z - r), y >> (z - r));
begin
    if l.kind = 'polygon' then
        return format(
            'select _x, _y, count(*) as n%s from (
                select p.*, _x, _y, _a, _a / p._total as _share
                from (select *, case when _g @ %2$s then _g else ST_Intersection(_g, %2$s) end as _r
                      from (select *, ST_Area(_g) as _total from (%3$s offset 0) s offset 0) s
                      where _total > 0 offset 0) p,
                    gridtiles._bbox_tile_range(p._r, %4$s) b,
                    generate_series(greatest(b.x0, %5$s), least(b.x1, %6$s)) _x,
                    generate_series(greatest(b.y0, %7$s), least(b.y1, %8$s)) _y,
                    lateral (select ST_Area(ST_Intersection(p._r, ST_TileEnvelope(%4$s, _x, _y))) as _a) a
             ) h where _a > 0 group by _x, _y',
            gridtiles._values_sql(l.spec, '_share', '_a'), envelope, gridtiles._source_sql(l, reg.filter),
            c, reg.x0, reg.x1, reg.y0, reg.y1);
    end if;
    sq := gridtiles._square_sql(l.srid, c);
    return format(
        'select _x, _y, count(*) as n%s from (select %s as _x, %s as _y, * from (%s%s) s) p
         where _x between %s and %s and _y between %s and %s group by _x, _y',
        gridtiles._values_sql(l.spec), sq.sx, sq.sy, gridtiles._source_sql(l, reg.filter),
        case when l.srid in (3857, 4326) then '' else ' offset 0' end,  -- one transform per point
        reg.x0, reg.x1, reg.y0, reg.y1);
end $$;

-- What the viewer styles with: per value, at the base zoom over the whole source, the `total`
-- (count and sum only), `min`, `max` and 7 quantile `breaks` (1/8 ... 7/8) of the tile property;
-- and `bounds`, the WGS84 extent [west, south, east, north] of the base squares with data.
-- Computed from the source on every call: it aggregates the whole source.
create or replace function gridtiles.stats(name text) returns jsonb
language plpgsql stable parallel safe as $$
declare
    l gridtiles.layers;
    result jsonb;
begin
    select * into strict l from gridtiles.layers where layers.name = stats.name;
    execute format(
        'select jsonb_build_object(''stats'', %s, ''bounds'', case when count(*) > 0 then array[
             ST_XMin(ST_Transform(ST_TileEnvelope(%2$s, min(_x), min(_y)), 4326)),
             ST_YMin(ST_Transform(ST_TileEnvelope(%2$s, max(_x), max(_y)), 4326)),
             ST_XMax(ST_Transform(ST_TileEnvelope(%2$s, max(_x), max(_y)), 4326)),
             ST_YMax(ST_Transform(ST_TileEnvelope(%2$s, min(_x), min(_y)), 4326))] end)
         from (%3$s) a',
        coalesce((select string_agg(format(
            'jsonb_build_object(%L, jsonb_build_object(''total'', %s, ''min'', min(%s), ''max'', max(%3$s), ''breaks'',
                percentile_cont(array[0.125, 0.25, 0.375, 0.5, 0.625, 0.75, 0.875]) within group (order by %3$s)))',
            v->>'name', case when v->>'agg' in ('count', 'sum') then format('sum(%s)', p) else 'null' end, p), ' || ' order by i)
            from jsonb_array_elements(l.spec->'values') with ordinality e(v, i),
                lateral (select case when v->>'agg' = 'count' then 'n' else quote_ident(v->>'name') end) q(p)),
            '''{}''::jsonb'),
        l.zb, gridtiles._aggregate_sql(l, l.zb, 0, 0, 0)
    ) into result;
    return result;
end $$;

-- Every tile with data in the published zoom range [max(0, zmin - detail), max(0, zb - detail)]:
-- the tiles with data at the highest published zoom (holding a point, or overlapping a polygon),
-- and their ancestors. A square's index at a coarser zoom is its index shifted right.
create or replace function gridtiles.tiles_with_data(name text) returns table (z int, x int, y int)
language plpgsql stable parallel safe as $$
declare
    l gridtiles.layers;
    tmin int;
    tmax int;
begin
    select * into strict l from gridtiles.layers where layers.name = tiles_with_data.name;
    tmin := greatest(0, l.zmin - l.detail);
    tmax := greatest(0, l.zb - l.detail);
    return query execute format(
        'select t, _x >> (%1$s - t), _y >> (%1$s - t)
         from (select _x, _y from (%2$s) a) a, generate_series(%3$s, %1$s) t
         group by 1, 2, 3 order by 1, 2, 3',
        tmax, gridtiles._aggregate_sql(l, tmax, 0, 0, 0), tmin);
end $$;
