-- Rollup: rebuild levels zb - 1 down to zmin from their children, then refresh stats and bounds.

drop function if exists gridtiles.rollup(text);  -- pre-release signature

-- On snapshot layers, every snapshot is rebuilt, or only `snapshot` if given.
create or replace function gridtiles.rollup(name text, snapshot date default null) returns void
language plpgsql as $$
declare
    l gridtiles.layers;
    cells text;
    by_snapshot text;
    scope text := '';
begin
    select * into strict l from gridtiles.layers where layers.name = rollup.name;
    perform gridtiles._check_writable(l);
    cells := format('gridtiles.%I', 'cells_' || l.name);
    by_snapshot := case when coalesce((l.spec->>'snapshots')::boolean, false) then 'snapshot, ' else '' end;
    if snapshot is not null then
        scope := ' and snapshot = ' || gridtiles._snapshot(l, snapshot);
    end if;
    execute format('delete from %s where z < %s%s', cells, l.zb, scope);
    for z in reverse l.zb - 1..l.zmin loop
        execute format(
            'insert into %1$s (%2$s, n%3$s) select %4$s%5$s, qk >> 2, sum(n)%6$s from %1$s where z = %7$s%8$s
             group by %4$sqk >> 2',
            cells, gridtiles._key(l.spec), gridtiles._column_list(l.spec), by_snapshot, z,
            gridtiles._merge_aggregates(l.spec), z + 1, scope
        );
    end loop;
    perform gridtiles.refresh_stats(name);
end $$;

-- Layer stats (per value at zb: min, max, 7 quantile breakpoints of the tile property and, for count
-- and sum values, the total), row count and WGS84 bounds of the base cells; of the latest snapshot
-- on snapshot layers. Scans the base level.
create or replace function gridtiles.refresh_stats(name text) returns void
language plpgsql as $$
declare
    l gridtiles.layers;
    cells text;
    base text;
    p record;
    layer_stats jsonb := '{}';
    box double precision[];
    total bigint;
    s jsonb;
begin
    select * into strict l from gridtiles.layers where layers.name = refresh_stats.name;
    cells := format('gridtiles.%I', 'cells_' || l.name);
    base := format('z = %s', l.zb);
    if coalesce((l.spec->>'snapshots')::boolean, false) then
        base := base || format(' and snapshot = (select max(snapshot) from %s)', cells);
    end if;
    for p in select * from gridtiles._properties(l.spec) loop
        execute format(
            'select jsonb_build_object(''total'', %s, ''min'', min(v), ''max'', max(v), ''breaks'',
                percentile_cont(array[0.125, 0.25, 0.375, 0.5, 0.625, 0.75, 0.875]) within group (order by v))
             from (select %s as v from %s where %s) c',
            case when p.agg in ('count', 'sum') then 'sum(v)' else 'null' end, p.expr, cells, base
        ) into s;
        layer_stats := layer_stats || jsonb_build_object(p.value, s);
    end loop;
    -- Bounds from the extreme tile indices at zb: two corner envelopes instead of one per cell.
    execute format(
        'select array[ST_XMin(nw), ST_YMin(se), ST_XMax(se), ST_YMax(nw)]
         from (select min(x) x0, min(y) y0, max(x) x1, max(y) y1
               from (select gridtiles._compact(qk) x, gridtiles._compact(qk >> 1) y from %1$s where %3$s) c) b,
         lateral (select ST_Transform(ST_TileEnvelope(%2$s, x0::int, y0::int), 4326) nw,
                         ST_Transform(ST_TileEnvelope(%2$s, x1::int, y1::int), 4326) se) e',
        cells, l.zb, base
    ) into box;
    execute format('select count(*) from %s', cells) into total;
    update gridtiles.layers set stats = layer_stats, cells = total, bounds = box, updated_at = now()
    where layers.name = l.name;
end $$;
