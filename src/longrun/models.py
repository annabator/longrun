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

    INTERVALS = "intervals"
    FIT = "fit"
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
    VITAMIN_D = "vitamin_d"
    BODY_FAT_PCT = "body_fat_pct"


class Activity(BaseModel):
    """A single training activity, normalised from a provider payload.

    Identity is `(source, external_id)` rather than any one provider's id, so
    the same activity pulled from two providers stays two rows and neither
    provider owns the key.

    Attributes:
        source: Which adapter produced this row (see :class:`Source`).
        external_id: The provider's own identifier, as text; Intervals.icu
            uses strings such as `i12345678`, Strava uses integers.
        distance_m: Total distance in metres.
        avg_speed: Average speed in metres per second.
        total_elevation_gain: Cumulative ascent in metres.
        feel: Subjective post-activity rating, as recorded upstream.
        rpe: Rating of perceived exertion, as recorded upstream.
        icu_load: Training load computed by Intervals.icu for this activity.
        icu_intensity: Intensity relative to threshold, computed upstream.
    """

    model_config = ConfigDict(frozen=True)

    source: str
    external_id: str
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
    feel: float | None = None
    rpe: float | None = None
    icu_load: float | None = None
    icu_intensity: float | None = None

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
        rr_ms: Beat-to-beat interval in milliseconds, where the source
            records it. FIT files carry these; the Intervals.icu streams API
            does not expose them, so this is usually populated by the FIT
            adapter rather than by Intervals ingestion.
    """

    model_config = ConfigDict(frozen=True)

    t_s: int
    hr: float | None = None
    velocity: float | None = None
    distance_m: float | None = None
    altitude: float | None = None
    cadence: float | None = None
    rr_ms: float | None = None


class WellnessDay(BaseModel):
    """Daily wellness metrics for a single date.

    One row per calendar day; re-ingesting the same date merges into it.

    Attributes:
        avg_sleeping_hr: Mean heart rate across the sleep period.
        hrv_rmssd: Heart-rate variability as rMSSD, in milliseconds. rMSSD
            specifically, because the readiness work in v1.1 takes its
            logarithm, and SDNN is not interchangeable with it.
        spo2: Blood oxygen saturation as a percentage.

    Deliberately absent: the rolling CTL, ATL and form figures. They are
    averages over a modelled load rather than measurements, and the per-
    activity `icu_load` they are built from is kept on
    :class:`Activity` instead.
    """

    model_config = ConfigDict(frozen=True)

    date: dt.date
    resting_hr: float | None = None
    avg_sleeping_hr: float | None = None
    hrv_rmssd: float | None = None
    sleep_score: float | None = None
    sleep_hours: float | None = None
    spo2: float | None = None
    weight_kg: float | None = None
    source: str = Source.MANUAL
    notes: str | None = None


class TestResult(BaseModel):
    """A point-in-time measurement, stored in long format.

    Long format keeps the schema open: VO2max, parkrun 5k times, blood results,
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
