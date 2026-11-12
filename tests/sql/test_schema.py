import psycopg

import gridtiles
from gridtiles.backends.postgres import install


def test_postgis_version(db: psycopg.Connection) -> None:
    (version,) = db.execute("select PostGIS_Lib_Version()").fetchone()
    assert version.startswith("3.4")


def test_gridtiles_version_matches_package(db: psycopg.Connection) -> None:
    (version,) = db.execute("select gridtiles.version()").fetchone()
    assert version == gridtiles.__version__


def test_install_is_idempotent(db: psycopg.Connection) -> None:
    assert install(db) == gridtiles.__version__
    assert install(db) == gridtiles.__version__
    assert db.execute("select value from gridtiles.meta where key = 'schema_version'").fetchone() == (gridtiles.__version__,)
