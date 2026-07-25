"""longrun: holistic, race-less endurance-training analytics.

Ingests Strava activities and manually logged health metrics into a local
SQLite store, then produces deterministic trend visualisations.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("longrun")
except PackageNotFoundError:  # pragma: no cover - only hit in a source checkout
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
