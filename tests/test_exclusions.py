"""Suppressing a wellness reading that is known to be wrong."""

import datetime as dt

import pandas as pd
import pytest

from longrun.analysis.service import daily_readings
from longrun.models import WellnessDay
from longrun.report.frames import load_wellness
from longrun.store import Store
from longrun.store.exclusions import add_exclusion, list_exclusions, remove_exclusion

DAY = dt.date(2026, 7, 19)


def _seed(store: Store) -> None:
    """Store three days, the middle one carrying an implausible resting HR."""
    store.upsert_wellness(
        [
            WellnessDay(date=dt.date(2026, 7, 18), resting_hr=56.0, hrv_rmssd=60.0),
            WellnessDay(date=DAY, resting_hr=40.0, hrv_rmssd=36.0),
            WellnessDay(date=dt.date(2026, 7, 20), resting_hr=55.0, hrv_rmssd=62.0),
        ]
    )


def _on(frame: pd.DataFrame, column: str) -> "pd.Series[float]":
    """Return one column, restricted to the excluded day."""
    return frame.loc[frame["date"] == pd.Timestamp(DAY), column]


def test_an_excluded_reading_is_hidden_from_the_report(store: Store) -> None:
    _seed(store)
    add_exclusion(store.conn, DAY.isoformat(), "resting_hr", "sensor lost the wrist")

    assert _on(load_wellness(store.conn), "resting_hr").isna().all()


def test_the_rest_of_that_day_survives(store: Store) -> None:
    """Excluding a resting heart rate says nothing about that morning's HRV."""
    _seed(store)
    add_exclusion(store.conn, DAY.isoformat(), "resting_hr", "sensor lost the wrist")

    assert _on(load_wellness(store.conn), "hrv_rmssd").iloc[0] == 36.0


def test_other_days_are_untouched(store: Store) -> None:
    _seed(store)
    add_exclusion(store.conn, DAY.isoformat(), "resting_hr", "sensor lost the wrist")

    frame = load_wellness(store.conn)

    assert frame["resting_hr"].notna().sum() == 2


def test_readiness_does_not_see_an_excluded_reading(store: Store) -> None:
    """A bad resting HR would otherwise drag the baseline it is judged against."""
    _seed(store)
    add_exclusion(store.conn, DAY.isoformat(), "resting_hr", "sensor lost the wrist")

    readings = daily_readings(store, dt.date(2026, 7, 1))

    on_the_day = next(reading for reading in readings if reading.date == DAY)
    assert on_the_day.resting_hr is None
    assert on_the_day.hrv_rmssd == 36.0


def test_the_stored_value_is_kept_so_the_call_can_be_undone(store: Store) -> None:
    _seed(store)
    add_exclusion(store.conn, DAY.isoformat(), "resting_hr", "sensor lost the wrist")

    assert remove_exclusion(store.conn, DAY.isoformat(), "resting_hr") is True
    assert _on(load_wellness(store.conn), "resting_hr").iloc[0] == 40.0


def test_removing_an_exclusion_that_was_never_made_says_so(store: Store) -> None:
    assert remove_exclusion(store.conn, DAY.isoformat(), "resting_hr") is False


def test_re_excluding_updates_the_reason_rather_than_duplicating(store: Store) -> None:
    _seed(store)
    add_exclusion(store.conn, DAY.isoformat(), "resting_hr", "first thought")
    add_exclusion(store.conn, DAY.isoformat(), "resting_hr", "on reflection")

    assert list_exclusions(store.conn) == [(DAY.isoformat(), "resting_hr", "on reflection")]


def test_a_reading_stays_excluded_after_the_next_sync_rewrites_it(store: Store) -> None:
    """Deleting the row would not work: the next sync fetches it again."""
    _seed(store)
    add_exclusion(store.conn, DAY.isoformat(), "resting_hr", "sensor lost the wrist")

    store.upsert_wellness([WellnessDay(date=DAY, resting_hr=40.0)])

    assert _on(load_wellness(store.conn), "resting_hr").isna().all()


def test_a_field_that_is_not_a_wellness_column_is_rejected(store: Store) -> None:
    """A typo would otherwise record an exclusion that silently does nothing."""
    with pytest.raises(ValueError, match="not a wellness field"):
        add_exclusion(store.conn, DAY.isoformat(), "restingHR", "typo")
