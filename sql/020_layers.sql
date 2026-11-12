-- Layer registry and per-layer cell tables. Spec checks mirror src/gridtiles/spec.py;
-- an invalid spec raises SQLSTATE 22023.

create table if not exists gridtiles.layers (
    name text primary key check (name ~ '^[a-z][a-z0-9_]{0,47}$'),
    spec jsonb not null,
    zb smallint not null,
    zmin smallint not null,
    detail smallint not null,
    stats jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

-- Stored columns per value, in table order: the single source for every function that writes or
-- reads cell values. `i` is the column position; `part` is 'value' or 'weight' (the mean's weight
-- sidecar); `merge` is the aggregate that combines partial results of the column (see _merge).
create or replace function gridtiles._columns(spec jsonb)
returns table (value text, agg text, col text, merge text, part text, src text, weight text, i bigint)
language sql immutable strict parallel safe as $$
    select v->>'name', v->>'agg', v->>'name' || c.suffix, c.merge, c.part, v->>'from', v->>'weight',
        row_number() over (order by e.i, c.pos)
    from jsonb_array_elements(spec->'values') with ordinality as e(v, i)
    join (values
        ('sum', '_sum', 'sum', 'value', 1), ('min', '_min', 'min', 'value', 1), ('max', '_max', 'max', 'value', 1),
        ('mean', '_sum', 'sum', 'value', 1), ('mean', '_w', 'sum', 'weight', 2)
    ) as c(agg, suffix, merge, part, pos) on c.agg = v->>'agg'
    order by e.i, c.pos
$$;

-- SQL expression merging two partial aggregates a and b (NULL means "no data").
create or replace function gridtiles._merge(merge text, a text, b text) returns text
language sql immutable strict parallel safe as $$
    select case merge
        when 'sum' then format('coalesce(%1$s + %2$s, %1$s, %2$s)', a, b)
        when 'min' then format('least(%s, %s)', a, b)
        when 'max' then format('greatest(%s, %s)', a, b)
    end
$$;

-- Comma-led list of a layer's value columns, e.g. ", population_sum, completeness_w".
create or replace function gridtiles._column_list(spec jsonb) returns text
language sql immutable strict parallel safe as $$
    select coalesce(string_agg(', ' || quote_ident(col), '' order by i), '')
    from gridtiles._columns(spec) c
$$;

-- Comma-led aggregates turning source records into value columns.
create or replace function gridtiles._aggregates(spec jsonb) returns text
language sql immutable strict parallel safe as $$
    select coalesce(string_agg(', ' || case
        when c.part = 'weight' then format('sum(case when %I is not null then %s end)', c.src, w)
        when c.agg in ('sum', 'mean') then format('sum(%I * %s)', c.src, w)
        else format('%s(%I)', c.merge, c.src)
    end, '' order by c.i), '')
    from gridtiles._columns(spec) c, lateral (select case when c.agg = 'mean' then coalesce(quote_ident(c.weight), '1') else '1' end as w) x(w)
$$;

-- Comma-led "col = merge(a.col, b.col)" assignments, e.g. for ON CONFLICT DO UPDATE.
create or replace function gridtiles._merge_set(spec jsonb, a text, b text) returns text
language sql immutable strict parallel safe as $$
    select coalesce(string_agg(format(', %I = %s', col,
        gridtiles._merge(merge, a || '.' || quote_ident(col), b || '.' || quote_ident(col))), '' order by i), '')
    from gridtiles._columns(spec) c
$$;

create or replace function gridtiles._check_spec(spec jsonb) returns void
language plpgsql immutable parallel safe as $$
declare
    k text;
    v jsonb;
begin
    if coalesce(spec->>'name', '') !~ '^[a-z][a-z0-9_]{0,47}$' then
        raise exception 'layer name % must match ^[a-z][a-z0-9_]{0,47}$', spec->'name' using errcode = '22023';
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

create or replace function gridtiles.create_layer(spec jsonb) returns void
language plpgsql as $$
declare
    name text := spec->>'name';
begin
    perform gridtiles._check_spec(spec);
    insert into gridtiles.layers (name, spec, zb, zmin, detail)
    values (name, spec, (spec->>'base_zoom')::int, (spec->>'min_zoom')::int, coalesce((spec->>'detail')::int, 4));
    execute format(
        'create table gridtiles.%I (z smallint, qk bigint, n bigint not null%s, primary key (z, qk))',
        'cells_' || name,
        (select coalesce(string_agg(format(', %I double precision', col), '' order by i), '')
         from gridtiles._columns(spec) c)
    );
end $$;

create or replace function gridtiles.drop_layer(name text) returns void
language plpgsql as $$
begin
    delete from gridtiles.layers l where l.name = drop_layer.name;
    if not found then
        raise exception 'layer % does not exist', name using errcode = '42704';
    end if;
    execute format('drop table gridtiles.%I', 'cells_' || name);
end $$;
