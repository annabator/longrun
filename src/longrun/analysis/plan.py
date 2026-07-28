"""The weekly session menu and the block rotation behind it.

Two decisions shape this module, and both are deliberate departures from how
training plans usually work.

**The menu is not a schedule.** It is a prioritised list of what is worth
doing this week, not a set of days. Nothing is assigned to Tuesday, nothing
is late, and a session not done rolls over rather than being missed. Life is
the thing that actually decides which day a run happens on, and a plan that
pretends otherwise gets abandoned in week three.

**Rollover is capped.** An uncapped backlog turns a fortnight of illness into
a fortnight of debt, which is exactly when someone is least able to pay it.
Beyond the cap the oldest outstanding sessions are dropped, because a week
that has already gone is not worth catching up on.

Variety is structural rather than decorative: the session library holds
several shapes of each quality family, and the picker rotates through them so
that a threshold block is not eight identical sessions.

Everything is a rule with a number, testable against fixed inputs.
"""

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from statistics import median

from longrun.analysis.goals import Emphasis, Goals
from longrun.analysis.vdot import Zone

BLOCK_WEEKS = 4
"""Weeks per emphasis block before rotating to the next."""

BACKLOG_CAP = 2
"""Most outstanding sessions carried forward. Beyond this the oldest go.

Two is roughly one missed week. Past that the sensible thing is to resume,
not to repay.
"""

BLOCK_ROTATION: tuple[Emphasis, ...] = (
    Emphasis.BASE,
    Emphasis.THRESHOLD,
    Emphasis.VO2MAX,
    Emphasis.CONSOLIDATION,
)
"""Standard rotation: build aerobically, sharpen, peak, absorb, repeat."""


class Family(StrEnum):
    """What kind of session this is, irrespective of its exact shape."""

    EASY = "easy"
    LONG = "long"
    THRESHOLD = "threshold"
    VO2MAX = "vo2max"
    HILLS = "hills"
    STRIDES = "strides"


@dataclass(frozen=True)
class Session:
    """One session the menu can suggest.

    Attributes:
        name: How it is described, e.g. `5x1000`.
        family: Which family it belongs to.
        zone: The pace zone its hard portion is run at.
        description: What to actually do.
        work_km: Roughly how far the hard portion covers, for sizing the
            session as a whole. Nominal, since a time-based rep covers
            different ground at different fitness; it is only ever used to
            suggest a total, never to prescribe one.
    """

    name: str
    family: Family
    zone: Zone
    description: str
    work_km: float = 0.0


# Several shapes per family, so a block rotates through them rather than
# repeating one session until it is stale.
SESSION_LIBRARY: tuple[Session, ...] = (
    Session("5x1000", Family.THRESHOLD, Zone.THRESHOLD, "5 x 1000m at threshold, 90s jog", 5.0),
    Session(
        "4x4min", Family.THRESHOLD, Zone.THRESHOLD, "4 x 4 minutes at threshold, 2min jog", 2.6
    ),
    Session(
        "2x15min", Family.THRESHOLD, Zone.THRESHOLD, "2 x 15 minutes at threshold, 3min jog", 4.9
    ),
    Session(
        "20min tempo", Family.THRESHOLD, Zone.THRESHOLD, "20 minutes continuous at threshold", 3.3
    ),
    Session("6x800", Family.VO2MAX, Zone.INTERVAL, "6 x 800m at interval pace, 2min jog", 4.8),
    Session(
        "5x3min", Family.VO2MAX, Zone.INTERVAL, "5 x 3 minutes at interval pace, 2min jog", 2.7
    ),
    Session("30/30s", Family.VO2MAX, Zone.INTERVAL, "12 x 30s hard / 30s easy, continuous", 2.2),
    Session("8x400", Family.VO2MAX, Zone.REPETITION, "8 x 400m at repetition pace, 90s jog", 3.2),
    Session("hill reps", Family.HILLS, Zone.INTERVAL, "8 x 45s uphill hard, jog down", 1.8),
    Session("long hills", Family.HILLS, Zone.THRESHOLD, "5 x 2min uphill steady, jog down", 3.0),
    Session(
        "strides",
        Family.STRIDES,
        Zone.REPETITION,
        "6 x 20s relaxed and fast after an easy run",
        0.6,
    ),
    Session("long run", Family.LONG, Zone.EASY, "Long run, conversational throughout"),
    Session("easy run", Family.EASY, Zone.EASY, "Easy run, conversational throughout"),
)

# Which quality families each block draws on, in preference order.
_BLOCK_FAMILIES: dict[Emphasis, tuple[Family, ...]] = {
    Emphasis.BASE: (Family.STRIDES, Family.HILLS),
    Emphasis.THRESHOLD: (Family.THRESHOLD, Family.HILLS),
    Emphasis.VO2MAX: (Family.VO2MAX, Family.THRESHOLD),
    Emphasis.CONSOLIDATION: (Family.STRIDES, Family.THRESHOLD),
}


LONG_RUN_GROWTH = 1.05
"""How far a long run may exceed the longest run actually done recently.

Five per cent a week compounds quickly enough to build and slowly enough
that nothing snaps. Suggesting a distance far beyond anything recent is how
a plan gets ignored, or gets followed and causes an injury.
"""

LONG_RUN_MIN_RATIO = 1.4
"""A long run has to be meaningfully longer than an easy one to be one."""

QUALITY_SUPPORT_KM = 2.5
"""Warm-up and cool-down around the hard part of a session."""

MIN_RUNS_FOR_SIZING = 4
"""Runs needed before a distance suggestion is worth making."""

SIZING_WINDOW_WEEKS = 8
"""How far back to look when sizing sessions.

Long enough to survive a quiet fortnight, short enough that the suggestion
follows current form rather than last season's.
"""


@dataclass(frozen=True)
class Sizing:
    """Suggested distances, calibrated to what is actually being run.

    Sizing a long run as a share of weekly volume is the usual approach and
    it collapses at low mileage: thirty per cent of a ten-kilometre week is
    three kilometres, which is not a long run by any reading. These figures
    come from the distribution of recent runs instead, so they stay
    recognisable whatever the volume.

    Attributes:
        easy_km: A typical easy run, the median of recent runs.
        long_km: A long run, just beyond the longest done recently.
    """

    easy_km: float
    long_km: float


def size_from_history(distances_km: Sequence[float]) -> Sizing | None:
    """Derive session distances from recent runs.

    Args:
        distances_km: Distances of recent runs, in kilometres.

    Returns:
        The suggested sizes, or None if there is too little history to
        suggest anything honestly.
    """
    usable = sorted(distance for distance in distances_km if distance > 0)
    if len(usable) < MIN_RUNS_FOR_SIZING:
        return None

    easy = float(median(usable))
    # Just past the longest recent run, but never so close to the easy run
    # that the two stop being different sessions.
    long_run = max(usable[-1] * LONG_RUN_GROWTH, easy * LONG_RUN_MIN_RATIO)
    return Sizing(easy_km=round(easy, 1), long_km=round(long_run, 1))


def suggested_distance(session: Session, sizing: Sizing | None) -> float | None:
    """Return how far a session might be, in kilometres.

    Args:
        session: The session to size.
        sizing: Distances derived from recent runs, if available.

    Returns:
        A suggested total distance, or None when there is no basis for one.
    """
    if sizing is None:
        return None
    if session.family is Family.LONG:
        return sizing.long_km
    if session.family is Family.EASY:
        return sizing.easy_km
    if session.family is Family.STRIDES:
        # Strides hang off the end of an easy run rather than replacing it.
        return round(sizing.easy_km + session.work_km, 1)
    return round(session.work_km + QUALITY_SUPPORT_KM, 1)


@dataclass(frozen=True)
class PlannedSession:
    """A session on the menu, with why it is there.

    Attributes:
        session: What to do.
        priority: 1 is the most worthwhile thing this week.
        rolled_over: Whether it is outstanding from a previous week.
        note: Spacing or readiness guidance; never an instruction.
        distance_km: Roughly how far, sized from recent running.
        duration_min: Roughly how long at easy pace, where that is the
            useful way to think about it.
    """

    session: Session
    priority: int
    rolled_over: bool = False
    note: str = ""
    distance_km: float | None = None
    duration_min: float | None = None

    def size_label(self) -> str:
        """Return a short description of the suggested size, if there is one."""
        if self.distance_km is None:
            return ""
        if self.duration_min is None:
            return f"~{self.distance_km:g} km"
        return f"~{self.distance_km:g} km ({round(self.duration_min)} min)"


@dataclass(frozen=True)
class WeeklyMenu:
    """What is worth doing this week.

    Attributes:
        week_start: Monday of the week described.
        emphasis: The block this week sits in.
        sessions: The menu, most worthwhile first.
        dropped: Sessions let go because the backlog was capped.
    """

    week_start: dt.date
    emphasis: Emphasis
    sessions: list[PlannedSession] = field(default_factory=list)
    dropped: list[Session] = field(default_factory=list)

    def describe(self) -> str:
        """Return a one-line summary for the CLI."""
        return f"{self.emphasis} block, {len(self.sessions)} sessions on the menu"


def block_for(week_index: int, rotation: Sequence[Emphasis] = BLOCK_ROTATION) -> Emphasis:
    """Return the emphasis for a week, counting from the start of training.

    Args:
        week_index: Zero-based week number.
        rotation: The block order to cycle through.

    Returns:
        The emphasis in force that week.
    """
    return rotation[(week_index // BLOCK_WEEKS) % len(rotation)]


def week_start_for(day: dt.date) -> dt.date:
    """Return the Monday on or before `day`."""
    return day - dt.timedelta(days=day.weekday())


def build_menu(
    goals: Goals,
    week_start: dt.date,
    week_index: int,
    outstanding: Sequence[Session] = (),
    readiness_note: str = "",
    sizing: Sizing | None = None,
    easy_pace_s_per_km: float | None = None,
) -> WeeklyMenu:
    """Build the week's menu.

    Rolled-over sessions come first: something outstanding is more worth
    doing than something new of the same kind. Beyond the backlog cap the
    oldest are dropped rather than accumulating.

    Args:
        goals: What the athlete is trying to do.
        week_start: Monday of the week.
        week_index: Zero-based week number, for the block rotation.
        outstanding: Sessions not done in previous weeks, oldest first.
        readiness_note: Guidance to attach to the hardest session, if any.
        sizing: Distances derived from recent running, for suggesting how
            far each session might be.
        easy_pace_s_per_km: Easy pace from the current anchor, used to turn
            an easy or long distance into a time on feet.

    Returns:
        The menu, most worthwhile first.
    """
    emphasis = goals.emphasis if goals.emphasis else block_for(week_index)
    carried = list(outstanding)[-BACKLOG_CAP:]
    dropped = list(outstanding)[: max(0, len(outstanding) - BACKLOG_CAP)]

    sessions: list[PlannedSession] = []
    priority = 1

    for session in carried:
        sessions.append(
            PlannedSession(
                session=session,
                priority=priority,
                rolled_over=True,
                note="outstanding from a previous week",
            )
        )
        priority += 1

    quality_wanted = max(0, goals.quality_sessions - sum(1 for s in carried if _is_quality(s)))
    for session in _quality_for(emphasis, week_index, quality_wanted):
        sessions.append(PlannedSession(session=session, priority=priority, note=readiness_note))
        priority += 1

    if goals.long_run and not any(s.session.family is Family.LONG for s in sessions):
        sessions.append(
            PlannedSession(
                session=_by_name("long run"),
                priority=priority,
                note="keep it conversational; it is the aerobic work, not a test",
            )
        )
        priority += 1

    while len(sessions) < goals.runs_per_week:
        sessions.append(
            PlannedSession(
                session=_by_name("easy run"),
                priority=priority,
                note="easy means easy; this is most of the training",
            )
        )
        priority += 1

    sized = [_with_size(planned, sizing, easy_pace_s_per_km) for planned in sessions]
    return WeeklyMenu(week_start=week_start, emphasis=emphasis, sessions=sized, dropped=dropped)


def _with_size(
    planned: PlannedSession, sizing: Sizing | None, easy_pace_s_per_km: float | None
) -> PlannedSession:
    """Attach a suggested distance, and a duration where time is the point."""
    distance = suggested_distance(planned.session, sizing)
    duration = None
    # Time on feet is how an easy or long run is judged. A rep session is
    # judged by the reps, and a total time for one would only mislead.
    if distance is not None and easy_pace_s_per_km and planned.session.zone is Zone.EASY:
        duration = distance * easy_pace_s_per_km / 60.0
    return replace(planned, distance_km=distance, duration_min=duration)


def outstanding_after(menu: WeeklyMenu, completed_names: Sequence[str]) -> list[Session]:
    """Return the menu items not matched by anything logged.

    Args:
        menu: The week's menu.
        completed_names: Names of sessions marked done.

    Returns:
        The sessions still outstanding, in menu order. Easy runs are not
        carried: missing one is not a debt worth repaying.
    """
    done = set(completed_names)
    return [
        planned.session
        for planned in menu.sessions
        if planned.session.name not in done and planned.session.family is not Family.EASY
    ]


def _is_quality(session: Session) -> bool:
    """Return whether a session counts against the weekly quality budget."""
    return session.family in (Family.THRESHOLD, Family.VO2MAX, Family.HILLS)


def _quality_for(emphasis: Emphasis, week_index: int, wanted: int) -> list[Session]:
    """Pick quality sessions for the block, rotating shapes week to week."""
    if wanted <= 0:
        return []
    families = _BLOCK_FAMILIES[emphasis]
    picked: list[Session] = []
    for slot in range(wanted):
        family = families[slot % len(families)]
        options = [session for session in SESSION_LIBRARY if session.family is family]
        if not options:
            continue
        # Rotating on the week index is what stops a block becoming eight
        # identical sessions.
        picked.append(options[(week_index + slot) % len(options)])
    return picked


def _by_name(name: str) -> Session:
    """Return a library session by name."""
    for session in SESSION_LIBRARY:
        if session.name == name:
            return session
    msg = f"No session named {name!r} in the library"
    raise KeyError(msg)
