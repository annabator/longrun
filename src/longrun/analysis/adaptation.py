"""Whether the training paces should move, and which way.

The question this answers is narrow on purpose: given what was prescribed and
what actually happened, is the current VDOT still the right anchor?

Two signals, in opposite directions:

* **Running quicker than prescribed at a lower heart rate** is the honest
  version of getting fitter. Either alone is not: quicker at a higher heart
  rate is just working harder, and lower heart rate at a slower pace is just
  an easier session.
* **Persistently poor readiness, or sessions consistently missed at pace**,
  argues for easing rather than pushing.

The output is a suggestion to re-test, not a silent adjustment. A VDOT that
drifts upward on its own eventually prescribes paces nobody can hit, and the
athlete has no idea why. A parkrun settles it properly.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from longrun.analysis.readiness import Readiness

PACE_MARGIN_PCT = 1.0
"""How much quicker than prescribed counts as genuinely quicker."""

HR_MARGIN_BPM = 2.0
"""How much lower the heart rate must be to count as easier."""

SESSIONS_FOR_SIGNAL = 3
"""Sessions agreeing before anything is suggested."""

POOR_READINESS_DAYS = 4
"""Amber or red days in the recent window before easing is suggested."""


class Adjustment(StrEnum):
    """What the evidence suggests doing.

    Attributes:
        HOLD: Nothing has changed enough to act on.
        RETEST: Sessions are going well enough that the anchor looks stale.
        EASE: Readiness or execution argues for backing off.
    """

    HOLD = "hold"
    RETEST = "retest"
    EASE = "ease"


@dataclass(frozen=True)
class SessionOutcome:
    """What was prescribed against what happened.

    Attributes:
        prescribed_pace_s_per_km: The pace the session called for.
        actual_pace_s_per_km: The pace actually run.
        prescribed_hr: Expected heart rate, if there was one.
        actual_hr: Heart rate actually seen.
    """

    prescribed_pace_s_per_km: float
    actual_pace_s_per_km: float
    prescribed_hr: float | None = None
    actual_hr: float | None = None

    @property
    def beat_the_pace(self) -> bool:
        """Whether the session was run meaningfully quicker than asked."""
        margin = self.prescribed_pace_s_per_km * (PACE_MARGIN_PCT / 100.0)
        return self.actual_pace_s_per_km <= self.prescribed_pace_s_per_km - margin

    @property
    def at_lower_effort(self) -> bool:
        """Whether it also cost meaningfully fewer beats."""
        if self.prescribed_hr is None or self.actual_hr is None:
            return False
        return self.actual_hr <= self.prescribed_hr - HR_MARGIN_BPM


@dataclass(frozen=True)
class AdaptationVerdict:
    """The suggestion and the reasoning behind it.

    Attributes:
        adjustment: What to do.
        reason: Why, in plain words.
    """

    adjustment: Adjustment
    reason: str

    def describe(self) -> str:
        """Return a one-line summary for the CLI."""
        return f"{self.adjustment}: {self.reason}"


def assess(
    outcomes: Sequence[SessionOutcome], recent_readiness: Sequence[Readiness]
) -> AdaptationVerdict:
    """Decide whether the anchor should move.

    Easing takes precedence over pushing: if readiness has been poor, the
    fact that sessions went well is not a reason to add more.

    Args:
        outcomes: Recent prescribed-versus-actual comparisons, oldest first.
        recent_readiness: Recent daily verdicts.

    Returns:
        The suggestion, with its reasoning.
    """
    poor_days = sum(
        1 for verdict in recent_readiness if verdict in (Readiness.AMBER, Readiness.RED)
    )
    if poor_days >= POOR_READINESS_DAYS:
        return AdaptationVerdict(
            adjustment=Adjustment.EASE,
            reason=(
                f"readiness has been amber or red on {poor_days} of the last "
                f"{len(recent_readiness)} days, which is worth respecting before "
                f"adding load"
            ),
        )

    if len(outcomes) < SESSIONS_FOR_SIGNAL:
        return AdaptationVerdict(
            adjustment=Adjustment.HOLD,
            reason=(
                f"only {len(outcomes)} comparable sessions logged; "
                f"{SESSIONS_FOR_SIGNAL} are needed before reading anything into them"
            ),
        )

    recent = outcomes[-SESSIONS_FOR_SIGNAL:]
    quicker = sum(1 for outcome in recent if outcome.beat_the_pace)
    quicker_and_easier = sum(
        1 for outcome in recent if outcome.beat_the_pace and outcome.at_lower_effort
    )

    if quicker_and_easier == len(recent):
        return AdaptationVerdict(
            adjustment=Adjustment.RETEST,
            reason=(
                f"the last {len(recent)} sessions were run quicker than prescribed at a "
                f"lower heart rate, so the anchor looks stale - a parkrun would settle it"
            ),
        )

    if quicker == 0:
        return AdaptationVerdict(
            adjustment=Adjustment.EASE,
            reason=(
                f"none of the last {len(recent)} sessions reached its prescribed pace, "
                f"which usually means the paces are ahead of current fitness"
            ),
        )

    return AdaptationVerdict(
        adjustment=Adjustment.HOLD,
        reason="sessions are landing about where they should; nothing argues for a change",
    )
