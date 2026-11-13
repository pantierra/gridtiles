-- Quadkeys as integer Morton codes.
-- A cell is identified by (z, qk): x in the even bits, y in the odd bits, XYZ tile addressing.
-- Public qk_* functions validate their input: out of range raises SQLSTATE 22003. Internal _*
-- helpers skip validation.
-- PostgreSQL gives |, &, <<, >> equal precedence, so bit expressions are fully parenthesised.

create or replace function gridtiles._check_zoom(z int, lo int default 0, hi int default 30) returns void
language plpgsql immutable parallel safe as $$
begin
    if z not between lo and hi then
        raise exception 'zoom % outside [%, %]', z, lo, hi using errcode = '22003';
    end if;
end $$;

create or replace function gridtiles._check_cell(z int, qk bigint) returns void
language plpgsql immutable parallel safe as $$
begin
    perform gridtiles._check_zoom(z);
    if qk < 0 or qk >= 1::bigint << (2 * z) then
        raise exception 'quadkey % outside [0, 4^%)', qk, z using errcode = '22003';
    end if;
end $$;

-- Interleave zero bits: bit i of v moves to bit 2i.
create or replace function gridtiles._spread(v bigint) returns bigint
language plpgsql immutable strict parallel safe as $$
begin
    v := (v | (v << 16)) & 0x0000FFFF0000FFFF;
    v := (v | (v << 8)) & 0x00FF00FF00FF00FF;
    v := (v | (v << 4)) & 0x0F0F0F0F0F0F0F0F;
    v := (v | (v << 2)) & 0x3333333333333333;
    return (v | (v << 1)) & 0x5555555555555555;
end $$;

-- Inverse of _spread: keep the even bits, packed.
create or replace function gridtiles._compact(v bigint) returns bigint
language plpgsql immutable strict parallel safe as $$
begin
    v := v & 0x5555555555555555;
    v := (v | (v >> 1)) & 0x3333333333333333;
    v := (v | (v >> 2)) & 0x0F0F0F0F0F0F0F0F;
    v := (v | (v >> 4)) & 0x00FF00FF00FF00FF;
    v := (v | (v >> 8)) & 0x0000FFFF0000FFFF;
    return (v | (v >> 16)) & 0x00000000FFFFFFFF;
end $$;

create or replace function gridtiles._qk(x bigint, y bigint) returns bigint
language sql immutable strict parallel safe as $$
    select gridtiles._spread(x) | (gridtiles._spread(y) << 1)
$$;

-- EPSG:3857 metres -> cell; points on a boundary go east / south, the edges are clamped.
create or replace function gridtiles._from_mercator(mx double precision, my double precision, z int) returns bigint
language plpgsql immutable strict parallel safe as $$
declare
    size constant double precision := 40075016.68557849::double precision / (1::bigint << z);
    top constant bigint := (1::bigint << z) - 1;
begin
    return gridtiles._qk(
        least(greatest(floor((mx + 20037508.342789244) / size), 0), top)::bigint,
        least(greatest(floor((20037508.342789244 - my) / size), 0), top)::bigint
    );
end $$;

create or replace function gridtiles.qk_from_xyz(z int, x int, y int) returns bigint
language plpgsql immutable strict parallel safe as $$
begin
    perform gridtiles._check_zoom(z);
    if x < 0 or y < 0 or x >= 1::bigint << z or y >= 1::bigint << z then
        raise exception 'tile %/% outside [0, 2^%)', x, y, z using errcode = '22003';
    end if;
    return gridtiles._qk(x, y);
end $$;

create or replace function gridtiles.qk_to_xy(z int, qk bigint, out x int, out y int)
language plpgsql immutable strict parallel safe as $$
begin
    perform gridtiles._check_cell(z, qk);
    x := gridtiles._compact(qk);
    y := gridtiles._compact(qk >> 1);
end $$;

-- Bing quadkey string: exactly z base-4 digits, most significant first.
create or replace function gridtiles.qk_to_text(z int, qk bigint) returns text
language plpgsql immutable strict parallel safe as $$
begin
    perform gridtiles._check_cell(z, qk);
    return coalesce(string_agg(((qk >> (2 * i)) & 3)::text, '' order by i desc), '')
        from generate_series(0, z - 1) i;
end $$;

create or replace function gridtiles.qk_envelope(z int, qk bigint) returns geometry(Polygon, 3857)
language plpgsql immutable strict parallel safe as $$
begin
    perform gridtiles._check_cell(z, qk);
    return ST_TileEnvelope(z, gridtiles._compact(qk)::int, gridtiles._compact(qk >> 1)::int);
end $$;

-- Cell containing a point of any SRID; empty -> NULL. Latitude is clamped to the Web Mercator
-- limit and projected with the spherical Web Mercator formula.
create or replace function gridtiles.qk_from_point(geom geometry, z int) returns bigint
language plpgsql immutable strict parallel safe as $$
declare
    lat double precision;
begin
    perform gridtiles._check_zoom(z);
    if ST_IsEmpty(geom) then
        return null;
    elsif ST_SRID(geom) = 3857 then
        return gridtiles._from_mercator(ST_X(geom), ST_Y(geom), z);
    elsif ST_SRID(geom) <> 4326 then
        geom := ST_Transform(geom, 4326);
    end if;
    lat := radians(greatest(-85.0511287798, least(85.0511287798, ST_Y(geom))));
    return gridtiles._from_mercator(
        ST_X(geom) * 20037508.342789244 / 180,
        ln(tan(pi() / 4 + lat / 2)) * 20037508.342789244 / pi(),
        z
    );
end $$;
