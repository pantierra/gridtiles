-- MVT encoder: a tile at zoom t carries the cells at c(t) = clamp(t + detail, zmin, zb).
-- For c >= t that is one contiguous key range of descendants; for c < t (overzoom) the single
-- ancestor cell. One layer named `cells`, extent 4096, buffer 0.

-- The tile query as SQL text (exposed for EXPLAIN in tests).
create or replace function gridtiles._mvt_query(name text, z int, x int, y int) returns text
language plpgsql stable parallel safe as $$
declare
    l gridtiles.layers;
    tile bigint := gridtiles.qk_from_xyz(z, x, y);
    c int;
    lo bigint;
    hi bigint;
begin
    select * into strict l from gridtiles.layers where layers.name = _mvt_query.name;
    c := least(greatest(z + l.detail, l.zmin), l.zb);
    if c >= z then
        lo := tile << (2 * (c - z));
        hi := (tile + 1) << (2 * (c - z));
    else
        lo := tile >> (2 * (z - c));
        hi := lo + 1;
    end if;
    return format(
        'select ST_AsMVT(f, ''cells'', 4096, ''geom'') from (
            select gridtiles.qk_to_text(z, qk) as qk, z, n%s,
                ST_AsMVTGeom(gridtiles._envelope(z, qk), ST_TileEnvelope(%s, %s, %s), 4096, 0) as geom
            from gridtiles.%I where z = %s and qk >= %s and qk < %s order by qk
        ) f',
        (select coalesce(string_agg(format(', %s as %I', p.expr, p.value), '' order by p.i), '')
         from gridtiles._properties(l.spec) with ordinality p(value, agg, expr, i) where p.agg <> 'count'),
        z, x, y, 'cells_' || l.name, c, lo, hi
    );
end $$;

-- Vector tile for one layer; an empty tile is a zero-length bytea.
create or replace function gridtiles.mvt(name text, z int, x int, y int) returns bytea
language plpgsql stable parallel safe as $$
declare
    result bytea;
begin
    execute gridtiles._mvt_query(name, z, x, y) into result;
    return coalesce(result, ''::bytea);
end $$;
