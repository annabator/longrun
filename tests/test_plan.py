"""Block rotation, the weekly menu, rollover and adaptation."""

import datetime as dt

from longrun.analysis.adaptation import Adjustment, SessionOutcome
from longrun.analysis.adaptation import assess as adaptation_assess
from longrun.analysis.goals import Emphasis, Goals
from longrun.analysis.plan import (
    BACKLOG_CAP,
    BLOCK_WEEKS,
    LONG_RUN_MIN_RATIO,
    QUALITY_SUPPORT_KM,
    SESSION_LIBRARY,
    SIZING_STEP_KM,
    SIZING_WINDOW_WEEKS,
    Family,
    _to_step,
    block_for,
    build_menu,
    outstanding_after,
    size_from_history,
    suggested_distance,
    week_start_for,
)
from longrun.analysis.readiness import Readiness
from longrun.analysis.service import recent_run_distances
from longrun.models import Activity, Source
from longrun.store import Store

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


def test_sizing_needs_enough_runs_to_be_honest() -> None:
    assert size_from_history([5.0, 5.0, 6.0]) is None


def test_zero_and_negative_distances_do_not_count_as_runs() -> None:
    """A gym session stored with no distance must not drag the median down."""
    assert size_from_history([0.0, 0.0, 5.0, 5.0, 6.0]) is None


def test_easy_is_the_median_run_and_long_goes_just_beyond_the_longest() -> None:
    sizing = size_from_history([4.0, 5.0, 5.0, 6.0, 8.0])

    assert sizing is not None
    assert sizing.easy_km == 5.0
    assert sizing.long_km == 8.5  # 8.0 * LONG_RUN_GROWTH, to the nearest half


def test_a_long_run_stays_longer_than_an_easy_one() -> None:
    """Someone running the same distance every time still gets a long run."""
    sizing = size_from_history([5.0, 5.0, 5.0, 5.0])

    assert sizing is not None
    assert sizing.long_km >= sizing.easy_km * LONG_RUN_MIN_RATIO


def test_low_mileage_does_not_produce_a_nonsense_long_run() -> None:
    """The share-of-weekly-volume approach fails here; this one must not.

    Four 5 km runs is a 10 km week if they fall a fortnight apart. Thirty per
    cent of that is 3 km, which is shorter than every run listed.
    """
    sizing = size_from_history([5.0, 5.5, 5.0, 6.0])

    assert sizing is not None
    assert sizing.long_km > max([5.0, 5.5, 5.0, 6.0])


def test_quality_sessions_are_sized_around_the_hard_part() -> None:
    goals = Goals(emphasis=Emphasis.THRESHOLD, runs_per_week=3, quality_sessions=1)
    sizing = size_from_history([5.0, 5.0, 5.0, 5.0])

    menu = build_menu(goals, MONDAY, week_index=0, sizing=sizing)
    quality = next(s for s in menu.sessions if s.session.family is Family.THRESHOLD)

    assert quality.distance_km == _to_step(quality.session.work_km + QUALITY_SUPPORT_KM)
    # A rep session is judged by the reps, not by a total time.
    assert quality.duration_min is None


def test_strides_are_added_to_an_easy_run_rather_than_replacing_it() -> None:
    sizing = size_from_history([5.0, 5.0, 5.0, 5.0])
    strides = next(s for s in SESSION_LIBRARY if s.family is Family.STRIDES)

    assert sizing is not None
    distance = suggested_distance(strides, sizing)
    assert distance is not None
    assert distance > sizing.easy_km


def test_easy_and_long_runs_get_a_time_on_feet() -> None:
    goals = Goals(emphasis=Emphasis.THRESHOLD, runs_per_week=3, quality_sessions=1)
    sizing = size_from_history([6.0, 6.0, 6.0, 6.0])

    menu = build_menu(goals, MONDAY, week_index=0, sizing=sizing, easy_pace_s_per_km=360.0)
    long_run = next(s for s in menu.sessions if s.session.family is Family.LONG)

    assert long_run.distance_km == 8.5
    assert long_run.duration_min is not None
    assert round(long_run.duration_min) == 51  # 8.5 km at 6:00/km


def test_without_a_pace_a_distance_is_still_suggested() -> None:
    goals = Goals(emphasis=Emphasis.THRESHOLD, runs_per_week=3, quality_sessions=1)

    menu = build_menu(goals, MONDAY, week_index=0, sizing=size_from_history([6.0] * 4))
    long_run = next(s for s in menu.sessions if s.session.family is Family.LONG)

    assert long_run.duration_min is None
    assert long_run.size_label() == "~8.5 km"


def test_with_no_history_the_menu_suggests_no_distances() -> None:
    """Better to say nothing than to invent a distance for a new athlete."""
    goals = Goals(emphasis=Emphasis.THRESHOLD, runs_per_week=3, quality_sessions=1)

    menu = build_menu(goals, MONDAY, week_index=0)

    assert all(planned.distance_km is None for planned in menu.sessions)
    assert all(planned.size_label() == "" for planned in menu.sessions)


def _run(store: Store, day: dt.date, sport: str, distance_m: float) -> None:
    """Store one activity, so sizing can be tested against what it reads."""
    store.upsert_activities(
        [
            Activity(
                source=Source.INTERVALS,
                external_id=f"{day:%Y%m%d}-{sport}",
                start_time=dt.datetime.combine(day, dt.time(7, 0), tzinfo=dt.UTC),
                sport_type=sport,
                name=f"{sport} session",
                distance_m=distance_m,
                moving_time_s=1800,
                elapsed_time_s=1800,
            )
        ]
    )


def test_a_bike_ride_does_not_size_the_long_run(store: Store) -> None:
    """A 40 km ride alongside 5 km runs must not suggest a 42 km long run."""
    today = dt.date(2026, 6, 15)
    for offset in range(4):
        _run(store, today - dt.timedelta(days=offset), "Run", 5_000.0)
    _run(store, today - dt.timedelta(days=1), "Ride", 40_000.0)

    distances = recent_run_distances(store, today)

    assert distances == [5.0] * 4


def test_activities_without_a_distance_are_left_out(store: Store) -> None:
    """A gym session arrives with distance 0; counting it would halve the median."""
    today = dt.date(2026, 6, 15)
    for offset in range(4):
        _run(store, today - dt.timedelta(days=offset), "Run", 6_000.0)
    _run(store, today - dt.timedelta(days=2), "WeightTraining", 0.0)

    assert recent_run_distances(store, today) == [6.0] * 4


def test_runs_beyond_the_window_do_not_size_anything(store: Store) -> None:
    today = dt.date(2026, 6, 15)
    _run(store, today - dt.timedelta(weeks=SIZING_WINDOW_WEEKS + 1), "Run", 20_000.0)
    _run(store, today, "Run", 5_000.0)

    assert recent_run_distances(store, today) == [5.0]


def test_distances_land_on_the_half_kilometre() -> None:
    """A suggestion of 7.9 km implies a precision the number does not have."""
    sizing = size_from_history([5.1, 5.3, 5.9, 6.1, 7.6])

    assert sizing is not None
    assert sizing.easy_km % SIZING_STEP_KM == 0
    assert sizing.long_km % SIZING_STEP_KM == 0
