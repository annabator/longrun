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

import pandas as pd

from longrun.analysis.vdot import RaceResult, pace_zones, vdot_from_result
from longrun.models import Metric

ANCHOR_WINDOW_DAYS = 120
"""How far back an anchoring result may be.

About four months: long enough to survive a quiet spell, short enough that
paces follow fitness rather than trailing a season behind.
"""

FIVE_K_M = 5000.0

_MIN_PLAUSIBLE_5K_S = 12 * 60
_MAX_PLAUSIBLE_5K_S = 60 * 60


@dataclass(frozen=True)
class Anchor:
    """The result the training paces are derived from.

    Attributes:
        result: The performance itself.
        date: When it was run.
        source: Where it came from, for the CLI to report.
        vdot: The VDOT it implies.
    """

    result: RaceResult
    date: dt.date
    source: str
    vdot: float

    def describe(self) -> str:
        """Return a one-line summary for the CLI."""
        minutes, seconds = divmod(round(self.result.duration_s), 60)
        return (
            f"VDOT {self.vdot:.1f} from a {minutes}:{seconds:02d} 5k "
            f"on {self.date:%Y-%m-%d} ({self.source})"
        )


def find_anchor(
    tests: pd.DataFrame, today: dt.date, window_days: int = ANCHOR_WINDOW_DAYS
) -> Anchor | None:
    """Return the best recent 5k to anchor training paces on.

    Args:
        tests: The long-format tests frame.
        today: The date the window is measured back from.
        window_days: How far back a result may be.

    Returns:
        The anchor, or None if no usable result was logged in the window.
    """
    candidates = _candidate_results(tests, today, window_days)
    if not candidates:
        return None

    # Fastest in the window: a single slow morning must not reset every pace.
    best = min(candidates, key=lambda entry: entry[1])
    day, seconds, source = best
    result = RaceResult(distance_m=FIVE_K_M, duration_s=seconds)
    return Anchor(result=result, date=day, source=source, vdot=vdot_from_result(result))


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
