"""Suppressing individual wellness readings that are known to be wrong.

A watch occasionally reports a figure that is physiologically implausible -
a resting heart rate of 40 on a night of broken sleep, recorded by an optical
sensor that lost the wrist. Deleting the row is not enough, because the next
sync fetches it again; and quietly rejecting outliers by rule is worse, since
a genuinely unusual morning is exactly the reading worth keeping.

So an exclusion is recorded explicitly, one cell at a time, with a reason.
The stored value is left untouched and suppressed on the way out, which keeps
the decision reversible and visible rather than baked into the data.
"""

import sqlite3
from collections.abc import Iterable

import pandas as pd

EXCLUDABLE_FIELDS = frozenset(
    {
        "resting_hr",
        "avg_sleeping_hr",
        "hrv_rmssd",
        "sleep_score",
        "sleep_hours",
        "spo2",
        "weight_kg",
    }
)
"""Wellness columns an exclusion may name. Anything else is a typo."""


def add_exclusion(conn: sqlite3.Connection, date: str, field: str, reason: str) -> None:
    """Record that one wellness reading should be ignored.

    Args:
        conn: An open connection.
        date: The day, as `YYYY-MM-DD`.
        field: Which wellness column to suppress.
        reason: Why, in the athlete's own words. Required, so that a future
            reader can judge whether the call still stands.

    Raises:
        ValueError: If `field` is not an excludable wellness column.
    """
    _require_known(field)
    with conn:
        conn.execute(
            "INSERT INTO wellness_exclusions (date, field, reason) VALUES (?, ?, ?) "
            "ON CONFLICT (date, field) DO UPDATE SET reason = excluded.reason",
            (date, field, reason),
        )


def remove_exclusion(conn: sqlite3.Connection, date: str, field: str) -> bool:
    """Restore a previously excluded reading.

    Args:
        conn: An open connection.
        date: The day, as `YYYY-MM-DD`.
        field: Which wellness column to stop suppressing.

    Returns:
        Whether an exclusion was actually removed.
    """
    with conn:
        cursor = conn.execute(
            "DELETE FROM wellness_exclusions WHERE date = ? AND field = ?", (date, field)
        )
    return cursor.rowcount > 0


def list_exclusions(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    """Return every exclusion as `(date, field, reason)`, oldest first."""
    rows = conn.execute(
        "SELECT date, field, reason FROM wellness_exclusions ORDER BY date, field"
    ).fetchall()
    return [(str(row["date"]), str(row["field"]), str(row["reason"])) for row in rows]


def exclusions_by_field(conn: sqlite3.Connection) -> dict[str, set[str]]:
    """Return the excluded dates for each field, as ISO date strings."""
    grouped: dict[str, set[str]] = {}
    for date, field, _ in list_exclusions(conn):
        grouped.setdefault(field, set()).add(date)
    return grouped


def apply_exclusions(
    conn: sqlite3.Connection, frame: pd.DataFrame, *, date_column: str = "date"
) -> pd.DataFrame:
    """Blank out excluded cells in a wellness frame.

    The row survives with its other columns intact: excluding a resting heart
    rate is not a statement about that morning's HRV.

    Args:
        conn: An open connection.
        frame: A wellness frame with a date column.
        date_column: Name of the date column, dates or timestamps alike.

    Returns:
        A copy with the excluded cells set to null, or the frame unchanged
        when nothing applies.
    """
    grouped = exclusions_by_field(conn)
    if frame.empty or not grouped:
        return frame

    isodates = pd.to_datetime(frame[date_column], format="ISO8601").dt.strftime("%Y-%m-%d")
    cleaned = frame.copy()
    for field, dates in grouped.items():
        if field in cleaned:
            cleaned.loc[isodates.isin(dates), field] = None
    return cleaned


def _require_known(field: str) -> None:
    """Reject a field name that is not a wellness column."""
    if field not in EXCLUDABLE_FIELDS:
        known: Iterable[str] = sorted(EXCLUDABLE_FIELDS)
        msg = f"{field!r} is not a wellness field. Try one of: {', '.join(known)}."
        raise ValueError(msg)
