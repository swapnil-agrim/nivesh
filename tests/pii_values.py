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


ALL = [pan, phone, phone_spaced, email, api_key, bearer, folio, account, dp_id, address_line]
