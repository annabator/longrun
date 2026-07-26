"""Block rotation, the weekly menu, rollover and adaptation."""

import datetime as dt

from longrun.analysis.adaptation import Adjustment, SessionOutcome
from longrun.analysis.adaptation import assess as adaptation_assess
from longrun.analysis.goals import Emphasis, Goals
from longrun.analysis.plan import (
    BACKLOG_CAP,
    BLOCK_WEEKS,
    Family,
    block_for,
    build_menu,
    outstanding_after,
    week_start_for,
)
from longrun.analysis.readiness import Readiness

MONDAY = dt.date(2026, 6, 15)


def test_blocks_hold_for_four_weeks_then_rotate() -> None:
    assert block_for(0) is Emphasis.BASE
    assert block_for(BLOCK_WEEKS - 1) is Emphasis.BASE
    assert block_for(BLOCK_WEEKS) is Emphasis.THRESHOLD
    assert block_for(BLOCK_WEEKS * 2) is Emphasis.VO2MAX
    assert block_for(BLOCK_WEEKS * 3) is Emphasis.CONSOLIDATION


def test_the_rotation_wraps_around() -> None:
    assert block_for(BLOCK_WEEKS * 4) is Emphasis.BASE


def test_week_start_is_the_monday_on_or_before() -> None:
    assert week_start_for(dt.date(2026, 6, 18)) == MONDAY
    assert week_start_for(MONDAY) == MONDAY


def test_the_menu_holds_the_number_of_runs_wanted() -> None:
    goals = Goals(emphasis=Emphasis.THRESHOLD, runs_per_week=4, quality_sessions=1)

    menu = build_menu(goals, MONDAY, week_index=0)

    assert len(menu.sessions) == 4


def test_the_menu_draws_quality_from_the_block() -> None:
    goals = Goals(emphasis=Emphasis.VO2MAX, runs_per_week=4, quality_sessions=1)

    menu = build_menu(goals, MONDAY, week_index=0)

    families = {planned.session.family for planned in menu.sessions}
    assert Family.VO2MAX in families


def test_quality_sessions_vary_from_week_to_week() -> None:
    """A block should not be the same session eight times."""
    goals = Goals(emphasis=Emphasis.THRESHOLD, runs_per_week=3, quality_sessions=1)

    picked = {
        build_menu(goals, MONDAY, week_index=week).sessions[0].session.name for week in range(4)
    }

    assert len(picked) > 1


def test_a_long_run_is_included_when_wanted() -> None:
    goals = Goals(runs_per_week=3, quality_sessions=0, long_run=True)

    menu = build_menu(goals, MONDAY, week_index=0)

    assert any(planned.session.family is Family.LONG for planned in menu.sessions)


def test_no_long_run_when_not_wanted() -> None:
    goals = Goals(runs_per_week=3, quality_sessions=0, long_run=False)

    menu = build_menu(goals, MONDAY, week_index=0)

    assert not any(planned.session.family is Family.LONG for planned in menu.sessions)


def test_outstanding_sessions_come_first_and_are_marked() -> None:
    goals = Goals(emphasis=Emphasis.THRESHOLD, runs_per_week=4, quality_sessions=1)
    carried = build_menu(goals, MONDAY, week_index=0).sessions[0].session

    menu = build_menu(goals, MONDAY, week_index=1, outstanding=[carried])

    assert menu.sessions[0].session.name == carried.name
    assert menu.sessions[0].rolled_over is True
    assert menu.sessions[0].priority == 1


def test_the_backlog_is_capped_and_the_oldest_dropped() -> None:
    """A fortnight of illness must not become a fortnight of debt."""
    goals = Goals(runs_per_week=4, quality_sessions=1)
    library = build_menu(goals, MONDAY, week_index=0).sessions
    outstanding = [planned.session for planned in library[:4]]

    menu = build_menu(goals, MONDAY, week_index=1, outstanding=outstanding)

    assert sum(1 for planned in menu.sessions if planned.rolled_over) == BACKLOG_CAP
    assert len(menu.dropped) == len(outstanding) - BACKLOG_CAP
    # The ones dropped are the oldest, not the most recent.
    assert menu.dropped == outstanding[: len(outstanding) - BACKLOG_CAP]


def test_a_completed_session_is_not_carried_forward() -> None:
    goals = Goals(emphasis=Emphasis.THRESHOLD, runs_per_week=3, quality_sessions=1)
    menu = build_menu(goals, MONDAY, week_index=0)
    done = [menu.sessions[0].session.name]

    remaining = outstanding_after(menu, done)

    assert all(session.name not in done for session in remaining)


def test_easy_runs_are_never_carried_forward() -> None:
    """Missing an easy run is not a debt worth repaying."""
    goals = Goals(runs_per_week=4, quality_sessions=0, long_run=False)
    menu = build_menu(goals, MONDAY, week_index=0)

    remaining = outstanding_after(menu, [])

    assert all(session.family is not Family.EASY for session in remaining)


def test_a_readiness_note_is_attached_to_quality_not_forced() -> None:
    goals = Goals(emphasis=Emphasis.THRESHOLD, runs_per_week=3, quality_sessions=1)

    menu = build_menu(goals, MONDAY, week_index=0, readiness_note="readiness is red")

    quality = next(
        planned for planned in menu.sessions if planned.session.family is Family.THRESHOLD
    )
    assert "readiness is red" in quality.note
    # The session is still on the menu; the note is guidance, not removal.
    assert quality.session.name


def test_adaptation_holds_without_enough_sessions() -> None:
    outcomes = [SessionOutcome(300.0, 290.0, 165.0, 160.0)]

    verdict = adaptation_assess(outcomes, [Readiness.GREEN] * 7)

    assert verdict.adjustment is Adjustment.HOLD
    assert "comparable sessions" in verdict.reason


def test_quicker_at_a_lower_heart_rate_suggests_a_retest() -> None:
    outcomes = [SessionOutcome(300.0, 290.0, 165.0, 160.0) for _ in range(3)]

    verdict = adaptation_assess(outcomes, [Readiness.GREEN] * 7)

    assert verdict.adjustment is Adjustment.RETEST
    assert "parkrun" in verdict.reason


def test_quicker_at_a_higher_heart_rate_is_not_a_retest() -> None:
    """Working harder is not the same as getting fitter."""
    outcomes = [SessionOutcome(300.0, 290.0, 165.0, 172.0) for _ in range(3)]

    verdict = adaptation_assess(outcomes, [Readiness.GREEN] * 7)

    assert verdict.adjustment is not Adjustment.RETEST


def test_missing_the_pace_every_time_suggests_easing() -> None:
    outcomes = [SessionOutcome(300.0, 320.0, 165.0, 168.0) for _ in range(3)]

    verdict = adaptation_assess(outcomes, [Readiness.GREEN] * 7)

    assert verdict.adjustment is Adjustment.EASE


def test_poor_readiness_outweighs_good_sessions() -> None:
    """Sessions going well is not a reason to add load on top of fatigue."""
    outcomes = [SessionOutcome(300.0, 290.0, 165.0, 160.0) for _ in range(3)]
    recent = [Readiness.RED, Readiness.AMBER, Readiness.RED, Readiness.AMBER, Readiness.GREEN]

    verdict = adaptation_assess(outcomes, recent)

    assert verdict.adjustment is Adjustment.EASE
    assert "readiness" in verdict.reason


def test_sessions_landing_where_expected_hold() -> None:
    outcomes = [
        SessionOutcome(300.0, 299.0, 165.0, 165.0),
        SessionOutcome(300.0, 295.0, 165.0, 166.0),
        SessionOutcome(300.0, 301.0, 165.0, 164.0),
    ]

    verdict = adaptation_assess(outcomes, [Readiness.GREEN] * 7)

    assert verdict.adjustment is Adjustment.HOLD
