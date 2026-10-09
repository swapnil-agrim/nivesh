"""Salted, letters-only reference for demat and folio identifiers (NFR-3).

Letters only: a hex digest would contain a 9+ digit run about 4 percent of the time, which the
redaction digit sweep would mask. 12 letters carry about 56 bits.
"""

import hashlib
import hmac

from nivesh_core.errors import SecretNotFound
from nivesh_core.secrets import get_secret

_LEN = 12
SALT_NAME = "FOLIO_SALT"


def hash_ref(value: str, salt: str) -> str:
    n = int.from_bytes(hmac.new(salt.encode(), value.encode(), hashlib.sha256).digest(), "big")
    out = []
    for _ in range(_LEN):
        n, r = divmod(n, 26)
        out.append(chr(97 + r))
    return "".join(out)


def salt_fingerprint(salt: str) -> str:
    """Letters-only fingerprint stored with the data to detect a changed salt."""
    return hash_ref("nivesh-salt-fingerprint", salt)


def get_salt() -> str:
    try:
        return get_secret(SALT_NAME)
    except SecretNotFound:
        raise SecretNotFound(
            f"secret {SALT_NAME!r} is not set; run `nivesh secrets set {SALT_NAME}`"
        ) from None
