"""Load stored records into pandas frames for reporting.

This module is the boundary where rows become frames. It also holds the few
derived series the dashboard needs - a rolling average, a running personal
best - so that every figure is drawn from values already computed here, and
the plotting code stays free of analysis.

Deliberately absent: correlation coefficients, regressions and any other
inferential statistic. The health metrics are sampled monthly at best, and a
coefficient computed over a handful of irregular points reads as authoritative
while being nothing of the sort. The dashboard shows the series and lets the
reader look.
"""

import sqlite3
from dataclasses import dataclass

import pandas as pd

from longrun.models import Metric
from longrun.store import Store

RESTING_HR_WINDOW = "28D"
"""Averaging window for resting HR, which is daily and noisy."""

RESTING_HR_WINDOW_LABEL = "28-day average"
"""How that window is named on screen; the offset string is not user-facing."""

_DURATION_UNIT = "s"


@dataclass(frozen=True)
class ReportData:
    """Everything the dashboard draws, loaded once.

    Attributes:
        activities: One row per stored activity, oldest first.
        wellness: One row per stored wellness day, oldest first.
        tests: Long-format measurements, oldest first.
    """

    activities: pd.DataFrame
    wellness: pd.DataFrame
    tests: pd.DataFrame

    @property
    def is_empty(self) -> bool:
        """Whether there is nothing at all to report on."""
        return self.activities.empty and self.wellness.empty and self.tests.empty


def load_report_data(store: Store) -> ReportData:
    """Load every table the dashboard needs.

    Args:
        store: An initialised store.

    Returns:
        Frames for activities, wellness and tests.
    """
    return ReportData(
        activities=load_activities(store.conn),
        wellness=load_wellness(store.conn),
        tests=load_tests(store.conn),
    )


def load_activities(conn: sqlite3.Connection) -> pd.DataFrame:
    """Read activities, oldest first, with parsed timestamps.

    Args:
        conn: An open connection.

    Returns:
        A frame with a timezone-aware `start_time` column.
    """
    frame = pd.read_sql_query("SELECT * FROM activities ORDER BY start_time", conn)
    if frame.empty:
        return frame
    frame["start_time"] = pd.to_datetime(frame["start_time"], format="ISO8601", utc=True)
    return frame


def load_wellness(conn: sqlite3.Connection) -> pd.DataFrame:
    """Read daily wellness rows, oldest first, with parsed dates.

    Args:
        conn: An open connection.

    Returns:
        A frame with a datetime `date` column.
    """
    frame = pd.read_sql_query("SELECT * FROM wellness_daily ORDER BY date", conn)
    if frame.empty:
        return frame
    frame["date"] = pd.to_datetime(frame["date"], format="ISO8601")
    return frame


def load_tests(conn: sqlite3.Connection) -> pd.DataFrame:
    """Read long-format test results, oldest first, with parsed dates.

    Args:
        conn: An open connection.

    Returns:
        A frame with a datetime `date` column.
    """
    frame = pd.read_sql_query("SELECT * FROM tests ORDER BY date, metric", conn)
    if frame.empty:
        return frame
    frame["date"] = pd.to_datetime(frame["date"], format="ISO8601")
    return frame


def metric_series(tests: pd.DataFrame, metric: str) -> pd.DataFrame:
    """Return the rows for one metric, oldest first.

    Args:
        tests: The long-format tests frame.
        metric: Metric name to select.

    Returns:
        A frame with `date`, `value` and `unit` columns; empty if the metric
        has never been recorded.
    """
    if tests.empty:
        return pd.DataFrame(columns=["date", "value", "unit"])
    selected = tests.loc[tests["metric"] == metric, ["date", "value", "unit"]]
    return selected.sort_values("date").reset_index(drop=True)


def mark_personal_bests(series: pd.DataFrame) -> pd.DataFrame:
    """Flag each row that improved on every previous result.

    A duration metric improves by getting smaller and everything else by
    getting larger, decided from the unit rather than the metric name so that
    a new duration metric needs no code change.

    Args:
        series: Output of :func:`metric_series`.

    Returns:
        The frame with an added boolean `is_pb` column.
    """
    if series.empty:
        return series.assign(is_pb=pd.Series(dtype=bool))

    lower_is_better = bool((series["unit"] == _DURATION_UNIT).all())
    running = series["value"].cummin() if lower_is_better else series["value"].cummax()
    # The first result is a personal best by default; later ones must strictly
    # improve, so equalling a best does not re-flag it.
    improved = series["value"] != running.shift(1)
    is_pb = (series["value"] == running) & (improved | (series.index == 0))
    return series.assign(is_pb=is_pb)


def resting_hr_trend(wellness: pd.DataFrame) -> pd.DataFrame:
    """Return daily resting HR alongside its rolling average.

    Args:
        wellness: The wellness frame.

    Returns:
        A frame with `date`, `resting_hr` and `rolling` columns, restricted to
        days where resting HR was recorded.
    """
    if wellness.empty or "resting_hr" not in wellness:
        return pd.DataFrame(columns=["date", "resting_hr", "rolling"])

    daily = wellness.loc[wellness["resting_hr"].notna(), ["date", "resting_hr"]]
    if daily.empty:
        return daily.assign(rolling=pd.Series(dtype=float))

    rolling = daily.set_index("date")["resting_hr"].rolling(RESTING_HR_WINDOW, min_periods=3).mean()
    return daily.assign(rolling=rolling.to_numpy())


def weekly_volume(activities: pd.DataFrame) -> pd.DataFrame:
    """Aggregate activity distance into weekly totals.

    Args:
        activities: The activities frame.

    Returns:
        A frame with `week_start` and `distance_km` columns.
    """
    if activities.empty:
        return pd.DataFrame(columns=["week_start", "distance_km"])

    weekly = (
        activities.set_index("start_time")["distance_m"]
        .resample("W-MON", label="left", closed="left")
        .sum()
        .div(1000.0)
    )
    week_starts = pd.DatetimeIndex(weekly.index).tz_localize(None)
    return pd.DataFrame({"week_start": week_starts, "distance_km": weekly.to_numpy()})


def format_duration(seconds: float) -> str:
    """Render seconds as `mm:ss`, or `h:mm:ss` beyond an hour.

    Args:
        seconds: A duration in seconds.

    Returns:
        A human-readable duration.
    """
    total = round(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def known_metrics_present(tests: pd.DataFrame) -> set[str]:
    """Return which recognised metrics appear in the data.

    Args:
        tests: The long-format tests frame.

    Returns:
        The subset of :class:`~longrun.models.Metric` values present.
    """
    if tests.empty:
        return set()
    recorded = set(tests["metric"].unique())
    return {metric.value for metric in Metric if metric.value in recorded}
