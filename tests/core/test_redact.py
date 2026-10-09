from nivesh_core.redact import MASK, redact_json, redact_text


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
