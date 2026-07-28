"""Loading hand-maintained CSVs, including the ways they go wrong."""

import datetime as dt
from pathlib import Path

import pytest

from longrun.errors import IngestError
from longrun.ingest.manual import (
    is_wellness_metric,
    load_tests_csv,
    load_wellness_csv,
    measurement_entry,
    parse_measurement,
    wellness_entry,
)


def test_sample_wellness_loads(sample_dir: Path) -> None:
    rows = load_wellness_csv(sample_dir / "wellness_sample.csv")

    assert rows
    assert rows[0].date == dt.date(2025, 1, 1)
    # Weight is recorded monthly, so most daily rows leave it blank.
    assert any(row.weight_kg is not None for row in rows)
    assert any(row.weight_kg is None for row in rows)


def test_blank_cells_become_absent_not_zero(tmp_path: Path) -> None:
    """A partially filled row should contribute what it has and nothing more."""
    path = tmp_path / "wellness.csv"
    path.write_text("date,resting_hr,weight_kg\n2026-01-01,,61.4\n", encoding="utf-8")

    row = load_wellness_csv(path)[0]

    assert row.resting_hr is None
    assert row.weight_kg == 61.4


def test_durations_are_converted_to_seconds(tmp_path: Path) -> None:
    """A parkrun is written as it is recorded, not converted by hand."""
    path = tmp_path / "tests.csv"
    path.write_text(
        "date,metric,value,unit\n2026-01-03,parkrun_5k,22:47,\n2026-01-10,long_effort,1:29:04,\n",
        encoding="utf-8",
    )

    rows = load_tests_csv(path)

    assert rows[0].value == 1367.0
    assert rows[0].unit == "s"
    assert rows[1].value == 5344.0


def test_metric_names_are_normalised(tmp_path: Path) -> None:
    """`VO2max` and `vo2max` must not become two separate series."""
    path = tmp_path / "tests.csv"
    path.write_text("date,metric,value,unit\n2026-01-01, VO2max ,52,ml/kg/min\n", encoding="utf-8")

    assert load_tests_csv(path)[0].metric == "vo2max"


def test_ambiguous_date_is_rejected_with_its_line_number(tmp_path: Path) -> None:
    """A misread date bends a trend line silently; an error does not."""
    path = tmp_path / "tests.csv"
    path.write_text(
        "date,metric,value,unit\n2026-01-01,vo2max,52,ml/kg/min\n03/04/2026,vo2max,53,ml/kg/min\n",
        encoding="utf-8",
    )

    with pytest.raises(IngestError, match=r":3: .*ISO-8601"):
        load_tests_csv(path)


def test_missing_required_column_names_what_is_absent(tmp_path: Path) -> None:
    path = tmp_path / "tests.csv"
    path.write_text("date,value\n2026-01-01,52\n", encoding="utf-8")

    with pytest.raises(IngestError, match="metric"):
        load_tests_csv(path)


def test_non_numeric_value_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "tests.csv"
    path.write_text("date,metric,value,unit\n2026-01-01,vo2max,fifty,ml/kg/min\n", encoding="utf-8")

    with pytest.raises(IngestError, match="not a number"):
        load_tests_csv(path)


def test_wellness_metrics_route_to_the_wellness_day() -> None:
    assert is_wellness_metric("weight") is True
    assert is_wellness_metric("Resting_HR") is True
    assert is_wellness_metric("vo2max") is False
    assert is_wellness_metric("ferritin") is False


def test_a_logged_reading_leaves_the_rest_of_the_day_absent() -> None:
    """One field set and the others None is what lets the store merge it.

    A logged weight must slot into whatever that day already holds rather
    than replacing it.
    """
    entry = wellness_entry("weight", "74.2", dt.date(2026, 7, 20), "manual")

    assert entry.weight_kg == 74.2
    assert entry.resting_hr is None
    assert entry.hrv_rmssd is None


def test_hrv_is_accepted_as_a_name_for_rmssd() -> None:
    entry = wellness_entry("hrv", "62", dt.date(2026, 7, 20), "manual")

    assert entry.hrv_rmssd == 62.0


def test_a_logged_reading_must_be_a_number() -> None:
    with pytest.raises(IngestError, match="not a number"):
        wellness_entry("weight", "heavy", dt.date(2026, 7, 20), "manual")


def test_a_known_metric_supplies_its_own_unit() -> None:
    """Typing `--unit ml/kg/min` every time is friction that stops it happening."""
    entry = measurement_entry("vo2max", "45", dt.date(2026, 7, 28), "", "manual", None)

    assert entry.unit == "ml/kg/min"
    assert entry.value == 45.0


def test_a_logged_duration_becomes_seconds() -> None:
    entry = measurement_entry("parkrun_5k", "29:25", dt.date(2026, 7, 25), "", "manual", "windy")

    assert entry.value == 1765.0
    assert entry.unit == "s"
    assert entry.notes == "windy"


def test_an_unknown_metric_asks_for_a_unit_rather_than_guessing() -> None:
    with pytest.raises(IngestError, match="unit is required"):
        measurement_entry("wibble", "5", dt.date(2026, 7, 28), "", "manual", None)


def test_an_explicit_unit_overrides_the_default() -> None:
    entry = measurement_entry("vo2max", "45", dt.date(2026, 7, 28), "ml/min/kg", "manual", None)

    assert entry.unit == "ml/min/kg"


def test_a_logged_metric_name_is_normalised() -> None:
    """So that `VO2max` typed at the prompt is the same series as `vo2max`."""
    entry = measurement_entry(" VO2max ", "45", dt.date(2026, 7, 28), "", "manual", None)

    assert entry.metric == "vo2max"


def test_the_csv_and_the_prompt_parse_a_duration_the_same_way() -> None:
    """One shared parser, so `29:25` means the same however it is entered."""
    assert parse_measurement("29:25", "") == (1765.0, "s")
    assert parse_measurement("1:29:04", "") == (5344.0, "s")
    assert parse_measurement("45", "ml/kg/min") == (45.0, "ml/kg/min")


def test_blank_rows_are_skipped(tmp_path: Path) -> None:
    """Spreadsheet exports often end with empty lines."""
    path = tmp_path / "wellness.csv"
    path.write_text("date,resting_hr\n2026-01-01,48\n,\n\n", encoding="utf-8")

    assert len(load_wellness_csv(path)) == 1
