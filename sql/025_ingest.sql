-- Ingest: aggregate a staging table of points into a layer's base cells (z = zb).
-- The staging table has a `geom` column plus one double precision column per value source and
-- weight. Repeated loads add up: existing cells are merged per aggregator. Rows are written in
-- quadkey order, so concurrent loads into one layer lock cells in the same order and avoid deadlocks.

create or replace function gridtiles.ingest_points(name text, staging regclass) returns bigint
language plpgsql as $$
declare
    l gridtiles.layers;
    written bigint;
begin
    select * into strict l from gridtiles.layers where layers.name = ingest_points.name;
    execute format(
        'insert into gridtiles.%I as t (z, qk, n%s)
         select %s, _cell, count(*)%s
         from (select gridtiles.qk_from_point(geom, %s) as _cell, * from %s
               where geom is not null and not ST_IsEmpty(geom)) s
         group by _cell
         order by _cell
         on conflict (z, qk) do update set n = t.n + excluded.n%s',
        'cells_' || l.name, gridtiles._column_list(l.spec), l.zb, gridtiles._aggregates(l.spec), l.zb, staging,
        gridtiles._merge_set(l.spec, 't', 'excluded')
    );
    get diagnostics written = row_count;
    return written;
end $$;
