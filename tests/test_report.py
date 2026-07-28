"""The dashboard builds from the synthetic dataset, and its derived series hold."""

import datetime as dt
import re
from pathlib import Path

import pandas as pd
import pytest

from longrun.analysis.cycle import cycles_from_days
from longrun.models import Activity, Metric
from longrun.report import build_report, load_report_data
from longrun.report.figures import (
    build_cycle_figure,
    build_load_context_figure,
    build_trend_figure,
    build_wellness_figure,
)
from longrun.report.frames import (
    DEFAULT_WINDOW_MONTHS,
    by_cycle_day,
    format_duration,
    load_context_by_week,
    mark_personal_bests,
    metric_series,
    other_activities,
    report_window,
    rolling_baseline,
    running_activities,
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
    """The key promises a shaded band per phase; check they exist.

    Plotly silently discards a shape aimed at a subplot that has no traces
    yet, so this went unnoticed until the page was looked at. Asserting on
    the shapes keeps a key from describing something invisible.
    """
    data = load_report_data(seeded_store, cycles_from_days(FAKE_CYCLE_LOG))

    figure = build_cycle_figure(data)

    bands = [shape for shape in figure.layout.shapes if shape.type == "rect"]
    assert bands
    # Named, so the page's theme script can recolour them by phase rather
    # than by the order they happened to be added in.
    assert all(str(shape.name).startswith("phase:") for shape in bands)


def test_every_derivable_phase_gets_its_own_colour(seeded_store: Store) -> None:
    data = load_report_data(seeded_store, cycles_from_days(FAKE_CYCLE_LOG))

    figure = build_cycle_figure(data)

    coloured = {
        (str(shape.name), str(shape.fillcolor))
        for shape in figure.layout.shapes
        if shape.type == "rect"
    }
    # One colour per phase name, and no two phases sharing one.
    by_name = dict(coloured)
    assert len(by_name) == len(coloured)
    assert len(set(by_name.values())) == len(by_name)


def _mixed_activities() -> pd.DataFrame:
    """A week holding a run, a long ride and a distance-less gym session."""
    return pd.DataFrame(
        {
            "start_time": pd.to_datetime(["2026-06-15", "2026-06-16", "2026-06-17"], utc=True),
            "sport_type": ["Run", "Ride", "WeightTraining"],
            "name": ["Morning Run", "Commute", "Gym"],
            "distance_m": [10000.0, 40000.0, 0.0],
            "moving_time_s": [3000, 4800, 2700],
            "avg_hr": [148.0, 132.0, None],
            "icu_load": [80.0, 120.0, 22.0],
        }
    )


def test_a_ride_is_not_counted_as_running_mileage() -> None:
    """A 40km ride in the weekly running volume was the bug this fixes."""
    weekly = weekly_volume(_mixed_activities())

    assert weekly["distance_km"].sum() == pytest.approx(10.0)


def test_running_and_other_activities_split_on_sport() -> None:
    activities = _mixed_activities()

    assert len(running_activities(activities)) == 1
    assert len(other_activities(activities)) == 2


def test_load_context_counts_sessions_and_load_not_distance() -> None:
    weekly = load_context_by_week(_mixed_activities())

    assert len(weekly) == 1
    assert weekly["sessions"].iloc[0] == 2
    assert weekly["load"].iloc[0] == pytest.approx(142.0)
    assert "distance_km" not in weekly


def test_load_context_is_empty_without_other_sports(seeded_store: Store) -> None:
    data = load_report_data(seeded_store)

    assert load_context_by_week(data.activities).empty


def test_the_cross_training_panel_appears_only_with_other_sports(
    store: Store, tmp_path: Path, activity: Activity
) -> None:
    store.upsert_activities([activity])
    without = build_report(load_report_data(store), tmp_path / "a.html").read_text(encoding="utf-8")

    store.upsert_activities(
        [activity.model_copy(update={"external_id": "ride", "sport_type": "Ride"})]
    )
    with_other = build_report(load_report_data(store), tmp_path / "b.html").read_text(
        encoding="utf-8"
    )

    assert "Cross-training" not in without
    assert "Cross-training" in with_other


def test_pace_is_blank_for_a_non_running_activity(
    store: Store, tmp_path: Path, activity: Activity
) -> None:
    """A ride's pace reads as an absurd running pace, so it is not shown."""
    store.upsert_activities(
        [activity.model_copy(update={"external_id": "ride", "sport_type": "Ride"})]
    )

    document = build_report(load_report_data(store), tmp_path / "report.html").read_text(
        encoding="utf-8"
    )

    table = document.split("<tbody>")[1].split("</tbody>")[0]
    assert "Ride" in table
    # Distance is real and kept; pace is not.
    assert '<td class="num">10.00</td>' in table
    assert '<td class="num">-</td>' in table


def test_the_load_context_figure_builds_without_other_sports(seeded_store: Store) -> None:
    data = load_report_data(seeded_store)

    assert build_load_context_figure(data) is not None


def test_every_dated_panel_shares_one_range(seeded_store: Store) -> None:
    """Sparse and dense series must sit on the same timeline.

    A week of readings auto-scaling to its own week, beside two years of
    parkruns, is exactly what makes a shared-timeline view useless.
    """
    since = dt.date(2025, 12, 1)
    data = load_report_data(seeded_store)
    window = report_window(data, since)

    figure = build_trend_figure(data, window)

    layout = figure.to_plotly_json()["layout"]
    ranges = {
        tuple(axis["range"])
        for name, axis in layout.items()
        if name.startswith("xaxis") and axis.get("range")
    }
    assert len(ranges) == 1
    assert ranges.pop()[0].startswith("2025-12-01")


def test_the_window_starts_where_asked(seeded_store: Store) -> None:
    data = load_report_data(seeded_store)

    window = report_window(data, dt.date(2025, 12, 1))

    assert window is not None
    start, end = window
    assert start == dt.date(2025, 12, 1)
    assert end > start


def test_asking_for_the_whole_history_leaves_the_axes_auto_ranging(
    seeded_store: Store,
) -> None:
    data = load_report_data(seeded_store)

    assert report_window(data, None, whole_history=True) is None
    layout = build_trend_figure(data, None).to_plotly_json()["layout"]
    assert all(not axis.get("range") for name, axis in layout.items() if name.startswith("xaxis"))


def test_the_default_window_shows_recent_whole_months(seeded_store: Store) -> None:
    """Years of history squeeze the interesting part into a strip at the edge."""
    window = report_window(load_report_data(seeded_store), None)

    assert window is not None
    start, end = window
    assert start.day == 1
    assert start < end
    # Six whole months back, so the axis labels stay tidy and the start date
    # does not slide a day at a time as readings arrive.
    assert (end.year - start.year) * 12 + end.month - start.month == DEFAULT_WINDOW_MONTHS


def test_narrowing_the_view_does_not_move_the_personal_best(seeded_store: Store) -> None:
    """The range is set on the axis, not by dropping rows.

    Filtering first would make the fastest run inside the window look like a
    personal best when the real one sits just outside it.
    """
    data = load_report_data(seeded_store)
    marked = mark_personal_bests(metric_series(data.tests, "parkrun_5k"))
    best_before = marked.loc[marked["is_pb"], "value"].min()

    build_trend_figure(data, report_window(data, dt.date(2026, 6, 1)))

    again = mark_personal_bests(metric_series(data.tests, "parkrun_5k"))
    assert again.loc[again["is_pb"], "value"].min() == best_before


def test_duration_formatting() -> None:
    assert format_duration(1367) == "22:47"
    assert format_duration(5344) == "1:29:04"
    assert format_duration(59.6) == "1:00"


def test_a_narrowed_view_scales_its_y_axes_to_what_it_shows(seeded_store: Store) -> None:
    """An off-screen personal best must not squash this year into half a panel."""
    data = load_report_data(seeded_store)
    recent = (dt.date(2026, 6, 1), dt.date(2026, 6, 30))

    figure = build_trend_figure(data, recent)

    low, high = figure.layout.yaxis3.range
    shown = metric_series(data.tests, Metric.PARKRUN_5K)
    inside = shown.loc[
        (shown["date"] >= pd.Timestamp(recent[0])) & (shown["date"] <= pd.Timestamp(recent[1]))
    ]
    assert low <= inside["value"].min()
    assert high >= inside["value"].max()
    # The all-time best predates the window, so the axis must not reach it.
    assert low > shown["value"].min()


def test_the_whole_history_still_scales_to_everything(seeded_store: Store) -> None:
    data = load_report_data(seeded_store)

    figure = build_trend_figure(data, None)

    assert figure.layout.yaxis3.range is None


def test_the_wellness_panel_starts_where_its_readings_do(seeded_store: Store) -> None:
    """HRV and resting HR arrive later than the rest; blank axis says nothing."""
    data = load_report_data(seeded_store)
    generous = (dt.date(2020, 1, 1), dt.date(2026, 6, 30))

    figure = build_wellness_figure(data, generous)

    start = dt.date.fromisoformat(str(figure.layout.xaxis.range[0]))
    first_reading = data.wellness.loc[data.wellness["hrv_rmssd"].notna(), "date"].min().date()
    assert start == first_reading


def test_the_wellness_panel_never_widens_a_window_it_was_given(seeded_store: Store) -> None:
    data = load_report_data(seeded_store)
    narrow = (dt.date(2026, 6, 1), dt.date(2026, 6, 30))

    figure = build_wellness_figure(data, narrow)

    assert dt.date.fromisoformat(str(figure.layout.xaxis.range[0])) == narrow[0]


def test_each_cycle_panel_says_how_many_cycles_it_rests_on(seeded_store: Store) -> None:
    """A sparse panel must not read as missing cycles."""
    cycles = cycles_from_days(FAKE_CYCLE_LOG)
    data = load_report_data(seeded_store, cycles)

    figure = build_cycle_figure(data)

    titles = [str(annotation.text) for annotation in figure.layout.annotations]
    counted = [title for title in titles if "cycle" in title]
    assert len(counted) == 4
    for title in counted:
        shown = int(re.search(r"(\d+) cycles?", title).group(1))  # type: ignore[union-attr]
        assert 1 <= shown <= len(cycles)


def test_a_panel_counts_only_the_cycles_that_carry_its_measure(seeded_store: Store) -> None:
    """Weight starts earlier than HRV here, so it should rest on more cycles."""
    data = load_report_data(seeded_store, cycles_from_days(FAKE_CYCLE_LOG))

    figure = build_cycle_figure(data)

    counts = {
        str(annotation.text).split(" &nbsp;")[0]: int(found.group(1))
        for annotation in figure.layout.annotations
        if (found := re.search(r"(\d+) cycles?", str(annotation.text)))
    }
    placed = by_cycle_day(data.wellness, "date", "hrv_rmssd", cycles_from_days(FAKE_CYCLE_LOG))
    assert counts["HRV rMSSD (ms)"] == placed["cycle_start"].nunique()
