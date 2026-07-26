"""Versioned, idempotent schema migrations.

Each migration takes the database from one `SCHEMA_VERSION` to the next and
must be safe to attempt against a database that has already had it applied -
every step checks the live column list rather than trusting the recorded
version, so a database interrupted mid-migration recovers on the next run.

Existing rows are always preserved. Where a migration has to rebuild a table,
it follows SQLite's documented procedure: foreign keys off, rebuild inside a
transaction, integrity check, foreign keys back on. Primary keys are carried
across unchanged so that dependent rows keep pointing at the right parent.
"""

import sqlite3
from collections.abc import Callable

# v1 -> v2 column additions, applied with ALTER TABLE ... ADD COLUMN.
_V2_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("activity_streams", "rr_ms", "REAL"),
    ("wellness_daily", "avg_sleeping_hr", "REAL"),
    ("wellness_daily", "spo2", "REAL"),
    ("wellness_daily", "ctl", "REAL"),
    ("wellness_daily", "atl", "REAL"),
    ("wellness_daily", "form", "REAL"),
)

_V2_ACTIVITIES_DDL = """
CREATE TABLE activities_v2 (
    id                   INTEGER PRIMARY KEY,
    source               TEXT    NOT NULL DEFAULT 'intervals',
    external_id          TEXT    NOT NULL,
    start_time           TEXT    NOT NULL,
    sport_type           TEXT    NOT NULL,
    name                 TEXT    NOT NULL,
    distance_m           REAL    NOT NULL,
    moving_time_s        INTEGER NOT NULL,
    elapsed_time_s       INTEGER NOT NULL,
    avg_hr               REAL,
    max_hr               REAL,
    avg_speed            REAL,
    total_elevation_gain REAL,
    avg_cadence          REAL,
    feel                 REAL,
    rpe                  REAL,
    icu_load             REAL,
    icu_intensity        REAL,
    UNIQUE (source, external_id)
)
"""

# The Strava id becomes the external id, as text, keeping its own source label
# so a later Intervals ingest of the same session does not collide with it.
_V2_ACTIVITIES_COPY = """
INSERT INTO activities_v2 (
    id, source, external_id, start_time, sport_type, name, distance_m,
    moving_time_s, elapsed_time_s, avg_hr, max_hr, avg_speed,
    total_elevation_gain, avg_cadence
)
SELECT
    id, COALESCE(source, 'strava'), CAST(strava_id AS TEXT), start_time,
    sport_type, name, distance_m, moving_time_s, elapsed_time_s, avg_hr,
    max_hr, avg_speed, total_elevation_gain, avg_cadence
FROM activities
"""


def columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Return the column names of `table`, empty if it does not exist.

    Args:
        conn: An open connection.
        table: Table name to inspect.

    Returns:
        The set of column names.
    """
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row["name"]) for row in rows}


def current_version(conn: sqlite3.Connection) -> int:
    """Return the schema version recorded in the database.

    Args:
        conn: An open connection.

    Returns:
        The recorded version, or 0 for a database that has never been
        initialised.
    """
    tables = {
        str(row["name"])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    if "schema_meta" not in tables:
        return 0 if "activities" not in tables else 1
    row = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
    if row is None:
        return 1
    try:
        return int(row["value"])
    except (TypeError, ValueError):
        return 1


def migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """Retarget the schema from Strava-keyed to source-keyed activities.

    Rebuilds `activities` so identity is `(source, external_id)` instead of
    `strava_id`, and adds the fields Intervals.icu supplies: per-activity
    `feel`, `rpe`, `icu_load` and `icu_intensity`; `rr_ms` on streams; and
    sleeping heart rate, SpO2, CTL, ATL and form on wellness. The `hrv` column
    becomes `hrv_rmssd`, since v1.1 readiness needs rMSSD specifically.

    Args:
        conn: An open connection.
    """
    _add_missing_columns(conn)
    _rename_hrv_column(conn)
    _rebuild_activities(conn)


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """Add each new nullable column that is not already present."""
    for table, column, column_type in _V2_ADDED_COLUMNS:
        existing = columns(conn, table)
        if not existing or column in existing:
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")


def _rename_hrv_column(conn: sqlite3.Connection) -> None:
    """Rename `wellness_daily.hrv` to `hrv_rmssd`, preserving its values."""
    existing = columns(conn, "wellness_daily")
    if "hrv" not in existing or "hrv_rmssd" in existing:
        return
    conn.execute("ALTER TABLE wellness_daily RENAME COLUMN hrv TO hrv_rmssd")


def _rebuild_activities(conn: sqlite3.Connection) -> None:
    """Rebuild `activities` with the source-keyed identity, keeping all rows."""
    existing = columns(conn, "activities")
    if not existing or "strava_id" not in existing:
        return

    # Foreign keys must be off across a table rebuild, or dropping the old
    # table would cascade into activity_streams and delete every sample.
    # The pragma cannot be changed inside a transaction.
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN")
        conn.execute(_V2_ACTIVITIES_DDL)
        conn.execute(_V2_ACTIVITIES_COPY)
        conn.execute("DROP TABLE activities")
        conn.execute("ALTER TABLE activities_v2 RENAME TO activities")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_activities_start_time ON activities (start_time)"
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        conn.execute("PRAGMA foreign_keys = ON")
    if violations:
        msg = f"Migration left {len(violations)} orphaned rows; the database was not modified."
        raise sqlite3.IntegrityError(msg)


_MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    1: migrate_v1_to_v2,
}
"""Maps a starting version to the migration that advances it by one."""


def apply_migrations(conn: sqlite3.Connection, target: int) -> int:
    """Bring the database up to `target`, one version at a time.

    Args:
        conn: An open connection.
        target: The schema version to reach.

    Returns:
        The version now in force.
    """
    version = current_version(conn)
    while version < target and version in _MIGRATIONS:
        _MIGRATIONS[version](conn)
        version += 1
    return version
