"""Command-line entry point for longrun.

Commands are thin: they resolve configuration, open the store, call into the
ingestion or reporting layers, and report what happened. Anything a user could
reasonably get wrong - missing credentials, an unparseable CSV, an exhausted
rate limit - arrives as a :class:`~longrun.errors.LongrunError` and is printed
as a plain message rather than a traceback.
"""

import datetime as dt
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

import httpx
import typer

from longrun import __version__
from longrun.analysis import (
    Cycle,
    cycles_from_days,
    median_cycle_length,
    ovulation_estimate,
    phase_for,
)
from longrun.analysis.anchor import Confidence, DetectedEffort, anchor_paces, find_anchor
from longrun.analysis.goals import Emphasis, load_goals, save_goals
from longrun.analysis.plan import WeeklyMenu, build_menu, week_start_for
from longrun.analysis.readiness import Readiness
from longrun.analysis.readiness import assess as readiness_assess
from longrun.analysis.service import (
    completed_session_names,
    daily_readings,
    detect_five_k_efforts,
)
from longrun.config import Settings, load_settings
from longrun.errors import IngestError, LongrunError
from longrun.ingest.base import IngestSummary
from longrun.ingest.fit import FitFolderIngester
from longrun.ingest.intervals import IntervalsClient, IntervalsIngester
from longrun.ingest.manual import load_tests_csv, load_wellness_csv
from longrun.ingest.strava import StravaAuthenticator, StravaClient, StravaIngester, TokenStore
from longrun.ingest.strava.auth import DEFAULT_SCOPE, build_authorize_url
from longrun.report import build_report, load_report_data
from longrun.report.frames import load_tests
from longrun.store import Store
from longrun.store.private import CycleStore

app = typer.Typer(
    name="longrun",
    help="Holistic, race-less endurance-training analytics.",
    no_args_is_help=True,
    add_completion=False,
)
ingest_app = typer.Typer(
    help="Load activities and health metrics into the store. Intervals.icu is the default source.",
    no_args_is_help=True,
)
auth_app = typer.Typer(help="Authorise longrun against a data provider.", no_args_is_help=True)
cycle_app = typer.Typer(
    help="Log period days and see the cycle derived from them. "
    "Stored separately, owner-only, never committed and never sent anywhere.",
    no_args_is_help=True,
)
app.add_typer(ingest_app, name="ingest")
app.add_typer(auth_app, name="auth")
app.add_typer(cycle_app, name="cycle")

READINESS_LOOKBACK_DAYS = 30
"""How much history to load when judging readiness for one day."""


def _version_callback(value: bool) -> None:
    """Print the installed version and exit."""
    if value:
        typer.echo(f"longrun {__version__}")
        raise typer.Exit


@app.callback()
def main(
    ctx: typer.Context,
    db: Annotated[
        Path | None,
        typer.Option("--db", help="SQLite database to use. Defaults to data/longrun.db."),
    ] = None,
    version: Annotated[  # noqa: ARG001 - consumed by its eager callback, before the body runs
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True, help="Show version."),
    ] = False,
) -> None:
    """Holistic, race-less endurance-training analytics."""
    settings = load_settings()
    if db is not None:
        settings = settings.model_copy(update={"db_path": db})
    ctx.obj = settings


@auth_app.command("strava")
def auth_strava(ctx: typer.Context) -> None:
    """Authorise against Strava and cache the resulting tokens.

    Prints the URL to approve the application, then exchanges the `code`
    parameter from the redirect for a token pair. Run this once; afterwards
    ingestion refreshes tokens on its own.
    """
    settings = _settings(ctx)
    with _friendly_errors():
        client_id, _ = settings.strava.require_credentials()
        url = build_authorize_url(client_id, settings.strava.redirect_uri, DEFAULT_SCOPE)

        typer.echo("Open this URL and approve access:\n")
        typer.echo(f"  {url}\n")
        typer.echo(
            "You will be redirected to a URL containing `code=...`. The page itself\n"
            "may fail to load - that is expected, only the address bar matters.\n"
        )
        code = typer.prompt("Paste the code value here").strip()

        with httpx.Client(timeout=30.0) as http:
            authenticator = StravaAuthenticator(settings.strava, TokenStore(settings.tokens))
            bundle = authenticator.authorize(http, code)
        typer.echo(f"\nAuthorised. Tokens cached in {settings.tokens}")
        typer.echo(f"Access token valid until {bundle.expires_at:%Y-%m-%d %H:%M %Z}")


@ingest_app.command("intervals")
def ingest_intervals_command(
    ctx: typer.Context,
    since: Annotated[
        dt.datetime | None,
        typer.Option(
            "--since",
            formats=["%Y-%m-%d"],
            help="Earliest date to fetch. Defaults to resuming from what is stored.",
        ),
    ] = None,
    limit: Annotated[
        int | None,
        typer.Option("--limit", help="Stop after this many activities."),
    ] = None,
    streams: Annotated[
        bool,
        typer.Option("--streams/--no-streams", help="Also fetch per-activity streams."),
    ] = True,
    wellness: Annotated[
        bool,
        typer.Option("--wellness/--no-wellness", help="Also fetch daily wellness."),
    ] = True,
    rr: Annotated[
        bool,
        typer.Option(
            "--rr/--no-rr",
            help="Download each activity file for beat-to-beat R-R intervals. "
            "Off by default: it is the largest request made and nothing "
            "consumes the data yet.",
        ),
    ] = False,
) -> None:
    """Pull activities, streams and daily wellness from Intervals.icu.

    This is the default source. Re-running is safe and incremental: streams
    are fetched once per activity, not on every sync.

    Beat-to-beat R-R intervals are available with `--rr` but off by default,
    since downloading an activity file per run is expensive and nothing reads
    the result yet.
    """
    settings = _settings(ctx)
    with _friendly_errors(), Store.open(settings.database) as store:
        athlete_id, api_key = settings.intervals.require_credentials()
        client = IntervalsClient(athlete_id, api_key)
        try:
            ingester = IntervalsIngester(
                client,
                since=since.date() if since is not None else None,
                with_streams=streams,
                with_wellness=wellness,
                with_rr=rr,
                limit=limit,
            )
            summary = ingester.ingest(store, progress=typer.echo)
        finally:
            client.close()
        _echo_summary(summary, store)


@ingest_app.command("fit")
def ingest_fit_command(
    ctx: typer.Context,
    folder: Annotated[
        Path | None,
        typer.Option("--folder", help="Directory of .fit exports. Defaults to data/raw."),
    ] = None,
    reparse: Annotated[
        bool,
        typer.Option("--reparse", help="Re-read files whose streams are already stored."),
    ] = False,
) -> None:
    """Parse a folder of FIT files, including beat-to-beat R-R intervals.

    The only source of R-R data, which neither Strava nor the Intervals.icu
    API exposes.
    """
    settings = _settings(ctx)
    with _friendly_errors(), Store.open(settings.database) as store:
        ingester = FitFolderIngester(folder or settings.raw, skip_known=not reparse)
        summary = ingester.ingest(store, progress=typer.echo)
        _echo_summary(summary, store)


@ingest_app.command("strava")
def ingest_strava_command(
    ctx: typer.Context,
    since: Annotated[
        dt.datetime | None,
        typer.Option(
            "--since",
            formats=["%Y-%m-%d"],
            help="Only ingest activities after this date. Defaults to resuming "
            "from the latest stored activity.",
        ),
    ] = None,
    limit: Annotated[
        int | None,
        typer.Option("--limit", help="Stop after this many activities."),
    ] = None,
    streams: Annotated[
        bool,
        typer.Option("--streams/--no-streams", help="Also fetch per-activity streams."),
    ] = True,
) -> None:
    """Pull activities and their streams from Strava.

    Secondary source: Strava now requires a paid subscription for these
    endpoints. Prefer `longrun ingest intervals`.
    """
    settings = _settings(ctx)
    with _friendly_errors(), Store.open(settings.database) as store:
        after = since.replace(tzinfo=dt.UTC) if since is not None else None
        authenticator = StravaAuthenticator(settings.strava, TokenStore(settings.tokens))
        client = StravaClient(authenticator)
        try:
            ingester = StravaIngester(client, since=after, with_streams=streams, limit=limit)
            summary = ingester.ingest(store, progress=typer.echo)
        finally:
            client.close()
        _echo_summary(summary, store)


@ingest_app.command("manual")
def ingest_manual_command(
    ctx: typer.Context,
    wellness: Annotated[
        Path | None,
        typer.Option("--wellness", exists=True, dir_okay=False, help="CSV of daily wellness rows."),
    ] = None,
    tests: Annotated[
        Path | None,
        typer.Option("--tests", exists=True, dir_okay=False, help="CSV of test results."),
    ] = None,
) -> None:
    """Load manually maintained CSVs into the store.

    Re-running with the same file is safe: rows are matched on their natural
    key and updated rather than duplicated.
    """
    settings = _settings(ctx)
    if wellness is None and tests is None:
        typer.secho(
            "Pass --wellness and/or --tests with a CSV path.", fg=typer.colors.RED, err=True
        )
        raise typer.Exit(code=1)

    with _friendly_errors(), Store.open(settings.database) as store:
        if wellness is not None:
            written = store.upsert_wellness(load_wellness_csv(wellness))
            typer.echo(f"Loaded {written} wellness rows from {wellness}")
        if tests is not None:
            written = store.upsert_tests(load_tests_csv(tests))
            typer.echo(f"Loaded {written} test results from {tests}")
        _echo_counts(store)


@cycle_app.command("log")
def cycle_log_command(
    ctx: typer.Context,
    dates: Annotated[
        list[str] | None,
        typer.Argument(
            help="Dates to log as period days (YYYY-MM-DD). Defaults to today.",
        ),
    ] = None,
    days: Annotated[
        int,
        typer.Option("--days", min=1, help="Log this many consecutive days from the first date."),
    ] = 1,
) -> None:
    """Record period days.

    Stored in a separate, owner-only database that nothing else reads and no
    command exports. Logging the same day twice changes nothing.
    """
    settings = _settings(ctx)
    with _friendly_errors():
        first = _parse_dates(dates) or [_today()]
        wanted = sorted(
            {day for start in first for day in _consecutive(start, days if len(first) == 1 else 1)}
        )
        with CycleStore.open(settings.cycle_database) as store:
            added = store.log_days(wanted)
        typer.echo(f"Logged {added} new day(s); {len(wanted) - added} were already recorded.")
        typer.echo(f"Stored in {settings.cycle_database} (owner-only, never committed).")


@cycle_app.command("list")
def cycle_list_command(
    ctx: typer.Context,
    limit: Annotated[int, typer.Option("--limit", min=1, help="Show this many recent cycles.")] = 6,
) -> None:
    """Show the cycles derived from the logged days.

    Phases are computed here and never stored, and each cycle is reported with
    how far its estimate can be trusted.
    """
    settings = _settings(ctx)
    with _friendly_errors():
        with CycleStore.open(settings.cycle_database) as store:
            logged = store.logged_days()
        if not logged:
            typer.echo("No period days logged yet. Use `longrun cycle log`.")
            return

        cycles = cycles_from_days(logged)
        median = median_cycle_length(cycles)
        typer.echo(f"{len(logged)} logged days across {len(cycles)} cycles")
        typer.echo(f"Median completed cycle: {median} days" if median else "No completed cycle yet")
        typer.echo("")
        for cycle in cycles[-limit:]:
            length = f"{cycle.length_days} days" if cycle.length_days else "in progress"
            ovulation = ovulation_estimate(cycle, median)
            estimate = phase_for(cycle.start, cycles)
            typer.echo(
                f"  {cycle.start:%Y-%m-%d}  {length:<12} "
                f"period {cycle.period_days}d  "
                f"ovulation ~{ovulation:%Y-%m-%d}  [{estimate.confidence}]"
            )
        typer.echo("\nPhases are soft estimates; see `longrun cycle today` for the reasoning.")


@cycle_app.command("today")
def cycle_today_command(
    ctx: typer.Context,
    date: Annotated[
        str | None, typer.Argument(help="Date to describe (YYYY-MM-DD). Defaults to today.")
    ] = None,
) -> None:
    """Describe where one date sits in the cycle, and how sure that is."""
    settings = _settings(ctx)
    with _friendly_errors():
        day = _parse_dates([date] if date else None)
        target = day[0] if day else _today()
        with CycleStore.open(settings.cycle_database) as store:
            logged = store.logged_days()

        estimate = phase_for(target, cycles_from_days(logged))
        typer.echo(f"{target:%Y-%m-%d}")
        typer.echo(f"  phase      {estimate.phase}")
        typer.echo(f"  cycle day  {estimate.cycle_day if estimate.cycle_day else '-'}")
        typer.echo(f"  confidence {estimate.confidence}")
        typer.echo(f"  because    {estimate.reason}")


@cycle_app.command("rm")
def cycle_remove_command(
    ctx: typer.Context,
    dates: Annotated[list[str], typer.Argument(help="Dates to remove (YYYY-MM-DD).")],
) -> None:
    """Remove logged period days, for correcting a mistake."""
    settings = _settings(ctx)
    with _friendly_errors():
        wanted = _parse_dates(dates)
        with CycleStore.open(settings.cycle_database) as store:
            removed = store.remove_days(wanted)
        typer.echo(f"Removed {removed} day(s).")


@app.command("goals")
def goals_command(
    ctx: typer.Context,
    emphasis: Annotated[
        Emphasis | None, typer.Option("--emphasis", help="What this block is for.")
    ] = None,
    runs: Annotated[
        int | None, typer.Option("--runs", min=1, max=14, help="Runs wanted per week.")
    ] = None,
    quality: Annotated[
        int | None, typer.Option("--quality", min=0, max=7, help="Hard sessions per week.")
    ] = None,
    long_run: Annotated[
        bool | None, typer.Option("--long-run/--no-long-run", help="Want a weekly long run.")
    ] = None,
    constraints: Annotated[
        str | None, typer.Option("--constraints", help="Free text, kept alongside the plan.")
    ] = None,
) -> None:
    """Show or update the training goals the plan is built around.

    With no options this prints what is currently set.
    """
    settings = _settings(ctx)
    with _friendly_errors():
        current = load_goals(settings.goals_path)
        updates = {
            key: value
            for key, value in {
                "emphasis": emphasis,
                "runs_per_week": runs,
                "quality_sessions": quality,
                "long_run": long_run,
                "constraints": constraints,
            }.items()
            if value is not None
        }
        if updates:
            current = current.model_copy(update=updates)
            save_goals(current, settings.goals_path)
            typer.echo(f"Saved to {settings.goals_path}")

        typer.echo(current.describe())
        if current.constraints:
            typer.echo(f"constraints: {current.constraints}")


@app.command("anchor")
def anchor_command(
    ctx: typer.Context,
    candidates: Annotated[
        bool,
        typer.Option("--candidates", help="List the 5k efforts found in recent runs."),
    ] = False,
) -> None:
    """Recompute training paces from the best recent 5k.

    Uses published formulas rather than copyrighted tables. A logged parkrun
    always wins; failing that, the fastest hard 5k found inside a recent run
    is used, since a watch names everything the same and the trace is what
    identifies the effort.
    """
    settings = _settings(ctx)
    with _friendly_errors(), Store.open(settings.database) as store:
        today = _today()
        detected = detect_five_k_efforts(store, today)

        if candidates:
            _echo_candidates(detected)
            return

        found = find_anchor(load_tests(store.conn), today, detected=detected)
        if found is None:
            typer.secho(
                "Nothing to anchor paces on: no 5k logged in the last few months, "
                "and no hard 5k found inside a recent run. Log a parkrun with "
                "`longrun ingest manual --tests`, or see `longrun anchor --candidates`.",
                fg=typer.colors.YELLOW,
                err=True,
            )
            raise typer.Exit(code=1)

        typer.echo(found.describe())
        typer.echo("")
        for line in anchor_paces(found):
            typer.echo(f"  {line}")
        if found.confidence is Confidence.DETECTED:
            typer.secho(
                "\nThis came from a GPS trace, not a confirmed result. Logging the "
                "parkrun gives a firmer anchor.",
                fg=typer.colors.YELLOW,
            )
        typer.echo("\nEasy pace is a ceiling, not a target; slower is usually better.")


@app.command("readiness")
def readiness_command(
    ctx: typer.Context,
    date: Annotated[
        str | None, typer.Argument(help="Date to judge (YYYY-MM-DD). Defaults to today.")
    ] = None,
) -> None:
    """Judge readiness from the rolling ln(rMSSD) baseline and what surrounds it."""
    settings = _settings(ctx)
    with _friendly_errors(), Store.open(settings.database) as store:
        parsed = _parse_dates([date] if date else None)
        target = parsed[0] if parsed else _today()
        readings = daily_readings(store, target - dt.timedelta(days=READINESS_LOOKBACK_DAYS))
        verdict = readiness_assess(readings, target)

        typer.echo(f"{target:%Y-%m-%d}  {verdict.readiness}")
        for reason in verdict.reasons:
            typer.echo(f"  - {reason}")
        if verdict.baseline is not None:
            typer.echo(
                f"\nbaseline {verdict.baseline.mean_rmssd:.0f} ms "
                f"(normal range {verdict.baseline.lower_rmssd:.0f}"
                f"-{verdict.baseline.upper_rmssd:.0f} ms, "
                f"{verdict.baseline.samples} readings)"
            )
        typer.echo("\nThis is a hint, not an instruction.")


@app.command("plan")
def plan_command(
    ctx: typer.Context,
    today_only: Annotated[
        bool, typer.Option("--today", help="Show just the next thing worth doing.")
    ] = False,
) -> None:
    """Show what is worth doing this week.

    A prioritised menu rather than a schedule: nothing is assigned to a day,
    nothing is late, and anything not done rolls over up to a cap.
    """
    settings = _settings(ctx)
    with _friendly_errors(), Store.open(settings.database) as store:
        goals = load_goals(settings.goals_path)
        today = _today()
        week_start = week_start_for(today)
        readings = daily_readings(store, today - dt.timedelta(days=READINESS_LOOKBACK_DAYS))
        verdict = readiness_assess(readings, today)
        note = _readiness_note(verdict.readiness)

        menu = build_menu(goals, week_start, _week_index(store, week_start), readiness_note=note)
        done = completed_session_names(store, week_start)

        if today_only:
            _echo_next_session(menu, done)
            return

        typer.echo(f"Week of {week_start:%Y-%m-%d} - {menu.describe()}")
        typer.echo(f"Readiness today: {verdict.readiness}\n")
        for planned in menu.sessions:
            marker = "x" if planned.session.name.lower() in done else " "
            carried = " (rolled over)" if planned.rolled_over else ""
            typer.echo(f"  [{marker}] {planned.priority}. {planned.session.name}{carried}")
            typer.echo(f"        {planned.session.description}")
            if planned.note:
                typer.echo(f"        note: {planned.note}")
        if menu.dropped:
            names = ", ".join(session.name for session in menu.dropped)
            typer.echo(f"\nDropped from the backlog (past the cap): {names}")
        typer.echo("\nOrder is priority, not a timetable. Fit them around your week.")


@app.command("report")
def report_command(
    ctx: typer.Context,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", dir_okay=False, help="Where to write the HTML dashboard."),
    ] = None,
    title: Annotated[str, typer.Option("--title", help="Page title.")] = "longrun",
    with_cycle: Annotated[
        bool,
        typer.Option(
            "--with-cycle",
            help="Include the by-cycle-day panel. Off by default so a report is "
            "free of this data unless asked for.",
        ),
    ] = False,
) -> None:
    """Generate the static HTML trend dashboard.

    The cycle panel is opt-in: without `--with-cycle` the page contains no
    trace of that data, which is what makes a report safe to show someone.
    """
    settings = _settings(ctx)
    destination = output or settings.report
    with _friendly_errors(), Store.open(settings.database) as store:
        cycles = _load_cycles(settings) if with_cycle else []
        data = load_report_data(store, cycles)
        if data.is_empty:
            typer.secho(
                "The store is empty, so the report would have nothing to show. "
                "Run an ingest command first.",
                fg=typer.colors.YELLOW,
                err=True,
            )
            raise typer.Exit(code=1)
        written = build_report(data, destination, title=title)
    typer.echo(f"Wrote {written}")
    if with_cycle:
        typer.secho(
            "This report contains cycle data. Treat the file as private.",
            fg=typer.colors.YELLOW,
        )


def _echo_candidates(detected: Sequence[DetectedEffort]) -> None:
    """List the 5k efforts found in recent runs, hardest first."""
    if not detected:
        typer.echo(
            "No 5k splits found in recent runs. Detection needs the per-activity "
            "streams, so check the runs were ingested with them."
        )
        return

    typer.echo(f"{len(detected)} run(s) contained a 5k split:\n")
    for effort in sorted(detected, key=lambda entry: entry.duration_s):
        minutes, seconds = divmod(round(effort.duration_s), 60)
        heart_rate = f"{effort.avg_hr:.0f} bpm" if effort.avg_hr is not None else "no HR"
        marker = "*" if effort.is_hard else " "
        hint = "  Saturday" if effort.looks_like_a_parkrun else ""
        typer.echo(
            f"  {marker} {effort.date:%Y-%m-%d}  {minutes}:{seconds:02d}  "
            f"{heart_rate:<8}{hint}  {effort.activity_name}"
        )
    typer.echo(
        "\n* hard enough to anchor on. Log the real parkrun with "
        "`longrun ingest manual --tests` for a firmer anchor."
    )


def _readiness_note(readiness: Readiness) -> str:
    """Turn a readiness verdict into guidance for the hardest session."""
    if readiness is Readiness.RED:
        return "readiness is red; this one is worth swapping for easy running"
    if readiness is Readiness.AMBER:
        return "readiness is amber; start it and judge by feel after the first rep"
    return ""


def _week_index(store: Store, week_start: dt.date) -> int:
    """Return how many weeks of training precede this one.

    Counted from the first stored activity, so the block rotation follows
    the athlete's own history rather than a calendar.

    Args:
        store: An initialised store.
        week_start: Monday of the week in question.

    Returns:
        The zero-based week number.
    """
    first = store.conn.execute("SELECT MIN(start_time) AS first FROM activities").fetchone()
    if first is None or first["first"] is None:
        return 0
    started = dt.datetime.fromisoformat(str(first["first"])).date()
    return max(0, (week_start - week_start_for(started)).days // 7)


def _echo_next_session(menu: WeeklyMenu, done: list[str]) -> None:
    """Print the highest-priority session still outstanding."""
    for planned in menu.sessions:
        if planned.session.name.lower() in done:
            continue
        typer.echo(f"{planned.session.name}: {planned.session.description}")
        if planned.note:
            typer.echo(f"note: {planned.note}")
        return
    typer.echo("Everything on this week's menu is done. Anything else is a bonus.")


def _load_cycles(settings: Settings) -> list[Cycle]:
    """Open the separate cycle store and derive its cycles.

    Kept out of the reporting layer on purpose: that module never opens this
    database, so a report can only contain cycle data by an explicit request
    made here.

    Args:
        settings: Resolved settings.

    Returns:
        The derived cycles, empty if nothing has been logged.
    """
    with CycleStore.open(settings.cycle_database) as store:
        return cycles_from_days(store.logged_days())


def _today() -> dt.date:
    """Return today's date in UTC, matching how everything else is stored."""
    return dt.datetime.now(tz=dt.UTC).date()


def _consecutive(start: dt.date, days: int) -> list[dt.date]:
    """Return `days` dates running from `start`."""
    return [start + dt.timedelta(days=offset) for offset in range(days)]


def _parse_dates(values: list[str] | None) -> list[dt.date]:
    """Parse ISO-8601 dates, rejecting anything ambiguous.

    Args:
        values: Date strings, or None.

    Returns:
        The parsed dates, in the order given.

    Raises:
        IngestError: If a value is not an ISO-8601 date.
    """
    parsed: list[dt.date] = []
    for value in values or []:
        try:
            parsed.append(dt.date.fromisoformat(value.strip()))
        except ValueError as exc:
            msg = f"{value!r} is not an ISO-8601 date (expected YYYY-MM-DD)"
            raise IngestError(msg) from exc
    return parsed


def _settings(ctx: typer.Context) -> Settings:
    """Return the settings resolved by the top-level callback."""
    if isinstance(ctx.obj, Settings):
        return ctx.obj
    return load_settings()


def _echo_summary(summary: IngestSummary, store: Store) -> None:
    """Print what a run wrote, then what the store now holds."""
    typer.echo(f"\nIngested {summary.describe()}")
    _echo_counts(store)


def _echo_counts(store: Store) -> None:
    """Print a one-line summary of what the store now holds."""
    counts = store.counts()
    summary = ", ".join(f"{count:,} {table}" for table, count in counts.items())
    typer.echo(f"Store now holds {summary}")


@contextmanager
def _friendly_errors() -> Iterator[None]:
    """Turn expected failures into a message and a non-zero exit code."""
    try:
        yield
    except LongrunError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
