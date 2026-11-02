-- Rollup: rebuild levels zb - 1 down to zmin from their children, then refresh stats and bounds.

create or replace function gridtiles.rollup(name text) returns void
language plpgsql as $$
declare
    l gridtiles.layers;
    cells text;
begin
    select * into strict l from gridtiles.layers where layers.name = rollup.name;
    cells := format('gridtiles.%I', 'cells_' || l.name);
    execute format('delete from %s where z < %s', cells, l.zb);
    for z in reverse l.zb - 1..l.zmin loop
        execute format(
            'insert into %1$s (z, qk, n%2$s) select %3$s, qk >> 2, sum(n)%4$s from %1$s where z = %5$s group by qk >> 2',
            cells, gridtiles._column_list(l.spec), z, gridtiles._merge_aggregates(l.spec), z + 1
        );
    end loop;
    perform gridtiles.refresh_stats(name);
end $$;

-- Layer stats (per value at zb: total, min, max and 7 quantile breakpoints of the tile property),
-- row count and WGS84 bounds of the base cells. Scans the base level.
create or replace function gridtiles.refresh_stats(name text) returns void
language plpgsql as $$
declare
    l gridtiles.layers;
    cells text;
    p record;
    layer_stats jsonb := '{}';
    box double precision[];
    total bigint;
    s jsonb;
begin
    select * into strict l from gridtiles.layers where layers.name = refresh_stats.name;
    cells := format('gridtiles.%I', 'cells_' || l.name);
    for p in select * from gridtiles._properties(l.spec) loop
        execute format(
            'select jsonb_build_object(''total'', sum(v), ''min'', min(v), ''max'', max(v), ''breaks'',
                percentile_cont(array[0.125, 0.25, 0.375, 0.5, 0.625, 0.75, 0.875]) within group (order by v))
             from (select %s as v from %s where z = %s) c',
            p.expr, cells, l.zb
        ) into s;
        layer_stats := layer_stats || jsonb_build_object(p.value, s);
    end loop;
    -- Bounds from the extreme tile indices at zb: two corner envelopes instead of one per cell.
    execute format(
        'select array[ST_XMin(nw), ST_YMin(se), ST_XMax(se), ST_YMax(nw)]
         from (select min(x) x0, min(y) y0, max(x) x1, max(y) y1
               from (select gridtiles._compact(qk) x, gridtiles._compact(qk >> 1) y from %1$s where z = %2$s) c) b,
         lateral (select ST_Transform(ST_TileEnvelope(%2$s, x0::int, y0::int), 4326) nw,
                         ST_Transform(ST_TileEnvelope(%2$s, x1::int, y1::int), 4326) se) e',
        cells, l.zb
    ) into box;
    execute format('select count(*) from %s', cells) into total;
    update gridtiles.layers set stats = layer_stats, cells = total, bounds = box, updated_at = now()
    where layers.name = l.name;
end $$;
