import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pytest
from fastmcp import Client

import nivesh_mcp.holdings as mh
from nivesh_core.db import init_stores
from nivesh_core.db.duck import open_duck
from nivesh_core.db.sqlite import open_sqlite
from nivesh_core.holdings_store import save_ingest
from nivesh_core.market_models import PriceBar
from nivesh_core.market_store import upsert_bars, write_macro
from nivesh_core.pii_scan import scan_text
from nivesh_core.redact import is_sensitive_key
from nivesh_mcp.registry import SERVERS
from tests.holdings_fx import holding
from tests.us_fx import D, lot, usd_holding

Env = tuple[list[str], Path]
NOW = datetime(2026, 1, 5, 6, 0, tzinfo=UTC)  # IST date 2026-01-05, a Monday


@pytest.fixture
def env(cli_env: Env, fake_keyring: object, monkeypatch: pytest.MonkeyPatch) -> Path:
    args, data = cli_env
    monkeypatch.setenv("NIVESH_CONFIG_DIR", args[1])
    monkeypatch.setattr(mh, "_now", lambda: NOW)
    init_stores(data)
    return data


async def call(tool: str, **args: Any) -> dict[str, Any]:
    async with Client(SERVERS["holdings"].mcp) as c:
        res = await c.call_tool(tool, args)
    out: dict[str, Any] = json.loads(res.content[0].text)  # type: ignore[union-attr]
    return out


def seed(data: Path) -> None:
    c = open_sqlite(data / "nivesh.sqlite")
    day = date(2026, 1, 5)
    inr = holding(quantity=D(10), price=D(100), avg_cost=D(90), as_of=day)
    usd = usd_holding(quantity=D(10), price=D(10), avg_cost=D(8), as_of=day)
    save_ingest(c, kind="investright", source_label="x", digest=None, as_of=day,
                holdings=[inr], txns=[], holder_refs=[], warnings=[])  # fmt: skip
    save_ingest(c, kind="us_csv", source_label="y", digest="d", as_of=day,
                holdings=[usd], txns=[], holder_refs=[], warnings=[])  # fmt: skip
    c.close()


def rates(data: Path, *pairs: tuple[str, str]) -> None:
    d = open_duck(data / "nivesh.duckdb")
    write_macro(d, "usdinr", [(date.fromisoformat(a), Decimal(b)) for a, b in pairs], "fred")
    d.close()


def keys(obj: Any) -> list[str]:
    if isinstance(obj, dict):
        return [k for k in obj] + [x for v in obj.values() for x in keys(v)]
    if isinstance(obj, list):
        return [x for v in obj for x in keys(v)]
    return []


async def test_combined_portfolio_reports_rate_date_source_and_exposure(env: Path) -> None:
    seed(env)
    rates(env, ("2026-01-02", "80"), ("2026-01-05", "82"))
    out = await call("combined_portfolio")
    d = out["data"]
    assert d["fx"] == {
        "available": True, "fx_rate": 82, "fx_date": "2026-01-05",
        "fx_source": "fred:DEXINUS (RBI reference rate not wired)", "stale": False, "reason": None,
    }  # fmt: skip
    usd = next(r for r in d["holdings"] if r["currency"] == "USD")
    assert usd["value_usd"] == 100 and usd["value_inr"] == 8200 and usd["price"] == 10
    assert d["totals"]["value_inr"] == 1000 + 8200 and d["totals"]["value_usd"] == 100
    assert d["totals"]["unavailable_count"] == 0
    by = {e["currency"]: e for e in d["exposure"]}
    assert by["INR"]["value_inr"] == 1000 and by["USD"]["value_usd"] == 100
    assert abs(by["INR"]["exposure_pct"] + by["USD"]["exposure_pct"] - 100) < 1e-9
    assert by["USD"]["available"] is True


async def test_get_holdings_us_row_has_currency_value_usd_value_inr(env: Path) -> None:
    seed(env)
    rates(env, ("2026-01-05", "80"))
    out = await call("get_holdings", source="us_csv")
    (row,) = out["data"]["holdings"]
    assert (row["currency"], row["value_usd"], row["value_inr"], row["source"]) == (
        "USD", 100, 8000, "us_csv",
    )  # fmt: skip
    assert out["data"]["fx"]["fx_rate"] == 80 and row["weight"] == 1.0


async def test_missing_rate_reports_unavailable_not_zero(env: Path) -> None:
    seed(env)
    out = await call("combined_portfolio")
    d = out["data"]
    usd = next(r for r in d["holdings"] if r["currency"] == "USD")
    assert usd["value_inr"] is None and usd["weight"] is None and usd["value_usd"] == 100
    assert d["fx"]["available"] is False and d["fx"]["fx_rate"] is None
    assert "no USDINR observation" in d["fx"]["reason"]
    assert d["totals"]["value_inr"] == 1000 and d["totals"]["unavailable_count"] == 1
    assert any("excluded from INR totals" in n for n in d["notes"])
    (us,) = [e for e in d["exposure"] if e["currency"] == "USD"]
    assert us["exposure_pct"] is None and us["available"] is False


async def test_weekend_valuation_reports_friday_rate_date(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(env)
    rates(env, ("2026-01-02", "80"))
    monkeypatch.setattr(mh, "_now", lambda: datetime(2026, 1, 4, 6, 0, tzinfo=UTC))  # Sunday
    out = await call("combined_portfolio")
    assert out["data"]["fx"]["fx_date"] == "2026-01-02" and out["data"]["fx"]["stale"] is False


async def test_stale_rate_flagged(env: Path) -> None:
    seed(env)
    rates(env, ("2025-12-20", "80"))  # 16 days old: beyond the 10 day stale limit, inside the cap
    out = await call("combined_portfolio")
    assert out["data"]["fx"]["available"] is True and out["data"]["fx"]["stale"] is True


async def test_very_old_rate_is_unavailable(env: Path) -> None:
    seed(env)
    rates(env, ("2025-10-01", "80"))
    out = await call("combined_portfolio")
    assert out["data"]["fx"]["available"] is False


async def test_duckdb_locked_by_writer_reports_unavailable_not_crash(env: Path) -> None:
    seed(env)
    with duckdb.connect(str(env / "nivesh.duckdb")):  # a writer holds the file
        out = await call("combined_portfolio")
    fx = out["data"]["fx"]
    assert fx["available"] is False and "busy" in fx["reason"]
    usd = next(r for r in out["data"]["holdings"] if r["currency"] == "USD")
    assert usd["value_inr"] is None


async def test_usdinr_role_not_configured_reports_reason(
    env: Path, cli_env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(env)
    cfg = Path(cli_env[0][1]) / "nivesh.yaml"
    cfg.write_text("\n".join(x for x in cfg.read_text().splitlines() if "usdinr:" not in x))
    out = await call("combined_portfolio")
    assert out["data"]["fx"]["available"] is False
    assert "usdinr" in out["data"]["fx"]["reason"]


async def test_get_holdings_source_filter_accepts_us_csv_and_alpaca_and_error_lists_them(
    env: Path,
) -> None:
    seed(env)
    from fastmcp.exceptions import ToolError

    assert (await call("get_holdings", source="alpaca"))["data"]["holdings"] == []
    with pytest.raises(ToolError, match="us_csv"):
        await call("get_holdings", source="bogus")


async def test_payload_survives_redaction_with_real_values(env: Path) -> None:
    seed(env)
    rates(env, ("2026-01-05", "83.1234"))
    out = await call("combined_portfolio")
    assert "[REDACTED" not in json.dumps(out) and "REDACTED" not in json.dumps(out)
    usd = next(r for r in out["data"]["holdings"] if r["currency"] == "USD")
    assert usd["value_inr"] == float(Decimal(100) * Decimal("83.1234"))


async def test_payload_is_pii_scan_clean(env: Path) -> None:
    seed(env)
    rates(env, ("2026-01-05", "83.1234"))
    for tool in ("combined_portfolio", "get_holdings"):
        assert scan_text(json.dumps(await call(tool))) == []


async def test_payload_keys_pass_is_sensitive_key_check(env: Path) -> None:
    seed(env)
    rates(env, ("2026-01-05", "83"))
    for tool in ("combined_portfolio", "get_holdings"):
        bad = [k for k in keys(await call(tool)) if is_sensitive_key(k)]
        assert bad == []


async def test_config_usd_inr_does_not_affect_valuation(env: Path, cli_env: Env) -> None:
    seed(env)
    rates(env, ("2026-01-05", "80"))
    cfg = Path(cli_env[0][1]) / "nivesh.yaml"
    text = cfg.read_text()
    assert "usd_inr" in text
    import re

    cfg.write_text(re.sub(r"usd_inr:.*", "usd_inr: 1.0", text))
    out = await call("combined_portfolio")
    usd = next(r for r in out["data"]["holdings"] if r["currency"] == "USD")
    assert usd["value_inr"] == 8000


# ---- get_lots ----------------------------------------------------------------------------------
def seed_lots(data: Path, with_price: str | None = "110", price_day: str = "2026-01-05") -> None:
    c = open_sqlite(data / "nivesh.sqlite")
    day = date(2026, 1, 5)
    usd = usd_holding(quantity=D(10), price=D(100), avg_cost=D(100), as_of=day)
    lots = [
        lot(acquired_on=date(2025, 1, 5), quantity=D(6), cost_per_unit=D(100)),
        lot(acquired_on=date(2025, 7, 5), quantity=D(4), cost_per_unit=D(100)),
    ]
    save_ingest(c, kind="us_csv", source_label="y", digest="d", as_of=day, holdings=[usd],
                txns=[], holder_refs=[], warnings=[], lots=lots)  # fmt: skip
    sid = c.execute("select id from security where symbol = 'AAPL'").fetchone()[0]
    c.close()
    if with_price:
        d = open_duck(data / "nivesh.duckdb")
        upsert_bars(d, [PriceBar(security_id=sid, date=date.fromisoformat(price_day),
                                 close=Decimal(with_price), source="yahoo")])  # fmt: skip
        d.close()


async def test_get_lots_lists_lots_with_acquired_on_holding_days_and_xirr_usd(env: Path) -> None:
    seed_lots(env)
    out = await call("get_lots")
    d = out["data"]
    first = d["lots"][0]
    assert first["acquired_on"] == "2025-01-05" and first["holding_days"] == 365
    assert first["long_term"] is True and first["quantity"] == 6
    assert d["lots"][1]["holding_days"] == 184 and d["lots"][1]["long_term"] is False
    (sec,) = d["securities"]
    assert sec["price"] == 110 and sec["price_date"] == "2026-01-05"
    assert 0.1 < sec["xirr_usd"] < 0.3 and sec["reason_usd"] is None
    assert d["overall_xirr_usd"] == sec["xirr_usd"] and d["total"] == 2
    assert d["long_term_days"] == 365 and "informational" in d["long_term_basis"]


async def test_get_lots_paginates_with_next_cursor(env: Path) -> None:
    seed_lots(env)
    one = await call("get_lots", limit=1)
    assert len(one["data"]["lots"]) == 1 and one["next_cursor"] == 1
    two = await call("get_lots", limit=1, cursor=1)
    assert two["data"]["lots"][0]["acquired_on"] == "2025-07-05" and two["next_cursor"] is None


async def test_get_lots_filters_by_symbol_and_unknown_symbol_errors(env: Path) -> None:
    from fastmcp.exceptions import ToolError

    seed_lots(env)
    assert len((await call("get_lots", symbol="aapl"))["data"]["lots"]) == 2
    with pytest.raises(ToolError, match="no US lots"):
        await call("get_lots", symbol="ZZZZ")


async def test_get_lots_without_prices_reports_unavailable_reason(env: Path) -> None:
    seed_lots(env, with_price=None)
    d = (await call("get_lots"))["data"]
    (sec,) = d["securities"]
    assert sec["xirr_usd"] is None and sec["reason_usd"] == "no recent stored price"
    assert d["overall_xirr_usd"] is None and "AAPL" in d["overall_reason_usd"]
    assert sec["price"] is None


async def test_get_lots_old_price_unavailable(env: Path) -> None:
    seed_lots(env, price_day="2025-12-01")
    (sec,) = (await call("get_lots"))["data"]["securities"]
    assert sec["xirr_usd"] is None


async def test_get_lots_with_duckdb_busy_reports_reason_not_crash(env: Path) -> None:
    seed_lots(env)
    with duckdb.connect(str(env / "nivesh.duckdb")):
        d = (await call("get_lots"))["data"]
    assert d["securities"][0]["xirr_usd"] is None
    assert "busy" in d["securities"][0]["reason_usd"]


async def test_partial_lot_coverage_is_flagged(env: Path) -> None:
    c = open_sqlite(env / "nivesh.sqlite")
    day = date(2026, 1, 5)
    save_ingest(c, kind="us_csv", source_label="y", digest="d", as_of=day,
                holdings=[usd_holding(quantity=D(10), as_of=day)], txns=[], holder_refs=[],
                warnings=[], lots=[lot(quantity=D(4))])  # fmt: skip
    c.close()
    (sec,) = (await call("get_lots"))["data"]["securities"]
    assert sec["lots_cover_quantity"] is False and sec["holding_quantity"] == 10
    assert sec["note"] == "XIRR covers only dated lots"


async def test_get_lots_no_threshold_means_no_long_term_flag(env: Path, cli_env: Env) -> None:
    seed_lots(env)
    cfg = Path(cli_env[0][1]) / "nivesh.yaml"
    cfg.write_text(cfg.read_text().replace("us_long_term_days: 365", "us_long_term_days: null"))
    d = (await call("get_lots"))["data"]
    assert d["long_term_days"] is None and d["lots"][0]["long_term"] is None


def test_get_lots_docstring_has_no_write_words_and_no_tax_advice() -> None:
    from nivesh_mcp.base import _desc_write_words

    doc = SERVERS["holdings"].tool_docs["get_lots"]
    assert _desc_write_words(doc) == []
    lowered = doc.lower()
    assert "advice" not in lowered and "recommend" not in lowered and "should" not in lowered
    assert "informational" in lowered


def test_holdings_server_has_eight_tools_none_write() -> None:
    from nivesh_mcp.base import is_write_name

    tools = SERVERS["holdings"].tool_names
    assert len(tools) == 8 and "get_lots" in tools
    assert not [t for t in tools if is_write_name(t)]


async def test_get_lots_payload_keys_pass_is_sensitive_key_check(env: Path) -> None:
    seed_lots(env)
    out = await call("get_lots")
    assert [k for k in keys(out) if is_sensitive_key(k)] == []
    assert scan_text(json.dumps(out)) == []


async def test_get_lots_shows_both_xirr_usd_and_xirr_inr_and_reasons(env: Path) -> None:
    seed_lots(env)
    rates(env, ("2025-01-03", "80"), ("2025-07-04", "84"), ("2026-01-05", "90"))
    d = (await call("get_lots"))["data"]
    (sec,) = d["securities"]
    assert sec["xirr_usd"] is not None and sec["xirr_inr"] is not None
    assert sec["xirr_inr"] > sec["xirr_usd"] and sec["reason_inr"] is None
    assert d["overall_xirr_inr"] == sec["xirr_inr"] and d["overall_reason_inr"] is None


async def test_get_lots_inr_unavailable_without_rates_but_usd_shown(env: Path) -> None:
    seed_lots(env)
    d = (await call("get_lots"))["data"]
    (sec,) = d["securities"]
    assert sec["xirr_usd"] is not None and sec["xirr_inr"] is None
    assert "no USDINR rate" in sec["reason_inr"]
    assert d["overall_xirr_inr"] is None and d["overall_xirr_usd"] is not None


async def test_get_lots_inr_unavailable_when_usdinr_not_configured(env: Path, cli_env: Env) -> None:
    seed_lots(env)
    cfg = Path(cli_env[0][1]) / "nivesh.yaml"
    cfg.write_text("\n".join(x for x in cfg.read_text().splitlines() if "usdinr:" not in x))
    (sec,) = (await call("get_lots"))["data"]["securities"]
    assert sec["xirr_usd"] is not None and sec["xirr_inr"] is None
    assert "usdinr" in sec["reason_inr"]
