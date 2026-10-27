"""MotherDuck (hosted DuckDB) export.

Mirrors the local tables so the data can be queried with SQL from anywhere:

* ``devices``               every known device; replaced on each export.
* ``sessions``              one row per session (``screen_events``), with the
                            local ``day`` it starts on.
* ``daily_device_stats``    per device and local day.
* ``daily_app_stats``       per device, local day and app.
* ``daily_category_stats``  per device, local day and category.

Like the InfluxDB export, the unit is a (device, local day): its rows are
deleted and rewritten in one transaction, so re-exports are idempotent. Only
days whose aggregates changed since the last export are sent.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
import sqlite3
from typing import Any

from .config import Settings, integration_complete
from .db import now_utc_iso, parse_timestamp, set_state, transaction
from .influx import _midnight, touched_days

DESTINATION = "motherduck"

# name → (columns DDL, local query); every query takes device_id, first, last.
TABLES: dict[str, tuple[str, str]] = {
    "sessions": (
        """event_key VARCHAR, device_id VARCHAR, day DATE, started_at TIMESTAMPTZ,
        duration_s DOUBLE, bundle_id VARCHAR, title VARCHAR, category VARCHAR""",
        "",  # built in Python: the local day depends on the time zone
    ),
    "daily_device_stats": (
        """day DATE, device_id VARCHAR, duration_s DOUBLE, late_duration_s DOUBLE,
        deep_night_duration_s DOUBLE, event_count BIGINT,
        first_activity_at TIMESTAMPTZ, last_activity_at TIMESTAMPTZ""",
        """SELECT day, device_id, duration_s, late_duration_s, deep_night_duration_s,
        event_count, first_activity_at, last_activity_at FROM daily_device_stats
        WHERE device_id=? AND day BETWEEN ? AND ?""",
    ),
    "daily_app_stats": (
        """day DATE, device_id VARCHAR, bundle_id VARCHAR, title VARCHAR,
        category VARCHAR, duration_s DOUBLE, event_count BIGINT""",
        """SELECT day, device_id, bundle_id, title, category, duration_s, event_count
        FROM daily_app_stats WHERE device_id=? AND day BETWEEN ? AND ?""",
    ),
    "daily_category_stats": (
        """day DATE, device_id VARCHAR, category VARCHAR, duration_s DOUBLE,
        event_count BIGINT""",
        """SELECT day, device_id, category, duration_s, event_count
        FROM daily_category_stats WHERE device_id=? AND day BETWEEN ? AND ?""",
    ),
}
DEVICES_DDL = """device_id VARCHAR, name VARCHAR, platform VARCHAR,
first_seen_at TIMESTAMPTZ, last_seen_at TIMESTAMPTZ"""


def connect(settings: Settings) -> Any:
    """Open the MotherDuck database, creating it if needed."""
    try:
        import duckdb
    except ImportError as exc:
        raise RuntimeError(
            "MotherDuck export needs the motherduck extra: uv tool install --force "
            '"screentime-exporter[mcp,motherduck] @ git+…"'
        ) from exc
    con = duckdb.connect(
        "md:", config={"motherduck_token": settings.motherduck_token or ""}
    )
    name = '"' + str(settings.motherduck_database).replace('"', '""') + '"'
    con.execute(f"CREATE DATABASE IF NOT EXISTS {name}")
    con.execute(f"USE {name}")
    return con


def _session_rows(
    conn: sqlite3.Connection, device_id: str, days: set[date], settings: Settings
) -> list[tuple[Any, ...]]:
    tz = settings.timezone
    start = _midnight(min(days), tz)
    end = _midnight(max(days) + timedelta(days=1), tz)
    rows = []
    for row in conn.execute(
        """SELECT * FROM screen_events WHERE device_id = ?
          AND julianday(started_at_utc) >= julianday(?)
          AND julianday(started_at_utc) < julianday(?)""",
        (device_id, start.isoformat(), end.isoformat()),
    ):
        started = parse_timestamp(row["started_at_utc"])
        day = started.astimezone(tz).date()
        if day in days:
            rows.append(
                (
                    row["event_key"],
                    device_id,
                    day,
                    started,
                    row["duration_s"],
                    row["bundle_id"],
                    row["title"],
                    row["category"],
                )
            )
    return rows


def export_motherduck(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    from_date: date | None = None,
) -> int:
    """Rewrite every touched (device, day) in MotherDuck; return rows written.

    ``from_date`` re-sends all days from that local date and leaves the
    incremental cursor alone.
    """
    if not integration_complete(settings, "motherduck"):
        return 0
    rows = touched_days(conn, from_date, destination=DESTINATION)
    if not rows:
        return 0
    by_device: dict[str, set[date]] = defaultdict(set)
    for row in rows:
        by_device[row["device_id"]].add(date.fromisoformat(row["day"]))

    # Collect locally first, so MotherDuck only sees one bulk insert per table.
    staged: dict[str, list[tuple[Any, ...]]] = {name: [] for name in TABLES}
    for device_id, days in by_device.items():
        params = (device_id, min(days).isoformat(), max(days).isoformat())
        staged["sessions"] += _session_rows(conn, device_id, days, settings)
        for name, (_, query) in TABLES.items():
            if query:
                staged[name] += [
                    tuple(r)
                    for r in conn.execute(query, params)
                    if date.fromisoformat(r["day"]) in days
                ]
    devices = [
        tuple(r)
        for r in conn.execute(
            "SELECT device_id, name, platform, first_seen_at, last_seen_at FROM devices"
        )
    ]
    touched = [
        (device_id, day) for device_id, days in by_device.items() for day in days
    ]

    con = connect(settings)
    try:
        for name, (ddl, _) in {**TABLES, "devices": (DEVICES_DDL, "")}.items():
            con.execute(f"CREATE TABLE IF NOT EXISTS {name} ({ddl})")
            con.execute(f"CREATE OR REPLACE TEMP TABLE stage_{name} ({ddl})")
        con.execute(
            "CREATE OR REPLACE TEMP TABLE stage_touched (device_id VARCHAR, day DATE)"
        )
        con.executemany("INSERT INTO stage_touched VALUES (?, ?)", touched)
        for name, items in {**staged, "devices": devices}.items():
            if items:
                marks = ", ".join("?" * len(items[0]))
                con.executemany(f"INSERT INTO stage_{name} VALUES ({marks})", items)
        con.execute("BEGIN")
        try:
            for name in TABLES:
                con.execute(
                    f"""DELETE FROM {name} USING stage_touched AS t
                    WHERE {name}.device_id = t.device_id AND {name}.day = t.day"""
                )
                con.execute(f"INSERT INTO {name} SELECT * FROM stage_{name}")
            con.execute("DELETE FROM devices")
            con.execute("INSERT INTO devices SELECT * FROM stage_devices")
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    finally:
        con.close()

    with transaction(conn):
        if from_date is None:
            conn.execute(
                """INSERT INTO influx_sync_cursors(destination, updated_at)
                VALUES (?, ?) ON CONFLICT(destination)
                DO UPDATE SET updated_at=excluded.updated_at""",
                (DESTINATION, max(row["updated_at"] for row in rows)),
            )
        set_state(conn, "last_motherduck_success_at", now_utc_iso())
    return sum(len(items) for items in staged.values())


def check_motherduck(settings: Settings) -> str | None:
    """None if the database can be opened, else what is wrong."""
    try:
        connect(settings).close()
    except Exception as exc:
        return f"cannot open md:{settings.motherduck_database}: {exc}"
    return None
