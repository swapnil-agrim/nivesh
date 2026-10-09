import keyring
import pytest

from nivesh_core.errors import SecretNotFound
from nivesh_core.holder_ref import get_salt, hash_ref, salt_fingerprint
from nivesh_core.redact import MASK, redact_json, redact_text
from tests import pii_values as pv

SALT = pv.salt()


def test_hash_ref_is_deterministic() -> None:
    assert hash_ref("x", SALT) == hash_ref("x", SALT)


def test_hash_ref_differs_by_value_and_by_salt() -> None:
    assert hash_ref("x", SALT) != hash_ref("y", SALT)
    assert hash_ref("x", SALT) != hash_ref("x", SALT + "z")


def test_hash_ref_is_twelve_lowercase_letters() -> None:
    assert all(
        len(r) == 12 and r.isalpha() and r.islower()
        for r in (hash_ref(str(i), SALT) for i in range(50))
    )


def test_hash_ref_survives_redaction_unchanged() -> None:
    refs = [hash_ref(f"in{i}/client{i}", SALT) for i in range(1000)]
    assert all(redact_text(r) == r for r in refs)
    assert all(redact_json({"holder_ref": r})["holder_ref"] == r for r in refs)
    assert MASK not in str(redact_json({"holder_ref": refs[0]}))


def test_hash_ref_does_not_contain_the_input() -> None:
    assert pv.client_code() not in hash_ref(pv.client_code(), SALT)


def test_salt_fingerprint_is_letters_and_differs_by_salt() -> None:
    assert salt_fingerprint(SALT).isalpha() and salt_fingerprint(SALT) != salt_fingerprint("other")


def test_get_salt_missing_raises_secret_not_found_with_hint(
    fake_keyring: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FOLIO_SALT", raising=False)
    with pytest.raises(SecretNotFound, match="nivesh secrets set FOLIO_SALT"):
        get_salt()


def test_get_salt_reads_env_then_keychain(
    fake_keyring: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FOLIO_SALT", raising=False)
    keyring.set_password("nivesh", "FOLIO_SALT", "fromkeychain")
    assert get_salt() == "fromkeychain"
    monkeypatch.setenv("FOLIO_SALT", "fromenv")
    assert get_salt() == "fromenv"
