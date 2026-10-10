"""PII never reaches tool output, recorded fixtures or error text (NFR-3). Run nightly too."""

import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastmcp import Client

from nivesh_adapters.base import AdapterResult
from nivesh_adapters.recorder import make_client
from nivesh_agents.runtime import safe_error
from nivesh_core.pii_scan import scan_paths, scan_text
from nivesh_core.trace import Tracer
from nivesh_mcp.base import ReadOnlyServer
from tests import pii_values as pv
from tests.cli.test_market import funds, macro, net, newsnet, prices  # noqa: F401 - fixtures

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def pii_blob() -> str:
    return " | ".join(m() for m in (pv.pan, pv.phone, pv.email, pv.folio, pv.api_key, pv.bearer))


async def test_no_pii_in_tool_output_fixtures_or_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = pii_blob()
    assert scan_text(raw), "control: scanner must see the raw input"

    # (a) tool wrapper
    s = ReadOnlyServer("demo")

    @s.tool
    def leaky() -> AdapterResult:
        """Return PII-laden data."""
        return AdapterResult(data={"a": [{"b": raw}]}, source="t", as_of=NOW, fetched_at=NOW)

    async with Client(s.mcp) as c:
        res = await c.call_tool("leaky", {})
    out = tmp_path / "out"
    out.mkdir()
    (out / "tool.json").write_text(res.content[0].text)  # type: ignore[union-attr]
    assert json.loads((out / "tool.json").read_text())["source"] == "t"

    # (b) recorder
    monkeypatch.setenv("NIVESH_RECORD", "1")
    fx = tmp_path / "fx"
    client = make_client(
        "demo",
        fixture_dir=fx,
        inner=httpx.MockTransport(lambda r: httpx.Response(200, json={"note": raw})),
    )
    client.get("https://example.test/q")

    # (c) error text
    (out / "err.txt").write_text(safe_error(RuntimeError(raw)))

    # (d) trace output
    tr = Tracer(out, 1)
    tr.assistant_text(raw)
    tr.tool_call("t1", "demo", {"q": raw})
    tr.tool_result("t1", {"rows": [raw]})
    tr.engine_call("f", [raw], raw)
    tr.error(raw)

    assert scan_paths([out, fx]) == []


def test_cas_pipeline_leaves_no_pii_anywhere(
    cli_env: tuple[list[str], Path], fake_keyring: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    import keyring
    from typer.testing import CliRunner

    from nivesh_adapters import cas
    from nivesh_cli.main import app
    from nivesh_core.db import init_stores
    from nivesh_core.db.sqlite import open_sqlite
    from tests.cas_models import demat_data, pii_strings, rta_data

    args, data = cli_env
    raw = demat_data().model_dump_json() + rta_data().model_dump_json()
    assert scan_text(raw), "control: the raw models contain PII"

    keyring.set_password("nivesh", "CAS_PASSWORD", pv.cas_password())
    keyring.set_password("nivesh", "FOLIO_SALT", pv.salt())
    models = {b"demat": demat_data(), b"rta": rta_data(parse_warnings=["warn " + pv.email()])}
    monkeypatch.setattr(cas, "_read", lambda p, pw: models[p.read_bytes()])
    init_stores(data)
    (data / "inbox").mkdir()
    (data / "inbox" / "a.pdf").write_bytes(b"demat")
    (data / "inbox" / "b.pdf").write_bytes(b"rta")

    r = CliRunner().invoke(app, [*args, "ingest"])
    assert r.exit_code == 0, r.output

    conn = open_sqlite(data / "nivesh.sqlite")
    dump = "\n".join(conn.iterdump())
    reports = " ".join(row[0] for row in conn.execute("select report from ingest"))
    conn.close()
    files = "\n".join(
        p.read_text()
        for p in data.rglob("*")
        if p.is_file() and p.suffix in {".json", ".jsonl", ".txt"}
    )
    for label, text in {"db": dump, "report": reports, "cli": r.output, "files": files}.items():
        assert scan_text(text) == [], label
        for pii in pii_strings():
            assert pii not in text, label
    assert scan_paths([data / "inbox"]) == []


async def test_market_pipeline_leaves_no_pii_anywhere(
    cli_env: tuple[list[str], Path],
    fake_keyring: object,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """Fixtures -> master build and every ingest command -> DuckDB/SQLite dumps, CLI output and
    all 20 MCP tool payloads are scan-clean; the EDGAR contact and vendor keys appear nowhere."""
    import sqlite3

    import duckdb
    from typer.testing import CliRunner

    import nivesh_cli.market as cm
    import nivesh_mcp.common as common
    from nivesh_adapters.estimates import Estimates
    from nivesh_cli.main import app
    from nivesh_mcp.registry import SERVERS
    from tests.market_fx import fmp_calendar, fmp_estimates
    from tests.mcp.mkt_fx import NOW, call

    args, data = cli_env
    for fx in ("net", "prices", "funds", "macro", "newsnet"):  # fixtures from the CLI tests
        request.getfixturevalue(fx)
    monkeypatch.setenv("FMP_API_KEY", pv.fmp_key())
    est = httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200, json=fmp_calendar() if "earning_calendar" in r.url.path else fmp_estimates()
            )
        )
    )
    monkeypatch.setattr(cm, "_estimates", lambda s: Estimates(est))
    runner = CliRunner()
    outputs = []
    for cmd in (
        ["market", "prices", "RELIANCE", "--start", "2026-01-22", "--end", "2026-01-28"],
        ["market", "fundamentals", "AAPL"],
        ["market", "fundamentals", "RELIANCE"],
        ["market", "macro"],
        ["market", "estimates", "AAPL"],
        ["market", "news", "--security", "RELIANCE"],
    ):
        r = runner.invoke(app, [*args, *cmd])
        assert r.exit_code == 0, (cmd, r.output)
        outputs.append(r.output)

    sql = sqlite3.connect(data / "nivesh.sqlite")
    sqlite_dump = "\n".join(sql.iterdump())
    sql.close()
    duck = duckdb.connect(str(data / "nivesh.duckdb"), read_only=True)
    parts = []
    for (table,) in duck.execute("show tables").fetchall():
        if table == "cache_entry":  # public feed bodies, kept unmasked by design (EDGAR ids)
            continue
        cols = [c[0] for c in duck.execute(f"describe {table}").fetchall()]  # noqa: S608
        if table == "filing":  # url / doc_key hold public EDGAR accession numbers (follow-up D9)
            cols = [c for c in cols if c not in ("url", "doc_key")]
        parts.append(str(duck.execute(f"select {', '.join(cols)} from {table}").fetchall()))  # noqa: S608
    duck.close()

    monkeypatch.setenv("NIVESH_CONFIG_DIR", args[1])
    monkeypatch.setattr(common, "now", lambda: NOW)
    common._ready.clear()
    rel, aapl = {"security": "RELIANCE"}, {"security": "AAPL"}
    span = {"start": "2026-01-01", "end": "2026-02-01"}
    calls: dict[str, list[tuple[str, dict[str, Any]]]] = {
        "market": [
            ("get_prices", {**rel, **span}), ("get_index", {"name": "NIFTY 50", **span}),
            ("get_quote_eod", rel), ("get_corporate_actions", rel),
            ("resolve_security", {"query": "reliance"}), ("get_last_trading_day", {}),
        ],
        "fundamentals": [
            ("get_statements", aapl), ("get_ratios", aapl), ("get_peers", rel),
            ("get_estimates", aapl), ("get_shareholding", rel),
        ],
        "filings": [("list_filings", aapl), ("get_announcements", {})],
        "news": [
            ("get_news", {}), ("get_events_calendar", {"window_days": 366}),
            ("get_next_results_date", rel),
        ],
        "macro": [("get_series", {"series_id": "usdinr"}), ("get_flows_india", {}),
                  ("get_rates_snapshot", {})],
    }  # fmt: skip
    payloads = []
    for server, tools in calls.items():
        for tool, targs in tools:
            payloads.append(str(await call(server, tool, **targs)))
        assert {t for t, _ in tools} <= set(SERVERS[server].tool_names)
    listing = await call("filings", "list_filings", security="AAPL")
    fid = listing["data"]["filings"][0]["filing_id"]
    payloads.append(str(await call("filings", "get_filing_text", filing_id=fid, section="mdna")))

    secrets_ = (pv.edgar_contact(), pv.fred_key(), pv.fmp_key())
    texts = {"sqlite": sqlite_dump, "duckdb": "\n".join(parts), "cli": "\n".join(outputs),
             "mcp": "\n".join(payloads)}  # fmt: skip
    for needle in ("sec_edgar", "bse_xbrl", "nse_bhavcopy", "usdinr", "fmp", "example-news"):
        assert needle in texts["duckdb"], needle  # the pipeline really stored each data family
    assert '"close"' in texts["mcp"] or "'close'" in texts["mcp"]
    for label, text in texts.items():
        assert scan_text(text) == [], label
        assert not any(s in text for s in secrets_), label
    # the cache table keeps public bodies unmasked, but never the contact or vendor keys
    duck = duckdb.connect(str(data / "nivesh.duckdb"), read_only=True)
    blob = str(duck.execute("select payload, params_hash from cache_entry").fetchall())
    duck.close()
    assert not any(s in blob for s in secrets_)


async def test_us_holdings_pipeline_leaves_no_pii_anywhere(
    cli_env: tuple[list[str], Path], fake_keyring: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """US CSV + Alpaca fixture -> stores -> CLI output -> MCP payloads are scan-clean, and the
    dummy broker credentials appear in none of them."""
    import keyring
    from typer.testing import CliRunner

    import nivesh_cli.holdings as ch
    import nivesh_mcp.holdings as mh
    from nivesh_cli.main import app
    from nivesh_core.db import init_stores
    from nivesh_core.db.duck import open_duck
    from nivesh_core.db.sqlite import open_sqlite
    from nivesh_core.market_store import write_macro
    from nivesh_mcp.registry import SERVERS

    root = Path(__file__).resolve().parents[1] / "fixtures"
    args, data = cli_env
    init_stores(data)
    dummy_id, dummy_pw = pv.alpaca_key_value(), pv.alpaca_secret_value()
    keyring.set_password("nivesh", "ALPACA_KEY", dummy_id)
    keyring.set_password("nivesh", "ALPACA_SECRET", dummy_pw)

    def handler(req: httpx.Request) -> httpx.Response:
        name = "account" if req.url.path == "/v2/account" else "positions"
        return httpx.Response(200, text=(root / "alpaca" / f"{name}.json").read_text())

    monkeypatch.setattr(
        ch, "_alpaca_http", lambda: httpx.Client(transport=httpx.MockTransport(handler))
    )
    runner = CliRunner()
    conn = open_sqlite(data / "nivesh.sqlite")
    conn.execute(
        "INSERT INTO security (symbol, exchange, name, currency, market) "
        "VALUES ('MSFT', 'NASDAQ', 'Microsoft Corp', 'USD', 'US')"
    )
    conn.close()
    outputs = []
    for cmd in (
        ["import-csv", str(root / "csv" / "us_valid.csv"), "--market", "us"],
        ["sync-us"],
    ):
        r = runner.invoke(app, [*args, *cmd])
        assert r.exit_code == 0, (cmd, r.output)
        outputs.append(r.output)
    duck = open_duck(data / "nivesh.duckdb")
    write_macro(duck, "usdinr", [(datetime(2026, 1, 5).date(), Decimal("83.5"))], "fred")
    duck.close()

    monkeypatch.setenv("NIVESH_CONFIG_DIR", args[1])
    monkeypatch.setattr(mh, "_now", lambda: datetime(2026, 1, 5, 6, tzinfo=UTC))
    payloads = []
    for tool in ("combined_portfolio", "get_holdings", "get_lots"):
        async with Client(SERVERS["holdings"].mcp) as c:
            res = await c.call_tool(tool, {})
        payloads.append(res.content[0].text)  # type: ignore[union-attr]

    sql = open_sqlite(data / "nivesh.sqlite")
    # content digests are 64 hex chars and can hold a 9-digit run by chance; they are not PII
    dump = re.sub(r"[0-9a-f]{64}", "<digest>", "\n".join(sql.iterdump()))
    sql.close()
    files = "\n".join(
        p.read_text(errors="ignore")
        for p in data.rglob("*")
        if p.is_file() and p.suffix in {".json", ".jsonl", ".txt", ".log"}
    )
    texts = {"cli": "\n".join(outputs), "db": dump, "files": files, "mcp": "\n".join(payloads)}
    for label, text in texts.items():
        found = scan_text(text)
        assert found == [], (label, [text[f.start - 30 : f.end + 5] for f in found])
        for secret_value in (dummy_id, dummy_pw, "PA-SAMPLE", "acct-sample-id", "asset-sample"):
            assert secret_value not in text, (label, secret_value)
    raw = open(data / "nivesh.duckdb", "rb").read()
    assert dummy_id.encode() not in raw and dummy_pw.encode() not in raw


def test_mf_pipeline_leaves_no_pii_anywhere(
    cli_env: tuple[list[str], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """MF fixtures -> nav/meta/holdings/returns/overlap/doctor/discover -> SQLite and DuckDB
    dumps (cache included) and all CLI output are scan-clean; the holdings credential, supplied
    only as a reference, is sent as a header and appears nowhere else."""
    import json
    import sqlite3

    import duckdb
    from typer.testing import CliRunner

    import nivesh_cli.mf as cmf
    from nivesh_adapters.mf_data import MfHoldingsClient, MfMetaClient
    from nivesh_adapters.nav import AmfiNavAll, MfapiClient
    from nivesh_cli.main import app
    from nivesh_core.db import init_stores
    from nivesh_core.db.sqlite import open_sqlite
    from nivesh_core.security_master import build_master
    from tests.market_fx import mrow
    from tests.mf_fx import FX, G, Net

    args, data = cli_env
    cfg = Path(args[1]) / "nivesh.yaml"
    cfg.write_text(cfg.read_text().replace("holdings_source: fixture", "holdings_source: mfdata"))
    monkeypatch.setenv("MFDATA_API_KEY", pv.mf_source_ref_value())
    init_stores(data)
    sql = open_sqlite(data / "nivesh.sqlite")
    build_master(
        sql,
        [
            mrow(
                G, "AMFI", isin=G, asset_class="mf", amfi_code="100001", name="Example Fund Growth"
            ),
            mrow("INE000A01010", name="Example Alpha Industries"),
            mrow("INE111A01011", name="Example Beta Bank"),
        ],
        [],
    )
    sql.close()
    feed = Net(
        mfapi={"100001": json.loads((FX / "mfapi_scheme.json").read_text())},
        meta={"100001": json.loads((FX / "meta.json").read_text())},
        holdings={"100001": json.loads((FX / "holdings_12m.json").read_text())},
    )
    monkeypatch.setattr(cmf, "_mfapi", lambda: MfapiClient(feed.client()))
    monkeypatch.setattr(cmf, "_navall", lambda: AmfiNavAll(feed.client()))
    monkeypatch.setattr(cmf, "_meta_client", lambda: MfMetaClient(feed.client()))
    monkeypatch.setattr(
        cmf, "_holdings_client",
        lambda source, ref: MfHoldingsClient(feed.client(), source=source, key_ref=ref),
    )  # fmt: skip
    runner = CliRunner()
    outputs = []
    for cmd in (
        ["mf", "nav", "100001"], ["mf", "meta", "100001"], ["mf", "holdings", "100001"],
        ["mf", "returns", "100001"], ["mf", "doctor"],
        ["mf", "discover", "--category", "Large Cap"],
    ):  # fmt: skip
        r = runner.invoke(app, [*args, *cmd])
        assert r.exit_code in (0, 1), (cmd, r.output)  # doctor exits 1: nothing is held
        outputs.append(r.output)
    dummy_ref = pv.mf_source_ref_value()
    sent = [v for req in feed.requests for v in req.headers.values()]
    assert dummy_ref in sent, "control: the credential was used for the request"
    assert not any(dummy_ref in str(req.url) for req in feed.requests)

    s = sqlite3.connect(data / "nivesh.sqlite")
    sqlite_dump = "\n".join(s.iterdump())
    s.close()
    d = duckdb.connect(str(data / "nivesh.duckdb"), read_only=True)
    parts = []
    for (table,) in d.execute("show tables").fetchall():
        cols = [c[0] for c in d.execute(f"describe {table}").fetchall()]  # noqa: S608
        if table == "cache_entry":  # the params hash is hex digest noise, not data
            cols = [c for c in cols if c != "params_hash"]
        parts.append(str(d.execute(f"select {', '.join(cols)} from {table}").fetchall()))  # noqa: S608
    duck_dump = "\n".join(parts)
    d.close()
    files = "\n".join(
        p.read_text(errors="ignore")
        for p in data.rglob("*")
        if p.is_file() and p.suffix in {".json", ".jsonl", ".txt", ".log"}
    )
    texts = {"sqlite": sqlite_dump, "duckdb": duck_dump, "files": files, "cli": "\n".join(outputs)}
    for label, text in texts.items():
        found = scan_text(text)
        assert found == [], (label, [text[f.start - 30 : f.end + 5] for f in found])
        assert dummy_ref not in text, label
        assert pv.mf_param_name() not in text, label
    assert "added 6" in outputs[0] and "months stored 12" in outputs[2]


def test_mf_fixtures_and_config_have_no_key_shaped_literals() -> None:
    root = Path(__file__).resolve().parents[2]
    files = [*sorted((root / "tests" / "fixtures" / "mf").glob("*")), root / "tests" / "mf_fx.py"]
    files += [root / "docs" / "adr" / "0007-mutual-fund-intelligence.md"]
    texts = {f.name: f.read_text() for f in files}
    block = (root / "config" / "nivesh.yaml").read_text().split("\nmf:\n", 1)[1]
    texts["nivesh.yaml mf block"] = block
    shaped = re.compile(
        r"(?i)\b(api[_-]?key|apikey|token|secret|password)\b\s*[:=]\s*['\"]?[A-Za-z0-9+/_-]{6,}"
        r"|[A-Za-z0-9+/]{32,}"
    )
    for name, text in texts.items():
        assert shaped.search(text) is None, name
    assert "ref:MFDATA_API_KEY" in block and scan_paths([root / "tests" / "fixtures" / "mf"]) == []
