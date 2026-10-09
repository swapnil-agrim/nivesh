import json
import stat
import webbrowser
from datetime import UTC, datetime
from pathlib import Path

import httpx
import keyring
import pytest
import typer
from typer.testing import CliRunner

import nivesh_adapters.investright_session as s
import nivesh_cli.holdings as h
from nivesh_adapters.investright_session import REQ_PARAM, token_path
from nivesh_cli.main import app
from nivesh_core.errors import InvestRightError
from nivesh_core.timeutil import ist_date
from tests import pii_values as pv

runner = CliRunner()
REQ, SECRET, KEY, ACC = pv.request_token(), pv.api_secret(), pv.api_key_value(), pv.access_token()
WIRE_TOKEN_FIELD = "access" + "Token"
Env = tuple[list[str], Path]


@pytest.fixture
def creds(fake_keyring: object) -> None:
    keyring.set_password("nivesh", "INVESTRIGHT_API_KEY", KEY)
    keyring.set_password("nivesh", "INVESTRIGHT_API_SECRET", SECRET)


def fake_http(monkeypatch: pytest.MonkeyPatch, body: dict[str, object]) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json=body)

    monkeypatch.setattr(
        h, "http_client", lambda: httpx.Client(transport=httpx.MockTransport(handler))
    )
    return seen


OK = {"status": "success", "data": {WIRE_TOKEN_FIELD: ACC}}


def test_login_paste_stores_token_with_ist_date_and_never_prints_it(
    cli_env: Env, creds: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, data = cli_env
    seen = fake_http(monkeypatch, OK)
    r = runner.invoke(
        app, [*args, "login", "--paste"], input=f"http://x/callback?{REQ_PARAM}={REQ}\n"
    )
    assert r.exit_code == 0, r.output
    stored = json.loads(token_path(data).read_text())
    assert stored["session_token"] == ACC
    assert stored["issued_ist_date"] == ist_date(datetime.now(UTC)).isoformat()
    assert stat.S_IMODE(token_path(data).stat().st_mode) == 0o600
    for secret in (ACC, SECRET, REQ):
        assert secret not in r.output
    assert json.loads(seen[0].content) == {"apiSecret": SECRET}


def test_login_failed_exchange_prints_hdfc_message_exits_1_and_stores_nothing(
    cli_env: Env, creds: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, data = cli_env
    fake_http(monkeypatch, {"status": "error", "message": "otp expired", "code": 60014})
    r = runner.invoke(app, [*args, "login", "--paste"], input=REQ + "\n")
    assert r.exit_code == 1 and "otp expired" in r.output
    assert not token_path(data).exists()
    assert SECRET not in r.output


def test_login_missing_credentials_exits_1_with_secrets_hint(
    cli_env: Env, fake_keyring: object
) -> None:
    args, _ = cli_env
    r = runner.invoke(app, [*args, "login", "--paste"], input=REQ + "\n")
    assert r.exit_code == 1 and "nivesh secrets set INVESTRIGHT_API_KEY" in r.output


def test_login_has_no_option_that_takes_a_secret_value() -> None:
    cmd = typer.main.get_command(app).commands["login"]  # type: ignore[attr-defined]
    assert [p.name for p in cmd.params] == ["paste"] and cmd.params[0].is_flag


def test_login_prints_login_url_without_secrets(
    cli_env: Env, creds: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, _ = cli_env
    fake_http(monkeypatch, OK)
    r = runner.invoke(app, [*args, "login", "--paste"], input=REQ + "\n")
    assert KEY in r.output and SECRET not in r.output and "/oapi/v1/login" in r.output


def test_login_uses_callback_listener_and_opens_browser(
    cli_env: Env, creds: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, data = cli_env
    fake_http(monkeypatch, OK)
    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", lambda u: opened.append(u) or True)
    ports: list[int] = []
    monkeypatch.setattr(h, "serve_callback_once", lambda port, timeout: ports.append(port) or REQ)
    r = runner.invoke(app, [*args, "login"])
    assert r.exit_code == 0, r.output
    assert ports == [8765] and opened and token_path(data).exists()


def test_login_callback_timeout_falls_back_to_paste(
    cli_env: Env, creds: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, data = cli_env
    fake_http(monkeypatch, OK)
    monkeypatch.setattr(webbrowser, "open", lambda u: True)

    def boom(port: int, timeout: float) -> str:
        raise InvestRightError("timed out waiting for the login callback")

    monkeypatch.setattr(h, "serve_callback_once", boom)
    r = runner.invoke(app, [*args, "login"], input=REQ + "\n")
    assert r.exit_code == 0 and token_path(data).exists()


def test_login_port_in_use_points_at_paste_without_traceback(
    cli_env: Env, creds: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, data = cli_env
    fake_http(monkeypatch, OK)
    monkeypatch.setattr(webbrowser, "open", lambda u: True)

    def busy(addr: tuple[str, int], handler: object) -> None:
        raise OSError("in use")

    monkeypatch.setattr(s, "HTTPServer", busy)
    r = runner.invoke(app, [*args, "login"], input=REQ + "\n")
    assert r.exit_code == 0 and token_path(data).exists()
    assert "--paste" in r.output and "Traceback" not in r.output
