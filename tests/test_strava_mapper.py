"""Mapping Strava payloads, including the ragged shapes real exports contain."""

import datetime as dt
from typing import Any

import pytest

from longrun.errors import IngestError
from longrun.ingest.strava.mapper import activity_from_payload, stream_points_from_payload
from longrun.models import Source

PAYLOAD: dict[str, Any] = {
    "id": 12345,
    "name": "Tempo",
    "sport_type": "Run",
    "start_date": "2026-05-02T07:15:09Z",
    "distance": 10012.3,
    "moving_time": 2740,
    "elapsed_time": 2801,
    "average_heartrate": 152.4,
    "max_heartrate": 178.0,
    "average_speed": 3.65,
    "total_elevation_gain": 62.0,
    "average_cadence": 86.5,
}


def test_activity_fields_are_mapped() -> None:
    activity = activity_from_payload(PAYLOAD)

    assert activity.strava_id == 12345
    assert activity.start_time == dt.datetime(2026, 5, 2, 7, 15, 9, tzinfo=dt.UTC)
    assert activity.distance_m == pytest.approx(10012.3)
    assert activity.avg_hr == pytest.approx(152.4)
    assert activity.source == Source.STRAVA


def test_legacy_type_field_is_accepted() -> None:
    """Older payloads spell the discipline `type` rather than `sport_type`."""
    payload = {key: value for key, value in PAYLOAD.items() if key != "sport_type"}
    payload["type"] = "TrailRun"

    assert activity_from_payload(payload).sport_type == "TrailRun"


def test_missing_required_field_names_the_activity() -> None:
    payload = {key: value for key, value in PAYLOAD.items() if key != "distance"}

    with pytest.raises(IngestError, match="12345"):
        activity_from_payload(payload)


def test_short_streams_are_padded_not_truncated() -> None:
    """A sensor dropping out mid-run must not shorten the whole activity."""
    points = stream_points_from_payload(
        {
            "time": {"data": [0, 1, 2, 3]},
            "heartrate": {"data": [120, 130, 140]},
            "cadence": {"data": [None, 84, 86, 87]},
        }
    )

    assert [point.t_s for point in points] == [0, 1, 2, 3]
    assert [point.hr for point in points] == [120.0, 130.0, 140.0, None]
    assert points[0].cadence is None
    assert points[1].cadence == 84.0


def test_streams_without_a_time_series_yield_nothing() -> None:
    """Manually entered activities carry no streams at all."""
    assert stream_points_from_payload({"heartrate": {"data": [120, 130]}}) == []
    assert stream_points_from_payload({}) == []


def test_malformed_stream_is_skipped_not_fatal() -> None:
    """One unusable stream must not cost the whole activity."""
    points = stream_points_from_payload(
        {"time": {"data": [0, 1]}, "heartrate": {"unexpected": True}}
    )

    assert len(points) == 2
    assert all(point.hr is None for point in points)
