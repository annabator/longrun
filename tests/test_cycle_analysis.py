"""Deriving cycles and phases from logged days.

Every date here is fabricated for the test. The pattern is deliberately
tidier than any real log, so that a failure points at the arithmetic rather
than at ambiguity in the input.
"""

import datetime as dt

from longrun.analysis.cycle import (
    Confidence,
    Phase,
    cycle_day_for,
    cycles_from_days,
    median_cycle_length,
    ovulation_estimate,
    phase_for,
)


def _period(start: dt.date, days: int = 5) -> list[dt.date]:
    """Return `days` consecutive logged days from `start`."""
    return [start + dt.timedelta(days=offset) for offset in range(days)]


# Three cycles of 28, 29 and 30 days, plus a fourth still running.
FAKE_LOG = (
    _period(dt.date(2026, 1, 5))
    + _period(dt.date(2026, 2, 2))
    + _period(dt.date(2026, 3, 3))
    + _period(dt.date(2026, 4, 2))
)


def test_consecutive_days_group_into_one_cycle() -> None:
    cycles = cycles_from_days(_period(dt.date(2026, 1, 5)))

    assert len(cycles) == 1
    assert cycles[0].start == dt.date(2026, 1, 5)
    assert cycles[0].period_days == 5


def test_a_gap_starts_a_new_cycle() -> None:
    cycles = cycles_from_days(FAKE_LOG)

    assert [cycle.start for cycle in cycles] == [
        dt.date(2026, 1, 5),
        dt.date(2026, 2, 2),
        dt.date(2026, 3, 3),
        dt.date(2026, 4, 2),
    ]


def test_a_missed_log_inside_a_period_does_not_split_it() -> None:
    """Someone forgetting to log day three has not started a new cycle."""
    days = [dt.date(2026, 1, 5), dt.date(2026, 1, 6), dt.date(2026, 1, 9)]

    cycles = cycles_from_days(days)

    assert len(cycles) == 1
    assert cycles[0].period_days == 5


def test_the_last_cycle_is_in_progress() -> None:
    cycles = cycles_from_days(FAKE_LOG)

    assert cycles[-1].is_complete is False
    assert cycles[-1].length_days is None
    assert cycles[0].is_complete is True


def test_cycle_lengths_are_start_to_start() -> None:
    cycles = cycles_from_days(FAKE_LOG)

    assert cycles[0].length_days == 28
    assert cycles[1].length_days == 29
    assert cycles[2].length_days == 30


def test_median_length_ignores_the_cycle_in_progress() -> None:
    assert median_cycle_length(cycles_from_days(FAKE_LOG)) == 29


def test_median_length_is_none_before_any_cycle_completes() -> None:
    assert median_cycle_length(cycles_from_days(_period(dt.date(2026, 1, 5)))) is None


def test_ovulation_is_counted_back_from_the_next_period() -> None:
    """The luteal phase is the consistent half, so it anchors the estimate."""
    cycles = cycles_from_days(FAKE_LOG)

    # Second period starts 2 February, so ovulation lands fourteen days before.
    assert ovulation_estimate(cycles[0], 29) == dt.date(2026, 1, 19)


def test_ovulation_for_the_current_cycle_is_projected_forward() -> None:
    """With no next period to count back from, the median stands in."""
    cycles = cycles_from_days(FAKE_LOG)

    # 2 April plus (29 - 14) days.
    assert ovulation_estimate(cycles[-1], 29) == dt.date(2026, 4, 17)


def test_cycle_day_counts_the_first_period_day_as_one() -> None:
    cycles = cycles_from_days(FAKE_LOG)

    assert cycle_day_for(dt.date(2026, 1, 5), cycles) == 1
    assert cycle_day_for(dt.date(2026, 1, 20), cycles) == 16


def test_a_date_before_any_log_has_no_cycle_day() -> None:
    cycles = cycles_from_days(FAKE_LOG)

    assert cycle_day_for(dt.date(2025, 12, 1), cycles) is None


def test_logged_days_are_menstrual() -> None:
    cycles = cycles_from_days(FAKE_LOG)

    assert phase_for(dt.date(2026, 1, 5), cycles).phase is Phase.MENSTRUAL
    assert phase_for(dt.date(2026, 1, 9), cycles).phase is Phase.MENSTRUAL


def test_the_days_after_a_period_are_follicular() -> None:
    cycles = cycles_from_days(FAKE_LOG)

    assert phase_for(dt.date(2026, 1, 12), cycles).phase is Phase.FOLLICULAR


def test_the_ovulation_window_spans_the_estimate() -> None:
    cycles = cycles_from_days(FAKE_LOG)

    for day in (dt.date(2026, 1, 18), dt.date(2026, 1, 19), dt.date(2026, 1, 20)):
        assert phase_for(day, cycles).phase is Phase.OVULATION


def test_the_days_before_the_next_period_are_luteal() -> None:
    cycles = cycles_from_days(FAKE_LOG)

    assert phase_for(dt.date(2026, 1, 28), cycles).phase is Phase.LUTEAL


def test_a_completed_typical_cycle_is_estimated() -> None:
    cycles = cycles_from_days(FAKE_LOG)

    estimate = phase_for(dt.date(2026, 1, 20), cycles)

    assert estimate.confidence is Confidence.ESTIMATED
    assert "counted back" in estimate.reason


def test_the_cycle_in_progress_is_only_provisional() -> None:
    """It has no next period to anchor on, and says so."""
    cycles = cycles_from_days(FAKE_LOG)

    estimate = phase_for(dt.date(2026, 4, 20), cycles)

    assert estimate.confidence is Confidence.PROVISIONAL
    assert "in progress" in estimate.reason


def test_confidence_is_low_without_enough_history() -> None:
    """Two cycles is not enough to have a median worth projecting from."""
    days = _period(dt.date(2026, 1, 5)) + _period(dt.date(2026, 2, 2))

    estimate = phase_for(dt.date(2026, 2, 20), cycles_from_days(days))

    assert estimate.confidence is Confidence.LOW


def test_an_irregular_cycle_lowers_confidence() -> None:
    """A 50-day gap makes the ovulation estimate close to meaningless."""
    days = _period(dt.date(2026, 1, 5)) + _period(dt.date(2026, 2, 24))

    estimate = phase_for(dt.date(2026, 1, 25), cycles_from_days(days))

    assert estimate.confidence is Confidence.LOW
    assert "outside the usual range" in estimate.reason


def test_a_date_with_no_cycle_is_unknown() -> None:
    estimate = phase_for(dt.date(2025, 12, 1), cycles_from_days(FAKE_LOG))

    assert estimate.phase is Phase.UNKNOWN
    assert estimate.confidence is Confidence.NONE
    assert estimate.cycle_day is None


def test_nothing_logged_yields_no_cycles() -> None:
    assert cycles_from_days([]) == []
    assert phase_for(dt.date(2026, 1, 1), []).phase is Phase.UNKNOWN


def test_duplicate_and_unsorted_input_is_tolerated() -> None:
    """The store keeps days unique, but the derivation should not rely on it."""
    days = [dt.date(2026, 1, 6), dt.date(2026, 1, 5), dt.date(2026, 1, 5)]

    cycles = cycles_from_days(days)

    assert len(cycles) == 1
    assert cycles[0].period_days == 2
