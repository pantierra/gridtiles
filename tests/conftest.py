"""Shared fixtures: a fresh `gridtiles_test` database with `sql/*.sql` applied."""

import os
from collections.abc import Iterator
from pathlib import Path

import psycopg
import psycopg.conninfo
import pytest

SQL_DIR = Path(__file__).parent.parent / "sql"
FIXTURES = Path(__file__).parent / "fixtures"
TEST_DB = "gridtiles_test"
ADMIN_DSN = os.environ.get("GRIDTILES_TEST_DSN", "postgresql://postgres:postgres@localhost:5439/postgres")


def apply_sql(conn: psycopg.Connection) -> None:
    for path in sorted(SQL_DIR.glob("*.sql")):
        conn.execute(path.read_text())


@pytest.fixture(scope="session")
def db_conn() -> Iterator[psycopg.Connection]:
    """Recreate the test database, apply the SQL files and yield a connection to it."""
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute(f"drop database if exists {TEST_DB} with (force)")
        admin.execute(f"create database {TEST_DB}")
    dsn = psycopg.conninfo.make_conninfo(ADMIN_DSN, dbname=TEST_DB)
    with psycopg.connect(dsn, autocommit=True) as conn:
        apply_sql(conn)
        yield conn


@pytest.fixture
def db(db_conn: psycopg.Connection) -> Iterator[psycopg.Connection]:
    """A connection inside a transaction that is rolled back after the test."""
    with db_conn.transaction(force_rollback=True):
        yield db_conn
