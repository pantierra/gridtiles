"""Fixtures giving the backend operations, on every backend."""

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from gridtiles.backends.base import Backend
from gridtiles.backends.postgres import PostgresBackend

from ..conftest import FIXTURES, fresh_database

BACKENDS = ["postgres"]


def open_fresh(kind: str, tmp: Path, name: str) -> Backend:
    """A new, committed store with the schema installed."""
    backend: Backend = PostgresBackend(fresh_database(name))
    backend.install()
    return backend


@pytest.fixture(params=BACKENDS)
def backend(request: pytest.FixtureRequest, db: psycopg.Connection, tmp_path: Path) -> Iterator[Backend]:
    """Each backend in isolation: PostgreSQL inside the rolled-back test transaction."""
    with PostgresBackend(db) as backend:
        yield backend


def rows(backend: Backend, query: str) -> list[tuple]:
    """Run SQL on the backend's store. Tests only: this reaches past the
    protocol to the backend's connection to inspect cell tables directly."""
    return backend.conn.execute(query).fetchall()  # type: ignore[attr-defined]


__all__ = ["BACKENDS", "FIXTURES", "open_fresh", "rows"]
