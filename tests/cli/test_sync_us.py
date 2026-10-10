import sqlite3
from pathlib import Path

import httpx
import keyring
import pytest
from typer.testing import CliRunner

import nivesh_cli.holdings as h
from nivesh_cli.main import app
from nivesh_core.db import init_stores
from nivesh_core.db.sqlite import open_sqlite
from nivesh_core.holdings_store import latest_holdings
from nivesh_core.pii_scan import scan_text
from tests import pii_values as pv

runner = CliRunner()
Env = tuple[list[str], Path]
FX = Path(__file__).resolve().parents[1] / "fixtures" / "alpaca"


def transport(account: str | None = None) -> httpx.Client:
    def handler(req: httpx.Request) -> httpx.Response:
        name = "account" if req.url.path == "/v2/account" else "positions"
        text = account if (account and name == "account") else (FX / f"{name}.json").read_text()
        return httpx.Response(200, text=text)

    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture
def ready(cli_env: Env, fake_keyring: object, monkeypatch: pytest.MonkeyPatch) -> Env:
    args, data = cli_env
    init_stores(data)
    keyring.set_password("nivesh", "ALPACA_KEY", pv.alpaca_key_value())
    keyring.set_password("nivesh", "ALPACA_SECRET", pv.alpaca_secret_value())
    monkeypatch.setattr(h, "_alpaca_http", lambda: transport())
    return args, data


def test_sync_us_stores_alpaca_holdings_and_reports_counts(ready: Env) -> None:
    args, data = ready
    r = runner.invoke(app, [*args, "sync-us"])
    assert r.exit_code == 0, r.output
    assert "synced 3 US holdings" in r.output and "skipped" in r.output
    c = open_sqlite(data / "nivesh.sqlite")
    got = {x.symbol: x for x in latest_holdings(c)}
    assert set(got) == {"AAPL", "SPYX", "AMEXCO"}
    assert got["AAPL"].currency == "USD" and got["AAPL"].source == "alpaca"
    assert c.execute("select kind from account").fetchone() == ("alpaca",)
    c.close()


def test_sync_us_twice_replaces_previous_snapshot(ready: Env) -> None:
    args, data = ready
    runner.invoke(app, [*args, "sync-us"])
    runner.invoke(app, [*args, "sync-us"])
    c = open_sqlite(data / "nivesh.sqlite")
    assert len(latest_holdings(c)) == 3
    c.close()


def test_sync_us_missing_secret_exit_1_with_set_hint(
    cli_env: Env, fake_keyring: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, _ = cli_env
    monkeypatch.setattr(h, "_alpaca_http", lambda: transport())
    r = runner.invoke(app, [*args, "sync-us"])
    assert r.exit_code == 1 and "nivesh secrets set ALPACA_KEY" in r.output


def test_sync_us_non_usd_account_refused(ready: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    args, data = ready
    monkeypatch.setattr(
        h, "_alpaca_http", lambda: transport('{"currency": "EUR", "status": "ACTIVE"}')
    )
    r = runner.invoke(app, [*args, "sync-us"])
    assert r.exit_code == 1 and "not USD" in r.output
    assert sqlite3.connect(data / "nivesh.sqlite").execute(
        "select count(*) from ingest"
    ).fetchone() == (0,)


def test_sync_us_output_has_no_secret_or_account_values(ready: Env) -> None:
    args, _ = ready
    r = runner.invoke(app, [*args, "sync-us"])
    for bad in (
        pv.alpaca_key_value(),
        pv.alpaca_secret_value(),
        "PA-SAMPLE",
        "acct-sample-id",
        "AAPL",
    ):
        assert bad not in r.output
    assert scan_text(r.output) == []


def test_sync_us_auth_failure_exits_1_without_echoing_secrets(
    ready: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, _ = ready
    deny = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(401, text="{}")))
    monkeypatch.setattr(h, "_alpaca_http", lambda: deny)
    r = runner.invoke(app, [*args, "sync-us"])
    assert r.exit_code == 1 and "credentials" in r.output
    assert pv.alpaca_secret_value() not in r.output
