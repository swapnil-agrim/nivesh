"""`nivesh login | sync | ingest | import-csv` (E2). Registered on the main app by main.py."""

import hashlib
import webbrowser
from pathlib import Path
from typing import Annotated

import httpx
import typer

from nivesh_adapters.alpaca import AlpacaClient, normalise_positions
from nivesh_adapters.cas_ingest import ingest_inbox
from nivesh_adapters.csv_import import convert_preset, import_csv
from nivesh_adapters.csv_import_us import convert_us_preset, import_us_csv
from nivesh_adapters.investright import InvestRightClient
from nivesh_adapters.investright_normalise import ltp_map, normalise_holdings
from nivesh_adapters.investright_session import (
    TokenStore,
    complete_login,
    http_client,
    login_url,
    serve_callback_once,
    token_path,
)
from nivesh_cli.common import settings_of, user_errors
from nivesh_core.db import init_stores
from nivesh_core.db.sqlite import open_sqlite
from nivesh_core.errors import InvestRightError, NiveshError, SecretNotFound
from nivesh_core.holder_ref import get_salt
from nivesh_core.holdings_store import ingest_exists, save_ingest
from nivesh_core.paths import ensure_data_dir, private_umask
from nivesh_core.secrets import SecretRef
from nivesh_core.security_resolver import TableResolver, UsSymbolResolver
from nivesh_core.timeutil import ist_date, utcnow

holdings_app = typer.Typer()
CALLBACK_WAIT_SECONDS = 120


def secret_or_hint(ref: str) -> str:
    """Resolve a `ref:NAME` secret; a missing one tells the user the command to set it."""
    r = SecretRef(ref)
    try:
        return r.resolve()
    except SecretNotFound:
        raise NiveshError(
            f"secret {r.name!r} is not set; run `nivesh secrets set {r.name}`"
        ) from None


@holdings_app.command()
def login(
    ctx: typer.Context,
    paste: Annotated[bool, typer.Option(help="Paste the redirect URL or token instead.")] = False,
) -> None:
    """Daily InvestRight login: capture the request token, exchange it, store the session."""
    settings = settings_of(ctx)
    ir = settings.investright
    with user_errors():
        key = secret_or_hint(ir.app_key)
        app_secret_value = secret_or_hint(ir.api_secret)
        data_dir = ensure_data_dir(Path(settings.data_dir))
        store = TokenStore(token_path(data_dir))
        url = login_url(ir, key)
        typer.echo(f"Open this page and complete OTP + consent:\n{url}")
        pasted = ""
        if not paste:
            webbrowser.open(url)
            try:
                pasted = serve_callback_once(ir.redirect_port, CALLBACK_WAIT_SECONDS)
            except InvestRightError as exc:
                typer.echo(f"{exc}; paste the redirect URL or request token")
        if not pasted:
            pasted = typer.prompt("Redirect URL or request token", hide_input=True)
        complete_login(http_client(), ir, key, app_secret_value, pasted, store)
    typer.echo(f"logged in; session valid until the IST date changes ({store.issued()})")


@holdings_app.command()
def sync(
    ctx: typer.Context,
    ltp: Annotated[bool, typer.Option(help="Enrich prices with last traded price.")] = False,
) -> None:
    """Fetch InvestRight holdings and store a snapshot (needs today's login)."""
    settings = settings_of(ctx)
    ir = settings.investright
    data_dir = Path(settings.data_dir)
    with user_errors():
        init_stores(data_dir)
        bearer_value = TokenStore(token_path(data_dir)).token()
        client = InvestRightClient(
            ir.base_url, secret_or_hint(ir.app_key), bearer_value, ir.user_agent, http_client()
        )
        rows = client.holdings().data
        today = ist_date(utcnow())
        conn = open_sqlite(data_dir / "nivesh.sqlite")
        try:
            resolver = TableResolver(conn)
            prices = {}
            if ltp:
                first = normalise_holdings(rows, resolver, {}, today)
                pairs = [(x.exchange, x.symbol) for x in first if x.exchange != "ISIN"]
                prices = ltp_map(client.fetch_ltp(pairs).data) if pairs else {}
            found = normalise_holdings(rows, resolver, prices, today)
            report = save_ingest(
                conn, kind="investright", source_label="sync", digest=None, as_of=today,
                holdings=found, txns=[], holder_refs=[], warnings=[],
            )  # fmt: skip
        finally:
            conn.close()
    unresolved = sum(x.unresolved for x in found)
    typer.echo(f"synced {report.holdings} holdings ({unresolved} unresolved ISINs) as of {today}")


def _alpaca_http() -> httpx.Client | None:
    """The HTTP client for the US broker; None means the recorder's default (tests replace this)."""
    return None


@holdings_app.command("sync-us")
def sync_us(ctx: typer.Context) -> None:
    """Fetch Alpaca positions (read-only) and store them as USD holdings."""
    settings = settings_of(ctx)
    ub = settings.us_broker
    data_dir = Path(settings.data_dir)
    with user_errors():
        client = AlpacaClient(
            ub.base_url, secret_or_hint(ub.alpaca_key), secret_or_hint(ub.alpaca_secret),
            _alpaca_http(),
        )  # fmt: skip
        if client.account().data.get("currency") != "USD":
            raise NiveshError("the Alpaca account currency is not USD; only USD is supported")
        rows = client.positions().data
        today = ist_date(utcnow())
        init_stores(data_dir)
        conn = open_sqlite(data_dir / "nivesh.sqlite")
        try:
            found, warnings = normalise_positions(rows, UsSymbolResolver(conn), today)
            report = save_ingest(
                conn, kind="alpaca", source_label="sync", digest=None, as_of=today,
                holdings=found, txns=[], holder_refs=[], warnings=warnings,
            )  # fmt: skip
        finally:
            conn.close()
    typer.echo(f"synced {report.holdings} US holdings (Alpaca) as of {today}")
    for w in warnings:
        typer.echo(f"  warning: {w}")


@holdings_app.command()
def ingest(ctx: typer.Context) -> None:
    """Ingest CAS PDFs from <data_dir>/inbox (password and salt come from the keychain)."""
    data_dir = Path(ctx.obj[0].data_dir)
    with user_errors():
        init_stores(data_dir)
        inbox = data_dir / "inbox"
        if not inbox.exists():
            with private_umask():
                inbox.mkdir(mode=0o700)
            typer.echo(f"created {inbox}; drop CAS PDFs there and run `nivesh ingest` again")
            return
        cas_pass = secret_or_hint("ref:CAS_PASSWORD")
        salt = get_salt()
        conn = open_sqlite(data_dir / "nivesh.sqlite")
        try:
            result = ingest_inbox(conn, inbox, cas_pass, salt, TableResolver(conn))
        finally:
            conn.close()
    for rep in result.reports:
        typer.echo(
            f"statement {rep.source_label} ({rep.kind}) as of {rep.as_of}: "
            f"{rep.holdings} holdings, {rep.transactions} transactions"
        )
        if rep.kind == "cas_demat":
            for ref in rep.holder_refs:
                typer.echo(
                    f"  holder_ref {ref} (set investright.demat_ref to scope reconciliation)"
                )
        for w in rep.warnings:
            typer.echo(f"  warning: {w}")
    if result.skipped:
        typer.echo(f"skipped {len(result.skipped)} already ingested file(s)")
    for err in result.errors:
        typer.echo(f"error: {err}", err=True)
    if result.errors:
        raise typer.Exit(1)


@holdings_app.command("import-csv")
def import_csv_cmd(
    ctx: typer.Context,
    path: Annotated[Path, typer.Argument(help="CSV in the holdings template layout.")],
    preset: Annotated[
        str | None,
        typer.Option(help="in: zerodha | groww | upstox; us: alpaca | robinhood export."),
    ] = None,
    label: Annotated[
        str | None, typer.Option(help="Account label (required with --preset).")
    ] = None,
    market: Annotated[str, typer.Option(help="in (default) or us (USD holdings).")] = "in",
) -> None:
    """Import holdings for accounts the other sources do not cover; all-or-nothing."""
    data_dir = Path(settings_of(ctx).data_dir)
    with user_errors():
        if market not in ("in", "us"):
            raise NiveshError("--market must be in or us")
        if not path.is_file():
            raise NiveshError("file not found")
        if market == "us":
            _import_us(data_dir, path, preset, label)
            return
        if preset and not label:
            raise NiveshError("--label is required with --preset")
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        text = raw.decode("utf-8", errors="replace")
        if preset and label:
            text = convert_preset(text, preset, label)
        init_stores(data_dir)
        conn = open_sqlite(data_dir / "nivesh.sqlite")
        try:
            if ingest_exists(conn, "csv", digest):
                typer.echo("already imported (same file content); nothing to do")
                return
            today = ist_date(utcnow())
            result = import_csv(text, TableResolver(conn), today)
            if result.errors:
                for err in result.errors:
                    typer.echo(str(err), err=True)
                raise NiveshError(f"{len(result.errors)} invalid row(s); nothing was imported")
            save_ingest(
                conn, kind="csv", source_label=digest[:8], digest=digest, as_of=today,
                holdings=result.holdings, txns=[], holder_refs=[], warnings=[],
            )  # fmt: skip
        finally:
            conn.close()
    accounts = len({h.source_label for h in result.holdings})
    typer.echo(f"imported {len(result.holdings)} holdings into {accounts} account(s)")


def _import_us(data_dir: Path, path: Path, preset: str | None, label: str | None) -> None:
    if preset and not label:
        raise NiveshError("--label is required with --preset")
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    text = raw.decode("utf-8", errors="replace")
    if preset and label:
        text = convert_us_preset(text, preset, label)
    init_stores(data_dir)
    conn = open_sqlite(data_dir / "nivesh.sqlite")
    try:
        if ingest_exists(conn, "us_csv", digest):
            typer.echo("already imported (same file content); nothing to do")
            return
        today = ist_date(utcnow())
        result = import_us_csv(text, UsSymbolResolver(conn), today)
        if result.errors:
            for err in result.errors:
                typer.echo(str(err), err=True)
            raise NiveshError(f"{len(result.errors)} invalid row(s); nothing was imported")
        save_ingest(
            conn, kind="us_csv", source_label=digest[:8], digest=digest, as_of=today,
            holdings=result.holdings, txns=[], holder_refs=[], warnings=[], lots=result.lots,
        )  # fmt: skip
    finally:
        conn.close()
    accounts = len({h.source_label for h in result.holdings})
    typer.echo(
        f"imported {len(result.holdings)} holdings and {len(result.lots)} lots "
        f"into {accounts} account(s)"
    )
