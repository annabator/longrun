"""Logging period days.

All dates here are invented for the test. Nothing in this file, or in any
fixture it uses, comes from a real record.
"""

import datetime as dt
from pathlib import Path

from longrun.store.private import CycleStore

# An obviously fictional log: a tidy 28-day cycle nobody actually has.
FAKE_PERIOD = [dt.date(2026, 1, 5) + dt.timedelta(days=offset) for offset in range(5)]


def test_logging_records_the_days(tmp_path: Path) -> None:
    with CycleStore.open(tmp_path / "cycle.db") as store:
        added = store.log_days(FAKE_PERIOD)

        assert added == 5
        assert store.logged_days() == FAKE_PERIOD


def test_logging_the_same_day_twice_changes_nothing(tmp_path: Path) -> None:
    """A command that can be safely re-run is one that gets re-run correctly."""
    with CycleStore.open(tmp_path / "cycle.db") as store:
        store.log_days(FAKE_PERIOD)

        added = store.log_days(FAKE_PERIOD)

        assert added == 0
        assert store.count() == 5


def test_days_come_back_in_order_however_they_went_in(tmp_path: Path) -> None:
    with CycleStore.open(tmp_path / "cycle.db") as store:
        store.log_days(reversed(FAKE_PERIOD))

        assert store.logged_days() == FAKE_PERIOD


def test_removing_a_day_corrects_a_mistake(tmp_path: Path) -> None:
    with CycleStore.open(tmp_path / "cycle.db") as store:
        store.log_days(FAKE_PERIOD)

        removed = store.remove_days([FAKE_PERIOD[-1]])

        assert removed == 1
        assert FAKE_PERIOD[-1] not in store.logged_days()


def test_removing_a_day_that_was_never_logged_is_harmless(tmp_path: Path) -> None:
    with CycleStore.open(tmp_path / "cycle.db") as store:
        store.log_days(FAKE_PERIOD)

        assert store.remove_days([dt.date(2026, 6, 1)]) == 0
        assert store.count() == 5


def test_days_can_be_read_back_in_a_range(tmp_path: Path) -> None:
    with CycleStore.open(tmp_path / "cycle.db") as store:
        store.log_days(FAKE_PERIOD)

        selected = store.logged_days(since=dt.date(2026, 1, 7), until=dt.date(2026, 1, 8))

        assert selected == [dt.date(2026, 1, 7), dt.date(2026, 1, 8)]


def test_an_empty_store_reads_back_empty(tmp_path: Path) -> None:
    with CycleStore.open(tmp_path / "cycle.db") as store:
        assert store.logged_days() == []
        assert store.count() == 0


def test_the_store_only_holds_dates(tmp_path: Path) -> None:
    """The least possible is stored: no flow, no symptoms, no derived phase."""
    with CycleStore.open(tmp_path / "cycle.db") as store:
        columns = {str(row["name"]) for row in store.conn.execute("PRAGMA table_info(period_days)")}

        assert columns == {"date"}


def test_reopening_keeps_what_was_logged(tmp_path: Path) -> None:
    path = tmp_path / "cycle.db"
    with CycleStore.open(path) as store:
        store.log_days(FAKE_PERIOD)

    with CycleStore.open(path) as store:
        assert store.logged_days() == FAKE_PERIOD
