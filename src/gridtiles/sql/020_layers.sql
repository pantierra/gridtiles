-- Layer registry: a layer is a named view over a source relation with a geometry column. Nothing
-- is copied; tiles aggregate the source on request (040_mvt.sql). Spec checks mirror
-- src/gridtiles/spec.py; an invalid spec raises SQLSTATE 22023.

-- Databases installed before live aggregation hold a materialized registry (cell tables, rollup).
-- Its objects are dropped on upgrade; registered layers would lose their cells, so they must be
-- dropped by the user first.
do $$
begin
    if exists (select from information_schema.columns
               where table_schema = 'gridtiles' and table_name = 'layers' and column_name = 'cells') then
        if exists (select from gridtiles.layers) then
            raise exception 'gridtiles.layers holds materialized layers (%): drop them with gridtiles.drop_layer before upgrading',
                (select string_agg(name, ', ' order by name) from gridtiles.layers) using errcode = '55000';
        end if;
        drop function if exists gridtiles.ingest_points(text, regclass), gridtiles.ingest_polygons(text, regclass, bigint),
            gridtiles.rollup(text), gridtiles._insert_cells(gridtiles.layers, text, text),
            gridtiles._bbox_tile_range(geometry, int), gridtiles._mercator_polygon(geometry),
            gridtiles._columns(jsonb), gridtiles._merge(text, text, text), gridtiles._column_list(jsonb),
            gridtiles._aggregates(jsonb, text, text), gridtiles._merge_set(jsonb, text, text),
            gridtiles._merge_aggregates(jsonb), gridtiles._properties(jsonb),
            gridtiles._mvt_query(text, int, int, int), gridtiles.mvt(text, int, int, int);
        drop table gridtiles.layers;
    end if;
end $$;

create table if not exists gridtiles.layers (
    name text primary key check (name ~ '^[a-z][a-z0-9_]{0,47}$'),
    spec jsonb not null,
    source text not null,  -- quoted, schema-qualified relation name; resolved on every request
    geometry text not null,
    srid int not null,
    kind text not null check (kind in ('point', 'polygon')),
    zb smallint not null,
    zmin smallint not null,
    detail smallint not null,
    created_at timestamptz not null default now()
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
    elsif src->'kind' is not null and src->>'kind' not in ('point', 'polygon') then
        raise exception 'source.kind % not one of point, polygon', src->'kind' using errcode = '22023';
    elsif src->'srid' is not null and coalesce(src->>'srid', '') !~ '^[1-9]\d{0,8}$' then
        raise exception 'source.srid must be a positive integer, got %', src->'srid' using errcode = '22023';
    elsif exists (select from jsonb_object_keys(src) o(key) where o.key not in ('table', 'geometry', 'kind', 'srid')) then
        raise exception 'source has unknown keys' using errcode = '22023';
    end if;
    -- Checked before any ::int cast, which would raise 22P02 instead of 22023; detail is optional.
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

create or replace function gridtiles.create_layer(spec jsonb) returns void
language plpgsql as $$
#variable_conflict use_variable
declare
    name text := spec->>'name';
    rel regclass;
    relkind char;
    qualified text;
    geom text := coalesce(spec#>>'{source,geometry}', 'geom');
    att record;
    srid int;
    kind text;
    s record;
begin
    perform gridtiles._check_spec(spec);
    rel := to_regclass(spec#>>'{source,table}');
    if rel is null then
        raise exception 'source table % does not exist', spec#>'{source,table}' using errcode = '42P01';
    end if;
    select format('%I.%I', n.nspname, c.relname), c.relkind into qualified, relkind
    from pg_class c join pg_namespace n on n.oid = c.relnamespace where c.oid = rel;

    select atttypid, atttypmod into att from pg_attribute
    where attrelid = rel and attname = geom and attnum > 0 and not attisdropped;
    if not found then
        raise exception 'source % has no column %', qualified, geom using errcode = '42703';
    elsif att.atttypid <> 'geometry'::regtype then
        raise exception 'source column %.% is %, not geometry', qualified, geom, format_type(att.atttypid, att.atttypmod)
            using errcode = '42804';
    end if;

    -- SRID and kind come from the column's type modifier, or from the spec for an untyped column.
    srid := nullif(postgis_typmod_srid(att.atttypmod), 0);
    if srid is not null and (spec#>>'{source,srid}')::int is distinct from null and (spec#>>'{source,srid}')::int <> srid then
        raise exception 'source.srid % differs from the SRID % of %.%', spec#>'{source,srid}', srid, qualified, geom
            using errcode = '22023';
    end if;
    srid := coalesce(srid, (spec#>>'{source,srid}')::int);
    if srid is null then
        raise exception 'column %.% has no SRID constraint: set source.srid', qualified, geom using errcode = '22023';
    elsif not exists (select from spatial_ref_sys r where r.srid = srid) then
        raise exception 'SRID % is not in spatial_ref_sys', srid using errcode = '22023';
    end if;
    kind := case regexp_replace(postgis_typmod_type(att.atttypmod), '(ZM|Z|M)$', '')
        when 'Point' then 'point' when 'Polygon' then 'polygon' when 'MultiPolygon' then 'polygon'
        when 'Geometry' then spec#>>'{source,kind}'
    end;
    if kind is null and postgis_typmod_type(att.atttypmod) <> 'Geometry' then
        raise exception 'column %.% holds %; only points and polygons are supported', qualified, geom,
            postgis_typmod_type(att.atttypmod) using errcode = '0A000';
    elsif kind is null then
        raise exception 'column %.% has no geometry type constraint: set source.kind', qualified, geom
            using errcode = '22023';
    elsif spec#>>'{source,kind}' is distinct from null and spec#>>'{source,kind}' <> kind then
        raise exception 'source.kind % differs from the % column %.%', spec#>'{source,kind}', kind, qualified, geom
            using errcode = '22023';
    elsif kind = 'polygon' then
        raise exception 'polygon sources are not supported yet' using errcode = '0A000';
    end if;

    for s in select * from gridtiles._sources(spec) loop
        select atttypid, atttypmod into att from pg_attribute
        where attrelid = rel and attname = s.col and attnum > 0 and not attisdropped;
        if not found then
            raise exception 'source % has no column %', qualified, s.col using errcode = '42703';
        elsif (select typcategory from pg_type where oid = att.atttypid) <> 'N' then
            raise exception 'source column %.% is %, not numeric', qualified, s.col, format_type(att.atttypid, att.atttypmod)
                using errcode = '42804';
        end if;
    end loop;

    -- Tiles filter on the geometry (EPSG:4326 and 3857) or on its transform to 4326 (any other SRID).
    if relkind in ('r', 'p', 'm') and not exists (
        select from pg_index i join pg_class ic on ic.oid = i.indexrelid join pg_am am on am.oid = ic.relam
        where i.indrelid = rel and am.amname = 'gist' and (
            (srid in (3857, 4326) and i.indexprs is null
             and (select attnum from pg_attribute where attrelid = rel and attname = geom) = any(i.indkey::int2[]))
            or (srid not in (3857, 4326) and pg_get_indexdef(i.indexrelid) ilike '%st_transform(%' || geom || '%4326)%'))
    ) then
        raise warning 'source % has no GiST index for tiles: create index on % using gist (%)', qualified, qualified,
            case when srid in (3857, 4326) then quote_ident(geom) else format('ST_Transform(%I, 4326)', geom) end;
    end if;

    insert into gridtiles.layers (name, spec, source, geometry, srid, kind, zb, zmin, detail)
    values (name, spec, qualified, geom, srid, kind, (spec->>'base_zoom')::int, (spec->>'min_zoom')::int,
            coalesce((spec->>'detail')::int, 4));
    -- Tile server entry point (Martin function source, which passes the URL query as query_params).
    -- A malformed parameter (SQLSTATE 22023) gives an empty tile and a logged warning, not an error.
    execute format(
        'create function gridtiles.%I(z integer, x integer, y integer, query_params json) returns bytea
         language plpgsql stable as $f$
         begin
             return gridtiles.mvt(%L, z, x, y, coalesce(query_params::jsonb, %L));
         exception when invalid_parameter_value then
             raise warning %L, sqlerrm;
             return %L::bytea;
         end $f$',
        'tile_' || name, name, '{}', 'tile_' || name || ': %', ''
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
end $$;
