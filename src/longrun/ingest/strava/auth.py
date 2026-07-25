"""Strava OAuth2 authorization-code flow and token lifecycle.

Strava issues short-lived access tokens (six hours) alongside a long-lived
refresh token. The first authorisation is interactive - the athlete approves
the application in a browser - after which longrun refreshes silently.

Tokens are cached in a JSON file with owner-only permissions, kept outside
version control by `.gitignore`.
"""

import datetime as dt
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
from pydantic import BaseModel, SecretStr

from longrun.config import StravaSettings
from longrun.errors import AuthError

AUTHORIZE_URL = "https://www.strava.com/oauth/authorize"
TOKEN_URL = "https://www.strava.com/oauth/token"

DEFAULT_SCOPE = "read,activity:read_all"
"""Read profile data plus all activities, including those marked private."""

_EXPIRY_SKEW = dt.timedelta(seconds=120)
"""Refresh slightly early, so a token cannot expire mid-request."""


class TokenBundle(BaseModel):
    """An access token, its refresh token, and its expiry."""

    access_token: SecretStr
    refresh_token: SecretStr
    expires_at: dt.datetime

    @property
    def expired(self) -> bool:
        """Whether the access token has expired, or is about to."""
        return dt.datetime.now(tz=dt.UTC) >= self.expires_at - _EXPIRY_SKEW

    @classmethod
    def from_response(cls, payload: dict[str, Any]) -> "TokenBundle":
        """Build a bundle from a Strava token endpoint response.

        Args:
            payload: Decoded JSON body from the token endpoint.

        Returns:
            The parsed token bundle.

        Raises:
            AuthError: If the response is missing required fields.
        """
        try:
            return cls(
                access_token=SecretStr(payload["access_token"]),
                refresh_token=SecretStr(payload["refresh_token"]),
                expires_at=dt.datetime.fromtimestamp(payload["expires_at"], tz=dt.UTC),
            )
        except (KeyError, TypeError) as exc:
            msg = f"Unexpected token response from Strava: missing {exc}"
            raise AuthError(msg) from exc


class TokenStore:
    """Reads and writes the cached token bundle on disk."""

    def __init__(self, path: Path) -> None:
        """Bind the store to a file location.

        Args:
            path: Where the token JSON is kept; created on first save.
        """
        self.path = path

    def load(self) -> TokenBundle | None:
        """Return the cached bundle, or None if nothing is cached.

        Returns:
            The stored token bundle, or None when no cache file exists.

        Raises:
            AuthError: If the cache file exists but cannot be parsed.
        """
        if not self.path.exists():
            return None
        try:
            return TokenBundle.model_validate_json(self.path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            msg = f"Could not read the token cache at {self.path}: {exc}"
            raise AuthError(msg) from exc

    def save(self, bundle: TokenBundle) -> None:
        """Persist a bundle with owner-only permissions.

        Args:
            bundle: Tokens to cache.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "access_token": bundle.access_token.get_secret_value(),
            "refresh_token": bundle.refresh_token.get_secret_value(),
            "expires_at": bundle.expires_at.isoformat(),
        }
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        self.path.chmod(0o600)


def build_authorize_url(client_id: str, redirect_uri: str, scope: str = DEFAULT_SCOPE) -> str:
    """Build the URL the athlete visits to approve the application.

    Args:
        client_id: Strava API application id.
        redirect_uri: Callback URL registered with the application.
        scope: Comma-separated Strava scopes.

    Returns:
        A fully formed authorisation URL.
    """
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "approval_prompt": "auto",
            "scope": scope,
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def exchange_code(
    client: httpx.Client, client_id: str, client_secret: str, code: str
) -> TokenBundle:
    """Exchange an authorization code for an initial token bundle.

    Args:
        client: HTTP client used for the request.
        client_id: Strava API application id.
        client_secret: Strava API application secret.
        code: The `code` parameter from the redirect after approval.

    Returns:
        The newly issued tokens.

    Raises:
        AuthError: If Strava rejects the exchange.
    """
    return _post_token(
        client,
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
        },
    )


def refresh_tokens(
    client: httpx.Client, client_id: str, client_secret: str, refresh_token: str
) -> TokenBundle:
    """Exchange a refresh token for a fresh access token.

    Args:
        client: HTTP client used for the request.
        client_id: Strava API application id.
        client_secret: Strava API application secret.
        refresh_token: The long-lived refresh token.

    Returns:
        The refreshed tokens; Strava may rotate the refresh token too.

    Raises:
        AuthError: If Strava rejects the refresh.
    """
    return _post_token(
        client,
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
    )


def _post_token(client: httpx.Client, data: dict[str, str]) -> TokenBundle:
    """POST to the token endpoint and parse the result."""
    try:
        response = client.post(TOKEN_URL, data=data, timeout=30.0)
    except httpx.HTTPError as exc:
        msg = f"Could not reach the Strava token endpoint: {exc}"
        raise AuthError(msg) from exc
    if response.status_code != httpx.codes.OK:
        msg = f"Strava rejected the token request ({response.status_code}): {response.text}"
        raise AuthError(msg)
    return TokenBundle.from_response(response.json())


class StravaAuthenticator:
    """Supplies a valid access token, refreshing and caching as required.

    Resolution order on first use: the cached token file, then a
    `STRAVA_REFRESH_TOKEN` from configuration. If neither is present the
    caller must run the interactive flow (`longrun auth strava`).
    """

    def __init__(self, settings: StravaSettings, token_store: TokenStore) -> None:
        """Bind the authenticator to credentials and a token cache.

        Args:
            settings: Strava application credentials.
            token_store: Where tokens are cached between runs.
        """
        self._settings = settings
        self._store = token_store
        self._bundle: TokenBundle | None = None

    def access_token(self, client: httpx.Client) -> str:
        """Return a currently valid access token.

        Refreshes and re-caches transparently when the cached token has
        expired.

        Args:
            client: HTTP client used for any refresh request.

        Returns:
            A valid bearer token.

        Raises:
            AuthError: If no credentials are available to obtain one.
        """
        bundle = self._bundle or self._store.load() or self._bootstrap_from_settings()
        if bundle.expired:
            client_id, client_secret = self._settings.require_credentials()
            bundle = refresh_tokens(
                client, client_id, client_secret, bundle.refresh_token.get_secret_value()
            )
            self._store.save(bundle)
        self._bundle = bundle
        return bundle.access_token.get_secret_value()

    def authorize(self, client: httpx.Client, code: str) -> TokenBundle:
        """Complete the interactive flow and cache the resulting tokens.

        Args:
            client: HTTP client used for the exchange.
            code: The `code` parameter from the redirect after approval.

        Returns:
            The newly issued tokens.
        """
        client_id, client_secret = self._settings.require_credentials()
        bundle = exchange_code(client, client_id, client_secret, code)
        self._store.save(bundle)
        self._bundle = bundle
        return bundle

    def _bootstrap_from_settings(self) -> TokenBundle:
        """Seed a bundle from a configured refresh token, forcing a refresh."""
        if self._settings.refresh_token is None:
            msg = (
                "No Strava tokens available. Run `longrun auth strava` to authorise, "
                "or set STRAVA_REFRESH_TOKEN in your .env."
            )
            raise AuthError(msg)
        return TokenBundle(
            access_token=SecretStr(""),
            refresh_token=self._settings.refresh_token,
            # In the past, so the first call always refreshes.
            expires_at=dt.datetime.now(tz=dt.UTC) - dt.timedelta(seconds=1),
        )
