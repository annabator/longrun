"""Translate Strava API payloads into longrun domain models.

This is the only module that knows Strava's field names. Everything
downstream sees :class:`~longrun.models.Activity` and
:class:`~longrun.models.StreamPoint`, so a second provider would mean a new
mapper rather than changes to the store or the reports.
"""

import datetime as dt
from collections.abc import Mapping, Sequence
from typing import Any

from longrun.errors import IngestError
from longrun.models import Activity, Source, StreamPoint

_STREAM_FIELD_BY_KEY: Mapping[str, str] = {
    "heartrate": "hr",
    "velocity_smooth": "velocity",
    "distance": "distance_m",
    "altitude": "altitude",
    "cadence": "cadence",
}
"""Strava stream type -> :class:`StreamPoint` field."""


def activity_from_payload(payload: Mapping[str, Any]) -> Activity:
    """Build an :class:`Activity` from a Strava activity summary.

    Args:
        payload: A single entry from `/athlete/activities`.

    Returns:
        The normalised activity.

    Raises:
        IngestError: If required fields are absent or unparseable.
    """
    try:
        return Activity(
            source=Source.STRAVA,
            external_id=str(payload["id"]),
            start_time=_parse_timestamp(payload["start_date"]),
            # `sport_type` is the modern field; `type` is its legacy spelling.
            sport_type=str(payload.get("sport_type") or payload.get("type") or "Unknown"),
            name=str(payload.get("name") or "Untitled"),
            distance_m=float(payload["distance"]),
            moving_time_s=int(payload["moving_time"]),
            elapsed_time_s=int(payload["elapsed_time"]),
            avg_hr=_optional_float(payload.get("average_heartrate")),
            max_hr=_optional_float(payload.get("max_heartrate")),
            avg_speed=_optional_float(payload.get("average_speed")),
            total_elevation_gain=_optional_float(payload.get("total_elevation_gain")),
            avg_cadence=_optional_float(payload.get("average_cadence")),
        )
    except (KeyError, TypeError, ValueError) as exc:
        activity_id = payload.get("id", "<unknown>")
        msg = f"Could not parse Strava activity {activity_id}: {exc}"
        raise IngestError(msg) from exc


def stream_points_from_payload(payload: Mapping[str, Any]) -> list[StreamPoint]:
    """Build stream samples from a Strava streams response.

    Expects the keyed-by-type shape, which is what
    :meth:`~longrun.ingest.strava.client.StravaClient.fetch_streams`
    requests. The `time` stream defines the sample count; other streams are
    read positionally and padded with None where they are shorter, which
    happens when a sensor drops out mid-run.

    Args:
        payload: The streams response for one activity.

    Returns:
        One :class:`StreamPoint` per sample, empty if the activity has no
        time stream (manual entries, for instance).
    """
    series = _normalise_streams(payload)
    times = series.get("time")
    if not times:
        return []

    points: list[StreamPoint] = []
    for index, raw_time in enumerate(times):
        if raw_time is None:
            continue
        fields = {
            field: _optional_float(_at(series.get(key), index))
            for key, field in _STREAM_FIELD_BY_KEY.items()
        }
        points.append(StreamPoint(t_s=int(raw_time), **fields))
    return points


def _normalise_streams(payload: Mapping[str, Any]) -> dict[str, Sequence[Any]]:
    """Reduce the streams payload to a mapping of stream type -> data list.

    Entries that do not carry a usable `data` sequence are dropped rather
    than raising, so one malformed stream cannot lose an entire activity.
    """
    return {
        str(key): value["data"]
        for key, value in payload.items()
        if isinstance(value, Mapping) and isinstance(value.get("data"), Sequence)
    }


def _at(series: Sequence[Any] | None, index: int) -> Any:
    """Return `series[index]`, or None when the series is short or absent."""
    if series is None or index >= len(series):
        return None
    return series[index]


def _optional_float(value: Any) -> float | None:
    """Coerce a value to float, mapping absent or non-numeric input to None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_timestamp(value: str) -> dt.datetime:
    """Parse a Strava UTC timestamp such as `2026-05-02T12:15:09Z`."""
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
