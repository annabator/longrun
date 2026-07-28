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

import datetime as dt
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field

import pandas as pd

from longrun.analysis.cycle import Cycle
from longrun.models import Metric, is_running_sport
from longrun.store import Store
from longrun.store.exclusions import apply_exclusions

DEFAULT_WINDOW_MONTHS = 6
"""How much history the panels show unless asked otherwise.

Only the view narrows. Every figure is still computed over the whole store,
so a personal best set two years ago is still marked as one - it simply sits
off the left of the axis.
"""

RESTING_HR_WINDOW = "28D"
"""Averaging window for resting HR, which is daily and noisy."""

RESTING_HR_WINDOW_LABEL = "28-day average"
"""How that window is named on screen; the offset string is not user-facing."""

HRV_BASELINE_WINDOW = "7D"
"""Baseline window for rMSSD.

Seven days is the span the v1.1 readiness work uses, so the dashboard shows
the same baseline the plan engine will reason about rather than a prettier one.
"""

HRV_BASELINE_LABEL = "7-day baseline"

_DURATION_UNIT = "s"


@dataclass(frozen=True)
class ReportData:
    """Everything the dashboard draws, loaded once.

    Attributes:
        activities: One row per stored activity, oldest first.
        wellness: One row per stored wellness day, oldest first.
        tests: Long-format measurements, oldest first.
        cycles: Cycles derived from logged period days. Empty unless the
            report was explicitly asked to include them, so a dashboard is
            free of this data unless requested.
    """

    activities: pd.DataFrame
    wellness: pd.DataFrame
    tests: pd.DataFrame
    cycles: list[Cycle] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        """Whether there is nothing at all to report on."""
        return self.activities.empty and self.wellness.empty and self.tests.empty


def load_report_data(store: Store, cycles: Sequence[Cycle] | None = None) -> ReportData:
    """Load every table the dashboard needs.

    Cycles are passed in rather than read here, because they live in a
    separate database that this module deliberately does not open. A caller
    that wants the cycle panel has to go and get them on purpose.

    Args:
        store: An initialised store.
        cycles: Cycles derived from logged period days, if the report is to
            include them.

    Returns:
        Frames for activities, wellness and tests.
    """
    return ReportData(
        activities=load_activities(store.conn),
        wellness=load_wellness(store.conn),
        tests=load_tests(store.conn),
        cycles=list(cycles or []),
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
    frame = apply_exclusions(conn, frame)
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


def wellness_series(wellness: pd.DataFrame, column: str) -> pd.DataFrame:
    """Return the days on which one wellness column was recorded.

    Args:
        wellness: The wellness frame.
        column: Column to extract.

    Returns:
        A frame with `date` and `value`, empty if the column is absent or
        never populated.
    """
    if wellness.empty or column not in wellness:
        return pd.DataFrame(columns=["date", "value"])
    recorded = wellness.loc[wellness[column].notna(), ["date", column]]
    return recorded.rename(columns={column: "value"}).reset_index(drop=True)


def rolling_baseline(series: pd.DataFrame, window: str, min_periods: int = 3) -> pd.DataFrame:
    """Add a trailing rolling mean to a date-indexed series.

    The window is a time span rather than a sample count, so gaps in a daily
    metric shrink the window instead of silently reaching further back.

    Args:
        series: Frame with `date` and `value`, oldest first.
        window: Pandas offset string, such as `28D`.
        min_periods: Minimum samples before a value is produced.

    Returns:
        The frame with an added `rolling` column.
    """
    if series.empty:
        return series.assign(rolling=pd.Series(dtype=float))
    rolling = series.set_index("date")["value"].rolling(window, min_periods=min_periods).mean()
    return series.assign(rolling=rolling.to_numpy())


def by_cycle_day(
    frame: pd.DataFrame, date_column: str, value_column: str, cycles: Sequence[Cycle]
) -> pd.DataFrame:
    """Place each row of a series on its cycle day.

    Rows outside any known cycle are dropped rather than guessed at.

    Args:
        frame: Any frame carrying a date column and a value column.
        date_column: Name of the date column.
        value_column: Name of the value column.
        cycles: Cycles derived from logged period days.

    Returns:
        A frame with `cycle_day`, `cycle_start` and `value`, empty when
        nothing lines up.
    """
    empty = pd.DataFrame(columns=["cycle_day", "cycle_start", "value"])
    if frame.empty or not cycles or value_column not in frame:
        return empty

    recorded = frame.loc[frame[value_column].notna(), [date_column, value_column]]
    if recorded.empty:
        return empty

    rows: list[dict[str, object]] = []
    for moment, value in zip(recorded[date_column], recorded[value_column], strict=True):
        day = pd.Timestamp(moment).date()
        cycle = next((candidate for candidate in cycles if candidate.contains(day)), None)
        if cycle is None:
            continue
        rows.append(
            {
                "cycle_day": (day - cycle.start).days + 1,
                "cycle_start": cycle.start,
                "value": float(value),
            }
        )
    return pd.DataFrame(rows, columns=["cycle_day", "cycle_start", "value"]) if rows else empty


def weekly_volume(activities: pd.DataFrame) -> pd.DataFrame:
    """Aggregate activity distance into weekly totals.

    Args:
        activities: The activities frame.

    Returns:
        A frame with `week_start` and `distance_km` columns.
    """
    runs = running_activities(activities)
    if runs.empty:
        return pd.DataFrame(columns=["week_start", "distance_km"])

    weekly = (
        runs.set_index("start_time")["distance_m"]
        .resample("W-MON", label="left", closed="left")
        .sum()
        .div(1000.0)
    )
    week_starts = pd.DatetimeIndex(weekly.index).tz_localize(None)
    return pd.DataFrame({"week_start": week_starts, "distance_km": weekly.to_numpy()})


def report_window(
    data: "ReportData", since: dt.date | None, *, whole_history: bool = False
) -> tuple[dt.date, dt.date] | None:
    """Return the date range every panel should share.

    Setting one explicit range on every figure is what keeps the panels
    aligned. Letting each auto-scale to its own data would put a series with
    a week of readings on a different timeline from one with two years, which
    is precisely what makes a shared-timeline view worth having.

    The range is applied to the axis rather than by filtering rows, so that
    figures computed over the whole history - a personal best, a rolling
    baseline - stay correct while the view narrows.

    Args:
        data: Loaded report frames.
        since: Earliest date to show, or None for the default window.
        whole_history: Show everything, ignoring the default window.

    Returns:
        A `(start, end)` pair, or None when no narrowing is wanted.
    """
    if whole_history:
        return None
    latest = _latest_date(data)
    start = since if since is not None else _default_start(latest)
    return start, max(latest, start)


def _default_start(latest: dt.date) -> dt.date:
    """Return the start of the default window: whole months, recent ones.

    Years of history compress the recent months into an unreadable strip at
    the right-hand edge, which is where the interesting part is. Snapping to
    the first of the month keeps the axis labels tidy and the start date
    stable as new readings arrive, rather than sliding a day at a time.
    """
    month = latest.month - DEFAULT_WINDOW_MONTHS
    year = latest.year + (month - 1) // 12
    return dt.date(year, (month - 1) % 12 + 1, 1)


def _latest_date(data: "ReportData") -> dt.date:
    """Return the most recent date anywhere in the report data."""
    candidates: list[dt.date] = []
    if not data.activities.empty:
        candidates.append(pd.Timestamp(data.activities["start_time"].max()).date())
    for frame in (data.wellness, data.tests):
        if not frame.empty:
            candidates.append(pd.Timestamp(frame["date"].max()).date())
    return max(candidates) if candidates else dt.datetime.now(tz=dt.UTC).date()


def running_activities(activities: pd.DataFrame) -> pd.DataFrame:
    """Return only the activities that are running.

    Cycling reports a distance too, so anything summing `distance_m` across
    every sport quietly counts a ride as mileage. Filtering here keeps the
    panel titled "weekly running volume" honest.

    Args:
        activities: The activities frame.

    Returns:
        The running subset, empty if there is none.
    """
    if activities.empty or "sport_type" not in activities:
        return activities
    return activities.loc[activities["sport_type"].astype(str).map(is_running_sport)]


def other_activities(activities: pd.DataFrame) -> pd.DataFrame:
    """Return the activities that are not running.

    Args:
        activities: The activities frame.

    Returns:
        The non-running subset, empty if there is none.
    """
    if activities.empty or "sport_type" not in activities:
        return activities.iloc[0:0]
    return activities.loc[~activities["sport_type"].astype(str).map(is_running_sport)]


def load_context_by_week(activities: pd.DataFrame) -> pd.DataFrame:
    """Aggregate non-running sessions into weekly counts and load.

    Cross-training still costs something, and a heavy week of it explains a
    tired run better than the running log alone does. Counted in sessions and
    in the load figure Intervals.icu computes, since distance means nothing
    across a swim, a gym session and a windsurf.

    Args:
        activities: The activities frame.

    Returns:
        A frame with `week_start`, `sessions` and `load`.
    """
    others = other_activities(activities)
    columns = ["week_start", "sessions", "load"]
    if others.empty:
        return pd.DataFrame(columns=columns)

    indexed = others.set_index("start_time")
    weekly = indexed.resample("W-MON", label="left", closed="left")
    sessions = weekly.size()
    load = (
        weekly["icu_load"].sum() if "icu_load" in others else pd.Series(0.0, index=sessions.index)
    )
    week_starts = pd.DatetimeIndex(sessions.index).tz_localize(None)
    return pd.DataFrame(
        {"week_start": week_starts, "sessions": sessions.to_numpy(), "load": load.to_numpy()}
    )


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
