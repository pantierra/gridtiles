-- Incremental upserts: apply a delta of base cells and recompute only the touched ancestors.
-- The result equals a full rollup from the final base level, bit for bit: ancestors are rebuilt
-- from their four children with the same ordered merges.

-- `delta` is shaped like the layer's cell table and holds cells at zb. mode 'add' merges them into
-- existing cells (like a load); 'replace' overwrites the touched cells. Runs under a per-layer
-- advisory lock, so concurrent upserts apply one after the other. Returns the base cells written.
create or replace function gridtiles.apply_delta(name text, delta regclass, mode text default 'add') returns bigint
language plpgsql as $$
declare
    l gridtiles.layers;
    cells text;
    written bigint;
begin
    if mode not in ('add', 'replace') then
        raise exception 'mode % not one of add, replace', mode using errcode = '22023';
    end if;
    select * into strict l from gridtiles.layers where layers.name = apply_delta.name;
    perform gridtiles._check_writable(l);
    if coalesce((l.spec->>'snapshots')::boolean, false) then
        raise exception 'layer % has snapshots: load a new snapshot instead of upserting', l.name using errcode = '0A000';
    end if;
    perform pg_advisory_xact_lock(hashtextextended('gridtiles.layer:' || l.name, 0));
    cells := format('gridtiles.%I', 'cells_' || l.name);
    execute format(
        'insert into %1$s as t (z, qk, n%2$s) select z, qk, n%2$s from %3$s
         on conflict (z, qk) do update set n = %4$s%5$s',
        cells, gridtiles._column_list(l.spec), delta,
        case mode when 'add' then 't.n + excluded.n' else 'excluded.n' end,
        case mode when 'add' then gridtiles._merge_set(l.spec, 't', 'excluded')
             else gridtiles._replace_set(l.spec, 'excluded') end
    );
    get diagnostics written = row_count;

    -- Level by level, rebuild the parents of what changed below from their four children.
    for z in reverse l.zb - 1..l.zmin loop
        execute format(
            'delete from %1$s c using (select distinct qk >> %4$s as p from %3$s) t where c.z = %2$s and c.qk = t.p',
            cells, z, delta, 2 * (l.zb - z)
        );
        execute format(
            'insert into %1$s (z, qk, n%5$s)
             select %2$s, c.qk >> 2, sum(c.n)%6$s
             from (select distinct qk >> %4$s as p from %3$s) t
             join %1$s c on c.z = %2$s + 1 and c.qk >= t.p << 2 and c.qk < (t.p + 1) << 2
             group by c.qk >> 2',
            cells, z, delta, 2 * (l.zb - z), gridtiles._column_list(l.spec), gridtiles._merge_aggregates(l.spec)
        );
    end loop;
    return written;
end $$;
