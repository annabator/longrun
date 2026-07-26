"""Mapping Intervals.icu payloads, including its inconsistent field naming."""

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest

from longrun.errors import IngestError
from longrun.ingest.intervals import (
    activity_from_payload,
    stream_points_from_payload,
    wellness_from_payload,
)
from longrun.models import Source

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_activity_fields_are_mapped() -> None:
    payload = _load("intervals_activities.json")[0]

    activity = activity_from_payload(payload)

    assert activity.source == Source.INTERVALS
    assert activity.external_id == "i84213770"
    assert activity.sport_type == "Run"
    assert activity.distance_m == pytest.approx(10240.5)
    assert activity.moving_time_s == 2914
    assert activity.avg_hr == pytest.approx(157.2)


def test_subjective_and_load_fields_are_mapped() -> None:
    """feel, RPE and the Intervals load figures are the point of this source."""
    payload = _load("intervals_activities.json")[0]

    activity = activity_from_payload(payload)

    assert activity.feel == pytest.approx(4.0)
    # Intervals.icu calls RPE `perceived_exertion`.
    assert activity.rpe == pytest.approx(7.0)
    assert activity.icu_load == pytest.approx(96.4)
    assert activity.icu_intensity == pytest.approx(88.2)


def test_missing_optional_fields_stay_none() -> None:
    """Not every activity is rated, and a gym session has no distance."""
    _, long_run, tennis = _load("intervals_activities.json")

    mapped_long = activity_from_payload(long_run)
    mapped_tennis = activity_from_payload(tennis)

    assert mapped_long.feel is None
    assert mapped_long.rpe is None
    assert mapped_long.icu_intensity is None
    assert mapped_tennis.sport_type == "Tennis"
    assert mapped_tennis.distance_m == 0.0


def test_naive_local_start_is_treated_as_utc() -> None:
    """`start_date_local` carries no offset; guessing one would shift everything."""
    payload = _load("intervals_activities.json")[0]

    assert activity_from_payload(payload).start_time == dt.datetime(
        2026, 6, 20, 7, 42, 11, tzinfo=dt.UTC
    )


def test_activity_without_required_field_names_itself() -> None:
    payload = {"id": "i999", "type": "Run"}

    with pytest.raises(IngestError, match="i999"):
        activity_from_payload(payload)


def test_wellness_camel_case_fields_are_mapped() -> None:
    record = _load("intervals_wellness.json")[0]

    day = wellness_from_payload(record)

    assert day.date == dt.date(2026, 6, 20)
    assert day.resting_hr == pytest.approx(47.0)
    assert day.avg_sleeping_hr == pytest.approx(51.4)
    assert day.hrv_rmssd == pytest.approx(68.2)
    assert day.sleep_score == pytest.approx(84.0)
    assert day.spo2 == pytest.approx(97.0)
    assert day.weight_kg == pytest.approx(61.1)
    assert day.source == Source.INTERVALS


def test_sleep_seconds_become_hours() -> None:
    record = _load("intervals_wellness.json")[0]

    assert wellness_from_payload(record).sleep_hours == pytest.approx(7.5)


def test_rolling_load_fields_are_ignored() -> None:
    """The fixture still carries CTL, ATL and form; the mapper drops them.

    They are averages over a modelled load rather than measurements, so
    Intervals.icu stays the place to read them.
    """
    record = _load("intervals_wellness.json")[0]

    day = wellness_from_payload(record)

    assert not hasattr(day, "ctl")
    assert not hasattr(day, "atl")
    assert not hasattr(day, "form")
    # The measured fields on the same record still map.
    assert day.hrv_rmssd == pytest.approx(68.2)


def test_snake_case_wellness_fields_are_also_accepted() -> None:
    """The API is not consistent between resources, so both spellings map."""
    record = _load("intervals_wellness.json")[2]

    day = wellness_from_payload(record)

    assert day.date == dt.date(2026, 6, 22)
    assert day.resting_hr == pytest.approx(48.0)
    assert day.hrv_rmssd == pytest.approx(64.0)
    assert day.sleep_hours == pytest.approx(7.75)


def test_wellness_without_a_date_is_rejected() -> None:
    with pytest.raises(IngestError, match="without a date"):
        wellness_from_payload({"restingHR": 50})


def test_streams_pad_short_series_and_ignore_malformed_ones() -> None:
    payload = _load("intervals_streams.json")

    points = stream_points_from_payload(payload)

    assert [point.t_s for point in points] == [0, 1, 2, 3, 4]
    # heartrate is one sample short of time; the run is not truncated.
    assert points[4].hr is None
    assert points[1].hr == pytest.approx(131.0)
    assert points[0].cadence is None
    # The `watts` entry has no data list and must not abort the parse.
    assert points[4].velocity == pytest.approx(3.55)


def test_streams_without_time_yield_nothing() -> None:
    assert stream_points_from_payload({"heartrate": {"data": [120]}}) == []
    assert stream_points_from_payload({}) == []
