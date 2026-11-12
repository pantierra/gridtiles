-- Rollup: rebuild levels zb - 1 down to zmin from their children, then refresh the layer stats.

create or replace function gridtiles.rollup(name text) returns void
language plpgsql as $$
declare
    l gridtiles.layers;
    cells text;
    p record;
    layer_stats jsonb := '{}';
    s jsonb;
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

    -- Per value at zb: total (count and sum only; a sum of means or extremes means nothing), min, max
    -- and 7 quantile breakpoints of the tile property.
    for p in select * from gridtiles._properties(l.spec) loop
        execute format(
            'select jsonb_build_object(''total'', %s, ''min'', min(v), ''max'', max(v), ''breaks'',
                percentile_cont(array[0.125, 0.25, 0.375, 0.5, 0.625, 0.75, 0.875]) within group (order by v))
             from (select %s as v from %s where z = %s) c',
            case when p.agg in ('count', 'sum') then 'sum(v)' else 'null' end, p.expr, cells, l.zb
        ) into s;
        layer_stats := layer_stats || jsonb_build_object(p.value, s);
    end loop;
    update gridtiles.layers set stats = layer_stats, updated_at = now() where layers.name = l.name;
end $$;
