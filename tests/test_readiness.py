"""The readiness rules, on fabricated readings chosen to isolate each one."""

import datetime as dt
import math

import pytest

from longrun.analysis.readiness import (
    BASELINE_DAYS,
    DailyReading,
    Readiness,
    assess,
    baseline_for,
)

TODAY = dt.date(2026, 6, 20)


def _steady(days: int = BASELINE_DAYS, rmssd: float = 60.0, **kwargs: float) -> list[DailyReading]:
    """A week of unremarkable mornings ending the day before TODAY."""
    return [
        DailyReading(
            date=TODAY - dt.timedelta(days=offset),
            hrv_rmssd=rmssd + (offset % 3) - 1,
            resting_hr=48.0,
            sleep_hours=8.0,
            **kwargs,
        )
        for offset in range(1, days + 1)
    ]


def test_no_baseline_without_enough_readings() -> None:
    readings = _steady(days=2)

    assert baseline_for(readings, TODAY) is None


def test_the_baseline_is_computed_on_the_logarithm() -> None:
    """Taking the log first is what makes the range meaningful on skewed data."""
    readings = [
        DailyReading(date=TODAY - dt.timedelta(days=offset), hrv_rmssd=value)
        for offset, value in enumerate([40.0, 50.0, 60.0, 70.0], start=1)
    ]

    baseline = baseline_for(readings, TODAY)

    assert baseline is not None
    expected = sum(math.log(v) for v in (40.0, 50.0, 60.0, 70.0)) / 4
    assert baseline.mean_ln == pytest.approx(expected)
    # The geometric mean sits below the arithmetic one on a skewed series.
    assert baseline.mean_rmssd < 55.0


def test_todays_reading_is_excluded_from_its_own_baseline() -> None:
    readings = [*_steady(), DailyReading(date=TODAY, hrv_rmssd=5.0)]

    baseline = baseline_for(readings, TODAY)

    assert baseline is not None
    assert baseline.mean_rmssd > 50.0


def test_a_normal_morning_is_green() -> None:
    readings = [
        *_steady(),
        DailyReading(date=TODAY, hrv_rmssd=60.0, resting_hr=48.0, sleep_hours=8.0),
    ]

    verdict = assess(readings, TODAY)

    assert verdict.readiness is Readiness.GREEN
    assert verdict.reasons == []


def test_one_low_morning_alone_is_not_suppression() -> None:
    """A single reading below the range is noise, and is reported as such."""
    readings = [
        *_steady(),
        DailyReading(date=TODAY, hrv_rmssd=20.0, resting_hr=48.0, sleep_hours=8.0),
    ]

    verdict = assess(readings, TODAY)

    assert verdict.readiness is Readiness.GREEN
    assert any("below the normal range today" in reason for reason in verdict.reasons)


def test_two_low_mornings_running_count_as_suppression() -> None:
    readings = [
        *_steady(),
        DailyReading(date=TODAY - dt.timedelta(days=1), hrv_rmssd=20.0),
        DailyReading(date=TODAY, hrv_rmssd=20.0, resting_hr=48.0, sleep_hours=8.0),
    ]

    verdict = assess(readings, TODAY)

    assert verdict.readiness is Readiness.AMBER
    assert any("days running" in reason for reason in verdict.reasons)


def test_an_elevated_resting_heart_rate_fires() -> None:
    readings = [
        *_steady(),
        DailyReading(date=TODAY, hrv_rmssd=60.0, resting_hr=54.0, sleep_hours=8.0),
    ]

    verdict = assess(readings, TODAY)

    assert verdict.readiness is Readiness.AMBER
    assert any("resting heart rate" in reason for reason in verdict.reasons)


def test_poor_sleep_fires() -> None:
    readings = [
        *_steady(),
        DailyReading(date=TODAY, hrv_rmssd=60.0, resting_hr=48.0, sleep_hours=4.5),
    ]

    verdict = assess(readings, TODAY)

    assert verdict.readiness is Readiness.AMBER
    assert any("hours of sleep" in reason for reason in verdict.reasons)


def test_several_factors_together_are_red() -> None:
    """The verdict escalates when signals agree, not when one is loud."""
    readings = [
        *_steady(),
        DailyReading(date=TODAY - dt.timedelta(days=1), hrv_rmssd=20.0),
        DailyReading(
            date=TODAY,
            hrv_rmssd=20.0,
            resting_hr=55.0,
            sleep_hours=4.0,
        ),
    ]

    verdict = assess(readings, TODAY)

    assert verdict.readiness is Readiness.RED
    assert len(verdict.reasons) >= 3


def test_the_verdict_rests_on_the_measured_signals_alone() -> None:
    """Nothing asks the athlete to rate anything.

    A morning with no subjective rating at all must still reach a full
    verdict from HRV, resting heart rate and sleep.
    """
    readings = [
        *_steady(),
        DailyReading(date=TODAY - dt.timedelta(days=1), hrv_rmssd=20.0),
        DailyReading(date=TODAY, hrv_rmssd=20.0, resting_hr=55.0, sleep_hours=4.0),
    ]

    verdict = assess(readings, TODAY)

    assert verdict.readiness is Readiness.RED
    assert all("rated" not in reason for reason in verdict.reasons)


def test_a_session_that_felt_poor_fires() -> None:
    readings = [*_steady(), DailyReading(date=TODAY, hrv_rmssd=60.0, resting_hr=48.0, feel=1.0)]

    verdict = assess(readings, TODAY)

    assert verdict.readiness is Readiness.AMBER
    assert any("felt poor" in reason for reason in verdict.reasons)


def test_nothing_logged_today_is_unknown() -> None:
    verdict = assess(_steady(), TODAY)

    assert verdict.readiness is Readiness.UNKNOWN


def test_no_history_at_all_is_unknown() -> None:
    verdict = assess([DailyReading(date=TODAY, hrv_rmssd=60.0)], TODAY)

    assert verdict.readiness is Readiness.UNKNOWN
    assert "baseline" in verdict.describe()
