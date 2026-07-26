"""Shared fixtures.

Everything here is built from the synthetic sample dataset or from values
made up in the test itself. No fixture reads a real athlete's data.
"""

import datetime as dt
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from longrun.ingest.manual import load_tests_csv, load_wellness_csv
from longrun.ingest.strava.mapper import activity_from_payload, stream_points_from_payload
from longrun.models import Activity, Source
from longrun.store import Store

SAMPLE_DIR = Path(__file__).resolve().parent.parent / "sample_data"


@pytest.fixture
def sample_dir() -> Path:
    return SAMPLE_DIR


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    with Store.open(tmp_path / "test.db") as opened:
        yield opened


@pytest.fixture
def seeded_store(tmp_path: Path) -> Iterator[Store]:
    """A store loaded with the whole synthetic dataset."""
    payload = json.loads((SAMPLE_DIR / "strava_activities.json").read_text(encoding="utf-8"))
    with Store.open(tmp_path / "seeded.db") as opened:
        opened.upsert_wellness(load_wellness_csv(SAMPLE_DIR / "wellness_sample.csv"))
        opened.upsert_tests(load_tests_csv(SAMPLE_DIR / "tests_sample.csv"))
        opened.upsert_activities([activity_from_payload(item) for item in payload["activities"]])
        for strava_id, streams in payload["streams"].items():
            opened.upsert_streams(
                Source.STRAVA, str(strava_id), stream_points_from_payload(streams)
            )
        yield opened


@pytest.fixture
def activity() -> Activity:
    return Activity(
        source=Source.STRAVA,
        external_id="4242",
        start_time=dt.datetime(2026, 3, 1, 7, 30, tzinfo=dt.UTC),
        sport_type="Run",
        name="Morning run",
        distance_m=10_000.0,
        moving_time_s=2700,
        elapsed_time_s=2790,
        avg_hr=148.0,
    )
