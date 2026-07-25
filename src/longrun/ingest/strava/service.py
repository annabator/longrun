"""Orchestrates a Strava ingestion run.

Each activity is committed as it is fetched rather than at the end, so a run
cut short by a rate limit leaves the store consistent and the next run
resumes from where it stopped.
"""

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass

from longrun.ingest.strava.client import StravaClient
from longrun.ingest.strava.mapper import activity_from_payload, stream_points_from_payload
from longrun.store import Store

ProgressFn = Callable[[str], None]
"""Receives human-readable progress lines; the CLI passes an echo function."""


@dataclass(frozen=True)
class IngestSummary:
    """Counts describing what one ingestion run wrote."""

    activities: int = 0
    activities_with_streams: int = 0
    stream_points: int = 0
    streams_skipped: int = 0

    def describe(self) -> str:
        """Return a one-line human summary of the run."""
        return (
            f"{self.activities} activities, "
            f"{self.stream_points} stream samples across "
            f"{self.activities_with_streams} activities "
            f"({self.streams_skipped} already had streams)"
        )


def ingest_strava(
    store: Store,
    client: StravaClient,
    *,
    since: dt.datetime | None = None,
    with_streams: bool = True,
    limit: int | None = None,
    progress: ProgressFn | None = None,
) -> IngestSummary:
    """Pull activities, and optionally their streams, into the store.

    Args:
        store: Destination store; must already be initialised.
        client: Configured Strava client.
        since: Only ingest activities starting after this time. Defaults to
            the latest activity already stored, making runs incremental.
        with_streams: Whether to also fetch per-activity streams, which is
            the expensive part of the API budget.
        limit: Stop after this many activities; useful for a first trial run.
        progress: Optional callback for progress lines.

    Returns:
        Counts of what was written.
    """
    report = progress or (lambda _: None)
    after = since if since is not None else store.latest_activity_start()
    already_have_streams = store.stored_stream_activity_ids() if with_streams else set()

    if after is not None:
        report(f"Fetching activities after {after:%Y-%m-%d %H:%M %Z}")
    else:
        report("Fetching the full activity history")

    activities = 0
    with_stream_data = 0
    stream_points = 0
    skipped = 0

    for payload in client.iter_activities(after=after):
        activity = activity_from_payload(payload)
        store.upsert_activities([activity])
        activities += 1
        report(f"  {activity.start_time:%Y-%m-%d}  {activity.name}")

        if with_streams:
            if activity.strava_id in already_have_streams:
                skipped += 1
            else:
                points = stream_points_from_payload(client.fetch_streams(activity.strava_id))
                if points:
                    stream_points += store.upsert_streams(activity.strava_id, points)
                    with_stream_data += 1

        if limit is not None and activities >= limit:
            report(f"Reached the requested limit of {limit} activities")
            break

    return IngestSummary(
        activities=activities,
        activities_with_streams=with_stream_data,
        stream_points=stream_points,
        streams_skipped=skipped,
    )
