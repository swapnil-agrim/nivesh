"""Dummy PII built by concatenation so no whole credential-shaped literal sits in the repo."""


def pan() -> str:
    return "ABCDE" + "1234" + "F"


def phone() -> str:
    return "+91" + " " + "98765" + " " + "43210"


def phone_spaced() -> str:
    return "98765" + " " + "43210"


def email() -> str:
    return "owner" + "@" + "example.com"


def api_key() -> str:
    return "sk" + "-" + "x" * 20


def bearer() -> str:
    return "Bear" + "er " + "abc" + "." + "def" + "123"


def folio() -> str:
    return "folio " + "12345" + "678901"


def account() -> str:
    return "a/c " + "98765" + "4321012"


def dp_id() -> str:
    return "IN" + "30" + "1234" + "5678" + "9012"


def address_line() -> str:
    return "Address: " + "12 Some Street, Mumbai 400001"


# Low-entropy dummies for the E2 tests (never realistic credentials).
def request_token() -> str:
    return "req" + "x" * 12


def api_secret() -> str:
    return "sec" + "y" * 12


def api_key_value() -> str:
    return "key" + "z" * 12


def access_token() -> str:
    return "acc" + "w" * 12


def salt() -> str:
    return "salt" * 4


def holder_name() -> str:
    return "Test" + " " + "Holder" + " " + "Name"


def dp_code() -> str:
    return "IN" + "30" + "0000"


def client_code() -> str:
    return "1234" + "5678"


def cas_password() -> str:
    return "pw" * 4


ALL = [pan, phone, phone_spaced, email, api_key, bearer, folio, account, dp_id, address_line]


def folio_number() -> str:
    return "12345" + "678901"


def person_address() -> str:
    return "12 Some Street, Mumbai 400001"


def mobile_digits() -> str:
    return "98765" + "43210"


# E4 dummies (never real credentials or contacts).
def edgar_contact() -> str:
    return "ops" + "@" + "example" + ".invalid"


def fred_key() -> str:
    return "fk" + "x" * 12


def fmp_key() -> str:
    return "mk" + "y" * 12


def cik_synthetic() -> str:
    return "0" + "0012" + "3456" + "7"  # zero-led 10-digit CIK shape; not a phone pattern
