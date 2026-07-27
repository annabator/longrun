"""End-to-end Intervals ingestion against a mocked API."""

import gzip
import json
from pathlib import Path
from typing import Any

import httpx

from longrun.ingest.intervals import IntervalsClient, IntervalsIngester
from longrun.models import Source
from longrun.store import Store

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# Derived rather than hardcoded: the fixture gains activities as real
# shapes turn up, and the counts should follow it.
ACTIVITY_COUNT = len(json.loads((FIXTURES / "intervals_activities.json").read_text()))
STREAM_SAMPLES_PER_ACTIVITY = 5


def _fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _handler(
    calls: list[str], *, files: dict[str, httpx.Response] | None = None
) -> httpx.MockTransport:
    """Serve the fixtures, recording which paths were asked for."""
    file_responses = files or {}

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(path)
        if path.endswith("/activities"):
            # Only the first window carries data; later windows are empty, so
            # the walk terminates the way it would against the real API.
            served = any(other.endswith("/activities") for other in calls[:-1])
            return httpx.Response(200, json=[] if served else _fixture("intervals_activities.json"))
        if path.endswith("/wellness"):
            served = any(other.endswith("/wellness") for other in calls[:-1])
            return httpx.Response(200, json=[] if served else _fixture("intervals_wellness.json"))
        if path.endswith("/streams"):
            return httpx.Response(200, json=_fixture("intervals_streams.json"))
        for suffix, response in file_responses.items():
            if path.endswith(suffix):
                return response
        return httpx.Response(404, json={})

    return httpx.MockTransport(handle)


def _client(calls: list[str], *, files: dict[str, httpx.Response] | None = None) -> IntervalsClient:
    return IntervalsClient(
        "i1234",
        "secret-key",
        http=httpx.Client(transport=_handler(calls, files=files)),
        sleep=lambda _: None,
    )


def test_ingest_writes_activities_streams_and_wellness(store: Store) -> None:
    calls: list[str] = []
    client = _client(calls)

    summary = IntervalsIngester(client).ingest(store)

    counts = store.counts()
    assert counts["activities"] == ACTIVITY_COUNT
    assert counts["activity_streams"] == ACTIVITY_COUNT * STREAM_SAMPLES_PER_ACTIVITY
    # The date-only record carries no measurement and is not stored.
    assert counts["wellness_daily"] == 3
    assert summary.activities == ACTIVITY_COUNT
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


def test_wellness_rows_carry_the_measured_fields(store: Store) -> None:
    """The measured series are what the store keeps; rolling load is not."""
    IntervalsIngester(_client([])).ingest(store)

    row = store.conn.execute(
        "SELECT * FROM wellness_daily WHERE date = ?", ("2026-06-20",)
    ).fetchone()

    assert row["hrv_rmssd"] == 68.2
    assert row["resting_hr"] == 47.0
    assert row["avg_sleeping_hr"] == 51.4
    assert row["spo2"] == 97.0
    # The payload still carries CTL and ATL; the store does not.
    assert {"ctl", "atl", "form"}.isdisjoint(set(row.keys()))


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

    assert summary.streams_skipped == ACTIVITY_COUNT
    assert not any(path.endswith("/streams") for path in calls)


def _fake_fit(payload: bytes = b"") -> bytes:
    """Bytes carrying the FIT header marker, for testing the transport only.

    The client decides what to keep by the header, and the parser is exercised
    separately, so these need the marker rather than a valid body.
    """
    return b"\x00" * 8 + b".FIT" + payload


def test_rr_download_is_off_by_default(store: Store) -> None:
    """Nothing reads beat data yet, so a routine sync must not pay for it."""
    calls: list[str] = []

    IntervalsIngester(_client(calls)).ingest(store)

    assert not any("file" in path for path in calls)


def test_rr_download_prefers_the_original_file(store: Store) -> None:
    """The original upload is the faithful one, so it is asked for first."""
    calls: list[str] = []
    files = {"/file": httpx.Response(200, content=_fake_fit())}

    IntervalsIngester(_client(calls, files=files), with_rr=True, limit=1).ingest(store)

    assert any(path.endswith("/file") for path in calls)
    # The generated fallback is not needed once the original arrives.
    assert not any(path.endswith("/fit-file") for path in calls)


def test_rr_download_falls_back_to_the_generated_fit(store: Store) -> None:
    """An original that is a GPX carries no beat data, so the FIT is tried."""
    calls: list[str] = []
    files = {
        "/file": httpx.Response(200, content=b"<?xml version='1.0'?><gpx></gpx>"),
        "/fit-file": httpx.Response(200, content=_fake_fit()),
    }

    IntervalsIngester(_client(calls, files=files), with_rr=True, limit=1).ingest(store)

    assert any(path.endswith("/fit-file") for path in calls)


def test_gzipped_activity_file_is_decompressed() -> None:
    """The original-file endpoint serves gzip whether or not it says so."""
    calls: list[str] = []
    files = {"/file": httpx.Response(200, content=gzip.compress(_fake_fit()))}
    client = _client(calls, files=files)

    data = client.fetch_activity_fit("i84213770")

    assert data is not None
    assert data.startswith(b"\x00" * 8 + b".FIT")


def test_missing_activity_file_is_not_an_error(store: Store) -> None:
    """Most activities have no file to serve; a sync must not stop for it."""
    calls: list[str] = []

    summary = IntervalsIngester(_client(calls), with_rr=True).ingest(store)

    assert summary.activities == ACTIVITY_COUNT
    assert store.counts()["activities"] == ACTIVITY_COUNT


def test_unparseable_activity_file_is_reported_not_fatal(store: Store) -> None:
    """A file with a FIT header but a broken body must not end the run."""
    calls: list[str] = []
    files = {"/file": httpx.Response(200, content=_fake_fit(b"not really a fit body"))}
    lines: list[str] = []

    summary = IntervalsIngester(_client(calls, files=files), with_rr=True).ingest(
        store, progress=lines.append
    )

    assert summary.activities == ACTIVITY_COUNT
    assert any("could not be parsed" in line for line in lines)


def test_one_unparseable_activity_does_not_end_the_sync(store: Store) -> None:
    """A sync that has already written rows must not be lost to one bad entry."""
    broken = [*_fixture("intervals_activities.json"), {"id": "i999", "type": "Run"}]
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(path)
        if path.endswith("/activities"):
            served = any(other.endswith("/activities") for other in calls[:-1])
            return httpx.Response(200, json=[] if served else broken)
        if path.endswith("/wellness"):
            return httpx.Response(200, json=[])
        if path.endswith("/streams"):
            return httpx.Response(200, json=_fixture("intervals_streams.json"))
        return httpx.Response(404, json={})

    client = IntervalsClient(
        "i1234",
        "key",
        http=httpx.Client(transport=httpx.MockTransport(handle)),
        sleep=lambda _: None,
    )
    lines: list[str] = []

    summary = IntervalsIngester(client).ingest(store, progress=lines.append)

    assert summary.activities == ACTIVITY_COUNT
    assert store.counts()["activities"] == ACTIVITY_COUNT
    assert any("skipped an activity" in line for line in lines)
    assert any("could not be parsed" in line for line in lines)


def test_activities_without_distance_are_stored_as_load_context(store: Store) -> None:
    IntervalsIngester(_client([])).ingest(store)

    rows = store.conn.execute(
        "SELECT sport_type, distance_m, icu_load FROM activities WHERE external_id IN (?, ?)",
        ("i84224001", "i84225500"),
    ).fetchall()

    assert len(rows) == 2
    assert all(row["distance_m"] == 0.0 for row in rows)
    assert all(row["icu_load"] is not None for row in rows)


def test_rows_are_labelled_with_their_source(store: Store) -> None:
    IntervalsIngester(_client([])).ingest(store)

    sources = {
        row["source"] for row in store.conn.execute("SELECT DISTINCT source FROM activities")
    }

    assert sources == {Source.INTERVALS}
