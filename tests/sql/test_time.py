"""Time parameters: a snapshot or range is a filter on the source's time column, a diff two
aggregations subtracted per square, inside one tile query.
"""

import mercantile
import psycopg
import pytest
from psycopg.types.json import Jsonb

from .helpers import SPEC, SPEC_MINMAX, create, features, source, tile

JAN, FEB = "2024-01-01", "2024-02-01"
SNAP_SPEC = {**SPEC_MINMAX, "name": "snap", "source": {"table": "snap", "time": "day"}}
T8 = mercantile.tile(13.4, 52.5, 8)


@pytest.fixture
def snap(db: psycopg.Connection) -> psycopg.Connection:
    """Two snapshots of the 10k points: in February, every third point is gone and the rest doubled."""
    source(db, "points_10k")
    db.execute(
        f"create table snap as select geom, pop, osm_ratio, area, date '{JAN}' as day from pop_points"
        f" union all select geom, pop * 2, osm_ratio, area, date '{FEB}' from pop_points where pop % 3 <> 0"
    )
    db.execute("create index on snap using gist (geom)")
    db.execute("create index on snap (day)")
    db.execute(f"create table jan as select * from snap where day = '{JAN}'")
    db.execute(f"create table feb as select * from snap where day = '{FEB}'")
    create(db, SNAP_SPEC)
    for name in ("jan", "feb"):
        create(db, {**SPEC_MINMAX, "name": name, "source": {"table": name}})
    return db


def by_qk(data: bytes) -> dict[str, dict]:
    return {f["properties"]["qk"]: f["properties"] for f in features(data)}


@pytest.mark.parametrize("z", [4, 8, 12])
def test_snapshot_equals_a_table_of_it(snap: psycopg.Connection, z: int) -> None:
    t = mercantile.tile(13.4, 52.5, z)
    assert tile(snap, z, t.x, t.y, "snap", {"at": JAN}) == tile(snap, z, t.x, t.y, "jan")
    assert tile(snap, z, t.x, t.y, "snap", {"at": FEB}) == tile(snap, z, t.x, t.y, "feb")
    assert tile(snap, z, t.x, t.y, "snap", {"start": FEB}) == tile(snap, z, t.x, t.y, "feb")
    assert tile(snap, z, t.x, t.y, "snap", {"end": FEB}) == tile(snap, z, t.x, t.y, "jan")
    assert tile(snap, z, t.x, t.y, "snap", {"start": JAN, "end": "2024-01-02"}) == tile(snap, z, t.x, t.y, "jan")


def test_no_parameter_aggregates_all_rows(snap: psycopg.Connection) -> None:
    total = sum(p["n"] for p in by_qk(tile(snap, 8, T8.x, T8.y, "snap")).values())
    parts = [sum(p["n"] for p in by_qk(tile(snap, 8, T8.x, T8.y, name)).values()) for name in ("jan", "feb")]
    assert total == sum(parts)


def test_diff_subtracts_per_square(snap: psycopg.Connection) -> None:
    diff = by_qk(tile(snap, 8, T8.x, T8.y, "snap", {"at": FEB, "diff": JAN}))
    jan, feb = by_qk(tile(snap, 8, T8.x, T8.y, "jan")), by_qk(tile(snap, 8, T8.x, T8.y, "feb"))
    assert diff.keys() == jan.keys() | feb.keys()
    for qk, d in diff.items():
        a, b = jan.get(qk), feb.get(qk)
        assert d["n"] == (b["n"] if b else 0) - (a["n"] if a else 0)
        assert d["population"] == (b["population"] if b else 0) - (a["population"] if a else 0)
        for value in ("completeness", "pop_min", "pop_max"):
            if a and b and value in a and value in b:
                assert d[value] == b[value] - a[value]
            else:
                assert value not in d


def test_diff_with_itself_is_zero(snap: psycopg.Connection) -> None:
    for p in by_qk(tile(snap, 8, T8.x, T8.y, "snap", {"at": JAN, "diff": JAN})).values():
        assert (p["n"], p["population"], p["completeness"]) == (0, 0, 0)


def test_stats_and_tiles_follow_parameters(snap: psycopg.Connection) -> None:
    (at,) = snap.execute("select gridtiles.stats('snap', %s)", (Jsonb({"at": JAN}),)).fetchone()
    (jan,) = snap.execute("select gridtiles.stats('jan')").fetchone()
    assert at == jan
    tiles = snap.execute("select * from gridtiles.tiles_with_data('snap', %s)", (Jsonb({"at": FEB}),)).fetchall()
    assert tiles == snap.execute("select * from gridtiles.tiles_with_data('feb')").fetchall()


def test_timestamp_column(db: psycopg.Connection) -> None:
    source(db, "points_small")
    db.execute("alter table pop_points add column seen timestamptz")
    db.execute(
        "update pop_points set seen = case when pop <= 40"
        " then timestamptz '2024-01-01 08:00Z' else timestamptz '2024-01-02 08:00Z' end"
    )
    create(db, {**SPEC, "source": {"table": "pop_points", "time": "seen"}})
    t = mercantile.tile(13.405, 52.52, 12)
    early = by_qk(tile(db, 12, t.x, t.y, params={"end": "2024-01-02T00:00:00Z"}))
    assert sum(p["n"] for p in early.values()) == 4
    snapshot = tile(db, 12, t.x, t.y, params={"at": "2024-01-01 08:00+00"})
    assert snapshot == tile(db, 12, t.x, t.y, params={"end": "2024-01-02"})


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"at": "2024-13-01"}, "parameter at: '2024-13-01' is not a valid date"),
        ({"at": "yesterday'; drop table snap; --"}, "is not a valid date"),
        ({"at": 20240101}, "parameter at: expected a string, got 20240101"),
        ({"at": JAN, "start": JAN}, "parameter at excludes start and end"),
        ({"diff": JAN}, "parameter diff needs at"),
        ({"end": ""}, "parameter end: '' is not a valid date"),
    ],
)
def test_malformed_parameters(snap: psycopg.Connection, params: dict, message: str) -> None:
    with pytest.raises(psycopg.errors.InvalidParameterValue, match=message), snap.transaction():
        tile(snap, 8, T8.x, T8.y, "snap", params)
    notices: list[str] = []
    snap.add_notice_handler(lambda d: notices.append(d.message_primary or ""))
    (data,) = snap.execute("select gridtiles.tile_snap(8, %s, %s, %s::json)", (T8.x, T8.y, Jsonb(params))).fetchone()
    assert bytes(data) == b""
    assert len(notices) == 1 and message in notices[0]
    assert snap.execute("select count(*) > 0 from snap").fetchone() == (True,)


def test_time_parameter_without_time_column(snap: psycopg.Connection) -> None:
    with pytest.raises(psycopg.errors.InvalidParameterValue, match="layer jan has no time column"):
        tile(snap, 8, T8.x, T8.y, "jan", {"at": JAN})


@pytest.mark.parametrize(
    ("column", "error"),
    [("missing", psycopg.errors.UndefinedColumn), ("pop", psycopg.errors.DatatypeMismatch)],
)
def test_bad_time_column(snap: psycopg.Connection, column: str, error: type) -> None:
    with pytest.raises(error):
        create(snap, {**SNAP_SPEC, "name": "bad", "source": {"table": "snap", "time": column}})


def test_snapshot_query_uses_the_time_index(snap: psycopg.Connection) -> None:
    snap.execute("set local enable_seqscan = off")
    (query,) = snap.execute("select gridtiles._mvt_query('snap', 0, 0, 0, %s)", (Jsonb({"at": JAN}),)).fetchone()
    plan = "\n".join(r[0] for r in snap.execute("explain " + query).fetchall())
    assert "snap_day_idx" in plan


def test_polygon_diff(db: psycopg.Connection) -> None:
    source(db, "polygons_500", "polys")
    db.execute(
        f"create table snap as select geom, pop, osm_ratio, area, date '{JAN}' as day from polys"
        f" union all select geom, pop + 1, osm_ratio, area, date '{FEB}' from polys where pop % 2 = 0"
    )
    db.execute("create index on snap using gist (geom)")
    create(db, SNAP_SPEC)
    diff = by_qk(tile(db, 8, T8.x, T8.y, "snap", {"at": FEB, "diff": JAN}))
    jan = by_qk(tile(db, 8, T8.x, T8.y, "snap", {"at": JAN}))
    feb = by_qk(tile(db, 8, T8.x, T8.y, "snap", {"at": FEB}))
    assert diff.keys() == jan.keys() | feb.keys()
    for qk, d in diff.items():
        a, b = jan.get(qk, {"n": 0, "population": 0}), feb.get(qk, {"n": 0, "population": 0})
        assert d["n"] == b["n"] - a["n"]
        assert d["population"] == b["population"] - a["population"]
