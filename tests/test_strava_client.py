"""Token refresh and the two rate-limit behaviours the client distinguishes."""

import datetime as dt
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from longrun.config import StravaSettings
from longrun.errors import RateLimitError
from longrun.ingest.strava import StravaAuthenticator, StravaClient, TokenStore

ACTIVITY: dict[str, Any] = {
    "id": 1,
    "name": "Run",
    "sport_type": "Run",
    "start_date": "2026-05-02T07:15:09Z",
    "distance": 5000.0,
    "moving_time": 1500,
    "elapsed_time": 1520,
}


def _token_response() -> httpx.Response:
    expires = dt.datetime.now(tz=dt.UTC) + dt.timedelta(hours=6)
    return httpx.Response(
        200,
        json={
            "access_token": "fresh-access",
            "refresh_token": "rotated-refresh",
            "expires_at": int(expires.timestamp()),
        },
    )


def _build_client(
    handler: httpx.MockTransport, tmp_path: Path, sleeps: list[float]
) -> StravaClient:
    settings = StravaSettings(
        client_id="1",
        client_secret=SecretStr("secret"),
        refresh_token=SecretStr("seed"),
    )
    authenticator = StravaAuthenticator(settings, TokenStore(tmp_path / "tokens.json"))
    return StravaClient(authenticator, http=httpx.Client(transport=handler), sleep=sleeps.append)


def test_refreshed_token_is_persisted_with_owner_only_permissions(tmp_path: Path) -> None:
    """Strava rotates the refresh token, so the new one has to be written back."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return _token_response()
        return httpx.Response(200, json=[])

    sleeps: list[float] = []
    client = _build_client(httpx.MockTransport(handle), tmp_path, sleeps)
    list(client.iter_activities())

    tokens = tmp_path / "tokens.json"
    assert "rotated-refresh" in tokens.read_text(encoding="utf-8")
    assert tokens.stat().st_mode & 0o777 == 0o600


def test_short_window_limit_is_waited_out_then_retried(tmp_path: Path) -> None:
    """A 15-minute window resets soon, so the client sleeps rather than failing."""
    state = {"limited": False}

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return _token_response()
        headers = {"X-RateLimit-Limit": "200,2000", "X-RateLimit-Usage": "200,500"}
        if not state["limited"]:
            state["limited"] = True
            return httpx.Response(429, headers=headers)
        return httpx.Response(200, json=[ACTIVITY], headers=headers)

    sleeps: list[float] = []
    client = _build_client(httpx.MockTransport(handle), tmp_path, sleeps)
    activities = list(client.iter_activities())

    assert len(activities) == 1
    assert len(sleeps) == 1
    assert 0 < sleeps[0] <= 15 * 60


def test_exhausted_daily_budget_raises_rather_than_sleeping(tmp_path: Path) -> None:
    """Waiting out a daily limit could mean hours, so the run stops instead."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return _token_response()
        return httpx.Response(
            429,
            headers={"X-RateLimit-Limit": "200,2000", "X-RateLimit-Usage": "200,2000"},
        )

    sleeps: list[float] = []
    client = _build_client(httpx.MockTransport(handle), tmp_path, sleeps)

    with pytest.raises(RateLimitError, match="daily rate limit"):
        list(client.iter_activities())

    assert sleeps == []


def test_activity_without_streams_is_not_an_error(tmp_path: Path) -> None:
    """Strava answers 404 for activities that carry no streams."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return _token_response()
        return httpx.Response(404, json={})

    sleeps: list[float] = []
    client = _build_client(httpx.MockTransport(handle), tmp_path, sleeps)

    assert client.fetch_streams(1) == {}
