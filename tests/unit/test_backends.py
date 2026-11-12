import pytest

from gridtiles.backends.base import kind_of, parse_ref


@pytest.mark.parametrize(
    ("url", "kind"),
    [
        ("postgresql://u:p@localhost:5439/db", "postgres"),
        ("postgres:///db", "postgres"),
        ("host=localhost dbname=db", "postgres"),
        ("cells.duckdb", "duckdb"),
        ("data/year=2026/cells.duckdb", "duckdb"),
    ],
)
def test_kind_of(url: str, kind: str) -> None:
    assert kind_of(url) == kind


def test_parse_ref() -> None:
    assert parse_ref("pop") == ("pop", None)
    assert str(parse_ref("pop@2026-10-01")[1]) == "2026-10-01"
