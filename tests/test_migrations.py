"""The v1 to v2 migration, exercised against a real v1 database with rows in it.

The v1 DDL is duplicated here on purpose. A migration test that builds its
fixture from the current schema only ever proves the code agrees with itself;
this one pins the shape that actually shipped, so the test keeps meaning
something after `schema.py` moves on.
"""

import sqlite3
from pathlib import Path

from longrun.store import SCHEMA_VERSION, Store
from longrun.store.migrations import apply_migrations, columns, current_version
from longrun.store.schema import connect

_V1_DDL = (
    "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """
    CREATE TABLE activities (
        id                   INTEGER PRIMARY KEY,
        strava_id            INTEGER NOT NULL UNIQUE,
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
        source               TEXT    NOT NULL DEFAULT 'strava'
    )
    """,
    """
    CREATE TABLE activity_streams (
        activity_id INTEGER NOT NULL REFERENCES activities (id) ON DELETE CASCADE,
        t_s         INTEGER NOT NULL,
        hr          REAL,
        velocity    REAL,
        distance_m  REAL,
        altitude    REAL,
        cadence     REAL,
        PRIMARY KEY (activity_id, t_s)
    )
    """,
    """
    CREATE TABLE wellness_daily (
        date        TEXT PRIMARY KEY,
        resting_hr  REAL,
        hrv         REAL,
        sleep_hours REAL,
        sleep_score REAL,
        weight_kg   REAL,
        source      TEXT NOT NULL DEFAULT 'manual',
        notes       TEXT
    )
    """,
    """
    CREATE TABLE tests (
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
)


def _build_v1_database(path: Path) -> None:
    """Create a v1 database holding one activity, its streams and wellness."""
    conn = connect(path)
    try:
        with conn:
            for statement in _V1_DDL:
                conn.execute(statement)
            conn.execute("INSERT INTO schema_meta (key, value) VALUES ('schema_version', '1')")
            conn.execute(
                """
                INSERT INTO activities (
                    id, strava_id, start_time, sport_type, name, distance_m,
                    moving_time_s, elapsed_time_s, avg_hr, source
                ) VALUES (7, 998877, '2025-05-02T07:15:09+00:00', 'Run',
                          'Old Strava run', 10000.0, 2700, 2800, 148.0, 'strava')
                """
            )
            conn.executemany(
                "INSERT INTO activity_streams (activity_id, t_s, hr) VALUES (7, ?, ?)",
                [(0, 120.0), (1, 124.0), (2, 129.0)],
            )
            conn.execute(
                """
                INSERT INTO wellness_daily (date, resting_hr, hrv, weight_kg, source)
                VALUES ('2025-05-02', 48.0, 61.5, 61.2, 'manual')
                """
            )
            conn.execute(
                """
                INSERT INTO tests (date, metric, value, unit, source)
                VALUES ('2025-05-02', 'vo2max', 51.0, 'ml/kg/min', 'manual')
                """
            )
    finally:
        conn.close()


def test_v1_database_is_detected_as_version_one(tmp_path: Path) -> None:
    path = tmp_path / "v1.db"
    _build_v1_database(path)

    conn = connect(path)
    try:
        assert current_version(conn) == 1
    finally:
        conn.close()


def test_migration_preserves_activities_and_maps_the_strava_id(tmp_path: Path) -> None:
    """The Strava id becomes the external id, keeping its own source label."""
    path = tmp_path / "v1.db"
    _build_v1_database(path)

    with Store.open(path) as store:
        row = store.conn.execute("SELECT * FROM activities").fetchone()

        assert row["id"] == 7
        assert row["source"] == "strava"
        assert row["external_id"] == "998877"
        assert row["name"] == "Old Strava run"
        assert row["avg_hr"] == 148.0


def test_migration_does_not_cascade_delete_streams(tmp_path: Path) -> None:
    """Rebuilding activities must not take the streams down with it."""
    path = tmp_path / "v1.db"
    _build_v1_database(path)

    with Store.open(path) as store:
        rows = store.conn.execute(
            "SELECT t_s, hr, rr_ms FROM activity_streams ORDER BY t_s"
        ).fetchall()

        assert [row["t_s"] for row in rows] == [0, 1, 2]
        assert rows[0]["hr"] == 120.0
        assert rows[0]["rr_ms"] is None


def test_migration_renames_hrv_and_keeps_its_values(tmp_path: Path) -> None:
    path = tmp_path / "v1.db"
    _build_v1_database(path)

    with Store.open(path) as store:
        row = store.conn.execute("SELECT * FROM wellness_daily").fetchone()

        assert row["hrv_rmssd"] == 61.5
        assert row["resting_hr"] == 48.0
        assert row["weight_kg"] == 61.2
        assert set(row.keys()) >= {"avg_sleeping_hr", "spo2"}
        assert "hrv" not in set(row.keys())


def test_v1_database_is_carried_all_the_way_to_the_current_version(tmp_path: Path) -> None:
    """A v1 store runs both migrations in order, not just the first."""
    path = tmp_path / "v1.db"
    _build_v1_database(path)

    with Store.open(path) as store:
        wellness = columns(store.conn, "wellness_daily")

        assert current_version(store.conn) == SCHEMA_VERSION
        # v2 added the load columns, v3 removed them again.
        assert {"ctl", "atl", "form"}.isdisjoint(wellness)
        assert "hrv_rmssd" in wellness


def test_v3_drops_the_rolling_load_columns_and_keeps_the_rest(tmp_path: Path) -> None:
    """Dropping CTL and ATL must not disturb the measured series beside them."""
    path = tmp_path / "v2.db"
    _build_v1_database(path)

    # Bring the file to v2 only, then confirm what v3 does to it.
    conn = connect(path)
    try:
        apply_migrations(conn, 2)
        conn.execute("UPDATE schema_meta SET value = '2' WHERE key = 'schema_version'")
        conn.execute("UPDATE wellness_daily SET ctl = 62.4, atl = 71.8, form = -9.4")
        assert {"ctl", "atl", "form"} <= columns(conn, "wellness_daily")
    finally:
        conn.close()

    with Store.open(path) as store:
        row = store.conn.execute("SELECT * FROM wellness_daily").fetchone()

        assert {"ctl", "atl", "form"}.isdisjoint(set(row.keys()))
        assert row["hrv_rmssd"] == 61.5
        assert row["resting_hr"] == 48.0
        assert row["weight_kg"] == 61.2


def test_migration_leaves_tests_untouched(tmp_path: Path) -> None:
    path = tmp_path / "v1.db"
    _build_v1_database(path)

    with Store.open(path) as store:
        row = store.conn.execute("SELECT * FROM tests").fetchone()

        assert row["metric"] == "vo2max"
        assert row["value"] == 51.0


def test_migration_records_the_new_version(tmp_path: Path) -> None:
    path = tmp_path / "v1.db"
    _build_v1_database(path)

    with Store.open(path) as store:
        assert current_version(store.conn) == SCHEMA_VERSION


def test_migration_is_idempotent(tmp_path: Path) -> None:
    """Opening an already-migrated database must change nothing."""
    path = tmp_path / "v1.db"
    _build_v1_database(path)

    with Store.open(path) as store:
        first = store.counts()
    with Store.open(path) as store:
        assert store.counts() == first
        assert current_version(store.conn) == SCHEMA_VERSION


def test_migrated_database_accepts_new_writes(tmp_path: Path) -> None:
    """The migrated table must take an Intervals row beside the Strava one."""
    path = tmp_path / "v1.db"
    _build_v1_database(path)

    with Store.open(path) as store:
        store.conn.execute(
            """
            INSERT INTO activities (
                source, external_id, start_time, sport_type, name, distance_m,
                moving_time_s, elapsed_time_s, icu_load
            ) VALUES ('intervals', 'i123', '2026-06-20T07:42:11+00:00', 'Run',
                      'New run', 10240.5, 2914, 3020, 96.4)
            """
        )

        assert store.counts()["activities"] == 2


def test_fresh_database_is_created_at_the_current_version(tmp_path: Path) -> None:
    """A new database skips migrations entirely and still lands on v2."""
    path = tmp_path / "fresh.db"

    with Store.open(path) as store:
        assert current_version(store.conn) == SCHEMA_VERSION
        assert "external_id" in columns(store.conn, "activities")
        assert "strava_id" not in columns(store.conn, "activities")


def test_foreign_keys_are_enforced_after_migration(tmp_path: Path) -> None:
    """The pragma is toggled during the rebuild and must be restored."""
    path = tmp_path / "v1.db"
    _build_v1_database(path)

    with Store.open(path) as store:
        enabled = store.conn.execute("PRAGMA foreign_keys").fetchone()[0]

        assert enabled == 1
        try:
            store.conn.execute("INSERT INTO activity_streams (activity_id, t_s) VALUES (9999, 0)")
        except sqlite3.IntegrityError:
            pass
        else:  # pragma: no cover - only reached if enforcement is off
            msg = "an orphaned stream row was accepted after migration"
            raise AssertionError(msg)
