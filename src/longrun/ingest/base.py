"""The contract every ingestion adapter satisfies.

An adapter knows how to reach one source and translate it into the models in
:mod:`longrun.models`. It does not know how the store works beyond calling
its upserts, and nothing downstream knows which adapter produced a row apart
from the `source` column.

Keeping this narrow is what let Intervals.icu become the default source
without the store or the reports changing: a new adapter implements
:class:`Ingester` and appears in the CLI, and that is the whole change.
"""

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Protocol, runtime_checkable

from longrun.store import Store

Progress = Callable[[str], None]
"""Receives human-readable progress lines; the CLI passes an echo function."""


@dataclass(frozen=True)
class IngestSummary:
    """What one ingestion run wrote.

    Counts are additive so that a run touching several record types, or an
    adapter delegating to another, can combine summaries with :meth:`merge`.
    """

    activities: int = 0
    activities_with_streams: int = 0
    stream_points: int = 0
    streams_skipped: int = 0
    wellness_days: int = 0
    tests: int = 0

    def merge(self, other: "IngestSummary") -> "IngestSummary":
        """Return the element-wise sum of this summary and `other`.

        Args:
            other: Summary to add.

        Returns:
            The combined counts.
        """
        return replace(
            self,
            activities=self.activities + other.activities,
            activities_with_streams=self.activities_with_streams + other.activities_with_streams,
            stream_points=self.stream_points + other.stream_points,
            streams_skipped=self.streams_skipped + other.streams_skipped,
            wellness_days=self.wellness_days + other.wellness_days,
            tests=self.tests + other.tests,
        )

    def describe(self) -> str:
        """Return a one-line human summary, omitting anything untouched.

        Returns:
            A sentence naming only the record types this run wrote.
        """
        parts: list[str] = []
        if self.activities or self.stream_points:
            parts.append(f"{self.activities} activities")
        if self.stream_points:
            parts.append(
                f"{self.stream_points} stream samples across "
                f"{self.activities_with_streams} activities"
            )
        if self.streams_skipped:
            parts.append(f"{self.streams_skipped} already had streams")
        if self.wellness_days:
            parts.append(f"{self.wellness_days} wellness days")
        if self.tests:
            parts.append(f"{self.tests} test results")
        return ", ".join(parts) if parts else "nothing new"


@runtime_checkable
class Ingester(Protocol):
    """Pulls records from one source into the store.

    Implementations are expected to be idempotent: running twice over the same
    window must leave the store as it was after the first run, because every
    write is an upsert on a natural key.
    """

    source: str
    """The `source` label written on every row this adapter produces."""

    def ingest(self, store: Store, *, progress: Progress | None = None) -> IngestSummary:
        """Pull everything this adapter is configured to fetch.

        Args:
            store: Destination store, already initialised.
            progress: Optional callback for progress lines.

        Returns:
            Counts of what was written.
        """
        ...
