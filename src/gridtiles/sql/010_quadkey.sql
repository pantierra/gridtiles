-- The grid: square (x, y) at zoom z is the Web Mercator tile z/x/y (XYZ addressing, y = 0 at the
-- north edge). Its quadkey is the Bing quadkey: z base-4 digits, most significant first. All
-- quadkeys of a zoom have the same length, so their text order is the Morton order of the squares.

create or replace function gridtiles.quadkey(z int, x int, y int) returns text
language sql immutable strict parallel safe as $$
    select coalesce(string_agg((((x >> i) & 1) + 2 * ((y >> i) & 1))::text, '' order by i desc), '')
    from generate_series(0, z - 1) i
$$;
