-- Test-only reference for the tile query's point assignment (_square_sql inlines the same float
-- operations): the square at zoom z holding a point of any SRID, as PL/pgSQL. Installed by the
-- test fixture and the benchmark after the schema.

-- NULL if empty. The latitude is clamped to the Web Mercator limit and projected with the
-- spherical formula; a point on a boundary belongs to the square east / south of it, points
-- beyond the edges to the edge squares.
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
