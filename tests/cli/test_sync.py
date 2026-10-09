import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

import nivesh_cli.holdings as h
from nivesh_adapters.investright_session import TokenStore, token_path
from nivesh_cli.main import app
from nivesh_core.db import init_stores
from nivesh_core.db.sqlite import open_sqlite
from nivesh_core.holdings import Holding
from nivesh_core.holdings_store import latest_holdings
from tests import pii_values as pv
from tests.holdings_fx import fixture_http

runner = CliRunner()
Env = tuple[list[str], Path]


@pytest.fixture
def ready(cli_env: Env, fake_keyring: object, monkeypatch: pytest.MonkeyPatch) -> Env:
    """Logged in today, credentials in the keychain, seeded security row, fixture transport."""
    import keyring

    args, data = cli_env
    init_stores(data)
    keyring.set_password("nivesh", "INVESTRIGHT_API_KEY", pv.api_key_value())
    TokenStore(token_path(data)).save(pv.access_token())
    c = open_sqlite(data / "nivesh.sqlite")
    c.execute(
        "insert into security (symbol, exchange, name, isin, currency, asset_class) "
        "values ('TWOCO', 'NSE', 'Two Co', 'INE111A01011', 'INR', 'equity')"
    )
    c.close()
    monkeypatch.setattr(h, "http_client", lambda: fixture_http()[0])
    return args, data


def stored(data: Path) -> dict[str, Holding]:
    c = open_sqlite(data / "nivesh.sqlite")
    try:
        return {x.symbol: x for x in latest_holdings(c)}
    finally:
        c.close()


def test_sync_fetches_normalises_and_saves_snapshots(ready: Env) -> None:
    args, data = ready
    r = runner.invoke(app, [*args, "sync"])
    assert r.exit_code == 0, r.output
    got = stored(data)
    assert set(got) == {"RELI", "TWOCO", "CLOSEONLY", "INE999Z01019"}
    assert got["TWOCO"].name == "Two Co" and got["INE999Z01019"].unresolved
    assert "4 holdings" in r.output and "1 unresolved" in r.output


def test_sync_without_valid_session_exits_1_with_login_hint(
    cli_env: Env, fake_keyring: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    import keyring

    args, data = cli_env
    init_stores(data)
    keyring.set_password("nivesh", "INVESTRIGHT_API_KEY", pv.api_key_value())
    r = runner.invoke(app, [*args, "sync"])
    assert r.exit_code == 1 and "nivesh login" in r.output
    yesterday = lambda: datetime.now(UTC) - timedelta(days=2)  # noqa: E731
    TokenStore(token_path(data), yesterday).save(pv.access_token())
    r = runner.invoke(app, [*args, "sync"])
    assert r.exit_code == 1 and "nivesh login" in r.output


def test_sync_session_expired_from_api_exits_1_with_static_ip_hint(
    ready: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, _ = ready
    monkeypatch.setattr(h, "http_client", lambda: fixture_http(status=401, only="error_60014")[0])
    r = runner.invoke(app, [*args, "sync"])
    assert r.exit_code == 1 and "static IP" in r.output
    assert pv.access_token() not in r.output


def test_sync_with_ltp_flag_sets_price_basis_ltp(ready: Env) -> None:
    args, data = ready
    r = runner.invoke(app, [*args, "sync", "--ltp"])
    assert r.exit_code == 0, r.output
    got = stored(data)
    assert got["RELI"].price_basis == "ltp" and got["TWOCO"].price_basis == "previous_close"


def test_sync_prints_counts_not_holding_values(ready: Env) -> None:
    args, _ = ready
    r = runner.invoke(app, [*args, "sync"])
    assert "RELI" not in r.output and "2450" not in r.output


def test_sync_missing_api_key_exits_1_with_hint(cli_env: Env, fake_keyring: object) -> None:
    args, data = cli_env
    init_stores(data)
    TokenStore(token_path(data)).save(pv.access_token())
    r = runner.invoke(app, [*args, "sync"])
    assert r.exit_code == 1 and "nivesh secrets set INVESTRIGHT_API_KEY" in r.output


def test_sync_twice_appends_snapshots(ready: Env) -> None:
    args, data = ready
    runner.invoke(app, [*args, "sync"])
    runner.invoke(app, [*args, "sync"])
    c = sqlite3.connect(data / "nivesh.sqlite")
    assert c.execute("select count(*) from holding_snapshot").fetchone() == (8,)
    assert len(stored(data)) == 4
