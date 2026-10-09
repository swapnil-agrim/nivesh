class NiveshError(Exception):
    """Base class for expected, user-facing failures."""


class ConfigError(NiveshError):
    """Invalid or missing configuration; message names the offending field path."""


class SecretNotFound(NiveshError):
    """A referenced secret is in neither the environment nor the OS keychain."""
