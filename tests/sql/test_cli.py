import json
from pathlib import Path

import psycopg
import pytest
from typer.testing import CliRunner

from gridtiles.cli import app

from ..conftest import FIXTURES, fresh_database

runner = CliRunner()


@pytest.fixture(scope="module")
def url() -> str:
    url = fresh_database("gridtiles_cli")
    assert run("init", "--db", url).exit_code == 0
    assert run("import", FIXTURES / "points_10k.parquet", "--table", "pop_points", "--db", url).exit_code == 0
    return url


def run(*args: str | Path):
    return runner.invoke(app, [str(a) for a in args])


def test_help_lists_commands() -> None:
    result = run("--help")
    assert result.exit_code == 0
    for command in ("init", "import", "add", "drop", "layers", "cache", "serve", "export"):
        assert command in result.output
    assert "load" not in result.output


def test_init_again(url: str) -> None:
    assert run("init", "--db", url).exit_code == 0


def test_import_report(url: str) -> None:
    result = run("import", FIXTURES / "points_small.csv", "--table", "small", "--db", url)
    assert result.exit_code == 0, result.output
    assert "ImportReport(records_read=8, records_imported=8" in result.output
    again = run("import", FIXTURES / "points_small.csv", "--table", "small", "--db", url)
    assert again.exit_code == 2 and "exists" in again.output
    assert run("import", FIXTURES / "points_small.csv", "--table", "small", "--replace", "--db", url).exit_code == 0


def test_add_list_drop(url: str) -> None:
    result = run("add", "pop", "--spec", FIXTURES / "spec_pop.json", "--db", url)
    assert result.exit_code == 0, result.output
    listing = run("layers", "--db", url)
    assert listing.exit_code == 0
    assert listing.output == "pop\tpop_points\tz2-16\tdetail 4\ttiles z0-12\n"
    assert run("add", "pop", "--spec", FIXTURES / "spec_pop.json", "--db", url).exit_code == 2
    assert run("drop", "pop", "--db", url).exit_code == 0
    assert run("drop", "pop", "--db", url).exit_code == 2
    with psycopg.connect(url) as conn:
        assert conn.execute("select count(*) from pop_points").fetchone() == (9800,)


def test_add_from_toml(url: str, tmp_path: Path) -> None:
    config = tmp_path / "gridtiles.toml"
    config.write_text(
        f'[backend]\nurl = "{url}"\n\n[source]\ntable = "pop_points"\n\n[grid]\nbase_zoom = 14\nmin_zoom = 4\n\n'
        '[[value]]\nname = "population"\nfrom = "pop"\nagg = "sum"\n'
    )
    result = run("add", "small", "--config", config)
    assert result.exit_code == 0, result.output
    assert "Added layer small over pop_points" in result.output


@pytest.mark.parametrize("path", sorted((FIXTURES / "invalid_specs").glob("*.json")), ids=lambda p: p.stem)
def test_invalid_spec_exits_2(url: str, path: Path) -> None:
    name = json.loads(path.read_text())["name"]
    layer = name if name != "pop" else f"bad_{path.stem}"  # the argument sets the name: carry an invalid one over
    result = run("add", layer, "--spec", path, "--db", url)
    assert result.exit_code == 2
    assert "Invalid layer spec" in result.output


@pytest.mark.parametrize("content", [None, "{not json"], ids=["missing", "malformed"])
def test_unreadable_spec_exits_2(url: str, tmp_path: Path, content: str | None) -> None:
    path = tmp_path / "spec.json"
    if content is not None:
        path.write_text(content)
    result = run("add", "unread", "--spec", path, "--db", url)
    assert result.exit_code == 2
    assert "Cannot read layer spec" in result.output


def test_cache_and_export(url: str, tmp_path: Path) -> None:
    assert run("add", "cached", "--spec", FIXTURES / "spec_pop.json", "--db", url).exit_code == 0
    result = run("cache", "cached", "--max-zoom", "6", "--db", url)
    assert result.exit_code == 0 and result.output.startswith("Cached ")
    assert run("cache", "cached", "--drop", "--db", url).output == "Dropped the cache of cached\n"
    assert run("cache", "cached", "--db", url).exit_code == 2
    assert run("cache", "missing", "--drop", "--db", url).exit_code == 2
    result = run("export", "--layer", "cached", "-o", tmp_path / "cached.pmtiles", "--db", url)
    assert result.exit_code == 0, result.output
    assert (tmp_path / "cached.pmtiles").exists()
    assert run("export", "--layer", "cached", "-o", tmp_path / "x.pmtiles", "--at", "2024-01-01", "--db", url).exit_code == 2


def test_bad_source_exits_2(url: str, tmp_path: Path) -> None:
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({**json.loads((FIXTURES / "spec_pop.json").read_text()), "source": {"table": "missing"}}))
    result = run("add", "nosource", "--spec", spec, "--db", url)
    assert result.exit_code == 2
    assert 'source table "missing" does not exist' in result.output


def test_unsupported_geometry_exits_2(url: str, tmp_path: Path) -> None:
    path = tmp_path / "line.geojson"
    line = {"type": "Feature", "properties": {}, "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]}}
    path.write_text(json.dumps({"type": "FeatureCollection", "features": [line]}))
    result = run("import", path, "--table", "lines", "--db", url)
    assert result.exit_code == 2
    assert "only points or only polygons are supported, got ST_LineString" in result.output


def test_missing_database_exits_2(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("GRIDTILES_DB", raising=False)
    monkeypatch.chdir(tmp_path)
    result = run("layers")
    assert result.exit_code == 2
    assert "No database" in result.output
