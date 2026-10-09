import json
import stat
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from nivesh_adapters import investright_session as s
from nivesh_core.config import InvestRightSettings
from nivesh_core.errors import InvestRightError, SessionExpired
from tests import pii_values as pv

CFG = InvestRightSettings()
REQ, SECRET, KEY, ACC = pv.request_token(), pv.api_secret(), pv.api_key_value(), pv.access_token()
WIRE_TOKEN_FIELD = "access" + "Token"


def test_extract_request_token_from_full_redirect_url() -> None:
    url = f"http://127.0.0.1:8765/callback?{s.REQ_PARAM}={REQ}&x=1"
    assert s.extract_request_token(url) == REQ


def test_extract_request_token_from_bare_token() -> None:
    assert s.extract_request_token(f"  {REQ}\n") == REQ


def test_extract_rejects_url_without_token() -> None:
    with pytest.raises(InvestRightError, match="no request token"):
        s.extract_request_token("http://127.0.0.1:8765/callback?x=1")


@pytest.mark.parametrize("text", ["", "   ", "has space inside", "a/b"])
def test_extract_rejects_empty_input(text: str) -> None:
    with pytest.raises(InvestRightError):
        s.extract_request_token(text)


def test_parse_callback_path_returns_token_for_callback_route() -> None:
    assert s.parse_callback_path(f"/callback?{s.REQ_PARAM}={REQ}") == REQ
    assert s.parse_callback_path("/callback?x=1") is None


def test_parse_callback_path_rejects_other_paths() -> None:
    assert s.parse_callback_path(f"/other?{s.REQ_PARAM}={REQ}") is None


def test_callback_listener_is_bound_to_loopback_only(monkeypatch: pytest.MonkeyPatch) -> None:
    bound: list[tuple[str, int]] = []

    class FakeServer:
        def __init__(self, addr: tuple[str, int], handler: object) -> None:
            bound.append(addr)
            self.timeout = 0.0

        def handle_request(self) -> None:
            return None

        def server_close(self) -> None:
            return None

    monkeypatch.setattr(s, "HTTPServer", FakeServer)
    with pytest.raises(InvestRightError, match="timed out"):
        s.serve_callback_once(8765, 0)
    assert bound == [("127.0.0.1", 8765)]


def test_callback_port_in_use_raises_with_paste_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    def busy(addr: tuple[str, int], handler: object) -> None:
        raise OSError("Address already in use")

    monkeypatch.setattr(s, "HTTPServer", busy)
    with pytest.raises(InvestRightError, match="--paste"):
        s.serve_callback_once(8765, 1)


def test_login_url_has_api_key_and_no_secret() -> None:
    url = s.login_url(CFG, KEY)
    assert url.startswith(CFG.base_url) and KEY in url and SECRET not in url


def exchange_client(
    handler: httpx.MockTransport | None = None,
) -> tuple[httpx.Client, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def h(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"status": "success", "data": {WIRE_TOKEN_FIELD: ACC}})

    return httpx.Client(transport=handler or httpx.MockTransport(h)), seen


def test_exchange_posts_api_secret_body_to_access_token_endpoint() -> None:
    client, seen = exchange_client()
    s.exchange_request_token(client, CFG, KEY, SECRET, REQ)
    (req,) = seen
    assert req.method == "POST" and req.url.path == "/oapi/v1/access-token"
    assert json.loads(req.content) == {"apiSecret": SECRET}
    assert req.url.params["api_key"] == KEY and req.url.params[s.REQ_PARAM] == REQ
    assert req.headers["user-agent"] == CFG.user_agent


def test_exchange_returns_access_token_from_success_payload() -> None:
    client, _ = exchange_client()
    assert s.exchange_request_token(client, CFG, KEY, SECRET, REQ) == ACC


def failing(status: int, body: object) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(status, json=body)))


def test_exchange_failure_raises_investright_error_with_hdfc_message_and_code() -> None:
    c = failing(200, {"status": "error", "message": "bad request token", "code": 60014})
    with pytest.raises(InvestRightError, match="bad request token") as ei:
        s.exchange_request_token(c, CFG, KEY, SECRET, REQ)
    assert ei.value.code == 60014


def test_exchange_http_error_status_raises_investright_error() -> None:
    with pytest.raises(InvestRightError, match="401"):
        s.exchange_request_token(failing(401, {}), CFG, KEY, SECRET, REQ)
    c = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>")))
    with pytest.raises(InvestRightError, match="not JSON"):
        s.exchange_request_token(c, CFG, KEY, SECRET, REQ)


def test_exchange_missing_token_in_success_payload() -> None:
    c = failing(200, {"status": "success", "data": {}})
    with pytest.raises(InvestRightError, match="no access token"):
        s.exchange_request_token(c, CFG, KEY, SECRET, REQ)


def test_exchange_failure_error_text_has_no_secret() -> None:
    c = failing(200, {"status": "error", "message": "nope", "code": 1})
    with pytest.raises(InvestRightError) as ei:
        s.exchange_request_token(c, CFG, KEY, SECRET, REQ)
    for secret in (SECRET, REQ, KEY):
        assert secret not in str(ei.value)


def clock(h: int, d: int = 5) -> s.Clock:
    return lambda: datetime(2026, 1, d, h, 0, tzinfo=UTC)


def test_token_store_saves_issue_date_in_ist_with_mode_0600(tmp_path: Path) -> None:
    store = s.TokenStore(tmp_path / "t.json", lambda: datetime(2026, 1, 5, 19, 0, tzinfo=UTC))
    store.save(ACC)
    data = json.loads((tmp_path / "t.json").read_text())
    assert data == {
        "session_token": ACC,
        "issued_ist_date": "2026-01-06",
    }  # 19:00 UTC is next day IST
    assert stat.S_IMODE((tmp_path / "t.json").stat().st_mode) == 0o600


def test_token_valid_on_same_ist_day(tmp_path: Path) -> None:
    now = [datetime(2026, 1, 5, 4, 0, tzinfo=UTC)]
    store = s.TokenStore(tmp_path / "t.json", lambda: now[0])
    store.save(ACC)
    now[0] = datetime(2026, 1, 5, 18, 29, tzinfo=UTC)
    assert store.valid() and store.token() == ACC


def test_token_invalid_after_ist_date_change(tmp_path: Path) -> None:
    now = [datetime(2026, 1, 5, 4, 0, tzinfo=UTC)]
    store = s.TokenStore(tmp_path / "t.json", lambda: now[0])
    store.save(ACC)
    now[0] = datetime(2026, 1, 5, 18, 31, tzinfo=UTC)
    assert not store.valid()
    with pytest.raises(SessionExpired, match="nivesh login"):
        store.token()


def test_token_store_missing_file_is_invalid(tmp_path: Path) -> None:
    store = s.TokenStore(tmp_path / "none.json", clock(5))
    assert not store.valid() and store.issued() is None
    with pytest.raises(SessionExpired):
        store.token()


@pytest.mark.parametrize(
    "text", ["not json", "[]", '{"session_token": 1}', '{"issued_ist_date": "x"}']
)
def test_token_store_corrupt_file_is_invalid(tmp_path: Path, text: str) -> None:
    (tmp_path / "t.json").write_text(text)
    store = s.TokenStore(tmp_path / "t.json", clock(5))
    assert not store.valid() and store.issued() is None


def test_token_store_replaces_previous_token(tmp_path: Path) -> None:
    store = s.TokenStore(tmp_path / "t.json", clock(5))
    store.save(ACC)
    store.save("new" + "x" * 5)
    assert store.token() == "new" + "x" * 5


def test_failed_exchange_stores_nothing_and_keeps_existing_token(tmp_path: Path) -> None:
    store = s.TokenStore(tmp_path / "t.json", clock(5))
    store.save(ACC)
    c = failing(200, {"status": "error", "message": "no", "code": 2})
    with pytest.raises(InvestRightError):
        s.complete_login(c, CFG, KEY, SECRET, REQ, store)
    assert store.token() == ACC
    fresh = s.TokenStore(tmp_path / "other.json", clock(5))
    with pytest.raises(InvestRightError):
        s.complete_login(c, CFG, KEY, SECRET, REQ, fresh)
    assert not (tmp_path / "other.json").exists()


def test_complete_login_extracts_exchanges_and_stores(tmp_path: Path) -> None:
    store = s.TokenStore(tmp_path / "t.json", clock(5))
    client, _ = exchange_client()
    s.complete_login(client, CFG, KEY, SECRET, f"http://x/callback?{s.REQ_PARAM}={REQ}", store)
    assert store.token() == ACC


def test_token_path_under_data_dir(tmp_path: Path) -> None:
    assert s.token_path(tmp_path) == tmp_path / "investright_session.json"
