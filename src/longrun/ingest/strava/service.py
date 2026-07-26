"""Orchestrates a Strava ingestion run.

Strava is a secondary source as of v1.1: its API now requires a paid
subscription for the endpoints longrun needs, so Intervals.icu is the default.
The adapter is kept because it still works for anyone who has that
subscription, and because it costs nothing to keep once it is behind the
common ingester interface.

Each activity is committed as it is fetched rather than at the end, so a run
cut short by a rate limit leaves the store consistent and the next run
resumes from where it stopped.
"""

import datetime as dt

from longrun.ingest.base import IngestSummary, Progress
from longrun.ingest.strava.client import StravaClient
from longrun.ingest.strava.mapper import activity_from_payload, stream_points_from_payload
from longrun.models import Source
from longrun.store import Store


class StravaIngester:
    """Pulls activities and streams from the Strava API."""

    source = Source.STRAVA

    def __init__(
        self,
        client: StravaClient,
        *,
        since: dt.datetime | None = None,
        with_streams: bool = True,
        limit: int | None = None,
    ) -> None:
        """Configure a run.

        Args:
            client: Configured Strava client.
            since: Only ingest activities starting after this time. Defaults
                to resuming from the latest stored Strava activity.
            with_streams: Whether to also fetch per-activity streams.
            limit: Stop after this many activities.
        """
        self._client = client
        self._since = since
        self._with_streams = with_streams
        self._limit = limit

    def ingest(self, store: Store, *, progress: Progress | None = None) -> IngestSummary:
        """Pull activities, and optionally their streams, into the store.

        Args:
            store: Destination store, already initialised.
            progress: Optional callback for progress lines.

        Returns:
            Counts of what was written.
        """
        report = progress or (lambda _: None)
        after = self._since if self._since is not None else store.latest_activity_start(self.source)
        already_have_streams = (
            store.stored_stream_activity_ids(self.source) if self._with_streams else set()
        )

        if after is not None:
            report(f"Fetching activities after {after:%Y-%m-%d %H:%M %Z}")
        else:
            report("Fetching the full activity history")

        activities = 0
        with_stream_data = 0
        stream_points = 0
        skipped = 0

        for payload in self._client.iter_activities(after=after):
            activity = activity_from_payload(payload)
            store.upsert_activities([activity])
            activities += 1
            report(f"  {activity.start_time:%Y-%m-%d}  {activity.name}")

            if self._with_streams:
                if activity.external_id in already_have_streams:
                    skipped += 1
                else:
                    points = stream_points_from_payload(
                        self._client.fetch_streams(int(activity.external_id))
                    )
                    if points:
                        stream_points += store.upsert_streams(
                            self.source, activity.external_id, points
                        )
                        with_stream_data += 1

            if self._limit is not None and activities >= self._limit:
                report(f"Reached the requested limit of {self._limit} activities")
                break

        return IngestSummary(
            activities=activities,
            activities_with_streams=with_stream_data,
            stream_points=stream_points,
            streams_skipped=skipped,
        )
