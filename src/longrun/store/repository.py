"""Read and write access to the longrun SQLite store.

Every write is an upsert keyed on a natural key, so re-ingesting the same
source is safe and produces no duplicates:

* `activities` is keyed on `strava_id` and fully overwritten, because Strava
  is the single source of truth for an activity.
* `wellness_daily` is keyed on `date` and merged with `COALESCE`, so a CSV
  that only carries weight cannot wipe a resting-HR value logged from another
  source. The trade-off is that a value cannot be cleared by re-ingesting a
  blank; correct a value by ingesting the new one.
* `tests` is keyed on `(date, metric, source)`.
"""

import datetime as dt
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Self

from longrun.models import Activity, StreamPoint, TestResult, WellnessDay
from longrun.store.schema import connect, init_db

_ACTIVITY_UPSERT = """
INSERT INTO activities (
    strava_id, start_time, sport_type, name, distance_m, moving_time_s,
    elapsed_time_s, avg_hr, max_hr, avg_speed, total_elevation_gain,
    avg_cadence, source
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (strava_id) DO UPDATE SET
    start_time           = excluded.start_time,
    sport_type           = excluded.sport_type,
    name                 = excluded.name,
    distance_m           = excluded.distance_m,
    moving_time_s        = excluded.moving_time_s,
    elapsed_time_s       = excluded.elapsed_time_s,
    avg_hr               = excluded.avg_hr,
    max_hr               = excluded.max_hr,
    avg_speed            = excluded.avg_speed,
    total_elevation_gain = excluded.total_elevation_gain,
    avg_cadence          = excluded.avg_cadence,
    source               = excluded.source
"""

_STREAM_UPSERT = """
INSERT INTO activity_streams (activity_id, t_s, hr, velocity, distance_m, altitude, cadence)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (activity_id, t_s) DO UPDATE SET
    hr         = excluded.hr,
    velocity   = excluded.velocity,
    distance_m = excluded.distance_m,
    altitude   = excluded.altitude,
    cadence    = excluded.cadence
"""

_WELLNESS_UPSERT = """
INSERT INTO wellness_daily (
    date, resting_hr, hrv, sleep_hours, sleep_score, weight_kg, source, notes
) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (date) DO UPDATE SET
    resting_hr  = COALESCE(excluded.resting_hr,  wellness_daily.resting_hr),
    hrv         = COALESCE(excluded.hrv,         wellness_daily.hrv),
    sleep_hours = COALESCE(excluded.sleep_hours, wellness_daily.sleep_hours),
    sleep_score = COALESCE(excluded.sleep_score, wellness_daily.sleep_score),
    weight_kg   = COALESCE(excluded.weight_kg,   wellness_daily.weight_kg),
    source      = excluded.source,
    notes       = COALESCE(excluded.notes,       wellness_daily.notes)
"""

_TEST_UPSERT = """
INSERT INTO tests (date, metric, value, unit, source, notes)
VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT (date, metric, source) DO UPDATE SET
    value = excluded.value,
    unit  = excluded.unit,
    notes = COALESCE(excluded.notes, tests.notes)
"""


class Store:
    """Typed access to the longrun database.

    Wraps a :class:`sqlite3.Connection`. Prefer :meth:`open`, which also
    applies the schema.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        """Wrap an already-open connection.

        Args:
            conn: An open SQLite connection.
        """
        self.conn = conn

    @classmethod
    @contextmanager
    def open(cls, path: Path) -> Iterator[Self]:
        """Open an initialised store at `path` as a context manager.

        Args:
            path: Location of the database file; created if absent.

        Yields:
            A ready-to-use store.
        """
        conn = connect(path)
        try:
            init_db(conn)
            yield cls(conn)
        finally:
            conn.close()

    # -- writes ------------------------------------------------------------

    def upsert_activities(self, activities: Iterable[Activity]) -> int:
        """Insert or update activities, keyed on `strava_id`.

        Args:
            activities: Activities to persist.

        Returns:
            The number of activities written.
        """
        rows = [
            (
                a.strava_id,
                _iso_datetime(a.start_time),
                a.sport_type,
                a.name,
                a.distance_m,
                a.moving_time_s,
                a.elapsed_time_s,
                a.avg_hr,
                a.max_hr,
                a.avg_speed,
                a.total_elevation_gain,
                a.avg_cadence,
                a.source,
            )
            for a in activities
        ]
        with self.conn:
            self.conn.executemany(_ACTIVITY_UPSERT, rows)
        return len(rows)

    def upsert_streams(self, strava_id: int, points: Iterable[StreamPoint]) -> int:
        """Insert or update the stream samples for one activity.

        Args:
            strava_id: Strava identifier of the parent activity, which must
                already exist in `activities`.
            points: Samples to persist, keyed on `(activity_id, t_s)`.

        Returns:
            The number of samples written; zero if the activity is unknown.
        """
        activity_id = self.activity_row_id(strava_id)
        if activity_id is None:
            return 0
        rows = [
            (activity_id, p.t_s, p.hr, p.velocity, p.distance_m, p.altitude, p.cadence)
            for p in points
        ]
        with self.conn:
            self.conn.executemany(_STREAM_UPSERT, rows)
        return len(rows)

    def upsert_wellness(self, days: Iterable[WellnessDay]) -> int:
        """Insert or merge daily wellness rows, keyed on `date`.

        Args:
            days: Wellness records to persist.

        Returns:
            The number of rows written.
        """
        rows = [
            (
                d.date.isoformat(),
                d.resting_hr,
                d.hrv,
                d.sleep_hours,
                d.sleep_score,
                d.weight_kg,
                d.source,
                d.notes,
            )
            for d in days
        ]
        with self.conn:
            self.conn.executemany(_WELLNESS_UPSERT, rows)
        return len(rows)

    def upsert_tests(self, results: Iterable[TestResult]) -> int:
        """Insert or update test results, keyed on `(date, metric, source)`.

        Args:
            results: Measurements to persist.

        Returns:
            The number of rows written.
        """
        rows = [(r.date.isoformat(), r.metric, r.value, r.unit, r.source, r.notes) for r in results]
        with self.conn:
            self.conn.executemany(_TEST_UPSERT, rows)
        return len(rows)

    # -- reads -------------------------------------------------------------

    def activity_row_id(self, strava_id: int) -> int | None:
        """Return the internal row id for a Strava activity, if stored.

        Args:
            strava_id: Strava identifier of the activity.

        Returns:
            The `activities.id` value, or None if the activity is unknown.
        """
        row = self.conn.execute(
            "SELECT id FROM activities WHERE strava_id = ?", (strava_id,)
        ).fetchone()
        return int(row["id"]) if row is not None else None

    def latest_activity_start(self) -> dt.datetime | None:
        """Return the start time of the most recent stored activity.

        Used to make Strava ingestion incremental.

        Returns:
            A timezone-aware datetime, or None if no activities are stored.
        """
        row = self.conn.execute("SELECT MAX(start_time) AS latest FROM activities").fetchone()
        if row is None or row["latest"] is None:
            return None
        return dt.datetime.fromisoformat(str(row["latest"]))

    def stored_stream_activity_ids(self) -> set[int]:
        """Return the Strava ids of activities that already have streams.

        Lets ingestion skip re-downloading streams, which is the expensive
        part of the Strava API budget.

        Returns:
            A set of `strava_id` values.
        """
        rows = self.conn.execute(
            """
            SELECT DISTINCT a.strava_id AS strava_id
            FROM activities a
            JOIN activity_streams s ON s.activity_id = a.id
            """
        ).fetchall()
        return {int(row["strava_id"]) for row in rows}

    def counts(self) -> dict[str, int]:
        """Return the row count of each table, for CLI summaries.

        Returns:
            A mapping of table name to row count.
        """
        tables = ("activities", "activity_streams", "wellness_daily", "tests")
        return {
            table: int(self.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])
            for table in tables
        }


def _iso_datetime(value: dt.datetime) -> str:
    """Render a datetime as UTC ISO-8601 text for storage."""
    return value.astimezone(dt.UTC).isoformat()
