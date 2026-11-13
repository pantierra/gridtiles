import psycopg
import pytest

from gridtiles.cli import _Refreshed


def test_refreshed(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [0.0]
    monkeypatch.setattr("gridtiles.cli.time.monotonic", lambda: clock[0])
    builds: list[int] = []
    fail = [False]

    def build() -> dict:
        if fail[0]:
            raise psycopg.OperationalError("database down")
        builds.append(1)
        return {"build": len(builds)}

    refreshed = _Refreshed(build, 300)
    assert refreshed() == {"build": 1}
    clock[0] = 299
    assert refreshed() == {"build": 1}  # still fresh
    clock[0], fail[0] = 301, True
    assert refreshed() == {"build": 1}  # the last result while a rebuild fails
    fail[0] = False
    assert refreshed() == {"build": 2}
    with pytest.raises(psycopg.OperationalError):
        fail[0] = True
        _Refreshed(build, 300)()  # nothing to fall back to
