"""Translate Intervals.icu payloads into longrun domain models.

Intervals.icu is not consistent about naming between its resources: activity
fields are snake_case (`icu_training_load`, `perceived_exertion`) while
wellness fields are camelCase (`restingHR`, `sleepSecs`), and the published
documentation lags the deployed API in places.

Rather than betting on one spelling, every field is read through a list of
candidate keys, first match wins. That costs a few lines here and makes
ingestion robust to a rename upstream, which matters because the failure mode
of guessing wrong is a column that silently fills with nulls.
"""

import datetime as dt
from collections.abc import Mapping, Sequence
from typing import Any

from longrun.errors import IngestError
from longrun.models import Activity, Source, StreamPoint, WellnessDay

# Candidate keys per field, in priority order.
_ACTIVITY_KEYS: Mapping[str, tuple[str, ...]] = {
    "id": ("id", "activity_id"),
    "start": ("start_date_local", "start_date", "startDateLocal"),
    "sport": ("type", "sport_type", "sportType"),
    "name": ("name",),
    "distance": ("distance", "icu_distance"),
    "moving_time": ("moving_time", "movingTime"),
    "elapsed_time": ("elapsed_time", "elapsedTime"),
    "avg_hr": ("average_heartrate", "icu_average_hr", "averageHeartrate"),
    "max_hr": ("max_heartrate", "icu_max_hr", "maxHeartrate"),
    "avg_speed": ("average_speed", "averageSpeed"),
    "elevation": ("total_elevation_gain", "icu_elevation_gain", "totalElevationGain"),
    "avg_cadence": ("average_cadence", "averageCadence"),
    "feel": ("feel",),
    "rpe": ("perceived_exertion", "rpe", "perceivedExertion"),
    "load": ("icu_training_load", "icu_load", "training_load", "trainingLoad"),
    "intensity": ("icu_intensity", "intensity"),
}

_WELLNESS_KEYS: Mapping[str, tuple[str, ...]] = {
    "date": ("id", "date", "localDate"),
    "resting_hr": ("restingHR", "resting_hr", "restingHr"),
    "avg_sleeping_hr": ("avgSleepingHR", "avg_sleeping_hr", "averageSleepingHR"),
    "hrv_rmssd": ("hrv", "hrvRMSSD", "hrv_rmssd", "rmssd"),
    "sleep_score": ("sleepScore", "sleep_score"),
    "sleep_secs": ("sleepSecs", "sleep_secs", "sleepTime"),
    "sleep_hours": ("sleepHours", "sleep_hours", "sleep"),
    "spo2": ("spO2", "spo2", "SpO2"),
}
# `weight` is deliberately absent. Intervals.icu carries the figure entered
# once at sign-up on every wellness record, so it is a constant rather than a
# measurement. Ingesting it would flatten the weight trend into a straight
# line and, because wellness merges field by field, a later sync would
# overwrite a real weight entered by hand. Weight comes from a CSV only.

_STREAM_FIELD_BY_TYPE: Mapping[str, str] = {
    "heartrate": "hr",
    "velocity_smooth": "velocity",
    "distance": "distance_m",
    "altitude": "altitude",
    "cadence": "cadence",
}

_SECONDS_PER_HOUR = 3600.0


def activity_from_payload(payload: Mapping[str, Any]) -> Activity:
    """Build an :class:`Activity` from an Intervals.icu activity summary.

    Args:
        payload: One entry from the activities endpoint.

    Returns:
        The normalised activity.

    Raises:
        IngestError: If required fields are absent or unparseable.
    """
    activity_id = _first(payload, _ACTIVITY_KEYS["id"])
    # Only the identity and the timestamp are genuinely required. Distance and
    # duration are absent from whole categories of activity - a gym session,
    # yoga, windsurfing - and those are worth storing as load context even
    # though they cover no ground. Treating them as required rejected real
    # sessions and, worse, stopped the sync that met them.
    moving = _number(payload, _ACTIVITY_KEYS["moving_time"])
    elapsed = _number(payload, _ACTIVITY_KEYS["elapsed_time"])
    try:
        return Activity(
            source=Source.INTERVALS,
            external_id=str(activity_id),
            start_time=_parse_timestamp(str(_require(payload, _ACTIVITY_KEYS["start"], "start"))),
            sport_type=str(_first(payload, _ACTIVITY_KEYS["sport"]) or "Unknown"),
            name=str(_first(payload, _ACTIVITY_KEYS["name"]) or "Untitled"),
            distance_m=_number(payload, _ACTIVITY_KEYS["distance"]) or 0.0,
            moving_time_s=int(moving if moving is not None else elapsed or 0.0),
            elapsed_time_s=int(elapsed if elapsed is not None else moving or 0.0),
            avg_hr=_number(payload, _ACTIVITY_KEYS["avg_hr"]),
            max_hr=_number(payload, _ACTIVITY_KEYS["max_hr"]),
            avg_speed=_number(payload, _ACTIVITY_KEYS["avg_speed"]),
            total_elevation_gain=_number(payload, _ACTIVITY_KEYS["elevation"]),
            avg_cadence=_number(payload, _ACTIVITY_KEYS["avg_cadence"]),
            feel=_number(payload, _ACTIVITY_KEYS["feel"]),
            rpe=_number(payload, _ACTIVITY_KEYS["rpe"]),
            icu_load=_number(payload, _ACTIVITY_KEYS["load"]),
            icu_intensity=_number(payload, _ACTIVITY_KEYS["intensity"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        msg = f"Could not parse Intervals.icu activity {activity_id or '<unknown>'}: {exc}"
        raise IngestError(msg) from exc


def wellness_from_payload(payload: Mapping[str, Any]) -> WellnessDay:
    """Build a :class:`WellnessDay` from an Intervals.icu wellness record.

    Two things the endpoint returns are deliberately ignored. The rolling CTL,
    ATL and form figures are averages over a modelled load rather than
    measurements, and Intervals.icu remains the place to read them. Weight is
    the value entered once at sign-up, repeated unchanged on every record, so
    it is a profile setting wearing a measurement's clothes.

    Args:
        payload: One record from the wellness endpoint.

    Returns:
        The normalised wellness day.

    Raises:
        IngestError: If the record carries no usable date.
    """
    raw_date = _first(payload, _WELLNESS_KEYS["date"])
    if raw_date is None:
        msg = "An Intervals.icu wellness record arrived without a date; skipping is unsafe."
        raise IngestError(msg)
    try:
        day = dt.date.fromisoformat(str(raw_date)[:10])
    except ValueError as exc:
        msg = f"Could not parse the date on an Intervals.icu wellness record: {raw_date!r}"
        raise IngestError(msg) from exc

    return WellnessDay(
        date=day,
        resting_hr=_number(payload, _WELLNESS_KEYS["resting_hr"]),
        avg_sleeping_hr=_number(payload, _WELLNESS_KEYS["avg_sleeping_hr"]),
        hrv_rmssd=_number(payload, _WELLNESS_KEYS["hrv_rmssd"]),
        sleep_score=_number(payload, _WELLNESS_KEYS["sleep_score"]),
        sleep_hours=_sleep_hours(payload),
        spo2=_number(payload, _WELLNESS_KEYS["spo2"]),
        # Never read from this source; see the note on _WELLNESS_KEYS.
        weight_kg=None,
        source=Source.INTERVALS,
    )


def stream_points_from_payload(payload: Mapping[str, Any]) -> list[StreamPoint]:
    """Build stream samples from an Intervals.icu streams response.

    The `time` stream defines the sample count; other streams are read
    positionally and padded with None where they are shorter, which happens
    when a sensor drops out mid-run.

    Args:
        payload: The streams response for one activity.

    Returns:
        One :class:`StreamPoint` per sample, empty if there is no time stream.
    """
    series = _stream_series(payload)
    times = series.get("time")
    if not times:
        return []

    points: list[StreamPoint] = []
    for index, raw_time in enumerate(times):
        if raw_time is None:
            continue
        fields = {
            field: _to_float(_at(series.get(stream_type), index))
            for stream_type, field in _STREAM_FIELD_BY_TYPE.items()
        }
        points.append(StreamPoint(t_s=int(raw_time), **fields))
    return points


def _stream_series(payload: Mapping[str, Any]) -> dict[str, Sequence[Any]]:
    """Reduce a streams payload to a mapping of stream type to data list."""
    series: dict[str, Sequence[Any]] = {}
    for key, value in payload.items():
        if isinstance(value, Mapping) and isinstance(value.get("data"), Sequence):
            series[str(value.get("type") or key)] = value["data"]
        elif isinstance(value, Sequence) and not isinstance(value, str | bytes):
            # Some responses inline the samples directly under the type name.
            series[str(key)] = value
    return series


def _sleep_hours(payload: Mapping[str, Any]) -> float | None:
    """Return sleep duration in hours, from whichever field is present."""
    seconds = _number(payload, _WELLNESS_KEYS["sleep_secs"])
    if seconds is not None:
        return seconds / _SECONDS_PER_HOUR
    return _number(payload, _WELLNESS_KEYS["sleep_hours"])


def _first(payload: Mapping[str, Any], keys: Sequence[str]) -> Any:
    """Return the first non-null value among `keys`, or None."""
    for key in keys:
        value = payload.get(key)
        if value is not None:
            return value
    return None


def _require(payload: Mapping[str, Any], keys: Sequence[str], label: str) -> Any:
    """Return the first present value among `keys`, or raise."""
    value = _first(payload, keys)
    if value is None:
        msg = f"missing {label} (tried {', '.join(keys)})"
        raise KeyError(msg)
    return value


def _number(payload: Mapping[str, Any], keys: Sequence[str]) -> float | None:
    """Return the first numeric value among `keys`, or None."""
    return _to_float(_first(payload, keys))


def _to_float(value: Any) -> float | None:
    """Coerce a value to float, mapping absent or non-numeric input to None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _at(series: Sequence[Any] | None, index: int) -> Any:
    """Return `series[index]`, or None when the series is short or absent."""
    if series is None or index >= len(series):
        return None
    return series[index]


def _parse_timestamp(value: str) -> dt.datetime:
    """Parse an Intervals.icu timestamp, which may carry no timezone.

    `start_date_local` is wall-clock local time with no offset. It is treated
    as UTC so that stored values stay unambiguous and ordering holds; the
    alternative, guessing a timezone, would silently shift every activity.
    """
    text = value.replace("Z", "+00:00")
    parsed = dt.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.UTC)
    return parsed
