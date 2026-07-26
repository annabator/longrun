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
    source, external_id, start_time, sport_type, name, distance_m,
    moving_time_s, elapsed_time_s, avg_hr, max_hr, avg_speed,
    total_elevation_gain, avg_cadence, feel, rpe, icu_load, icu_intensity
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (source, external_id) DO UPDATE SET
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
    -- Subjective fields are merged rather than overwritten: a re-ingest that
    -- predates the athlete rating the session must not blank the rating.
    feel                 = COALESCE(excluded.feel,          activities.feel),
    rpe                  = COALESCE(excluded.rpe,           activities.rpe),
    icu_load             = COALESCE(excluded.icu_load,      activities.icu_load),
    icu_intensity        = COALESCE(excluded.icu_intensity, activities.icu_intensity)
"""

_STREAM_UPSERT = """
INSERT INTO activity_streams (
    activity_id, t_s, hr, velocity, distance_m, altitude, cadence, rr_ms
) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (activity_id, t_s) DO UPDATE SET
    hr         = excluded.hr,
    velocity   = excluded.velocity,
    distance_m = excluded.distance_m,
    altitude   = excluded.altitude,
    cadence    = excluded.cadence,
    -- R-R comes from FIT files, which may be ingested after Intervals.icu
    -- has already supplied the rest of the stream; keep whichever has it.
    rr_ms      = COALESCE(excluded.rr_ms, activity_streams.rr_ms)
"""

_WELLNESS_UPSERT = """
INSERT INTO wellness_daily (
    date, resting_hr, avg_sleeping_hr, hrv_rmssd, sleep_score, sleep_hours,
    spo2, weight_kg, ctl, atl, form, source, notes
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (date) DO UPDATE SET
    resting_hr      = COALESCE(excluded.resting_hr,      wellness_daily.resting_hr),
    avg_sleeping_hr = COALESCE(excluded.avg_sleeping_hr, wellness_daily.avg_sleeping_hr),
    hrv_rmssd       = COALESCE(excluded.hrv_rmssd,       wellness_daily.hrv_rmssd),
    sleep_score     = COALESCE(excluded.sleep_score,     wellness_daily.sleep_score),
    sleep_hours     = COALESCE(excluded.sleep_hours,     wellness_daily.sleep_hours),
    spo2            = COALESCE(excluded.spo2,            wellness_daily.spo2),
    weight_kg       = COALESCE(excluded.weight_kg,       wellness_daily.weight_kg),
    ctl             = COALESCE(excluded.ctl,             wellness_daily.ctl),
    atl             = COALESCE(excluded.atl,             wellness_daily.atl),
    form            = COALESCE(excluded.form,            wellness_daily.form),
    source          = excluded.source,
    notes           = COALESCE(excluded.notes,           wellness_daily.notes)
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
        """Insert or update activities, keyed on `(source, external_id)`.

        Args:
            activities: Activities to persist.

        Returns:
            The number of activities written.
        """
        rows = [
            (
                a.source,
                a.external_id,
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
                a.feel,
                a.rpe,
                a.icu_load,
                a.icu_intensity,
            )
            for a in activities
        ]
        with self.conn:
            self.conn.executemany(_ACTIVITY_UPSERT, rows)
        return len(rows)

    def upsert_streams(self, source: str, external_id: str, points: Iterable[StreamPoint]) -> int:
        """Insert or update the stream samples for one activity.

        Args:
            source: Adapter that produced the parent activity.
            external_id: The provider's identifier for the parent activity,
                which must already exist in `activities`.
            points: Samples to persist, keyed on `(activity_id, t_s)`.

        Returns:
            The number of samples written; zero if the activity is unknown.
        """
        activity_id = self.activity_row_id(source, external_id)
        if activity_id is None:
            return 0
        rows = [
            (activity_id, p.t_s, p.hr, p.velocity, p.distance_m, p.altitude, p.cadence, p.rr_ms)
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
                d.avg_sleeping_hr,
                d.hrv_rmssd,
                d.sleep_score,
                d.sleep_hours,
                d.spo2,
                d.weight_kg,
                d.ctl,
                d.atl,
                d.form,
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

    def activity_row_id(self, source: str, external_id: str) -> int | None:
        """Return the internal row id for a stored activity, if present.

        Args:
            source: Adapter that produced the activity.
            external_id: The provider's identifier for it.

        Returns:
            The `activities.id` value, or None if the activity is unknown.
        """
        row = self.conn.execute(
            "SELECT id FROM activities WHERE source = ? AND external_id = ?",
            (source, external_id),
        ).fetchone()
        return int(row["id"]) if row is not None else None

    def latest_activity_start(self, source: str | None = None) -> dt.datetime | None:
        """Return the start time of the most recent stored activity.

        Used to make ingestion incremental. Scoped to one source when given,
        so a sparse FIT backfill cannot make an Intervals sync think it is
        already up to date.

        Args:
            source: Restrict to one adapter's activities.

        Returns:
            A timezone-aware datetime, or None if no activities are stored.
        """
        if source is None:
            row = self.conn.execute("SELECT MAX(start_time) AS latest FROM activities").fetchone()
        else:
            row = self.conn.execute(
                "SELECT MAX(start_time) AS latest FROM activities WHERE source = ?", (source,)
            ).fetchone()
        if row is None or row["latest"] is None:
            return None
        return dt.datetime.fromisoformat(str(row["latest"]))

    def latest_wellness_date(self, source: str | None = None) -> dt.date | None:
        """Return the most recent date with a stored wellness row.

        Args:
            source: Restrict to one adapter's rows.

        Returns:
            The date, or None if no wellness is stored.
        """
        if source is None:
            row = self.conn.execute("SELECT MAX(date) AS latest FROM wellness_daily").fetchone()
        else:
            row = self.conn.execute(
                "SELECT MAX(date) AS latest FROM wellness_daily WHERE source = ?", (source,)
            ).fetchone()
        if row is None or row["latest"] is None:
            return None
        return dt.date.fromisoformat(str(row["latest"]))

    def stored_stream_activity_ids(self, source: str) -> set[str]:
        """Return the external ids of one source's activities that have streams.

        Lets ingestion skip re-downloading streams, which is the expensive
        part of any provider's request budget.

        Args:
            source: Adapter to report on.

        Returns:
            A set of `external_id` values.
        """
        rows = self.conn.execute(
            """
            SELECT DISTINCT a.external_id AS external_id
            FROM activities a
            JOIN activity_streams s ON s.activity_id = a.id
            WHERE a.source = ?
            """,
            (source,),
        ).fetchall()
        return {str(row["external_id"]) for row in rows}

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
