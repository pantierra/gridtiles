-- MVT encoder: a tile at zoom z carries the squares at c = clamp(z + detail, zmin, zb), the source
-- aggregated into them on request. For c >= z those are the 2^(c - z) x 2^(c - z) squares of the
-- tile; for c < z (overzoom) the single ancestor square. One layer named `cells`, extent 4096,
-- buffer 0, features in quadkey order.

-- Check request parameters; raises SQLSTATE 22023 on a malformed one. Unknown keys are ignored,
-- since tile servers forward every URL query parameter.
create or replace function gridtiles._check_params(l gridtiles.layers, params jsonb) returns void
language plpgsql immutable parallel safe as $$
begin
    if jsonb_typeof(params) is distinct from 'object' then
        raise exception 'tile parameters must be a JSON object, got %', params using errcode = '22023';
    end if;
end $$;

-- The tile query as SQL text (exposed for EXPLAIN in tests).
create or replace function gridtiles._mvt_query(name text, z int, x int, y int, params jsonb default '{}')
returns text
language plpgsql stable parallel safe as $$
declare
    l gridtiles.layers;
    c int;
begin
    perform gridtiles.qk_from_xyz(z, x, y);  -- validates the tile address (SQLSTATE 22003)
    select * into strict l from gridtiles.layers where layers.name = _mvt_query.name;
    perform gridtiles._check_params(l, params);
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
        z, x, y, gridtiles._aggregate_sql(l, c, z, x, y)
    );
end $$;

-- Vector tile for one layer; an empty tile is a zero-length bytea.
create or replace function gridtiles.mvt(name text, z int, x int, y int, params jsonb default '{}') returns bytea
language plpgsql stable parallel safe as $$
declare
    result bytea;
begin
    execute gridtiles._mvt_query(name, z, x, y, params) into result;
    return coalesce(result, ''::bytea);
end $$;
