"""Derive cycle structure from logged period days.

Nothing here is stored. Logged days are the raw record; cycles, cycle day and
phase are computed on demand, in keeping with the rest of the project's
raw-data-in, interpretation-out shape.

Everything this module returns is an estimate and says so. Two limits drive
that, and both are structural rather than fixable with better arithmetic:

* **Ovulation is inferred, not observed.** Without temperature or hormone
  data, the only usable regularity is that the luteal phase - ovulation to
  the next period - is more consistent than the follicular one. So ovulation
  is estimated *backwards* from the next period start, which means a
  completed cycle can be described far better than the one in progress.
* **The current cycle is a projection.** Its phases lean on a median cycle
  length that a change in cycle length invalidates without warning.

Phase boundaries are therefore soft, and every estimate carries the reason it
should or should not be trusted.
"""

import datetime as dt
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

NEW_CYCLE_GAP_DAYS = 10
"""A gap this long between logged days starts a new cycle.

Comfortably longer than a missed log or two inside one period, and
comfortably shorter than the gap between periods even on a short cycle.
"""

LUTEAL_PHASE_DAYS = 14
"""Assumed days from ovulation to the next period start.

The luteal phase is the more consistent half, which is what makes counting
back from the next period a better estimator than counting forward to a
fixed day 14.
"""

OVULATION_WINDOW_DAYS = 1
"""Days either side of the estimate still treated as the ovulation window."""

TYPICAL_CYCLE_DAYS = range(21, 36)
"""Cycle lengths treated as regular; outside this, confidence drops."""

MIN_CYCLES_FOR_MEDIAN = 3
"""Completed cycles needed before a median length is worth leaning on."""

DEFAULT_CYCLE_DAYS = 28
"""Fallback length used only when nothing better has been logged yet."""


class Phase(StrEnum):
    """A cycle phase, as an estimate rather than a diagnosis."""

    MENSTRUAL = "menstrual"
    FOLLICULAR = "follicular"
    OVULATION = "ovulation"
    LUTEAL = "luteal"
    UNKNOWN = "unknown"


class Confidence(StrEnum):
    """How much weight an estimate can carry.

    Attributes:
        ESTIMATED: A completed cycle of typical length.
        PROVISIONAL: The cycle in progress, projected from a median length.
        LOW: Irregular length, or too little history to have a median.
        NONE: Nothing logged that covers the date at all.
    """

    ESTIMATED = "estimated"
    PROVISIONAL = "provisional"
    LOW = "low"
    NONE = "none"


@dataclass(frozen=True)
class Cycle:
    """One cycle, bounded by the period days that start and end it.

    Attributes:
        start: First logged day of the period that opens the cycle.
        period_end: Last logged day of that period.
        next_start: First day of the following cycle, or None if this is the
            cycle in progress.
    """

    start: dt.date
    period_end: dt.date
    next_start: dt.date | None

    @property
    def is_complete(self) -> bool:
        """Whether the following period has been logged."""
        return self.next_start is not None

    @property
    def length_days(self) -> int | None:
        """Length in days, or None while the cycle is still in progress."""
        if self.next_start is None:
            return None
        return (self.next_start - self.start).days

    @property
    def period_days(self) -> int:
        """How many days the opening period spanned, ends inclusive."""
        return (self.period_end - self.start).days + 1

    def contains(self, day: dt.date) -> bool:
        """Return whether `day` falls inside this cycle.

        Args:
            day: The date to test.

        Returns:
            True if the date is on or after the start and before the next.
        """
        if day < self.start:
            return False
        if self.next_start is None:
            return True
        return day < self.next_start


@dataclass(frozen=True)
class PhaseEstimate:
    """A phase and cycle day for one date, with the caveats attached.

    Attributes:
        phase: The estimated phase.
        cycle_day: Day within the cycle, counting the first period day as 1.
        confidence: How much the estimate can carry.
        reason: Why it carries that much, in plain words.
    """

    phase: Phase
    cycle_day: int | None
    confidence: Confidence
    reason: str


def cycles_from_days(days: Sequence[dt.date]) -> list[Cycle]:
    """Group logged period days into cycles.

    Consecutive or near-consecutive days form one period; a gap of
    :data:`NEW_CYCLE_GAP_DAYS` or more starts the next cycle. Grouping this
    way tolerates a missed log inside a period without splitting it.

    Args:
        days: Logged period days, in any order.

    Returns:
        Cycles in date order, the last one in progress unless a later period
        has been logged.
    """
    ordered = sorted(set(days))
    if not ordered:
        return []

    groups: list[list[dt.date]] = [[ordered[0]]]
    for day in ordered[1:]:
        if (day - groups[-1][-1]).days >= NEW_CYCLE_GAP_DAYS:
            groups.append([day])
        else:
            groups[-1].append(day)

    cycles: list[Cycle] = []
    for index, group in enumerate(groups):
        next_start = groups[index + 1][0] if index + 1 < len(groups) else None
        cycles.append(Cycle(start=group[0], period_end=group[-1], next_start=next_start))
    return cycles


def median_cycle_length(cycles: Sequence[Cycle]) -> int | None:
    """Return the median length of the completed cycles.

    Args:
        cycles: Cycles, typically from :func:`cycles_from_days`.

    Returns:
        The median in days, or None if no cycle has completed.
    """
    lengths = [cycle.length_days for cycle in cycles if cycle.length_days is not None]
    if not lengths:
        return None
    return int(statistics.median(lengths))


def ovulation_estimate(cycle: Cycle, median_length: int | None) -> dt.date | None:
    """Estimate the ovulation date for one cycle.

    Counted back from the next period start, since the luteal phase is the
    more consistent half. While a cycle is still in progress there is no next
    start to count back from, so the median length stands in and the result is
    a projection.

    Args:
        cycle: The cycle to estimate for.
        median_length: Median completed cycle length, if known.

    Returns:
        The estimated date, or None if there is no basis for one.
    """
    if cycle.next_start is not None:
        return cycle.next_start - dt.timedelta(days=LUTEAL_PHASE_DAYS)
    length = median_length or DEFAULT_CYCLE_DAYS
    return cycle.start + dt.timedelta(days=length - LUTEAL_PHASE_DAYS)


def phase_for(day: dt.date, cycles: Sequence[Cycle]) -> PhaseEstimate:
    """Estimate the phase and cycle day for one date.

    Args:
        day: The date to describe.
        cycles: Cycles, typically from :func:`cycles_from_days`.

    Returns:
        The estimate, including why it should or should not be trusted.
    """
    cycle = next((candidate for candidate in cycles if candidate.contains(day)), None)
    if cycle is None:
        return PhaseEstimate(
            phase=Phase.UNKNOWN,
            cycle_day=None,
            confidence=Confidence.NONE,
            reason="no period logged on or before this date",
        )

    cycle_day = (day - cycle.start).days + 1
    median_length = median_cycle_length(cycles)
    confidence, reason = _confidence_for(cycle, cycles, median_length)
    ovulation = ovulation_estimate(cycle, median_length)

    phase = _phase_for(day, cycle, ovulation)
    return PhaseEstimate(phase=phase, cycle_day=cycle_day, confidence=confidence, reason=reason)


def cycle_day_for(day: dt.date, cycles: Sequence[Cycle]) -> int | None:
    """Return the cycle day for one date, counting the first period day as 1.

    Args:
        day: The date to place.
        cycles: Cycles, typically from :func:`cycles_from_days`.

    Returns:
        The cycle day, or None if no cycle covers the date.
    """
    cycle = next((candidate for candidate in cycles if candidate.contains(day)), None)
    if cycle is None:
        return None
    return (day - cycle.start).days + 1


def _phase_for(day: dt.date, cycle: Cycle, ovulation: dt.date | None) -> Phase:
    """Place a date in a phase, given the cycle it falls in."""
    if day <= cycle.period_end:
        return Phase.MENSTRUAL
    if ovulation is None:
        return Phase.UNKNOWN
    window = dt.timedelta(days=OVULATION_WINDOW_DAYS)
    if ovulation - window <= day <= ovulation + window:
        return Phase.OVULATION
    if day < ovulation:
        return Phase.FOLLICULAR
    return Phase.LUTEAL


def _confidence_for(
    cycle: Cycle, cycles: Sequence[Cycle], median_length: int | None
) -> tuple[Confidence, str]:
    """Decide how far an estimate for this cycle can be trusted."""
    completed = sum(1 for candidate in cycles if candidate.is_complete)

    if not cycle.is_complete:
        if completed < MIN_CYCLES_FOR_MEDIAN:
            return (
                Confidence.LOW,
                f"cycle in progress and only {completed} completed cycles logged, "
                f"so its length is guessed rather than projected",
            )
        return (
            Confidence.PROVISIONAL,
            f"cycle in progress, projected from a median of {median_length} days",
        )

    length = cycle.length_days
    if length is not None and length not in TYPICAL_CYCLE_DAYS:
        return (
            Confidence.LOW,
            f"cycle length of {length} days is outside the usual range, "
            f"so the ovulation estimate is weak",
        )
    return (
        Confidence.ESTIMATED,
        f"completed cycle of {length} days, ovulation counted back from the next period",
    )
