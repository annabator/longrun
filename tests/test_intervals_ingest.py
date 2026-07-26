"""End-to-end Intervals ingestion against a mocked API."""

import json
from pathlib import Path
from typing import Any

import httpx

from longrun.ingest.intervals import IntervalsClient, IntervalsIngester
from longrun.models import Source
from longrun.store import Store

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _handler(calls: list[str]) -> httpx.MockTransport:
    """Serve the fixtures, recording which paths were asked for."""

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/activities"):
            # Only the first window carries data; later windows are empty, so
            # the walk terminates the way it would against the real API.
            served = any(path.endswith("/activities") for path in calls[:-1])
            return httpx.Response(200, json=[] if served else _fixture("intervals_activities.json"))
        if request.url.path.endswith("/wellness"):
            served = any(path.endswith("/wellness") for path in calls[:-1])
            return httpx.Response(200, json=[] if served else _fixture("intervals_wellness.json"))
        if request.url.path.endswith("/streams"):
            return httpx.Response(200, json=_fixture("intervals_streams.json"))
        return httpx.Response(404, json={})

    return httpx.MockTransport(handle)


def _client(calls: list[str]) -> IntervalsClient:
    return IntervalsClient(
        "i1234",
        "secret-key",
        http=httpx.Client(transport=_handler(calls)),
        sleep=lambda _: None,
    )


def test_ingest_writes_activities_streams_and_wellness(store: Store) -> None:
    calls: list[str] = []
    client = _client(calls)

    summary = IntervalsIngester(client).ingest(store)

    counts = store.counts()
    assert counts["activities"] == 3
    assert counts["activity_streams"] == 15  # 5 samples for each of 3 activities
    # The date-only record carries no measurement and is not stored.
    assert counts["wellness_daily"] == 3
    assert summary.activities == 3
    assert summary.wellness_days == 3


def test_ingest_is_idempotent(store: Store) -> None:
    """Running twice must leave the store exactly as the first run did."""
    IntervalsIngester(_client([])).ingest(store)
    first = store.counts()

    IntervalsIngester(_client([])).ingest(store)

    assert store.counts() == first


def test_stored_rows_carry_the_upstream_load_fields(store: Store) -> None:
    IntervalsIngester(_client([])).ingest(store)

    row = store.conn.execute(
        "SELECT feel, rpe, icu_load, icu_intensity FROM activities WHERE external_id = ?",
        ("i84213770",),
    ).fetchone()

    assert row["feel"] == 4.0
    assert row["rpe"] == 7.0
    assert row["icu_load"] == 96.4
    assert row["icu_intensity"] == 88.2


def test_wellness_rows_carry_fitness_fields(store: Store) -> None:
    IntervalsIngester(_client([])).ingest(store)

    row = store.conn.execute(
        "SELECT ctl, atl, form, hrv_rmssd, spo2 FROM wellness_daily WHERE date = ?",
        ("2026-06-20",),
    ).fetchone()

    assert row["ctl"] == 62.4
    assert row["atl"] == 71.8
    assert row["form"] == 62.4 - 71.8
    assert row["hrv_rmssd"] == 68.2
    assert row["spo2"] == 97.0


def test_streams_can_be_skipped(store: Store) -> None:
    calls: list[str] = []

    IntervalsIngester(_client(calls), with_streams=False).ingest(store)

    assert store.counts()["activity_streams"] == 0
    assert not any(path.endswith("/streams") for path in calls)


def test_wellness_can_be_skipped(store: Store) -> None:
    calls: list[str] = []

    IntervalsIngester(_client(calls), with_wellness=False).ingest(store)

    assert store.counts()["wellness_daily"] == 0
    assert not any(path.endswith("/wellness") for path in calls)


def test_limit_stops_early(store: Store) -> None:
    IntervalsIngester(_client([]), limit=1).ingest(store)

    assert store.counts()["activities"] == 1


def test_second_run_skips_activities_that_already_have_streams(store: Store) -> None:
    """Streams are the expensive call, so they are fetched once per activity."""
    IntervalsIngester(_client([])).ingest(store)

    calls: list[str] = []
    summary = IntervalsIngester(_client(calls)).ingest(store)

    assert summary.streams_skipped == 3
    assert not any(path.endswith("/streams") for path in calls)


def test_rows_are_labelled_with_their_source(store: Store) -> None:
    IntervalsIngester(_client([])).ingest(store)

    sources = {
        row["source"] for row in store.conn.execute("SELECT DISTINCT source FROM activities")
    }

    assert sources == {Source.INTERVALS}
