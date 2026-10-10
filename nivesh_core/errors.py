class NiveshError(Exception):
    """Base class for expected, user-facing failures."""


class ConfigError(NiveshError):
    """Invalid or missing configuration; message names the offending field path."""


class SecretNotFound(NiveshError):
    """A referenced secret is in neither the environment nor the OS keychain."""


class SessionExpired(NiveshError):
    """The broker session is missing, expired, or rejected (login again; check the static IP)."""


class InvestRightError(NiveshError):
    """InvestRight returned an error; carries HDFC's message and numeric code when present."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class CalendarUnknown(NiveshError):
    """A trading-calendar question was asked for a year with no holiday data (never guessed)."""


class MarketDataError(NiveshError):
    """A market-data source failed or sent something unusable."""


class RateLimited(MarketDataError):
    """The source answered HTTP 429; back off instead of retrying in a loop."""


class SourceUnavailable(MarketDataError):
    """The source is down, blocked or returned an unexpected HTTP status."""
