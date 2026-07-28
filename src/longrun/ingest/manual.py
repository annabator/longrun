"""Load manually maintained CSV files into the store.

Backfilling matters here: weight, VO2max, blood tests and parkrun results
mostly predate any tooling and live in spreadsheets. These loaders are
deliberately forgiving about optional columns and blank cells, and
deliberately strict about dates, because a misread date silently corrupts a
trend line in a way a missing value does not.

Dates must be ISO-8601 (`YYYY-MM-DD`). Ambiguous formats such as `03/04/2026`
are rejected rather than guessed at.

Durations may be written the way they are recorded in practice - `22:47` for
a parkrun, or `1:29:04` for a longer effort - and are converted to seconds.
"""

import csv
import datetime as dt
from collections.abc import Iterator, Mapping
from pathlib import Path

from longrun.errors import IngestError
from longrun.models import Source, TestResult, WellnessDay

WELLNESS_COLUMNS = (
    "date",
    "resting_hr",
    "avg_sleeping_hr",
    "hrv_rmssd",
    "sleep_score",
    "sleep_hours",
    "spo2",
    "weight_kg",
    "source",
    "notes",
)
"""Recognised columns for a wellness CSV; only `date` is required.

`hrv` is still accepted as a synonym for `hrv_rmssd`, so spreadsheets written
against v1.0 keep loading without being edited.
"""

TEST_COLUMNS = ("date", "metric", "value", "unit", "source", "notes")
"""Recognised columns for a tests CSV; `notes` and `source` are optional."""


WELLNESS_METRIC_FIELDS: Mapping[str, str] = {
    "weight": "weight_kg",
    "weight_kg": "weight_kg",
    "resting_hr": "resting_hr",
    "avg_sleeping_hr": "avg_sleeping_hr",
    "hrv": "hrv_rmssd",
    "hrv_rmssd": "hrv_rmssd",
    "sleep_hours": "sleep_hours",
    "sleep_score": "sleep_score",
    "spo2": "spo2",
}
"""Metric names that belong on a wellness day rather than in `tests`.

One reading per day of something the body is doing continuously. Anything
else - a race result, a blood test, a VO2max estimate - is a point-in-time
measurement and goes to the long-format table instead.
"""

DEFAULT_UNITS: Mapping[str, str] = {
    "vo2max": "ml/kg/min",
    "ferritin": "ug/L",
    "vitamin_d": "nmol/L",
    "haemoglobin": "g/dL",
    "body_fat_pct": "%",
}
"""Units assumed for well-known metrics, so a unit need not be typed."""


def is_wellness_metric(metric: str) -> bool:
    """Return whether a metric belongs in `wellness_daily`.

    Args:
        metric: The metric name, in any case.

    Returns:
        True for a daily wellness field.
    """
    return metric.strip().lower() in WELLNESS_METRIC_FIELDS


def wellness_entry(metric: str, raw_value: str, day: dt.date, source: str) -> WellnessDay:
    """Build a wellness day carrying one logged reading.

    Every other field is left absent, so the store merges this into whatever
    that day already holds rather than replacing it.

    Args:
        metric: A name from :data:`WELLNESS_METRIC_FIELDS`.
        raw_value: The value as typed.
        day: The date it applies to.
        source: Where it came from.

    Returns:
        A wellness day with exactly one field populated.

    Raises:
        IngestError: If the value is not a number.
    """
    field = WELLNESS_METRIC_FIELDS[metric.strip().lower()]
    try:
        value = float(raw_value.strip())
    except ValueError as exc:
        msg = f"{raw_value!r} is not a number"
        raise IngestError(msg) from exc
    fields: dict[str, object] = {"date": day, "source": source, field: value}
    return WellnessDay.model_validate(fields)


def measurement_entry(
    metric: str,
    raw_value: str,
    day: dt.date,
    unit: str,
    source: str,
    note: str | None,
) -> TestResult:
    """Build a long-format measurement from one logged reading.

    Args:
        metric: The metric name.
        raw_value: The value as typed; a duration such as `29:25` is accepted.
        day: The date it applies to.
        unit: The unit, or empty to use a known default.
        source: Where it came from.
        note: Optional free text.

    Returns:
        The measurement.

    Raises:
        IngestError: If the value or unit cannot be resolved.
    """
    name = metric.strip().lower()
    resolved_unit = unit.strip() or DEFAULT_UNITS.get(name, "")
    try:
        value, final_unit = parse_measurement(raw_value, resolved_unit)
    except ValueError as exc:
        msg = f"Could not log {metric}: {exc}"
        raise IngestError(msg) from exc
    return TestResult(
        date=day, metric=name, value=value, unit=final_unit, source=source, notes=note
    )


def load_wellness_csv(path: Path) -> list[WellnessDay]:
    """Read a wellness CSV into domain models.

    Recognised columns are listed in :data:`WELLNESS_COLUMNS`. Only `date` is
    required; any other column may be absent, and blank cells become None so
    that a partially filled row still contributes what it has.

    Args:
        path: CSV file to read.

    Returns:
        One record per row, in file order.

    Raises:
        IngestError: If the file is unreadable, lacks a `date` column, or a
            row cannot be parsed.
    """
    records: list[WellnessDay] = []
    for line_no, row in _rows(path, required={"date"}):
        records.append(
            WellnessDay(
                date=_parse_date(row["date"], path, line_no),
                resting_hr=_parse_optional_number(row.get("resting_hr"), path, line_no),
                avg_sleeping_hr=_parse_optional_number(row.get("avg_sleeping_hr"), path, line_no),
                # `hrv` is accepted as a synonym so spreadsheets written
                # against v1.0 keep loading unchanged.
                hrv_rmssd=_parse_optional_number(
                    row.get("hrv_rmssd") or row.get("hrv"), path, line_no
                ),
                sleep_score=_parse_optional_number(row.get("sleep_score"), path, line_no),
                sleep_hours=_parse_optional_number(row.get("sleep_hours"), path, line_no),
                spo2=_parse_optional_number(row.get("spo2"), path, line_no),
                weight_kg=_parse_optional_number(row.get("weight_kg"), path, line_no),
                source=_clean(row.get("source")) or Source.MANUAL,
                notes=_clean(row.get("notes")) or None,
            )
        )
    return records


def load_tests_csv(path: Path) -> list[TestResult]:
    """Read a long-format tests CSV into domain models.

    Recognised columns are listed in :data:`TEST_COLUMNS`. `date`, `metric`
    and `value` are required. A `value` written as a duration (`22:47`) is
    converted to seconds and its unit normalised to `s`, so parkrun times
    sort and compare correctly.

    Args:
        path: CSV file to read.

    Returns:
        One record per row, in file order.

    Raises:
        IngestError: If the file is unreadable, lacks a required column, or
            a row cannot be parsed.
    """
    records: list[TestResult] = []
    for line_no, row in _rows(path, required={"date", "metric", "value"}):
        value, unit = _parse_measurement(row, path, line_no)
        records.append(
            TestResult(
                date=_parse_date(row["date"], path, line_no),
                metric=_clean(row["metric"]),
                value=value,
                unit=unit,
                source=_clean(row.get("source")) or Source.MANUAL,
                notes=_clean(row.get("notes")) or None,
            )
        )
    return records


def _rows(path: Path, required: set[str]) -> Iterator[tuple[int, Mapping[str, str]]]:
    """Yield `(line number, row)` pairs, skipping rows that are entirely blank.

    Args:
        path: CSV file to read.
        required: Column names that must be present in the header.

    Yields:
        The 1-based line number in the file and the parsed row.

    Raises:
        IngestError: If the file cannot be read or a required column is
            missing from the header.
    """
    try:
        # utf-8-sig strips the byte-order mark spreadsheet exports often add.
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            header = {name.strip() for name in reader.fieldnames or []}
            missing = required - header
            if missing:
                msg = (
                    f"{path} is missing required column(s): {', '.join(sorted(missing))}. "
                    f"Found: {', '.join(sorted(header)) or '<no header>'}"
                )
                raise IngestError(msg)
            for line_no, raw in enumerate(reader, start=2):
                row = {(key or "").strip(): (value or "") for key, value in raw.items()}
                if not any(value.strip() for value in row.values()):
                    continue
                yield line_no, row
    except OSError as exc:
        msg = f"Could not read {path}: {exc}"
        raise IngestError(msg) from exc


def parse_measurement(raw_value: str, unit: str) -> tuple[float, str]:
    """Return the numeric value and unit for a measurement.

    Shared by the CSV loader and by `longrun log`, so a duration written as
    `29:25` means the same thing however it is entered.

    Args:
        raw_value: The value as written.
        unit: The unit as written, which may be empty for a duration.

    Returns:
        A `(value, unit)` pair, durations converted to seconds.

    Raises:
        ValueError: If the value is blank, unparseable, or has no unit.
    """
    value_text = raw_value.strip()
    unit_text = unit.strip()
    if ":" in value_text:
        return parse_duration(value_text), "s"
    if not value_text:
        msg = "value is required but blank"
        raise ValueError(msg)
    try:
        value = float(value_text)
    except ValueError as exc:
        msg = f"{value_text!r} is not a number"
        raise ValueError(msg) from exc
    if not unit_text:
        msg = "unit is required unless the value is a duration"
        raise ValueError(msg)
    return value, unit_text


def parse_duration(raw: str) -> float:
    """Convert `mm:ss` or `hh:mm:ss` to seconds.

    Args:
        raw: The duration as written.

    Returns:
        The duration in seconds.

    Raises:
        ValueError: If it is not a recognisable duration.
    """
    parts = raw.split(":")
    max_parts = 3  # hh:mm:ss is the longest form accepted
    if len(parts) > max_parts:
        msg = f"{raw!r} is not a duration (expected mm:ss or hh:mm:ss)"
        raise ValueError(msg)
    try:
        numbers = [float(part) for part in parts]
    except ValueError as exc:
        msg = f"{raw!r} is not a duration (expected mm:ss or hh:mm:ss)"
        raise ValueError(msg) from exc
    seconds = 0.0
    for number in numbers:
        seconds = seconds * 60 + number
    return seconds


def _parse_measurement(row: Mapping[str, str], path: Path, line_no: int) -> tuple[float, str]:
    """Return the value and unit for a tests row, naming the offending line."""
    try:
        return parse_measurement(_clean(row.get("value")), _clean(row.get("unit")))
    except ValueError as exc:
        raise _row_error(path, line_no, str(exc)) from exc


def _parse_date(raw: str, path: Path, line_no: int) -> dt.date:
    """Parse an ISO-8601 date, rejecting ambiguous formats."""
    text = _clean(raw)
    try:
        return dt.date.fromisoformat(text)
    except ValueError as exc:
        raise _row_error(
            path, line_no, f"{text!r} is not an ISO-8601 date (expected YYYY-MM-DD)"
        ) from exc


def _parse_optional_number(raw: str | None, path: Path, line_no: int) -> float | None:
    """Parse a numeric cell, treating blanks as absent."""
    text = _clean(raw)
    if not text:
        return None
    try:
        return float(text)
    except ValueError as exc:
        raise _row_error(path, line_no, f"{text!r} is not a number") from exc


def _clean(raw: str | None) -> str:
    """Strip surrounding whitespace, mapping None to an empty string."""
    return (raw or "").strip()


def _row_error(path: Path, line_no: int, problem: str) -> IngestError:
    """Build an error that points at the offending line."""
    return IngestError(f"{path}:{line_no}: {problem}")
