-- Public functions: vector tiles, stats, time values, the tiles with data, and the cache.

-- MVT encoder: a tile at zoom z carries the squares at c = clamp(z + detail, zmin, zb), the source
-- aggregated into them on request, filtered by the parameters (see _time_filter). For c >= z
-- those are the 2^(c - z) x 2^(c - z) squares of the tile; for c < z (overzoom) the single
-- ancestor square. One layer named `cells`, extent 4096, buffer 0, features in quadkey order.

-- The tile query as SQL text (exposed for EXPLAIN in tests).
create or replace function gridtiles._mvt_query(name text, z int, x int, y int, params jsonb default '{}',
    cached boolean default true) returns text
language plpgsql stable parallel safe as $$
declare
    l gridtiles.layers;
    c int;
begin
    perform gridtiles.qk_from_xyz(z, x, y);  -- validates the tile address (SQLSTATE 22003)
    select * into strict l from gridtiles.layers where layers.name = _mvt_query.name;
    c := least(greatest(z + l.detail, l.zmin), l.zb);
    return format(
        'select ST_AsMVT(f, ''cells'', 4096, ''geom'') from (
            select gridtiles.qk_to_text(%1$s, gridtiles._qk(_x, _y)) as qk, %1$s::smallint as z, n%2$s,
                ST_AsMVTGeom(ST_TileEnvelope(%1$s, _x, _y), ST_TileEnvelope(%3$s, %4$s, %5$s), 4096, 0) as geom
            from (%6$s) a
            order by gridtiles._qk(_x, _y)
        ) f',
        c,
        (select coalesce(string_agg(format(', %I', v->>'name'), '' order by i), '')
         from jsonb_array_elements(l.spec->'values') with ordinality e(v, i) where v->>'agg' <> 'count'),
        z, x, y, gridtiles._aggregate_sql(l, c, z, x, y, params, cached)
    );
end $$;

-- Vector tile for one layer; an empty tile is a zero-length bytea. With `cached` false, levels
-- in the cache (refresh_cache) are aggregated from the source as well. JIT is off: PostGIS functions
-- carry high cost estimates, so the planner would JIT-compile most tile queries, which takes
-- longer (around 150 ms) than running them.
create or replace function gridtiles.mvt(name text, z int, x int, y int, params jsonb default '{}',
    cached boolean default true) returns bytea
language plpgsql stable parallel safe set jit = off as $$
declare
    result bytea;
begin
    execute gridtiles._mvt_query(name, z, x, y, params, cached) into result;
    return coalesce(result, ''::bytea);
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
        l.zb, gridtiles._aggregate_sql(l, l.zb, 0, 0, 0, params, false)
    ) into result;
    return result;
end $$;

-- The distinct values of a layer's time column, ascending, as text; NULL without a time column or
-- with more than 1000 values.
create or replace function gridtiles.times(name text) returns text[]
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
        'select array_agg(t::text order by t) from (select distinct %1$I as t from %2$s where %1$I is not null limit 1001) d',
        l.time_column, l.source) into result;
    return case when cardinality(result) > 1000 then null else coalesce(result, '{}') end;
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
        tmax, gridtiles._aggregate_sql(l, tmax, 0, 0, 0, params, false), tmin);
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
    if max_zoom < l.zmin then
        raise exception 'max_zoom % is below min_zoom %', max_zoom, l.zmin using errcode = '22023';
    end if;
    execute format('drop table if exists gridtiles.%I', tbl);
    if max_zoom is not null then
        execute format('create table gridtiles.%I as %s', tbl, (
            select string_agg(format('select %s::smallint as _z, * from (%s) a', c, gridtiles._aggregate_sql(l, c, 0, 0, 0, '{}', false)),
                ' union all ')
            from generate_series(l.zmin, least(max_zoom, l.zb)) c));
        get diagnostics squares = row_count;
        execute format('create index on gridtiles.%I (_z, _x, _y)', tbl);
    end if;
    update gridtiles.layers set (cache_zoom, cached_at) = (select least(max_zoom, zb), clock_timestamp() where max_zoom is not null)
    where layers.name = l.name;
    return squares;
end $$;
