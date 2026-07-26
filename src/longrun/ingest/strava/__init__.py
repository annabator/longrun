"""Strava ingestion: OAuth2, a rate-limit-aware API client, and mappers.

Secondary to Intervals.icu since v1.1, because Strava now gates the endpoints
longrun needs behind a paid subscription.
"""

from longrun.ingest.strava.auth import StravaAuthenticator, TokenBundle, TokenStore
from longrun.ingest.strava.client import StravaClient
from longrun.ingest.strava.service import StravaIngester

__all__ = [
    "StravaAuthenticator",
    "StravaClient",
    "StravaIngester",
    "TokenBundle",
    "TokenStore",
]
