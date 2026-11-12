-- pg_gridtiles: schema and version. Every file in sql/ must be idempotent.

create extension if not exists postgis;

create schema if not exists gridtiles;

create or replace function gridtiles.version() returns text
language sql immutable parallel safe
as $$ select '0.0.0' $$; -- x-release-please-version

create table if not exists gridtiles.meta (key text primary key, value text not null);
