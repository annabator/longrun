"""Readiness from a rolling ln(rMSSD) baseline.

The approach is the one Altini and HRV4Training describe, and it is worth
being precise about why each part is there.

* **The logarithm.** rMSSD is strongly right-skewed, so a raw mean is pulled
  around by the occasional high morning. Taking `ln` first makes the
  distribution roughly symmetric, which is what allows a normal range built
  from a standard deviation to mean anything.
* **A rolling baseline, not an all-time one.** Fitness and circumstances
  move. Seven days is short enough to follow them and long enough to average
  out one poor night.
* **A range, not a threshold.** A single reading below baseline is noise. The
  normal range is the baseline plus or minus a multiple of the recent
  standard deviation, and only readings outside it are treated as signal.

Suppression is not called from HRV alone. A low morning that comes with an
elevated resting heart rate or poor sleep is a different thing from a low
morning on its own, and the verdict says which factors fired.

The signals that matter are the measured ones: HRV, resting heart rate and
sleep. Nothing here asks the athlete to rate anything, and nothing depends on
a rating existing. Where a post-session feel happens to have been recorded it
is used, and where it has not the verdict is unaffected.

Everything here is a rule with a number attached, chosen so that it can be
tested against fixed inputs and argued with.
"""

import datetime as dt
import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

BASELINE_DAYS = 7
"""Rolling window for the ln(rMSSD) baseline."""

NORMAL_RANGE_SDS = 1.0
"""How many standard deviations of ln(rMSSD) count as the normal range."""

MIN_READINGS_FOR_BASELINE = 4
"""Readings needed inside the window before a baseline means anything."""

CONSECUTIVE_LOW_DAYS = 2
"""Days below the normal range before HRV alone counts as suppressed."""

RESTING_HR_ELEVATION_BPM = 3.0
"""Rise over the baseline resting heart rate that counts as elevated."""

POOR_SLEEP_HOURS = 6.0
"""Sleep below this counts as a contributing factor."""

LOW_FEEL = 2.0
"""Post-session feel at or below this counts as a contributing factor.

Intervals.icu records feel on a rising scale, so a low number is a bad day.
Optional, like everything subjective here: it fires when a rating happens to
be there and is silent when it is not.
"""

AMBER_FACTORS = 1
RED_FACTORS = 2


class Readiness(StrEnum):
    """The verdict.

    Attributes:
        GREEN: Nothing in the data argues against training as planned.
        AMBER: One signal is off; worth noticing, not worth panicking about.
        RED: Several signals agree; ease off.
        UNKNOWN: Not enough logged to say anything.
    """

    GREEN = "green"
    AMBER = "amber"
    RED = "red"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class DailyReading:
    """One day's inputs to the readiness calculation.

    Attributes:
        date: The day.
        hrv_rmssd: Morning rMSSD in milliseconds.
        resting_hr: Morning resting heart rate.
        sleep_hours: Hours slept.
        feel: Worst post-session feel logged that day, if any. Optional, and
            usually absent: nothing asks for it.
    """

    date: dt.date
    hrv_rmssd: float | None = None
    resting_hr: float | None = None
    sleep_hours: float | None = None
    feel: float | None = None


@dataclass(frozen=True)
class Baseline:
    """The rolling ln(rMSSD) baseline and the range around it.

    Attributes:
        mean_ln: Mean of ln(rMSSD) across the window.
        sd_ln: Standard deviation of ln(rMSSD) across the window.
        samples: How many readings the window held.
    """

    mean_ln: float
    sd_ln: float
    samples: int

    @property
    def lower_rmssd(self) -> float:
        """Bottom of the normal range, back in milliseconds."""
        return math.exp(self.mean_ln - NORMAL_RANGE_SDS * self.sd_ln)

    @property
    def upper_rmssd(self) -> float:
        """Top of the normal range, back in milliseconds."""
        return math.exp(self.mean_ln + NORMAL_RANGE_SDS * self.sd_ln)

    @property
    def mean_rmssd(self) -> float:
        """Baseline itself, back in milliseconds."""
        return math.exp(self.mean_ln)


@dataclass(frozen=True)
class ReadinessVerdict:
    """A readiness call and what drove it.

    Attributes:
        readiness: The verdict.
        reasons: The factors that fired, in plain words.
        baseline: The baseline it was judged against, if there was one.
    """

    readiness: Readiness
    reasons: list[str]
    baseline: Baseline | None

    def describe(self) -> str:
        """Return a one-line summary for the CLI."""
        if not self.reasons:
            return f"{self.readiness}: nothing in the data argues against training as planned"
        return f"{self.readiness}: {'; '.join(self.reasons)}"


def baseline_for(readings: Sequence[DailyReading], today: dt.date) -> Baseline | None:
    """Return the rolling ln(rMSSD) baseline ending the day before `today`.

    The current morning is excluded, since a reading cannot be compared
    against a baseline it is part of.

    Args:
        readings: Daily readings, any order.
        today: The day being judged.

    Returns:
        The baseline, or None if too few readings fall in the window.
    """
    earliest = today - dt.timedelta(days=BASELINE_DAYS)
    values = [
        math.log(reading.hrv_rmssd)
        for reading in readings
        if reading.hrv_rmssd is not None
        and reading.hrv_rmssd > 0
        and earliest <= reading.date < today
    ]
    if len(values) < MIN_READINGS_FOR_BASELINE:
        return None
    # A single-valued window has no spread; treat it as no usable range.
    sd = statistics.stdev(values) if len(values) > 1 else 0.0
    return Baseline(mean_ln=statistics.fmean(values), sd_ln=sd, samples=len(values))


def assess(readings: Sequence[DailyReading], today: dt.date) -> ReadinessVerdict:
    """Judge readiness for `today` from the readings around it.

    Args:
        readings: Daily readings, any order.
        today: The day to judge.

    Returns:
        The verdict, listing whichever factors fired.
    """
    by_date = {reading.date: reading for reading in readings}
    reading = by_date.get(today)
    baseline = baseline_for(readings, today)

    if reading is None or baseline is None:
        return ReadinessVerdict(
            readiness=Readiness.UNKNOWN,
            reasons=["not enough logged in the last week to have a baseline"],
            baseline=baseline,
        )

    reasons: list[str] = []
    factors = 0

    low_run = _consecutive_low_days(readings, today, baseline)
    if low_run >= CONSECUTIVE_LOW_DAYS:
        factors += 1
        reasons.append(
            f"HRV below the normal range {low_run} days running "
            f"(baseline {baseline.mean_rmssd:.0f} ms, floor {baseline.lower_rmssd:.0f} ms)"
        )
    elif low_run == 1:
        reasons.append(
            f"HRV below the normal range today "
            f"(baseline {baseline.mean_rmssd:.0f} ms, floor {baseline.lower_rmssd:.0f} ms)"
        )

    resting_baseline = _mean_resting_hr(readings, today)
    if (
        reading.resting_hr is not None
        and resting_baseline is not None
        and reading.resting_hr - resting_baseline >= RESTING_HR_ELEVATION_BPM
    ):
        factors += 1
        reasons.append(
            f"resting heart rate {reading.resting_hr:.0f} is "
            f"{reading.resting_hr - resting_baseline:.0f} bpm over its baseline"
        )

    if reading.sleep_hours is not None and reading.sleep_hours < POOR_SLEEP_HOURS:
        factors += 1
        reasons.append(f"only {reading.sleep_hours:.1f} hours of sleep")

    if reading.feel is not None and reading.feel <= LOW_FEEL:
        factors += 1
        reasons.append(f"the last session felt poor (feel {reading.feel:.0f})")

    return ReadinessVerdict(readiness=_verdict_for(factors), reasons=reasons, baseline=baseline)


def _verdict_for(factors: int) -> Readiness:
    """Map a count of firing factors onto a verdict."""
    if factors >= RED_FACTORS:
        return Readiness.RED
    if factors >= AMBER_FACTORS:
        return Readiness.AMBER
    return Readiness.GREEN


def _consecutive_low_days(
    readings: Sequence[DailyReading], today: dt.date, baseline: Baseline
) -> int:
    """Count days up to and including `today` with rMSSD below the range."""
    by_date = {reading.date: reading for reading in readings}
    floor = baseline.lower_rmssd
    run = 0
    day = today
    while True:
        reading = by_date.get(day)
        if reading is None or reading.hrv_rmssd is None or reading.hrv_rmssd >= floor:
            return run
        run += 1
        day -= dt.timedelta(days=1)


def _mean_resting_hr(readings: Sequence[DailyReading], today: dt.date) -> float | None:
    """Return the mean resting heart rate over the baseline window."""
    earliest = today - dt.timedelta(days=BASELINE_DAYS)
    values = [
        reading.resting_hr
        for reading in readings
        if reading.resting_hr is not None and earliest <= reading.date < today
    ]
    if len(values) < MIN_READINGS_FOR_BASELINE:
        return None
    return statistics.fmean(values)
