"""A separate, restricted store for cycle tracking.

This is the most sensitive data the project holds, and it lives in its own
database file rather than a table in `longrun.db`. That separation is the
point: the main store can be copied, shared for debugging or backed up
somewhere only partly trusted, and none of this travels with it.

Three properties are deliberate and should not be relaxed:

* **Manual entry only.** Nothing in this module reaches a network, and no
  ingestion adapter writes here. There is no export command.
* **Least possible data.** One table of dates. No flow, no symptoms, no
  derived phase - phases are computed at read time in
  :mod:`longrun.analysis.cycle` and never stored.
* **Owner-only on disk.** The directory is created `0700` and the database
  `0600`, so another account on the same machine cannot read it.
"""

import datetime as dt
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Self

CYCLE_SCHEMA_VERSION = 1
"""Versioned separately from the main store, which it shares nothing with."""

_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS schema_meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    # One column on purpose. A logged day is the whole of what is recorded;
    # cycle day and phase are derived when needed and never written down.
    """
    CREATE TABLE IF NOT EXISTS period_days (
        date TEXT PRIMARY KEY
    )
    """,
)

_DIR_MODE = 0o700
_FILE_MODE = 0o600


def connect_private(path: Path) -> sqlite3.Connection:
    """Open the cycle database, creating it with owner-only permissions.

    Args:
        path: Location of the database file.

    Returns:
        An open connection.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(_DIR_MODE)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # Set before anything is written, so the file is never briefly readable.
    path.touch(mode=_FILE_MODE, exist_ok=True)
    path.chmod(_FILE_MODE)
    return conn


def init_private_db(conn: sqlite3.Connection) -> None:
    """Create the cycle schema if it does not already exist.

    Args:
        conn: An open connection.
    """
    with conn:
        for statement in _DDL:
            conn.execute(statement)
        conn.execute(
            """
            INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?)
            ON CONFLICT (key) DO UPDATE SET value = excluded.value
            """,
            (str(CYCLE_SCHEMA_VERSION),),
        )


class CycleStore:
    """Reads and writes logged period days.

    Deliberately small. Anything that interprets these dates belongs in
    :mod:`longrun.analysis.cycle`, so that what is stored stays raw.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        """Wrap an already-open connection.

        Args:
            conn: An open connection to the cycle database.
        """
        self.conn = conn

    @classmethod
    @contextmanager
    def open(cls, path: Path) -> Iterator[Self]:
        """Open an initialised cycle store at `path`.

        Args:
            path: Location of the database file; created if absent.

        Yields:
            A ready-to-use store.
        """
        conn = connect_private(path)
        try:
            init_private_db(conn)
            yield cls(conn)
        finally:
            conn.close()

    def log_days(self, days: Iterable[dt.date]) -> int:
        """Record one or more days as period days.

        Logging a day twice is a no-op, so a command can be repeated without
        creating duplicates.

        Args:
            days: Dates to record.

        Returns:
            The number of days newly recorded.
        """
        rows = [(day.isoformat(),) for day in days]
        if not rows:
            return 0
        before = self.count()
        with self.conn:
            self.conn.executemany(
                "INSERT INTO period_days (date) VALUES (?) ON CONFLICT (date) DO NOTHING", rows
            )
        return self.count() - before

    def remove_days(self, days: Iterable[dt.date]) -> int:
        """Remove logged days, for correcting a mistake.

        Args:
            days: Dates to remove.

        Returns:
            The number of days removed.
        """
        rows = [(day.isoformat(),) for day in days]
        if not rows:
            return 0
        before = self.count()
        with self.conn:
            self.conn.executemany("DELETE FROM period_days WHERE date = ?", rows)
        return before - self.count()

    def logged_days(
        self, since: dt.date | None = None, until: dt.date | None = None
    ) -> list[dt.date]:
        """Return every logged day in range, oldest first.

        Args:
            since: Earliest date to include, inclusive.
            until: Latest date to include, inclusive.

        Returns:
            The logged dates, sorted.
        """
        clauses: list[str] = []
        params: list[str] = []
        if since is not None:
            clauses.append("date >= ?")
            params.append(since.isoformat())
        if until is not None:
            clauses.append("date <= ?")
            params.append(until.isoformat())
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"SELECT date FROM period_days{where} ORDER BY date", params
        ).fetchall()
        return [dt.date.fromisoformat(str(row["date"])) for row in rows]

    def count(self) -> int:
        """Return how many days are logged.

        Returns:
            The row count.
        """
        row = self.conn.execute("SELECT COUNT(*) AS n FROM period_days").fetchone()
        return int(row["n"])
