"""Command-line entry point for longrun.

Commands are thin: they resolve configuration, open the store, call into the
ingestion or reporting layers, and report what happened. Anything a user could
reasonably get wrong - missing credentials, an unparseable CSV, an exhausted
rate limit - arrives as a :class:`~longrun.errors.LongrunError` and is printed
as a plain message rather than a traceback.
"""

import datetime as dt
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

import httpx
import typer

from longrun import __version__
from longrun.config import Settings, load_settings
from longrun.errors import LongrunError
from longrun.ingest.base import IngestSummary
from longrun.ingest.fit import FitFolderIngester
from longrun.ingest.intervals import IntervalsClient, IntervalsIngester
from longrun.ingest.manual import load_tests_csv, load_wellness_csv
from longrun.ingest.strava import StravaAuthenticator, StravaClient, StravaIngester, TokenStore
from longrun.ingest.strava.auth import DEFAULT_SCOPE, build_authorize_url
from longrun.report import build_report, load_report_data
from longrun.store import Store

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
app.add_typer(ingest_app, name="ingest")
app.add_typer(auth_app, name="auth")


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


@app.command("report")
def report_command(
    ctx: typer.Context,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", dir_okay=False, help="Where to write the HTML dashboard."),
    ] = None,
    title: Annotated[str, typer.Option("--title", help="Page title.")] = "longrun",
) -> None:
    """Generate the static HTML trend dashboard."""
    settings = _settings(ctx)
    destination = output or settings.report
    with _friendly_errors(), Store.open(settings.database) as store:
        data = load_report_data(store)
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
