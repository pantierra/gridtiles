-- Layer registry: a layer is a named view over a source relation with a geometry column. Nothing
-- is copied; tiles aggregate the source on request (040_api.sql). Spec checks mirror
-- src/gridtiles/spec.py; an invalid spec raises SQLSTATE 22023.

create table if not exists gridtiles.layers (
    name text primary key check (name ~ '^[a-z][a-z0-9_]{0,47}$'),
    spec jsonb not null,
    source text not null,  -- quoted, schema-qualified relation name; resolved on every request
    geometry text not null,
    srid int not null,
    kind text not null check (kind in ('point', 'polygon')),
    time_column text,  -- filtered by the tile parameters at, start, end and diff
    time_type regtype,
    zb smallint not null,
    zmin smallint not null,
    detail smallint not null,
    cache_zoom smallint,  -- tiles at levels up to this read gridtiles.cache_<name> (refresh_cache)
    cached_at timestamptz
);

create or replace function gridtiles._check_spec(spec jsonb) returns void
language plpgsql immutable parallel safe as $$
declare
    k text;
    v jsonb;
    src jsonb := spec->'source';
begin
    if coalesce(spec->>'name', '') !~ '^[a-z][a-z0-9_]{0,47}$' then
        raise exception 'layer name % must match ^[a-z][a-z0-9_]{0,47}$', spec->'name' using errcode = '22023';
    end if;
    if jsonb_typeof(src) is distinct from 'object' or jsonb_typeof(src->'table') is distinct from 'string' then
        raise exception 'source must be an object with a table name' using errcode = '22023';
    elsif src->'geometry' is not null and jsonb_typeof(src->'geometry') <> 'string' then
        raise exception 'source.geometry must be a column name' using errcode = '22023';
    elsif src->'time' is not null and jsonb_typeof(src->'time') <> 'string' then
        raise exception 'source.time must be a column name' using errcode = '22023';
    elsif exists (select from jsonb_object_keys(src) o(key) where o.key not in ('table', 'geometry', 'time')) then
        raise exception 'source has unknown keys' using errcode = '22023';
    end if;
    -- Checked ahead of the ::int casts below; detail is optional.
    foreach k in array array['base_zoom', 'min_zoom', 'detail'] loop
        if (k <> 'detail' or spec->>k is not null) and coalesce(spec->>k, '') !~ '^\d{1,2}$' then
            raise exception '% must be a non-negative integer, got %', k, spec->k using errcode = '22023';
        end if;
    end loop;
    if not coalesce((spec->>'min_zoom')::int between 0 and (spec->>'base_zoom')::int and (spec->>'base_zoom')::int <= 30, false) then
        raise exception 'need 0 <= min_zoom <= base_zoom <= 30, got % and %', spec->'min_zoom', spec->'base_zoom'
            using errcode = '22023';
    end if;
    if coalesce((spec->>'detail')::int, 4) not between 0 and 8 then
        raise exception 'detail % outside [0, 8]', spec->'detail' using errcode = '22023';
    end if;
    if jsonb_typeof(spec->'values') is distinct from 'array' then
        raise exception 'values must be an array' using errcode = '22023';
    end if;
    for v in select jsonb_array_elements(spec->'values') loop
        if coalesce(v->>'name', '') !~ '^[a-z][a-z0-9_]{0,47}$' then
            raise exception 'value name % must match ^[a-z][a-z0-9_]{0,47}$', v->'name' using errcode = '22023';
        elsif jsonb_typeof(v->'from') is distinct from 'string' then
            raise exception 'value %: from must be a column name', v->'name' using errcode = '22023';
        elsif coalesce(v->>'agg', '') not in ('count', 'sum', 'min', 'max', 'mean') then
            raise exception 'value %: agg % not one of count, sum, min, max, mean', v->'name', v->'agg'
                using errcode = '22023';
        elsif v->>'weight' is not null and v->>'agg' <> 'mean' then
            raise exception 'value %: weight is only allowed with agg mean', v->'name' using errcode = '22023';
        end if;
    end loop;
    if (select count(distinct x->>'name') <> count(*) from jsonb_array_elements(spec->'values') x) then
        raise exception 'duplicate value names' using errcode = '22023';
    end if;
end $$;

-- The columns a layer reads from its source: each value's `from` and `weight` (count reads none).
-- `alias` is the name the tile query gives the column after casting it to double precision.
create or replace function gridtiles._sources(spec jsonb) returns table (col text, alias text)
language sql immutable strict parallel safe as $$
    select col, '_s' || row_number() over (order by min(i), col)
    from jsonb_array_elements(spec->'values') with ordinality e(v, i),
        lateral (values (v->>'from'), (v->>'weight')) s(col)
    where v->>'agg' <> 'count' and col is not null
    group by col
$$;

-- The type of a source column; raises SQLSTATE 42703 if it does not exist.
create or replace function gridtiles._column_type(rel regclass, col text, out typ regtype, out typmod int)
language plpgsql stable as $$
begin
    select atttypid, atttypmod into typ, typmod from pg_attribute
    where attrelid = rel and attname = col and attnum > 0 and not attisdropped;
    if not found then
        raise exception 'source % has no column %', rel, col using errcode = '42703';
    end if;
end $$;

-- Register a layer over its source: a typed point or polygon geometry column, numeric value
-- columns and an optional date or timestamp column. Creates the tile function tile_<name>, a
-- Martin function source: a malformed time parameter gives an empty tile and a logged warning.
create or replace function gridtiles.create_layer(spec jsonb) returns void
language plpgsql as $$
declare
    name text := spec->>'name';
    rel regclass;
    geom text := coalesce(spec#>>'{source,geometry}', 'geom');
    time_column text := spec#>>'{source,time}';
    g record;
    time_type regtype;
    kind text;
    col text;
begin
    perform gridtiles._check_spec(spec);
    rel := to_regclass(spec#>>'{source,table}');
    if rel is null then
        raise exception 'source table % does not exist', spec#>'{source,table}' using errcode = '42P01';
    end if;
    g := gridtiles._column_type(rel, geom);
    kind := case regexp_replace(postgis_typmod_type(g.typmod), '(ZM|Z|M)$', '')
        when 'Point' then 'point' when 'Polygon' then 'polygon' when 'MultiPolygon' then 'polygon' end;
    if g.typ <> 'geometry'::regtype or kind is null or postgis_typmod_srid(g.typmod) = 0 then
        raise exception 'source column %.% is %, not geometry(Point|Polygon|MultiPolygon, <srid>)', rel, geom,
            format_type(g.typ, g.typmod) using errcode = '42804';
    end if;
    if time_column is not null then
        time_type := (gridtiles._column_type(rel, time_column)).typ;
        if time_type not in ('date'::regtype, 'timestamp'::regtype, 'timestamptz'::regtype) then
            raise exception 'source.time column %.% is %, not a date or timestamp', rel, time_column, time_type
                using errcode = '42804';
        end if;
    end if;
    for col in select s.col from gridtiles._sources(spec) s loop
        if (select typcategory from pg_type where oid = (gridtiles._column_type(rel, col)).typ) <> 'N' then
            raise exception 'source column %.% is not numeric', rel, col using errcode = '42804';
        end if;
    end loop;

    insert into gridtiles.layers (name, spec, source, geometry, srid, kind, time_column, time_type, zb, zmin, detail)
    select name, spec, format('%I.%I', n.nspname, c.relname), geom, postgis_typmod_srid(g.typmod), kind, time_column,
        time_type, (spec->>'base_zoom')::int, (spec->>'min_zoom')::int, coalesce((spec->>'detail')::int, 4)
    from pg_class c join pg_namespace n on n.oid = c.relnamespace where c.oid = rel;
    execute format(
        'create function gridtiles.%5$I(z integer, x integer, y integer, query_params json) returns bytea
         language plpgsql stable as $f$
         begin
             begin
                 perform gridtiles._time_filter(l, coalesce(query_params::jsonb, %2$L))
                 from gridtiles.layers l where l.name = %1$L;
             exception when invalid_parameter_value then
                 raise warning %3$L, sqlerrm;
                 return %4$L::bytea;
             end;
             return gridtiles.mvt(%1$L, z, x, y, coalesce(query_params::jsonb, %2$L));
         end $f$',
        name, '{}', 'tile_' || name || ': %', '', 'tile_' || name
    );
end $$;

create or replace function gridtiles.drop_layer(name text) returns void
language plpgsql as $$
begin
    delete from gridtiles.layers l where l.name = drop_layer.name;
    if not found then
        raise exception 'layer % does not exist', name using errcode = '42704';
    end if;
    execute format('drop function gridtiles.%I(integer, integer, integer, json)', 'tile_' || name);
    execute format('drop table if exists gridtiles.%I', 'cache_' || name);
end $$;
