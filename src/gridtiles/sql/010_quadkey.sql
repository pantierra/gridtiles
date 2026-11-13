-- The grid: square (x, y) at zoom z is the Web Mercator tile z/x/y (XYZ addressing, y = 0 at the
-- north edge). Its quadkey is the Bing quadkey: z base-4 digits, most significant first. All
-- quadkeys of a zoom have the same length, so their text order is the Morton order of the squares.

create or replace function gridtiles.quadkey(z int, x int, y int) returns text
language sql immutable strict parallel safe as $$
    select coalesce(string_agg((((x >> i) & 1) + 2 * ((y >> i) & 1))::text, '' order by i desc), '')
    from generate_series(0, z - 1) i
$$;

-- The square at zoom z holding a point of any SRID; NULL if empty. The latitude is clamped to the
-- Web Mercator limit and projected with the spherical formula; a point on a boundary belongs to
-- the square east / south of it, points beyond the edges to the edge squares. The tile query
-- inlines these float operations (_square_sql).
create or replace function gridtiles.point_square(geom geometry, z int, out x int, out y int)
language plpgsql immutable strict parallel safe as $$
declare
    size constant double precision := 40075016.68557849::double precision / (1::bigint << z);
    top constant bigint := (1::bigint << z) - 1;
    mx double precision;
    my double precision;
    lat double precision;
begin
    if ST_IsEmpty(geom) then
        return;
    elsif ST_SRID(geom) = 3857 then
        mx := ST_X(geom);
        my := ST_Y(geom);
    else
        if ST_SRID(geom) <> 4326 then
            geom := ST_Transform(geom, 4326);
        end if;
        lat := radians(greatest(-85.0511287798, least(85.0511287798, ST_Y(geom))));
        mx := ST_X(geom) * 20037508.342789244 / 180;
        my := ln(tan(pi() / 4 + lat / 2)) * 20037508.342789244 / pi();
    end if;
    x := least(greatest(floor((mx + 20037508.342789244) / size), 0), top);
    y := least(greatest(floor((20037508.342789244 - my) / size), 0), top);
end $$;
