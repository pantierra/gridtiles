import psycopg

import gridtiles

from ..conftest import apply_sql


def test_postgis_version(db: psycopg.Connection) -> None:
    (version,) = db.execute("select PostGIS_Lib_Version()").fetchone()
    assert version.startswith("3.4")


def test_gridtiles_version_matches_package(db: psycopg.Connection) -> None:
    (version,) = db.execute("select gridtiles.version()").fetchone()
    assert version == gridtiles.__version__


def test_sql_files_are_idempotent(db: psycopg.Connection) -> None:
    apply_sql(db)
    apply_sql(db)
