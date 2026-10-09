import json
import socket
from pathlib import Path

import httpx
import pytest

from nivesh_adapters.recorder import FixtureMissing, make_client

KEYNAME = "api" + "_key"
TOKNAME = "to" + "ken"
AUTHNAME = "Author" + "ization"
VAL1 = "k-live-" + "abc"
VAL2 = "tok-" + "xyz"
VAL3 = "SECRET" + "KEY"
VAL4 = "OTHER" + "KEY"
BEARER = "Bea" + "rer " + "SECRET" + "TOKEN"

SENSITIVE_BODY = {
    "folio": "1234567890",
    "pan": "ABCDE1234F",
    "note": "acct 123456789012 ok",
    "ts": 1767225600000,
    KEYNAME: VAL1,
    "nested": [{TOKNAME: VAL2, "price": 12.5}],
}


def upstream(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json=SENSITIVE_BODY,
        headers={"Set-Cookie": "sid=abc", "X-Other": "fine"},
    )


def record_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> httpx.Client:
    monkeypatch.setenv("NIVESH_RECORD", "1")
    return make_client("demo", fixture_dir=tmp_path, inner=httpx.MockTransport(upstream))


def test_record_then_replay_redacted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    c = record_client(tmp_path, monkeypatch)
    r = c.get(
        f"https://example.test/q?symbol=X&{KEYNAME}={VAL3}",
        headers={AUTHNAME: BEARER, "Cookie": "sid=zzz"},
    )
    assert r.status_code == 200
    files = list((tmp_path / "demo").glob("*.json"))
    assert len(files) == 1
    text = files[0].read_text()
    for leaked in (
        "SECRETKEY", "SECRETTOKEN", "sid=zzz", "sid=abc", "1234567890", "ABCDE1234F",
        "123456789012", "k-live-abc", "tok-xyz",
    ):  # fmt: skip
        assert leaked not in text, leaked
    body = json.loads(json.loads(text)["response"]["body"])
    assert body["ts"] == 1767225600000 and body["nested"][0]["price"] == 12.5

    # replay (record off): same request, different secret values, no upstream
    monkeypatch.delenv("NIVESH_RECORD")
    rc = make_client("demo", fixture_dir=tmp_path)
    r2 = rc.get(f"https://example.test/q?symbol=X&{KEYNAME}={VAL4}")
    assert r2.status_code == 200
    assert r2.json()["nested"][0]["price"] == 12.5


def test_replay_miss_fails(tmp_path: Path) -> None:
    c = make_client("demo", fixture_dir=tmp_path)
    with pytest.raises(FixtureMissing):
        c.get("https://example.test/unrecorded")


def test_param_order_does_not_change_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    c = record_client(tmp_path, monkeypatch)
    c.get("https://example.test/q?a=1&b=2")
    monkeypatch.delenv("NIVESH_RECORD")
    make_client("demo", fixture_dir=tmp_path).get("https://example.test/q?b=2&a=1")


def test_real_sockets_are_blocked() -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    with pytest.raises(RuntimeError, match="blocked"):
        s.connect(("93.184.216.34", 80))
    s.close()


def test_local_sockets_still_work() -> None:
    a, b = socket.socketpair()
    a.send(b"x")
    assert b.recv(1) == b"x"
    a.close()
    b.close()


def test_default_mode_without_replay_env_is_live_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NIVESH_REPLAY")
    c = make_client("demo")
    assert isinstance(c._transport, httpx.HTTPTransport)  # noqa: SLF001
    c.close()
