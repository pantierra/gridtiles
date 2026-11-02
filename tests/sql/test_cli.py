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


def test_append_and_replace(url: str) -> None:
    spec = FIXTURES / "spec_pop.json"
    assert run("load", FIXTURES / "points_small.csv", "--layer", "inc", "--spec", spec, "--db", url).exit_code == 0
    result = run("load", FIXTURES / "points_small.csv", "--layer", "inc", "--append", "--refresh-stats", "--db", url)
    assert result.exit_code == 0, result.output
    assert run("load", FIXTURES / "points_small.csv", "--layer", "inc", "--replace-cells", "--db", url).exit_code == 0
    both = run("load", FIXTURES / "points_small.csv", "--layer", "inc", "--append", "--replace-cells", "--db", url)
    assert both.exit_code == 2


def test_snapshots_and_diff(url: str, tmp_path: Path) -> None:
    spec = tmp_path / "spec_ts.json"
    spec.write_text(json.dumps({**json.loads((FIXTURES / "spec_pop.json").read_text()), "snapshots": True}))
    for path, day in (("points_10k.parquet", "2026-10-01"), ("points_10k_v2.parquet", "2026-11-01")):
        result = run("load", FIXTURES / path, "--layer", "ts", "--spec", spec, "--snapshot", day, "--db", url)
        assert result.exit_code == 0, result.output
    assert run("load", FIXTURES / "points_small.csv", "--layer", "ts", "--db", url).exit_code == 2  # snapshot missing
    result = run("diff", "ts@2026-10-01", "ts@2026-11-01", "--as", "ts_delta", "--db", url)
    assert result.exit_code == 0, result.output
    assert "ts_delta" in run("layers", "--db", url).output
    assert run("diff", "ts@2026-10-01", "missing", "--as", "nope", "--db", url).exit_code == 2


def test_duckdb_refuses_diff(tmp_path: Path) -> None:
    db = str(tmp_path / "cells.duckdb")
    run("init", "--db", db)
    result = run("diff", "a", "b", "--as", "c", "--db", db)
    assert result.exit_code == 2 and "snapshots" in result.output


def test_failed_first_load_leaves_no_layer(url: str, tmp_path: Path) -> None:
    path = tmp_path / "line.geojson"
    line = {"type": "LineString", "coordinates": [[13.4, 52.5], [13.5, 52.6]]}
    feature = {"type": "Feature", "properties": {"pop": 1, "osm_ratio": 0.5, "area": 1}, "geometry": line}
    path.write_text(json.dumps({"type": "FeatureCollection", "features": [feature]}))
    result = run("load", path, "--layer", "lines", "--spec", FIXTURES / "spec_pop.json", "--db", url)
    assert result.exit_code == 2
    assert "lines" not in run("layers", "--db", url).output
