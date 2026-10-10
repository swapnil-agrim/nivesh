import json
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import httpx
import pytest

from nivesh_adapters.market_ingest import ingest_prices
from nivesh_adapters.prices_in import IndiaPrices
from nivesh_adapters.prices_us import UsPrices
from nivesh_adapters.quality import DataQualityError
from nivesh_core.config import MarketSettings, Settings
from nivesh_core.db import init_stores
from nivesh_core.db.duck import open_duck
from nivesh_core.db.sqlite import open_sqlite
from nivesh_core.errors import CalendarUnknown, NiveshError
from nivesh_core.market_store import bars_by_source, get_bars
from nivesh_core.security_master import SecurityMaster, SecurityRow, build_master
from tests.market_fx import Feed, mrow

FX = Path(__file__).resolve().parents[1] / "fixtures" / "market"
D = Decimal
IN_DAYS = [date(2026, 1, 22), date(2026, 1, 23), date(2026, 1, 27), date(2026, 1, 28)]
US_DAYS = [date(2024, 3, 27), date(2024, 3, 28), date(2024, 4, 1), date(2024, 4, 2)]


@dataclass
class Env:
    sql: sqlite3.Connection
    duck: duckdb.DuckDBPyConnection
    settings: Settings
    master: SecurityMaster

    def sec(self, symbol: str) -> SecurityRow:
        return self.master.by_symbol(symbol)[0]


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    init_stores(tmp_path)
    sql = open_sqlite(tmp_path / "nivesh.sqlite")
    build_master(
        sql,
        [
            mrow("RELIANCE", isin="INE002A01018"),
            mrow("AAPL", "NASDAQ", market="US", currency="USD"),
            mrow("NIFTY 50", name="NIFTY 50", asset_class="index"),
            mrow("SCHEMEX", "AMFI", isin="INF000A01011", asset_class="mf"),
        ],
        [],
    )
    duck = open_duck(tmp_path / "nivesh.duckdb")
    settings = Settings(
        data_dir=str(tmp_path), market=MarketSettings(nse_holidays={2026: [date(2026, 1, 26)]})
    )
    yield Env(sql, duck, settings, SecurityMaster(sql))
    duck.close()
    sql.close()


def adapters(feed: Feed) -> dict[str, Callable[[], Any]]:
    c = httpx.Client(transport=httpx.MockTransport(feed))
    return {"india": lambda: IndiaPrices(c), "us": lambda: UsPrices(c)}


def run_in(env: Env, feed: Feed, start: date = IN_DAYS[0], end: date = IN_DAYS[-1], **kw: Any):  # type: ignore[no-untyped-def]
    return ingest_prices(
        env.duck, env.settings, env.sec("RELIANCE"), start, end, **adapters(feed), **kw
    )


def india_feed(**kw: Any) -> Feed:
    return Feed(closes=dict(zip(IN_DAYS, [100.0, 101.0, 102.0, 103.0], strict=True)), **kw)


def test_ingest_prices_in_stores_bars_actions_adj_close_and_flags(env: Env) -> None:
    feed = india_feed(actions=[{"subject": "Dividend - Rs 2 Per Share", "exDate": "27-Jan-2026"}])
    feed.yahoo = {**feed.closes, IN_DAYS[1]: 101.0 * 1.02}  # one 2 percent disagreement
    rep = run_in(env, feed)
    assert rep.flags == {"ok": 3, "mismatch": 1} and rep.gaps == [] and rep.failed == []
    assert rep.actions == 1
    bars = get_bars(env.duck, rep.security_id)
    assert [b.source for b in bars] == ["nse_bhavcopy"] * 4
    assert bars[1].flag == "mismatch" and bars[0].flag == "ok"
    # dividend 2 on ex 27-Jan: factor (101.0*1.0... prev close 101) -> (101 - 2) / 101
    assert bars[0].adj_close == (D("100.0") * (D("101.0") - 2) / D("101.0")).quantize(D("0.000001"))
    assert bars[3].adj_close == D("103.000000")
    assert set(bars_by_source(env.duck, rep.security_id)) == {"nse_bhavcopy", "yahoo"}


def test_ingest_prices_in_republic_day_is_not_a_gap(env: Env) -> None:
    rep = run_in(env, india_feed())
    assert rep.gaps == []  # 24/25 weekend, 26 Republic Day


def test_ingest_reports_real_gaps_with_dates(env: Env) -> None:
    feed = india_feed()
    del feed.closes[IN_DAYS[2]]
    rep = run_in(env, feed)
    assert rep.gaps == [IN_DAYS[2]]


def test_ingest_prices_us_holiday_is_not_a_gap(env: Env) -> None:
    feed = Feed(yahoo=dict(zip(US_DAYS, [173.31, 171.48, 170.03, 168.84], strict=True)))
    feed.stooq = (FX / "stooq_aapl.csv").read_text()
    rep = ingest_prices(
        env.duck, env.settings, env.sec("AAPL"), US_DAYS[0], US_DAYS[-1], **adapters(feed)
    )
    assert rep.gaps == [] and rep.flags == {"ok": 4}  # 2024-03-29 is Good Friday
    assert [b.source for b in get_bars(env.duck, rep.security_id)] == ["yahoo"] * 4


def test_second_provider_none_disables_cross_check(env: Env) -> None:
    env.settings = env.settings.model_copy(
        update={"market": env.settings.market.model_copy(update={"us_secondary": "none"})}
    )
    feed = Feed(yahoo=dict(zip(US_DAYS, [11.0, 12.0, 13.0, 14.0], strict=True)))
    rep = ingest_prices(
        env.duck, env.settings, env.sec("AAPL"), US_DAYS[0], US_DAYS[-1], **adapters(feed)
    )
    assert rep.flags == {"single_source": 4} and not any("stooq" in c for c in feed.calls)


def test_ingest_index_uses_index_file(env: Env) -> None:
    feed = india_feed()
    rep = ingest_prices(
        env.duck, env.settings, env.sec("NIFTY 50"), IN_DAYS[0], IN_DAYS[1], **adapters(feed)
    )
    bars = get_bars(env.duck, rep.security_id)
    assert [b.source for b in bars] == ["nse_indices"] * 2 and bars[0].close == D("2")
    assert any("%5ENSEI" in c or "^NSEI" in c for c in feed.calls)


def test_ingest_uses_cached_fetch_with_price_ttl_and_redact_false_and_does_not_refetch_inside_ttl(
    env: Env,
) -> None:
    feed = india_feed()
    run_in(env, feed)
    n = len(feed.calls)
    run_in(env, feed)
    assert len(feed.calls) == n  # all served from cache
    row = env.duck.execute(
        "SELECT payload FROM cache_entry WHERE adapter = 'prices_in' AND payload LIKE '%TradDt%'"
    ).fetchone()
    assert row and "RELIANCE" in json.loads(row[0])  # stored unredacted


def test_cached_hit_and_fresh_fetch_produce_identical_bars(env: Env) -> None:
    feed = india_feed()
    run_in(env, feed)
    first = get_bars(env.duck, env.sec("RELIANCE").id)
    run_in(env, feed)
    assert get_bars(env.duck, env.sec("RELIANCE").id) == first


def test_ingest_stale_fallback_marks_result_stale_when_source_fails_after_first_success(
    env: Env,
) -> None:
    feed = india_feed()
    assert run_in(env, feed).stale is False
    feed.fail = {"nseindia", "yahoo"}
    rep = run_in(env, feed, refresh=True)
    assert rep.stale is True and rep.gaps == []


def test_failed_source_without_cache_is_reported_not_fatal(env: Env) -> None:
    feed = india_feed(fail={"yahoo"})
    rep = run_in(env, feed)
    assert rep.flags == {"single_source": 4} and any("yahoo" in f for f in rep.failed)


def test_ingest_is_idempotent_on_rerun(env: Env) -> None:
    run_in(env, india_feed())
    n = env.duck.execute("SELECT count(*) FROM price_bar").fetchone()
    run_in(env, india_feed(), refresh=True)
    assert env.duck.execute("SELECT count(*) FROM price_bar").fetchone() == n


def test_ingest_rejects_bad_bar_without_partial_write(env: Env) -> None:
    feed = india_feed()
    feed.closes[IN_DAYS[3]] = -5.0
    with pytest.raises(DataQualityError):
        run_in(env, feed)
    assert env.duck.execute("SELECT count(*) FROM price_bar").fetchone() == (0,)
    assert env.duck.execute(
        "SELECT count(*) FROM cache_entry WHERE payload LIKE '%-5.0%'"
    ).fetchone() == (0,)  # bad data is never cached


def test_adj_close_recomputed_after_new_action(env: Env) -> None:
    run_in(env, india_feed(), IN_DAYS[0], IN_DAYS[1])
    sid = env.sec("RELIANCE").id
    assert get_bars(env.duck, sid)[0].adj_close == D("100.000000")
    feed = india_feed(actions=[{"subject": "Bonus 1:1", "exDate": "27-Jan-2026"}])
    run_in(env, feed, IN_DAYS[2], IN_DAYS[3])
    assert get_bars(env.duck, sid)[0].adj_close == D("50.000000")


def test_ingest_split_does_not_false_flag_across_split(env: Env) -> None:
    feed = india_feed(
        actions=[{"subject": "Face Value Split From Rs 10/- To Rs 5/-", "exDate": "27-Jan-2026"}]
    )
    feed.yahoo = {d: (c / 2 if d < IN_DAYS[2] else c) for d, c in feed.closes.items()}
    assert run_in(env, feed).flags == {"ok": 4}


def test_ingest_rejects_bad_range_mf_and_unknown_calendar_year(env: Env) -> None:
    feed = india_feed()
    with pytest.raises(NiveshError, match="after end"):
        run_in(env, feed, IN_DAYS[1], IN_DAYS[0])
    with pytest.raises(NiveshError, match="mutual fund"):
        ingest_prices(
            env.duck, env.settings, env.sec("SCHEMEX"), IN_DAYS[0], IN_DAYS[1], **adapters(feed)
        )
    with pytest.raises(CalendarUnknown, match="nse_holidays"):
        run_in(env, feed, date(2027, 1, 4), date(2027, 1, 5))


def test_adapter_data_is_json_native(env: Env) -> None:
    feed = india_feed(actions=[{"subject": "Bonus 1:1", "exDate": "27-Jan-2026"}])
    feed.stooq = (FX / "stooq_aapl.csv").read_text()
    c = httpx.Client(transport=httpx.MockTransport(feed))
    results = [
        IndiaPrices(c).fetch(resource="nse_bhavcopy", day="2026-01-22").data,
        IndiaPrices(c).fetch(resource="indices", day="2026-01-22").data,
        IndiaPrices(c).fetch(resource="corp_actions", symbol="RELIANCE", start="a", end="b").data,
        IndiaPrices(c)
        .fetch(resource="yahoo", symbol="X.NS", start="2026-01-22", end="2026-01-28")
        .data,
        UsPrices(c)
        .fetch(resource="stooq", symbol="AAPL", start="2024-03-27", end="2024-04-02")
        .data,
    ]
    for data in results:
        assert json.loads(json.dumps(data)) == data


def test_ingest_bonus_from_nse_and_split_from_yahoo_adjusts_once(env: Env) -> None:
    feed = india_feed(
        actions=[{"subject": "Bonus 1:1", "exDate": "27-Jan-2026"}],
        splits=((2, 2, 1),),
    )
    rep = run_in(env, feed)
    bars = get_bars(env.duck, rep.security_id)
    assert bars[0].adj_close == D("50.000000") and bars[3].adj_close == D("103.000000")
