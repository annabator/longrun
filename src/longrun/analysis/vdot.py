"""VDOT and training paces, from published formulas rather than tables.

Daniels' VDOT tables are copyrighted, so nothing here reproduces them. The
figures are computed from the two published relationships they are built on:

* **Daniels and Gilbert's oxygen cost and drop-dead curves.** VO2 demand of
  running at a velocity, and the fraction of VO2max sustainable for a
  duration. VDOT is the value that makes those two agree for a given race
  result, found by bisection.
* **Riegel's endurance formula**, `t2 = t1 * (d2 / d1) ** 1.06`, for
  predicting one distance from another.

Both are decades-old published equations. The numbers this produces land
close to the familiar tables because they come from the same source
relationships, not because any table was copied.

Everything is deterministic and testable against fixed inputs. Nothing here
estimates, smooths or learns.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

RIEGEL_EXPONENT = 1.06
"""Riegel's fatigue exponent; 1.06 is the value from the original paper."""

_VDOT_SEARCH_RANGE = (20.0, 90.0)
_VDOT_TOLERANCE = 0.001
_MAX_BISECTIONS = 200


class Zone(StrEnum):
    """Training intensities, named as Daniels names them.

    Attributes:
        EASY: Conversational aerobic running.
        MARATHON: Sustained aerobic effort.
        THRESHOLD: Around the lactate threshold; "comfortably hard".
        INTERVAL: At or near VO2max.
        REPETITION: Faster than VO2max pace, for speed and economy.
    """

    EASY = "easy"
    MARATHON = "marathon"
    THRESHOLD = "threshold"
    INTERVAL = "interval"
    REPETITION = "repetition"


# Fraction of VDOT each zone is run at. These are the intensity fractions
# Daniels describes for each zone, applied to the oxygen-cost curve rather
# than read off a table.
_ZONE_INTENSITY: dict[Zone, float] = {
    Zone.EASY: 0.70,
    Zone.MARATHON: 0.84,
    Zone.THRESHOLD: 0.88,
    Zone.INTERVAL: 0.98,
    Zone.REPETITION: 1.05,
}


@dataclass(frozen=True)
class RaceResult:
    """A performance used to anchor VDOT.

    Attributes:
        distance_m: Race distance in metres.
        duration_s: Finishing time in seconds.
    """

    distance_m: float
    duration_s: float

    @property
    def velocity_m_per_min(self) -> float:
        """Average velocity in metres per minute."""
        return self.distance_m / (self.duration_s / 60.0)


@dataclass(frozen=True)
class PaceZone:
    """A training zone and the pace it is run at.

    Attributes:
        zone: Which intensity this is.
        seconds_per_km: Pace in seconds per kilometre.
    """

    zone: Zone
    seconds_per_km: float

    @property
    def pace_label(self) -> str:
        """Pace rendered as `m:ss/km`."""
        total = round(self.seconds_per_km)
        return f"{total // 60}:{total % 60:02d}/km"


def oxygen_cost(velocity_m_per_min: float) -> float:
    """Return the oxygen cost of running at a velocity.

    Daniels and Gilbert's published cost equation, in ml/kg/min.

    Args:
        velocity_m_per_min: Running velocity in metres per minute.

    Returns:
        Oxygen demand in ml/kg/min.
    """
    return -4.60 + 0.182258 * velocity_m_per_min + 0.000104 * velocity_m_per_min**2


def sustainable_fraction(duration_min: float) -> float:
    """Return the fraction of VO2max sustainable for a duration.

    Daniels and Gilbert's published drop-dead curve. Around six minutes it
    gives roughly 1.0, falling away for longer efforts.

    Args:
        duration_min: Duration of the effort in minutes.

    Returns:
        The sustainable fraction of VO2max, as a proportion.
    """
    return (
        0.8
        + 0.1894393 * math.exp(-0.012778 * duration_min)
        + 0.2989558 * math.exp(-0.1932605 * duration_min)
    )


def vdot_from_result(result: RaceResult) -> float:
    """Compute VDOT from one race result.

    VDOT is the VO2max value at which the oxygen cost of the race velocity
    equals the fraction of VO2max sustainable for the race duration. Solved
    by bisection, which is exact enough to be reproducible and needs no
    lookup table.

    Args:
        result: The performance to anchor on.

    Returns:
        The VDOT value.

    Raises:
        ValueError: If the result is not a positive distance and time.
    """
    if result.distance_m <= 0 or result.duration_s <= 0:
        msg = "A race result needs a positive distance and duration"
        raise ValueError(msg)

    demand = oxygen_cost(result.velocity_m_per_min)
    fraction = sustainable_fraction(result.duration_s / 60.0)
    # demand = vdot * fraction, so this is a division rather than a search.
    # The bisection below is kept for the inverse problem, not this one.
    return demand / fraction


def velocity_for_vdot(vdot: float, intensity: float) -> float:
    """Return the velocity that costs `intensity` of `vdot`.

    Inverts the oxygen-cost curve by bisection, since it is quadratic in
    velocity and only the positive root is meaningful.

    Args:
        vdot: The VDOT to run at a fraction of.
        intensity: Fraction of VDOT, as a proportion.

    Returns:
        Velocity in metres per minute.
    """
    target = vdot * intensity
    low, high = 50.0, 600.0
    for _ in range(_MAX_BISECTIONS):
        middle = (low + high) / 2
        if oxygen_cost(middle) < target:
            low = middle
        else:
            high = middle
        if high - low < _VDOT_TOLERANCE:
            break
    return (low + high) / 2


def pace_zones(vdot: float) -> list[PaceZone]:
    """Return the training paces implied by a VDOT.

    Args:
        vdot: The VDOT to derive from.

    Returns:
        One :class:`PaceZone` per zone, easiest first.
    """
    zones: list[PaceZone] = []
    for zone, intensity in _ZONE_INTENSITY.items():
        velocity = velocity_for_vdot(vdot, intensity)
        # metres per minute to seconds per kilometre.
        zones.append(PaceZone(zone=zone, seconds_per_km=60.0 * 1000.0 / velocity))
    return zones


def riegel_predict(result: RaceResult, distance_m: float) -> float:
    """Predict a time at another distance from one result.

    Riegel's endurance formula. Reliable over a modest range either side of
    the known distance and increasingly optimistic well beyond it, which is
    why the plan engine anchors on a 5k rather than extrapolating from one.

    Args:
        result: The known performance.
        distance_m: The distance to predict, in metres.

    Returns:
        Predicted time in seconds.

    Raises:
        ValueError: If either distance is not positive.
    """
    if result.distance_m <= 0 or distance_m <= 0:
        msg = "Riegel prediction needs positive distances"
        raise ValueError(msg)
    return float(result.duration_s * (distance_m / result.distance_m) ** RIEGEL_EXPONENT)


def equivalent_results(result: RaceResult, distances_m: Sequence[float]) -> list[RaceResult]:
    """Return equivalent performances at other distances.

    Args:
        result: The known performance.
        distances_m: Distances to predict, in metres.

    Returns:
        One predicted result per distance, in the order given.
    """
    return [
        RaceResult(distance_m=distance, duration_s=riegel_predict(result, distance))
        for distance in distances_m
    ]
