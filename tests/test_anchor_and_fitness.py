"""Anchor selection and the per-run fitness measures."""

import datetime as dt

import pandas as pd
import pytest

from longrun.analysis.anchor import (
    ANCHOR_WINDOW_DAYS,
    Confidence,
    DetectedEffort,
    find_anchor,
    hard_effort_threshold,
    personal_best,
)
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


def _effort(
    duration_s: float,
    *,
    avg_hr: float | None = 178.0,
    threshold: float | None = 160.0,
    weekday: int = 5,
    day: dt.date = dt.date(2026, 6, 6),
) -> DetectedEffort:
    """A detected 5k, hard by default."""
    return DetectedEffort(
        date=day,
        duration_s=duration_s,
        avg_hr=avg_hr,
        hard_threshold=threshold,
        weekday=weekday,
        activity_name="Morning Run",
    )


def test_a_logged_parkrun_beats_a_faster_detected_effort() -> None:
    """The course is fixed and the result confirmed; a GPS trace is neither.

    A faster solo effort is worth knowing about, but it belongs in a re-test
    suggestion rather than in a silent change of anchor.
    """
    tests = _tests_frame([("2026-06-06", "parkrun_5k", 1367.0, "s")])

    anchor = find_anchor(tests, TODAY, detected=[_effort(1200.0)])

    assert anchor is not None
    assert anchor.result.duration_s == 1367.0
    assert anchor.confidence is Confidence.LOGGED


def test_detection_is_used_when_nothing_is_logged() -> None:
    anchor = find_anchor(pd.DataFrame(), TODAY, detected=[_effort(1271.0)])

    assert anchor is not None
    assert anchor.result.duration_s == 1271.0
    assert anchor.confidence is Confidence.DETECTED
    assert "detected" in anchor.describe()


def test_an_easy_effort_never_becomes_the_anchor() -> None:
    """A jog setting race paces is the failure this gate exists to prevent."""
    easy = _effort(1271.0, avg_hr=140.0, threshold=160.0)

    assert easy.is_hard is False
    assert find_anchor(pd.DataFrame(), TODAY, detected=[easy]) is None


def test_an_effort_without_heart_rate_is_refused() -> None:
    """Fail closed: a dead strap means the effort is skipped, not assumed."""
    assert _effort(1271.0, avg_hr=None).is_hard is False


def test_detection_is_off_without_a_calibrated_threshold() -> None:
    """Too little history to know what hard means for this athlete."""
    assert _effort(1271.0, threshold=None).is_hard is False


def test_the_fastest_hard_effort_wins() -> None:
    anchor = find_anchor(
        pd.DataFrame(), TODAY, detected=[_effort(1400.0), _effort(1271.0), _effort(1500.0)]
    )

    assert anchor is not None
    assert anchor.result.duration_s == 1271.0


def test_a_saturday_effort_is_flagged_as_possibly_a_parkrun() -> None:
    """A hint for the listing only; it never includes or excludes anything."""
    saturday = _effort(1271.0, weekday=5)
    wednesday = _effort(1271.0, weekday=2)

    assert saturday.looks_like_a_parkrun is True
    assert "parkrun" in saturday.describe()
    assert wednesday.looks_like_a_parkrun is False
    # Both are equally usable as anchors.
    assert wednesday.is_hard is True


def test_the_threshold_calibrates_to_the_athletes_own_peak() -> None:
    runs = pd.DataFrame({"max_hr": [180.0] * 12})

    threshold = hard_effort_threshold(runs)

    assert threshold == pytest.approx(180.0 * 0.92)


def test_the_threshold_ignores_a_cadence_lock_spike() -> None:
    """An optical sensor reading 215 never happened and must not set the bar."""
    runs = pd.DataFrame({"max_hr": [180.0] * 12 + [215.0]})

    threshold = hard_effort_threshold(runs)

    assert threshold is not None
    assert threshold < 180.0


def test_no_threshold_without_enough_runs() -> None:
    assert hard_effort_threshold(pd.DataFrame({"max_hr": [180.0] * 3})) is None
    assert hard_effort_threshold(pd.DataFrame()) is None


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
