-- Live aggregation: SQL builders that aggregate a layer's source into the squares of one region
-- at zoom c. The public functions in 040_api.sql run the queries built here.
-- Square (_x, _y) at zoom c is ST_TileEnvelope(c, _x, _y); its quadkey is quadkey(c, _x, _y).

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
-- 4326 (_source_sql transforms any other SRID to 4326 first): the spherical Web Mercator formula
-- with the latitude clamped, inlined so a tile needs no function call per row. A point on a
-- boundary goes east / south; points beyond the edges go to the edge squares.
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

-- Tile index range at zoom z covered by the bounding box of an EPSG:3857 geometry, widened by 1 mm:
-- offset by half the world width, a coordinate's float spacing is several nanometres, so an edge
-- just across a square boundary would round onto it. Squares only touched have no area and drop out.
create or replace function gridtiles._bbox_tile_range(g geometry, z int, out x0 int, out x1 int, out y0 int, out y1 int)
language sql immutable strict parallel safe as $$
    select greatest(floor((ST_XMin(g) - 0.001 + 20037508.342789244) / size)::int, 0),
           least(floor((ST_XMax(g) + 0.001 + 20037508.342789244) / size)::int, top),
           greatest(floor((20037508.342789244 - ST_YMax(g) - 0.001) / size)::int, 0),
           least(floor((20037508.342789244 - ST_YMin(g) + 0.001) / size)::int, top)
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
-- the layer's cache_zoom read the cache instead if `cached`, unless a parameter filters the source.
create or replace function gridtiles._aggregate_sql(l gridtiles.layers, c int, z int, x int, y int, params jsonb,
    cached boolean) returns text
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
    if cached and c <= l.cache_zoom and t.filter = 'true' then
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
