-- Diff: a read-only layer whose values are the difference of tile properties, B - A, at every level.
-- Cells are matched by a full outer join on (z, qk). A missing cell counts as 0 for sums and n; mean,
-- min and max differences exist only where both sides have data. The result keeps the stored-column
-- shape of its inputs, so tiles and stats work unchanged: a mean's difference is stored as `_sum`
-- with weight `_w` = 1.

create or replace function gridtiles.diff(
    a text, a_snapshot date, b text, b_snapshot date, name text
) returns void
language plpgsql as $$
declare
    la gridtiles.layers;
    lb gridtiles.layers;
    side text;
    cols text;
    exprs text;
begin
    select * into strict la from gridtiles.layers where layers.name = a;
    select * into strict lb from gridtiles.layers where layers.name = b;
    if (la.zb, la.zmin, la.detail) is distinct from (lb.zb, lb.zmin, lb.detail)
       or la.spec->'values' is distinct from lb.spec->'values' then
        raise exception 'layers % and % differ in zooms or values', a, b using errcode = '22023';
    end if;
    perform gridtiles.create_layer(
        (lb.spec - 'snapshots') || jsonb_build_object(
            'name', name,
            'diff', jsonb_build_array(a || coalesce('@' || a_snapshot, ''), b || coalesce('@' || b_snapshot, ''))
        )
    );

    select string_agg(format(', %I', c.col), '' order by c.i),
           string_agg(', ' || case
               -- weight 1 where the mean's difference exists, so merging diff cells averages them
               when c.part = 'weight' then format('case when b.%1$I > 0 and a.%1$I > 0 then 1 end', c.col)
               when c.agg = 'sum' then format('coalesce(b.%1$I, 0) - coalesce(a.%1$I, 0)', c.col)
               when c.agg = 'mean' then format('b.%1$I / nullif(b.%2$I, 0) - a.%1$I / nullif(a.%2$I, 0)',
                                               c.col, c.value || '_w')
               else format('b.%1$I - a.%1$I', c.col)
           end, '' order by c.i)
    into cols, exprs
    from gridtiles._columns(lb.spec) c;

    execute format(
        'insert into gridtiles.%I (z, qk, n%s)
         select coalesce(b.z, a.z), coalesce(b.qk, a.qk), coalesce(b.n, 0) - coalesce(a.n, 0)%s
         from (select * from gridtiles.%I where true%s) a
         full outer join (select * from gridtiles.%I where true%s) b on a.z = b.z and a.qk = b.qk',
        'cells_' || name, coalesce(cols, ''), coalesce(exprs, ''),
        'cells_' || a, coalesce(' and snapshot = ' || gridtiles._snapshot(la, a_snapshot), ''),
        'cells_' || b, coalesce(' and snapshot = ' || gridtiles._snapshot(lb, b_snapshot), '')
    );
    perform gridtiles.refresh_stats(name);
end $$;
