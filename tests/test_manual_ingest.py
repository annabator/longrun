"""Loading hand-maintained CSVs, including the ways they go wrong."""

import datetime as dt
from pathlib import Path

import pytest

from longrun.errors import IngestError
from longrun.ingest.manual import load_tests_csv, load_wellness_csv


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


def test_blank_rows_are_skipped(tmp_path: Path) -> None:
    """Spreadsheet exports often end with empty lines."""
    path = tmp_path / "wellness.csv"
    path.write_text("date,resting_hr\n2026-01-01,48\n,\n\n", encoding="utf-8")

    assert len(load_wellness_csv(path)) == 1
