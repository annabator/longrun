"""HTTP client for the Intervals.icu API.

Authentication is HTTP basic with the literal username `API_KEY` and the
athlete's key as the password, which is what Intervals.icu documents for
personal API keys.

The API has no published rate-limit headers, so the client is deliberately
conservative: it paces requests, backs off exponentially on 429 and 5xx, and
gives up with a clear message rather than hammering someone else's server.
"""

import datetime as dt
import gzip
import time
from collections.abc import Callable, Iterator, Sequence
from typing import Any

import httpx

from longrun.errors import AuthError, RateLimitError

API_BASE_URL = "https://intervals.icu/api/v1"

STREAM_TYPES: tuple[str, ...] = (
    "time",
    "heartrate",
    "velocity_smooth",
    "distance",
    "altitude",
    "cadence",
)
"""Stream types requested per activity.

Intervals.icu does not expose beat-to-beat R-R intervals through this API;
those come from the FIT adapter. See :mod:`longrun.ingest.fit`.
"""

BASIC_AUTH_USERNAME = "API_KEY"
"""Intervals.icu expects this literal username with a personal API key."""

_MAX_RETRIES = 4
_BACKOFF_BASE_S = 2.0
_REQUEST_TIMEOUT_S = 60.0
_PAGE_DAYS = 180
"""Window size when walking history; the API returns a date range, not pages."""

_FILE_TIMEOUT_S = 120.0
"""Activity files are far larger than a JSON payload, so they get longer."""

_GZIP_MAGIC = b"\x1f\x8b"
_FIT_MAGIC = b".FIT"

_CREDENTIALS_MESSAGE = (
    "Intervals.icu rejected the credentials. Check INTERVALS_ATHLETE_ID "
    "and INTERVALS_API_KEY in your .env; the key is the one on "
    "https://intervals.icu/settings."
)


class IntervalsClient:
    """Reads activities, streams and wellness from Intervals.icu."""

    def __init__(
        self,
        athlete_id: str,
        api_key: str,
        *,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_retries: int = _MAX_RETRIES,
    ) -> None:
        """Create a client.

        Args:
            athlete_id: The athlete id from the Intervals.icu settings page.
            api_key: The personal API key from the same page.
            http: HTTP client to use; one is created if omitted.
            sleep: Injected for testing, so backoff waits are instant.
            max_retries: Attempts per request before giving up.
        """
        self._athlete_id = athlete_id
        self._auth = (BASIC_AUTH_USERNAME, api_key)
        self._http = http or httpx.Client(timeout=_REQUEST_TIMEOUT_S)
        self._sleep = sleep
        self._max_retries = max_retries

    def iter_activities(
        self, oldest: dt.date, newest: dt.date | None = None
    ) -> Iterator[dict[str, Any]]:
        """Yield activity summaries between two dates, oldest first.

        The API takes a date range rather than a page number, so history is
        walked in windows to keep any single response a sensible size.

        Args:
            oldest: Earliest date to fetch, inclusive.
            newest: Latest date to fetch, inclusive; defaults to today.

        Yields:
            Raw activity payloads, exactly as Intervals.icu returned them.
        """
        end = newest or today_utc()
        for window_start, window_end in _date_windows(oldest, end, _PAGE_DAYS):
            payload = self._get(
                f"/athlete/{self._athlete_id}/activities",
                {"oldest": window_start.isoformat(), "newest": window_end.isoformat()},
            )
            if isinstance(payload, list):
                yield from payload

    def fetch_streams(
        self, activity_id: str, types: Sequence[str] = STREAM_TYPES
    ) -> dict[str, Any]:
        """Fetch the time-series streams for one activity.

        Args:
            activity_id: Intervals.icu identifier of the activity.
            types: Stream types to request.

        Returns:
            The raw streams payload, or an empty mapping for activities that
            carry none (manual entries, for instance).
        """
        payload = self._get(
            f"/activity/{activity_id}/streams",
            {"types": ",".join(types)},
            missing_ok=True,
        )
        if isinstance(payload, dict):
            return payload
        if isinstance(payload, list):
            # Some deployments answer with a list of stream objects rather
            # than an object keyed by type; normalise to the keyed shape.
            return {str(item.get("type")): item for item in payload if isinstance(item, dict)}
        return {}

    def fetch_wellness(
        self, oldest: dt.date, newest: dt.date | None = None
    ) -> list[dict[str, Any]]:
        """Fetch daily wellness records between two dates.

        Args:
            oldest: Earliest date to fetch, inclusive.
            newest: Latest date to fetch, inclusive; defaults to today.

        Returns:
            One raw record per day the athlete has data for.
        """
        end = newest or today_utc()
        records: list[dict[str, Any]] = []
        for window_start, window_end in _date_windows(oldest, end, _PAGE_DAYS):
            payload = self._get(
                f"/athlete/{self._athlete_id}/wellness",
                {"oldest": window_start.isoformat(), "newest": window_end.isoformat()},
            )
            if isinstance(payload, list):
                records.extend(item for item in payload if isinstance(item, dict))
        return records

    def fetch_activity_fit(self, activity_id: str) -> bytes | None:
        """Download an activity's FIT file, for the R-R the streams API omits.

        Two endpoints exist and they are not equivalent. `/file` returns the
        file as originally uploaded, gzip-compressed, and is the faithful one -
        but it is whatever the device sent, so it may be a GPX or TCX with no
        beat data in it at all. `/fit-file` returns a FIT that Intervals.icu
        generated from its own model, which is always a FIT but has been
        through a round trip.

        The original is tried first and the generated file is the fallback,
        so the most faithful source available is the one used.

        Args:
            activity_id: Intervals.icu identifier of the activity.

        Returns:
            Decompressed FIT bytes, or None if neither endpoint yields one.
        """
        for path in (f"/activity/{activity_id}/file", f"/activity/{activity_id}/fit-file"):
            payload = self._get_bytes(path)
            if payload is None:
                continue
            data = _maybe_gunzip(payload)
            if _looks_like_fit(data):
                return data
        return None

    def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        self._http.close()

    def _get_bytes(self, path: str) -> bytes | None:
        """GET a binary body, returning None when the file is not available.

        Args:
            path: Path relative to the API root.

        Returns:
            The raw response body, or None on 404 or an empty response.

        Raises:
            AuthError: If the credentials are rejected.
            RateLimitError: If retries are exhausted.
        """
        for attempt in range(self._max_retries):
            response = self._http.get(
                f"{API_BASE_URL}{path}",
                auth=self._auth,
                timeout=_FILE_TIMEOUT_S,
                follow_redirects=True,
            )

            if response.status_code in (httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN):
                raise AuthError(_CREDENTIALS_MESSAGE)
            if response.status_code == httpx.codes.NOT_FOUND:
                return None
            if response.status_code == httpx.codes.TOO_MANY_REQUESTS or (
                response.status_code >= httpx.codes.INTERNAL_SERVER_ERROR
            ):
                self._sleep(_backoff_seconds(response, attempt))
                continue

            response.raise_for_status()
            return response.content or None

        msg = (
            f"Gave up on {path} after {self._max_retries} attempts. Already-ingested "
            f"data has been saved; re-run to resume."
        )
        raise RateLimitError(msg)

    def _get(self, path: str, params: dict[str, Any], *, missing_ok: bool = False) -> Any:
        """Perform a GET against the API, retrying on rate limits and 5xx.

        Args:
            path: Path relative to the API root.
            params: Query parameters.
            missing_ok: Treat 404 as an empty result instead of an error.

        Returns:
            The decoded JSON body.

        Raises:
            AuthError: If the credentials are rejected.
            RateLimitError: If retries are exhausted.
            httpx.HTTPStatusError: For any other unsuccessful response.
        """
        for attempt in range(self._max_retries):
            response = self._http.get(
                f"{API_BASE_URL}{path}",
                params=params,
                auth=self._auth,
                timeout=_REQUEST_TIMEOUT_S,
            )

            if response.status_code in (httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN):
                raise AuthError(_CREDENTIALS_MESSAGE)
            if missing_ok and response.status_code == httpx.codes.NOT_FOUND:
                return {}
            if response.status_code == httpx.codes.TOO_MANY_REQUESTS or (
                response.status_code >= httpx.codes.INTERNAL_SERVER_ERROR
            ):
                self._sleep(_backoff_seconds(response, attempt))
                continue

            response.raise_for_status()
            return response.json()

        msg = (
            f"Gave up on {path} after {self._max_retries} attempts. Already-ingested "
            f"data has been saved; re-run to resume."
        )
        raise RateLimitError(msg)


def _maybe_gunzip(payload: bytes) -> bytes:
    """Decompress `payload` if it is gzipped, otherwise return it unchanged.

    The original-file endpoint serves gzip whether or not the response
    advertises it, so the magic number is checked rather than the header.
    """
    if not payload.startswith(_GZIP_MAGIC):
        return payload
    try:
        return gzip.decompress(payload)
    except (OSError, EOFError):
        return payload


def _looks_like_fit(payload: bytes) -> bool:
    """Return whether `payload` carries the FIT header marker.

    A FIT file writes the ASCII bytes `.FIT` at offset 8. Checking guards
    against handing a GPX, a TCX or an error page to the parser.
    """
    return payload[8:12] == _FIT_MAGIC


def today_utc() -> dt.date:
    """Return today's date in UTC.

    Everything longrun stores is UTC-normalised, so the local-timezone
    `date.today()` would put run boundaries an hour or two out of step with
    the timestamps it is compared against.
    """
    return dt.datetime.now(tz=dt.UTC).date()


def _backoff_seconds(response: httpx.Response, attempt: int) -> float:
    """Return how long to wait before retrying, honouring `Retry-After`."""
    retry_after = response.headers.get("Retry-After")
    if retry_after and retry_after.isdigit():
        return float(retry_after)
    return float(_BACKOFF_BASE_S * (2**attempt))


def _date_windows(
    oldest: dt.date, newest: dt.date, size_days: int
) -> Iterator[tuple[dt.date, dt.date]]:
    """Split a date range into consecutive inclusive windows.

    Args:
        oldest: Start of the range, inclusive.
        newest: End of the range, inclusive.
        size_days: Maximum days per window.

    Yields:
        `(start, end)` pairs covering the range in order.
    """
    start = oldest
    step = dt.timedelta(days=size_days - 1)
    while start <= newest:
        end = min(start + step, newest)
        yield start, end
        start = end + dt.timedelta(days=1)
