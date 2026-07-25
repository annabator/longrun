"""Domain models shared across ingestion, storage and reporting.

These are the only shapes that cross module boundaries: ingestion adapters
translate provider payloads or CSV rows into these models, the store persists
them, and reporting reads them back. Provider-specific dictionaries never leak
past an adapter.
"""

import datetime as dt
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, field_validator


class Source(StrEnum):
    """Well-known provenance labels.

    `source` fields are plain strings so that new providers need no code
    change; these constants simply name the ones longrun ships with.
    """

    STRAVA = "strava"
    MANUAL = "manual"
    SYNTHETIC = "synthetic"


class Metric(StrEnum):
    """Well-known metric names for :class:`TestResult`.

    The `tests` table is long-format and accepts any metric string; these
    constants exist so that reporting can recognise the metrics it knows how
    to plot without constraining what may be logged.
    """

    VO2MAX = "vo2max"
    PARKRUN_5K = "parkrun_5k"
    FERRITIN = "ferritin"
    BODY_FAT_PCT = "body_fat_pct"


class Activity(BaseModel):
    """A single training activity, normalised from a provider payload.

    Attributes:
        strava_id: Provider-assigned identifier; the natural key for upserts.
        distance_m: Total distance in metres.
        avg_speed: Average speed in metres per second.
        total_elevation_gain: Cumulative ascent in metres.
    """

    model_config = ConfigDict(frozen=True)

    strava_id: int
    start_time: dt.datetime
    sport_type: str
    name: str
    distance_m: float
    moving_time_s: int
    elapsed_time_s: int
    avg_hr: float | None = None
    max_hr: float | None = None
    avg_speed: float | None = None
    total_elevation_gain: float | None = None
    avg_cadence: float | None = None
    source: str = Source.STRAVA

    @field_validator("start_time")
    @classmethod
    def _require_timezone(cls, value: dt.datetime) -> dt.datetime:
        """Treat naive timestamps as UTC so stored values are unambiguous."""
        if value.tzinfo is None:
            return value.replace(tzinfo=dt.UTC)
        return value


class StreamPoint(BaseModel):
    """One sample from an activity's time-series streams.

    Attributes:
        t_s: Seconds elapsed since the start of the activity.
        velocity: Instantaneous smoothed speed in metres per second.
        distance_m: Cumulative distance in metres at this sample.
        altitude: Elevation in metres.
    """

    model_config = ConfigDict(frozen=True)

    t_s: int
    hr: float | None = None
    velocity: float | None = None
    distance_m: float | None = None
    altitude: float | None = None
    cadence: float | None = None


class WellnessDay(BaseModel):
    """Daily wellness metrics for a single date.

    One row per calendar day; re-ingesting the same date overwrites it.
    """

    model_config = ConfigDict(frozen=True)

    date: dt.date
    resting_hr: float | None = None
    hrv: float | None = None
    sleep_hours: float | None = None
    sleep_score: float | None = None
    weight_kg: float | None = None
    source: str = Source.MANUAL
    notes: str | None = None


class TestResult(BaseModel):
    """A point-in-time measurement, stored in long format.

    Long format keeps the schema open: VO2max, parkrun 5k times, ferritin,
    body composition and any metric added in future are all rows here rather
    than columns, so no migration is needed to start tracking something new.

    Attributes:
        metric: Metric name, lower-cased (see :class:`Metric`).
        value: Numeric value in `unit`; durations are stored in seconds.
        unit: Unit of measurement, e.g. `ml/kg/min`, `s`, `ug/L`.
    """

    model_config = ConfigDict(frozen=True)

    date: dt.date
    metric: str
    value: float
    unit: str
    source: str = Source.MANUAL
    notes: str | None = None

    @field_validator("metric", "unit")
    @classmethod
    def _normalise(cls, value: str) -> str:
        """Strip surrounding whitespace so CSV padding cannot fork a metric."""
        return value.strip()

    @field_validator("metric")
    @classmethod
    def _lower_metric(cls, value: str) -> str:
        """Lower-case metric names so `VO2max` and `vo2max` are one series."""
        return value.lower()

    def is_personal_best_against(self, previous: Self | None) -> bool:
        """Return whether this result improves on `previous`.

        Lower is better for duration metrics; higher is better otherwise.
        `previous` being None means there is nothing to beat yet.
        """
        if previous is None:
            return True
        if self.unit == "s":
            return self.value < previous.value
        return self.value > previous.value
