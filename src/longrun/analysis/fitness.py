"""Per-run measures of how training is actually going.

These are the figures that answer "am I getting fitter" without waiting for
another race. All of them come from stream data already stored, and all are
plain arithmetic over it.

Two are worth explaining, because they are easy to compute and easy to
misread:

* **Aerobic decoupling** compares the pace-to-heart-rate ratio in the first
  half of a run against the second. Drift upward means heart rate climbed to
  hold the same pace, which is the usual sign of an aerobic ceiling being
  reached. It only means anything on a long, steady effort - on intervals it
  measures the session structure, not the athlete.
* **Efficiency factor** is speed per heartbeat. Comparing it across runs is
  only fair between similar efforts, so it is reported per run and trended
  rather than averaged across everything.

Nothing here decides anything on its own; the plan engine reads these
alongside readiness.
"""

import statistics
from collections.abc import Sequence
from dataclasses import dataclass

import pandas as pd

MIN_SAMPLES_FOR_DECOUPLING = 600
"""Roughly ten minutes of per-second samples before a split means anything."""

MIN_HR_FOR_RATIO = 60.0
"""Below this a heart-rate reading is a dropout rather than a measurement."""


@dataclass(frozen=True)
class RunMetrics:
    """What one run says about current fitness.

    Attributes:
        efficiency_factor: Metres per minute per beat per minute.
        decoupling_pct: Percentage drift in the pace-to-heart-rate ratio
            between the first and second halves; positive means drifting.
        avg_hr: Mean heart rate across the samples used.
        avg_velocity: Mean velocity in metres per second.
        samples: How many stream samples contributed.
    """

    efficiency_factor: float | None
    decoupling_pct: float | None
    avg_hr: float | None
    avg_velocity: float | None
    samples: int


def run_metrics(streams: pd.DataFrame) -> RunMetrics:
    """Compute the fitness measures for one activity's streams.

    Args:
        streams: Per-sample rows with `t_s`, `hr` and `velocity` columns.

    Returns:
        The measures, with any that cannot be computed left as None.
    """
    if streams.empty or "hr" not in streams or "velocity" not in streams:
        return RunMetrics(None, None, None, None, 0)

    usable = streams.loc[
        streams["hr"].notna()
        & streams["velocity"].notna()
        & (streams["hr"] >= MIN_HR_FOR_RATIO)
        & (streams["velocity"] > 0)
    ]
    if usable.empty:
        return RunMetrics(None, None, None, None, 0)

    avg_hr = float(usable["hr"].mean())
    avg_velocity = float(usable["velocity"].mean())
    return RunMetrics(
        efficiency_factor=efficiency_factor(avg_velocity, avg_hr),
        decoupling_pct=decoupling(usable),
        avg_hr=avg_hr,
        avg_velocity=avg_velocity,
        samples=len(usable),
    )


def efficiency_factor(velocity_m_per_s: float, avg_hr: float) -> float | None:
    """Return speed per heartbeat, in metres per minute per bpm.

    Args:
        velocity_m_per_s: Mean velocity in metres per second.
        avg_hr: Mean heart rate.

    Returns:
        The efficiency factor, or None if the heart rate is unusable.
    """
    if avg_hr < MIN_HR_FOR_RATIO:
        return None
    return (velocity_m_per_s * 60.0) / avg_hr


def decoupling(streams: pd.DataFrame) -> float | None:
    """Return the aerobic decoupling across a run, as a percentage.

    Splits the samples in half by time and compares the pace-to-heart-rate
    ratio of each. Requires enough samples to make halves meaningful.

    Args:
        streams: Usable per-sample rows, already filtered.

    Returns:
        The percentage drift, positive meaning heart rate drifted upward for
        the same pace, or None if there is too little to split.
    """
    if len(streams) < MIN_SAMPLES_FOR_DECOUPLING:
        return None

    ordered = streams.sort_values("t_s")
    midpoint = len(ordered) // 2
    first, second = ordered.iloc[:midpoint], ordered.iloc[midpoint:]

    first_ratio = _ratio(first)
    second_ratio = _ratio(second)
    if first_ratio is None or second_ratio is None or first_ratio == 0:
        return None
    return (first_ratio - second_ratio) / first_ratio * 100.0


def pace_at_heart_rate(
    streams: pd.DataFrame, target_hr: float, tolerance_bpm: float = 5.0
) -> float | None:
    """Return the mean pace held around a given heart rate.

    Tracking this across runs is the clearest single answer to whether
    aerobic fitness is improving: the same heart rate carrying a quicker
    pace is what getting fitter looks like.

    Args:
        streams: Per-sample rows with `hr` and `velocity`.
        target_hr: The heart rate to look around.
        tolerance_bpm: How far either side still counts.

    Returns:
        Mean pace in seconds per kilometre, or None if the run never sat
        there for long.
    """
    if streams.empty or "hr" not in streams or "velocity" not in streams:
        return None
    window = streams.loc[
        streams["hr"].between(target_hr - tolerance_bpm, target_hr + tolerance_bpm)
        & streams["velocity"].notna()
        & (streams["velocity"] > 0)
    ]
    if window.empty:
        return None
    velocity = float(window["velocity"].mean())
    return 1000.0 / velocity


def trend(values: Sequence[float]) -> float | None:
    """Return the median of the second half minus that of the first.

    A deliberately blunt comparison rather than a fitted slope: with a
    handful of irregular runs a regression line invites more confidence than
    the data supports, while a difference of medians says only what it says.

    Args:
        values: The series, oldest first.

    Returns:
        The difference, or None if there is too little to halve.
    """
    minimum = 4  # two per half
    if len(values) < minimum:
        return None
    midpoint = len(values) // 2
    return statistics.median(values[midpoint:]) - statistics.median(values[:midpoint])


def _ratio(streams: pd.DataFrame) -> float | None:
    """Return mean velocity divided by mean heart rate for a slice."""
    if streams.empty:
        return None
    avg_hr = float(streams["hr"].mean())
    if avg_hr < MIN_HR_FOR_RATIO:
        return None
    return float(streams["velocity"].mean()) / avg_hr
