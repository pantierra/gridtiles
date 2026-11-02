import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gridtiles.cli import app

from ..conftest import FIXTURES, fresh_database

runner = CliRunner()


@pytest.fixture(scope="module")
def url() -> str:
    url = fresh_database("gridtiles_cli")
    assert run("init", "--db", url).exit_code == 0
    return url


def run(*args: str | Path):
    return runner.invoke(app, [str(a) for a in args])


def test_help_lists_commands() -> None:
    result = run("--help")
    assert result.exit_code == 0
    for command in ("init", "load", "layers", "serve"):
        assert command in result.output


def test_init_again(url: str) -> None:
    assert run("init", "--db", url).exit_code == 0


def test_load_and_list(url: str) -> None:
    result = run(
        "load", FIXTURES / "points_10k.parquet", "--layer", "pop", "--spec", FIXTURES / "spec_pop.json", "--db", url
    )
    assert result.exit_code == 0, result.output
    assert "LoadReport(records_read=10000, records_loaded=9800" in result.output
    listing = run("layers", "--db", url)
    assert listing.exit_code == 0
    assert listing.output.startswith("pop\tz2-16\tdetail 4\ttiles z0-12\t")


def test_load_from_toml(url: str, tmp_path: Path) -> None:
    config = tmp_path / "gridtiles.toml"
    config.write_text(
        f'[backend]\nurl = "{url}"\n\n[grid]\nbase_zoom = 14\nmin_zoom = 4\n\n'
        '[[value]]\nname = "population"\nfrom = "pop"\nagg = "sum"\n'
    )
    result = run("load", FIXTURES / "points_small.csv", "--layer", "small", "--config", config)
    assert result.exit_code == 0, result.output
    assert "records_loaded=8" in result.output


@pytest.mark.parametrize("path", sorted((FIXTURES / "invalid_specs").glob("*.json")), ids=lambda p: p.stem)
def test_invalid_spec_exits_2(url: str, path: Path) -> None:
    name = json.loads(path.read_text())["name"]
    layer = name if name != "pop" else f"bad_{path.stem}"  # --layer sets the name: carry an invalid one over
    result = run("load", FIXTURES / "points_small.csv", "--layer", layer, "--spec", path, "--db", url)
    assert result.exit_code == 2
    assert "Invalid layer spec" in result.output


def test_missing_database_exits_2(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("GRIDTILES_DB", raising=False)
    monkeypatch.chdir(tmp_path)
    result = run("layers")
    assert result.exit_code == 2
    assert "No database" in result.output


def test_duckdb_cannot_serve_live(tmp_path: Path) -> None:
    db = str(tmp_path / "cells.duckdb")
    assert run("init", "--db", db).exit_code == 0
    spec = FIXTURES / "spec_pop.json"
    assert run("load", FIXTURES / "points_small.csv", "--layer", "pop", "--spec", spec, "--db", db).exit_code == 0
    result = run("serve", "--live", "--db", db)
    assert result.exit_code == 2
    assert "'live'" in result.output and "--embedded" in result.output
