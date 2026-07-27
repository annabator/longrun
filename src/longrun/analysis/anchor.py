"""Find the performance that VDOT should be anchored on.

parkrun is the natural anchor: a fixed, measured 5k course run under roughly
comparable conditions every couple of weeks, which is exactly what makes a
series of them worth comparing. A hard solo 5k counts too, but is preferred
only when nothing better is available, since the course and effort vary.

The anchor is deliberately conservative:

* **Recent, not best ever.** A result from eighteen months ago describes a
  different athlete. Anything older than the window is ignored.
* **Best within the window, not latest.** One bad morning should not drop
  every training pace; a genuine decline shows up as the good results ageing
  out of the window instead.
"""

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

import pandas as pd

from longrun.analysis.vdot import RaceResult, pace_zones, vdot_from_result
from longrun.models import Metric

ANCHOR_WINDOW_DAYS = 120
"""How far back an anchoring result may be.

About four months: long enough to survive a quiet spell, short enough that
paces follow fitness rather than trailing a season behind.
"""

FIVE_K_M = 5000.0

HARD_EFFORT_FRACTION = 0.92
"""Fraction of peak heart rate a split must reach to count as a hard effort."""

PEAK_HR_PERCENTILE = 0.95
"""Percentile of per-run max heart rate taken as the athlete's peak.

Not the outright maximum: an optical sensor locking onto cadence throws out
readings above 200 that never happened, and one of those would raise the bar
out of reach for every genuine effort.
"""

MIN_RUNS_FOR_THRESHOLD = 10
"""Runs needed before a peak heart rate is worth calibrating against."""

_SATURDAY = 5

_MIN_PLAUSIBLE_5K_S = 12 * 60
_MAX_PLAUSIBLE_5K_S = 60 * 60


class Confidence(StrEnum):
    """How firm the anchor is.

    Attributes:
        LOGGED: A parkrun or race the athlete confirmed. Best available.
        DETECTED: A hard 5k found inside a run. Plausible but unverified.
    """

    LOGGED = "logged"
    DETECTED = "detected"


@dataclass(frozen=True)
class DetectedEffort:
    """A fast 5k found inside an activity.

    Attributes:
        date: When the activity started.
        duration_s: The split time.
        avg_hr: Mean heart rate across the split.
        hard_threshold: The heart rate the split had to reach to count.
        weekday: Day of week the activity fell on, 0 being Monday.
        activity_name: What the activity was called, for the listing.
    """

    date: dt.date
    duration_s: float
    avg_hr: float | None
    hard_threshold: float | None
    weekday: int
    activity_name: str

    @property
    def is_hard(self) -> bool:
        """Whether the split was run at an effort worth anchoring on.

        Fails closed: with no heart rate, or no threshold to compare against,
        the effort is not usable. A jog cannot be allowed to set race paces.
        """
        if self.avg_hr is None or self.hard_threshold is None:
            return False
        return self.avg_hr >= self.hard_threshold

    @property
    def looks_like_a_parkrun(self) -> bool:
        """Whether it fell on a Saturday.

        A hint for the listing only. parkrun is Saturday morning, but a
        Saturday effort is not necessarily a parkrun and this is never used
        to include or exclude anything.
        """
        return self.weekday == _SATURDAY

    def describe(self) -> str:
        """Return a short provenance string."""
        hint = ", Saturday - possibly a parkrun" if self.looks_like_a_parkrun else ""
        return f"detected 5k inside {self.activity_name!r}{hint}"


@dataclass(frozen=True)
class Anchor:
    """The result the training paces are derived from.

    Attributes:
        result: The performance itself.
        date: When it was run.
        source: Where it came from, for the CLI to report.
        vdot: The VDOT it implies.
        confidence: Whether it was confirmed or inferred.
    """

    result: RaceResult
    date: dt.date
    source: str
    vdot: float
    confidence: Confidence = Confidence.LOGGED

    def describe(self) -> str:
        """Return a one-line summary for the CLI."""
        minutes, seconds = divmod(round(self.result.duration_s), 60)
        return (
            f"VDOT {self.vdot:.1f} from a {minutes}:{seconds:02d} 5k "
            f"on {self.date:%Y-%m-%d} ({self.source})"
        )


def find_anchor(
    tests: pd.DataFrame,
    today: dt.date,
    window_days: int = ANCHOR_WINDOW_DAYS,
    detected: Sequence[DetectedEffort] = (),
) -> Anchor | None:
    """Return the best recent 5k to anchor training paces on.

    A logged parkrun always wins, whatever a detected effort says. The course
    is fixed and measured and the result was confirmed by a person; a detected
    effort is inferred from a GPS trace and could be a net-downhill route or a
    watch over-reading. Detection fills the gap when nothing is logged rather
    than competing on speed.

    Where a detected effort is quicker than the anchoring parkrun, that is
    worth knowing, but it belongs in
    :mod:`longrun.analysis.adaptation` as a suggestion to re-test, not in a
    silent change of anchor.

    Args:
        tests: The long-format tests frame.
        today: The date the window is measured back from.
        window_days: How far back a result may be.
        detected: Efforts found in the activities, used only as a fallback.

    Returns:
        The anchor, or None if nothing usable is available.
    """
    logged = _candidate_results(tests, today, window_days)
    if logged:
        # Fastest in the window: a single slow morning must not reset paces.
        day, seconds, metric = min(logged, key=lambda entry: entry[1])
        result = RaceResult(distance_m=FIVE_K_M, duration_s=seconds)
        return Anchor(
            result=result,
            date=day,
            source=f"{metric}, logged",
            vdot=vdot_from_result(result),
            confidence=Confidence.LOGGED,
        )

    usable = [effort for effort in detected if effort.is_hard]
    if not usable:
        return None

    best = min(usable, key=lambda effort: effort.duration_s)
    result = RaceResult(distance_m=FIVE_K_M, duration_s=best.duration_s)
    return Anchor(
        result=result,
        date=best.date,
        source=best.describe(),
        vdot=vdot_from_result(result),
        confidence=Confidence.DETECTED,
    )


def hard_effort_threshold(activities: pd.DataFrame) -> float | None:
    """Return the heart rate a split must reach to count as a hard effort.

    Calibrated to the athlete rather than to a textbook maximum: a fraction
    of their own observed peak. The peak is taken as the 95th percentile of
    per-run `max_hr` rather than the outright maximum, because a wrist sensor
    locking onto cadence produces readings of 200-plus that never happened.

    A percentile of *average* heart rate was the obvious alternative and is
    wrong: a quantile always admits some fraction of runs, so through a block
    containing no hard running it would still nominate the top of an easy
    distribution. This can return None, and that is the point.

    Args:
        activities: Running activities to calibrate against.

    Returns:
        The threshold in bpm, or None if there is too little to calibrate on.
    """
    if activities.empty or "max_hr" not in activities:
        return None
    peaks = activities["max_hr"].dropna()
    if len(peaks) < MIN_RUNS_FOR_THRESHOLD:
        return None
    return float(peaks.quantile(PEAK_HR_PERCENTILE)) * HARD_EFFORT_FRACTION


def anchor_paces(anchor: Anchor) -> list[str]:
    """Return the training paces implied by an anchor, ready to print.

    Args:
        anchor: The anchoring result.

    Returns:
        One `zone  m:ss/km` line per zone.
    """
    return [f"{zone.zone:<11} {zone.pace_label}" for zone in pace_zones(anchor.vdot)]


def _candidate_results(
    tests: pd.DataFrame, today: dt.date, window_days: int
) -> list[tuple[dt.date, float, str]]:
    """Collect plausible 5k results inside the window."""
    if tests.empty:
        return []

    earliest = today - dt.timedelta(days=window_days)
    wanted = {Metric.PARKRUN_5K.value, "5k", "5k_time"}
    selected = tests.loc[tests["metric"].isin(wanted)]
    if selected.empty:
        return []

    dates = pd.to_datetime(selected["date"]).dt.date.tolist()
    seconds_list = selected["value"].astype(float).tolist()
    units = selected["unit"].astype(str).tolist()
    metrics = selected["metric"].astype(str).tolist()

    candidates: list[tuple[dt.date, float, str]] = []
    for day, seconds, unit, metric in zip(dates, seconds_list, units, metrics, strict=True):
        if not earliest <= day <= today:
            continue
        # A duration metric is stored in seconds; anything else in this
        # position is a mislabelled row rather than a race result.
        if unit != "s" or not _MIN_PLAUSIBLE_5K_S <= seconds <= _MAX_PLAUSIBLE_5K_S:
            continue
        candidates.append((day, seconds, metric))
    return candidates


def personal_best(results: Sequence[tuple[dt.date, float]]) -> tuple[dt.date, float] | None:
    """Return the fastest of a set of dated results.

    Args:
        results: `(date, seconds)` pairs.

    Returns:
        The fastest pair, or None if there are none.
    """
    if not results:
        return None
    return min(results, key=lambda entry: entry[1])
