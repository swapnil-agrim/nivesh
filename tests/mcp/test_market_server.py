from datetime import date, timedelta
from decimal import Decimal

import duckdb
import pytest
from fastmcp.exceptions import ToolError

import nivesh_mcp.common as common
from nivesh_core.market_models import CorpAction, PriceBar
from nivesh_core.market_store import upsert_bars, write_prices
from nivesh_core.pii_scan import scan_text
from nivesh_mcp.base import is_write_name
from nivesh_mcp.registry import SERVERS
from tests.mcp.mkt_fx import Mkt, call

D = Decimal
TOOLS = ["get_prices", "get_index", "get_quote_eod", "get_corporate_actions", "resolve_security",
         "get_last_trading_day"]  # fmt: skip
START = date(2024, 1, 1)


def bar(sid: int, day: date, close: str, source: str = "nse_bhavcopy", **kw: object) -> PriceBar:
    c = D(close)
    return PriceBar(security_id=sid, date=day, open=c, high=c + 1, low=c - 1, close=c,
                    volume=1234567, adj_close=c, source=source, **kw)  # type: ignore[arg-type]  # fmt: skip


def series(sid: int, n: int, start: date = START, source: str = "nse_bhavcopy") -> list[PriceBar]:
    return [bar(sid, start + timedelta(days=i), str(100 + i), source) for i in range(n)]


def test_market_tools_exact_set() -> None:
    assert SERVERS["market"].tool_names == TOOLS
    assert not [t for t in TOOLS if is_write_name(t)]


async def test_get_prices_returns_ohlcv_adj_close_source_flag_as_of(mkt: Mkt) -> None:
    sid = mkt.ids["RELIANCE"]
    with mkt.duck() as c:
        upsert_bars(c, [bar(sid, date(2026, 1, 2), "101.5", flag="ok"),
                        bar(sid, date(2026, 1, 1), "100.25")])  # fmt: skip
    out = await call("market", "get_prices", security="RELIANCE", start="2026-01-01",
                     end="2026-01-31")  # fmt: skip
    assert out["source"] and out["as_of"] == "2026-01-02" and out["stale"] is False
    d = out["data"]
    assert d["symbol"] == "RELIANCE" and d["currency"] == "INR" and d["total"] == 2
    first, last = d["bars"]
    assert first["date"] == "2026-01-01" and last["close"] == 101.5 and last["flag"] == "ok"
    assert last["volume"] == 1234567 and last["source"] == "nse_bhavcopy"
    assert {"open", "high", "low", "adj_close"} <= set(last)


async def test_get_prices_paginates_and_never_exceeds_500_rows(mkt: Mkt) -> None:
    sid = mkt.ids["RELIANCE"]
    with mkt.duck() as c:
        upsert_bars(c, series(sid, 600))
    got: list[str] = []
    cursor = 0
    while True:
        out = await call("market", "get_prices", security="RELIANCE", start="2024-01-01",
                         end="2030-01-01", limit=100000, cursor=cursor)  # fmt: skip
        assert len(out["data"]["bars"]) <= 500
        got += [b["date"] for b in out["data"]["bars"]]
        if out.get("next_cursor") is None:
            break
        cursor = out["next_cursor"]
    assert len(got) == 600 and got == sorted(set(got))


async def test_get_prices_start_after_end_is_a_tool_error(mkt: Mkt) -> None:
    with pytest.raises(ToolError, match="after end"):
        await call(
            "market", "get_prices", security="RELIANCE", start="2026-02-01", end="2026-01-01"
        )
    with pytest.raises(ToolError, match="start must be an ISO date"):
        await call("market", "get_prices", security="RELIANCE", start="x", end="2026-01-01")
    with pytest.raises(ToolError, match="cursor"):
        await call("market", "get_prices", security="RELIANCE", start="2026-01-01",
                   end="2026-01-02", cursor=-1)  # fmt: skip


async def test_get_prices_unadjusted_option_and_empty_range(mkt: Mkt) -> None:
    sid = mkt.ids["RELIANCE"]
    with mkt.duck() as c:
        upsert_bars(c, [PriceBar(security_id=sid, date=date(2026, 1, 2), close=D("100"),
                                 adj_close=D("50"), source="nse_bhavcopy")])  # fmt: skip
    out = await call("market", "get_prices", security="RELIANCE", start="2026-01-01",
                     end="2026-01-05")  # fmt: skip
    assert out["data"]["bars"][0]["adj_close"] == 50.0 and out["data"]["bars"][0]["close"] == 100.0
    empty = await call("market", "get_prices", security="TCS", start="2026-01-01", end="2026-01-05")
    assert empty["data"]["bars"] == [] and empty["data"]["total"] == 0


async def test_get_index_by_name_and_unknown_name_lists_known_names(mkt: Mkt) -> None:
    sid = mkt.ids["NIFTY 50"]
    with mkt.duck() as c:
        upsert_bars(c, [bar(sid, date(2026, 1, 2), "24000.5", "nse_indices")])
    out = await call("market", "get_index", name="nifty 50", start="2026-01-01", end="2026-01-05")
    assert out["data"]["name"] == "NIFTY 50" and out["data"]["bars"][0]["close"] == 24000.5
    with pytest.raises(ToolError, match="NIFTY 50.*NIFTY BANK"):
        await call(
            "market", "get_index", name="Nifty Nonsense", start="2026-01-01", end="2026-01-05"
        )


async def test_get_quote_eod_returns_latest_bar_and_previous_close(mkt: Mkt) -> None:
    sid = mkt.ids["RELIANCE"]
    with mkt.duck() as c:
        upsert_bars(c, [bar(sid, date(2026, 1, 1), "100"), bar(sid, date(2026, 1, 2), "110")])
    out = await call("market", "get_quote_eod", security="RELIANCE")
    d = out["data"]
    assert d["date"] == "2026-01-02" and d["close"] == 110.0 and d["previous_close"] == 100.0
    assert d["change_pct"] == pytest.approx(10.0) and out["as_of"] == "2026-01-02"
    none = await call("market", "get_quote_eod", security="TCS")
    assert none["data"]["close"] is None and none["data"]["reason"]


async def test_get_quote_eod_change_is_split_adjusted_across_ex_date(mkt: Mkt) -> None:
    sid = mkt.ids["RELIANCE"]
    with mkt.duck() as c:
        write_prices(c, [bar(sid, date(2026, 1, 1), "1000"), bar(sid, date(2026, 1, 2), "110")], [
            CorpAction(security_id=sid, ex_date=date(2026, 1, 2), kind="split", ratio=D("10"),
                       source="nse_corp_actions"),
        ])  # fmt: skip
    d = (await call("market", "get_quote_eod", security="RELIANCE"))["data"]
    assert d["change_pct"] == pytest.approx(10.0) and d["close"] == 110.0
    assert d["previous_close"] == pytest.approx(100.0)
    assert d["previous_close_basis"] == "split_adjusted"


async def test_get_quote_eod_mixed_sources_never_double_adjust(mkt: Mkt) -> None:
    sid = mkt.ids["RELIANCE"]
    with mkt.duck() as c:  # raw bhavcopy before the split, Yahoo (already adjusted) after it
        write_prices(c, [bar(sid, date(2026, 1, 1), "1000"),
                         bar(sid, date(2026, 1, 2), "110", source="yahoo")], [
            CorpAction(security_id=sid, ex_date=date(2026, 1, 2), kind="split", ratio=D("10"),
                       source="nse_corp_actions"),
        ])  # fmt: skip
    d = (await call("market", "get_quote_eod", security="RELIANCE"))["data"]
    assert d["change_pct"] == pytest.approx(10.0)


async def test_get_corporate_actions_filters_since(mkt: Mkt) -> None:
    sid = mkt.ids["RELIANCE"]
    with mkt.duck() as c:
        write_prices(c, [], [
            CorpAction(security_id=sid, ex_date=date(2025, 1, 10), kind="bonus", ratio=D("1"),
                       source="nse_corp_actions"),
            CorpAction(security_id=sid, ex_date=date(2025, 9, 1), kind="dividend", amount=D("5.5"),
                       source="nse_corp_actions"),
        ])  # fmt: skip
    out = await call("market", "get_corporate_actions", security="RELIANCE", since="2025-06-01")
    acts = out["data"]["actions"]
    assert [(a["ex_date"], a["kind"], a["amount"]) for a in acts] == [
        ("2025-09-01", "dividend", 5.5)
    ]
    assert (
        len((await call("market", "get_corporate_actions", security="RELIANCE"))["data"]["actions"])
        == 2
    )


async def test_resolve_security_returns_single_id_or_ranked_candidates(mkt: Mkt) -> None:
    one = await call("market", "resolve_security", query="INE002A01018")
    assert one["data"]["security_id"] == mkt.ids["RELIANCE"] and one["data"]["matched_by"] == "isin"
    assert one["data"]["security"]["symbol"] == "RELIANCE"
    many = await call("market", "resolve_security", query="Tata")
    assert many["data"]["security_id"] is None
    assert {c["symbol"] for c in many["data"]["candidates"]} >= {"TATAMOTORS"}
    nothing = await call("market", "resolve_security", query="zzzzqq")
    assert nothing["data"]["candidates"] == [] and nothing["data"]["security_id"] is None
    us = await call("market", "resolve_security", query="AAPL", market="US")
    assert us["data"]["security"]["currency"] == "USD"


async def test_ambiguous_security_arg_error_lists_candidates(mkt: Mkt) -> None:
    with pytest.raises(ToolError, match="ambiguous.*TATAMOTORS"):
        await call("market", "get_prices", security="Tata", start="2026-01-01", end="2026-01-02")
    with pytest.raises(ToolError, match="no security matches"):
        await call("market", "get_quote_eod", security="zzzzqq")


async def test_get_last_trading_day_nse_in_ist(mkt: Mkt, monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import UTC, datetime

    out = await call("market", "get_last_trading_day")
    assert out["data"]["last_trading_day"] == "2026-01-05" and out["data"]["market"] == "NSE"
    for utc_hour, expect in ((9, "2026-01-02"), (10, "2026-01-05")):  # close is 15:30 IST = 10:00Z
        monkeypatch.setattr(
            common, "now", lambda h=utc_hour: datetime(2026, 1, 5, h, 0, tzinfo=UTC)
        )
        got = await call("market", "get_last_trading_day", market="NSE")
        assert got["data"]["last_trading_day"] == expect
    monkeypatch.setattr(common, "now", lambda: datetime(2026, 1, 27, 12, 0, tzinfo=UTC))
    assert (await call("market", "get_last_trading_day"))["data"][
        "last_trading_day"
    ] == "2026-01-27"
    monkeypatch.setattr(common, "now", lambda: datetime(2026, 1, 26, 12, 0, tzinfo=UTC))  # holiday
    assert (await call("market", "get_last_trading_day"))["data"][
        "last_trading_day"
    ] == "2026-01-23"


async def test_get_last_trading_day_nyse_good_friday(
    mkt: Mkt, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime

    monkeypatch.setattr(common, "now", lambda: datetime(2024, 3, 31, 12, 0, tzinfo=UTC))  # Sunday
    out = await call("market", "get_last_trading_day", market="NYSE")
    assert out["data"]["last_trading_day"] == "2024-03-28"  # Good Friday 29 March is a holiday


async def test_unknown_nse_year_error_names_config_key(
    mkt: Mkt, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime

    monkeypatch.setattr(common, "now", lambda: datetime(2031, 1, 6, 12, 0, tzinfo=UTC))
    with pytest.raises(ToolError, match="market.nse_holidays"):
        await call("market", "get_last_trading_day")
    with pytest.raises(ToolError, match="market must be one of"):
        await call("market", "get_last_trading_day", market="LSE")


async def test_payload_survives_redaction_numbers_and_names_intact(mkt: Mkt) -> None:
    sid = mkt.ids["RELIANCE"]
    with mkt.duck() as c:
        upsert_bars(c, [bar(sid, date(2026, 1, 2), "2987.65")])
    out = await call(
        "market", "get_prices", security="RELIANCE", start="2026-01-01", end="2026-01-05"
    )
    b = out["data"]["bars"][0]
    assert b["close"] == 2987.65 and b["volume"] == 1234567 and "[REDACTED]" not in str(out)
    assert out["data"]["isin"] == "INE002A01018" and out["data"]["name"].startswith("Reliance")


async def test_payload_scan_has_no_pii_patterns(mkt: Mkt) -> None:
    sid = mkt.ids["RELIANCE"]
    with mkt.duck() as c:
        upsert_bars(c, series(sid, 30, date(2026, 1, 1)))
    out = await call(
        "market", "get_prices", security="RELIANCE", start="2026-01-01", end="2026-02-28"
    )
    assert scan_text(str(out)) == []


async def test_read_while_writer_active_raises_ingest_in_progress(mkt: Mkt) -> None:
    common._ready.add(mkt.data.resolve())
    with duckdb.connect(str(mkt.data / "nivesh.duckdb")):  # an ingest holds the write lock
        with pytest.raises(ToolError, match="ingest in progress"):
            await call("market", "get_quote_eod", security="RELIANCE")
