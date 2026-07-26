"""Parse FIT files into longrun domain models.

This is the secondary source, and the only one that yields beat-to-beat R-R
intervals: watches record them in FIT `hrv` messages, and neither Strava nor
the Intervals.icu API exposes them. The v1.1 readiness work needs rMSSD, which
is computed from exactly these intervals, so the path is worth keeping even
though the API covers everything else.

R-R samples arrive in their own message stream with no timestamps of their
own. They are accumulated forwards from the activity start, then attached to
the record sample whose second they fall in, which keeps one row per second in
`activity_streams` rather than exploding it to one row per heartbeat.
"""

import datetime as dt
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import fitdecode

from longrun.errors import IngestError
from longrun.models import Activity, Source, StreamPoint

_SEMICIRCLE_TO_DEGREES = 180.0 / 2**31
_MS_PER_SECOND = 1000.0


def read_fit_file(path: Path) -> tuple[Activity, list[StreamPoint]]:
    """Parse one FIT file into an activity and its stream samples.

    Args:
        path: The `.fit` file to read.

    Returns:
        The activity and its samples, oldest sample first.

    Raises:
        IngestError: If the file cannot be parsed or carries no session.
    """
    records: list[dict[str, Any]] = []
    session: dict[str, Any] | None = None
    rr_intervals: list[float] = []

    try:
        with fitdecode.FitReader(str(path)) as reader:
            for frame in reader:
                if not isinstance(frame, fitdecode.FitDataMessage):
                    continue
                if frame.name == "record":
                    records.append(_frame_values(frame))
                elif frame.name == "session" and session is None:
                    session = _frame_values(frame)
                elif frame.name == "hrv":
                    rr_intervals.extend(_hrv_intervals(frame))
    except (fitdecode.FitError, OSError, ValueError) as exc:
        msg = f"Could not read {path.name}: {exc}"
        raise IngestError(msg) from exc

    if session is None:
        msg = f"{path.name} contains no session message, so it is not a complete activity."
        raise IngestError(msg)

    activity = _activity_from_session(session, path)
    points = _stream_points(records, activity.start_time, rr_intervals)
    return activity, points


def iter_fit_files(folder: Path) -> Iterator[Path]:
    """Yield every `.fit` file under `folder`, in a stable order.

    Args:
        folder: Directory to scan, searched recursively.

    Yields:
        Paths to FIT files, sorted so runs are reproducible.
    """
    yield from sorted(
        path
        for path in folder.rglob("*")
        if path.is_file() and path.suffix.lower() in (".fit", ".FIT".lower())
    )


def _frame_values(frame: fitdecode.FitDataMessage) -> dict[str, Any]:
    """Collect a FIT message's fields into a plain dictionary."""
    return {field.name: field.value for field in frame.fields}


def _hrv_intervals(frame: fitdecode.FitDataMessage) -> list[float]:
    """Extract R-R intervals in milliseconds from one `hrv` message.

    FIT stores these in seconds, several per message, with unused slots set
    to None.
    """
    intervals: list[float] = []
    for field in frame.fields:
        if field.name != "time":
            continue
        raw = field.value
        values = raw if isinstance(raw, list | tuple) else [raw]
        intervals.extend(
            float(value) * _MS_PER_SECOND
            for value in values
            if isinstance(value, int | float) and value > 0
        )
    return intervals


def _activity_from_session(session: dict[str, Any], path: Path) -> Activity:
    """Build an :class:`Activity` from a FIT session message."""
    start = session.get("start_time")
    if not isinstance(start, dt.datetime):
        msg = f"{path.name} has no usable session start time."
        raise IngestError(msg)

    elapsed = _as_float(session.get("total_elapsed_time")) or 0.0
    moving = _as_float(session.get("total_timer_time")) or elapsed
    return Activity(
        source=Source.FIT,
        # The filename is the stable identity here: FIT files carry no id that
        # survives a re-export, and re-reading the same file must not duplicate.
        external_id=path.stem,
        start_time=start if start.tzinfo else start.replace(tzinfo=dt.UTC),
        sport_type=str(session.get("sport") or "Unknown").title(),
        name=path.stem,
        distance_m=_as_float(session.get("total_distance")) or 0.0,
        moving_time_s=int(moving),
        elapsed_time_s=int(elapsed),
        avg_hr=_as_float(session.get("avg_heart_rate")),
        max_hr=_as_float(session.get("max_heart_rate")),
        avg_speed=_as_float(session.get("avg_speed")),
        total_elevation_gain=_as_float(session.get("total_ascent")),
        avg_cadence=_as_float(session.get("avg_running_cadence"))
        or _as_float(session.get("avg_cadence")),
    )


def _stream_points(
    records: list[dict[str, Any]], start: dt.datetime, rr_intervals: list[float]
) -> list[StreamPoint]:
    """Build stream samples, attaching R-R intervals to the second they fall in."""
    rr_by_second = _rr_by_second(rr_intervals)
    points: list[StreamPoint] = []
    for record in records:
        timestamp = record.get("timestamp")
        if not isinstance(timestamp, dt.datetime):
            continue
        moment = timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=dt.UTC)
        offset = int((moment - start).total_seconds())
        if offset < 0:
            continue
        points.append(
            StreamPoint(
                t_s=offset,
                hr=_as_float(record.get("heart_rate")),
                velocity=_as_float(record.get("speed")) or _as_float(record.get("enhanced_speed")),
                distance_m=_as_float(record.get("distance")),
                altitude=_as_float(record.get("altitude"))
                or _as_float(record.get("enhanced_altitude")),
                cadence=_as_float(record.get("cadence")),
                rr_ms=rr_by_second.get(offset),
            )
        )
    return points


def _rr_by_second(rr_intervals: list[float]) -> dict[int, float]:
    """Map each elapsed second to the mean R-R interval recorded within it.

    R-R samples have no timestamps, so their position in time is recovered by
    accumulating the intervals themselves. Where several beats fall in one
    second, their mean is kept: the alternative, one row per beat, would
    change the meaning of the streams table.
    """
    totals: dict[int, list[float]] = {}
    elapsed_ms = 0.0
    for interval in rr_intervals:
        elapsed_ms += interval
        second = int(elapsed_ms / _MS_PER_SECOND)
        totals.setdefault(second, []).append(interval)
    return {second: sum(values) / len(values) for second, values in totals.items()}


def _as_float(value: Any) -> float | None:
    """Coerce a FIT field value to float, mapping anything unusable to None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
