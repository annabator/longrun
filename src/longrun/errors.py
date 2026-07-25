"""Exception hierarchy for longrun.

A single base class lets the CLI turn any expected failure into a clean
message and non-zero exit code, while unexpected exceptions still surface
with a traceback.
"""


class LongrunError(Exception):
    """Base class for all errors raised deliberately by longrun."""


class ConfigError(LongrunError):
    """Required configuration is missing or malformed."""


class AuthError(LongrunError):
    """Authentication with an upstream provider failed."""


class RateLimitError(LongrunError):
    """An upstream provider's rate limit was exhausted."""


class IngestError(LongrunError):
    """Input data could not be parsed or ingested."""


class ReportError(LongrunError):
    """A report could not be generated."""
