"""Orchestrates an Intervals.icu ingestion run.

Records are committed as they arrive rather than at the end, so a run cut
short by a network failure leaves the store consistent and the next run
resumes from where it stopped.
"""

import datetime as dt

from longrun.errors import IngestError, LongrunError
from longrun.ingest.base import IngestSummary, Progress
from longrun.ingest.fit.reader import read_rr_by_second
from longrun.ingest.intervals.client import IntervalsClient, today_utc
from longrun.ingest.intervals.mapper import (
    activity_from_payload,
    stream_points_from_payload,
    wellness_from_payload,
)
from longrun.models import Source, WellnessDay
from longrun.store import Store

DEFAULT_HISTORY_DAYS = 365 * 3
"""How far back a first run reaches when the store is empty."""

_WELLNESS_REFRESH_DAYS = 60
"""Re-fetch this much recent wellness on every run.

A watch can sync a day's readings long after the day itself, so recent
wellness is refetched rather than assumed complete.
"""


class IntervalsIngester:
    """Pulls activities, streams and daily wellness from Intervals.icu."""

    source = Source.INTERVALS

    def __init__(
        self,
        client: IntervalsClient,
        *,
        since: dt.date | None = None,
        with_streams: bool = True,
        with_wellness: bool = True,
        with_rr: bool = False,
        limit: int | None = None,
    ) -> None:
        """Configure a run.

        Args:
            client: Configured API client.
            since: Earliest date to fetch. Defaults to resuming from the
                latest stored Intervals activity.
            with_streams: Whether to fetch per-activity streams.
            with_wellness: Whether to fetch daily wellness.
            with_rr: Whether to download each activity's FIT file for the
                beat-to-beat intervals the streams API does not expose. Off by
                default: it is the largest request the adapter makes, an
                activity recorded without a chest strap has none to find, and
                nothing consumes the data yet. The path is kept working so
                that per-beat analysis can switch it on later.
            limit: Stop after this many activities.
        """
        self._client = client
        self._since = since
        self._with_streams = with_streams
        self._with_wellness = with_wellness
        self._with_rr = with_rr
        self._limit = limit

    def ingest(self, store: Store, *, progress: Progress | None = None) -> IngestSummary:
        """Pull activities, streams and wellness into the store.

        Args:
            store: Destination store, already initialised.
            progress: Optional callback for progress lines.

        Returns:
            Counts of what was written.
        """
        report = progress or (lambda _: None)
        summary = self._ingest_activities(store, report)
        if self._with_wellness:
            summary = summary.merge(self._ingest_wellness(store, report))
        return summary

    def _ingest_activities(self, store: Store, report: Progress) -> IngestSummary:
        """Fetch activities, and their streams when configured to."""
        oldest = self._resolve_since(store)
        report(f"Fetching activities from {oldest.isoformat()}")

        already_have_streams = (
            store.stored_stream_activity_ids(self.source) if self._with_streams else set()
        )
        already_have_rr = store.activities_with_rr(self.source) if self._with_rr else set()
        activities = 0
        with_stream_data = 0
        stream_points = 0
        skipped = 0
        rr_activities = 0

        for payload in self._client.iter_activities(oldest):
            activity = activity_from_payload(payload)
            store.upsert_activities([activity])
            activities += 1
            report(f"  {activity.start_time:%Y-%m-%d}  {activity.name}")

            if self._with_streams:
                if activity.external_id in already_have_streams:
                    skipped += 1
                else:
                    points = stream_points_from_payload(
                        self._client.fetch_streams(activity.external_id)
                    )
                    if points:
                        stream_points += store.upsert_streams(
                            self.source, activity.external_id, points
                        )
                        with_stream_data += 1

            if self._with_rr and activity.external_id not in already_have_rr:
                rr_activities += self._ingest_rr(store, activity.external_id, report)

            if self._limit is not None and activities >= self._limit:
                report(f"Reached the requested limit of {self._limit} activities")
                break

        if self._with_rr:
            report(f"Recovered beat-to-beat intervals for {rr_activities} activities")

        return IngestSummary(
            activities=activities,
            activities_with_streams=with_stream_data,
            stream_points=stream_points,
            streams_skipped=skipped,
        )

    def _ingest_rr(self, store: Store, external_id: str, report: Progress) -> int:
        """Download one activity's FIT file and store its R-R intervals.

        Returns 0 rather than raising when an activity has no beat data:
        anything recorded without a chest strap simply has none, which is
        normal and must not interrupt a sync.

        Args:
            store: Destination store.
            external_id: Intervals.icu identifier of the activity.
            report: Progress callback.

        Returns:
            1 if beat data was stored, 0 otherwise.
        """
        try:
            data = self._client.fetch_activity_fit(external_id)
        except LongrunError as exc:
            # A single unavailable file must not end a sync that has already
            # written activities and wellness.
            report(f"    could not fetch the activity file: {exc}")
            return 0
        if data is None:
            return 0

        try:
            rr_by_second = read_rr_by_second(data)
        except IngestError as exc:
            report(f"    activity file could not be parsed: {exc}")
            return 0
        if not rr_by_second:
            return 0

        written = store.upsert_stream_rr(self.source, external_id, rr_by_second)
        if written:
            report(f"    {written} seconds of beat-to-beat intervals")
            return 1
        return 0

    def _ingest_wellness(self, store: Store, report: Progress) -> IngestSummary:
        """Fetch daily wellness, refreshing the recent rolling figures."""
        oldest = self._resolve_wellness_since(store)
        report(f"Fetching daily wellness from {oldest.isoformat()}")

        records = self._client.fetch_wellness(oldest)
        days = [wellness_from_payload(record) for record in records]
        # Days with nothing but a date carry no information and would only
        # bump the source label on an otherwise richer row.
        days = [day for day in days if _has_measurement(day)]
        written = store.upsert_wellness(days) if days else 0
        return IngestSummary(wellness_days=written)

    def _resolve_since(self, store: Store) -> dt.date:
        """Return the earliest activity date to request."""
        if self._since is not None:
            return self._since
        latest = store.latest_activity_start(self.source)
        if latest is None:
            return today_utc() - dt.timedelta(days=DEFAULT_HISTORY_DAYS)
        return latest.date()

    def _resolve_wellness_since(self, store: Store) -> dt.date:
        """Return the earliest wellness date to request."""
        if self._since is not None:
            return self._since
        latest = store.latest_wellness_date(self.source)
        if latest is None:
            return today_utc() - dt.timedelta(days=DEFAULT_HISTORY_DAYS)
        return latest - dt.timedelta(days=_WELLNESS_REFRESH_DAYS)


def _has_measurement(day: WellnessDay) -> bool:
    """Return whether a wellness day carries anything beyond its date."""
    measured = (
        "resting_hr",
        "avg_sleeping_hr",
        "hrv_rmssd",
        "sleep_score",
        "sleep_hours",
        "spo2",
        "weight_kg",
    )
    return any(getattr(day, field, None) is not None for field in measured)
