"""FIT ingestion: R-R placement, and the failure paths a backfill will hit.

The binary parsing itself belongs to fitdecode and is not re-tested here.
What is tested is the logic this project adds on top: recovering when in time
each R-R interval occurred, and refusing to let one bad export end a run.
"""

import datetime as dt
from pathlib import Path

import pytest

from longrun.errors import IngestError
from longrun.ingest.fit import FitFolderIngester, activity_key, iter_fit_files
from longrun.ingest.fit.reader import _activity_from_session, _rr_by_second, read_fit_file
from longrun.store import Store


def test_rr_intervals_are_placed_by_accumulated_time() -> None:
    """R-R samples carry no timestamps; position comes from the intervals."""
    # Four beats of 500 ms fill the first two seconds, two per second.
    placed = _rr_by_second([500.0, 500.0, 500.0, 500.0])

    assert placed == {0: 500.0, 1: 500.0, 2: 500.0}


def test_several_beats_in_one_second_are_averaged() -> None:
    """One row per second is the contract; beats within a second are merged."""
    placed = _rr_by_second([300.0, 300.0, 400.0])

    assert placed[0] == pytest.approx(300.0)
    assert placed[1] == pytest.approx(400.0)


def test_slow_beats_leave_seconds_without_a_value() -> None:
    """A 1.5 s interval means some seconds simply have no R-R sample."""
    placed = _rr_by_second([1500.0, 1500.0])

    assert set(placed) == {1, 3}


def test_no_intervals_places_nothing() -> None:
    assert _rr_by_second([]) == {}


def test_activity_key_is_the_session_start_instant() -> None:
    """Identity comes from the run, so two exports of it agree."""
    start = dt.datetime(2026, 7, 25, 8, 2, 18, tzinfo=dt.UTC)

    assert activity_key(start) == "2026-07-25T08:02:18Z"


def test_activity_key_normalises_to_utc() -> None:
    """The same instant in another zone must not become a second activity."""
    utc = dt.datetime(2026, 7, 25, 8, 2, 18, tzinfo=dt.UTC)
    elsewhere = utc.astimezone(dt.timezone(dt.timedelta(hours=5)))

    assert activity_key(elsewhere) == activity_key(utc)


def test_naive_start_is_treated_as_utc() -> None:
    naive = dt.datetime(2026, 7, 25, 8, 2, 18)  # noqa: DTZ001 - the case under test

    assert activity_key(naive) == "2026-07-25T08:02:18Z"


def test_two_exports_of_one_run_share_an_identity(tmp_path: Path) -> None:
    """The real case: one run exported twice under different filenames.

    The files differ slightly - a re-export recomputes elevation and cadence -
    but they describe one session and must not become two activities.
    """
    session = {
        "start_time": dt.datetime(2026, 7, 25, 8, 2, 18, tzinfo=dt.UTC),
        "sport": "running",
        "total_distance": 5468.0,
        "total_elapsed_time": 1930.76,
        "total_timer_time": 1931.08,
        "avg_heart_rate": 184,
        "total_ascent": 9,
    }
    reexport = {**session, "total_ascent": 24, "total_timer_time": 1930.0}

    first = _activity_from_session(session, tmp_path / "i169446382.fit")
    second = _activity_from_session(reexport, tmp_path / "i169446382_Morning_Run.fit")

    assert first.external_id == second.external_id
    assert (first.source, first.external_id) == (second.source, second.external_id)
    # The names still differ, since they come from the filenames.
    assert first.name != second.name


def test_a_session_without_a_start_time_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(IngestError, match="session start time"):
        _activity_from_session({"sport": "running"}, tmp_path / "x.fit")


def test_missing_folder_is_a_clear_error(tmp_path: Path, store: Store) -> None:
    ingester = FitFolderIngester(tmp_path / "not-here")

    with pytest.raises(IngestError, match="does not exist"):
        ingester.ingest(store)


def test_unparseable_file_is_skipped_not_fatal(tmp_path: Path, store: Store) -> None:
    """One corrupt export must not end a backfill of hundreds of files."""
    (tmp_path / "broken.fit").write_bytes(b"this is not a FIT file")
    lines: list[str] = []

    summary = FitFolderIngester(tmp_path).ingest(store, progress=lines.append)

    assert summary.activities == 0
    assert any("skipped" in line for line in lines)
    assert store.counts()["activities"] == 0


def test_reading_an_unparseable_file_raises(tmp_path: Path) -> None:
    path = tmp_path / "broken.fit"
    path.write_bytes(b"not a FIT file")

    with pytest.raises(IngestError, match=r"broken\.fit"):
        read_fit_file(path)


def test_empty_folder_writes_nothing(tmp_path: Path, store: Store) -> None:
    summary = FitFolderIngester(tmp_path).ingest(store)

    assert summary.activities == 0
    assert summary.stream_points == 0


def test_fit_files_are_found_recursively_and_in_order(tmp_path: Path) -> None:
    (tmp_path / "nested").mkdir()
    (tmp_path / "b.fit").write_bytes(b"")
    (tmp_path / "a.fit").write_bytes(b"")
    (tmp_path / "nested" / "c.FIT").write_bytes(b"")
    (tmp_path / "notes.txt").write_text("ignored")

    found = [path.name for path in iter_fit_files(tmp_path)]

    assert found == ["a.fit", "b.fit", "c.FIT"]
