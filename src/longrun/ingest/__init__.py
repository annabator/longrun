"""Ingestion adapters.

Each adapter translates one external representation - a Strava API payload,
a CSV row - into the domain models in :mod:`longrun.models`. Nothing
provider-shaped travels further into the application than this package.
"""
