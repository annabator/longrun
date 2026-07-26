"""The store's contract: an idempotent schema and upserts that never duplicate."""

import datetime as dt
from pathlib import Path

from longrun.models import Activity, StreamPoint, WellnessDay

# Aliased: pytest would otherwise try to collect the model class as a test suite.
from longrun.models import TestResult as Measurement
from longrun.store import SCHEMA_VERSION, Store, init_db
from longrun.store.schema import connect


def test_init_db_is_idempotent(tmp_path: Path) -> None:
    """Re-initialising an existing database must be a no-op, not an error."""
    path = tmp_path / "twice.db"
    conn = connect(path)
    try:
        init_db(conn)
        init_db(conn)
        version = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    finally:
        conn.close()

    assert version["value"] == str(SCHEMA_VERSION)
    assert {"activities", "activity_streams", "wellness_daily", "tests"} <= tables


def test_activity_upsert_updates_in_place(store: Store, activity: Activity) -> None:
    """Re-ingesting an activity updates the existing row rather than adding one."""
    store.upsert_activities([activity])
    store.upsert_activities([activity.model_copy(update={"name": "Renamed", "avg_hr": 151.0})])

    rows = store.conn.execute("SELECT name, avg_hr FROM activities").fetchall()

    assert len(rows) == 1
    assert rows[0]["name"] == "Renamed"
    assert rows[0]["avg_hr"] == 151.0


def test_stream_upsert_is_keyed_on_time_offset(store: Store, activity: Activity) -> None:
    """Re-ingesting streams overwrites samples instead of duplicating them."""
    store.upsert_activities([activity])
    store.upsert_streams(activity.strava_id, [StreamPoint(t_s=0, hr=120.0), StreamPoint(t_s=1)])
    store.upsert_streams(activity.strava_id, [StreamPoint(t_s=0, hr=130.0)])

    rows = store.conn.execute("SELECT t_s, hr FROM activity_streams ORDER BY t_s").fetchall()

    assert [row["t_s"] for row in rows] == [0, 1]
    assert rows[0]["hr"] == 130.0


def test_streams_for_unknown_activity_are_dropped(store: Store) -> None:
    """Streams without a parent activity are refused, not orphaned."""
    written = store.upsert_streams(999_999, [StreamPoint(t_s=0, hr=120.0)])

    assert written == 0
    assert store.counts()["activity_streams"] == 0


def test_wellness_merge_keeps_values_from_other_sources(store: Store) -> None:
    """A CSV carrying only weight must not erase a resting HR logged elsewhere."""
    day = dt.date(2026, 3, 1)
    store.upsert_wellness([WellnessDay(date=day, resting_hr=48.0, source="watch")])
    store.upsert_wellness([WellnessDay(date=day, weight_kg=61.2, source="scales")])

    row = store.conn.execute("SELECT * FROM wellness_daily").fetchone()

    assert row["resting_hr"] == 48.0
    assert row["weight_kg"] == 61.2


def test_tests_upsert_is_keyed_on_date_metric_and_source(store: Store) -> None:
    """The same metric from two sources coexists; the same source updates."""
    day = dt.date(2026, 3, 1)
    store.upsert_tests(
        [Measurement(date=day, metric="vo2max", value=52.0, unit="ml/kg/min", source="watch")]
    )
    store.upsert_tests(
        [Measurement(date=day, metric="vo2max", value=53.5, unit="ml/kg/min", source="watch")]
    )
    store.upsert_tests(
        [Measurement(date=day, metric="vo2max", value=50.0, unit="ml/kg/min", source="lab")]
    )

    rows = store.conn.execute("SELECT source, value FROM tests ORDER BY source").fetchall()

    assert [(row["source"], row["value"]) for row in rows] == [("lab", 50.0), ("watch", 53.5)]


def test_latest_activity_start_drives_incremental_ingest(store: Store, activity: Activity) -> None:
    """The latest stored start time is what makes the next Strava run incremental."""
    assert store.latest_activity_start() is None

    later = activity.model_copy(
        update={
            "strava_id": 4243,
            "start_time": dt.datetime(2026, 4, 1, 6, 0, tzinfo=dt.UTC),
        }
    )
    store.upsert_activities([activity, later])

    assert store.latest_activity_start() == dt.datetime(2026, 4, 1, 6, 0, tzinfo=dt.UTC)
