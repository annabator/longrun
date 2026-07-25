"""A rate-limit-aware HTTP client for the Strava API.

Strava enforces two budgets at once: a rolling 15-minute allowance and a
daily one. The two failure modes deserve different treatment, so they get it:

* a 15-minute window is worth waiting out, because it resets at the next
  quarter hour, and the client sleeps until then;
* a daily budget is not, because it can be hours away, so the client raises
  and lets the athlete resume later. Already-ingested data is committed as it
  goes, so a resumed run picks up where it stopped.
"""

import datetime as dt
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from longrun.errors import RateLimitError
from longrun.ingest.strava.auth import StravaAuthenticator

API_BASE_URL = "https://www.strava.com/api/v3"

STREAM_KEYS: tuple[str, ...] = (
    "time",
    "heartrate",
    "velocity_smooth",
    "distance",
    "altitude",
    "cadence",
)
"""Stream types requested per activity."""

_RATE_LIMIT_WINDOW_S = 15 * 60
_MAX_RETRIES = 3
_SERVER_ERROR_BACKOFF_S = 2.0
_REQUEST_TIMEOUT_S = 30.0


@dataclass(frozen=True)
class RateLimitStatus:
    """The rate-limit budgets reported by Strava on every response."""

    short_limit: int
    short_usage: int
    daily_limit: int
    daily_usage: int

    @classmethod
    def from_headers(cls, headers: httpx.Headers) -> "RateLimitStatus | None":
        """Parse the `X-RateLimit-*` headers, if present and well formed.

        Args:
            headers: Response headers from a Strava API call.

        Returns:
            The parsed budgets, or None if Strava did not report them.
        """
        limits = headers.get("X-RateLimit-Limit")
        usage = headers.get("X-RateLimit-Usage")
        if not limits or not usage:
            return None
        try:
            short_limit, daily_limit = (int(part) for part in limits.split(",", 1))
            short_usage, daily_usage = (int(part) for part in usage.split(",", 1))
        except ValueError:
            return None
        return cls(short_limit, short_usage, daily_limit, daily_usage)

    @property
    def daily_exhausted(self) -> bool:
        """Whether the daily budget has been used up."""
        return self.daily_usage >= self.daily_limit


class StravaClient:
    """Reads activities and streams from the Strava API."""

    def __init__(
        self,
        authenticator: StravaAuthenticator,
        *,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_retries: int = _MAX_RETRIES,
    ) -> None:
        """Create a client.

        Args:
            authenticator: Supplies and refreshes the bearer token.
            http: HTTP client to use; one is created if omitted.
            sleep: Injected for testing, so rate-limit waits are instant.
            max_retries: Attempts per request before giving up.
        """
        self._auth = authenticator
        self._http = http or httpx.Client(timeout=_REQUEST_TIMEOUT_S)
        self._sleep = sleep
        self._max_retries = max_retries
        self.last_rate_limit: RateLimitStatus | None = None

    def iter_activities(
        self, after: dt.datetime | None = None, per_page: int = 200
    ) -> Iterator[dict[str, Any]]:
        """Yield activity summaries, newest page by page, oldest first.

        Args:
            after: Only return activities that started strictly after this
                time. Passing the latest stored start time makes ingestion
                incremental.
            per_page: Page size; Strava caps this at 200.

        Yields:
            Raw activity payloads, exactly as Strava returned them.
        """
        page = 1
        params: dict[str, Any] = {"per_page": per_page}
        if after is not None:
            params["after"] = int(after.astimezone(dt.UTC).timestamp())
        while True:
            payload = self._get("/athlete/activities", {**params, "page": page})
            if not isinstance(payload, list) or not payload:
                return
            yield from payload
            if len(payload) < per_page:
                return
            page += 1

    def fetch_streams(self, activity_id: int, keys: Sequence[str] = STREAM_KEYS) -> dict[str, Any]:
        """Fetch the time-series streams for one activity.

        Args:
            activity_id: Strava identifier of the activity.
            keys: Stream types to request.

        Returns:
            The raw streams payload keyed by type, or an empty mapping for
            activities that carry no streams (manual entries, for instance).
        """
        payload = self._get(
            f"/activities/{activity_id}/streams",
            {"keys": ",".join(keys), "key_by_type": "true"},
            missing_ok=True,
        )
        return payload if isinstance(payload, dict) else {}

    def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        self._http.close()

    def _get(self, path: str, params: dict[str, Any], *, missing_ok: bool = False) -> Any:
        """Perform a GET against the API, retrying on rate limits and 5xx.

        Args:
            path: Path relative to the API root.
            params: Query parameters.
            missing_ok: Treat 404 as an empty result instead of an error.

        Returns:
            The decoded JSON body.

        Raises:
            RateLimitError: If the daily budget is spent, or retries run out.
            httpx.HTTPStatusError: For any other unsuccessful response.
        """
        for attempt in range(self._max_retries):
            token = self._auth.access_token(self._http)
            response = self._http.get(
                f"{API_BASE_URL}{path}",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
                timeout=_REQUEST_TIMEOUT_S,
            )
            self.last_rate_limit = RateLimitStatus.from_headers(response.headers)

            if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
                self._wait_out_rate_limit(response)
                continue
            if missing_ok and response.status_code == httpx.codes.NOT_FOUND:
                return {}
            if response.status_code >= httpx.codes.INTERNAL_SERVER_ERROR:
                self._sleep(_SERVER_ERROR_BACKOFF_S * (2**attempt))
                continue

            response.raise_for_status()
            return response.json()

        msg = (
            f"Gave up on {path} after {self._max_retries} attempts against Strava's "
            f"rate limits. Already-ingested data has been saved; re-run to resume."
        )
        raise RateLimitError(msg)

    def _wait_out_rate_limit(self, response: httpx.Response) -> None:
        """Sleep until the short window resets, or raise if the day is spent.

        Args:
            response: The 429 response, carrying the budget headers.

        Raises:
            RateLimitError: If the daily budget has been exhausted.
        """
        status = self.last_rate_limit
        if status is not None and status.daily_exhausted:
            msg = (
                f"Strava's daily rate limit is exhausted "
                f"({status.daily_usage}/{status.daily_limit} requests). "
                f"Already-ingested data has been saved; re-run tomorrow to resume."
            )
            raise RateLimitError(msg)

        retry_after = response.headers.get("Retry-After")
        if retry_after and retry_after.isdigit():
            self._sleep(float(retry_after))
            return
        self._sleep(_seconds_until_window_reset())


def _seconds_until_window_reset(now: dt.datetime | None = None) -> float:
    """Return the seconds remaining until Strava's next quarter-hour reset.

    Args:
        now: Current time; defaults to now in UTC. Injected for testing.

    Returns:
        Seconds until the next 15-minute boundary, always at least one.
    """
    moment = now or dt.datetime.now(tz=dt.UTC)
    seconds_into_window = (moment.minute * 60 + moment.second) % _RATE_LIMIT_WINDOW_S
    return max(_RATE_LIMIT_WINDOW_S - seconds_into_window, 1.0)
