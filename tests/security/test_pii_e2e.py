"""PII never reaches tool output, recorded fixtures or error text (NFR-3). Run nightly too."""

import json
from datetime import UTC, datetime
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
