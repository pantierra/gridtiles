from gridtiles.backends.base import parse_ref


def test_parse_ref() -> None:
    assert parse_ref("pop") == ("pop", None)
    assert str(parse_ref("pop@2026-10-01")[1]) == "2026-10-01"
