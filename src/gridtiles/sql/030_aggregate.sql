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

-- Request parameters on the layer's time column, as SQL conditions: `filter` selects the source
-- rows; for a diff, `side_b` and `side_a` tell its two sides apart. Raises SQLSTATE 22023 on a
-- malformed parameter, before any value is cast. Unknown keys are ignored, since tile servers
-- forward every URL query parameter.
--   at=V           time = V (a snapshot)
--   start=V, end=V time >= start, time < end; either may be absent
--   diff=V         with at: the tile of `at` minus the tile of `diff`
create or replace function gridtiles._time_filter(l gridtiles.layers, params jsonb,
    out filter text, out side_b text, out side_a text)
language plpgsql stable parallel safe as $$
declare
    k text;
    col text := quote_ident(l.time_column);
    typ text := l.time_type::text;
begin
    if jsonb_typeof(params) is distinct from 'object' then
        raise exception 'tile parameters must be a JSON object, got %', params using errcode = '22023';
    end if;
    foreach k in array array['at', 'start', 'end', 'diff'] loop
        continue when not params ? k;
        if l.time_column is null then
            raise exception 'parameter %: layer % has no time column', k, l.name using errcode = '22023';
        elsif jsonb_typeof(params->k) <> 'string' then
            raise exception 'parameter %: expected a string, got %', k, params->k using errcode = '22023';
        elsif not pg_input_is_valid(params->>k, typ) then
            raise exception 'parameter %: % is not a valid %', k, quote_literal(params->>k), typ using errcode = '22023';
        end if;
    end loop;
    if params ? 'at' and (params ? 'start' or params ? 'end') then
        raise exception 'parameter at excludes start and end' using errcode = '22023';
    elsif params ? 'diff' and not params ? 'at' then
        raise exception 'parameter diff needs at' using errcode = '22023';
    end if;
    side_b := case when params ? 'at' then format('%s = %L::%s', col, params->>'at', typ) end;
    side_a := case when params ? 'diff' then format('%s = %L::%s', col, params->>'diff', typ) end;
    filter := coalesce('(' || side_b || ' or ' || side_a || ')', side_b, nullif(concat_ws(' and ',
        case when params ? 'start' then format('%s >= %L::%s', col, params->>'start', typ) end,
        case when params ? 'end' then format('%s < %L::%s', col, params->>'end', typ) end), ''), 'true');
end $$;

-- The source rows of a region: `_g`, the geometry ready to aggregate (a point in EPSG:3857 or
-- 4326, see _square_sql; a polygon from _mercator_polygon), each source column as double
-- precision under its alias (_sources), so source column names never clash with the query's own,
-- and for a diff the booleans _in_b and _in_a. `region` and `time` are SQL conditions.
create or replace function gridtiles._source_sql(l gridtiles.layers, region text, "time" text,
    side_b text, side_a text) returns text
language sql stable parallel safe as $$
    select format('select %s as _g%s%s from %s where %s and %s and not ST_IsEmpty(%I)',
        case when l.kind = 'polygon' then format('gridtiles._mercator_polygon(%I)', l.geometry)
             when l.srid in (3857, 4326) then quote_ident(l.geometry)
             else format('ST_Transform(%I, 4326)', l.geometry) end,
        coalesce((select string_agg(format(', %I::double precision as %s', s.col, s.alias), '' order by s.alias)
                  from gridtiles._sources(l.spec) s), ''),
        case when side_a is not null then format(', %s as _in_b, %s as _in_a', side_b, side_a) else '' end,
        l.source, region, "time", l.geometry)
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

-- Each non-count value as an aggregate over the rows of a square (those matching `cond`, if
-- given): sums add the value times `share` (1 for a point), means weigh it by its weight times
-- `area` (1 for a point), min/max ignore both. Sums add their terms in ascending order, so the
-- result does not depend on the order rows are read in. Count values are the property `n`.
create or replace function gridtiles._value_exprs(spec jsonb, share text, area text, cond text default null)
returns table (name text, agg text, expr text, i bigint)
language sql immutable parallel safe as $$
    select v->>'name', v->>'agg', case v->>'agg'
        when 'sum' then format('sum(%1$s * %2$s order by %1$s * %2$s)%3$s', s.alias, share, f)
        when 'mean' then format('sum(%1$s order by %1$s)%3$s / nullif(sum(%2$s order by %2$s)%3$s, 0)',
            format('%s * %s * %s', s.alias, w, area), format('case when %s is not null then %s * %s end', s.alias, w, area), f)
        else format('%s(%s)%s', v->>'agg', s.alias, f)
    end, e.i
    from jsonb_array_elements(spec->'values') with ordinality e(v, i)
    join gridtiles._sources(spec) s on s.col = v->>'from',
    lateral (select coalesce((select alias from gridtiles._sources(spec) where col = v->>'weight'), '1'),
                    case when cond is not null then format(' filter (where %s)', cond) else '' end) x(w, f)
    where v->>'agg' <> 'count'
$$;

-- `n` and the comma-led value columns of a square. For a diff, side b minus side a: counts and
-- sums treat a side without data as 0; means, minima and maxima exist only where both sides have
-- data.
create or replace function gridtiles._values_sql(spec jsonb, share text, area text, diff boolean) returns text
language sql immutable parallel safe as $$
    select case when not diff then 'count(*) as n' || coalesce(
        (select string_agg(format(', %s as %I', expr, name), '' order by i) from gridtiles._value_exprs(spec, share, area)), '')
    else 'count(*) filter (where _in_b) - count(*) filter (where _in_a) as n' || coalesce(
        (select string_agg(format(case when b.agg = 'sum' then ', coalesce(%s, 0) - coalesce(%s, 0) as %I'
                                       else ', %s - %s as %I' end, b.expr, a.expr, b.name), '' order by b.i)
         from gridtiles._value_exprs(spec, share, area, '_in_b') b
         join gridtiles._value_exprs(spec, share, area, '_in_a') a using (i)), '')
    end
$$;

-- One row per square of the region with data: _x, _y, n and one column per non-count value.
-- Points: each point counts in the square holding it. Polygons: each polygon is split over the
-- squares it overlaps by covered area (in EPSG:3857): sums get value x covered fraction, means
-- are weighted by covered area, min/max apply to every touched square, and n counts the polygons
-- touching a square; a zero-area touch does not count. A polygon is clipped to the region once
-- (unless it lies inside), then to each square; its share divides by the area of the whole
-- polygon. `offset 0` keeps the planner from inlining a subquery's expressions into every
-- reference, which would repair, project and clip a polygon again for every use. Levels up to
-- the layer's cache_zoom read the cache instead, unless a parameter filters the source.
create or replace function gridtiles._aggregate_sql(l gridtiles.layers, c int, z int, x int, y int, params jsonb)
returns text
language plpgsql stable parallel safe as $$
declare
    t record := gridtiles._time_filter(l, params);
    reg record := gridtiles._region(l, c, z, x, y);
    src text := gridtiles._source_sql(l, reg.filter, t.filter, t.side_b, t.side_a);
    diff boolean := t.side_a is not null;
    sq record;
    r int := least(z, c);
    envelope text := format('ST_TileEnvelope(%s, %s, %s)', r, x >> (z - r), y >> (z - r));
begin
    if c <= l.cache_zoom and t.filter = 'true' then
        return format('select _x, _y, n%s from gridtiles.%I where _z = %s and _x between %s and %s and _y between %s and %s',
            (select coalesce(string_agg(format(', %I', name), '' order by i), '') from gridtiles._value_exprs(l.spec, '1', '1')),
            'cache_' || l.name, c, reg.x0, reg.x1, reg.y0, reg.y1);
    end if;
    if l.kind = 'polygon' then
        return format(
            'select _x, _y, %s from (
                select p.*, _x, _y, _a, _a / p._total as _share
                from (select *, case when _g @ %2$s then _g else ST_ClipByBox2D(_g, %2$s) end as _r
                      from (select *, ST_Area(_g) as _total from (%3$s offset 0) s offset 0) s
                      where _total > 0 offset 0) p,
                    gridtiles._bbox_tile_range(p._r, %4$s) b,
                    generate_series(greatest(b.x0, %5$s), least(b.x1, %6$s)) _x,
                    generate_series(greatest(b.y0, %7$s), least(b.y1, %8$s)) _y,
                    lateral (select ST_Area(ST_ClipByBox2D(p._r, ST_TileEnvelope(%4$s, _x, _y))) as _a offset 0) a
             ) h where _a > 0 group by _x, _y',
            gridtiles._values_sql(l.spec, '_share', '_a', diff), envelope, src, c, reg.x0, reg.x1, reg.y0, reg.y1);
    end if;
    sq := gridtiles._square_sql(l.srid, c);
    return format(
        'select _x, _y, %s from (select %s as _x, %s as _y, * from (%s%s) s) p
         where _x between %s and %s and _y between %s and %s group by _x, _y',
        gridtiles._values_sql(l.spec, '1', '1', diff), sq.sx, sq.sy, src,
        case when l.srid in (3857, 4326) then '' else ' offset 0' end,  -- one transform per point
        reg.x0, reg.x1, reg.y0, reg.y1);
end $$;

-- What the viewer styles with: per value, at the base zoom over the whole source (filtered by
-- `params`, as tiles are), the `total` (count and sum only), `min`, `max` and 7 quantile `breaks`
-- (1/8 ... 7/8) of the tile property; and `bounds`, the WGS84 extent [west, south, east, north]
-- of the base squares with data. It aggregates the whole source.
create or replace function gridtiles.stats(name text, params jsonb default '{}') returns jsonb
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
        l.zb, gridtiles._aggregate_sql(l, l.zb, 0, 0, 0, params)
    ) into result;
    return result;
end $$;

-- The distinct values of a layer's time column, ascending, as text; NULL without a time column or
-- with more than `max_count` values.
create or replace function gridtiles.times(name text, max_count int default 1000) returns text[]
language plpgsql stable parallel safe as $$
declare
    l gridtiles.layers;
    result text[];
begin
    select * into strict l from gridtiles.layers where layers.name = times.name;
    if l.time_column is null then
        return null;
    end if;
    execute format(
        'select array_agg(t::text order by t) from (select distinct %1$I as t from %2$s where %1$I is not null limit %3$s) d',
        l.time_column, l.source, max_count + 1) into result;
    return case when cardinality(result) > max_count then null else coalesce(result, '{}') end;
end $$;

-- Every tile with data in the published zoom range [max(0, zmin - detail), max(0, zb - detail)]:
-- the tiles with data at the highest published zoom (holding a point, or overlapping a polygon),
-- and their ancestors. A square's index at a coarser zoom is its index shifted right.
create or replace function gridtiles.tiles_with_data(name text, params jsonb default '{}')
returns table (z int, x int, y int)
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
        tmax, gridtiles._aggregate_sql(l, tmax, 0, 0, 0, params), tmin);
end $$;

-- Cache the squares of levels min_zoom .. max_zoom, without parameters, in gridtiles.cache_<name>;
-- tiles at those levels then read the cache, which lags the source until the next refresh. A NULL
-- max_zoom drops the cache. Returns the number of cached squares.
create or replace function gridtiles.refresh_cache(name text, max_zoom int) returns bigint
language plpgsql as $$
declare
    l gridtiles.layers;
    tbl text := 'cache_' || name;
    squares bigint := 0;
begin
    select * into strict l from gridtiles.layers where layers.name = refresh_cache.name for update;
    execute format('drop table if exists gridtiles.%I', tbl);
    l.cache_zoom := null;  -- build from the source, not from the old cache
    if max_zoom is not null then
        execute format('create table gridtiles.%I as %s', tbl, (
            select string_agg(format('select %s::smallint as _z, * from (%s) a', c, gridtiles._aggregate_sql(l, c, 0, 0, 0, '{}')),
                ' union all ')
            from generate_series(l.zmin, least(max_zoom, l.zb)) c));
        get diagnostics squares = row_count;
        execute format('create index on gridtiles.%I (_z, _x, _y)', tbl);
    end if;
    update gridtiles.layers set (cache_zoom, cached_at) = (select least(max_zoom, zb), clock_timestamp() where max_zoom is not null)
    where layers.name = l.name;
    return squares;
end $$;
