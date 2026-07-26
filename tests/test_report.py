"""The dashboard builds from the synthetic dataset, and its derived series hold."""

import datetime as dt
import re
from pathlib import Path

import pandas as pd
import pytest

from longrun.analysis.cycle import cycles_from_days
from longrun.models import Activity
from longrun.report import build_report, load_report_data
from longrun.report.figures import build_cycle_figure, build_wellness_figure
from longrun.report.frames import (
    by_cycle_day,
    format_duration,
    mark_personal_bests,
    metric_series,
    rolling_baseline,
    weekly_volume,
    wellness_series,
)
from longrun.store import Store

# An obviously fabricated log, spanning part of the synthetic sample period so
# that some readings land inside a cycle and plenty land outside one.
FAKE_CYCLE_LOG = [
    dt.date(2026, 1, 5) + dt.timedelta(days=cycle * 28 + offset)
    for cycle in range(6)
    for offset in range(5)
]


def test_report_builds_from_synthetic_data(seeded_store: Store, tmp_path: Path) -> None:
    """The end-to-end path has to survive real-shaped, gappy data."""
    data = load_report_data(seeded_store)
    output = build_report(data, tmp_path / "report.html", title="test report")

    document = output.read_text(encoding="utf-8")

    assert output.exists()
    assert "<title>test report</title>" in document
    assert "Recent activities" in document
    assert "Plotly.newPlot" in document

    # Self-contained: opening a page of personal health data must pull nothing
    # from the network. Assert against tags that would actually load something.
    # The inlined bundle contains CDN URLs in its own map-tile and config
    # defaults, so search the markup with script bodies removed, and separately
    # rule out any external script.
    markup = re.sub(r"<script\b[^>]*>.*?</script>", "", document, flags=re.DOTALL)
    assert 'src="http' not in markup
    assert "<link" not in markup
    assert re.search(r"<script[^>]+\bsrc=", document) is None


def test_report_states_that_no_correlation_is_computed(seeded_store: Store, tmp_path: Path) -> None:
    """The sparse-data caveat is part of the product, not an optional nicety."""
    data = load_report_data(seeded_store)
    document = build_report(data, tmp_path / "report.html").read_text(encoding="utf-8")

    assert "no correlation" in document


def test_report_escapes_activity_names(store: Store, tmp_path: Path, activity: Activity) -> None:
    """Activity names are user data and could contain markup."""
    store.upsert_activities([activity.model_copy(update={"name": "<script>x</script>"})])
    data = load_report_data(store)

    document = build_report(data, tmp_path / "report.html").read_text(encoding="utf-8")

    assert "&lt;script&gt;x&lt;/script&gt;" in document


def test_personal_bests_require_a_strict_improvement() -> None:
    """Equalling a best is not a new best."""
    series = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-01-03", "2026-01-10", "2026-01-17", "2026-01-24"]),
            "value": [1500.0, 1450.0, 1450.0, 1400.0],
            "unit": ["s", "s", "s", "s"],
        }
    )

    marked = mark_personal_bests(series)

    assert marked["is_pb"].tolist() == [True, True, False, True]


def test_personal_bests_for_a_metric_where_higher_is_better() -> None:
    """VO2max improves upward, and the unit is what decides the direction."""
    series = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-01-01", "2026-02-01", "2026-03-01"]),
            "value": [48.0, 47.0, 51.0],
            "unit": ["ml/kg/min"] * 3,
        }
    )

    assert mark_personal_bests(series)["is_pb"].tolist() == [True, False, True]


def test_parkrun_series_is_present_in_the_sample_data(seeded_store: Store) -> None:
    data = load_report_data(seeded_store)

    parkrun = metric_series(data.tests, "parkrun_5k")

    assert not parkrun.empty
    assert (parkrun["unit"] == "s").all()
    assert mark_personal_bests(parkrun)["is_pb"].iloc[0]


def test_weekly_volume_sums_distance_per_week(seeded_store: Store) -> None:
    data = load_report_data(seeded_store)

    weekly = weekly_volume(data.activities)

    assert not weekly.empty
    assert weekly["distance_km"].sum() > 0
    # Weekly buckets must account for every metre, allowing for the float
    # accumulation order differing between the two sums.
    assert weekly["distance_km"].sum() == pytest.approx(
        float(data.activities["distance_m"].sum()) / 1000.0
    )


def test_empty_store_produces_an_empty_report_dataset(store: Store) -> None:
    assert load_report_data(store).is_empty


def test_report_includes_the_wellness_section(seeded_store: Store, tmp_path: Path) -> None:
    data = load_report_data(seeded_store)

    document = build_report(data, tmp_path / "report.html").read_text(encoding="utf-8")

    assert "Wellness" in document
    assert "HRV rMSSD" in document


def test_report_no_longer_shows_rolling_training_load(seeded_store: Store, tmp_path: Path) -> None:
    """CTL, ATL and form were dropped in favour of the measured series."""
    data = load_report_data(seeded_store)

    document = build_report(data, tmp_path / "report.html").read_text(encoding="utf-8")

    assert "Fitness and form" not in document
    assert "Fatigue (ATL)" not in document


def test_hrv_baseline_is_a_trailing_mean(seeded_store: Store) -> None:
    data = load_report_data(seeded_store)

    hrv = rolling_baseline(wellness_series(data.wellness, "hrv_rmssd"), "7D")

    assert not hrv.empty
    assert "rolling" in hrv
    # The first two days cannot have a three-sample baseline yet.
    assert hrv["rolling"].isna().iloc[0]
    assert hrv["rolling"].notna().iloc[-1]


def test_figures_build_on_an_empty_store(store: Store) -> None:
    """Every panel must render an explanation rather than crash when bare."""
    data = load_report_data(store)

    assert build_wellness_figure(data) is not None


def test_the_cycle_panel_is_absent_unless_asked_for(seeded_store: Store, tmp_path: Path) -> None:
    """A report built the ordinary way carries no trace of this data."""
    data = load_report_data(seeded_store)

    document = build_report(data, tmp_path / "report.html").read_text(encoding="utf-8")

    assert "By cycle day" not in document
    assert "cycle" not in document.lower().split("<script")[0]


def test_the_cycle_panel_appears_when_cycles_are_supplied(
    seeded_store: Store, tmp_path: Path
) -> None:
    cycles = cycles_from_days(FAKE_CYCLE_LOG)
    data = load_report_data(seeded_store, cycles)

    document = build_report(data, tmp_path / "report.html").read_text(encoding="utf-8")

    assert "By cycle day" in document
    assert "--with-cycle" in document


def test_readings_are_placed_on_their_cycle_day(seeded_store: Store) -> None:
    cycles = cycles_from_days(FAKE_CYCLE_LOG)
    data = load_report_data(seeded_store, cycles)

    placed = by_cycle_day(data.wellness, "date", "hrv_rmssd", cycles)

    assert not placed.empty
    assert placed["cycle_day"].min() >= 1
    # The sample covers more than one cycle, so a given day recurs.
    assert placed["cycle_day"].value_counts().max() > 1


def test_readings_outside_any_cycle_are_dropped_not_guessed(seeded_store: Store) -> None:
    """A reading with no logged cycle around it has no cycle day."""
    cycles = cycles_from_days(FAKE_CYCLE_LOG)
    data = load_report_data(seeded_store, cycles)

    placed = by_cycle_day(data.wellness, "date", "hrv_rmssd", cycles)
    covered = data.wellness["hrv_rmssd"].notna().sum()

    assert 0 < len(placed) < covered


def test_placing_by_cycle_day_without_cycles_is_empty(seeded_store: Store) -> None:
    data = load_report_data(seeded_store)

    assert by_cycle_day(data.wellness, "date", "hrv_rmssd", []).empty


def test_the_cycle_figure_builds_with_nothing_logged(store: Store) -> None:
    data = load_report_data(store, cycles_from_days([]))

    assert build_cycle_figure(data) is not None


def test_the_cycle_panel_actually_draws_its_phase_bands(seeded_store: Store) -> None:
    """The caption promises a shaded period and an ovulation line; check they exist.

    Plotly silently discards a shape aimed at a subplot that has no traces
    yet, so this went unnoticed until the page was looked at. Asserting on
    the shapes keeps a caption from describing something invisible.
    """
    data = load_report_data(seeded_store, cycles_from_days(FAKE_CYCLE_LOG))

    figure = build_cycle_figure(data)

    kinds = [shape.type for shape in figure.layout.shapes]
    assert kinds.count("rect") >= 1
    assert kinds.count("line") >= 1


def test_duration_formatting() -> None:
    assert format_duration(1367) == "22:47"
    assert format_duration(5344) == "1:29:04"
    assert format_duration(59.6) == "1:00"
