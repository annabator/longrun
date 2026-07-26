"""Intervals.icu ingestion: the default source for activities and wellness.

Intervals.icu already aggregates what a watch uploads and computes CTL, ATL
and per-activity load deterministically, so longrun ingests those figures
rather than recomputing them. Everything derived downstream is still computed
here in Python from stored rows.
"""

from longrun.ingest.intervals.client import STREAM_TYPES, IntervalsClient
from longrun.ingest.intervals.mapper import (
    activity_from_payload,
    stream_points_from_payload,
    wellness_from_payload,
)
from longrun.ingest.intervals.service import IntervalsIngester

__all__ = [
    "STREAM_TYPES",
    "IntervalsClient",
    "IntervalsIngester",
    "activity_from_payload",
    "stream_points_from_payload",
    "wellness_from_payload",
]
