"""Anchor selection and the per-run fitness measures."""

import datetime as dt

import pandas as pd
import pytest

from longrun.analysis.anchor import ANCHOR_WINDOW_DAYS, find_anchor, personal_best
from longrun.analysis.fitness import decoupling, efficiency_factor, run_metrics, trend

TODAY = dt.date(2026, 6, 20)


def _tests_frame(rows: list[tuple[str, str, float, str]]) -> pd.DataFrame:
    """Build a long-format tests frame from `(date, metric, value, unit)`."""
    return pd.DataFrame(
        {
            "date": pd.to_datetime([row[0] for row in rows]),
            "metric": [row[1] for row in rows],
            "value": [row[2] for row in rows],
            "unit": [row[3] for row in rows],
        }
    )


def _streams(samples: int, hr: float, velocity: float, drift: float = 0.0) -> pd.DataFrame:
    """Build a stream frame, optionally drifting heart rate upward."""
    return pd.DataFrame(
        {
            "t_s": list(range(samples)),
            "hr": [hr + drift * (index / samples) for index in range(samples)],
            "velocity": [velocity] * samples,
        }
    )


def test_the_fastest_recent_parkrun_anchors() -> None:
    """One slow morning must not drop every training pace."""
    tests = _tests_frame(
        [
            ("2026-06-06", "parkrun_5k", 1367.0, "s"),
            ("2026-06-13", "parkrun_5k", 1450.0, "s"),
        ]
    )

    anchor = find_anchor(tests, TODAY)

    assert anchor is not None
    assert anchor.result.duration_s == 1367.0
    assert anchor.date == dt.date(2026, 6, 6)


def test_a_result_outside_the_window_is_ignored() -> None:
    """An old personal best describes a different athlete."""
    old = (TODAY - dt.timedelta(days=ANCHOR_WINDOW_DAYS + 10)).isoformat()
    tests = _tests_frame([(old, "parkrun_5k", 1200.0, "s")])

    assert find_anchor(tests, TODAY) is None


def test_a_recent_result_inside_the_window_is_used() -> None:
    recent = (TODAY - dt.timedelta(days=ANCHOR_WINDOW_DAYS - 5)).isoformat()
    tests = _tests_frame([(recent, "parkrun_5k", 1300.0, "s")])

    assert find_anchor(tests, TODAY) is not None


def test_a_non_duration_row_is_not_mistaken_for_a_race() -> None:
    """A VO2max value sitting in the same table is not a 5k time."""
    tests = _tests_frame([("2026-06-06", "parkrun_5k", 52.0, "ml/kg/min")])

    assert find_anchor(tests, TODAY) is None


def test_an_implausible_time_is_rejected() -> None:
    tests = _tests_frame([("2026-06-06", "parkrun_5k", 90.0, "s")])

    assert find_anchor(tests, TODAY) is None


def test_no_tests_means_no_anchor() -> None:
    assert find_anchor(pd.DataFrame(), TODAY) is None


def test_the_anchor_reports_its_vdot_and_provenance() -> None:
    tests = _tests_frame([("2026-06-06", "parkrun_5k", 1200.0, "s")])

    anchor = find_anchor(tests, TODAY)

    assert anchor is not None
    assert anchor.vdot == pytest.approx(49.8, abs=0.5)
    assert "parkrun" in anchor.describe()


def test_personal_best_picks_the_fastest() -> None:
    results = [(dt.date(2026, 1, 1), 1400.0), (dt.date(2026, 2, 1), 1350.0)]

    assert personal_best(results) == (dt.date(2026, 2, 1), 1350.0)


def test_personal_best_of_nothing_is_none() -> None:
    assert personal_best([]) is None


def test_efficiency_factor_is_speed_per_heartbeat() -> None:
    assert efficiency_factor(3.0, 150.0) == pytest.approx(3.0 * 60 / 150.0)


def test_efficiency_factor_needs_a_usable_heart_rate() -> None:
    assert efficiency_factor(3.0, 40.0) is None


def test_a_steady_run_barely_decouples() -> None:
    steady = _streams(1200, hr=150.0, velocity=3.0)

    assert decoupling(steady) == pytest.approx(0.0, abs=0.5)


def test_heart_rate_drifting_upward_shows_as_decoupling() -> None:
    """The same pace costing more beats later on is the signal."""
    drifting = _streams(1200, hr=150.0, velocity=3.0, drift=15.0)

    result = decoupling(drifting)

    assert result is not None
    assert result > 2.0


def test_a_short_run_cannot_be_split() -> None:
    assert decoupling(_streams(60, hr=150.0, velocity=3.0)) is None


def test_run_metrics_reports_what_it_can() -> None:
    metrics = run_metrics(_streams(1200, hr=150.0, velocity=3.0))

    assert metrics.samples == 1200
    assert metrics.avg_hr == pytest.approx(150.0)
    assert metrics.efficiency_factor is not None
    assert metrics.decoupling_pct is not None


def test_run_metrics_on_empty_streams_is_all_none() -> None:
    metrics = run_metrics(pd.DataFrame())

    assert metrics.samples == 0
    assert metrics.efficiency_factor is None


def test_dropouts_are_excluded_from_the_averages() -> None:
    """A zero heart rate is a lost signal, not a measurement."""
    streams = pd.DataFrame(
        {"t_s": [0, 1, 2, 3], "hr": [0.0, 150.0, 152.0, None], "velocity": [3.0, 3.0, 3.0, 3.0]}
    )

    metrics = run_metrics(streams)

    assert metrics.samples == 2
    assert metrics.avg_hr == pytest.approx(151.0)


def test_trend_compares_halves_rather_than_fitting_a_line() -> None:
    assert trend([1.0, 1.0, 2.0, 2.0]) == pytest.approx(1.0)
    assert trend([2.0, 2.0, 1.0, 1.0]) == pytest.approx(-1.0)


def test_trend_needs_enough_points() -> None:
    assert trend([1.0, 2.0]) is None
