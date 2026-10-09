"""Secrets are referenced by name in config ("ref:NAME"); values come from env or keychain."""

import os
import re

import keyring

from nivesh_core.errors import ConfigError, SecretNotFound

SERVICE = "nivesh"
REF_RE = re.compile(r"^ref:([A-Z][A-Z0-9_]{0,63})$")


def get_secret(name: str) -> str:
    value = os.environ.get(name)
    if value:
        return value
    try:
        value = keyring.get_password(SERVICE, name)
    except Exception:  # noqa: BLE001 - no usable keychain backend (headless CI) means "not found"
        value = None
    if value:
        return value
    raise SecretNotFound(f"secret {name!r} not found in environment or OS keychain")


class SecretRef:
    """A reference to a secret; repr shows the name only, never the value."""

    def __init__(self, ref: str) -> None:
        m = REF_RE.match(ref)
        if not m:
            raise ConfigError("secret reference must look like 'ref:UPPER_SNAKE_NAME'")
        self.name = m.group(1)

    def resolve(self) -> str:
        return get_secret(self.name)

    def __repr__(self) -> str:
        return f"SecretRef({self.name})"
