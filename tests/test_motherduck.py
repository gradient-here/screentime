from __future__ import annotations

from datetime import date

import duckdb
import pytest

from screentime import motherduck
from screentime.db import connect, init_db
from screentime.exporter import export_all
from screentime.importer import sync_sources
from screentime.motherduck import export_motherduck
from test_pipeline import BERLIN, MAC, FakeSource, event, make_settings


@pytest.fixture
def conn(tmp_path):
    connection = connect(tmp_path / "screentime.db")
    init_db(connection)
    yield connection
    connection.close()


@pytest.fixture
def duck(tmp_path, monkeypatch):
    """A local DuckDB file stands in for MotherDuck."""
    path = str(tmp_path / "remote.duckdb")
    monkeypatch.setattr(motherduck, "connect", lambda settings: duckdb.connect(path))

    def query(sql: str) -> list[tuple]:
        with duckdb.connect(path, read_only=True) as con:
            return con.execute(sql).fetchall()

    return query


def md_settings(tmp_path, **overrides):
    return make_settings(
        tmp_path,
        motherduck_database="screentime",
        motherduck_token="secret",
        **overrides,
    )


def test_export_mirrors_tables_and_is_incremental(conn, tmp_path, duck):
    source = FakeSource(
        [
            event("2026-07-25T10:00:00+00:00", 60),
            event("2026-07-24T21:30:00+00:00", 3600, "com.burbn.instagram"),
            event("2026-07-25T11:00:00+00:00", 600, device=MAC),
        ]
    )
    sync_sources(conn, [source], BERLIN, device_names={"mac:1": "Work Mac"})
    settings = md_settings(tmp_path)

    assert export_motherduck(conn, settings) > 0
    # 21:30 UTC is 23:30 in Berlin: the session belongs to the 24th.
    assert duck("SELECT device_id, day FROM sessions ORDER BY started_at") == [
        ("phone-1", date(2026, 7, 24)),
        ("phone-1", date(2026, 7, 25)),
        ("mac:1", date(2026, 7, 25)),
    ]
    assert duck("SELECT count(*) FROM daily_device_stats") == [(3,)]
    assert ("mac:1", "Work Mac") in duck("SELECT device_id, name FROM devices")

    assert export_motherduck(conn, settings) == 0  # nothing changed

    source.events = [event("2026-07-25T12:00:00+00:00", 60, device=MAC)]
    sync_sources(conn, [source], BERLIN)
    export_motherduck(conn, settings)
    # The rebuilt day is replaced, not duplicated; the 24th is untouched.
    assert duck("SELECT count(*) FROM sessions") == [(4,)]
    assert duck("SELECT count(*) FROM daily_device_stats") == [(3,)]
    assert duck(
        "SELECT duration_s FROM daily_device_stats WHERE device_id='mac:1'"
    ) == [(660.0,)]


def test_backfill_leaves_cursor_alone(conn, tmp_path, duck):
    sync_sources(
        conn, [FakeSource([event("2026-07-20T10:00:00+00:00"), event()])], BERLIN
    )
    export_motherduck(conn, md_settings(tmp_path), from_date=date(2026, 7, 25))
    assert duck("SELECT DISTINCT day FROM sessions") == [(date(2026, 7, 25),)]
    assert conn.execute("SELECT COUNT(*) FROM influx_sync_cursors").fetchone()[0] == 0


def test_failure_is_reported_and_cursor_kept(conn, tmp_path, monkeypatch):
    sync_sources(conn, [FakeSource([event()])], BERLIN)

    def offline(settings):
        raise RuntimeError("offline")

    monkeypatch.setattr(motherduck, "connect", offline)
    result = export_all(conn, md_settings(tmp_path))
    assert result.motherduck_status == "failed"
    assert result.errors == ["MotherDuck: offline"]
    assert conn.execute("SELECT COUNT(*) FROM influx_sync_cursors").fetchone()[0] == 0


def test_needs_database_and_token(conn, tmp_path):
    sync_sources(conn, [FakeSource([event()])], BERLIN)
    settings = make_settings(tmp_path, motherduck_token="secret")
    assert export_motherduck(conn, settings) == 0
