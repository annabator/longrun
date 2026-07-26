"""SQLite schema definition and idempotent initialisation.

The schema is intentionally plain stdlib `sqlite3`: the data volumes here are
small, the queries are simple, and a single file is trivial to back up.

Every statement is `IF NOT EXISTS`, so :func:`init_db` can be called on every
command without guarding it. Dates and timestamps are stored as ISO-8601 text
(timestamps always timezone-aware, UTC-normalised) rather than relying on the
sqlite3 adapters deprecated in Python 3.12.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from longrun.store.migrations import apply_migrations, current_version

SCHEMA_VERSION = 3
"""Bumped whenever a migration becomes necessary; recorded in `schema_meta`.

Version 2 retargeted ingestion from Strava to Intervals.icu: activities are
keyed on `(source, external_id)` rather than a Strava id, and carry the
subjective and load fields Intervals.icu computes. Version 3 dropped the
rolling CTL, ATL and form columns in favour of the measured series. See
:mod:`longrun.store.migrations`.
"""

_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS schema_meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    # Identity is (source, external_id): no single provider owns the key, so
    # the same session pulled from Intervals.icu and from a FIT file stays two
    # rows rather than silently overwriting one another.
    """
    CREATE TABLE IF NOT EXISTS activities (
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
    """,
    "CREATE INDEX IF NOT EXISTS idx_activities_start_time ON activities (start_time)",
    """
    CREATE TABLE IF NOT EXISTS activity_streams (
        activity_id INTEGER NOT NULL REFERENCES activities (id) ON DELETE CASCADE,
        t_s         INTEGER NOT NULL,
        hr          REAL,
        velocity    REAL,
        distance_m  REAL,
        altitude    REAL,
        cadence     REAL,
        rr_ms       REAL,
        PRIMARY KEY (activity_id, t_s)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS wellness_daily (
        date            TEXT PRIMARY KEY,
        resting_hr      REAL,
        avg_sleeping_hr REAL,
        hrv_rmssd       REAL,
        sleep_score     REAL,
        sleep_hours     REAL,
        spo2            REAL,
        weight_kg       REAL,
        source          TEXT NOT NULL DEFAULT 'manual',
        notes           TEXT
    )
    """,
    # Long format: one row per (date, metric, source). Holds VO2max, parkrun
    # 5k times, blood results, body composition and anything added in future.
    """
    CREATE TABLE IF NOT EXISTS tests (
        id     INTEGER PRIMARY KEY,
        date   TEXT NOT NULL,
        metric TEXT NOT NULL,
        value  REAL NOT NULL,
        unit   TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT 'manual',
        notes  TEXT,
        UNIQUE (date, metric, source)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tests_metric_date ON tests (metric, date)",
)


def connect(path: Path) -> sqlite3.Connection:
    """Open a connection to the SQLite database at `path`.

    Creates the parent directory if needed, enables foreign-key enforcement
    (off by default in SQLite) and returns rows as :class:`sqlite3.Row`.

    Args:
        path: Location of the database file.

    Returns:
        An open connection.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create or migrate the schema, then record the version in force.

    Safe to call repeatedly, and safe to call on a database written by an
    earlier version: tables are created only when absent, and an older
    database is migrated forward with its rows intact.

    Args:
        conn: An open connection.
    """
    existing = current_version(conn)
    with conn:
        for statement in _DDL:
            conn.execute(statement)

    # Migrations run outside the DDL transaction: the activities rebuild
    # toggles PRAGMA foreign_keys, which cannot change inside one.
    if 0 < existing < SCHEMA_VERSION:
        apply_migrations(conn, SCHEMA_VERSION)

    with conn:
        conn.execute(
            """
            INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?)
            ON CONFLICT (key) DO UPDATE SET value = excluded.value
            """,
            (str(SCHEMA_VERSION),),
        )


@contextmanager
def open_db(path: Path) -> Iterator[sqlite3.Connection]:
    """Open an initialised database as a context manager.

    Args:
        path: Location of the database file.

    Yields:
        A connection with the schema already applied.
    """
    conn = connect(path)
    try:
        init_db(conn)
        yield conn
    finally:
        conn.close()
