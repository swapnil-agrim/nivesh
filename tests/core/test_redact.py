import pytest

from nivesh_core.redact import MASK, is_sensitive_key, redact_json, redact_text
from tests import pii_values as pv


def test_text_patterns() -> None:
    out = redact_text(
        "Bearer abc.def PAN ABCDE1234F acct 123456789012 price 1234.56 sk-ant-abcdefgh12"
    )
    assert "abc.def" not in out and "ABCDE1234F" not in out and "123456789012" not in out
    assert "sk-ant" not in out and "1234.56" in out


def test_json_keys_and_numbers() -> None:
    out = redact_json(
        {"company": "Acme", "pan": "x", "token": "t", "ts": 1767225600000, "l": [{"folio": 5}]}
    )
    assert out["company"] == "Acme" and out["pan"] == MASK and out["token"] == MASK
    assert out["ts"] == 1767225600000 and out["l"][0]["folio"] == MASK


@pytest.mark.parametrize("make", pv.ALL, ids=lambda f: f.__name__)
def test_each_pii_kind_removed_inside_a_sentence(make) -> None:  # type: ignore[no-untyped-def]
    value = make()
    out = redact_text(f"note:\n{value}\nthanks")  # address masks to end of line
    # the identifying part must be gone (labels like "folio" may remain)
    ident = value.split()[-1]
    assert ident not in out and "thanks" in out


def test_blanket_digit_sweep_kept_at_all_edges() -> None:
    # recall over fidelity: unlabelled 9-18 digit runs are masked (recorded in ADR-0003)
    assert "1234567890123" not in redact_text("volume 1234567890123")
    assert (
        redact_text("price 2345.67 cap 1,234,567,890,123") == "price 2345.67 cap 1,234,567,890,123"
    )


@pytest.mark.parametrize("key", ["email", "phone", "Mobile", "home_address", "dp", "client_id"])
def test_more_sensitive_keys(key: str) -> None:
    assert is_sensitive_key(key)


def test_benign_text_not_masked() -> None:
    s = "account summary for the client was positive; address the risks"
    assert redact_text(s) == s


_FREE_TEXT = [
    ("call " + "98765" + "43210" + ".", "98765" + "43210"),
    ("A/c " + "1234" + "56789012" + ".", "1234" + "56789012"),
    ("Aadhaar " + "1234" + " 5678" + " 9012", "1234" + " 5678"),
    ("pan " + "abcde" + "1234" + "f", "abcde" + "1234"),
    ("ph " + "987" + "-654" + "-3210", "987" + "-654"),
    ("ph " + "022" + "-23456789", "23456789"),
    ("ph " + "98765" + "-43210" + ".", "98765" + "-43210"),
]


@pytest.mark.parametrize(("text", "needle"), _FREE_TEXT)
def test_free_text_variants_masked(text: str, needle: str) -> None:
    from nivesh_core.pii_scan import scan_text

    assert needle not in redact_text(text)
    assert scan_text(text)


def test_decimals_and_years_still_unmasked() -> None:
    s = "ratio 123456789.5 and 0.123456789 in 2024 2025 2026 pan-like Abcdefgh1234"
    assert redact_text(s) == s


@pytest.mark.parametrize(
    "text, gone",
    [
        ("id 1234  5678  9012 end", "5678"),
        ("call 98765 43210 now", "43210"),
        ("call 98765 43210 now", "43210"),
        ("pan ＡＢＣＤＥ１２３４Ｆ ok", "１２"),
        ("id １２３４ ５６７８ ９０１２", "５"),
    ],
)
def test_separator_and_width_variants_are_masked(text: str, gone: str) -> None:
    out = redact_text(text)
    assert gone not in out and MASK in out
