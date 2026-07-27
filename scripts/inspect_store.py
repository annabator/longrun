"""Report what is in the store and, more usefully, what is missing.

A sync that finishes without error can still have mapped a provider field to
nothing, leaving a column silently empty. That failure is invisible until
something downstream quietly has no data to work with, so this prints the fill
rate of every field that ingestion is supposed to populate and marks the empty
ones.

    uv run python scripts/inspect_store.py

Reads the configured database, or a path given as the first argument. Prints
counts and field coverage only - no dates, names or values from any single
activity.
"""

import sys
from pathlib import Path

from longrun.config import load_settings
from longrun.store import Store

ACTIVITY_FIELDS = (
    "avg_hr",
    "max_hr",
    "avg_speed",
    "avg_cadence",
    "total_elevation_gain",
    "feel",
    "rpe",
    "icu_load",
    "icu_intensity",
)

WELLNESS_FIELDS = (
    "resting_hr",
    "avg_sleeping_hr",
    "hrv_rmssd",
    "sleep_score",
    "sleep_hours",
    "spo2",
    "weight_kg",
)

MIN_RUNS_FOR_DETECTION = 10


def main() -> None:
    """Print a coverage report for the store."""
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else load_settings().database
    if not path.exists():
        print(f"No database at {path}. Run an ingest first.")  # noqa: T201
        raise SystemExit(1)

    with Store.open(path) as store:
        conn = store.conn
        report = _Report()

        report.section(f"store: {path}")
        for table, count in store.counts().items():
            report.line(f"{table:<20} {count:>9,}")

        report.section("activities by sport")
        rows = conn.execute(
            "SELECT sport_type, COUNT(*) n, COALESCE(SUM(distance_m), 0)/1000.0 km, "
            "SUM(icu_load) load FROM activities GROUP BY sport_type ORDER BY n DESC"
        ).fetchall()
        for row in rows:
            load = f"{row['load']:.0f}" if row["load"] is not None else "-"
            report.line(f"{row['sport_type']:<22} {row['n']:>4}  {row['km']:>8.1f} km  load {load}")

        row = conn.execute("SELECT MIN(start_time) a, MAX(start_time) b FROM activities").fetchone()
        report.section("date range")
        report.line(f"activities  {row['a']}  ->  {row['b']}")
        row = conn.execute("SELECT MIN(date) a, MAX(date) b FROM wellness_daily").fetchone()
        report.line(f"wellness    {row['a']}  ->  {row['b']}")

        activities = store.counts()["activities"]
        report.section("activity fields (populated / total)")
        for column in ACTIVITY_FIELDS:
            filled = conn.execute(f"SELECT COUNT({column}) n FROM activities").fetchone()["n"]
            report.field(column, filled, activities)

        wellness = store.counts()["wellness_daily"]
        report.section("wellness fields (populated / total)")
        for column in WELLNESS_FIELDS:
            filled = conn.execute(f"SELECT COUNT({column}) n FROM wellness_daily").fetchone()["n"]
            report.field(column, filled, wellness)

        report.section("streams")
        with_streams = conn.execute(
            "SELECT COUNT(DISTINCT activity_id) n FROM activity_streams"
        ).fetchone()["n"]
        with_distance = conn.execute(
            "SELECT COUNT(DISTINCT activity_id) n FROM activity_streams "
            "WHERE distance_m IS NOT NULL"
        ).fetchone()["n"]
        with_rr = conn.execute(
            "SELECT COUNT(DISTINCT activity_id) n FROM activity_streams WHERE rr_ms IS NOT NULL"
        ).fetchone()["n"]
        report.line(f"activities with streams        {with_streams:>4} / {activities}")
        report.line(f"  ... carrying distance        {with_distance:>4}  (needed to detect a 5k)")
        report.line(f"  ... carrying R-R intervals   {with_rr:>4}  (needs --rr or `ingest fit`)")

        report.section("what the plan engine has to work with")
        runs = conn.execute(
            "SELECT COUNT(*) n FROM activities WHERE LOWER(sport_type) IN "
            "('run', 'trailrun', 'virtualrun', 'running')"
        ).fetchone()["n"]
        with_peak = conn.execute(
            "SELECT COUNT(max_hr) n FROM activities WHERE LOWER(sport_type) IN "
            "('run', 'trailrun', 'virtualrun', 'running')"
        ).fetchone()["n"]
        report.line(f"running activities             {runs:>4}")
        report.line(
            f"  ... with max_hr                {with_peak:>4}  "
            f"({MIN_RUNS_FOR_DETECTION} needed to calibrate the effort gate)"
        )
        report.line(f"logged 5k results              {store.counts()['tests']:>4}")

        if report.empty_fields:
            report.section("empty - these may be missing field aliases")
            for name in report.empty_fields:
                report.line(name)

    print(report.render())  # noqa: T201 - developer script


class _Report:
    """Collects lines and remembers which fields came back empty."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.empty_fields: list[str] = []

    def section(self, title: str) -> None:
        """Start a new section."""
        if self.lines:
            self.lines.append("")
        self.lines.append(f"=== {title} ===")

    def line(self, text: str) -> None:
        """Add an indented line."""
        self.lines.append(f"  {text}")

    def field(self, name: str, filled: int, total: int) -> None:
        """Add a fill-rate line, flagging anything wholly empty.

        A field is only worth flagging when there were rows it could have
        been filled from: an empty table says nothing about the mapping.
        """
        missing = total and not filled
        flag = "!!" if missing else "  "
        self.lines.append(f"  {flag} {name:<22} {filled:>5} / {total}")
        if missing:
            self.empty_fields.append(name)

    def render(self) -> str:
        """Return the whole report."""
        return "\n".join(self.lines)


if __name__ == "__main__":
    main()
