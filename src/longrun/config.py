"""Configuration, loaded from the environment and an optional `.env` file.

Secrets are never defaulted and never logged: credentials are held as
:class:`pydantic.SecretStr` so an accidental repr cannot leak them.
"""

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from longrun.errors import ConfigError

_ENV_FILE = ".env"


class StravaSettings(BaseSettings):
    """Credentials for a Strava API application.

    Read from the environment (or `.env`) with a `STRAVA_` prefix, e.g.
    `STRAVA_CLIENT_ID`. See `.env.example` and README > Setup for how to
    register the application.
    """

    model_config = SettingsConfigDict(
        env_prefix="STRAVA_",
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    client_id: str | None = None
    client_secret: SecretStr | None = None
    refresh_token: SecretStr | None = None
    redirect_uri: str = "http://localhost/exchange_token"

    def require_credentials(self) -> tuple[str, str]:
        """Return the client id and secret, or raise if either is unset.

        Returns:
            A `(client_id, client_secret)` pair.

        Raises:
            ConfigError: If either credential is missing.
        """
        if not self.client_id or self.client_secret is None:
            msg = (
                "Strava credentials are not configured. Copy .env.example to .env "
                "and set STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET."
            )
            raise ConfigError(msg)
        return self.client_id, self.client_secret.get_secret_value()


class IntervalsSettings(BaseSettings):
    """Credentials for the Intervals.icu API.

    Read from the environment (or `.env`) with an `INTERVALS_` prefix. Both
    values come from <https://intervals.icu/settings>.
    """

    model_config = SettingsConfigDict(
        env_prefix="INTERVALS_",
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    athlete_id: str | None = None
    api_key: SecretStr | None = None

    def require_credentials(self) -> tuple[str, str]:
        """Return the athlete id and API key, or raise if either is unset.

        Returns:
            An `(athlete_id, api_key)` pair.

        Raises:
            ConfigError: If either credential is missing.
        """
        if not self.athlete_id or self.api_key is None:
            msg = (
                "Intervals.icu credentials are not configured. Copy .env.example to "
                ".env and set INTERVALS_ATHLETE_ID and INTERVALS_API_KEY, both from "
                "https://intervals.icu/settings."
            )
            raise ConfigError(msg)
        return self.athlete_id, self.api_key.get_secret_value()


class Settings(BaseSettings):
    """Local paths and provider settings for a longrun installation.

    Read from the environment (or `.env`) with a `LONGRUN_` prefix. Every
    default lives under `data_dir`, which is gitignored, so no configuration
    is required to run safely.
    """

    model_config = SettingsConfigDict(
        env_prefix="LONGRUN_",
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    data_dir: Path = Path("data")
    db_path: Path | None = None
    token_path: Path | None = None
    report_path: Path | None = None
    raw_dir: Path | None = None
    cycle_db_path: Path | None = None
    goals_file: Path | None = None
    intervals: IntervalsSettings = Field(default_factory=IntervalsSettings)
    strava: StravaSettings = Field(default_factory=StravaSettings)

    @property
    def database(self) -> Path:
        """Path to the SQLite database file."""
        return self.db_path or self.data_dir / "longrun.db"

    @property
    def tokens(self) -> Path:
        """Path to the Strava OAuth token cache."""
        return self.token_path or self.data_dir / "strava_tokens.json"

    @property
    def report(self) -> Path:
        """Default output path for the generated HTML dashboard."""
        return self.report_path or self.data_dir / "report.html"

    @property
    def raw(self) -> Path:
        """Folder scanned for `.fit` exports; gitignored with the rest of `data/`."""
        return self.raw_dir or self.data_dir / "raw"

    @property
    def goals_path(self) -> Path:
        """Where training goals are kept; configuration rather than a record."""
        return self.goals_file or self.data_dir / "goals.json"

    @property
    def cycle_database(self) -> Path:
        """Path to the cycle database.

        Kept in its own directory, and its own file, so that the main store
        can be copied or shared without this travelling with it.
        """
        return self.cycle_db_path or self.data_dir / "private" / "cycle.db"


def load_settings() -> Settings:
    """Load settings from the environment and `.env`.

    Returns:
        A fully populated :class:`Settings` instance.
    """
    return Settings()
