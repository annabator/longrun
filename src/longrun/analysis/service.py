"""Assemble stored rows into the inputs the analysis modules expect.

Keeps the reading of tables in one place, so that the analysis modules stay
pure functions over plain values and remain testable without a database.
"""

import datetime as dt
import math
from typing import Any

import pandas as pd

from longrun.analysis.anchor import ANCHOR_WINDOW_DAYS, DetectedEffort, hard_effort_threshold
from longrun.analysis.plan import SIZING_WINDOW_WEEKS
from longrun.analysis.readiness import DailyReading
from longrun.analysis.splits import best_split
from longrun.models import is_running_sport
from longrun.store import Store
from longrun.store.exclusions import apply_exclusions


def daily_readings(store: Store, since: dt.date) -> list[DailyReading]:
    """Build readiness inputs from wellness and the sessions around them.

    A post-session feel, where one exists, is attached to the day *after* the
    session it describes, since that is the morning its cost shows up in.

    Args:
        store: An initialised store.
        since: Earliest date to include.

    Returns:
        One reading per day with wellness logged, oldest first.
    """
    wellness = pd.read_sql_query(
        "SELECT date, hrv_rmssd, resting_hr, sleep_hours FROM wellness_daily "
        "WHERE date >= ? ORDER BY date",
        store.conn,
        params=(since.isoformat(),),
    )
    if wellness.empty:
        return []
    wellness = apply_exclusions(store.conn, wellness)

    sessions = pd.read_sql_query(
        "SELECT start_time, feel FROM activities WHERE start_time >= ?",
        store.conn,
        params=(since.isoformat(),),
    )
    worst = _worst_feel_by_day(sessions)

    readings: list[DailyReading] = []
    for row in wellness.itertuples():
        day = dt.date.fromisoformat(str(row.date))
        # Yesterday's session is what this morning is reacting to.
        yesterday = day - dt.timedelta(days=1)
        readings.append(
            DailyReading(
                date=day,
                hrv_rmssd=_optional(row.hrv_rmssd),
                resting_hr=_optional(row.resting_hr),
                sleep_hours=_optional(row.sleep_hours),
                feel=worst.get(yesterday),
            )
        )
    return readings


def detect_five_k_efforts(
    store: Store, today: dt.date, window_days: int = ANCHOR_WINDOW_DAYS
) -> list[DetectedEffort]:
    """Scan recent runs for the fastest 5k inside each.

    This is how a parkrun is found without knowing it was one. The watch
    names everything the same, so the trace is what identifies the effort:
    the quickest continuous 5000 m in the activity, which excludes a jog to
    the start line and any cool-down afterwards.

    Args:
        store: An initialised store.
        today: The date the window is measured back from.
        window_days: How far back an activity may be.

    Returns:
        One effort per run that yielded a split, oldest first. Whether an
        effort was hard enough to anchor on is decided by
        :attr:`~longrun.analysis.anchor.DetectedEffort.is_hard`.
    """
    earliest = today - dt.timedelta(days=window_days)
    runs = pd.read_sql_query(
        "SELECT id, start_time, name, sport_type, max_hr FROM activities "
        "WHERE start_time >= ? ORDER BY start_time",
        store.conn,
        params=(earliest.isoformat(),),
    )
    if runs.empty:
        return []
    runs = runs.loc[runs["sport_type"].astype(str).map(is_running_sport)]
    if runs.empty:
        return []

    threshold = hard_effort_threshold(runs)
    identifiers = runs["id"].astype(int).tolist()
    starts = runs["start_time"].astype(str).tolist()
    names = runs["name"].astype(str).tolist()

    efforts: list[DetectedEffort] = []
    for activity_id, start, name in zip(identifiers, starts, names, strict=True):
        streams = pd.read_sql_query(
            "SELECT t_s, distance_m, hr FROM activity_streams WHERE activity_id = ? ORDER BY t_s",
            store.conn,
            params=(activity_id,),
        )
        split = best_split(streams)
        if split is None:
            continue
        started = pd.Timestamp(start).to_pydatetime()
        efforts.append(
            DetectedEffort(
                date=started.date(),
                duration_s=split.duration_s,
                avg_hr=split.avg_hr,
                hard_threshold=threshold,
                weekday=started.weekday(),
                activity_name=name,
            )
        )
    return efforts


def recent_run_distances(
    store: Store, today: dt.date, weeks: int = SIZING_WINDOW_WEEKS
) -> list[float]:
    """Return the distances of recent runs, in kilometres.

    Only running counts: a ride's distance says nothing about how far a run
    should be. An activity with no distance upstream is stored as zero, and
    a zero is dropped rather than dragging the median down.

    Args:
        store: An initialised store.
        today: The day the plan is being built for.
        weeks: How far back to look.

    Returns:
        Distances in kilometres, oldest first.
    """
    since = today - dt.timedelta(weeks=weeks)
    rows = store.conn.execute(
        "SELECT sport_type, distance_m FROM activities WHERE start_time >= ? ORDER BY start_time",
        (since.isoformat(),),
    ).fetchall()

    distances: list[float] = []
    for row in rows:
        if not is_running_sport(row["sport_type"]):
            continue
        metres = _optional(row["distance_m"])
        if metres:
            distances.append(metres / 1000.0)
    return distances


def completed_session_names(store: Store, week_start: dt.date) -> list[str]:
    """Return the names of activities logged during one week.

    Used to mark menu items done. Matching is by name, which is loose on
    purpose: naming a run after the session is a habit worth encouraging
    rather than a format worth enforcing.

    Args:
        store: An initialised store.
        week_start: Monday of the week.

    Returns:
        Activity names from that week, lower-cased.
    """
    week_end = week_start + dt.timedelta(days=7)
    rows = store.conn.execute(
        "SELECT name FROM activities WHERE start_time >= ? AND start_time < ?",
        (week_start.isoformat(), week_end.isoformat()),
    ).fetchall()
    return [str(row["name"]).lower() for row in rows]


def _worst_feel_by_day(sessions: pd.DataFrame) -> dict[dt.date, float]:
    """Return the lowest post-session feel recorded on each day."""
    worst: dict[dt.date, float] = {}
    if sessions.empty:
        return worst

    for row in sessions.itertuples():
        day = pd.Timestamp(str(row.start_time)).date()
        feel = _optional(row.feel)
        if feel is not None:
            worst[day] = min(worst.get(day, feel), feel)
    return worst


def _optional(value: Any) -> float | None:
    """Coerce a stored value to float, mapping nulls and blanks to None."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) else number
