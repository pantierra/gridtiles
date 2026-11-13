"""Shared fixtures: a fresh `gridtiles_test` database with the gridtiles schema installed."""

import os
from collections.abc import Iterator
from pathlib import Path

import psycopg
import psycopg.conninfo
import pytest

from gridtiles.db import install

FIXTURES = Path(__file__).parent / "fixtures"
POINT_SQUARE = Path(__file__).parent / "point_square.sql"  # test-only reference of the point assignment
TEST_DB = "gridtiles_test"
ADMIN_DSN = os.environ.get("GRIDTILES_TEST_DSN", "postgresql://postgres:postgres@localhost:5439/postgres")


@pytest.fixture(scope="session")
def db_conn() -> Iterator[psycopg.Connection]:
    """Recreate the test database, install the schema and yield a connection to it."""
    with psycopg.connect(fresh_database(TEST_DB), autocommit=True) as conn:
        install(conn)
        conn.execute(POINT_SQUARE.read_text())
        yield conn


@pytest.fixture
def db(db_conn: psycopg.Connection) -> Iterator[psycopg.Connection]:
    """A connection inside a transaction that is rolled back after the test."""
    with db_conn.transaction(force_rollback=True):
        yield db_conn


def fresh_database(name: str) -> str:
    """Drop and recreate a database for tests that commit (CLI, end-to-end); returns its URL."""
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute(f"drop database if exists {name} with (force)")
        admin.execute(f"create database {name}")
    return psycopg.conninfo.make_conninfo(ADMIN_DSN, dbname=name)
